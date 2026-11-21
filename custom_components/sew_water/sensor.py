"""Sensor platform for the South East Water integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .billing import BillingPeriod
from .coordinator import SewConfigEntry, SewData
from .entity import SewEntity
from .sew_client import DailyUsage

# Entities read coordinator data only; no per-entity I/O to serialise.
PARALLEL_UPDATES = 0

type StateValue = date | float | int | str | None


@dataclass(frozen=True, kw_only=True)
class SewSensorDescription(SensorEntityDescription):
    """Describe a sensor and how to read its value from the coordinator data."""

    value_fn: Callable[[SewData], StateValue]
    attributes_fn: Callable[[SewData], dict[str, Any]] | None = None
    last_reset_fn: Callable[[SewData], datetime | None] | None = None


SENSORS: tuple[SewSensorDescription, ...] = (
    SewSensorDescription(
        key="daily_usage",
        translation_key="daily_usage",
        device_class=SensorDeviceClass.VOLUME,
        # A day's usage is a total that starts again each day, so ``last_reset`` marks the start of the
        # reading day; the volume device classes do not allow MEASUREMENT.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        value_fn=lambda data: data.latest.litres if data.latest else None,
        last_reset_fn=lambda data: dt_util.start_of_local_day(data.latest.day) if data.latest else None,
        attributes_fn=lambda data: {
            "reading_date": data.latest.day.isoformat() if data.latest else None,
            "hourly_readings": list(data.latest.readings) if data.latest else None,
        },
    ),
    SewSensorDescription(
        key="total_usage",
        translation_key="total_usage",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        value_fn=lambda data: data.total_litres,
    ),
    SewSensorDescription(
        key="last_reading_date",
        translation_key="last_reading_date",
        device_class=SensorDeviceClass.DATE,
        # The same date is an attribute of the daily sensor; this is for people who want it on a card.
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.latest.day if data.latest else None,
    ),
)


def _recycled_latest(data: SewData) -> DailyUsage | None:
    """Return the recycled meter's most recent day with a reading, if any."""
    return data.recycled.latest if data.recycled else None


# Created only when the account has a recycled-water meter. Same shapes as the mains sensors above.
RECYCLED_SENSORS: tuple[SewSensorDescription, ...] = (
    SewSensorDescription(
        key="recycled_daily_usage",
        translation_key="recycled_daily_usage",
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        value_fn=lambda data: latest.litres if (latest := _recycled_latest(data)) else None,
        last_reset_fn=lambda data: (
            dt_util.start_of_local_day(latest.day) if (latest := _recycled_latest(data)) else None
        ),
        attributes_fn=lambda data: {
            "reading_date": latest.day.isoformat() if (latest := _recycled_latest(data)) else None,
            "hourly_readings": list(latest.readings) if (latest := _recycled_latest(data)) else None,
        },
    ),
    SewSensorDescription(
        key="recycled_total_usage",
        translation_key="recycled_total_usage",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        value_fn=lambda data: data.recycled.total_litres if data.recycled else None,
    ),
    SewSensorDescription(
        key="recycled_last_reading_date",
        translation_key="recycled_last_reading_date",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: latest.day if (latest := _recycled_latest(data)) else None,
    ),
)


def _billing_value(read: Callable[[BillingPeriod], float | None]) -> Callable[[SewData], StateValue]:
    """Return a value function reading one figure of the billing period, rounded for display."""

    def value(data: SewData) -> StateValue:
        if data.billing is None or (figure := read(data.billing)) is None:
            return None
        return round(figure, 2)

    return value


def _billing_attributes(data: SewData) -> dict[str, Any]:
    """Return the billing period's dates and step 1 allowance."""
    if (period := data.billing) is None:
        return {}
    return {
        "period_start": period.start.isoformat(),
        "period_end": period.end.isoformat(),
        "days_in_period": period.days,
        "days_with_data": period.days_with_data,
        "step1_allowance": period.allowance,
    }


def _billing_reset(data: SewData) -> datetime | None:
    """Return the start of the billing period, when the period totals last started from zero."""
    return dt_util.start_of_local_day(data.billing.start) if data.billing else None


# Created only when a billing period is set in the options. Monetary sensors get the configured currency
# as their unit at setup.
BILLING_SENSORS: tuple[SewSensorDescription, ...] = (
    SewSensorDescription(
        key="billing_period_usage",
        translation_key="billing_period_usage",
        device_class=SensorDeviceClass.WATER,
        # The period total starts again each bill, marked by ``last_reset``.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        value_fn=_billing_value(lambda period: period.usage),
        last_reset_fn=_billing_reset,
        attributes_fn=_billing_attributes,
    ),
    SewSensorDescription(
        key="billing_period_cost",
        translation_key="billing_period_cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=_billing_value(lambda period: period.cost),
        last_reset_fn=_billing_reset,
    ),
    SewSensorDescription(
        key="step1_remaining",
        translation_key="step1_remaining",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_billing_value(lambda period: period.step1_remaining),
    ),
    SewSensorDescription(
        key="step1_daily_allowance",
        translation_key="step1_daily_allowance",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_billing_value(lambda period: period.daily_allowance),
    ),
    SewSensorDescription(
        key="projected_usage",
        translation_key="projected_usage",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_billing_value(lambda period: period.projected_usage),
    ),
    SewSensorDescription(
        key="projected_cost",
        translation_key="projected_cost",
        # A forecast, not a running total, so no state class: it stays out of long-term statistics.
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_billing_value(lambda period: period.projected_cost),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SewConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create one entity per description, plus the recycled-water and billing-period sensors when relevant."""
    coordinator = entry.runtime_data
    descriptions = SENSORS + (RECYCLED_SENSORS if coordinator.recycled_meter is not None else ())
    if coordinator.billing_anchor is not None:
        descriptions += tuple(
            replace(description, native_unit_of_measurement=hass.config.currency)
            if description.device_class is SensorDeviceClass.MONETARY
            else description
            for description in BILLING_SENSORS
        )
    async_add_entities(SewSensor(coordinator, description) for description in descriptions)


class SewSensor(SewEntity, SensorEntity):
    """A value derived from the last portal poll."""

    entity_description: SewSensorDescription

    @property
    def native_value(self) -> StateValue:
        """Return the sensor value from the coordinator data."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def last_reset(self) -> datetime | None:
        """Return when the value last started from zero, for descriptions that define it."""
        if self.entity_description.last_reset_fn is None:
            return None
        return self.entity_description.last_reset_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes when the description defines them."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)
