"""Base entity for the South East Water integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import SewCoordinator


class SewEntity(CoordinatorEntity[SewCoordinator]):
    """An entity on the account's meter device, named from translations."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: SewCoordinator, description: EntityDescription) -> None:
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
            translation_key="meter",
        )
