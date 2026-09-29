"""Offline tests for ``sew_client`` against recorded portal response shapes."""

from __future__ import annotations

from datetime import date
import json
import re
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import aiohttp
from aioresponses import aioresponses
import pytest
from yarl import URL

import sew_client
from sew_client import (  # loaded from file by conftest.py, without importing the HA package
    USAGE_BATCH_SIZE,
    AccountIds,
    SewAuthError,
    SewBusyError,
    SewClient,
    SewConnectionError,
    SewLoginUnexplainedError,
    SewProtocolError,
)

from .conftest import (
    AURA_TOKEN,
    BASE,
    BILLING_ACCOUNT_ID,
    FWUID_HOME,
    FWUID_LOGIN,
    HASH_HOME,
    METER_ID,
    METER_SERIAL,
    VIEWSTATE_1,
    VIEWSTATE_2,
    aura_envelope,
    aura_script_tag,
    home_headers,
    home_page,
    login_page,
    mfa_channel_page,
    mfa_code_page,
    usage_day,
)

AURA_URL = re.compile(rf"^{re.escape(BASE)}/s/sfsites/aura(\?.*)?$")
FRONTDOOR = f"{BASE}/secur/frontdoor.jsp?sid=FAKE-SID&retURL=%2F"
IDS = AccountIds(billing_account_id=BILLING_ACCOUNT_ID, meter_id=METER_ID)


def posted_form(mocked: aioresponses, url_pattern: str | re.Pattern[str], index: int = 0) -> dict[str, str]:
    """Return the decoded form body of the *index*-th POST whose URL matches."""
    posts = [
        (key, calls)
        for key, calls in mocked.requests.items()
        if key[0] == "POST"
        and (re.search(url_pattern, str(key[1])) if isinstance(url_pattern, re.Pattern) else url_pattern in str(key[1]))
    ]
    calls = [call for _, calls in posts for call in calls]
    data = calls[index].kwargs["data"]
    if isinstance(data, str):
        return {k: v[0] for k, v in parse_qs(data).items()}
    return {str(k): str(v) for k, v in data.items()}


def aura_message(form: dict[str, str]) -> dict[str, Any]:
    """Decode the ``message`` field of an Aura form post."""
    return json.loads(form["message"])


def mock_home(mocked: aioresponses, repeat: bool = False) -> None:
    """Serve the logged-in home page with a token cookie."""
    mocked.get(f"{BASE}/s/", status=200, body=home_page(), headers=home_headers(), repeat=repeat)


def mock_dead_home(mocked: aioresponses) -> None:
    """Serve a home page that bounces to the login page, as an expired session does."""
    mocked.get(f"{BASE}/s/", status=302, headers={"Location": f"{BASE}/s/login/?ec=302&startURL=%2Fs%2F"})
    mocked.get(f"{BASE}/s/login/?ec=302&startURL=%2Fs%2F", status=200, body=login_page())


async def login_to_mfa(client: SewClient, mocked: aioresponses) -> None:
    """Drive a successful credential login up to the channel-choice page."""
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    mocked.post(AURA_URL, status=200, payload=aura_envelope(FRONTDOOR))
    mocked.get(FRONTDOOR, status=302, headers={"Location": f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F"})
    mocked.get(f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F", status=200, body=mfa_channel_page())
    result = await client.async_login("user@example.com", "hunter2")
    assert result.mfa_required is True
    assert result.channels == ("Email", "SMS")


# ----------------------------------------------------------------------------- login


async def test_login_sends_credentials_with_login_context(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    form = posted_form(mocked, "/s/sfsites/aura")
    assert form["aura.token"] == "null"
    assert form["aura.pageURI"] == "/s/login/"
    context = json.loads(form["aura.context"])
    assert context["app"] == "siteforce:loginApp2"
    assert context["fwuid"] == FWUID_LOGIN
    (action,) = aura_message(form)["actions"]
    assert action["descriptor"] == "apex://cm_LoginAURA/ACTION$login"
    assert action["params"] == {"password": "hunter2", "startUrl": "/", "username": "user@example.com"}


async def test_login_rejects_bad_credentials(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    mocked.post(AURA_URL, status=200, payload=aura_envelope("Your login attempt has failed. Please try again."))
    with pytest.raises(SewAuthError, match="login attempt has failed"):
        await client.async_login("user@example.com", "wrong")


def debug_lines(mock_debug: MagicMock) -> list[str]:
    """Render the messages passed to a patched ``_LOGGER.debug``."""
    return [str(call.args[0]) % call.args[1:] for call in mock_debug.call_args_list]


async def test_login_with_empty_result_is_unexplained_not_auth(client: SewClient, mocked: aioresponses) -> None:
    """A null return value is not a credentials rejection: the portal said nothing (issue #1)."""
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    envelope = aura_envelope(None)
    envelope["actions"][0]["error"] = []
    mocked.post(AURA_URL, status=200, payload=envelope)
    with patch.object(sew_client._LOGGER, "debug") as debug, pytest.raises(SewLoginUnexplainedError):
        await client.async_login("user@example.com", "hunter2")
    assert any('"returnValue":null' in line for line in debug_lines(debug))


async def test_login_follows_redirect_carried_in_an_event(client: SewClient, mocked: aioresponses) -> None:
    """``aura.redirect`` puts the frontdoor URL in an event with a null return value."""
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    envelope = aura_envelope(None)
    envelope["events"] = [{"descriptor": "markup://aura:redirect", "attributes": {"values": {"url": FRONTDOOR}}}]
    mocked.post(AURA_URL, status=200, payload=envelope)
    mocked.get(FRONTDOOR, status=302, headers={"Location": f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F"})
    mocked.get(f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F", status=200, body=mfa_channel_page())
    result = await client.async_login("user@example.com", "hunter2")
    assert result.mfa_required is True


def test_redacted_envelope_masks_session_ids() -> None:
    envelope = aura_envelope(None)
    envelope["events"] = [{"descriptor": "markup://aura:redirect", "attributes": {"values": {"url": FRONTDOOR}}}]
    text = sew_client._redact(envelope)
    assert "FAKE-SID" not in text
    assert "sid=<redacted>&retURL" in text


FRONTDOOR_PAGE = """<html><head><meta HTTP-EQUIV="PRAGMA" CONTENT="NO-CACHE"></head><body>
<script>var tm=null;function lhdoredir(){tm&&(window.clearTimeout(tm),tm=null);window.location.replace?
window.location.replace("{target}"):window.location.href="{target}"}window.setTimeout?
tm=window.setTimeout(lhdoredir,1E3):lhdoredir();</script>
<noscript>Javascript is required. Click <a href="{target}">here</a> to continue.</noscript>
</body></html>"""


async def test_login_follows_frontdoor_script_redirect(client: SewClient, mocked: aioresponses) -> None:
    """The live frontdoor page is a 200 whose script redirects to the MFA page; it has no form."""
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    mocked.post(AURA_URL, status=200, payload=aura_envelope(FRONTDOOR))
    target = f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F"
    mocked.get(FRONTDOOR, status=200, body=FRONTDOOR_PAGE.replace("{target}", target))
    mocked.get(target, status=200, body=mfa_channel_page())
    result = await client.async_login("user@example.com", "hunter2")
    assert result.mfa_required is True
    assert result.channels == ("Email", "SMS")


async def test_login_follows_relative_frontdoor_script_redirect_home(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    mocked.post(AURA_URL, status=200, payload=aura_envelope(FRONTDOOR))
    mocked.get(FRONTDOOR, status=200, body=FRONTDOOR_PAGE.replace("{target}", "/s/"))
    mock_home(mocked)
    result = await client.async_login("user@example.com", "hunter2")
    assert result.mfa_required is False


async def test_login_without_mfa_lands_on_home(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    mocked.post(AURA_URL, status=200, payload=aura_envelope(FRONTDOOR))
    mocked.get(FRONTDOOR, status=302, headers={"Location": f"{BASE}/s/"})
    mock_home(mocked)
    result = await client.async_login("user@example.com", "hunter2")
    assert result.mfa_required is False
    assert result.channels == ()


async def test_login_page_without_context_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(f"{BASE}/s/login/", status=200, body="<html><body>maintenance</body></html>")
    with pytest.raises(SewProtocolError, match="Aura context"):
        await client.async_login("user@example.com", "hunter2")


async def test_server_error_is_connection_error(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(f"{BASE}/s/login/", status=503, body="unavailable")
    with pytest.raises(SewConnectionError, match="503"):
        await client.async_login("user@example.com", "hunter2")


async def test_network_failure_is_connection_error(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(f"{BASE}/s/login/", exception=aiohttp.ClientConnectionError("dns"))
    with pytest.raises(SewConnectionError, match="dns"):
        await client.async_login("user@example.com", "hunter2")


# ------------------------------------------------------------------------------- mfa


async def test_request_code_posts_channel_and_viewstate(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=200, body=mfa_code_page(), content_type="text/xml")
    await client.async_request_code("SMS")
    form = posted_form(mocked, "/PortalMFALoginFlow")
    assert form["AJAXREQUEST"] == "_viewRoot"
    assert form["j_id0:mfaForm"] == "j_id0:mfaForm"
    assert form["channel"] == "SMS"
    assert form["j_id0:mfaForm:channelRadio"] == "SMS"
    assert form["j_id0:mfaForm:j_id26"] == "Send code"
    for key, value in VIEWSTATE_1.items():
        assert form[key] == value
    assert "otpBox1" not in form


async def test_request_code_rejects_unknown_channel(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    with pytest.raises(SewProtocolError, match="Unknown MFA channel"):
        await client.async_request_code("Carrier pigeon")


async def test_request_code_before_login_is_protocol_error(client: SewClient) -> None:
    with pytest.raises(SewProtocolError, match="async_login"):
        await client.async_request_code("Email")


async def test_submit_code_carries_refreshed_viewstate_and_completes_login(
    client: SewClient, mocked: aioresponses
) -> None:
    await login_to_mfa(client, mocked)
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=200, body=mfa_code_page(), content_type="text/xml")
    await client.async_request_code("Email")
    mocked.post(
        f"{BASE}/PortalMFALoginFlow",
        status=200,
        body='<?xml version="1.0"?><html><head><meta name="Location" content="/s/" /></head></html>',
        headers={"Location": f"{BASE}/s/"},
        content_type="text/xml",
    )
    mock_home(mocked)
    await client.async_submit_code("12 34-56")
    form = posted_form(mocked, "/PortalMFALoginFlow", index=1)
    assert form["j_id0:mfaForm:otpHidden"] == "123456"
    assert [form[f"otpBox{n}"] for n in range(1, 7)] == list("123456")
    assert form["j_id0:mfaForm:j_id34"] == "Verify"
    for key, value in VIEWSTATE_2.items():
        assert form[key] == value
    assert form["com.salesforce.visualforce.ViewState"] != VIEWSTATE_1["com.salesforce.visualforce.ViewState"]


async def test_submit_wrong_code_raises_auth_error_and_allows_retry(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=200, body=mfa_code_page(), content_type="text/xml")
    await client.async_request_code("Email")
    mocked.post(
        f"{BASE}/PortalMFALoginFlow",
        status=200,
        body=mfa_code_page(error="The code you entered is invalid. Please try again."),
        content_type="text/xml",
    )
    with pytest.raises(SewAuthError, match="invalid"):
        await client.async_submit_code("000000")
    # The form is re-parsed so a second attempt can be made without restarting the flow.
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=200, headers={"Location": f"{BASE}/s/"}, body="")
    mock_home(mocked)
    await client.async_submit_code("123456")


async def test_submit_code_with_wrong_length_is_rejected_locally(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=200, body=mfa_code_page(), content_type="text/xml")
    await client.async_request_code("Email")
    with pytest.raises(SewAuthError, match="6 digits"):
        await client.async_submit_code("1234")


async def test_submit_code_before_request_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    with pytest.raises(SewProtocolError, match="async_request_code"):
        await client.async_submit_code("123456")


# --------------------------------------------------------------------------- session


async def test_is_alive_true_when_home_issues_token(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    assert await client.async_is_alive() is True


async def test_is_alive_false_when_bounced_to_login(client: SewClient, mocked: aioresponses) -> None:
    mock_dead_home(mocked)
    assert await client.async_is_alive() is False


async def test_is_alive_false_when_home_has_no_token(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(f"{BASE}/s/", status=200, body=home_page())
    assert await client.async_is_alive() is False


async def test_cookie_export_import_round_trip(session: aiohttp.ClientSession, client: SewClient) -> None:
    session.cookie_jar.update_cookies({"sid": "SESSION-ID", "renderCtx": "ctx"}, URL(f"{BASE}/"))
    session.cookie_jar.update_cookies({"_ga": "analytics"}, URL("https://www.southeastwater.com.au/"))
    session.cookie_jar.update_cookies({"unrelated": "x"}, URL("https://example.com/"))
    exported = client.export_cookies()
    names = {c["name"] for c in exported}
    assert "sid" in names and "renderCtx" in names
    assert "unrelated" not in names
    assert all(isinstance(c["value"], str) for c in exported)
    json.dumps(exported)  # must be serialisable for config-entry storage

    async with aiohttp.ClientSession() as fresh:
        restored = SewClient(fresh, BASE)
        restored.import_cookies(exported)
        jar = fresh.cookie_jar.filter_cookies(URL(f"{BASE}/s/"))
        assert jar["sid"].value == "SESSION-ID"
        assert jar["renderCtx"].value == "ctx"
        assert "unrelated" not in jar


# -------------------------------------------------------------------------- discovery


PROPERTY_ID = "a07900000000PROPAAE"
OTHER_PROPERTY_ID = "a07900000000OTHRAAE"
OTHER_ACCOUNT_ID = "a0890000008OTHRAAA"
OTHER_METER_ID = "a1K90000000OTHREAC"


def account_record(account_id: str = BILLING_ACCOUNT_ID, property_id: str | None = PROPERTY_ID) -> dict[str, Any]:
    """One billing account as ``getBillingAccountsAndMetersForUser`` returns it."""
    record: dict[str, Any] = {
        "Account_Opened__c": "2001-01-01",
        "HiAF_Account_Number_Check_Digit__c": "12345678",
        "Id": account_id,
        "Property_Address__c": "1 EXAMPLE STREET<br>EXAMPLEVILLE VIC 3000",
        "Role__c": "Owner Occupier",
    }
    if property_id is not None:
        record["Property__c"] = property_id
    return record


def meter_record(
    meter_id: str = METER_ID, property_id: str = PROPERTY_ID, digital: bool = True, serial: str | None = METER_SERIAL
) -> dict[str, Any]:
    """One meter as ``getBillingAccountsAndMetersForUser`` returns it."""
    return {"Id": meter_id, "Is_Digital__c": digital, "Name": serial, "Property__c": property_id}


def discovery_value(accounts: Any, meters: Any) -> dict[str, Any]:
    """The ``getBillingAccountsAndMetersForUser`` value as ``ApexActionController`` wraps it."""
    return {
        "cacheable": False,
        "returnValue": {"baSettings": [], "billingAccounts": accounts, "meters": meters},
    }


async def test_discover_ids_uses_home_token_and_the_usage_page_call(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(discovery_value([account_record()], [meter_record()])))
    ids = await client.async_discover_ids()
    assert ids == AccountIds(billing_account_id=BILLING_ACCOUNT_ID, meter_id=METER_ID, meter_serial=METER_SERIAL)

    form = posted_form(mocked, "/s/sfsites/aura")
    assert form["aura.token"] == AURA_TOKEN
    context = json.loads(form["aura.context"])
    assert context["app"] == "siteforce:communityApp"
    assert context["fwuid"] == FWUID_HOME
    assert context["loaded"] == {"APPLICATION@markup://siteforce:communityApp": HASH_HOME}
    (action,) = aura_message(form)["actions"]
    assert action["descriptor"] == "aura://ApexActionController/ACTION$execute"
    # The portal sends this call without a "params" key.
    assert action["params"] == {
        "cacheable": False,
        "classname": "MysewUsageBillingGraphController",
        "isContinuation": False,
        "method": "getBillingAccountsAndMetersForUser",
        "namespace": "",
    }


async def test_discover_ids_picks_the_account_whose_property_has_a_digital_meter(
    client: SewClient, mocked: aioresponses
) -> None:
    """A login can hold several accounts; the first one may be at a property with only a mechanical meter."""
    mock_home(mocked)
    accounts = [account_record(OTHER_ACCOUNT_ID, OTHER_PROPERTY_ID), account_record()]
    meters = [meter_record(OTHER_METER_ID, OTHER_PROPERTY_ID, digital=False, serial="12345678"), meter_record()]
    mocked.post(AURA_URL, status=200, payload=aura_envelope(discovery_value(accounts, meters)))
    with patch.object(sew_client._LOGGER, "info") as info:
        ids = await client.async_discover_ids()
    assert ids == AccountIds(billing_account_id=BILLING_ACCOUNT_ID, meter_id=METER_ID, meter_serial=METER_SERIAL)
    # Only one digital meter, so there was no choice to report.
    info.assert_not_called()


async def test_discover_ids_with_two_digital_meters_on_one_property_uses_the_first(
    client: SewClient, mocked: aioresponses
) -> None:
    mock_home(mocked)
    meters = [meter_record(), meter_record(OTHER_METER_ID, serial="SAHL999999")]
    mocked.post(AURA_URL, status=200, payload=aura_envelope(discovery_value([account_record()], meters)))
    with patch.object(sew_client._LOGGER, "info") as info, patch.object(sew_client._LOGGER, "debug") as debug:
        ids = await client.async_discover_ids()
    assert ids == AccountIds(billing_account_id=BILLING_ACCOUNT_ID, meter_id=METER_ID, meter_serial=METER_SERIAL)
    (info_line,) = debug_lines(info)
    assert info_line == "Found 2 digital meters across 1 billing account(s); using the first the portal lists"
    # Serials are identifiers, so they appear only at debug level.
    assert METER_SERIAL not in info_line and "SAHL999999" not in info_line
    assert f"['{METER_SERIAL}', 'SAHL999999']; using {METER_SERIAL}" in "\n".join(debug_lines(debug))


async def test_discover_ids_with_a_digital_meter_on_each_account_uses_the_first_account(
    client: SewClient, mocked: aioresponses
) -> None:
    """The first account wins with its own meter, even when the portal lists that meter second."""
    mock_home(mocked)
    accounts = [account_record(), account_record(OTHER_ACCOUNT_ID, OTHER_PROPERTY_ID)]
    meters = [meter_record(OTHER_METER_ID, OTHER_PROPERTY_ID, serial="SAHL999999"), meter_record()]
    mocked.post(AURA_URL, status=200, payload=aura_envelope(discovery_value(accounts, meters)))
    with patch.object(sew_client._LOGGER, "info") as info:
        ids = await client.async_discover_ids()
    assert ids == AccountIds(billing_account_id=BILLING_ACCOUNT_ID, meter_id=METER_ID, meter_serial=METER_SERIAL)
    assert debug_lines(info) == ["Found 2 digital meters across 2 billing account(s); using the first the portal lists"]


async def test_discover_ids_accepts_json_strings_and_missing_serial(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    value = {
        "billingAccounts": json.dumps([account_record()]),
        "meters": json.dumps([meter_record(serial=None)]),
    }
    mocked.post(AURA_URL, status=200, payload=aura_envelope(json.dumps(value)))
    ids = await client.async_discover_ids()
    assert ids == AccountIds(billing_account_id=BILLING_ACCOUNT_ID, meter_id=METER_ID, meter_serial=None)


async def test_discover_ids_without_accounts_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(discovery_value([], [meter_record()])))
    with pytest.raises(SewProtocolError, match="No billing account"):
        await client.async_discover_ids()


@pytest.mark.parametrize(
    "meters",
    [
        [],
        [meter_record(digital=False)],
        [meter_record(property_id=OTHER_PROPERTY_ID)],
    ],
    ids=["no-meters", "mechanical-only", "other-property"],
)
async def test_discover_ids_without_matching_digital_meter_is_protocol_error(
    client: SewClient, mocked: aioresponses, meters: list[dict[str, Any]]
) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(discovery_value([account_record()], meters)))
    with pytest.raises(SewProtocolError, match="No digital meter found for the 1 billing account"):
        await client.async_discover_ids()


async def test_discover_ids_without_property_on_account_is_protocol_error(
    client: SewClient, mocked: aioresponses
) -> None:
    mock_home(mocked)
    payload = aura_envelope(discovery_value([account_record(property_id=None)], [meter_record()]))
    mocked.post(AURA_URL, status=200, payload=payload)
    with pytest.raises(SewProtocolError, match="No digital meter"):
        await client.async_discover_ids()


@pytest.mark.parametrize("value", ["not json at all", None, [1, 2]], ids=["bad-string", "null", "list"])
async def test_discover_ids_with_unexpected_value_is_protocol_error(
    client: SewClient, mocked: aioresponses, value: Any
) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope({"cacheable": False, "returnValue": value}))
    with pytest.raises(SewProtocolError, match="Unexpected getBillingAccountsAndMetersForUser response"):
        await client.async_discover_ids()


async def test_discover_ids_refused_by_the_portal_reports_its_reason(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    refused = aura_envelope({"cacheable": False}, state="ERROR")
    refused["actions"][0]["error"] = [{"message": "You do not have access to the Apex class named 'X'."}]
    mocked.post(AURA_URL, status=200, payload=refused)
    with pytest.raises(SewProtocolError, match="do not have access"):
        await client.async_discover_ids()


async def test_dropped_action_is_logged_and_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    """Aura drops an action it cannot run with an empty action list and no reason (issue #5)."""
    mock_home(mocked)
    dropped = {"actions": [], "context": {"mode": "PROD", "app": "siteforce:communityApp"}, "perfSummary": {}}
    mocked.post(AURA_URL, status=200, payload=dropped)
    with patch.object(sew_client._LOGGER, "debug") as debug, pytest.raises(SewProtocolError, match="no action results"):
        await client.async_discover_ids()
    assert any("actions=0 expected=1 app=siteforce:communityApp" in line for line in debug_lines(debug))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('[{"Id": "x"}]', [{"Id": "x"}]),
        ("not json", []),
        ({"records": [{"Id": "x"}, "junk"]}, [{"Id": "x"}]),
        ({"Id": "x"}, [{"Id": "x"}]),
        (5, []),
        (None, []),
    ],
    ids=["json-string", "bad-string", "records-dict", "single-dict", "number", "none"],
)
def test_records_normalises_apex_values(value: Any, expected: list[dict[str, Any]]) -> None:
    assert sew_client._records(value) == expected


async def test_aura_error_body_with_malformed_message_is_protocol_error(
    client: SewClient, mocked: aioresponses
) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, body="*/{not json}/*ERROR*/")
    with pytest.raises(SewProtocolError, match="Aura error: no message"):
        await client.async_discover_ids()


async def test_aura_error_body_is_reported_with_its_message(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, body='*/{"message":"No apex action available for X.y"}/*ERROR*/')
    with pytest.raises(SewProtocolError, match="Aura error: No apex action available for X.y"):
        await client.async_discover_ids()


async def test_aura_error_body_about_limits_is_busy(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, body='*/{"message":"Concurrent requests limit exceeded"}/*ERROR*/')
    with pytest.raises(SewBusyError):
        await client.async_discover_ids()


async def test_aura_non_json_body_is_logged_and_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, body="<html>maintenance sid=SECRET</html>")
    with patch.object(sew_client._LOGGER, "debug") as debug, pytest.raises(SewProtocolError, match="not JSON"):
        await client.async_discover_ids()
    lines = debug_lines(debug)
    assert any("maintenance" in line for line in lines)
    assert not any("SECRET" in line for line in lines)


async def test_discover_ids_when_not_logged_in_is_auth_error(client: SewClient, mocked: aioresponses) -> None:
    mock_dead_home(mocked)
    with pytest.raises(SewAuthError):
        await client.async_discover_ids()


# ------------------------------------------------------------------------------ usage


async def test_fetch_usage_sums_hourly_readings(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    readings = [0, 14, 4, 0, 0, 0, 4, 0, 42, 128, 53, 37, 35, 40, 81, 69, 213, 207, 25, 56, 151, 15, 1, 5]
    mocked.post(
        AURA_URL,
        status=200,
        payload=aura_envelope(usage_day("2026-09-12", [10] * 24), usage_day("2026-09-13", readings)),
    )
    usage = await client.async_fetch_usage(IDS, date(2026, 9, 12), date(2026, 9, 13))
    assert [u.day for u in usage] == [date(2026, 9, 12), date(2026, 9, 13)]
    assert usage[0].litres == 240
    assert usage[1].litres == 1180
    assert usage[1].readings == tuple(readings)
    assert usage[1].serial == METER_SERIAL
    assert usage[1].message == "Ok"
    assert all(u.available for u in usage)

    form = posted_form(mocked, "/s/sfsites/aura")
    actions = aura_message(form)["actions"]
    assert [a["id"] for a in actions] == ["1;a", "2;a"]
    assert all(a["descriptor"] == "aura://ApexActionController/ACTION$execute" for a in actions)
    assert actions[0]["params"]["classname"] == "MysewUsageBillingGraphController"
    assert actions[0]["params"]["method"] == "getUsageData"
    assert actions[0]["params"]["params"] == {
        "baId": BILLING_ACCOUNT_ID,
        "dateFrom": "2026-09-12",
        "dateTo": "2026-09-12",
        "meterId": METER_ID,
        "resolution": "hourly",
    }
    assert form["aura.pageURI"] == "/s/"


async def test_fetch_usage_marks_missing_days_unavailable(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(usage_day("2026-09-13", None), {"returnValue": []}))
    usage = await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 14))
    assert [u.available for u in usage] == [False, False]
    assert [u.litres for u in usage] == [0, 0]


async def test_fetch_usage_batches_large_ranges(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    start = date(2026, 6, 1)
    total_days = USAGE_BATCH_SIZE + 5
    days = [date.fromordinal(start.toordinal() + n) for n in range(total_days)]
    first_batch = [usage_day(d.isoformat(), [1] * 24) for d in days[:USAGE_BATCH_SIZE]]
    second_batch = [usage_day(d.isoformat(), [2] * 24) for d in days[USAGE_BATCH_SIZE:]]
    mocked.post(AURA_URL, status=200, payload=aura_envelope(*first_batch))
    mocked.post(AURA_URL, status=200, payload=aura_envelope(*second_batch))
    usage = await client.async_fetch_usage(IDS, days[0], days[-1])
    assert len(usage) == total_days
    assert usage[0].litres == 24 and usage[-1].litres == 48
    first = aura_message(posted_form(mocked, "/s/sfsites/aura", index=0))["actions"]
    second = aura_message(posted_form(mocked, "/s/sfsites/aura", index=1))["actions"]
    assert len(first) == USAGE_BATCH_SIZE and len(second) == 5


async def test_fetch_usage_rejects_reversed_range(client: SewClient) -> None:
    with pytest.raises(ValueError):
        await client.async_fetch_usage(IDS, date(2026, 9, 14), date(2026, 9, 13))


async def test_fetch_usage_invalid_session_is_auth_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(
        AURA_URL,
        status=200,
        payload={"exceptionEvent": True, "event": {"descriptor": "markup://aura:invalidSession"}},
    )
    with pytest.raises(SewAuthError, match="invalidSession"):
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))


async def test_fetch_usage_action_error_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    payload = aura_envelope(None, state="ERROR")
    payload["actions"][0]["error"] = [{"message": "Attempt to de-reference a null object"}]
    mocked.post(AURA_URL, status=200, payload=payload)
    with pytest.raises(SewProtocolError, match="null object"):
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))


async def test_fetch_usage_non_json_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, body="<html>oops</html>", content_type="text/html")
    with pytest.raises(SewProtocolError, match="not JSON"):
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))


# -------------------------------------------------------------------------- throttling


async def test_concurrent_limit_apex_error_is_busy_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    payload = aura_envelope(None, state="ERROR")
    payload["actions"][0]["error"] = [{"message": "Unable to process request. Concurrent requests limit exceeded."}]
    mocked.post(AURA_URL, status=200, payload=payload)
    with pytest.raises(SewBusyError, match="Concurrent requests limit") as info:
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))
    assert info.value.retry_after is None
    assert isinstance(info.value, SewConnectionError)


async def test_http_429_with_retry_after_is_busy_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=429, body="slow down", headers={"Retry-After": "30"})
    with pytest.raises(SewBusyError, match="429") as info:
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))
    assert info.value.retry_after == 30.0


async def test_http_503_without_retry_after_is_busy_error(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(
        f"{BASE}/s/login/", status=503, body="maintenance", headers={"Retry-After": "Thu, 01 Jan 2026 00:00:00 GMT"}
    )
    with pytest.raises(SewBusyError) as info:
        await client.async_login("user@example.com", "hunter2")
    assert info.value.retry_after is None


async def test_client_out_of_sync_refreshes_context_and_retries(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(
        AURA_URL, status=200, payload={"exceptionEvent": True, "event": {"descriptor": "markup://aura:clientOutOfSync"}}
    )
    mocked.get(
        f"{BASE}/s/",
        status=200,
        body=home_page().replace(FWUID_HOME, "NEWFWUID"),
        headers={"Set-Cookie": "__Host-ERIC_PROD-123456789=NEW-TOKEN; Path=/; Secure; HttpOnly"},
    )
    mocked.post(AURA_URL, status=200, payload=aura_envelope(usage_day("2026-09-13", [1] * 24)))
    usage = await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))
    assert usage[0].litres == 24
    retry = posted_form(mocked, "/s/sfsites/aura", index=1)
    assert retry["aura.token"] == "NEW-TOKEN"
    assert json.loads(retry["aura.context"])["fwuid"] == "NEWFWUID"


async def test_client_out_of_sync_twice_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    out_of_sync = {"exceptionEvent": True, "event": {"descriptor": "markup://aura:clientOutOfSync"}}
    mocked.post(AURA_URL, status=200, payload=out_of_sync)
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=out_of_sync)
    with pytest.raises(SewProtocolError, match="out of sync"):
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))


async def test_client_out_of_sync_with_dead_session_is_auth_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(
        AURA_URL, status=200, payload={"exceptionEvent": True, "event": {"descriptor": "markup://aura:clientOutOfSync"}}
    )
    mock_dead_home(mocked)
    with pytest.raises(SewAuthError):
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))


# ------------------------------------------------------------------------- edge cases


def test_session_property_exposes_injected_session(session: aiohttp.ClientSession, client: SewClient) -> None:
    assert client.session is session


async def test_login_follows_relative_frontdoor_and_rejects_unknown_page(
    client: SewClient, mocked: aioresponses
) -> None:
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    mocked.post(AURA_URL, status=200, payload=aura_envelope("/secur/frontdoor.jsp?sid=FAKE-SID&retURL=%2F"))
    mocked.get(FRONTDOOR, status=302, headers={"Location": f"{BASE}/s/somewhere-else"})
    mocked.get(f"{BASE}/s/somewhere-else", status=200, body="<html></html>")
    with pytest.raises(SewProtocolError, match="Unexpected page after login"):
        await client.async_login("user@example.com", "hunter2")


async def test_login_page_skips_malformed_and_foreign_contexts(client: SewClient, mocked: aioresponses) -> None:
    """Broken or unrelated bootstrap URLs are skipped and the right app's context is still found."""
    page = (
        '<html><head><script src="/s/sfsites/l/%7Bnot-json/bootstrap.js"></script>'
        '<script src="/s/sfsites/l/%7B%22mode%22%3A%22PROD%22%7D/bootstrap.js"></script>'
        f"{aura_script_tag('siteforce:communityApp', FWUID_HOME, HASH_HOME)}"
        f"{login_page()}"
    )
    mocked.get(f"{BASE}/s/login/", status=200, body=page)
    mocked.post(AURA_URL, status=200, payload=aura_envelope("Your login attempt has failed. Please try again."))
    with pytest.raises(SewAuthError):
        await client.async_login("user@example.com", "wrong")
    context = json.loads(posted_form(mocked, "/s/sfsites/aura")["aura.context"])
    assert context["fwuid"] == FWUID_LOGIN


async def test_aura_401_is_auth_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=401, body="")
    with pytest.raises(SewAuthError, match="no longer valid"):
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))


async def test_aura_other_exception_event_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    payload = {"exceptionEvent": True, "event": {"descriptor": "markup://aura:systemError"}, "message": "boom"}
    mocked.post(AURA_URL, status=200, payload=payload)
    with pytest.raises(SewProtocolError, match="systemError"):
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))


async def test_aura_wrong_action_count_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(usage_day("2026-09-13", [1] * 24)))
    with pytest.raises(SewProtocolError, match="unexpected number of actions"):
        await client.async_fetch_usage(IDS, date(2026, 9, 12), date(2026, 9, 13))


async def test_mfa_page_without_form_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    mocked.post(AURA_URL, status=200, payload=aura_envelope(FRONTDOOR))
    mocked.get(FRONTDOOR, status=302, headers={"Location": f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F"})
    mocked.get(f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F", status=200, body="<html><body>nothing</body></html>")
    with pytest.raises(SewProtocolError, match="MFA form not found"):
        await client.async_login("user@example.com", "hunter2")


async def test_mfa_form_without_viewstate_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    page = re.sub(r'<input type="hidden" name="com\.salesforce[^>]*/>', "", mfa_channel_page())
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    mocked.post(AURA_URL, status=200, payload=aura_envelope(FRONTDOOR))
    mocked.get(FRONTDOOR, status=302, headers={"Location": f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F"})
    mocked.get(f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F", status=200, body=page)
    with pytest.raises(SewProtocolError, match="no ViewState"):
        await client.async_login("user@example.com", "hunter2")


async def test_mfa_form_without_send_button_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    page = (
        mfa_channel_page()
        .replace('value="Send code"', 'value="Continue"')
        .replace("<form", '<input type="text" /><form')
    )
    mocked.get(f"{BASE}/s/login/", status=200, body=login_page())
    mocked.post(AURA_URL, status=200, payload=aura_envelope(FRONTDOOR))
    mocked.get(FRONTDOOR, status=302, headers={"Location": f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F"})
    mocked.get(f"{BASE}/apex/PortalMFALoginFlow?retURL=%2F", status=200, body=page)
    await client.async_login("user@example.com", "hunter2")
    with pytest.raises(SewProtocolError, match="Send-code button"):
        await client.async_request_code("Email")


async def test_request_code_without_otp_boxes_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=200, body=mfa_channel_page(), content_type="text/xml")
    with pytest.raises(SewProtocolError, match="code entry form"):
        await client.async_request_code("Email")


async def test_mfa_post_non_200_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=404, body="gone")
    with pytest.raises(SewProtocolError, match="HTTP 404"):
        await client.async_request_code("Email")


async def test_submit_code_without_verify_button_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    page = mfa_code_page().replace('value="Verify"', 'value="Submit"')
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=200, body=page, content_type="text/xml")
    await client.async_request_code("Email")
    with pytest.raises(SewProtocolError, match="Verify button"):
        await client.async_submit_code("123456")


async def test_submit_code_accepted_but_home_dead_is_auth_error(client: SewClient, mocked: aioresponses) -> None:
    await login_to_mfa(client, mocked)
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=200, body=mfa_code_page(), content_type="text/xml")
    await client.async_request_code("Email")
    mocked.post(f"{BASE}/PortalMFALoginFlow", status=200, headers={"Location": f"{BASE}/s/"}, body="")
    mock_dead_home(mocked)
    with pytest.raises(SewAuthError, match="did not accept the code"):
        await client.async_submit_code("123456")


async def test_import_cookies_restores_flags_and_domain_cookies(
    session: aiohttp.ClientSession, client: SewClient
) -> None:
    client.import_cookies(
        [
            {
                "name": "sid",
                "value": "S",
                "domain": ".southeastwater.com.au",
                "path": "/",
                "secure": True,
                "httponly": True,
                "expires": "Wed, 01 Jan 2030 00:00:00 GMT",
            },
            {"name": "plain", "value": "P"},
        ]
    )
    jar = session.cookie_jar.filter_cookies(URL(f"{BASE}/s/"))
    assert jar["sid"].value == "S"
    assert jar["plain"].value == "P"
    exported = {c["name"]: c for c in client.export_cookies()}
    assert exported["sid"]["secure"] is True
    assert exported["sid"]["httponly"] is True
    assert exported["sid"]["expires"]


async def test_fetch_usage_accepts_single_object_return_value(client: SewClient, mocked: aioresponses) -> None:
    single = usage_day("2026-09-13", [2] * 24)
    single["returnValue"] = single["returnValue"][0]
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(single))
    (usage,) = await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))
    assert usage.litres == 48


async def test_fetch_usage_non_list_readings_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    bad = usage_day("2026-09-13", None)
    bad["returnValue"][0]["readings"] = "n/a"
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(bad))
    with pytest.raises(SewProtocolError, match="Unexpected readings"):
        await client.async_fetch_usage(IDS, date(2026, 9, 13), date(2026, 9, 13))


async def test_bad_gateway_is_connection_error(client: SewClient, mocked: aioresponses) -> None:
    mocked.get(f"{BASE}/s/login/", status=502, body="bad gateway")
    with pytest.raises(SewConnectionError, match="502"):
        await client.async_login("user@example.com", "hunter2")
