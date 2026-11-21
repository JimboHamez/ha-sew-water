"""Constants for the South East Water integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sew_water"

ATTRIBUTION: Final = "Data provided by South East Water"
MANUFACTURER: Final = "South East Water"
# Config entry title. Not the login email, because Home Assistant writes entry titles into its logs.
ENTRY_TITLE: Final = "South East Water"

# Config-entry data keys (username/password use homeassistant.const).
CONF_BILLING_ACCOUNT_ID: Final = "billing_account_id"
# Billing period (options): days in each period, and the first day of any one period (ISO date). The options
# form groups them in the CONF_BILLING_PERIOD section; they are stored flat.
CONF_BILLING_DAYS: Final = "billing_days"
CONF_BILLING_PERIOD: Final = "billing_period"
CONF_BILLING_START: Final = "billing_start"
CONF_COOKIES: Final = "cookies"
CONF_IMPORT_FROM: Final = "import_from"
# The mains meter, which every version has polled; kept so older entries and downgrades keep working.
CONF_METER_ID: Final = "meter_id"
CONF_METER_KIND: Final = "meter_kind"
CONF_METER_SERIAL: Final = "meter_serial"
# Every digital meter on the billing account, as a list of {meter_id, meter_serial, meter_kind}. Entries
# created before 2.3 lack it; the coordinator fills it in once (see SewCoordinator._async_upgrade_meters).
CONF_METERS: Final = "meters"
CONF_MFA_CHANNEL: Final = "mfa_channel"
CONF_MFA_CODE: Final = "mfa_code"
CONF_POLL_TIME: Final = "poll_time"
# Custom usage prices (options), used when the tariff option is TARIFF_CUSTOM.
CONF_PRICE_RECYCLED: Final = "price_recycled"
CONF_PRICE_STEP1: Final = "price_step1"
CONF_PRICE_STEP2: Final = "price_step2"
# The setup flow's property picker; its value is an index into the flow's property list, never stored.
CONF_PROPERTY: Final = "property"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_STEP_THRESHOLD: Final = "step_threshold"
# Which usage prices the cost statistics use (option).
CONF_TARIFF: Final = "tariff"

# Tariffs. The first three are South East Water's published prices (see pricing.py); recycled is used only
# for the recycled-water meter and is not offered as a choice.
TARIFF_CUSTOM: Final = "custom"
TARIFF_RECYCLED: Final = "recycled"
TARIFF_WATER_ONLY: Final = "water_only"
TARIFF_WATER_SEWAGE: Final = "water_sewage"
DEFAULT_TARIFF: Final = TARIFF_WATER_SEWAGE
# South East Water bills residential accounts quarterly; a period is 90 to 92 days.
DEFAULT_BILLING_DAYS: Final = 91
MAX_BILLING_DAYS: Final = 366

# Days of history imported the first time the integration runs.
BACKFILL_DAYS: Final = 90
# How many times the background import back to the installation date is tried per setup, and the
# wait between tries when the portal gives no retry hint.
BACKFILL_ATTEMPTS: Final = 3
BACKFILL_RETRY_MINUTES: Final = 30
# Days re-fetched on every poll, because the portal publishes readings late and back-fills them.
TRAILING_WINDOW_DAYS: Final = 30
# Default poll interval in minutes; at exactly one day the poll is aligned to the poll time (local).
DEFAULT_SCAN_INTERVAL: Final = 1440
MIN_SCAN_INTERVAL: Final = 60
# Default local time of day for the daily poll, as the time selector stores it (HH:MM:SS). The portal
# publishes the previous day's readings during the morning; users whose readings land later can move it.
DEFAULT_POLL_TIME: Final = "02:00:00"
# Random delay added to the daily poll so installations do not all query the portal at once.
POLL_JITTER_MINUTES: Final = 10
# Minutes between keep-alive requests. The portal drops a session left idle for more than 2 hours
# (measured: alive after 2 h idle, dead after 4 h), far less than the gap between daily polls. One
# keep-alive is a single page load; 30 minutes tolerates three missed ticks and is the interval
# proven over a full day of measurement.
KEEPALIVE_MINUTES: Final = 30

ISSUE_BACKFILL_FAILED: Final = "backfill_failed"
ISSUE_PRICES_OUT_OF_DATE: Final = "prices_out_of_date"

SERVICE_FORCE_IMPORT: Final = "force_import"
SERVICE_IMPORT_FROM_DATE: Final = "import_from_date"
SERVICE_ATTR_START_DATE: Final = "start_date"

STATISTIC_ID_MAINS: Final = f"{DOMAIN}:water_usage_mains"
STATISTIC_ID_RECYCLED: Final = f"{DOMAIN}:water_usage_recycled"
STATISTIC_ID_COST_MAINS: Final = f"{DOMAIN}:water_cost_mains"
STATISTIC_ID_COST_RECYCLED: Final = f"{DOMAIN}:water_cost_recycled"
