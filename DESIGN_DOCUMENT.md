# South East Water integration – design document

Version 2.0 (pure-HTTP rewrite). Last updated 2026-09-14.

This document records *why* the integration is built the way it is. The README covers how to use
it; the module docstrings cover how each piece works.

---

## 1. Goals

| # | Goal | Outcome |
|---|---|---|
| G1 | Daily mains water usage from `my.southeastwater.com.au` into Home Assistant. | Long-term statistics for the Energy dashboard plus live sensors. |
| G2 | No browser automation. | Version 1.x drove the portal through Browserless/Puppeteer. Every step of the portal flow was proven to be plain HTTP, so 2.0 uses `aiohttp` only. |
| G3 | Cope with the portal's mandatory one-time code. | Code required once at setup and again only when the session dies; sessions survive restarts. |
| G4 | Cope with the portal publishing readings late. | Every poll re-fetches and re-imports a trailing window. |
| G5 | Keep 1.x history. | Same statistic id and row timestamps, so existing rows are overwritten, never duplicated. |

Non-goals: recycled water (author has no such meter), Yarra Valley Water (same Salesforce backend but
untestable), YAML configuration, PyPI packaging.

## 2. Decisions

| ID | Decision | Rationale |
|---|---|---|
| D1 | Transport is `aiohttp`; the client lives inside the integration (`sew_client.py`) rather than on PyPI. | Personal HACS integration; one less release pipeline. The client stays framework-free so it can be tested offline. |
| D2 | Hourly resolution in long-term statistics (the portal's finest); daily in the sensors. | The portal returns 24 hourly readings per day at no extra cost, and hourly rows make every Energy dashboard view accurate. The sensors stay daily because that is the cadence at which data arrives. Changed from daily-only on 2026-09-15. |
| D3 | First run imports 90 days; every poll re-imports the last 30 days. | 90 days fits one batched request. 30 days comfortably covers the portal's publication lag and corrections. |
| D4 | Statistics **and** sensors. | A sensor cannot hold retroactive history; a statistic cannot drive automations or cards. |
| D5 | Daily poll pinned to 02:00 local when the interval is the default (1440 min). | The previous day's readings are usually published by then; a fixed interval from HA start time would drift. |
| D6 | Zero-reading days are shown as "not published" by the sensors but imported as 0 L. | The portal returns 24 zeros both for unpublished days and for old dates; the trailing re-import corrects a 0 once real data appears, so nothing is lost by importing it. |
| D7 | Session cookies persisted in the config entry and refreshed after every poll. | This is what makes MFA a once-per-session event instead of once-per-poll, and what lets sessions survive restarts. |
| D8 | Username and password stored in the config entry. | HA has no encrypted vault for integrations; `.storage` (mode 0600) is the standard. Storing the password means re-authentication needs only a new code. The password alone cannot open a session because the portal always demands a code. |
| D9 | Re-authentication is HA's standard reauth flow: choose channel → enter code. | No custom notifications; the repair card is what users already know. |
| D10 | Statistic rows are stamped at local midnight + *h* hours, computed in UTC; daily sensor is `VOLUME`/`MEASUREMENT`. | UTC arithmetic keeps the 24 rows distinct on daylight-saving days (wall-clock arithmetic would make two hours coincide when clocks go forward). Rows imported by 1.x sit at 11:00 local and are overwritten by hour 11 when their day is re-imported, so the running `sum` stays continuous. |
| D11 | Config-entry `VERSION = 2`; 1.x entries are refused, not migrated. | 1.x entries hold Browserless settings and no session; there is nothing to migrate. Statistics are unaffected. |
| D12 | Two test layers: the client offline with `aioresponses` (no Home Assistant needed) and the integration under `pytest-homeassistant-custom-component` with a scripted `FakeClient` and an in-memory recorder. Coverage is held at ≥ 95 % in CI; the integration declares the Platinum quality tier and tracks every rule in `quality_scale.yaml`. | The client is where the protocol risk is; the HA layer is where the statistics arithmetic and flow wiring can silently go wrong (the tests found an off-by-one bucket in the running-total lookup). |
| D13 | Billing-account ID, meter record ID and meter serial are not entities and not on the device card. | They identify the customer's account; as entities they would be persisted in the recorder and appear in every state dump. They stay in the config entry (needed for API calls), are redacted from diagnostics, and are logged once at debug level on startup for checking. |
| D14 | Throttling is detected from Salesforce's Apex error text ("concurrent requests limit exceeded"), plus HTTP 429/503 with `Retry-After` for good measure, and surfaced as `SewBusyError` → `UpdateFailed(retry_after=15 min)`. Usage batches are 30 actions and the daily poll carries up to 10 min of random jitter. | The core Salesforce platform does not use 429 for Aura requests; the limit that applies is the org-wide cap of 10 synchronous Apex requests running > 5 s, shared by every portal user. Keeping each batch under ~3 s stays out of that pool, jitter avoids installations colliding, and a short retry beats waiting for the next day. |
| D15 | `clientOutOfSync` reloads the home page for a fresh Aura context and retries once. | It means the cached `fwuid` is stale after a Salesforce release, not that the session is dead; treating it as an auth failure would demand a needless one-time code. |
| D16 | A keep-alive loads `/s/` every `KEEPALIVE_MINUTES` (30) between polls, skipped when the portal was touched more recently than that; a dead session starts reauth directly. | The portal's idle timeout is between 2 h and 4 h (measured 2026-09-16: alive after 2.0 h idle, dead after 4.0 h; Salesforce offers 2 h or 4 h at that range, so it is treated as 2 h). Daily polls are 24 h apart, so without it every poll would need a new code. 30 min is one page load, tolerates three missed ticks before the 2 h mark, and is the interval proven over 22 h. |

## 3. Architecture

```
┌──────────────┐   ConfigFlow (setup / reauth / options)
│  config_flow │──────────────┐
└──────────────┘              │ cookies, ids, credentials
                              ▼
┌──────────────┐   ┌───────────────────┐   ┌────────────────────┐
│  __init__    │──▶│  SewCoordinator   │──▶│ recorder statistics│  sew_water:water_usage_mains
│  (setup,     │   │  (poll, import,   │   └────────────────────┘
│   services)  │   │   cookie refresh) │──▶ SewData ──▶ sensor entities
└──────────────┘   └─────────┬─────────┘
                             │ SewClient (aiohttp session with cookie jar)
                             ▼
                    my.southeastwater.com.au
```

| Module | Responsibility |
|---|---|
| `sew_client.py` | Portal protocol only. No HA imports. Raises `SewAuthError`, `SewConnectionError`, `SewProtocolError`. |
| `config_flow.py` | Setup and reauth steps; options flow. Owns a private HTTP session for the duration of the flow. |
| `coordinator.py` | `DataUpdateCoordinator[SewData]`; window selection, statistics import, cookie persistence, scheduling. |
| `sensor.py` | `CoordinatorEntity` sensors described declaratively (`SewSensorDescription.value_fn`). Usage values only — account identifiers are never entities (D13). |
| `diagnostics.py` | Config-entry diagnostics with credentials, cookies and record ids redacted. |
| `const.py` | Keys, defaults, statistic id, timing constants. |

## 4. Portal protocol

Verified against captures taken 2026-09-13. The portal is Salesforce Experience Cloud; the pieces
in play are the Aura RPC endpoint, a `frontdoor.jsp` session hand-off and a Visualforce/RichFaces
login flow for the one-time code.

| Step | Request | Notes |
|---|---|---|
| 1 | `GET /s/login/` | Page embeds the Aura context (`fwuid`, loaded-app hash) URL-encoded inside `/s/sfsites/l/{…}/bootstrap.js` script URLs. App is `siteforce:loginApp2`, token is `null`. |
| 2 | `POST /s/sfsites/aura` action `apex://cm_LoginAURA/ACTION$login` `{username, password, startUrl:"/"}` | Good credentials → `returnValue` is a `/secur/frontdoor.jsp?sid=…` URL. Bad credentials → `state` is still `SUCCESS`, `returnValue` is the error text. |
| 3 | `GET` the frontdoor URL | Sets `sid`, `sid_Client`, `inst`, `oid`, `__Secure-has-sid` …; 302 → `/apex/PortalMFALoginFlow?retURL=/`. |
| 4 | Parse the MFA form | `form id="j_id0:mfaForm"`, radio `channel` = `Email` / `SMS`, submit `…:j_id26` = "Send code", four `com.salesforce.visualforce.ViewState*` hidden fields. |
| 5 | `POST /PortalMFALoginFlow` with `AJAXREQUEST=_viewRoot`, form id, `channel`, `…:channelRadio`, ViewState×4, send button | Response is a full XHTML page with a **new ViewState**, `otpBox1..6`, `…:otpHidden` and a "Verify" submit. The new ViewState must be carried forward. |
| 6 | `POST /PortalMFALoginFlow` with `otpHidden`, `otpBox1..6`, new ViewState×4, verify button | Success = `Location: /s/` header (the client also accepts `<meta name="Location">`). Failure = the same form again with an error span. |
| 7 | `GET /s/` | Every HTML page load issues a fresh Aura CSRF token in a `__Host-ERIC_PROD-*` cookie; the page names that cookie in its `"eikoocnekot"` bootstrap setting. Context app is `siteforce:communityApp`. A dead session redirects to `/s/login/`. |
| 8 | `POST /s/sfsites/aura` action `aura://ApexActionController/ACTION$execute`, class `MysewUsageBillingGraphController`, method `getUsageData`, params `{baId, meterId, dateFrom, dateTo, resolution:"hourly"}` | One action per day; 30 actions per POST (120 verified to work, 30 keeps each request under the 5 s long-running threshold, see D14). Returns `[{apiDate, readings[24], serialNo, message, status}]`; the client sums the 24 hourly litres. `resolution:"daily"` was never captured and is not used. |
| 9 | ID discovery, mirroring the portal's own start-up calls: `apex://cm_AccountBillingUsageAURA/ACTION$retrieveBillingAccounts` with `fieldsToRetrieve` (we ask for `Id, Name, Status__c, Property__c, Property__r.Digital_Meter__c`) → first `Billing_Account__c`; then `…$retrieveSObject` with `objectToReturn=Meter_Details__c`, `fieldsToRetrieve=Id, Name, Is_Digital__c, Digital_Meter__c, Property__c`, `whereClause=Property__c IN ('<Property__c>') AND (Is_Digital__c = true OR Digital_Meter__c = true)` → first meter (`Name` is the serial). Both return values are JSON **strings** that must be decoded. | `baId`/`meterId` are Salesforce record ids (`a08…`, `a1K…`), not the account number or meter serial. The meter is linked to the *property*, not the billing account. Captured live 2026-09-15. |

Every Aura POST sends `aura.context` (mode, app, fwuid, loaded), `aura.pageURI`, `aura.token` and the
`message` JSON, form-encoded. Error mapping: `exceptionEvent` naming `invalidSession` → `SewAuthError`;
`clientOutOfSync` → reload `/s/` and retry once (D15); an action `state: ERROR` or exception message
matching *concurrent requests limit / request limit exceeded / too many requests*, or HTTP 429/503 →
`SewBusyError` with any `Retry-After` seconds (D14); other errors → `SewProtocolError`.

**Throttling on this platform.** Experience Cloud/Aura does not return 429 or `Retry-After` (those
belong to Salesforce's Commerce and Marketing Cloud APIs). What applies is the org-wide *concurrent
long-running Apex* limit: at most 10 synchronous requests running longer than 5 s, across all users of
the org, after which every Apex request is refused with the error text above until one finishes.
Site page-view and login allocations are administrative and give no client-side signal; session reuse
keeps our contribution to one page view per day and one login per session.

## 5. Session lifecycle

```
setup ──▶ login+MFA ──▶ cookies saved in entry.data
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         │
   poll: import cookies ─▶ GET /s/ ─▶ token ─▶ usage ─▶ export cookies ─▶ entry.data
              │ (redirect to login / invalidSession)
              ▼
   ConfigEntryAuthFailed ──▶ HA reauth card ──▶ channel ─▶ code ─▶ cookies replaced ─▶ reload
```

- The coordinator's session is created per config entry with its own cookie jar
  (`async_create_clientsession(hass, cookie_jar=CookieJar())`) so portal cookies never mix with HA's
  shared session, and is closed on unload.
- `export_cookies` filters the jar to the portal domain and produces a JSON-serialisable list.
  Cookies are written back only when they changed, to avoid needless entry updates.
- Measured lifetime: a session kept alive with a request every 30 minutes survived 22 h with no
  failures and no absolute cap was seen. Idle, it was still alive after 2.0 h and dead after 4.0 h
  (staircase 2 h / 4 h / 8 h / 12 h, 2026-09-15/16), so the idle timeout is > 2 h and ≤ 4 h.
- Keep-alive (D16): `async_track_time_interval` every `KEEPALIVE_MINUTES`, registered from
  `async_setup_entry` and cancelled with the entry. It calls `async_is_alive()` (one `GET /s/`), writes
  back refreshed cookies, and skips when the entry is not loaded, a reauth flow is already open, or a
  poll/service call touched the portal within the interval (`coordinator.last_contact`). A dead session
  calls `entry.async_start_reauth`; transient errors are logged at debug and left to the next tick.

## 6. Statistics design

- One external statistic, `sew_water:water_usage_mains`, `has_sum=True`, `mean_type=NONE`,
  `unit_class="volume"`, unit litres.
- One row per **hour**: start = UTC instant of local midnight + *h* hours (h = 0..23), `state` = that
  hour's litres, `sum` = running total. On the 23-hour daylight-saving day the 24th reading, which
  would land on the next day's midnight, is folded into hour 22's row. A day the portal returns
  without hourly readings becomes a single midnight row carrying the day total.
- Re-import rule: the running total is rebuilt from the newest row *before* the window
  (`statistics_during_period`, hourly buckets, looking back up to ten years), then every hour in the
  window is written with `async_add_external_statistics` (720 rows per daily poll, 2160 on first run). Rows are keyed by start time, so the import overwrites in
  place and stays consistent even when an earlier day inside the window changes.
- First run is detected by the absence of any row (`get_last_statistics`), which is why an upgrade
  from 1.x takes the 30-day path rather than re-backfilling.
- `total_usage` sensor = the running total after the last imported day. It is `total_increasing`
  for card/automation use only; the README tells users to point the Energy dashboard at the
  statistic, because a once-a-day sensor would be attributed to the poll minute.

## 7. Scheduling

`SewCoordinator._interval` recomputes `update_interval` after every poll:

- interval == 1440 → `next 02:00 local − now` plus 0–10 min of random jitter (never a fixed 24 h, so it does not drift, and installations do not collide);
- any other value → `timedelta(minutes=interval)` (minimum 60, enforced by the options selector).

A `SewBusyError` from the poll becomes `UpdateFailed(retry_after=…)`: the portal's `Retry-After` if it sent one, else 15 minutes; the coordinator honours it for the next attempt and the daily schedule resumes after.

`import_from_date` bypasses the window and imports `start..yesterday` in the same code path, then
pushes the result to entities with `async_set_updated_data`.

## 8. Security and privacy

- Credentials, cookies and Salesforce record ids are redacted from diagnostics; identifiers are never entities (D13).
- Nothing sensitive is logged; the only debug line at auth time logs the portal's *rejection text*,
  never inputs.
- The client sends a fixed browser user agent; no third-party services are contacted.
- The probe tooling that captured the protocol lives outside the repository.

## 9. Open items

| Item | Status |
|---|---|
| Wrong-code response text and whether the a4j redirect arrives as a header or a meta tag. | Redirect verified from the 2026-09-15 capture: the verify POST answers 200 with a `Location` header (the client checks the header first, meta tag second). Wrong-code text still unverified. |
| `frontdoor.jsp` answers 200 with a script redirect, not a 302. | **Resolved 2026-09-17:** found on the first live run of the pure-HTTP login (b3). The client now follows the `location.replace(...)` URL from that page before looking for the MFA form. |
| Session idle timeout. | **Resolved 2026-09-16:** alive after 2.0 h idle, dead after 4.0 h idle, so the timeout is > 2 h and ≤ 4 h (Salesforce's 2 h default or 4 h). `KEEPALIVE_MINUTES` stays at 30 (D16): the 2 h result is at the boundary, so the margin matters more than halving one page load an hour. |
| First end-to-end run in a real Home Assistant. | Pending a one-time code from the account owner. |

## 10. Testing

`tests/test_sew_client.py` (51 cases, offline, `aioresponses`) covers: login success / bad
credentials / no-MFA / maintenance page / 5xx / network failure; MFA field names, ViewState carry-
forward, wrong code with retry, code length, step ordering; session alive / dead / token-less and
cookie round-trip; id discovery; usage summing, batching across 30-action pages, unavailable days,
`invalidSession`, action errors and non-JSON responses; throttling via Apex error text, HTTP 429 with
`Retry-After`, 503 with an unparseable `Retry-After`; `clientOutOfSync` resync success, repeated
failure, and dead session; plus the protocol edge cases (missing MFA form, ViewState or buttons,
malformed Aura contexts, HTTP 401/5xx, cookie flag round-trip, single-object usage payloads).

`tests/ha/` (62 cases, `pytest-homeassistant-custom-component`, in-memory recorder, scripted
`FakeClient`) covers: the config flow end to end (user → channel → code, every error and abort path,
reauth with and without a password change, reconfigure, options); entry setup, retry, reauth trigger, unload,
v1 refusal with its repair issue, and both services; the coordinator's 90-day backfill, 30-day trailing window, running-total
arithmetic across re-imports, hourly row layout including the daylight-saving fold, 02:00 scheduling,
throttling back-off and the keep-alive (registration, skip rules, dead session → reauth, transient
errors, cancellation on unload); the three sensors' values,
attributes, device grouping and unavailability; and diagnostics redaction.

Coverage is 99 % overall and every module is above the 95 % threshold, enforced with
`--cov-fail-under=95` in CI.

Tooling: `ruff` (120 columns, Google docstrings, HA import order), `mypy --strict`, `pytest` with
`asyncio_mode = auto`. Configuration in `pyproject.toml`.

## 11. History

| Version | Summary |
|---|---|
| 1.x (`v0-browserless` tag, `archive/browserless` branch) | Browserless/Puppeteer script logs in, scrapes the Aura token, batches usage calls. Broke when the portal added mandatory MFA and changed its Aura bootstrap. |
| 2.0 | This document. |
