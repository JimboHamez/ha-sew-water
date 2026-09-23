"""Tests for the setup, reauth and options flows."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sew_water.const import (
    CONF_BILLING_ACCOUNT_ID,
    CONF_COOKIES,
    CONF_IMPORT_FROM,
    CONF_METER_ID,
    CONF_METER_SERIAL,
    CONF_MFA_CHANNEL,
    CONF_MFA_CODE,
    CONF_POLL_TIME,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.sew_water.sew_client import (
    LoginResult,
    SewAuthError,
    SewConnectionError,
    SewLoginUnexplainedError,
    SewProtocolError,
)

from .conftest import (
    BILLING_ACCOUNT_ID,
    COOKIES,
    METER_ID,
    METER_SERIAL,
    PASSWORD,
    USERNAME,
    FakeClient,
)

CREDENTIALS = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


async def start_user_flow(hass: HomeAssistant) -> dict[str, Any]:
    """Open the setup wizard and return the first form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return dict(result)


async def submit(hass: HomeAssistant, flow_id: str, user_input: dict[str, Any] | None) -> dict[str, Any]:
    """Submit a step and let Home Assistant settle."""
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    await hass.async_block_till_done()
    return dict(result)


# ------------------------------------------------------------------------------ setup


async def test_full_flow_creates_entry(hass: HomeAssistant, fake_client: FakeClient) -> None:
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa_channel"

    result = await submit(hass, result["flow_id"], {CONF_MFA_CHANNEL: "sms"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa_code"

    result = await submit(hass, result["flow_id"], {CONF_MFA_CODE: "123456"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"

    result = await submit(hass, result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"] == {
        CONF_BILLING_ACCOUNT_ID: BILLING_ACCOUNT_ID,
        CONF_COOKIES: COOKIES,
        CONF_METER_ID: METER_ID,
        CONF_METER_SERIAL: METER_SERIAL,
        CONF_PASSWORD: PASSWORD,
        CONF_USERNAME: USERNAME,
    }
    assert result["result"].unique_id == USERNAME
    # The lowercase option key is translated back to the portal's channel name.
    assert ("request_code", "SMS") in fake_client.calls
    assert ("submit_code", "123456") in fake_client.calls
    assert fake_client.session.closed


async def test_username_is_stripped_and_lowercased_for_unique_id(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], {CONF_USERNAME: "  User@Example.com ", CONF_PASSWORD: PASSWORD})
    result = await submit(hass, result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USERNAME] == "User@Example.com"
    assert result["result"].unique_id == "user@example.com"
    assert ("login", ("User@Example.com", PASSWORD)) in fake_client.calls


@pytest.mark.parametrize("address", ["user", "user@", "@example.com", "user@example", "user example@x.com", " "])
async def test_malformed_email_is_rejected_before_login(
    hass: HomeAssistant, fake_client: FakeClient, address: str
) -> None:
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], {CONF_USERNAME: address, CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_USERNAME: "invalid_email"}
    assert not fake_client.calls

    result = await submit(hass, result["flow_id"], CREDENTIALS)
    assert result["step_id"] == "mfa_channel"


async def test_flow_without_mfa_skips_code_steps(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    result = await submit(hass, result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not any(name in ("request_code", "submit_code") for name, _ in fake_client.calls)


async def test_history_step_stores_installation_date(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    start = dt_util.now().date() - timedelta(days=400)
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    result = await submit(hass, result["flow_id"], {CONF_IMPORT_FROM: start.isoformat()})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IMPORT_FROM] == start.isoformat()
    # The new entry starts the background import; let it finish before the recorder is torn down.
    task = result["result"].runtime_data.backfill_task
    assert task is not None
    await task
    yesterday = dt_util.now().date() - timedelta(days=1)
    assert fake_client.fetch_ranges[-1] == (start, yesterday)


@pytest.mark.parametrize("offset_days", [0, 1])
async def test_history_step_rejects_today_and_later(
    hass: HomeAssistant, fake_client: FakeClient, offset_days: int
) -> None:
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    bad = (dt_util.now().date() + timedelta(days=offset_days)).isoformat()
    result = await submit(hass, result["flow_id"], {CONF_IMPORT_FROM: bad})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    assert result["errors"] == {CONF_IMPORT_FROM: "start_in_future"}

    result = await submit(hass, result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_IMPORT_FROM not in result["data"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SewAuthError("bad"), "invalid_auth"),
        (SewConnectionError("down"), "cannot_connect"),
        (SewLoginUnexplainedError("silent"), "login_unexplained"),
        (SewProtocolError("odd"), "unknown"),
    ],
)
async def test_login_errors_are_shown_and_recoverable(
    hass: HomeAssistant, fake_client: FakeClient, error: Exception, expected: str
) -> None:
    fake_client.login_error = error
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}

    fake_client.login_error = None
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    assert result["step_id"] == "mfa_channel"


async def test_login_without_channels_is_error(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.login_result = LoginResult(mfa_required=True, channels=())
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_mfa_channel"}


@pytest.mark.parametrize(
    ("error", "expected"),
    [(SewConnectionError("down"), "cannot_connect"), (SewProtocolError("odd"), "unknown")],
)
async def test_request_code_errors(
    hass: HomeAssistant, fake_client: FakeClient, error: Exception, expected: str
) -> None:
    fake_client.request_code_error = error
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    result = await submit(hass, result["flow_id"], {CONF_MFA_CHANNEL: "email"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa_channel"
    assert result["errors"] == {"base": expected}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SewAuthError("wrong code"), "invalid_code"),
        (SewConnectionError("down"), "cannot_connect"),
        (SewProtocolError("odd"), "unknown"),
    ],
)
async def test_submit_code_errors_allow_retry(
    hass: HomeAssistant, fake_client: FakeClient, error: Exception, expected: str
) -> None:
    fake_client.submit_code_error = error
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    result = await submit(hass, result["flow_id"], {CONF_MFA_CHANNEL: "email"})
    result = await submit(hass, result["flow_id"], {CONF_MFA_CODE: "000000"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa_code"
    assert result["errors"] == {"base": expected}

    fake_client.submit_code_error = None
    result = await submit(hass, result["flow_id"], {CONF_MFA_CODE: "123456"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SewAuthError("gone"), "invalid_auth"),
        (SewConnectionError("down"), "cannot_connect"),
        (SewProtocolError("no meter"), "unknown"),
    ],
)
async def test_discovery_errors_abort(
    hass: HomeAssistant, fake_client: FakeClient, error: Exception, reason: str
) -> None:
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    fake_client.discover_error = error
    result = await start_user_flow(hass)
    result = await submit(hass, result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_second_entry_is_refused(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Every entry writes the same external statistic, so only one entry is allowed."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


# ----------------------------------------------------------------------------- reauth


async def start_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Trigger reauth for an entry and return the confirm form."""
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"][CONF_USERNAME] == USERNAME
    return dict(result)


async def test_reauth_needs_only_a_new_code(
    hass: HomeAssistant, fake_client: FakeClient, mock_config_entry: MockConfigEntry
) -> None:
    fake_client.exported_cookies = [{"name": "sid", "value": "NEW", "domain": "my.southeastwater.com.au", "path": "/"}]
    result = await start_reauth(hass, mock_config_entry)
    result = await submit(hass, result["flow_id"], {})
    assert result["step_id"] == "mfa_channel"
    result = await submit(hass, result["flow_id"], {CONF_MFA_CHANNEL: "email"})
    assert result["step_id"] == "mfa_code"
    result = await submit(hass, result["flow_id"], {CONF_MFA_CODE: "654321"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # Stored credentials were re-used and the session replaced in place.
    assert ("login", (USERNAME, PASSWORD)) in fake_client.calls
    assert mock_config_entry.data[CONF_COOKIES][0]["value"] == "NEW"
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_reauth_with_rejected_password_asks_for_a_new_one(
    hass: HomeAssistant, fake_client: FakeClient, mock_config_entry: MockConfigEntry
) -> None:
    fake_client.login_error = SewAuthError("rejected")
    result = await start_reauth(hass, mock_config_entry)
    result = await submit(hass, result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_password"
    assert result["description_placeholders"][CONF_USERNAME] == USERNAME

    # Wrong again: stays on the password step with the error shown.
    result = await submit(hass, result["flow_id"], {CONF_PASSWORD: "still-wrong"})
    assert result["step_id"] == "reauth_password"
    assert result["errors"] == {"base": "invalid_auth"}

    fake_client.login_error = None
    result = await submit(hass, result["flow_id"], {CONF_PASSWORD: "new-secret"})
    assert result["step_id"] == "mfa_channel"
    result = await submit(hass, result["flow_id"], {CONF_MFA_CHANNEL: "email"})
    result = await submit(hass, result["flow_id"], {CONF_MFA_CODE: "111111"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-secret"


async def test_reauth_connection_error_stays_on_confirm(
    hass: HomeAssistant, fake_client: FakeClient, mock_config_entry: MockConfigEntry
) -> None:
    fake_client.login_error = SewConnectionError("down")
    result = await start_reauth(hass, mock_config_entry)
    result = await submit(hass, result["flow_id"], {})
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_with_different_account_aborts(
    hass: HomeAssistant, fake_client: FakeClient, mock_config_entry: MockConfigEntry
) -> None:
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    result = await start_reauth(hass, mock_config_entry)
    # Simulate the portal signing in a different user by changing the stored username mid-flow.
    hass.config_entries.async_update_entry(mock_config_entry, unique_id="someone@else.example")
    result = await submit(hass, result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


# ------------------------------------------------------------------------ reconfigure


async def start_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Open the reconfigure flow for a loaded entry and return its first form."""
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    return dict(result)


async def test_reconfigure_replaces_session_with_new_password(
    hass: HomeAssistant, fake_client: FakeClient, mock_config_entry: MockConfigEntry
) -> None:
    fake_client.exported_cookies = [{"name": "sid", "value": "NEW", "domain": "my.southeastwater.com.au", "path": "/"}]
    result = await start_reconfigure(hass, mock_config_entry)
    # The form is pre-filled with the stored email address.
    schema = result["data_schema"]
    assert schema is not None
    assert next(k for k in schema.schema if k == CONF_USERNAME).default() == USERNAME

    result = await submit(hass, result["flow_id"], {CONF_USERNAME: f" {USERNAME} ", CONF_PASSWORD: "new-secret"})
    assert result["step_id"] == "mfa_channel"
    result = await submit(hass, result["flow_id"], {CONF_MFA_CHANNEL: "sms"})
    assert result["step_id"] == "mfa_code"
    result = await submit(hass, result["flow_id"], {CONF_MFA_CODE: "111222"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    assert ("login", (USERNAME, "new-secret")) in fake_client.calls
    assert mock_config_entry.data[CONF_PASSWORD] == "new-secret"
    assert mock_config_entry.data[CONF_COOKIES][0]["value"] == "NEW"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_login_error_is_recoverable(
    hass: HomeAssistant, fake_client: FakeClient, mock_config_entry: MockConfigEntry
) -> None:
    fake_client.login_error = SewAuthError("nope")
    result = await start_reconfigure(hass, mock_config_entry)
    result = await submit(hass, result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "invalid_auth"}

    fake_client.login_error = None
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    result = await submit(hass, result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_rejects_malformed_email(
    hass: HomeAssistant, fake_client: FakeClient, mock_config_entry: MockConfigEntry
) -> None:
    result = await start_reconfigure(hass, mock_config_entry)
    result = await submit(hass, result["flow_id"], {CONF_USERNAME: "not-an-address", CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {CONF_USERNAME: "invalid_email"}
    assert not fake_client.calls


async def test_reconfigure_with_different_account_aborts(
    hass: HomeAssistant, fake_client: FakeClient, mock_config_entry: MockConfigEntry
) -> None:
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    result = await start_reconfigure(hass, mock_config_entry)
    result = await submit(hass, result["flow_id"], {CONF_USERNAME: "someone@else.example", CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert mock_config_entry.data[CONF_USERNAME] == USERNAME
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


# ---------------------------------------------------------------------------- options


async def test_options_flow_sets_scan_interval_and_poll_time(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 360, CONF_POLL_TIME: "10:30:00"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options == {CONF_POLL_TIME: "10:30:00", CONF_SCAN_INTERVAL: 360}
    assert setup_integration.state is config_entries.ConfigEntryState.LOADED
