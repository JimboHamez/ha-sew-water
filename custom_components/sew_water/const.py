"""Constants for the South East Water integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sew_water"

ATTRIBUTION: Final = "Data provided by South East Water"
MANUFACTURER: Final = "South East Water"

# Config-entry data keys (username/password use homeassistant.const).
CONF_BILLING_ACCOUNT_ID: Final = "billing_account_id"
CONF_COOKIES: Final = "cookies"
CONF_IMPORT_FROM: Final = "import_from"
CONF_METER_ID: Final = "meter_id"
CONF_METER_SERIAL: Final = "meter_serial"
CONF_MFA_CHANNEL: Final = "mfa_channel"
CONF_MFA_CODE: Final = "mfa_code"
CONF_POLL_TIME: Final = "poll_time"
CONF_SCAN_INTERVAL: Final = "scan_interval"

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

SERVICE_FORCE_IMPORT: Final = "force_import"
SERVICE_IMPORT_FROM_DATE: Final = "import_from_date"
SERVICE_ATTR_START_DATE: Final = "start_date"

STATISTIC_ID_MAINS: Final = f"{DOMAIN}:water_usage_mains"
