"""Data coordinator for the South East Water integration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
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
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
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
    CONF_METER_KIND,
    CONF_METER_SERIAL,
    CONF_METERS,
    CONF_POLL_TIME,
    CONF_SCAN_INTERVAL,
    DEFAULT_POLL_TIME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_BACKFILL_FAILED,
    KEEPALIVE_MINUTES,
    POLL_JITTER_MINUTES,
    STATISTIC_ID_MAINS,
    STATISTIC_ID_RECYCLED,
    TRAILING_WINDOW_DAYS,
)
from .sew_client import (
    METER_KIND_MAINS,
    METER_KIND_RECYCLED,
    AccountIds,
    DailyUsage,
    MeterKind,
    SewAuthError,
    SewBusyError,
    SewClient,
    SewConnectionError,
    SewError,
    SewProtocolError,
    mask_identifier,
    meter_kind,
)

_LOGGER = logging.getLogger(__name__)

# Seconds to wait before retrying when the portal reports it is throttling and gives no hint.
BUSY_RETRY_SECONDS = 15 * 60
# How far back to look for the last statistic row preceding a re-import window.
LOOKBACK_DAYS = 3660

type SewConfigEntry = ConfigEntry[SewCoordinator]

# The external statistic each kind of meter is imported into, and each statistic's display name.
STATISTIC_IDS: Mapping[MeterKind, str] = {
    METER_KIND_MAINS: STATISTIC_ID_MAINS,
    METER_KIND_RECYCLED: STATISTIC_ID_RECYCLED,
}
STATISTIC_NAMES: Mapping[str, str] = {
    STATISTIC_ID_MAINS: "South East Water mains usage",
    STATISTIC_ID_RECYCLED: "South East Water recycled usage",
}


def statistic_id_for(meter: AccountIds) -> str:
    """Return the external statistic a meter's readings are imported into.

    Args:
        meter: The meter, with its kind.

    Returns:
        ``sew_water:water_usage_mains`` or ``sew_water:water_usage_recycled``.
    """
    return STATISTIC_IDS[meter.kind]


def meters_to_data(meters: Sequence[AccountIds]) -> list[dict[str, str | None]]:
    """Serialise meters for the config entry's ``CONF_METERS`` list.

    The billing account is stored once, under ``CONF_BILLING_ACCOUNT_ID``, so it is left out here.

    Args:
        meters: The meters, as discovery returned them.

    Returns:
        One ``{meter_id, meter_kind, meter_serial}`` dict per meter, in the same order.
    """
    return [
        {CONF_METER_ID: meter.meter_id, CONF_METER_KIND: meter.kind, CONF_METER_SERIAL: meter.meter_serial}
        for meter in meters
    ]


def meters_from_data(billing_account_id: str, raw: object) -> list[AccountIds]:
    """Read the config entry's ``CONF_METERS`` list back, skipping anything malformed.

    An unknown kind is classified again from the serial rather than trusted.

    Args:
        billing_account_id: The entry's billing account record ID, shared by every meter.
        raw: The stored list, or None for an entry created before it existed.

    Returns:
        The meters in their stored order; empty when there is no usable list.
    """
    meters: list[AccountIds] = []
    if not isinstance(raw, list):
        return meters
    for item in raw:
        if not isinstance(item, dict) or not isinstance(meter_id := item.get(CONF_METER_ID), str):
            continue
        serial = item.get(CONF_METER_SERIAL)
        serial = serial if isinstance(serial, str) else None
        stored_kind = item.get(CONF_METER_KIND)
        kind = next((known for known in STATISTIC_IDS if known == stored_kind), meter_kind(serial))
        meters.append(
            AccountIds(billing_account_id=billing_account_id, meter_id=meter_id, meter_serial=serial, kind=kind)
        )
    return meters


@contextmanager
def _portal_errors() -> Iterator[None]:
    """Translate client errors into the coordinator's retry and reauth exceptions.

    Raises:
        ConfigEntryAuthFailed: If the stored session is no longer accepted, triggering reauth.
        UpdateFailed: If the portal is busy (with a short ``retry_after``), unreachable or answers
            unexpectedly.
    """
    try:
        yield
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


@dataclass(frozen=True)
class MeterData:
    """One meter's state after a poll.

    Attributes:
        ids: Billing account and meter identifiers, with the meter's kind.
        latest: Most recent day with a non-zero reading, if any.
        total_litres: Running total of every litre imported into the meter's statistic.
        window: Every day fetched for the meter in its last import, oldest first.
    """

    ids: AccountIds
    latest: DailyUsage | None
    total_litres: float
    window: tuple[DailyUsage, ...]


@dataclass(frozen=True)
class SewData:
    """State shared with the entities after each poll.

    The top-level fields describe the mains meter (or, on an account without one, the only meter), as
    they always have, so the original sensors are unchanged.

    Attributes:
        ids: Billing account and meter identifiers.
        latest: Most recent day with a non-zero reading, if any.
        total_litres: Running total of every litre imported into statistics.
        last_poll: When the portal was last read successfully.
        window: Every day fetched in the last poll, oldest first.
        recycled: The recycled-water meter's state, when the property has one.
    """

    ids: AccountIds
    latest: DailyUsage | None
    total_litres: float
    last_poll: datetime
    window: tuple[DailyUsage, ...]
    recycled: MeterData | None = None


class SewCoordinator(DataUpdateCoordinator[SewData]):
    """Poll the portal once a day and keep long-term statistics up to date."""

    config_entry: SewConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SewConfigEntry,
        client: SewClient,
        discovery_client: SewClient | None = None,
    ) -> None:
        """Initialise the coordinator.

        Args:
            hass: Home Assistant instance.
            entry: The config entry that owns this coordinator.
            client: Portal client whose session already holds the stored cookies.
            discovery_client: A client on the same session that may also list the account's meters,
                for an entry created before recycled meters were recognised. Used once, then dropped.
        """
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=self._interval(entry))
        self.client = client
        self.discovery_client = discovery_client
        self.last_contact: datetime | None = None
        self.backfill_task: asyncio.Task[None] | None = None
        self.ids: AccountIds
        self.recycled_meter: AccountIds | None
        self._meter_data: dict[AccountIds, MeterData] = {}
        self._load_meters()

    def _load_meters(self) -> None:
        """Read the meters to poll from the config entry.

        ``ids`` is the meter in ``CONF_METER_ID`` (the mains meter); ``recycled_meter`` is the first
        recycled meter in ``CONF_METERS`` other than that one. An entry without ``CONF_METERS`` polls
        just its stored meter until the one-time upgrade has listed the others.
        """
        data = self.config_entry.data
        billing_account_id = str(data[CONF_BILLING_ACCOUNT_ID])
        stored = meters_from_data(billing_account_id, data.get(CONF_METERS))
        serial = data.get(CONF_METER_SERIAL)
        self.ids = next(
            (meter for meter in stored if meter.meter_id == data[CONF_METER_ID]),
            AccountIds(
                billing_account_id=billing_account_id,
                meter_id=data[CONF_METER_ID],
                meter_serial=serial,
                kind=meter_kind(serial),
            ),
        )
        self.recycled_meter = next(
            (meter for meter in stored if meter.kind == METER_KIND_RECYCLED and meter.kind != self.ids.kind),
            None,
        )

    @property
    def meters(self) -> tuple[AccountIds, ...]:
        """Return every meter that is polled, the mains meter first."""
        return (self.ids,) if self.recycled_meter is None else (self.ids, self.recycled_meter)

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
        # Checked per meter: a meter added later, or a statistic cleared by the upgrade, needs its history
        # even when the other meter's is complete.
        missing = [
            meter for meter in self.meters if not await self._async_has_statistics_on(statistic_id_for(meter), start)
        ]
        if not missing:
            _LOGGER.debug("History back to %s is already imported", start)
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        for attempt in range(1, BACKFILL_ATTEMPTS + 1):
            try:
                await self.async_import_from(start, missing)
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
                _LOGGER.info("Imported %s history back to %s", " and ".join(meter.kind for meter in missing), start)
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                return

    # ------------------------------------------------------------------ polling

    async def _async_update_data(self) -> SewData:
        """Fetch each meter's trailing window (or the full backfill on its first run) and store statistics."""
        yesterday = dt_util.now().date() - timedelta(days=1)
        try:
            with _portal_errors():
                await self._async_check_session()
                await self._async_upgrade_meters()
            # Per meter, so a meter whose statistic is empty (new, or cleared by the upgrade) gets the
            # backfill window while the other keeps the short trailing one.
            starts: dict[AccountIds, date] = {}
            for meter in self.meters:
                first_run = await self._async_last_statistic_day(statistic_id_for(meter)) is None
                window_days = BACKFILL_DAYS if first_run else TRAILING_WINDOW_DAYS
                starts[meter] = yesterday - timedelta(days=window_days - 1)
            data = await self._async_import(starts, yesterday)
        finally:
            # Re-evaluate the delay to the next poll so the daily run stays pinned to the poll time.
            self.update_interval = self._interval(self.config_entry)
        return data

    async def async_poll_now(self) -> None:
        """Poll the portal outside the schedule, for the force-import action and the update button.

        Raises:
            HomeAssistantError: If the poll failed. An expired session has already started reauth.
        """
        # The coordinator swallows poll errors (and starts reauth itself), so report the failure here.
        await self.async_refresh()
        if not self.last_update_success:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="poll_failed",
                translation_placeholders={"error": str(self.last_exception)},
            ) from self.last_exception

    async def async_import_from(self, start: date, meters: Sequence[AccountIds] | None = None) -> None:
        """Import every day from ``start`` to yesterday, then notify entities.

        Args:
            start: First day to import.
            meters: The meters to import; every polled meter when omitted.
        """
        yesterday = dt_util.now().date() - timedelta(days=1)
        if start > yesterday:
            raise ValueError("start date must be before today")
        with _portal_errors():
            await self._async_check_session()
        targets = self.meters if meters is None else meters
        self.async_set_updated_data(await self._async_import(dict.fromkeys(targets, start), yesterday))

    async def _async_check_session(self) -> None:
        """Make sure the stored session is still logged in.

        Raises:
            ConfigEntryAuthFailed: If it is not, triggering reauth.
        """
        if not await self.client.async_is_alive():
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="session_expired")

    async def _async_upgrade_meters(self) -> None:
        """List the account's meters once for an entry created before recycled meters were recognised.

        Those entries hold only the meter discovery picked, the first digital meter the portal listed,
        which on a property with recycled water can be the recycled meter; its readings then went into
        the mains statistic. This stores every meter, points ``CONF_METER_ID`` at the mains meter and, if
        the stored meter was the recycled one, clears the mains statistic so the import that follows
        (the backfill window, then the installation-date backfill) rebuilds it from mains readings.

        This runs here rather than in ``async_migrate_entry`` because it needs the portal: a migration
        cannot use the entry's session before setup, and a failed migration leaves the entry in
        ``MIGRATION_ERROR`` with no retry, whereas here a network failure is an ordinary setup retry
        and a dead session an ordinary reauth.

        Raises:
            SewAuthError: If the session is not accepted.
            SewConnectionError: If the portal cannot be reached or is busy.
        """
        if CONF_METERS in self.config_entry.data or self.discovery_client is None:
            return
        try:
            meters = await self.discovery_client.async_discover_meters()
        except SewProtocolError as err:
            # Not worth blocking the integration over: the stored meter keeps working, and the lookup is
            # tried again on the next load.
            _LOGGER.warning("Could not list the meters on the account; polling the stored meter only: %s", err)
            return
        previous = self.ids
        previous_kind = next((m.kind for m in meters if m.meter_id == previous.meter_id), previous.kind)
        primary = meters[0]
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_BILLING_ACCOUNT_ID: primary.billing_account_id,
                CONF_METER_ID: primary.meter_id,
                CONF_METER_SERIAL: primary.meter_serial,
                CONF_METERS: meters_to_data(meters),
            },
        )
        self.discovery_client = None
        self._load_meters()
        _LOGGER.debug(
            "Listed the account's meters: %s",
            [f"{mask_identifier(meter.meter_serial)} ({meter.kind})" for meter in meters],
        )
        if previous_kind != METER_KIND_RECYCLED:
            return
        start = self.import_from or dt_util.now().date() - timedelta(days=BACKFILL_DAYS)
        _LOGGER.info(
            "The mains statistic held recycled-water readings from an earlier version; clearing it and "
            "importing mains usage again from %s",
            start,
        )
        # Wait until the recorder has run (and committed) the clear, so the window choice that follows sees
        # an empty statistic. ``on_done`` is called from the recorder thread.
        cleared = asyncio.Event()

        def _on_cleared() -> None:
            self.hass.loop.call_soon_threadsafe(cleared.set)

        get_instance(self.hass).async_clear_statistics([STATISTIC_ID_MAINS], on_done=_on_cleared)
        await cleared.wait()

    async def _async_import(self, starts: Mapping[AccountIds, date], end: date) -> SewData:
        """Fetch ``start``..``end`` for each meter, import statistics and build the shared state.

        Args:
            starts: First day to fetch, per meter.
            end: Last day to fetch, for every meter.

        Raises:
            ConfigEntryAuthFailed: If the stored session is no longer accepted, triggering reauth.
            UpdateFailed: If the portal is busy (with a short ``retry_after``), unreachable or answers
                unexpectedly.
        """
        fetched: list[tuple[AccountIds, list[DailyUsage]]] = []
        with _portal_errors():
            for meter, start in starts.items():
                fetched.append((meter, await self.client.async_fetch_usage(meter, start, end)))

        self.last_contact = dt_util.utcnow()
        self._async_store_cookies()
        for meter, usage in fetched:
            total = await self._async_import_statistics(statistic_id_for(meter), usage)
            latest = next((day for day in reversed(usage) if day.available and day.litres > 0), None)
            self._meter_data[meter] = MeterData(ids=meter, latest=latest, total_litres=total, window=tuple(usage))
            _LOGGER.debug(
                "Imported %d %s day(s) %s..%s; latest reading %s",
                len(usage),
                meter.kind,
                starts[meter],
                end,
                latest.day if latest else None,
            )
        mains = self._meter_data[self.ids]
        recycled = None if self.recycled_meter is None else self._meter_data.get(self.recycled_meter)
        return SewData(
            ids=mains.ids,
            latest=mains.latest,
            total_litres=mains.total_litres,
            last_poll=dt_util.utcnow(),
            window=mains.window,
            recycled=recycled,
        )

    def _async_store_cookies(self) -> None:
        """Persist the (possibly refreshed) session cookies so a restart needs no new login."""
        cookies = self.client.export_cookies()
        if cookies != self.config_entry.data.get(CONF_COOKIES):
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_COOKIES: cookies}
            )

    # --------------------------------------------------------------- statistics

    @staticmethod
    def _metadata(statistic_id: str) -> StatisticMetaData:
        """Describe a water external statistic used by the Energy dashboard."""
        return StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=STATISTIC_NAMES[statistic_id],
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.LITERS,
        )

    async def _async_last_statistic_day(self, statistic_id: str) -> date | None:
        """Return the local day of the newest stored statistic, or ``None`` before the first import."""
        rows = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if not (stats := rows.get(statistic_id)):
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

    async def _async_has_statistics_on(self, statistic_id: str, day: date) -> bool:
        """Return whether any statistic row exists for the local day ``day``."""
        row_start = self._row_start(day)
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            row_start,
            row_start + timedelta(days=1),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        return bool(rows.get(statistic_id))

    async def _async_sum_before(self, statistic_id: str, day: date) -> float:
        """Return the running sum of the newest statistic row before ``day`` (0 if there is none)."""
        row_start = self._row_start(day)
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            row_start - timedelta(days=LOOKBACK_DAYS),
            row_start,
            {statistic_id},
            # Hourly buckets so the cut-off is exact: a day bucket would start at midnight and swallow
            # the first row of the window itself.
            "hour",
            None,
            {"sum"},
        )
        if not (stats := rows.get(statistic_id)):
            return 0.0
        return float(stats[-1].get("sum") or 0.0)

    async def _async_import_statistics(self, statistic_id: str, usage: list[DailyUsage]) -> float:
        """Write one statistic row per hour and return the running total after the last day.

        Rows are keyed by their start time, so re-importing a day simply overwrites its hours; the
        running sum is rebuilt from the value recorded just before the window so corrections stay
        consistent.
        """
        if not usage:
            rows = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
            )
            stats = rows.get(statistic_id)
            return float(stats[0].get("sum") or 0.0) if stats else 0.0
        running = await self._async_sum_before(statistic_id, usage[0].day)
        statistics: list[StatisticData] = []
        for day in usage:
            for start, litres in self._hourly_rows(day):
                running += litres
                statistics.append(StatisticData(start=start, state=litres, sum=running))
        async_add_external_statistics(self.hass, self._metadata(statistic_id), statistics)
        return running
