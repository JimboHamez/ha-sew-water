# South East Water

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
![GitHub Release](https://img.shields.io/github/v/release/JimboHamez/ha-sew-water?style=for-the-badge)
[![hacs_downloads](https://img.shields.io/github/downloads/JimboHamez/ha-sew-water/latest/total?style=for-the-badge)](https://github.com/JimboHamez/ha-sew-water/releases/latest)
![GitHub License](https://img.shields.io/github/license/JimboHamez/ha-sew-water?style=for-the-badge)
![GitHub commit activity](https://img.shields.io/github/commit-activity/y/JimboHamez/ha-sew-water?style=for-the-badge)
![Maintenance](https://img.shields.io/maintenance/yes/2026?style=for-the-badge)
[![HA quality scale](https://img.shields.io/badge/HA%20quality%20scale-platinum-E5E4E2?style=for-the-badge)](#home-assistant-quality-scale)

[![Tests](https://github.com/JimboHamez/ha-sew-water/actions/workflows/test.yml/badge.svg)](https://github.com/JimboHamez/ha-sew-water/actions/workflows/test.yml)
[![Validate](https://github.com/JimboHamez/ha-sew-water/actions/workflows/validate.yaml/badge.svg)](https://github.com/JimboHamez/ha-sew-water/actions/workflows/validate.yaml)
[![hassfest](https://github.com/JimboHamez/ha-sew-water/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/JimboHamez/ha-sew-water/actions/workflows/hassfest.yaml)
[![Security](https://github.com/JimboHamez/ha-sew-water/actions/workflows/security.yml/badge.svg)](https://github.com/JimboHamez/ha-sew-water/actions/workflows/security.yml)

Daily mains water usage from the [South East Water](https://my.southeastwater.com.au) customer portal, straight into Home Assistant.

It gives you:

- **Energy dashboard water** — one long-term statistic, `sew_water:water_usage_mains`, with a row for every **hour**, so the dashboard's hourly, daily, weekly and monthly views are all real.
- **Sensors** — yesterday's litres (with the 24 hourly readings as attributes), a running total and the reading date.
- **One login** — sign in once with your portal email, password and a one-time code; the session is kept and re-used, and survives restarts.
- **Late data handled** — the portal publishes readings a day or two late and sometimes corrects them, so every poll re-imports the last 30 days.
- **Painless re-login** — when the portal finally expires the session, Home Assistant's standard *Reauthentication required* card asks only for a new code.

**Nothing else to install** — the integration talks to the portal directly over HTTPS; no add-ons, no browser automation, no extra Python packages.

---

## Why this exists

South East Water's digital meters report hourly usage to the customer portal, but the portal only shows it in a web page — there is no API, no export and no way to see it next to your other utilities in Home Assistant.

This integration maps the portal's login, one-time code and usage requests to plain HTTP. Setup asks for the code once, the resulting session is kept and refreshed on every poll, and each day's usage lands in Home Assistant's long-term statistics where the Energy dashboard can use it.

---

## 🆕 What's new in v2.1.0

- **Poll time is now an option** ([#3](https://github.com/JimboHamez/ha-sew-water/issues/3)). The daily poll was fixed at 02:00, but the portal publishes the previous day's readings later than that on at least some accounts, leaving *Daily usage* a day behind until the next poll. Set the time under ⚙ on the integration entry; the default stays 02:00.

From v2.0.0: the pure-HTTP rewrite — nothing to install, one-time code at setup then never again, hourly statistics for the Energy dashboard, 30-day re-import on every poll, full-history import from the meter's installation date, Reconfigure flow, diagnostics, repair issues and [Platinum](#home-assistant-quality-scale) on the quality scale. **Upgrading from 1.x:** remove the old integration and add it again; your statistics are kept.

Full history in the [CHANGELOG](CHANGELOG.md) · [release notes](https://github.com/JimboHamez/ha-sew-water/releases/tag/v2.1.0).

---

## Prerequisites

### 1. Portal account and a digital meter

A working login for [my.southeastwater.com.au](https://my.southeastwater.com.au). During setup the portal will send a one-time code to the email address or mobile number on the account, so have that to hand.

**Supported:** South East Water residential and business accounts with a **digital (smart) water meter** — the ones whose usage appears on the portal's *Usage* page as an hourly graph. One config entry covers one portal login; if the login has several billing accounts or meters, the first one the portal returns is used.

**Not supported:** conventional (manually read) meters, which have no daily data in the portal; recycled-water meters (the author has none to test against); other Victorian retailers, including Yarra Valley Water, even though they run the same portal software.

### 2. Recorder

Long-term statistics are stored by Home Assistant's [recorder](https://www.home-assistant.io/integrations/recorder/), which is on by default. History lives in the recorder database, not in this integration — removing and re-adding the integration keeps it.

### 3. Energy dashboard (optional)

Only needed if you want the water card on the Energy dashboard. Nothing to set up in advance; see [How it works → Energy dashboard](#energy-dashboard).

---

## Installation

![South East Water sensors in Home Assistant](images/dashboard.png)
<!-- placeholder: capture the device page showing the three sensors -->

### HACS (recommended)

1. HACS → Integrations → ⋮ → **Custom repositories**.
2. Add `https://github.com/JimboHamez/ha-sew-water` as type **Integration**.
3. Install **South East Water**.
4. Restart Home Assistant.

### Manual

1. Copy `custom_components/sew_water/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

### Removing the integration

1. **Settings → Devices & Services → South East Water** → ⋮ → **Delete**. This removes the config entry, the stored session and every entity it created.
2. If installed via HACS, HACS → **South East Water** → ⋮ → **Remove**; for a manual install delete `config/custom_components/sew_water/`.
3. Restart Home Assistant.

One thing is deliberately left behind: the **`sew_water:water_usage_mains` statistic** and its history stay in the recorder database, so reinstalling picks up where you left off. To delete it, use *Settings → Developer tools → Statistics* and remove the orphaned entry.

The integration uses only `aiohttp`, which ships with Home Assistant — nothing else to install.

---

## Configuration

**Settings → Devices & Services → Add Integration → South East Water**

The wizard has four steps; steps 2 and 3 only appear when the portal asks for a one-time code (it always does today).

### Step 1 — Sign in
![Step 1 — Sign in](images/config-step1-signin.png)
<!-- placeholder: capture the credentials page -->

| Field | Description |
|---|---|
| Email address | The email you log in to the portal with |
| Password | Your portal password |

### Step 2 — Send code by (only if the portal asks for a code)
![Step 2 — Send code by](images/config-step2-channel.png)
<!-- placeholder: capture the Email / SMS chooser -->

| Field | Default | Description |
|---|---|---|
| Send code by | Email | Where the portal sends the one-time code: **Email** or **Text message (SMS)** |

### Step 3 — One-time code (only if the portal asks for a code)
![Step 3 — One-time code](images/config-step3-code.png)
<!-- placeholder: capture the code entry page -->

| Field | Description |
|---|---|
| One-time code | The 6-digit code the portal just sent. Spaces and dashes are ignored. |

The billing account and meter are discovered automatically once the code is accepted.

### Step 4 — Import history (optional)

| Field | Description |
|---|---|
| Import history from | The date your digital meter was installed, or any earlier day you want statistics from. Leave blank to import the last 90 days only. |

Setup finishes straight away with the last 90 days; the rest is imported in the background, in one pass from the date you gave up to yesterday. If that pass fails (portal down or busy), it is retried a couple of times, then a **Repairs** issue tells you so — it runs again the next time the integration loads, or you can call `sew_water.import_from_date` yourself. Nothing is fetched twice: if the history back to that date is already in the recorder, the task exits immediately.

### Options

⚙ on the integration entry.

| Field | Default | Description |
|---|---|---|
| Poll interval | 1440 min | Minutes between polls. At the default the poll is pinned to the **poll time** below every day; any other value (minimum 60) is used as a plain interval. |
| Poll time | 02:00 | Local time of the daily poll. Only used at the default interval. The portal publishes the previous day's readings during the morning; if *Daily usage* is still a day behind after the poll, move this later (10:00 has been reported to work). |

### Re-authentication

When the portal expires the stored session, Home Assistant shows a **Reauthentication required** card. Click **Reconfigure**, then:

1. **Sign in again** — press *Request code* (your saved credentials are re-used).
2. **Send code by** — Email or SMS.
3. **One-time code** — enter the code.

If the portal rejects the saved password, an extra **Update password** step appears before the code is requested.

> **Heads up:** the portal requires a one-time code for *every* new login and offers no "remember this device". The integration avoids that by keeping the session alive between polls, so re-authentication should be rare.

### Reconfigure

To change your portal password, or to force a fresh login without waiting for the session to expire: **Settings → Devices & Services → South East Water** → ⋮ → **Reconfigure**. Enter the credentials (the email is pre-filled and must stay the same — a different email is a different account, so add it as a new entry instead), choose where to send the code and enter it. The stored session and password are replaced in place; entities and history are untouched.

---

## How it works

- **Session reuse** — the cookies from the one login you did at setup are stored in the config entry and re-sent on every poll. Each successful poll writes the refreshed cookies back, so the session survives Home Assistant restarts.
- **Keep-alive** — the portal drops a session that sits idle for more than about two hours (measured: still alive after 2 hours idle, gone after 4), far less than the gap between daily polls. So between polls the integration loads the portal home page once every 30 minutes (a single request, no data fetched) to keep the session alive. If that check finds the session gone, the *Reauthentication required* card appears straight away rather than at the next poll.
- **Trailing re-import** — every poll fetches the last 30 days in one batched request and re-imports them as hourly statistics (24 rows per day). Rows are keyed by their start hour, so re-importing overwrites in place: late-published days get filled in and corrections are applied without duplicates. The first poll after setup imports 90 days.
- **Statistics and sensors, not one or the other** — a sensor cannot carry retroactive history and a statistic cannot drive a card or an automation, so the integration keeps both. The statistic is the source of truth; the *Total usage* sensor mirrors its running total.
- **Daily poll at a fixed local time** — 02:00 by default, changeable in the options. The next poll is always scheduled as "next poll time" (plus a few random minutes so every installation doesn't hit the portal at the same second), so it never drifts. If the portal reports it is busy, the poll retries after 15 minutes rather than waiting a day.
- **Zero days** — the portal returns 24 zeros both for an unpublished day and for a genuinely empty one. The *Daily usage* / *Last reading date* sensors skip zero days; statistics import them as 0 L and a later poll corrects them if data appears.

The protocol, the statistics rules and every design decision are in [DESIGN_DOCUMENT.md](DESIGN_DOCUMENT.md).

### Energy dashboard

*Settings → Dashboards → Energy → Water consumption → Add water source* and pick **`sew_water:water_usage_mains`**.

> ⚠️ Use the statistic, not the `Total usage` sensor. The statistic has one row per hour with the correct timestamp, including back-filled and corrected days. The sensor only changes once per poll, so the dashboard would attribute a whole day's usage to the minute the poll ran.

---

## Sensors

All entities sit on one device, **South East Water**.

| Sensor | Unit | Description |
|---|---|---|
| `sensor.south_east_water_daily_usage` | L | Most recent published day's usage. Attributes: `reading_date`, `hourly_readings` (24 values). |
| `sensor.south_east_water_total_usage` | L | Running total of every day imported (`total_increasing`). |
| `sensor.south_east_water_last_reading_date` | date | Day the *Daily usage* value belongs to. Diagnostic; **disabled by default** — enable it from the entity's settings if you want it on a card (the same date is the `reading_date` attribute of *Daily usage*). |

Account identifiers (billing account, meter record ID, meter serial) are deliberately **not** exposed as entities — they identify your account and would otherwise be kept in the recorder. They live only in the config entry, are redacted from diagnostics, and are written once to the log at debug level on startup if you need to check them.

---

## Services

| Service | Description |
|---|---|
| `sew_water.force_import` | Poll the portal now instead of waiting for the next scheduled poll. |
| `sew_water.import_from_date` | Import every day from `start_date` up to yesterday — for example, back to the day your digital meter was installed. |

```yaml
action: sew_water.import_from_date
data:
  start_date: "2026-01-01"
```

---

## Use cases

- **Water on the Energy dashboard** — the main reason this exists: daily water next to electricity and gas, with the dashboard's day/week/month/year views and cost tracking (set a $/L price on the water source).
- **Leak and unusual-use alerts** — yesterday's total is a single number that is easy to compare against a threshold or a moving average; overnight hours in `hourly_readings` should be near zero on a healthy property.
- **Consumption targets** — track a running total against a budget for the billing quarter using the `Total usage` sensor and a utility meter helper with a quarterly cycle.
- **Long-term records** — the statistic is kept by the recorder for as long as your other long-term statistics, and `sew_water.import_from_date` can pull in everything since your digital meter was installed.

## Automation examples

**Alert when yesterday's usage was unusually high.** The sensor updates once a day after the daily poll, so a state trigger fires at most once per day.

```yaml
alias: High water use yesterday
triggers:
  - trigger: numeric_state
    entity_id: sensor.south_east_water_daily_usage
    above: 1000
actions:
  - action: notify.notify
    data:
      title: High water use
      message: >-
        {{ states('sensor.south_east_water_daily_usage') }} L used on
        {{ state_attr('sensor.south_east_water_daily_usage', 'reading_date') }}.
```

**Possible leak: water flowing every hour overnight.** A property with no leak normally shows several zero hours between midnight and 5 am.

```yaml
alias: Possible water leak
triggers:
  - trigger: state
    entity_id: sensor.south_east_water_daily_usage
    attribute: reading_date
conditions:
  - condition: template
    value_template: >-
      {% set hours = state_attr('sensor.south_east_water_daily_usage', 'hourly_readings') or [] %}
      {{ hours | length == 24 and hours[0:5] | min > 0 }}
actions:
  - action: notify.notify
    data:
      title: Possible water leak
      message: >-
        Water was used in every hour between midnight and 5 am on
        {{ state_attr('sensor.south_east_water_daily_usage', 'reading_date') }}.
```

**Quarterly water budget.** Create a [utility meter](https://www.home-assistant.io/integrations/utility_meter/) helper with source `sensor.south_east_water_total_usage` and a *quarterly* cycle; its value is the litres used so far this quarter and works as a gauge or a threshold for an automation.

---

## Known limitations

- **Data is a day or more behind.** The portal publishes a day's readings during the following day, sometimes later, and occasionally revises them. The integration polls once a day (02:00 by default) and re-imports the last 30 days so gaps and corrections are filled in, but you will never see today's usage, and yesterday's may be zero until the following poll. If yesterday is *consistently* missing after the poll, set a later **Poll time** in the options.
- **Hourly is the finest resolution.** The portal publishes hourly readings, so that is what the statistic stores; there is no finer data. On the day daylight saving starts (23 hours), the portal's 24th reading is folded into the last hour of that day.
- **History imported by earlier versions is daily.** Days imported before hourly statistics were introduced have a single row at 11:00; they are converted to hourly rows automatically as they fall inside the 30-day re-import window, or all at once with `sew_water.import_from_date`.
- **One-time code on every login.** The portal offers no "remember this device". Setup, re-authentication and reconfigure each need a code; the integration keeps the session alive with a small request every 30 minutes so this is rare, but it cannot be avoided when the portal ends the session (for example after a portal release or a password change).
- **One login, one meter.** If a portal login has several billing accounts or meters, only the first one returned by the portal is used. Mains water only — recycled-water meters are not read.
- **Backfill on first setup is 90 days** unless you give an installation date in the wizard. `sew_water.import_from_date` covers anything else.
- **Sensor totals versus statistics.** The *Total usage* sensor changes once per poll, so its history attributes the whole day to the minute the poll ran. Use the statistic for the Energy dashboard.
- **The portal is not an API.** The integration speaks the portal's own web protocol; a change on South East Water's side can stop it working until the integration is updated. The client is isolated in one module to keep such fixes small.

---

## Troubleshooting

| Symptom | What it means | What to do |
|---|---|---|
| **Reauthentication required** card | The portal ended the stored session. | Click *Reconfigure* on the card and enter a new one-time code. Your saved credentials are re-used; only if the password was rejected will it ask for a new one. |
| Setup shows *Could not reach the South East Water portal* | Home Assistant could not connect, or the portal returned a server error. | Check internet access from the HA host and whether [my.southeastwater.com.au](https://my.southeastwater.com.au) loads in a browser. The portal has maintenance windows. |
| Setup shows *The portal did not accept the sign-in but gave no reason* | The portal's login call returned neither a session nor a rejection message. This is not the normal wrong-password response. | Confirm the same details work at my.southeastwater.com.au. Then enable debug logging (below), retry, and open an issue with the `Login gave neither a redirect nor a reason` log line — session IDs in it are masked. |
| Setup shows *Enter a valid email address* | The address is missing an `@` or a domain. | Check for typos or stray characters; the check is deliberately loose. |
| Setup shows *The code was not accepted* | Wrong or expired code. | Codes are six digits and short-lived. Request a new one by going back a step. |
| Setup shows *The portal responded unexpectedly* | The portal's pages or responses did not match what the integration expects. | Enable debug logging (below), retry, and open an issue with the log excerpt. It usually means the portal changed. |
| Repair issue *needs to be set up again* | A config entry from an earlier integration version was found; it holds no portal session and cannot be migrated. | Delete that entry and add the integration again. Existing statistics are kept. |
| *Daily usage* is `unknown` or the last reading date is several days old | The portal has not published recent days yet, or is returning zeros for them. | Check the portal's *Usage* page for the same days. The next daily poll re-imports the last 30 days automatically; `sew_water.force_import` does it now. If this happens every day, set a later **Poll time** in the options. |
| Entities are *unavailable* | The last poll failed (portal down, busy or unreachable). | The coordinator logs the cause once and retries — after 15 minutes if the portal reported it was busy, otherwise at the next scheduled poll. Call `sew_water.force_import` to retry immediately. |
| Energy dashboard shows a big spike on one day | The `Total usage` *sensor* was chosen as the water source instead of the statistic. | Change the water source to `sew_water:water_usage_mains`. |
| History is missing before a certain date | Only 90 days are imported on first setup unless you gave an installation date. | Call `sew_water.import_from_date` with the date you want to start from. |
| **South East Water history import did not finish** repair issue | The background import from your installation date failed after its retries. | It runs again on the next reload or restart; or call `sew_water.import_from_date` with the same date to do it now. |

**Debug logging** — add to `configuration.yaml` and restart, or use *Settings → Devices & Services → South East Water → ⋮ → Enable debug logging*:

```yaml
logger:
  logs:
    custom_components.sew_water: debug
```

Debug output never includes your password, cookies or one-time codes. Account identifiers (billing account, meter record ID) appear once at startup so you can verify the right meter was picked. **Diagnostics** (⋮ → *Download diagnostics* on the entry) has the same redactions and is safe to attach to an issue.

---

## Roadmap

- **Recycled water** — not supported; the author has no recycled meter to test against. Contributions welcome.
- **Yarra Valley Water** — runs on the same Salesforce Experience Cloud backend and the client could be adapted, but it is untested and not included.

Full list in [DESIGN_DOCUMENT.md → Open items](DESIGN_DOCUMENT.md#9-open-items).

---

## Compatibility

| Component | Version |
|---|---|
| Home Assistant | 2025.8 or newer |
| Python | 3.13 (as shipped with Home Assistant) |
| Runtime dependencies | `aiohttp` (ships with Home Assistant) |
| Utility | South East Water only (mains water) |
| Quality scale | Platinum, self-assessed — see [Home Assistant quality scale](#home-assistant-quality-scale) |

Credentials are stored in the config entry — Home Assistant's private `.storage`, the same place every integration keeps its secrets. They are never logged and are redacted from diagnostics. Because the portal demands a one-time code on every login, the stored password alone cannot open a new session; it only saves you retyping it during re-authentication.

---

## Home Assistant quality scale

This integration is measured against Home Assistant's [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/checklist) — the checklist core integrations are held to, covering setup, entity naming, documentation, typing and test coverage. The rule-by-rule record is in [`quality_scale.yaml`](custom_components/sew_water/quality_scale.yaml).

**This is a self-assessment, not an awarded tier.** The quality scale is a programme for integrations that ship inside Home Assistant Core; a custom/HACS integration like this one is not eligible for an official rating. The badge reports our own audit against the published rules, so you can see what has and hasn't been done rather than take "custom integration" on trust.

**Bronze — all 17 applicable rules pass.** Setup runs entirely through the UI, `config_flow.py` is fully covered by tests, entities carry unique IDs and take their names from translations, the coordinator lives on `entry.runtime_data`, both actions (`force_import` and `import_from_date`) are registered at startup, and the portal login is exercised before an entry is created and again before setup completes. Three Bronze rules don't apply: `docs-triggers` and `docs-conditions` (this integration provides neither), and `entity-event-setup` (entities read the coordinator and subscribe to nothing else).

**Silver — all 10 rules pass.** The config entry unloads cleanly, every entity goes *unavailable* when a poll fails and comes back when the next one succeeds, the coordinator logs an outage once rather than every cycle, both actions raise a translated error instead of failing silently, `PARALLEL_UPDATES` is declared, and an expired portal session raises `ConfigEntryAuthFailed` so Home Assistant's standard *Reauthentication required* card asks for a new one-time code. Test coverage sits at 99% against the required 95%, which CI enforces.

**Gold — all 18 applicable rules pass.** The meter is a device, a diagnostics download (credentials, session cookies and account identifiers redacted) is available from the integration page, the wizard can be re-run against an existing entry via **Reconfigure**, entity names, icons and error messages come from translations, the reading-date sensor is a disabled-by-default diagnostic entity, a repair issue is raised for a version 1 entry that cannot be migrated, and the docs carry use cases, examples, troubleshooting and a known-limitations list.

Four Gold rules don't apply, all for the same reason: one config entry is one portal login with one meter. `discovery` and `discovery-update-info` assume something on the local network to find, and this is a cloud service; `dynamic-devices` and `stale-devices` assume devices can appear or disappear after setup, and here the only device is created with the entry and removed with it.

**Platinum — all three rules pass.** `strict-typing`: `mypy --strict` is clean across the package (checked in CI) and a `py.typed` marker ships with it. `async-dependency` and `inject-websession`: the portal client is `aiohttp` throughout with no blocking I/O, and it owns no network resources — the config flow and the coordinator each hand it a session created through Home Assistant's `async_create_clientsession`, with a dedicated cookie jar so the portal's session cookies never mix with other integrations'.

One thing worth stating plainly: those two rules assume the API code lives in a **separate published library** declared in `manifest.json` `requirements`, and here it lives in-component as `sew_client.py`. That split is deliberate. The rule exists so Home Assistant Core can version a dependency independently of the integration that uses it; this integration ships as one unit through HACS, and the client exists for exactly one portal with no other consumer. Splitting it would buy a second repository, a second release cadence and a version-compatibility surface between them, in exchange for nothing a user would notice.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE). Based on the original pyscript implementation by [BJReplay](https://github.com/BJReplay/ha-sew-water).
