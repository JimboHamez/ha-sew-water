"""Tests for the sensor platform."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import ATTR_ATTRIBUTION, STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sew_water.const import ATTRIBUTION, DOMAIN, MANUFACTURER, SERVICE_FORCE_IMPORT
from custom_components.sew_water.sew_client import SewConnectionError

from .conftest import BACKFILL_DAYS, FakeClient

DAILY = "sensor.south_east_water_daily_usage"
TOTAL = "sensor.south_east_water_total_usage"
LAST_DATE = "sensor.south_east_water_last_reading_date"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_entities_are_created_on_one_device(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    entries = er.async_entries_for_config_entry(entity_registry, setup_integration.entry_id)
    assert {entry.entity_id for entry in entries} == {DAILY, TOTAL, LAST_DATE}
    assert {entry.unique_id for entry in entries} == {
        f"{setup_integration.entry_id}_daily_usage",
        f"{setup_integration.entry_id}_total_usage",
        f"{setup_integration.entry_id}_last_reading_date",
    }
    device = device_registry.async_get_device(identifiers={(DOMAIN, setup_integration.entry_id)})
    assert device is not None
    assert device.manufacturer == MANUFACTURER
    assert device.name == MANUFACTURER
    assert len({entry.device_id for entry in entries}) == 1


async def test_daily_usage_reports_latest_day(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    yesterday = dt_util.now().date() - timedelta(days=1)
    state = hass.states.get(DAILY)
    assert state is not None
    assert state.state == "240"
    assert state.attributes["unit_of_measurement"] == "L"
    assert state.attributes["device_class"] == "volume"
    assert state.attributes["state_class"] == "total"
    assert state.attributes["last_reset"] == dt_util.start_of_local_day(yesterday).isoformat()
    assert state.attributes[ATTR_ATTRIBUTION] == ATTRIBUTION
    assert state.attributes["reading_date"] == yesterday.isoformat()
    assert state.attributes["hourly_readings"] == [10] * 24


async def test_total_usage_is_cumulative(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    state = hass.states.get(TOTAL)
    assert state is not None
    # First run backfills BACKFILL_DAYS days at 240 L each.
    assert float(state.state) == BACKFILL_DAYS * 240
    assert state.attributes["device_class"] == "water"
    assert state.attributes["state_class"] == "total_increasing"


async def test_last_reading_date_is_diagnostic_and_disabled_by_default(
    hass: HomeAssistant, setup_integration: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    entry = entity_registry.async_get(LAST_DATE)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(LAST_DATE) is None
    # The two headline sensors stay enabled.
    for entity_id in (DAILY, TOTAL):
        enabled = entity_registry.async_get(entity_id)
        assert enabled is not None and enabled.disabled_by is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_last_reading_date(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    yesterday = dt_util.now().date() - timedelta(days=1)
    state = hass.states.get(LAST_DATE)
    assert state is not None
    assert state.state == yesterday.isoformat()
    assert state.attributes["device_class"] == "date"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_skips_days_without_readings(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    """Yesterday has not arrived yet, so the latest reading is the day before."""
    yesterday = dt_util.now().date() - timedelta(days=1)
    fake_client.litres_per_hour = lambda day: 0 if day == yesterday else 5
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    daily = hass.states.get(DAILY)
    last_date = hass.states.get(LAST_DATE)
    assert daily is not None and last_date is not None
    assert daily.state == "120"
    assert last_date.state == (yesterday - timedelta(days=1)).isoformat()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_unknown_when_no_day_has_data(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.litres_per_hour = lambda _day: 0
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    daily = hass.states.get(DAILY)
    last_date = hass.states.get(LAST_DATE)
    assert daily is not None and last_date is not None
    assert daily.state == STATE_UNKNOWN
    assert daily.attributes["reading_date"] is None
    assert last_date.state == STATE_UNKNOWN


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_entities_unavailable_after_failed_poll(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.fetch_error = SewConnectionError("down")
    await hass.services.async_call(DOMAIN, SERVICE_FORCE_IMPORT, {}, blocking=True)
    await hass.async_block_till_done()
    for entity_id in (DAILY, TOTAL, LAST_DATE):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == STATE_UNAVAILABLE

    fake_client.fetch_error = None
    await hass.services.async_call(DOMAIN, SERVICE_FORCE_IMPORT, {}, blocking=True)
    await hass.async_block_till_done()
    state = hass.states.get(DAILY)
    assert state is not None
    assert state.state == "240"


async def test_total_grows_with_new_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    """Re-polling the same window does not double count; a changed day adjusts the total."""
    before = hass.states.get(TOTAL)
    assert before is not None
    await hass.services.async_call(DOMAIN, SERVICE_FORCE_IMPORT, {}, blocking=True)
    await hass.async_block_till_done()
    unchanged = hass.states.get(TOTAL)
    assert unchanged is not None
    assert float(unchanged.state) == float(before.state)

    yesterday = dt_util.now().date() - timedelta(days=1)
    fake_client.litres_per_hour = lambda day: 20 if day == yesterday else 10
    await hass.services.async_call(DOMAIN, SERVICE_FORCE_IMPORT, {}, blocking=True)
    await hass.async_block_till_done()
    after = hass.states.get(TOTAL)
    assert after is not None
    assert float(after.state) == float(before.state) + 240
