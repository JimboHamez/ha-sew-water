"""Data coordinator for the South East Water integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import random

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigEntryState
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .const import (
    BACKFILL_ATTEMPTS,
    BACKFILL_DAYS,
    BACKFILL_RETRY_MINUTES,
    CONF_BILLING_ACCOUNT_ID,
    CONF_COOKIES,
    CONF_IMPORT_FROM,
    CONF_METER_ID,
    CONF_METER_SERIAL,
    CONF_POLL_TIME,
    CONF_SCAN_INTERVAL,
    DEFAULT_POLL_TIME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_BACKFILL_FAILED,
    KEEPALIVE_MINUTES,
    POLL_JITTER_MINUTES,
    STATISTIC_ID_MAINS,
    TRAILING_WINDOW_DAYS,
)
from .sew_client import (
    AccountIds,
    DailyUsage,
    SewAuthError,
    SewBusyError,
    SewClient,
    SewConnectionError,
    SewError,
    SewProtocolError,
)

_LOGGER = logging.getLogger(__name__)

# Seconds to wait before retrying when the portal reports it is throttling and gives no hint.
BUSY_RETRY_SECONDS = 15 * 60
# How far back to look for the last statistic row preceding a re-import window.
LOOKBACK_DAYS = 3660

type SewConfigEntry = ConfigEntry[SewCoordinator]


@dataclass(frozen=True)
class SewData:
    """State shared with the entities after each poll.

    Attributes:
        ids: Billing account and meter identifiers.
        latest: Most recent day with a non-zero reading, if any.
        total_litres: Running total of every litre imported into statistics.
        last_poll: When the portal was last read successfully.
        window: Every day fetched in the last poll, oldest first.
    """

    ids: AccountIds
    latest: DailyUsage | None
    total_litres: float
    last_poll: datetime
    window: tuple[DailyUsage, ...]


class SewCoordinator(DataUpdateCoordinator[SewData]):
    """Poll the portal once a day and keep long-term statistics up to date."""

    config_entry: SewConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SewConfigEntry, client: SewClient) -> None:
        """Initialise the coordinator.

        Args:
            hass: Home Assistant instance.
            entry: The config entry that owns this coordinator.
            client: Portal client whose session already holds the stored cookies.
        """
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=self._interval(entry))
        self.client = client
        self.last_contact: datetime | None = None
        self.backfill_task: asyncio.Task[None] | None = None
        self.ids = AccountIds(
            billing_account_id=entry.data[CONF_BILLING_ACCOUNT_ID],
            meter_id=entry.data[CONF_METER_ID],
            meter_serial=entry.data.get(CONF_METER_SERIAL),
        )

    # --------------------------------------------------------------- scheduling

    @staticmethod
    def _interval(entry: SewConfigEntry) -> timedelta:
        """Return the time until the next poll.

        At the default one-day interval the poll is pinned to the configured local poll time (option
        ``poll_time``, default ``DEFAULT_POLL_TIME``) so it runs when the previous day's readings are
        most likely available, plus a random offset of up to ``POLL_JITTER_MINUTES`` so installations
        do not all hit the portal in the same second; any other interval is used as given.
        """
        minutes = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        if minutes != DEFAULT_SCAN_INTERVAL:
            return timedelta(minutes=minutes)
        poll_time = dt_util.parse_time(entry.options.get(CONF_POLL_TIME, DEFAULT_POLL_TIME))
        if poll_time is None:
            poll_time = dt_util.parse_time(DEFAULT_POLL_TIME)
        assert poll_time is not None
        now = dt_util.now()
        next_run = now.replace(hour=poll_time.hour, minute=poll_time.minute, second=poll_time.second, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        return next_run - now + timedelta(seconds=random.uniform(0, POLL_JITTER_MINUTES * 60))

    # --------------------------------------------------------------- keep-alive

    def async_start_keepalive(self) -> None:
        """Touch the portal periodically so the stored session outlives the gap between daily polls.

        The timer is cancelled when the config entry unloads.
        """
        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._async_keepalive,
                timedelta(minutes=KEEPALIVE_MINUTES),
                name=f"{DOMAIN} keep-alive",
                cancel_on_shutdown=True,
            )
        )

    async def _async_keepalive(self, now: datetime) -> None:
        """Load the portal home page once; start reauth if the session turns out to be dead."""
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return
        if any(True for _ in self.config_entry.async_get_active_flows(self.hass, {SOURCE_REAUTH})):
            return
        if self.last_contact is not None and now - self.last_contact < timedelta(minutes=KEEPALIVE_MINUTES):
            # A poll or service call already touched the portal recently.
            return
        try:
            alive = await self.client.async_is_alive()
        except SewError as err:
            # Transient: the next keep-alive or the daily poll will try again.
            _LOGGER.debug("Keep-alive skipped: %s", err)
            return
        if not alive:
            _LOGGER.warning("Portal session expired; a new login code is required")
            self.config_entry.async_start_reauth(self.hass)
            return
        self.last_contact = now
        self._async_store_cookies()
        _LOGGER.debug("Keep-alive OK")

    # ----------------------------------------------------------------- backfill

    @property
    def import_from(self) -> date | None:
        """Return the installation date chosen at setup, if any."""
        if not (raw := self.config_entry.data.get(CONF_IMPORT_FROM)):
            return None
        return dt_util.parse_date(raw)

    @callback
    def async_start_backfill(self) -> None:
        """Import history back to the installation date in the background, unless it is already there.

        The task is tied to the config entry, so unloading cancels it and the next setup starts it
        again if the history is still missing.
        """
        if (start := self.import_from) is None:
            return
        self.backfill_task = self.config_entry.async_create_background_task(
            self.hass, self._async_backfill(start), name=f"{DOMAIN} backfill"
        )

    async def _async_backfill(self, start: date) -> None:
        """Run the full import from ``start`` with a few retries, and raise a repair issue if it fails."""
        issue_id = f"{ISSUE_BACKFILL_FAILED}_{self.config_entry.entry_id}"
        # The first refresh has only just queued its rows with the recorder; wait for the commit so
        # the check below sees them.
        await get_instance(self.hass).async_block_till_done()
        if await self._async_has_statistics_on(start):
            _LOGGER.debug("History back to %s is already imported", start)
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        for attempt in range(1, BACKFILL_ATTEMPTS + 1):
            try:
                await self.async_import_from(start)
            except ValueError:
                _LOGGER.error("Cannot import history from %s: the date is not before today", start)
                return
            except ConfigEntryAuthFailed:
                _LOGGER.warning("History import from %s stopped: the portal session needs a new login code", start)
                self.config_entry.async_start_reauth(self.hass)
                return
            except UpdateFailed as err:
                if attempt == BACKFILL_ATTEMPTS:
                    _LOGGER.error("History import from %s failed after %d attempts: %s", start, attempt, err)
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        issue_id,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key=ISSUE_BACKFILL_FAILED,
                        translation_placeholders={"error": str(err), "start": start.isoformat()},
                    )
                    return
                delay = err.retry_after or BACKFILL_RETRY_MINUTES * 60
                _LOGGER.warning(
                    "History import from %s failed (attempt %d of %d), retrying in %d s: %s",
                    start,
                    attempt,
                    BACKFILL_ATTEMPTS,
                    delay,
                    err,
                )
                await asyncio.sleep(delay)
            else:
                _LOGGER.info("Imported history back to %s", start)
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                return

    # ------------------------------------------------------------------ polling

    async def _async_update_data(self) -> SewData:
        """Fetch the trailing window (or the full backfill on first run) and store statistics."""
        yesterday = dt_util.now().date() - timedelta(days=1)
        first_run = await self._async_last_statistic_day() is None
        window_days = BACKFILL_DAYS if first_run else TRAILING_WINDOW_DAYS
        start = yesterday - timedelta(days=window_days - 1)
        try:
            data = await self._async_import(start, yesterday)
        finally:
            # Re-evaluate the delay to the next poll so the daily run stays pinned to the poll time.
            self.update_interval = self._interval(self.config_entry)
        return data

    async def async_import_from(self, start: date) -> None:
        """Import every day from ``start`` to yesterday, then notify entities.

        Args:
            start: First day to import.
        """
        yesterday = dt_util.now().date() - timedelta(days=1)
        if start > yesterday:
            raise ValueError("start date must be before today")
        self.async_set_updated_data(await self._async_import(start, yesterday))

    async def _async_import(self, start: date, end: date) -> SewData:
        """Fetch ``start``..``end`` from the portal, import statistics and build the shared state.

        Raises:
            ConfigEntryAuthFailed: If the stored session is no longer accepted, triggering reauth.
            UpdateFailed: If the portal is busy (with a short ``retry_after``), unreachable or answers
                unexpectedly.
        """
        try:
            if not await self.client.async_is_alive():
                raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="session_expired")
            usage = await self.client.async_fetch_usage(self.ids, start, end)
        except SewAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="session_rejected",
                translation_placeholders={"error": str(err)},
            ) from err
        except SewBusyError as err:
            # Salesforce throttles with an Apex error rather than a 429; back off briefly instead of
            # waiting for the next daily poll.
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="portal_busy",
                translation_placeholders={"error": str(err)},
                retry_after=err.retry_after or BUSY_RETRY_SECONDS,
            ) from err
        except SewConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except SewProtocolError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="unexpected_response",
                translation_placeholders={"error": str(err)},
            ) from err

        self.last_contact = dt_util.utcnow()
        self._async_store_cookies()
        total = await self._async_import_statistics(usage)
        latest = next((day for day in reversed(usage) if day.available and day.litres > 0), None)
        _LOGGER.debug("Imported %d day(s) %s..%s; latest reading %s", len(usage), start, end, latest)
        return SewData(ids=self.ids, latest=latest, total_litres=total, last_poll=dt_util.utcnow(), window=tuple(usage))

    def _async_store_cookies(self) -> None:
        """Persist the (possibly refreshed) session cookies so a restart needs no new login."""
        cookies = self.client.export_cookies()
        if cookies != self.config_entry.data.get(CONF_COOKIES):
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_COOKIES: cookies}
            )

    # --------------------------------------------------------------- statistics

    @staticmethod
    def _metadata() -> StatisticMetaData:
        """Describe the mains-water external statistic used by the Energy dashboard."""
        return StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name="South East Water mains usage",
            source=DOMAIN,
            statistic_id=STATISTIC_ID_MAINS,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.LITERS,
        )

    async def _async_last_statistic_day(self) -> date | None:
        """Return the local day of the newest stored statistic, or ``None`` before the first import."""
        rows = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, STATISTIC_ID_MAINS, True, {"sum"}
        )
        if not (stats := rows.get(STATISTIC_ID_MAINS)):
            return None
        return dt_util.as_local(dt_util.utc_from_timestamp(stats[0]["start"])).date()

    @staticmethod
    def _row_start(day: date, hour: int = 0) -> datetime:
        """Return the UTC timestamp of the statistic row ``hour`` hours after local midnight of ``day``.

        The offset is applied in UTC so every row is a distinct instant on daylight-saving days, where
        wall-clock arithmetic would make two hours coincide or one vanish.
        """
        return dt_util.as_utc(dt_util.start_of_local_day(day)) + timedelta(hours=hour)

    @classmethod
    def _hourly_rows(cls, day: DailyUsage) -> list[tuple[datetime, float]]:
        """Split a day into ``(row start, litres)`` pairs, one per hourly reading.

        A day without hourly readings becomes a single midnight row carrying the day's total. On the
        23-hour day when daylight saving starts, the reading that would land on the next day's midnight
        is folded into the day's last row so the two days never share a row.
        """
        if not day.readings:
            return [(cls._row_start(day.day), float(day.litres))]
        next_midnight = cls._row_start(day.day + timedelta(days=1))
        rows: list[tuple[datetime, float]] = []
        for hour, litres in enumerate(day.readings):
            start = cls._row_start(day.day, hour)
            if start >= next_midnight and rows:
                last_start, last_litres = rows[-1]
                rows[-1] = (last_start, last_litres + litres)
                continue
            rows.append((start, float(litres)))
        return rows

    async def _async_has_statistics_on(self, day: date) -> bool:
        """Return whether any statistic row exists for the local day ``day``."""
        row_start = self._row_start(day)
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            row_start,
            row_start + timedelta(days=1),
            {STATISTIC_ID_MAINS},
            "hour",
            None,
            {"sum"},
        )
        return bool(rows.get(STATISTIC_ID_MAINS))

    async def _async_sum_before(self, day: date) -> float:
        """Return the running sum of the newest statistic row before ``day`` (0 if there is none)."""
        row_start = self._row_start(day)
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            row_start - timedelta(days=LOOKBACK_DAYS),
            row_start,
            {STATISTIC_ID_MAINS},
            # Hourly buckets so the cut-off is exact: a day bucket would start at midnight and swallow
            # the first row of the window itself.
            "hour",
            None,
            {"sum"},
        )
        if not (stats := rows.get(STATISTIC_ID_MAINS)):
            return 0.0
        return float(stats[-1].get("sum") or 0.0)

    async def _async_import_statistics(self, usage: list[DailyUsage]) -> float:
        """Write one statistic row per hour and return the running total after the last day.

        Rows are keyed by their start time, so re-importing a day simply overwrites its hours; the
        running sum is rebuilt from the value recorded just before the window so corrections stay
        consistent.
        """
        if not usage:
            rows = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, STATISTIC_ID_MAINS, True, {"sum"}
            )
            stats = rows.get(STATISTIC_ID_MAINS)
            return float(stats[0].get("sum") or 0.0) if stats else 0.0
        running = await self._async_sum_before(usage[0].day)
        statistics: list[StatisticData] = []
        for day in usage:
            for start, litres in self._hourly_rows(day):
                running += litres
                statistics.append(StatisticData(start=start, state=litres, sum=running))
        async_add_external_statistics(self.hass, self._metadata(), statistics)
        return running
