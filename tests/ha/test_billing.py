"""Tests for the billing-period sensors."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from homeassistant.components.recorder import get_instance
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sew_water.const import (
    CONF_BILLING_PERIOD,
    CONF_LAST_READ_DATE,
    CONF_POLL_TIME,
    CONF_PRICE_STEP1,
    CONF_READ_DAYS,
    CONF_SCAN_INTERVAL,
    CONF_TARIFF,
    DEFAULT_POLL_TIME,
    DEFAULT_SCAN_INTERVAL,
    TARIFF_CUSTOM,
    TARIFF_WATER_SEWAGE,
)
from custom_components.sew_water.pricing import published_rates

from .conftest import IDS, FakeClient
from .test_cost import entry_with_options
from .test_meters import setup_entry, yesterday

USAGE = "sensor.south_east_water_billing_period_usage"
COST = "sensor.south_east_water_billing_period_usage_cost"
REMAINING = "sensor.south_east_water_step_1_litres_left"
ALLOWANCE = "sensor.south_east_water_step_1_daily_allowance"
PROJECTED_USAGE = "sensor.south_east_water_projected_billing_period_usage"
PROJECTED_COST = "sensor.south_east_water_projected_billing_period_usage_cost"
BILLING_ENTITIES = {USAGE, COST, REMAINING, ALLOWANCE, PROJECTED_USAGE, PROJECTED_COST}
# The fake portal's default: 10 L an hour.
LITRES_PER_DAY = 240


def billing_entry(start: date, read_days: int = 91, **options: object) -> MockConfigEntry:
    """Build a one-meter entry whose current billing period starts on ``start`` (the day after a read)."""
    read_date = start - timedelta(days=1)
    return entry_with_options(
        [IDS], **{CONF_LAST_READ_DATE: read_date.isoformat(), CONF_READ_DAYS: read_days, **options}
    )


def number(hass: HomeAssistant, entity_id: str) -> float:
    """Return a sensor's numeric state."""
    state = hass.states.get(entity_id)
    assert state is not None
    return float(state.state)


async def test_no_billing_period_means_no_sensors(
    hass: HomeAssistant, setup_integration: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    entity_ids = {
        entry.entity_id for entry in er.async_entries_for_config_entry(entity_registry, setup_integration.entry_id)
    }
    assert not entity_ids & BILLING_ENTITIES
    assert setup_integration.runtime_data.data.billing is None


async def test_billing_period_sensors(hass: HomeAssistant, fake_client: FakeClient) -> None:
    start = yesterday() - timedelta(days=4)
    await setup_entry(hass, billing_entry(start))
    rates = published_rates(yesterday(), TARIFF_WATER_SEWAGE)
    assert rates.step2 is not None

    # Five days of 240 L in the period so far.
    assert number(hass, USAGE) == 5 * LITRES_PER_DAY
    state = hass.states.get(USAGE)
    assert state is not None
    assert state.attributes["period_start"] == start.isoformat()
    # Reads 91 days apart: the period ends on the next read day, and the bill counts 90 days.
    assert state.attributes["period_end"] == (start + timedelta(days=90)).isoformat()
    assert state.attributes["billed_days"] == 90
    assert state.attributes["days_with_data"] == 5
    assert state.attributes["step1_allowance"] == 440 * 90
    assert state.attributes["last_reset"] == dt_util.start_of_local_day(start).isoformat()

    assert number(hass, COST) == round(5 * LITRES_PER_DAY * rates.step1 / 1000, 2)
    cost = hass.states.get(COST)
    assert cost is not None
    assert cost.attributes["unit_of_measurement"] == hass.config.currency
    assert cost.attributes["device_class"] == "monetary"

    assert number(hass, REMAINING) == 440 * 90 - 5 * LITRES_PER_DAY
    # 91 calendar days, 5 with readings.
    assert number(hass, ALLOWANCE) == round((440 * 90 - 5 * LITRES_PER_DAY) / 86, 2)
    assert number(hass, PROJECTED_USAGE) == 91 * LITRES_PER_DAY
    assert number(hass, PROJECTED_COST) == round(91 * LITRES_PER_DAY * rates.step1 / 1000, 2)
    projected = hass.states.get(PROJECTED_COST)
    assert projected is not None
    assert "state_class" not in projected.attributes


async def test_period_older_than_the_trailing_window_reads_the_statistic(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    # The first run imports six days; the next poll only three, so the period's start lies before its window.
    start = yesterday() - timedelta(days=4)
    entry = await setup_entry(hass, billing_entry(start))
    await entry.runtime_data.async_poll_now()
    first, _last = fake_client.fetch_ranges[-1]
    assert first > start
    assert number(hass, USAGE) == 5 * LITRES_PER_DAY


async def test_first_refresh_does_not_wait_for_the_recorder(hass: HomeAssistant, fake_client: FakeClient) -> None:
    # While Home Assistant starts, the recorder holds its queue until startup finishes, so waiting for it
    # to commit inside the first refresh would hang setup until the bootstrap timeout.
    start = yesterday() - timedelta(days=4)
    with patch.object(get_instance(hass), "async_block_till_done", side_effect=AssertionError("waited")):
        await setup_entry(hass, billing_entry(start))
    assert number(hass, USAGE) == 5 * LITRES_PER_DAY


async def test_period_rolls_forward_from_an_old_start(hass: HomeAssistant, fake_client: FakeClient) -> None:
    start = yesterday() - timedelta(days=2)
    await setup_entry(hass, billing_entry(start - timedelta(days=30 * 4), read_days=30))
    state = hass.states.get(USAGE)
    assert state is not None
    assert state.attributes["period_start"] == start.isoformat()
    assert float(state.state) == 3 * LITRES_PER_DAY


async def test_flat_price_leaves_step_sensors_unknown(hass: HomeAssistant, fake_client: FakeClient) -> None:
    start = yesterday() - timedelta(days=4)
    await setup_entry(hass, billing_entry(start, **{CONF_TARIFF: TARIFF_CUSTOM, CONF_PRICE_STEP1: 2.0}))
    assert number(hass, COST) == pytest.approx(5 * LITRES_PER_DAY * 2.0 / 1000)
    for entity_id in (REMAINING, ALLOWANCE):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == STATE_UNKNOWN


async def test_period_with_no_readings_yet(hass: HomeAssistant, fake_client: FakeClient) -> None:
    # The portal has published nothing for the period, which started yesterday.
    fake_client.litres_per_hour = lambda day: 0 if day >= yesterday() else 10
    await setup_entry(hass, billing_entry(yesterday()))
    assert number(hass, USAGE) == 0
    state = hass.states.get(USAGE)
    assert state is not None
    assert state.attributes["days_with_data"] == 0
    # The projection carries on at the recent daily rate.
    assert number(hass, PROJECTED_USAGE) == 91 * LITRES_PER_DAY


async def test_options_flow_sets_and_clears_the_billing_period(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    read_date = yesterday() - timedelta(days=10)
    base = {
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        CONF_POLL_TIME: DEFAULT_POLL_TIME,
        CONF_TARIFF: TARIFF_WATER_SEWAGE,
    }
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**base, CONF_BILLING_PERIOD: {CONF_LAST_READ_DATE: read_date.isoformat(), CONF_READ_DAYS: 92}},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options[CONF_LAST_READ_DATE] == read_date.isoformat()
    assert setup_integration.options[CONF_READ_DAYS] == 92
    assert hass.states.get(USAGE) is not None

    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, CONF_BILLING_PERIOD: {CONF_READ_DAYS: 92}}
    )
    await hass.async_block_till_done()
    assert CONF_LAST_READ_DATE not in setup_integration.options
    assert setup_integration.runtime_data.data.billing is None
