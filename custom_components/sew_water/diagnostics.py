"""Diagnostics support for the South East Water integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_BILLING_ACCOUNT_ID, CONF_COOKIES, CONF_METER_ID, CONF_METER_SERIAL
from .coordinator import SewConfigEntry

TO_REDACT = {CONF_BILLING_ACCOUNT_ID, CONF_COOKIES, CONF_METER_ID, CONF_METER_SERIAL, CONF_PASSWORD, CONF_USERNAME}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SewConfigEntry) -> dict[str, Any]:
    """Return diagnostics with credentials, session cookies and account identifiers removed."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_poll": data.last_poll.isoformat() if data else None,
            "latest": asdict(data.latest) if data and data.latest else None,
            "total_litres": data.total_litres if data else None,
            "update_interval": str(coordinator.update_interval),
            "window_days": len(data.window) if data else 0,
        },
    }
