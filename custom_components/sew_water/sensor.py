"""Sensor platform for the South East Water integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import SewConfigEntry, SewCoordinator, SewData

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


async def async_setup_entry(
    hass: HomeAssistant, entry: SewConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create one entity per description."""
    coordinator = entry.runtime_data
    async_add_entities(SewSensor(coordinator, description) for description in SENSORS)


class SewSensor(CoordinatorEntity[SewCoordinator], SensorEntity):
    """A value derived from the last portal poll."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    entity_description: SewSensorDescription

    def __init__(self, coordinator: SewCoordinator, description: SewSensorDescription) -> None:
        """Bind the entity to its description and the account's device."""
        super().__init__(coordinator)
        self.entity_description = description
        # Keyed by the config entry, not the billing account, so account identifiers never reach the
        # entity or device registries.
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            manufacturer=MANUFACTURER,
            model="Digital water meter",
            name=MANUFACTURER,
        )

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
