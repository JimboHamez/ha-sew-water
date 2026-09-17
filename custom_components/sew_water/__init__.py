"""South East Water integration: daily water usage from the customer portal."""

from __future__ import annotations

from datetime import date
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import (
    CONF_COOKIES,
    DOMAIN,
    SERVICE_ATTR_START_DATE,
    SERVICE_FORCE_IMPORT,
    SERVICE_IMPORT_FROM_DATE,
)
from .coordinator import SewConfigEntry, SewCoordinator
from .sew_client import SewClient

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.SENSOR]

IMPORT_FROM_DATE_SCHEMA = vol.Schema({vol.Required(SERVICE_ATTR_START_DATE): cv.date})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide services."""

    def _coordinators() -> list[SewCoordinator]:
        return [entry.runtime_data for entry in hass.config_entries.async_loaded_entries(DOMAIN)]

    async def _force_import(call: ServiceCall) -> None:
        coordinators = _coordinators()
        if not coordinators:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entries")
        for coordinator in coordinators:
            await coordinator.async_refresh()

    async def _import_from_date(call: ServiceCall) -> None:
        coordinators = _coordinators()
        if not coordinators:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entries")
        start: date = call.data[SERVICE_ATTR_START_DATE]
        for coordinator in coordinators:
            try:
                await coordinator.async_import_from(start)
            except ValueError as err:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="start_in_future") from err
            except HomeAssistantError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="import_failed",
                    translation_placeholders={"error": str(err)},
                ) from err

    hass.services.async_register(DOMAIN, SERVICE_FORCE_IMPORT, _force_import)
    hass.services.async_register(DOMAIN, SERVICE_IMPORT_FROM_DATE, _import_from_date, schema=IMPORT_FROM_DATE_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SewConfigEntry) -> bool:
    """Restore the stored portal session and start polling."""
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    client = SewClient(session)
    client.import_cookies(entry.data.get(CONF_COOKIES, []))

    coordinator = SewCoordinator(hass, entry, client)
    # Identifiers are account data, so they are not exposed as entities; they can be checked here
    # with debug logging enabled.
    _LOGGER.debug(
        "Using billing account %s, meter %s (serial %s)",
        coordinator.ids.billing_account_id,
        coordinator.ids.meter_id,
        coordinator.ids.meter_serial,
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.async_start_keepalive()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_backfill()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SewConfigEntry) -> bool:
    """Stop polling; Home Assistant detaches the entry's HTTP session itself on unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Refuse to migrate version 1 entries; they hold no session and must be set up again."""
    if entry.version < 2:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"v1_entry_{entry.entry_id}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="v1_entry",
            translation_placeholders={"title": entry.title},
        )
        _LOGGER.error("Config entries from version 1 cannot be migrated; remove the integration and add it again")
        return False
    return True
