# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- *Daily usage* used state class `measurement`, which Home Assistant rejects for a volume sensor
  and logged a warning at startup (#1). It is now `total` with `last_reset` at the start of the
  reading day, so its long-term statistics add each day's figure instead of the day-to-day change.
- The config flow tried to close its private HTTP session with `close()`, which Home Assistant
  turns into a warning ("closes the Home Assistant aiohttp session") without closing anything, so
  the session leaked (#1). The flow now detaches the session; entry unload leaves the session to
  Home Assistant, which detaches it itself.

## [2.0.0b4] — 2026-09-17

Fourth beta. Fixes the login failing at the portal's redirect step, found on the first live run of b3.

### Fixed
- Login failed with "MFA form not found on page" on every account. The portal's `frontdoor.jsp`
  step answers with an HTML page whose script performs the redirect to the one-time-code page, not
  with an HTTP redirect; the client took that page for the code page. It now follows the scripted
  redirect. This was the first live run of the pure-HTTP login; the browser-based probes had hidden it.

## [2.0.0b3] — 2026-09-16

Third beta. Stops an unexplained portal login response from being reported as bad
credentials (#1), checks the email address before contacting the portal, and records the
measured session idle timeout.

### Fixed
- Setup reported "The email address or password was not accepted" when the portal's login call
  returned nothing at all (no redirect and no rejection text), which is not a credentials failure
  (#1). That case now shows its own message asking for a debug log, the full portal response is
  logged at debug level with session IDs masked, and a login redirect delivered as an Aura event
  rather than a return value is followed.

### Added
- The email address is checked for a plausible format before the portal is contacted; a typo shows
  an error on the field instead of a round trip and a misleading rejection.

### Changed
- Documentation: the portal's idle timeout has been measured at between 2 and 4 hours (alive after
  2 h idle, dead after 4 h). The 30-minute keep-alive is unchanged; the README and design document
  now give the measured figure instead of "about a day".

## [2.0.0b2] — 2026-09-15

Second beta. Fixes account discovery against the live portal, keeps the session alive between polls,
and stores statistics hourly. Reaches Platinum on the integration quality scale.

### Added
- Integration quality scale declared as **Platinum** (`quality_scale.yaml`, `manifest.json`); every
  Bronze–Platinum rule is recorded as done or exempt with a reason.
- **Reconfigure** flow: change the portal password or force a fresh login from the entry's menu
  without deleting it.
- Repair issue for version 1 config entries explaining that they must be removed and re-added.
- Translated icons (`icons.json`) for the sensors and services, and translated messages for every
  error the coordinator and services raise.
- README sections on supported meters, use cases, automation examples, known limitations and
  troubleshooting.
- Session keep-alive: the portal home page is loaded every 30 minutes between polls so the stored
  session does not hit the portal's idle timeout (measured at 24 hours, shorter than the daily poll
  gap). A dead session now triggers re-authentication immediately instead of at the next poll.
- Integration-level test suite (`tests/ha/`, `pytest-homeassistant-custom-component`) covering the
  config flow, setup/unload, services, coordinator, sensors and diagnostics. Coverage is 99 % and
  CI fails below 95 %.
- `data_description` help text on every setup, reauth and options field.
- `PARALLEL_UPDATES` declared on the sensor platform.

### Changed
- Long-term statistics are now **hourly** (24 rows per day) instead of one row per day, so the
  Energy dashboard's hourly view shows real usage. Days imported by earlier versions keep their single
  11:00 row until they are re-imported (automatically within the 30-day window, or via
  `sew_water.import_from_date`); the running total is unaffected.
- *Last reading date* is now a diagnostic entity and disabled by default; the same date remains the
  `reading_date` attribute of *Daily usage*.

### Fixed
- Account discovery now mirrors the portal's own calls (`retrieveBillingAccounts` with a field list,
  then meters by property with the digital-meter filter) and decodes the JSON-string return values;
  the previous guess returned no billing account against the live portal.
- Running total could omit the first day of a re-imported window: the lookup for the last statistic
  before the window used day buckets, which swallowed that day's row. It now uses hourly buckets.

## [2.0.0b1] — 2026-09-14

First beta of the pure-HTTP rewrite. Live-verified against the portal for login, MFA, session reuse
and usage; billing-account/meter discovery is verified on the author's account only.

### Changed
- Rewritten on a pure-`aiohttp` client; Browserless / headless Chrome are no longer needed.
- Setup walks through the portal's one-time code (email or SMS). The session is stored in the config
  entry, re-used on every poll and survives restarts.
- Re-authentication uses Home Assistant's standard reauth flow and needs only a new code.
- Daily poll pinned to 02:00 local time; every poll re-imports the last 30 days so late-published
  readings are filled in. First poll imports 90 days.
- Config entry version 2. Entries from 1.x are refused; remove and re-add the integration.
  Existing `sew_water:water_usage_mains` statistics are kept.
- Minimum Home Assistant 2025.8.

### Added
- `total_usage` sensor (running total, `total_increasing`).
- Throttling handling: Salesforce's concurrent-request limit (and any HTTP 429/503 with `Retry-After`)
  triggers a short retry instead of waiting for the next daily poll; a stale Aura context after a
  Salesforce release is refreshed automatically instead of demanding a new code.
- The daily poll carries up to 10 minutes of random jitter so installations do not all hit the portal at once.
- Config-entry diagnostics with credentials, cookies and record IDs redacted.
- Offline test suite for the portal client (`tests/`).

### Removed
- Yarra Valley Water portal option (untestable; same backend, could be re-added).
- Recycled-water statistic and sensor (untestable).
- Browserless URL/token, billing-account and meter-ID configuration fields (IDs are discovered).
- Billing-account and meter-ID sensors: account identifiers are no longer exposed as entities.

## [1.0.1] — 2026-09-13

### Fixed
- Login button selector for the SEW portal.
- Puppeteer script loaded off the event loop.

## [1.0.0]

Initial Browserless-based release: single batched Aura call per polling run, SEW and YVW portals,
mains and recycled statistics.
