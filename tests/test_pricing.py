"""Tests for the published price table and the per-day stepped cost calculation."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.sew_water.const import TARIFF_RECYCLED, TARIFF_WATER_ONLY, TARIFF_WATER_SEWAGE
from custom_components.sew_water.pricing import (
    NEWEST_PRICE_YEAR,
    OLDEST_PRICE_YEAR,
    SEW_PRICES,
    Rates,
    financial_year,
    hourly_costs,
    published_rates,
)


@pytest.mark.parametrize(
    ("day", "year"),
    [(date(2026, 6, 30), 2025), (date(2026, 7, 1), 2026), (date(2027, 1, 15), 2026), (date(2026, 12, 31), 2026)],
)
def test_financial_year_starts_on_1_july(day: date, year: int) -> None:
    assert financial_year(day) == year


def test_published_rates_follow_the_financial_year() -> None:
    assert published_rates(date(2026, 6, 30), TARIFF_WATER_SEWAGE) == Rates(step1=3.7685, step2=4.8084)
    assert published_rates(date(2026, 7, 1), TARIFF_WATER_SEWAGE) == Rates(step1=4.1047, step2=5.2373)
    assert published_rates(date(2026, 7, 1), TARIFF_WATER_ONLY) == Rates(step1=3.2766, step2=4.1807)
    assert published_rates(date(2026, 7, 1), TARIFF_RECYCLED) == Rates(step1=2.4389)


def test_published_rates_outside_the_table_use_the_nearest_year() -> None:
    oldest = SEW_PRICES[OLDEST_PRICE_YEAR][TARIFF_WATER_SEWAGE]
    newest = SEW_PRICES[NEWEST_PRICE_YEAR][TARIFF_WATER_SEWAGE]
    assert published_rates(date(OLDEST_PRICE_YEAR - 3, 8, 1), TARIFF_WATER_SEWAGE) == oldest
    assert published_rates(date(NEWEST_PRICE_YEAR + 2, 8, 1), TARIFF_WATER_SEWAGE) == newest


def test_every_year_has_every_tariff() -> None:
    for prices in SEW_PRICES.values():
        assert set(prices) == {TARIFF_RECYCLED, TARIFF_WATER_ONLY, TARIFF_WATER_SEWAGE}


def test_flat_price_charges_every_litre_the_same() -> None:
    assert hourly_costs([500.0, 1000.0], Rates(step1=2.0)) == pytest.approx([1.0, 2.0])


def test_step_2_starts_inside_the_hour_that_crosses_the_threshold() -> None:
    rates = Rates(step1=4.0, step2=5.0, threshold=440)
    costs = hourly_costs([400.0, 100.0, 60.0], rates)
    # 400 L at step 1; then 40 L at step 1 and 60 L at step 2; then all 60 L at step 2.
    assert costs == pytest.approx([1.6, 0.04 * 4 + 0.06 * 5, 0.06 * 5])


def test_day_under_the_threshold_is_all_step_1() -> None:
    assert sum(hourly_costs([10.0] * 24, Rates(step1=4.0, step2=5.0))) == pytest.approx(0.24 * 4)


def test_zero_threshold_charges_everything_at_step_2() -> None:
    assert hourly_costs([100.0], Rates(step1=4.0, step2=5.0, threshold=0)) == pytest.approx([0.5])


def test_bill_from_discussion_9_matches_when_usage_is_even() -> None:
    """A 91-day bill of 44 kL: step 1 40.04 kL (440 L x 91) at $4.1047, step 2 3.96 kL at $5.2373."""
    rates = Rates(step1=4.1047, step2=5.2373)
    total = sum(sum(hourly_costs([44000 / 91], rates)) for _ in range(91))
    assert round(total, 2) == round(40.04 * 4.1047 + 3.96 * 5.2373, 2)
