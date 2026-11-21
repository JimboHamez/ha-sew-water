"""Tests for the water cost statistics and the price options."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import partial
from unittest.mock import patch

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sew_water import coordinator as coordinator_module
from custom_components.sew_water.const import (
    CONF_BILLING_DAYS,
    CONF_BILLING_PERIOD,
    CONF_POLL_TIME,
    CONF_PRICE_RECYCLED,
    CONF_PRICE_STEP1,
    CONF_PRICE_STEP2,
    CONF_SCAN_INTERVAL,
    CONF_STEP_THRESHOLD,
    CONF_TARIFF,
    DEFAULT_BILLING_DAYS,
    DEFAULT_POLL_TIME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_PRICES_OUT_OF_DATE,
    STATISTIC_ID_COST_MAINS,
    STATISTIC_ID_COST_RECYCLED,
    TARIFF_CUSTOM,
    TARIFF_RECYCLED,
    TARIFF_WATER_ONLY,
    TARIFF_WATER_SEWAGE,
)
from custom_components.sew_water.pricing import NEWEST_PRICE_YEAR, published_rates
from custom_components.sew_water.sew_client import AccountIds

from .conftest import BACKFILL_DAYS, IDS, RECYCLED_IDS, RECYCLED_SERIAL, TRAILING_WINDOW_DAYS, FakeClient, entry_data
from .test_coordinator import read_rows
from .test_meters import make_entry, seed_mains_statistic, setup_entry, yesterday


def costs_by_day(rows: list[tuple[datetime, float, float]]) -> dict[date, float]:
    """Add up hourly cost rows per local day."""
    days: dict[date, float] = {}
    for start, state, _sum in rows:
        day = dt_util.as_local(start).date()
        days[day] = days.get(day, 0.0) + state
    return days


def entry_with_options(meters: list[AccountIds], **options: object) -> MockConfigEntry:
    """Build a current-version entry for ``meters`` with the given options."""
    base = make_entry(entry_data(meters))
    return MockConfigEntry(
        domain=base.domain,
        title=base.title,
        unique_id=base.unique_id,
        version=base.version,
        minor_version=base.minor_version,
        data=base.data,
        options=options,
    )


async def test_first_run_writes_a_cost_row_for_every_usage_row(hass: HomeAssistant, fake_client: FakeClient) -> None:
    # 30 L an hour is 720 L a day: 440 L at step 1 and 280 L at step 2.
    fake_client.litres_per_hour = lambda _day: 30
    await setup_entry(hass, make_entry(entry_data([IDS])))
    usage = await read_rows(hass)
    cost = await read_rows(hass, STATISTIC_ID_COST_MAINS)
    assert [row[0] for row in cost] == [row[0] for row in usage]
    assert len(cost) == BACKFILL_DAYS * 24

    for day, total in costs_by_day(cost).items():
        rates = published_rates(day, TARIFF_WATER_SEWAGE)
        assert rates.step2 is not None
        assert total == pytest.approx(0.44 * rates.step1 + 0.28 * rates.step2)
    assert cost[-1][2] == pytest.approx(sum(state for _start, state, _sum in cost))

    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={STATISTIC_ID_COST_MAINS})
    )
    meta = metadata[STATISTIC_ID_COST_MAINS][1]
    assert meta["has_sum"] is True
    assert meta["unit_of_measurement"] is None
    assert meta["name"] == "South East Water mains cost"


async def test_water_only_tariff_uses_its_prices(hass: HomeAssistant, fake_client: FakeClient) -> None:
    await setup_entry(hass, entry_with_options([IDS], **{CONF_TARIFF: TARIFF_WATER_ONLY}))
    for day, total in costs_by_day(await read_rows(hass, STATISTIC_ID_COST_MAINS)).items():
        # 10 L an hour is 240 L a day, all at step 1.
        assert total == pytest.approx(0.24 * published_rates(day, TARIFF_WATER_ONLY).step1)


def custom_entry(**options: object) -> MockConfigEntry:
    """Build an entry for a property with both meters and custom prices."""
    return entry_with_options([IDS, RECYCLED_IDS], **{CONF_TARIFF: TARIFF_CUSTOM, **options})


async def test_custom_flat_price_and_recycled_price(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.meters = [IDS, RECYCLED_IDS]
    fake_client.litres_per_hour = lambda _day: 50
    await setup_entry(
        hass, custom_entry(**{CONF_PRICE_STEP1: 2.0, CONF_STEP_THRESHOLD: 440.0, CONF_PRICE_RECYCLED: 1.0})
    )
    # No step 2 price: all 1,200 L a day at $2/kL.
    for total in costs_by_day(await read_rows(hass, STATISTIC_ID_COST_MAINS)).values():
        assert total == pytest.approx(2.4)
    for total in costs_by_day(await read_rows(hass, STATISTIC_ID_COST_RECYCLED)).values():
        assert total == pytest.approx(1.2)


async def test_custom_steps_and_published_recycled_fallback(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.meters = [IDS, RECYCLED_IDS]
    fake_client.litres_per_hour_by_serial = {RECYCLED_SERIAL: lambda _day: 10}
    fake_client.litres_per_hour = lambda _day: 25
    await setup_entry(hass, custom_entry(**{CONF_PRICE_STEP1: 1.0, CONF_PRICE_STEP2: 3.0, CONF_STEP_THRESHOLD: 500.0}))
    # 600 L a day: 500 L at $1/kL and 100 L at $3/kL.
    for total in costs_by_day(await read_rows(hass, STATISTIC_ID_COST_MAINS)).values():
        assert total == pytest.approx(0.8)
    # No custom recycled price: the published one.
    for day, total in costs_by_day(await read_rows(hass, STATISTIC_ID_COST_RECYCLED)).items():
        assert total == pytest.approx(0.24 * published_rates(day, TARIFF_RECYCLED).step1)


async def test_install_without_costs_imports_the_backfill_window(hass: HomeAssistant, fake_client: FakeClient) -> None:
    # Usage history from an earlier version, but no cost statistic yet.
    await seed_mains_statistic(hass, yesterday() - timedelta(days=BACKFILL_DAYS + 20), 7)
    await setup_entry(hass, make_entry(entry_data([IDS])))
    first, last = fake_client.fetch_ranges[0]
    assert (last - first).days + 1 == BACKFILL_DAYS
    assert len(await read_rows(hass, STATISTIC_ID_COST_MAINS)) == BACKFILL_DAYS * 24

    # With both statistics present, the next poll takes the trailing window again.
    await hass.config_entries.async_reload(next(iter(hass.config_entries.async_entries(DOMAIN))).entry_id)
    await hass.async_block_till_done()
    first, last = fake_client.fetch_ranges[-1]
    assert (last - first).days + 1 == TRAILING_WINDOW_DAYS


async def test_empty_import_writes_no_cost(hass: HomeAssistant, fake_client: FakeClient) -> None:
    entry = await setup_entry(hass, make_entry(entry_data([IDS])))
    before = await read_rows(hass, STATISTIC_ID_COST_MAINS)
    await entry.runtime_data._async_import_cost(IDS, [])
    assert await read_rows(hass, STATISTIC_ID_COST_MAINS) == before


async def test_cost_sum_continues_across_polls(hass: HomeAssistant, fake_client: FakeClient) -> None:
    entry = await setup_entry(hass, make_entry(entry_data([IDS])))
    before = await read_rows(hass, STATISTIC_ID_COST_MAINS)
    await entry.runtime_data.async_poll_now()
    after = await read_rows(hass, STATISTIC_ID_COST_MAINS)
    assert len(after) == len(before)
    assert after[-1][2] == pytest.approx(sum(state for _start, state, _sum in after))


async def test_prices_out_of_date_raises_and_clears_a_repair(hass: HomeAssistant, fake_client: FakeClient) -> None:
    issues = ir.async_get(hass)
    with patch.object(coordinator_module, "NEWEST_PRICE_YEAR", NEWEST_PRICE_YEAR - 5):
        entry = await setup_entry(hass, make_entry(entry_data([IDS])))
    issue = issues.async_get_issue(DOMAIN, f"{ISSUE_PRICES_OUT_OF_DATE}_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["known"] == f"{NEWEST_PRICE_YEAR - 5}-{(NEWEST_PRICE_YEAR - 4) % 100:02d}"

    await entry.runtime_data.async_poll_now()
    assert issues.async_get_issue(DOMAIN, f"{ISSUE_PRICES_OUT_OF_DATE}_{entry.entry_id}") is None


async def test_custom_prices_never_raise_the_repair(hass: HomeAssistant, fake_client: FakeClient) -> None:
    with patch.object(coordinator_module, "NEWEST_PRICE_YEAR", NEWEST_PRICE_YEAR - 5):
        entry = await setup_entry(hass, custom_entry(**{CONF_PRICE_STEP1: 2.0, CONF_PRICE_RECYCLED: 1.0}))
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_PRICES_OUT_OF_DATE}_{entry.entry_id}") is None


async def test_options_flow_custom_prices(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_POLL_TIME: DEFAULT_POLL_TIME,
            CONF_TARIFF: TARIFF_CUSTOM,
            CONF_BILLING_PERIOD: {CONF_BILLING_DAYS: DEFAULT_BILLING_DAYS},
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "custom_prices"
    # The form starts from today's published water and sewage prices.
    published = published_rates(dt_util.now().date(), TARIFF_WATER_SEWAGE)
    suggested = {str(key): key.description["suggested_value"] for key in result["data_schema"].schema}
    assert suggested[CONF_PRICE_STEP1] == published.step1
    assert suggested[CONF_PRICE_STEP2] == published.step2
    assert suggested[CONF_STEP_THRESHOLD] == 440.0

    # A blank step 2 price is left out, which means one flat price.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRICE_STEP1: 3.5, CONF_STEP_THRESHOLD: 440}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options == {
        CONF_BILLING_DAYS: DEFAULT_BILLING_DAYS,
        CONF_POLL_TIME: DEFAULT_POLL_TIME,
        CONF_PRICE_STEP1: 3.5,
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        CONF_STEP_THRESHOLD: 440.0,
        CONF_TARIFF: TARIFF_CUSTOM,
    }

    # Opening the options again keeps the blank step 2 price blank.
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_POLL_TIME: DEFAULT_POLL_TIME,
            CONF_TARIFF: TARIFF_CUSTOM,
            CONF_BILLING_PERIOD: {CONF_BILLING_DAYS: DEFAULT_BILLING_DAYS},
        },
    )
    suggested = {str(key): key.description.get("suggested_value") for key in result["data_schema"].schema}
    assert suggested[CONF_PRICE_STEP1] == 3.5
    assert suggested[CONF_PRICE_STEP2] is None

    # Setting recycled and step 2 prices stores them.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PRICE_STEP1: 3.5, CONF_PRICE_STEP2: 4.5, CONF_STEP_THRESHOLD: 440, CONF_PRICE_RECYCLED: 2.0},
    )
    await hass.async_block_till_done()
    assert setup_integration.options[CONF_PRICE_STEP2] == 4.5
    assert setup_integration.options[CONF_PRICE_RECYCLED] == 2.0
