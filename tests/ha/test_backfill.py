"""Tests for the background import back to the installation date."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sew_water import coordinator as coordinator_module
from custom_components.sew_water.const import (
    CONF_BILLING_ACCOUNT_ID,
    CONF_COOKIES,
    CONF_IMPORT_FROM,
    CONF_METER_ID,
    CONF_METER_SERIAL,
    DOMAIN,
    ISSUE_BACKFILL_FAILED,
)
from custom_components.sew_water.sew_client import SewConnectionError

from .conftest import BACKFILL_DAYS, BILLING_ACCOUNT_ID, COOKIES, METER_ID, METER_SERIAL, PASSWORD, USERNAME, FakeClient

TOTAL = "sensor.south_east_water_total_usage"


def entry_with_history(start: date) -> MockConfigEntry:
    """Build an entry as the setup wizard would with an installation date."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME,
        version=2,
        data={
            CONF_BILLING_ACCOUNT_ID: BILLING_ACCOUNT_ID,
            CONF_COOKIES: COOKIES,
            CONF_IMPORT_FROM: start.isoformat(),
            CONF_METER_ID: METER_ID,
            CONF_METER_SERIAL: METER_SERIAL,
            "password": PASSWORD,
            "username": USERNAME,
        },
    )


async def setup_with_history(hass: HomeAssistant, start: date) -> MockConfigEntry:
    """Set up an entry with an installation date and wait for the background import to finish."""
    entry = entry_with_history(start)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    task = entry.runtime_data.backfill_task
    assert task is not None
    await task
    await hass.async_block_till_done()
    return entry


@pytest.fixture(autouse=True)
def _fast_retries() -> None:
    """Retry twice with no delay instead of three times half an hour apart."""
    with (
        patch.object(coordinator_module, "BACKFILL_ATTEMPTS", 2),
        patch.object(coordinator_module, "BACKFILL_RETRY_MINUTES", 0),
    ):
        yield


async def test_history_older_than_first_window_is_imported(hass: HomeAssistant, fake_client: FakeClient) -> None:
    yesterday = dt_util.now().date() - timedelta(days=1)
    start = yesterday - timedelta(days=BACKFILL_DAYS + 9)
    entry = await setup_with_history(hass, start)
    assert entry.state is ConfigEntryState.LOADED
    # The first refresh imports the short window; the background task then fetches everything.
    assert fake_client.fetch_ranges == [(yesterday - timedelta(days=BACKFILL_DAYS - 1), yesterday), (start, yesterday)]
    state = hass.states.get(TOTAL)
    assert state is not None
    assert float(state.state) == (BACKFILL_DAYS + 10) * 240


async def test_history_inside_first_window_is_not_fetched_again(hass: HomeAssistant, fake_client: FakeClient) -> None:
    start = dt_util.now().date() - timedelta(days=BACKFILL_DAYS // 2)
    await setup_with_history(hass, start)
    assert len(fake_client.fetch_ranges) == 1


async def test_history_already_imported_is_skipped_on_reload(hass: HomeAssistant, fake_client: FakeClient) -> None:
    start = dt_util.now().date() - timedelta(days=BACKFILL_DAYS + 5)
    entry = await setup_with_history(hass, start)
    fetches = len(fake_client.fetch_ranges)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    task = entry.runtime_data.backfill_task
    assert task is not None
    await task
    # Only the reload's own first refresh hit the portal.
    assert len(fake_client.fetch_ranges) == fetches + 1


async def test_failed_import_retries_then_raises_issue(
    hass: HomeAssistant, fake_client: FakeClient, issue_registry: ir.IssueRegistry
) -> None:
    start = dt_util.now().date() - timedelta(days=BACKFILL_DAYS + 5)
    entry = entry_with_history(start)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    # The task has already checked the statistics table and is about to fetch when setup returns.
    fake_client.fetch_error = SewConnectionError("down")
    task = entry.runtime_data.backfill_task
    assert task is not None
    await task
    await hass.async_block_till_done()

    assert len(fake_client.fetch_ranges) == 3
    issue = issue_registry.async_get_issue(DOMAIN, f"{ISSUE_BACKFILL_FAILED}_{entry.entry_id}")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {
        "error": "Cannot reach the South East Water portal: down",
        "start": start.isoformat(),
    }

    # A later successful run clears the issue.
    fake_client.fetch_error = None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    task = entry.runtime_data.backfill_task
    assert task is not None
    await task
    assert issue_registry.async_get_issue(DOMAIN, f"{ISSUE_BACKFILL_FAILED}_{entry.entry_id}") is None


async def test_dead_session_during_import_starts_reauth(hass: HomeAssistant, fake_client: FakeClient) -> None:
    start = dt_util.now().date() - timedelta(days=BACKFILL_DAYS + 5)
    entry = entry_with_history(start)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    fake_client.alive = False
    task = entry.runtime_data.backfill_task
    assert task is not None
    await task
    await hass.async_block_till_done()

    assert any(True for _ in entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert len(fake_client.fetch_ranges) == 1


async def test_entry_without_history_starts_no_task(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert setup_integration.runtime_data.backfill_task is None
