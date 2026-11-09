"""Tests for properties with a mains and a recycled-water meter, and the upgrade of pre-2.3 entries."""

from __future__ import annotations

from datetime import date, timedelta
from functools import partial
from typing import Any
from unittest.mock import patch

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import async_add_external_statistics, get_metadata
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.sew_water import coordinator as coordinator_module
from custom_components.sew_water.const import (
    CONF_BILLING_ACCOUNT_ID,
    CONF_IMPORT_FROM,
    CONF_METER_ID,
    CONF_METER_SERIAL,
    CONF_METERS,
    DOMAIN,
    ENTRY_TITLE,
    SERVICE_ATTR_START_DATE,
    SERVICE_FORCE_IMPORT,
    SERVICE_IMPORT_FROM_DATE,
    STATISTIC_ID_MAINS,
    STATISTIC_ID_RECYCLED,
)
from custom_components.sew_water.coordinator import SewCoordinator, meters_from_data, meters_to_data
from custom_components.sew_water.sew_client import AccountIds, LoginResult, SewAuthError, SewProtocolError

from .conftest import (
    BACKFILL_DAYS,
    BILLING_ACCOUNT_ID,
    IDS,
    METER_ID,
    METER_SERIAL,
    RECYCLED_IDS,
    RECYCLED_METER_ID,
    RECYCLED_SERIAL,
    TRAILING_WINDOW_DAYS,
    USERNAME,
    FakeClient,
    entry_data,
)
from .test_coordinator import read_rows

DAILY = "sensor.south_east_water_daily_usage"
TOTAL = "sensor.south_east_water_total_usage"
RECYCLED_DAILY = "sensor.south_east_water_recycled_daily_usage"
RECYCLED_TOTAL = "sensor.south_east_water_recycled_total_usage"
RECYCLED_LAST_DATE = "sensor.south_east_water_recycled_last_reading_date"
REDACTED = "**REDACTED**"
# Litres per hour for each meter, so their statistics can be told apart.
MAINS_LPH = 10
RECYCLED_LPH = 3


def yesterday() -> date:
    """Local calendar day before today."""
    return dt_util.now().date() - timedelta(days=1)


def make_entry(data: dict[str, Any]) -> MockConfigEntry:
    """Build a current-version entry with the given data."""
    return MockConfigEntry(domain=DOMAIN, title=ENTRY_TITLE, unique_id=USERNAME, version=2, minor_version=2, data=data)


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    """Add and set up an entry, and let the recorder commit."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return entry


@pytest.fixture
def two_meters(fake_client: FakeClient) -> FakeClient:
    """A property with both meters, each with its own hourly usage."""
    fake_client.meters = [IDS, RECYCLED_IDS]
    fake_client.litres_per_hour_by_serial = {
        METER_SERIAL: lambda _day: MAINS_LPH,
        RECYCLED_SERIAL: lambda _day: RECYCLED_LPH,
    }
    return fake_client


def fetched_serials(fake_client: FakeClient) -> list[str | None]:
    """Return the serial of every usage fetch, in order."""
    return [serial for serial, _start, _end in fake_client.fetches]


async def seed_mains_statistic(hass: HomeAssistant, day: date, litres_per_hour: float) -> None:
    """Write one day of hourly rows into the mains statistic, as an earlier version would have."""
    start = dt_util.as_utc(dt_util.start_of_local_day(day))
    rows = [
        StatisticData(start=start + timedelta(hours=h), state=litres_per_hour, sum=litres_per_hour * (h + 1))
        for h in range(24)
    ]
    async_add_external_statistics(hass, SewCoordinator._metadata(STATISTIC_ID_MAINS), rows)
    await async_wait_recording_done(hass)


# ------------------------------------------------------------------------- polling


async def test_two_meters_write_two_statistics(hass: HomeAssistant, two_meters: FakeClient) -> None:
    entry = await setup_entry(hass, make_entry(entry_data([IDS, RECYCLED_IDS])))
    assert entry.state is ConfigEntryState.LOADED
    # One fetch per meter, mains first, both over the first-run window.
    assert fetched_serials(two_meters) == [METER_SERIAL, RECYCLED_SERIAL]
    for _serial, start, end in two_meters.fetches:
        assert end == yesterday()
        assert (end - start).days + 1 == BACKFILL_DAYS

    mains = await read_rows(hass)
    recycled = await read_rows(hass, STATISTIC_ID_RECYCLED)
    assert len(mains) == len(recycled) == BACKFILL_DAYS * 24
    assert mains[-1][2] == MAINS_LPH * 24 * BACKFILL_DAYS
    assert recycled[-1][2] == RECYCLED_LPH * 24 * BACKFILL_DAYS

    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={STATISTIC_ID_MAINS, STATISTIC_ID_RECYCLED})
    )
    assert metadata[STATISTIC_ID_MAINS][1]["name"] == "South East Water mains usage"
    assert metadata[STATISTIC_ID_RECYCLED][1]["name"] == "South East Water recycled usage"

    data = entry.runtime_data.data
    assert data.ids == IDS
    assert data.total_litres == MAINS_LPH * 24 * BACKFILL_DAYS
    assert data.recycled is not None
    assert data.recycled.ids == RECYCLED_IDS
    assert data.recycled.total_litres == RECYCLED_LPH * 24 * BACKFILL_DAYS
    assert data.recycled.latest is not None and data.recycled.latest.day == yesterday()
    assert len(data.recycled.window) == BACKFILL_DAYS


async def test_new_recycled_meter_gets_backfill_while_mains_keeps_trailing_window(
    hass: HomeAssistant, two_meters: FakeClient
) -> None:
    """The first-run window is chosen per meter, from that meter's own statistic."""
    entry = await setup_entry(hass, make_entry(entry_data([IDS])))
    assert fetched_serials(two_meters) == [METER_SERIAL]

    hass.config_entries.async_update_entry(entry, data=entry_data([IDS, RECYCLED_IDS]))
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    mains_fetch, recycled_fetch = two_meters.fetches[-2:]
    assert mains_fetch[0] == METER_SERIAL
    assert (mains_fetch[2] - mains_fetch[1]).days + 1 == TRAILING_WINDOW_DAYS
    assert recycled_fetch[0] == RECYCLED_SERIAL
    assert (recycled_fetch[2] - recycled_fetch[1]).days + 1 == BACKFILL_DAYS


async def test_force_import_and_import_from_date_cover_both_meters(hass: HomeAssistant, two_meters: FakeClient) -> None:
    await setup_entry(hass, make_entry(entry_data([IDS, RECYCLED_IDS])))
    fetches = len(two_meters.fetches)
    await hass.services.async_call(DOMAIN, SERVICE_FORCE_IMPORT, {}, blocking=True)
    assert fetched_serials(two_meters)[fetches:] == [METER_SERIAL, RECYCLED_SERIAL]

    start = yesterday() - timedelta(days=10)
    await hass.services.async_call(
        DOMAIN, SERVICE_IMPORT_FROM_DATE, {SERVICE_ATTR_START_DATE: start.isoformat()}, blocking=True
    )
    assert two_meters.fetches[-2:] == [(METER_SERIAL, start, yesterday()), (RECYCLED_SERIAL, start, yesterday())]


async def test_update_button_covers_both_meters(hass: HomeAssistant, two_meters: FakeClient) -> None:
    await setup_entry(hass, make_entry(entry_data([IDS, RECYCLED_IDS])))
    fetches = len(two_meters.fetches)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.south_east_water_update_now"}, blocking=True
    )
    assert fetched_serials(two_meters)[fetches:] == [METER_SERIAL, RECYCLED_SERIAL]


async def test_backfill_fills_a_meter_whose_history_is_missing(hass: HomeAssistant, two_meters: FakeClient) -> None:
    """Mains history back to the installation date exists; the recycled meter's does not."""
    start = yesterday() - timedelta(days=BACKFILL_DAYS + 4)
    entry = await setup_entry(hass, make_entry(entry_data([IDS], **{CONF_IMPORT_FROM: start.isoformat()})))
    task = entry.runtime_data.backfill_task
    assert task is not None
    await task
    assert two_meters.fetches[-1] == (METER_SERIAL, start, yesterday())

    hass.config_entries.async_update_entry(
        entry, data=entry_data([IDS, RECYCLED_IDS], **{CONF_IMPORT_FROM: start.isoformat()})
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    task = entry.runtime_data.backfill_task
    assert task is not None
    await task
    await hass.async_block_till_done()
    # Only the recycled meter is fetched back to the installation date.
    assert two_meters.fetches[-1] == (RECYCLED_SERIAL, start, yesterday())
    recycled = await read_rows(hass, STATISTIC_ID_RECYCLED)
    assert dt_util.as_local(recycled[0][0]).date() == start
    assert recycled[-1][2] == RECYCLED_LPH * 24 * ((yesterday() - start).days + 1)
    # The mains data in the sensors is kept from the poll before.
    assert entry.runtime_data.data.total_litres == MAINS_LPH * 24 * ((yesterday() - start).days + 1)


def test_meters_round_trip_and_malformed_items_are_skipped() -> None:
    raw = [*meters_to_data([IDS, RECYCLED_IDS]), "junk", {CONF_METER_SERIAL: "no id"}]
    raw.append({CONF_METER_ID: "a1K-unknown-kind", CONF_METER_SERIAL: 42, "meter_kind": "fizzy"})
    raw.append({CONF_METER_ID: "a1K-no-kind", CONF_METER_SERIAL: "RAHA111111"})
    meters = meters_from_data(BILLING_ACCOUNT_ID, raw)
    assert meters[:2] == [IDS, RECYCLED_IDS]
    # A kind that is not known is worked out again from the serial.
    assert meters[2] == AccountIds(BILLING_ACCOUNT_ID, "a1K-unknown-kind", None, "mains")
    assert meters[3] == AccountIds(BILLING_ACCOUNT_ID, "a1K-no-kind", "RAHA111111", "recycled")
    assert meters_from_data(BILLING_ACCOUNT_ID, None) == []


# -------------------------------------------------------------------------- upgrade


async def test_upgrade_replaces_recycled_meter_and_rebuilds_mains_statistic(
    hass: HomeAssistant, two_meters: FakeClient
) -> None:
    """A pre-2.3 entry that stored the recycled meter: its readings in the mains statistic are replaced."""
    old_day = yesterday() - timedelta(days=BACKFILL_DAYS + 20)
    await seed_mains_statistic(hass, old_day, 99)
    legacy = make_entry(entry_data(None, **{CONF_METER_ID: RECYCLED_METER_ID, CONF_METER_SERIAL: RECYCLED_SERIAL}))
    with patch.object(coordinator_module._LOGGER, "info") as info:
        entry = await setup_entry(hass, legacy)
    assert entry.state is ConfigEntryState.LOADED

    # The meters were listed once with a client that may send the lookup, then the entry was corrected.
    assert ("integration-discovery", False) in two_meters.constructed
    assert two_meters.calls.count(("discover_meters", None)) == 1
    assert entry.data[CONF_METER_ID] == METER_ID
    assert entry.data[CONF_METER_SERIAL] == METER_SERIAL
    assert entry.data[CONF_BILLING_ACCOUNT_ID] == BILLING_ACCOUNT_ID
    assert entry.data[CONF_METERS] == meters_to_data([IDS, RECYCLED_IDS])

    # The old recycled rows are gone from the mains statistic, which now holds mains readings only.
    mains = await read_rows(hass)
    assert len(mains) == BACKFILL_DAYS * 24
    assert dt_util.as_local(mains[0][0]).date() == yesterday() - timedelta(days=BACKFILL_DAYS - 1)
    assert {state for _start, state, _sum in mains} == {MAINS_LPH}
    assert mains[-1][2] == MAINS_LPH * 24 * BACKFILL_DAYS
    recycled = await read_rows(hass, STATISTIC_ID_RECYCLED)
    assert recycled[-1][2] == RECYCLED_LPH * 24 * BACKFILL_DAYS

    lines = [str(call.args[0]) % call.args[1:] for call in info.call_args_list]
    expected_start = dt_util.now().date() - timedelta(days=BACKFILL_DAYS)
    assert lines == [
        "The mains statistic held recycled-water readings from an earlier version; clearing it and importing "
        f"mains usage again from {expected_start}"
    ]

    # The sensors now show the mains meter, and the recycled meter has its own.
    daily = hass.states.get(DAILY)
    recycled_daily = hass.states.get(RECYCLED_DAILY)
    assert daily is not None and daily.state == str(MAINS_LPH * 24)
    assert recycled_daily is not None and recycled_daily.state == str(RECYCLED_LPH * 24)

    # A reload does not list the meters or clear anything again.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert two_meters.calls.count(("discover_meters", None)) == 1
    assert len(await read_rows(hass)) == BACKFILL_DAYS * 24


async def test_upgrade_checks_the_session_after_listing_the_meters(hass: HomeAssistant, two_meters: FakeClient) -> None:
    """The lookup loads the home page on its own client, so the polling client refreshes its token afterwards."""
    await setup_entry(hass, make_entry(entry_data(None)))
    names = [name for name, _arg in two_meters.calls]
    assert names.index("discover_meters") < names.index("is_alive") < names.index("fetch_usage")


async def test_upgrade_reimports_mains_from_the_installation_date(hass: HomeAssistant, two_meters: FakeClient) -> None:
    start = yesterday() - timedelta(days=BACKFILL_DAYS + 9)
    await seed_mains_statistic(hass, start, 99)
    legacy = make_entry(
        entry_data(
            None,
            **{
                CONF_IMPORT_FROM: start.isoformat(),
                CONF_METER_ID: RECYCLED_METER_ID,
                CONF_METER_SERIAL: RECYCLED_SERIAL,
            },
        )
    )
    with patch.object(coordinator_module._LOGGER, "info") as info:
        entry = await setup_entry(hass, legacy)
        task = entry.runtime_data.backfill_task
        assert task is not None
        await task
        await hass.async_block_till_done()
    assert f"from {start}" in str(info.call_args_list[0].args[0]) % info.call_args_list[0].args[1:]

    # Both meters are fetched back to the installation date, because neither statistic had that day.
    assert two_meters.fetches[-2:] == [(METER_SERIAL, start, yesterday()), (RECYCLED_SERIAL, start, yesterday())]
    days = (yesterday() - start).days + 1
    mains = await read_rows(hass)
    assert dt_util.as_local(mains[0][0]).date() == start
    assert {state for _start, state, _sum in mains} == {MAINS_LPH}
    assert mains[-1][2] == MAINS_LPH * 24 * days
    state = hass.states.get(TOTAL)
    assert state is not None and float(state.state) == MAINS_LPH * 24 * days


async def test_upgrade_of_an_entry_that_stored_the_mains_meter_keeps_its_statistic(
    hass: HomeAssistant, two_meters: FakeClient
) -> None:
    old_day = yesterday() - timedelta(days=BACKFILL_DAYS + 20)
    await seed_mains_statistic(hass, old_day, 7)
    with patch.object(coordinator_module._LOGGER, "info") as info:
        entry = await setup_entry(hass, make_entry(entry_data(None)))
    info.assert_not_called()
    assert entry.data[CONF_METERS] == meters_to_data([IDS, RECYCLED_IDS])
    assert entry.data[CONF_METER_ID] == METER_ID
    mains = await read_rows(hass)
    # The old day is still there, and the mains meter took the trailing window because it has history.
    assert dt_util.as_local(mains[0][0]).date() == old_day
    mains_fetch = two_meters.fetches[0]
    assert mains_fetch[0] == METER_SERIAL
    assert (mains_fetch[2] - mains_fetch[1]).days + 1 == TRAILING_WINDOW_DAYS
    assert hass.states.get(RECYCLED_DAILY) is not None


async def test_upgrade_lookup_failure_keeps_polling_the_stored_meter(
    hass: HomeAssistant, two_meters: FakeClient
) -> None:
    two_meters.discover_error = SewProtocolError("lookup changed")
    with patch.object(coordinator_module._LOGGER, "warning") as warning:
        entry = await setup_entry(hass, make_entry(entry_data(None)))
    assert entry.state is ConfigEntryState.LOADED
    warning.assert_called_once()
    assert CONF_METERS not in entry.data
    assert fetched_serials(two_meters) == [METER_SERIAL]
    assert hass.states.get(RECYCLED_DAILY) is None

    # Tried again on the next load.
    two_meters.discover_error = None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data[CONF_METERS] == meters_to_data([IDS, RECYCLED_IDS])


async def test_upgrade_with_rejected_session_starts_reauth(hass: HomeAssistant, two_meters: FakeClient) -> None:
    two_meters.discover_error = SewAuthError("expired")
    entry = make_entry(entry_data(None))
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(True for _ in entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert CONF_METERS not in entry.data


async def test_reauth_of_a_legacy_entry_leaves_the_upgrade_to_the_coordinator(
    hass: HomeAssistant, two_meters: FakeClient
) -> None:
    """Reauth must not overwrite the stored meter, or the mislabelled statistic would never be repaired."""
    old_day = yesterday() - timedelta(days=BACKFILL_DAYS + 20)
    await seed_mains_statistic(hass, old_day, 99)
    entry = make_entry(entry_data(None, **{CONF_METER_ID: RECYCLED_METER_ID, CONF_METER_SERIAL: RECYCLED_SERIAL}))
    two_meters.alive = False
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    two_meters.alive = True
    two_meters.login_result = LoginResult(mfa_required=False, channels=())
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await async_wait_recording_done(hass)

    # The reload after reauth ran the upgrade: the mains meter is stored and the statistic was rebuilt.
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_METER_ID] == METER_ID
    assert entry.data[CONF_METERS] == meters_to_data([IDS, RECYCLED_IDS])
    mains = await read_rows(hass)
    assert {state for _start, state, _sum in mains} == {MAINS_LPH}


async def test_config_flow_stores_the_mains_meter_and_every_meter(hass: HomeAssistant, two_meters: FakeClient) -> None:
    two_meters.login_result = LoginResult(mfa_required=False, channels=())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "pw"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_METER_ID] == METER_ID
    assert result["data"][CONF_METER_SERIAL] == METER_SERIAL
    assert result["data"][CONF_METERS] == [
        {"meter_id": METER_ID, "meter_kind": "mains", "meter_serial": METER_SERIAL},
        {"meter_id": RECYCLED_METER_ID, "meter_kind": "recycled", "meter_serial": RECYCLED_SERIAL},
    ]


# -------------------------------------------------------------------------- sensors


async def test_recycled_sensors_only_with_a_recycled_meter(
    hass: HomeAssistant, setup_integration: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    unique_ids = {
        entry.unique_id for entry in er.async_entries_for_config_entry(entity_registry, setup_integration.entry_id)
    }
    assert not any("recycled" in unique_id for unique_id in unique_ids)
    assert hass.states.get(RECYCLED_DAILY) is None


async def test_recycled_sensors(
    hass: HomeAssistant, two_meters: FakeClient, entity_registry: er.EntityRegistry
) -> None:
    entry = await setup_entry(hass, make_entry(entry_data([IDS, RECYCLED_IDS])))
    for key in ("recycled_daily_usage", "recycled_total_usage", "recycled_last_reading_date"):
        assert entity_registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}") is not None

    # The mains sensors are unchanged and read the mains meter.
    daily = hass.states.get(DAILY)
    total = hass.states.get(TOTAL)
    assert daily is not None and daily.state == str(MAINS_LPH * 24)
    assert total is not None and float(total.state) == MAINS_LPH * 24 * BACKFILL_DAYS

    recycled_daily = hass.states.get(RECYCLED_DAILY)
    assert recycled_daily is not None
    assert recycled_daily.state == str(RECYCLED_LPH * 24)
    assert recycled_daily.attributes["reading_date"] == yesterday().isoformat()
    assert recycled_daily.attributes["hourly_readings"] == [RECYCLED_LPH] * 24
    assert recycled_daily.attributes["last_reset"] == dt_util.start_of_local_day(yesterday()).isoformat()
    assert recycled_daily.attributes["friendly_name"] == "South East Water Recycled daily usage"
    recycled_total = hass.states.get(RECYCLED_TOTAL)
    assert recycled_total is not None
    assert float(recycled_total.state) == RECYCLED_LPH * 24 * BACKFILL_DAYS
    assert recycled_total.attributes["state_class"] == "total_increasing"

    # The date sensor is diagnostic and off by default, like its mains counterpart.
    registry_entry = entity_registry.async_get(RECYCLED_LAST_DATE)
    assert registry_entry is not None
    assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(RECYCLED_LAST_DATE) is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_recycled_sensors_unknown_without_readings(hass: HomeAssistant, two_meters: FakeClient) -> None:
    two_meters.litres_per_hour_by_serial[RECYCLED_SERIAL] = lambda _day: 0
    await setup_entry(hass, make_entry(entry_data([IDS, RECYCLED_IDS])))
    for entity_id in (RECYCLED_DAILY, RECYCLED_LAST_DATE):
        state = hass.states.get(entity_id)
        assert state is not None and state.state == "unknown"
    last_date = hass.states.get("sensor.south_east_water_last_reading_date")
    assert last_date is not None and last_date.state == yesterday().isoformat()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_recycled_sensors_before_the_recycled_meter_is_read(hass: HomeAssistant, two_meters: FakeClient) -> None:
    """Data without the recycled meter's part (as on a failed import of it) leaves its sensors unknown."""
    entry = await setup_entry(hass, make_entry(entry_data([IDS, RECYCLED_IDS])))
    coordinator: SewCoordinator = entry.runtime_data
    coordinator.async_set_updated_data(
        coordinator_module.SewData(
            ids=IDS,
            latest=None,
            total_litres=0.0,
            last_poll=dt_util.utcnow(),
            window=(),
        )
    )
    await hass.async_block_till_done()
    for entity_id in (RECYCLED_DAILY, RECYCLED_TOTAL, RECYCLED_LAST_DATE):
        state = hass.states.get(entity_id)
        assert state is not None and state.state == "unknown"


# ---------------------------------------------------------------------- diagnostics


async def test_diagnostics_redact_every_meter(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, two_meters: FakeClient
) -> None:
    entry = await setup_entry(hass, make_entry(entry_data([IDS, RECYCLED_IDS])))
    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert result["entry"]["data"][CONF_METERS] == REDACTED
    text = str(result)
    for identifier in (METER_ID, METER_SERIAL, RECYCLED_METER_ID, RECYCLED_SERIAL, BILLING_ACCOUNT_ID):
        assert identifier not in text

    coordinator = result["coordinator"]
    assert coordinator["meter_kinds"] == ["mains", "recycled"]
    recycled = coordinator["recycled"]
    assert recycled["latest"]["litres"] == RECYCLED_LPH * 24
    assert recycled["latest"]["serial"] == REDACTED
    assert recycled["total_litres"] == RECYCLED_LPH * 24 * BACKFILL_DAYS
    assert recycled["window_days"] == BACKFILL_DAYS
