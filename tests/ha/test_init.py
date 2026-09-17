"""Tests for entry setup, unload, migration and the services."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sew_water.const import (
    CONF_COOKIES,
    DOMAIN,
    SERVICE_ATTR_START_DATE,
    SERVICE_FORCE_IMPORT,
    SERVICE_IMPORT_FROM_DATE,
)
from custom_components.sew_water.sew_client import SewAuthError, SewConnectionError

from .conftest import COOKIES, REFRESHED_COOKIES, FakeClient


async def test_setup_restores_session_and_loads(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    assert setup_integration.state is ConfigEntryState.LOADED
    assert fake_client.imported_cookies == COOKIES
    assert ("is_alive", None) in fake_client.calls
    assert hass.services.has_service(DOMAIN, SERVICE_FORCE_IMPORT)
    assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_FROM_DATE)


@pytest.mark.usefixtures("fake_client")
async def test_unload_releases_session(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """The entry's session is created with auto-cleanup, so Home Assistant detaches it on unload."""
    sessions: list[aiohttp.ClientSession] = []

    def _record(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
        sessions.append(session := async_create_clientsession(*args, **kwargs))
        return session

    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.sew_water.async_create_clientsession", side_effect=_record):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(sessions) == 1
    assert not sessions[0].closed

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert sessions[0].closed


async def test_setup_retries_when_portal_unreachable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.alive_error = SewConnectionError("down")
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_starts_reauth_when_session_dead(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.alive = False
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"


async def test_v1_entry_is_refused_with_repair_issue(
    hass: HomeAssistant, fake_client: FakeClient, issue_registry: ir.IssueRegistry
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, title="old@example.com", version=1, data={"browserless_url": "http://x:3000"}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    issue = issue_registry.async_get_issue(DOMAIN, f"v1_entry_{entry.entry_id}")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable
    assert issue.translation_placeholders == {"title": "old@example.com"}


async def test_force_import_polls_now(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    fetches_before = len(fake_client.fetch_ranges)
    await hass.services.async_call(DOMAIN, SERVICE_FORCE_IMPORT, {}, blocking=True)
    assert len(fake_client.fetch_ranges) == fetches_before + 1


async def test_import_from_date_fetches_requested_range(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    start = dt_util.now().date() - timedelta(days=10)
    await hass.services.async_call(
        DOMAIN, SERVICE_IMPORT_FROM_DATE, {SERVICE_ATTR_START_DATE: start.isoformat()}, blocking=True
    )
    yesterday = dt_util.now().date() - timedelta(days=1)
    assert fake_client.fetch_ranges[-1] == (start, yesterday)


async def test_import_from_date_rejects_future_start(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    tomorrow = dt_util.now().date() + timedelta(days=1)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_IMPORT_FROM_DATE, {SERVICE_ATTR_START_DATE: tomorrow.isoformat()}, blocking=True
        )


async def test_import_from_date_reports_portal_failure(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.fetch_error = SewConnectionError("down")
    with pytest.raises(HomeAssistantError, match="Import failed") as excinfo:
        await hass.services.async_call(
            DOMAIN, SERVICE_IMPORT_FROM_DATE, {SERVICE_ATTR_START_DATE: date(2026, 1, 1).isoformat()}, blocking=True
        )
    assert excinfo.value.translation_key == "import_failed"


async def test_services_without_entries_raise(hass: HomeAssistant, fake_client: FakeClient) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, SERVICE_FORCE_IMPORT, {}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_IMPORT_FROM_DATE, {SERVICE_ATTR_START_DATE: "2026-01-01"}, blocking=True
        )


async def test_refreshed_cookies_are_written_back(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.exported_cookies = REFRESHED_COOKIES
    await hass.services.async_call(DOMAIN, SERVICE_FORCE_IMPORT, {}, blocking=True)
    assert setup_integration.data[CONF_COOKIES] == REFRESHED_COOKIES


async def test_poll_auth_failure_triggers_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.fetch_error = SewAuthError("expired")
    await hass.services.async_call(DOMAIN, SERVICE_FORCE_IMPORT, {}, blocking=True)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"
