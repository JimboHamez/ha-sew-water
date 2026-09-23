"""Button platform for the South East Water integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SewConfigEntry
from .entity import SewEntity

# A press polls the portal; one at a time is plenty and keeps presses from stacking up requests.
PARALLEL_UPDATES = 1

UPDATE_NOW = ButtonEntityDescription(key="update_now", translation_key="update_now")


async def async_setup_entry(
    hass: HomeAssistant, entry: SewConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create the update button."""
    async_add_entities([SewUpdateButton(entry.runtime_data, UPDATE_NOW)])


class SewUpdateButton(SewEntity, ButtonEntity):
    """Poll the portal now, like the force-import action."""

    @property
    def available(self) -> bool:
        """Stay pressable after a failed poll, which is when a retry is most useful."""
        return True

    async def async_press(self) -> None:
        """Poll the portal and raise if the poll failed."""
        await self.coordinator.async_poll_now()
