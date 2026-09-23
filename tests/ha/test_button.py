"""Tests for the button platform."""

from __future__ import annotations

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sew_water.sew_client import SewConnectionError

from .conftest import FakeClient

UPDATE_NOW = "button.south_east_water_update_now"


async def press(hass: HomeAssistant) -> None:
    """Press the update button and let Home Assistant settle."""
    await hass.services.async_call(BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: UPDATE_NOW}, blocking=True)
    await hass.async_block_till_done()


async def test_button_is_on_the_meter_device(
    hass: HomeAssistant, setup_integration: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    entry = entity_registry.async_get(UPDATE_NOW)
    assert entry is not None
    assert entry.unique_id == f"{setup_integration.entry_id}_update_now"
    assert entry.translation_key == "update_now"
    sensor = entity_registry.async_get("sensor.south_east_water_total_usage")
    assert sensor is not None
    assert entry.device_id == sensor.device_id
    state = hass.states.get(UPDATE_NOW)
    assert state is not None
    assert state.state == STATE_UNKNOWN


async def test_press_polls_now(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    fetches_before = len(fake_client.fetch_ranges)
    await press(hass)
    assert len(fake_client.fetch_ranges) == fetches_before + 1
    state = hass.states.get(UPDATE_NOW)
    assert state is not None
    assert state.state != STATE_UNKNOWN


async def test_press_reports_failure_and_stays_available(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.fetch_error = SewConnectionError("down")
    with pytest.raises(HomeAssistantError, match="Poll failed") as excinfo:
        await press(hass)
    assert excinfo.value.translation_key == "poll_failed"
    state = hass.states.get(UPDATE_NOW)
    assert state is not None
    assert state.state != STATE_UNAVAILABLE

    # The retry the button exists for.
    fake_client.fetch_error = None
    await press(hass)
    assert hass.states.get("sensor.south_east_water_total_usage").state != STATE_UNAVAILABLE
