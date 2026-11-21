"""Billing-period figures: usage and cost for the bill so far, the step 1 headroom and a projection.

South East Water charges step 1 on the first 440 litres a day *averaged over the billing period*: a
91-day bill has 40,040 litres at step 1, however they were spread. So unlike the cost statistics, which
apply the threshold per day, these figures use the bill's own rule. The integration does not know the
billing dates; the user gives the first day of one bill and the period length, and periods are assumed
to follow each other at that length.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from .pricing import LITRES_PER_KILOLITRE, Rates
from .sew_client import DailyUsage

# Days of recent usage averaged for the projection.
PROJECTION_DAYS: Final = 14


@dataclass(frozen=True)
class BillingPeriod:
    """The current billing period, as far as the portal's readings go.

    Attributes:
        start: First day of the period.
        end: First day of the next period (the meter-read date printed as the period's end on the bill).
        days: Days in the period, as the bill counts them (``end - start``).
        days_with_data: Days from ``start`` up to the newest reading, at most ``days``.
        usage: Litres used in the period so far.
        cost: Usage charge for ``usage`` with the bill's step rule, in dollars.
        allowance: Litres in the period charged at step 1, or None with a single flat price.
        step1_remaining: Litres of ``allowance`` not yet used, or None with a single flat price.
        daily_allowance: Litres a day that can be used for the rest of the period while staying within the
            step 1 allowance; None with a single flat price or when no days are left.
        projected_usage: Litres expected by the end of the period at the recent daily rate.
        projected_cost: Usage charge for ``projected_usage``, in dollars.
    """

    start: date
    end: date
    days: int
    days_with_data: int
    usage: float
    cost: float
    allowance: float | None
    step1_remaining: float | None
    daily_allowance: float | None
    projected_usage: float
    projected_cost: float


def period_bounds(anchor: date, length: int, day: date) -> tuple[date, date]:
    """Return the billing period containing ``day``.

    Args:
        anchor: The first day of any one billing period.
        length: Days in each period.
        day: The day to locate; it may be before or after ``anchor``.

    Returns:
        The period's first day and the first day of the next period.
    """
    offset = (day - anchor).days // length
    start = anchor + timedelta(days=offset * length)
    return start, start + timedelta(days=length)


def usage_charge(litres: float, days: int, rates: Rates) -> float:
    """Return the usage charge for a billing period, in dollars.

    Args:
        litres: Usage over the period.
        days: Days in the period; the step 1 allowance is ``rates.threshold`` litres for each.
        rates: The prices.

    Returns:
        The step 1 and step 2 charges together.
    """
    if rates.step2 is None:
        return litres * rates.step1 / LITRES_PER_KILOLITRE
    step1_litres = min(litres, rates.threshold * days)
    step2_litres = litres - step1_litres
    return (step1_litres * rates.step1 + step2_litres * rates.step2) / LITRES_PER_KILOLITRE


def daily_rate(window: Sequence[DailyUsage], last_day: date) -> float | None:
    """Return the average daily usage over the last ``PROJECTION_DAYS`` days with readings.

    Args:
        window: Recently fetched days, oldest first.
        last_day: The newest day to consider.

    Returns:
        Litres a day, or None when there are no readings.
    """
    days = [day.litres for day in window if day.available and day.day <= last_day][-PROJECTION_DAYS:]
    if not days:
        return None
    return sum(days) / len(days)


def build_period(
    *,
    start: date,
    end: date,
    last_day: date,
    usage: float,
    rates: Rates,
    window: Sequence[DailyUsage],
) -> BillingPeriod:
    """Work out the billing-period figures.

    Args:
        start: First day of the period.
        end: First day of the next period.
        last_day: The newest day with readings.
        usage: Litres used from ``start`` to ``last_day``.
        rates: The prices for the period.
        window: Recently fetched days, for the daily rate.

    Returns:
        The period's figures.
    """
    days = (end - start).days
    days_with_data = min(max((last_day - start).days + 1, 0), days)
    remaining_days = days - days_with_data
    rate = daily_rate(window, last_day)
    if rate is None:
        rate = usage / days_with_data if days_with_data else 0.0
    projected_usage = usage + rate * remaining_days

    allowance = step1_remaining = daily_allowance = None
    if rates.step2 is not None:
        allowance = rates.threshold * days
        step1_remaining = max(allowance - usage, 0.0)
        daily_allowance = step1_remaining / remaining_days if remaining_days else None

    return BillingPeriod(
        start=start,
        end=end,
        days=days,
        days_with_data=days_with_data,
        usage=usage,
        cost=usage_charge(usage, days, rates),
        allowance=allowance,
        step1_remaining=step1_remaining,
        daily_allowance=daily_allowance,
        projected_usage=projected_usage,
        projected_cost=usage_charge(projected_usage, days, rates),
    )
