"""Tests for the billing-period arithmetic."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.sew_water.billing import (
    PROJECTION_DAYS,
    build_period,
    daily_rate,
    period_bounds,
    usage_charge,
)
from custom_components.sew_water.pricing import Rates
from custom_components.sew_water.sew_client import DailyUsage

STEPPED = Rates(step1=4.1047, step2=5.2373)
FLAT = Rates(step1=2.0)


def day_of(day: date, litres: int, available: bool = True) -> DailyUsage:
    """Build a day without hourly readings."""
    return DailyUsage(day=day, litres=litres, readings=(), serial="S", message="Ok", available=available)


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        (date(2026, 7, 22), date(2026, 7, 22), date(2026, 10, 21)),
        (date(2026, 10, 20), date(2026, 7, 22), date(2026, 10, 21)),
        (date(2026, 10, 21), date(2026, 10, 21), date(2027, 1, 20)),
        (date(2026, 7, 21), date(2026, 4, 22), date(2026, 7, 22)),
    ],
)
def test_period_bounds_roll_forward_and_back(day: date, start: date, end: date) -> None:
    # The bill in discussion #9: 22/07/26 to 21/10/26, 91 days.
    assert period_bounds(date(2026, 7, 22), 91, day) == (start, end)


def test_usage_charge_matches_the_bill_from_discussion_9() -> None:
    # 44 kL over 91 days: 40.04 kL at step 1 ($164.35) and 3.96 kL at step 2 ($20.74).
    assert round(usage_charge(44_000, 91, STEPPED), 2) == 185.09


def test_usage_charge_under_the_allowance_and_flat() -> None:
    assert usage_charge(1000, 91, STEPPED) == pytest.approx(4.1047)
    assert usage_charge(1000, 1, FLAT) == pytest.approx(2.0)


def test_daily_rate_uses_recent_days_with_readings() -> None:
    last = date(2026, 11, 20)
    window = [day_of(last - timedelta(days=n), 100) for n in range(20, PROJECTION_DAYS - 1, -1)]
    window += [day_of(last - timedelta(days=n), 300) for n in range(PROJECTION_DAYS - 1, -1, -1)]
    window.append(day_of(last + timedelta(days=1), 0, available=False))
    assert daily_rate(window, last) == pytest.approx(300)
    assert daily_rate([day_of(last, 0, available=False)], last) is None


def test_build_period_part_way_through() -> None:
    start, end = date(2026, 10, 21), date(2027, 1, 20)
    last = start + timedelta(days=29)
    window = [day_of(start + timedelta(days=n), 500) for n in range(30)]
    period = build_period(start=start, end=end, last_day=last, usage=15_000, rates=STEPPED, window=window)
    assert period.days == 91
    assert period.days_with_data == 30
    assert period.allowance == 40_040
    assert period.step1_remaining == 25_040
    assert period.daily_allowance == pytest.approx(25_040 / 61)
    assert period.projected_usage == pytest.approx(15_000 + 500 * 61)
    assert period.cost == pytest.approx(15 * 4.1047)
    # 45.5 kL projected: 40.04 kL at step 1 and 5.46 kL at step 2.
    assert period.projected_cost == pytest.approx(40.04 * 4.1047 + 5.46 * 5.2373)


def test_build_period_over_the_allowance() -> None:
    start, end = date(2026, 10, 21), date(2027, 1, 20)
    last = end - timedelta(days=1)
    period = build_period(start=start, end=end, last_day=last, usage=50_000, rates=STEPPED, window=[])
    assert period.step1_remaining == 0
    # No days left.
    assert period.daily_allowance is None
    assert period.projected_usage == 50_000


def test_build_period_flat_price_has_no_step_figures() -> None:
    start, end = date(2026, 10, 21), date(2027, 1, 20)
    period = build_period(start=start, end=end, last_day=start, usage=200, rates=FLAT, window=[])
    assert period.allowance is None
    assert period.step1_remaining is None
    assert period.daily_allowance is None
    # No window: the period's own average carries the projection.
    assert period.projected_usage == pytest.approx(200 * 91)


def test_build_period_before_any_reading_in_it() -> None:
    start, end = date(2026, 10, 21), date(2027, 1, 20)
    period = build_period(start=start, end=end, last_day=start - timedelta(days=1), usage=0, rates=STEPPED, window=[])
    assert period.days_with_data == 0
    assert period.projected_usage == 0
    assert period.daily_allowance == pytest.approx(40_040 / 91)
