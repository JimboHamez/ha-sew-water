"""Fixtures for the integration-level tests.

The portal client is replaced by ``FakeClient`` so no HTTP happens; its behaviour is configured per test
by setting attributes before the integration is set up or the flow step is submitted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import date, timedelta
from typing import Any
from unittest.mock import PropertyMock, patch

from homeassistant.components.recorder import Recorder
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sew_water import coordinator as coordinator_module
from custom_components.sew_water.const import (
    CONF_BILLING_ACCOUNT_ID,
    CONF_COOKIES,
    CONF_METER_ID,
    CONF_METER_SERIAL,
    DOMAIN,
)
from custom_components.sew_water.sew_client import AccountIds, DailyUsage, LoginResult

USERNAME = "user@example.com"
PASSWORD = "hunter2"
BILLING_ACCOUNT_ID = "a0890000008XXXXAAA"
METER_ID = "a1K90000000YYYYEAC"
METER_SERIAL = "SAHL000000"
COOKIES = [{"name": "sid", "value": "SESSION", "domain": "my.southeastwater.com.au", "path": "/"}]
REFRESHED_COOKIES = [{"name": "sid", "value": "SESSION-2", "domain": "my.southeastwater.com.au", "path": "/"}]
IDS = AccountIds(billing_account_id=BILLING_ACCOUNT_ID, meter_id=METER_ID, meter_serial=METER_SERIAL)
# Import windows used in tests; the real 90/30-day windows would write thousands of hourly rows per test.
BACKFILL_DAYS = 6
TRAILING_WINDOW_DAYS = 3


def usage_for(day: date, litres_per_hour: int = 10) -> DailyUsage:
    """Build a day with 24 equal hourly readings."""
    readings = tuple([litres_per_hour] * 24)
    return DailyUsage(
        day=day,
        litres=sum(readings),
        readings=readings,
        serial=METER_SERIAL,
        message="Ok",
        available=bool(litres_per_hour),
    )


class FakeClient:
    """Stand-in for ``SewClient`` with scriptable outcomes."""

    def __init__(self) -> None:
        self.login_result = LoginResult(mfa_required=True, channels=("Email", "SMS"))
        self.login_error: Exception | None = None
        self.request_code_error: Exception | None = None
        self.submit_code_error: Exception | None = None
        self.discover_error: Exception | None = None
        self.alive = True
        self.alive_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.ids = IDS
        self.exported_cookies: list[dict[str, Any]] = list(COOKIES)
        self.imported_cookies: list[dict[str, Any]] | None = None
        self.litres_per_hour: Callable[[date], int] = lambda _day: 10
        self.calls: list[tuple[str, Any]] = []
        self.fetch_ranges: list[tuple[date, date]] = []
        self.session = _FakeSession()

    async def async_login(self, username: str, password: str) -> LoginResult:
        """Record the credentials and return the scripted result."""
        self.calls.append(("login", (username, password)))
        if self.login_error:
            raise self.login_error
        return self.login_result

    async def async_request_code(self, channel: str) -> None:
        """Record the channel."""
        self.calls.append(("request_code", channel))
        if self.request_code_error:
            raise self.request_code_error

    async def async_submit_code(self, code: str) -> None:
        """Record the code."""
        self.calls.append(("submit_code", code))
        if self.submit_code_error:
            raise self.submit_code_error

    async def async_discover_ids(self) -> AccountIds:
        """Return the scripted identifiers."""
        self.calls.append(("discover_ids", None))
        if self.discover_error:
            raise self.discover_error
        return self.ids

    def export_cookies(self) -> list[dict[str, Any]]:
        """Return the scripted cookie jar."""
        return list(self.exported_cookies)

    def import_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Remember what the integration restored."""
        self.imported_cookies = list(cookies)

    async def async_is_alive(self) -> bool:
        """Return the scripted liveness."""
        self.calls.append(("is_alive", None))
        if self.alive_error:
            raise self.alive_error
        return self.alive

    async def async_fetch_usage(self, ids: AccountIds, date_from: date, date_to: date) -> list[DailyUsage]:
        """Return one day per date in the range."""
        self.calls.append(("fetch_usage", (date_from, date_to)))
        self.fetch_ranges.append((date_from, date_to))
        if self.fetch_error:
            raise self.fetch_error
        days = (date_to - date_from).days + 1
        return [
            usage_for(date_from + timedelta(days=n), self.litres_per_hour(date_from + timedelta(days=n)))
            for n in range(days)
        ]


class _FakeSession:
    """Just enough of ``aiohttp.ClientSession`` for the config flow to release it."""

    def __init__(self) -> None:
        self.closed = False

    def detach(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _ha_environment(recorder_mock: Recorder, enable_custom_integrations: None) -> None:
    """Give every test an in-memory recorder (the integration depends on it) and custom-component loading.

    ``recorder_mock`` is listed first because its database fixture must be created before ``hass``.
    """


@pytest.fixture(autouse=True)
def _short_windows() -> Iterator[None]:
    """Shrink the backfill and trailing windows so each test writes dozens of rows, not thousands."""
    with (
        patch.object(coordinator_module, "BACKFILL_DAYS", BACKFILL_DAYS),
        patch.object(coordinator_module, "TRAILING_WINDOW_DAYS", TRAILING_WINDOW_DAYS),
    ):
        yield


@pytest.fixture
def entity_registry_enabled_by_default() -> Iterator[None]:
    """Enable entities the integration disables by default, as Home Assistant's own test suite does."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        new_callable=PropertyMock,
        return_value=True,
    ):
        yield


@pytest.fixture
def fake_client() -> Iterator[FakeClient]:
    """Patch every place the integration constructs a client."""
    client = FakeClient()
    with (
        patch("custom_components.sew_water.SewClient", return_value=client),
        patch("custom_components.sew_water.config_flow.SewClient", return_value=client),
        patch("custom_components.sew_water.config_flow.async_create_clientsession", return_value=client.session),
    ):
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A version-2 entry as the config flow would have created it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME,
        version=2,
        data={
            CONF_BILLING_ACCOUNT_ID: BILLING_ACCOUNT_ID,
            CONF_COOKIES: COOKIES,
            CONF_METER_ID: METER_ID,
            CONF_METER_SERIAL: METER_SERIAL,
            CONF_PASSWORD: PASSWORD,
            CONF_USERNAME: USERNAME,
        },
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_client: FakeClient
) -> AsyncIterator[MockConfigEntry]:
    """Set the entry up and yield it loaded."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    yield mock_config_entry
