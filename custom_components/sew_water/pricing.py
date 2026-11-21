"""South East Water usage prices and the hourly cost calculation.

Prices are South East Water's published residential usage charges, in dollars per kilolitre, for each
financial year (1 July to 30 June). Fixed quarterly service charges and concessions are not included.

Step 2 applies to usage above 440 litres a day. South East Water works the threshold out over the whole
billing period (440 litres times the days in the period), but the integration does not know the billing
period, so it applies the threshold to each day instead. Summed over a period that can only read the same
as the bill or higher, never lower, and it reads the same whenever every day stays on the same side of the
threshold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from .const import TARIFF_RECYCLED, TARIFF_WATER_ONLY, TARIFF_WATER_SEWAGE

# Litres a day charged at the step 1 price.
STEP_THRESHOLD_LITRES: Final = 440.0

LITRES_PER_KILOLITRE: Final = 1000.0


@dataclass(frozen=True)
class Rates:
    """Usage prices for one tariff.

    Attributes:
        step1: Price per kilolitre up to the daily threshold, or for all usage when there is no step 2.
        step2: Price per kilolitre above the daily threshold, or None for a single flat price.
        threshold: Litres a day charged at ``step1``; ignored without ``step2``.
    """

    step1: float
    step2: float | None = None
    threshold: float = STEP_THRESHOLD_LITRES


# Published residential prices, keyed by the year each financial year starts in (2026 is 2026-27).
SEW_PRICES: Final[Mapping[int, Mapping[str, Rates]]] = {
    2025: {
        TARIFF_RECYCLED: Rates(step1=2.3430),
        TARIFF_WATER_ONLY: Rates(step1=3.0084, step2=3.8383),
        TARIFF_WATER_SEWAGE: Rates(step1=3.7685, step2=4.8084),
    },
    2026: {
        TARIFF_RECYCLED: Rates(step1=2.4389),
        TARIFF_WATER_ONLY: Rates(step1=3.2766, step2=4.1807),
        TARIFF_WATER_SEWAGE: Rates(step1=4.1047, step2=5.2373),
    },
}
OLDEST_PRICE_YEAR: Final = min(SEW_PRICES)
NEWEST_PRICE_YEAR: Final = max(SEW_PRICES)


def financial_year(day: date) -> int:
    """Return the year the financial year containing ``day`` starts in.

    Args:
        day: A local calendar day.

    Returns:
        ``day.year`` from July onwards, otherwise the year before.
    """
    return day.year if day.month >= 7 else day.year - 1


def published_rates(day: date, tariff: str) -> Rates:
    """Return the published prices that applied on ``day``.

    A day outside the table takes the nearest year's prices: the oldest year for earlier history, and
    the newest year until the table is updated for a new financial year.

    Args:
        day: A local calendar day.
        tariff: ``TARIFF_WATER_SEWAGE``, ``TARIFF_WATER_ONLY`` or ``TARIFF_RECYCLED``.

    Returns:
        The tariff's prices for the day's financial year.
    """
    year = min(max(financial_year(day), OLDEST_PRICE_YEAR), NEWEST_PRICE_YEAR)
    return SEW_PRICES[year][tariff]


def hourly_costs(litres: Sequence[float], rates: Rates) -> list[float]:
    """Return the cost of each hour of one day, in dollars.

    The day's first ``rates.threshold`` litres are charged at step 1 and the rest at step 2, in the
    order the hours were used.

    Args:
        litres: The day's usage per hour, in order; a single value for a day without hourly readings.
        rates: The prices for the day.

    Returns:
        One cost per value in ``litres``.
    """
    costs: list[float] = []
    used = 0.0
    for hour in litres:
        if rates.step2 is None:
            step1_litres = hour
        else:
            step1_litres = min(hour, max(rates.threshold - used, 0.0))
        step2_litres = hour - step1_litres
        used += hour
        costs.append((step1_litres * rates.step1 + step2_litres * (rates.step2 or 0.0)) / LITRES_PER_KILOLITRE)
    return costs
