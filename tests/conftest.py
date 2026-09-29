"""Shared fixtures for the offline SEW client tests.

The HTML/JSON fixtures reproduce the *shape* of real portal responses captured on 2026-09-13 with
all identifiers replaced by obviously fake values.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
import importlib.util
import inspect
import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import quote

import aiohttp
from aioresponses import aioresponses
from aioresponses.compat import stream_reader_factory as _original_stream_reader_factory
import aioresponses.core as aioresponses_core
import pytest

# The client has no Home Assistant imports, but importing it through the package would execute
# custom_components/sew_water/__init__.py, which does. Load the module straight from its file so the
# tests run without Home Assistant installed.
_CLIENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "sew_water" / "sew_client.py"
_spec = importlib.util.spec_from_file_location("sew_client", _CLIENT_PATH)
assert _spec is not None and _spec.loader is not None
sew_client = importlib.util.module_from_spec(_spec)
sys.modules["sew_client"] = sew_client
_spec.loader.exec_module(sew_client)

SewClient = sew_client.SewClient

BASE = "https://my.southeastwater.com.au"
FWUID_LOGIN = "LOGINFWUID0000000000000000000000000000000000"
FWUID_HOME = "HOMEFWUID00000000000000000000000000000000000"
HASH_LOGIN = "1634_loginhash"
HASH_HOME = "1712_homehash"
TOKEN_COOKIE = "__Host-ERIC_PROD-123456789"
AURA_TOKEN = "eyJ0b2tlbiI6ImZha2UifQ.fake-aura-token-value"
BILLING_ACCOUNT_ID = "a0890000008XXXXAAA"
METER_ID = "a1K90000000YYYYEAC"
METER_SERIAL = "SAHL000000"
VIEWSTATE_1 = {
    "com.salesforce.visualforce.ViewState": "VS-ONE",
    "com.salesforce.visualforce.ViewStateVersion": "202609130000000001",
    "com.salesforce.visualforce.ViewStateMAC": "MAC-ONE",
    "com.salesforce.visualforce.ViewStateCSRF": "CSRF-ONE",
}
VIEWSTATE_2 = {k: v.replace("ONE", "TWO").replace("01", "02") for k, v in VIEWSTATE_1.items()}


def aura_script_tag(app: str, fwuid: str, loaded_hash: str) -> str:
    """Return the ``<script src="/s/sfsites/l/{...}/bootstrap.js">`` tag a portal page embeds."""
    ctx = {"mode": "PROD", "app": app, "fwuid": fwuid, "loaded": {f"APPLICATION@markup://{app}": loaded_hash}}
    return f'<script src="/s/sfsites/l/{quote(json.dumps(ctx, separators=(",", ":")), safe="")}/bootstrap.js"></script>'


def login_page() -> str:
    """Public login page with the loginApp2 Aura context."""
    return f"<html><head>{aura_script_tag('siteforce:loginApp2', FWUID_LOGIN, HASH_LOGIN)}</head><body></body></html>"


def home_page() -> str:
    """Logged-in home page naming the token cookie and carrying the communityApp context."""
    return (
        "<html><head>"
        f"{aura_script_tag('siteforce:communityApp', FWUID_HOME, HASH_HOME)}"
        '<script>window.__init = {"eikoocnekot":"' + TOKEN_COOKIE + '","host":"/s/sfsites"};</script>'
        "</head><body><h1>Overview | South East Water</h1></body></html>"
    )


def _viewstate_inputs(viewstate: dict[str, str]) -> str:
    return "".join(f'<input type="hidden" name="{k}" id="{k}" value="{v}" />' for k, v in viewstate.items())


def mfa_channel_page() -> str:
    """The first MFA page: choose Email or SMS and press *Send code*."""
    return (
        "<html><body>"
        '<form id="j_id0:mfaForm" name="j_id0:mfaForm" method="post" action="/PortalMFALoginFlow">'
        '<input type="hidden" name="j_id0:mfaForm" value="j_id0:mfaForm" />'
        '<input type="radio" name="channel" value="Email" /><input type="radio" name="channel" value="SMS" />'
        '<input type="hidden" name="j_id0:mfaForm:channelRadio" id="j_id0:mfaForm:channelRadio" value="" />'
        '<input type="submit" name="j_id0:mfaForm:j_id26" id="j_id0:mfaForm:j_id26" value="Send code" />'
        f"{_viewstate_inputs(VIEWSTATE_1)}"
        "</form></body></html>"
    )


def mfa_code_page(error: str | None = None) -> str:
    """The a4j response after *Send code*: six OTP boxes and a fresh ViewState."""
    banner = f"<span>{error}</span>" if error else ""
    boxes = "".join(f'<input type="text" name="otpBox{n}" id="otpBox{n}" maxlength="1" />' for n in range(1, 7))
    return (
        '<?xml version="1.0"?><html><head><meta name="Ajax-Update-Ids" content="j_id0:mfaForm" /></head><body>'
        '<form id="j_id0:mfaForm" name="j_id0:mfaForm" method="post" action="/PortalMFALoginFlow">'
        f"{banner}"
        '<input type="hidden" name="j_id0:mfaForm" value="j_id0:mfaForm" />'
        '<input type="hidden" name="j_id0:mfaForm:otpHidden" id="j_id0:mfaForm:otpHidden" value="" />'
        f"{boxes}"
        '<input type="submit" name="j_id0:mfaForm:j_id34" id="j_id0:mfaForm:j_id34" value="Verify" />'
        f"{_viewstate_inputs(VIEWSTATE_2)}"
        "</form></body></html>"
    )


def aura_envelope(*return_values: Any, state: str = "SUCCESS") -> dict[str, Any]:
    """Wrap return values in the Aura response envelope, one action per value."""
    return {
        "actions": [{"id": f"{i + 1};a", "state": state, "returnValue": rv} for i, rv in enumerate(return_values)],
        "context": {"mode": "PROD", "fwuid": FWUID_HOME},
        "perfSummary": {},
    }


def usage_day(day: str, readings: list[int] | None, status: int = 200) -> dict[str, Any]:
    """Build one ``getUsageData`` return value as ``ApexActionController`` wraps it."""
    entry = {
        "apiDate": f"{day}T00:00:00+00:00",
        "hasBlockedData": False,
        "message": "Ok",
        "readings": readings if readings is not None else [],
        "resolution": "hourly",
        "serialNo": METER_SERIAL,
        "status": status,
    }
    return {"returnValue": [entry], "cacheable": False}


def home_headers() -> dict[str, str]:
    """Headers the home page responds with: the fresh Aura token cookie."""
    return {"Set-Cookie": f"{TOKEN_COOKIE}={AURA_TOKEN}; Path=/; Secure; HttpOnly"}


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """An aiohttp session with a plain cookie jar."""
    async with aiohttp.ClientSession() as sess:
        yield sess


@pytest.fixture
def client(session: aiohttp.ClientSession) -> SewClient:
    """A client bound to the test session."""
    return SewClient(session, BASE)


class _StreamWriterDefaultResponse(aiohttp.ClientResponse):
    """A ``ClientResponse`` that supplies the ``stream_writer`` aioresponses does not pass."""

    def __init__(self, method: str, url: Any, **kwargs: Any) -> None:
        # aiohttp only reads ``output_size`` from it once the request has been sent.
        kwargs.setdefault("stream_writer", Mock(output_size=0))
        super().__init__(method, url, **kwargs)


def _stream_reader_with_parser(loop: Any = None) -> aiohttp.StreamReader:
    """Build aioresponses' body reader with a stub parser for aiohttp 3.14's flow control."""
    reader = _original_stream_reader_factory(loop)
    protocol: Any = reader._protocol
    if getattr(protocol, "_parser", None) is None:
        protocol._parser = Mock()
        protocol._parser.feed_data.return_value = ([], False, b"")
    return reader


def _aioresponses_compat_patches() -> ExitStack:
    """Patch aioresponses 0.7.9 for aiohttp 3.14 while it is in use.

    aiohttp 3.14 made ``stream_writer`` a required ``ClientResponse`` argument and made
    ``pause_reading()`` delegate to the protocol's parser, which aioresponses' bare protocol lacks.
    These mirror the unmerged upstream fixes (pnuckowski/aioresponses#288 and #292, issue #289). On
    older aiohttp nothing is patched, and both patches step aside once aioresponses supplies these
    itself. Remove this when a fixed aioresponses is released and pinned.
    """
    stack = ExitStack()
    if "stream_writer" not in inspect.signature(aiohttp.ClientResponse).parameters:
        return stack
    stack.enter_context(patch.object(aioresponses_core, "ClientResponse", _StreamWriterDefaultResponse))
    stack.enter_context(patch.object(aioresponses_core, "stream_reader_factory", _stream_reader_with_parser))
    return stack


@pytest.fixture
def mocked() -> Iterator[aioresponses]:
    """Intercept all outbound HTTP."""
    with _aioresponses_compat_patches(), aioresponses() as m:
        yield m
