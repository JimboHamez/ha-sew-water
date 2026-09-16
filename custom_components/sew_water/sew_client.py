"""Pure-aiohttp client for the South East Water customer portal.

The portal (``my.southeastwater.com.au``) is a Salesforce Experience Cloud site. Every step of the
login, multi-factor authentication and usage-data flow is plain HTTP, so no browser is needed:

1. ``GET /s/login/`` - the page embeds the Aura framework context (``fwuid`` and the loaded-app hash).
2. ``POST /s/sfsites/aura`` with the ``cm_LoginAURA.login`` Apex action - a good login returns a
   ``frontdoor.jsp`` URL, a bad one returns an error string.
3. ``GET`` the frontdoor URL - this sets the Salesforce session cookies and redirects to the MFA
   Visualforce page ``/apex/PortalMFALoginFlow``.
4. ``POST /PortalMFALoginFlow`` twice (RichFaces a4j form posts): once to choose the delivery channel
   and send the code, once to verify it. Each response carries a fresh JSF ViewState that must be
   carried into the next post.
5. ``GET /s/`` - every HTML page load issues a fresh Aura CSRF token in a ``__Host-ERIC_PROD-*``
   cookie; the page names that cookie in its ``eikoocnekot`` bootstrap setting.
6. ``POST /s/sfsites/aura`` with ``ApexActionController.execute`` actions to discover the billing
   account and meter record IDs and to fetch usage. Many single-day usage actions batch into one POST.

This module has no Home Assistant imports so it can be unit-tested offline with ``aioresponses``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from http.cookies import SimpleCookie
import json
import logging
import re
from typing import Any, cast
from urllib.parse import unquote

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://my.southeastwater.com.au"

APEX_PORTAL_CLASSNAME = "cm_AccountBillingUsageAURA"
# Field lists for the discovery queries. The portal asks for far more; these are the ones we use.
BILLING_ACCOUNT_FIELDS = "Id, Name, Status__c, Property__c, Property__r.Digital_Meter__c"
METER_FIELDS = "Id, Name, Is_Digital__c, Digital_Meter__c, Property__c"
METER_DIGITAL_FILTER = "(Is_Digital__c = true OR Digital_Meter__c = true)"
APEX_USAGE_CLASSNAME = "MysewUsageBillingGraphController"
APEX_USAGE_METHOD = "getUsageData"
AURA_APP_COMMUNITY = "siteforce:communityApp"
AURA_APP_LOGIN = "siteforce:loginApp2"
AURA_PATH = "/s/sfsites/aura"
HOME_PATH = "/s/"
LOGIN_PATH = "/s/login/"
MFA_FORM_PATH = "/PortalMFALoginFlow"
MFA_PAGE_PATH = "/apex/PortalMFALoginFlow"

MFA_CHANNEL_EMAIL = "Email"
MFA_CHANNEL_SMS = "SMS"

# One batched POST covers this many single-day usage actions. 120 was verified to work, but a
# request that runs longer than five seconds counts against Salesforce's org-wide concurrent
# long-running Apex limit (shared by every portal user); 30 actions finish comfortably under that.
USAGE_BATCH_SIZE = 30
# The portal reports hourly readings per day; the client sums them into a daily total.
USAGE_RESOLUTION = "hourly"

# Requests default to the browser user agent the portal was verified against.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

_AUTH_PATH_RE = re.compile(r"/login|/secur/|PortalMFALoginFlow|loginflow", re.IGNORECASE)
_AURA_CONTEXT_RE = re.compile(r"/s/sfsites/l/(%7B.+?%7D)/[a-z]+\.js", re.IGNORECASE)
_TOKEN_COOKIE_NAME_RE = re.compile(r'"eikoocnekot"\s*:\s*"([^"]+)"')
_VIEWSTATE_PREFIX = "com.salesforce.visualforce.ViewState"
_META_LOCATION_RE = re.compile(r'<meta\s+name="Location"\s+content="([^"]+)"', re.IGNORECASE)
# ``frontdoor.jsp`` answers 200 with a page whose script performs the redirect; the noscript link
# carries the same URL.
_SCRIPT_REDIRECT_RE = re.compile(r'location\.(?:replace\(|href\s*=\s*)"([^"]+)"', re.IGNORECASE)
_MFA_ERROR_RE = re.compile(
    r"(?:incorrect|invalid|expired|try again|locked|too many|unable|failed)[^<]{0,120}",
    re.IGNORECASE,
)
# Salesforce reports throttling as an Apex error message, not as an HTTP status.
_BUSY_RE = re.compile(r"concurrent requests limit|request limit exceeded|too many requests", re.IGNORECASE)
_FRONTDOOR_MARKER = "frontdoor.jsp"
_SID_RE = re.compile(r"(sid=)[^&\"'\s]+", re.IGNORECASE)


class SewError(Exception):
    """Base error for the SEW client."""


class SewConnectionError(SewError):
    """The portal could not be reached or returned a server error."""


class SewBusyError(SewConnectionError):
    """The portal is throttling requests; retry later.

    Attributes:
        retry_after: Seconds the portal asked us to wait, when it said.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Create the error with an optional retry hint."""
        super().__init__(message)
        self.retry_after = retry_after


class SewAuthError(SewError):
    """Credentials, the MFA code or the stored session were rejected."""


class SewProtocolError(SewError):
    """The portal responded with something the client does not understand."""


class SewLoginUnexplainedError(SewProtocolError):
    """The portal neither accepted the login nor said why.

    The login action succeeded but returned no redirect and no rejection text, so the client cannot
    tell whether the credentials were wrong or the portal wants something else from this account.
    """


@dataclass(frozen=True)
class AuraContext:
    """Aura framework context embedded in a portal page.

    Attributes:
        app: Aura application descriptor, e.g. ``siteforce:communityApp``.
        fwuid: Framework UID the server expects in every Aura request.
        loaded: Map of loaded application descriptors to their hashes.
    """

    app: str
    fwuid: str
    loaded: dict[str, str]

    def as_payload(self) -> dict[str, Any]:
        """Return the ``aura.context`` form field for an Aura request."""
        return {
            "app": self.app,
            "dn": [],
            "fwuid": self.fwuid,
            "globals": {},
            "loaded": self.loaded,
            "mode": "PROD",
            "uad": True,
        }


@dataclass(frozen=True)
class LoginResult:
    """Outcome of ``async_login``.

    Attributes:
        mfa_required: True when the portal wants a one-time code before the session is usable.
        channels: Delivery channels offered for the code (``Email``, ``SMS``); empty when no MFA.
    """

    mfa_required: bool
    channels: tuple[str, ...]


@dataclass(frozen=True)
class AccountIds:
    """Salesforce record IDs needed for usage queries.

    Attributes:
        billing_account_id: ``Billing_Account__c`` record ID (``a08...``).
        meter_id: ``Meter_Details__c`` record ID (``a1K...``).
        meter_serial: Physical meter serial number when the portal reports it.
    """

    billing_account_id: str
    meter_id: str
    meter_serial: str | None = None


@dataclass(frozen=True)
class DailyUsage:
    """Water usage for one calendar day.

    Attributes:
        day: The calendar day the readings belong to.
        litres: Total litres for the day (sum of the hourly readings).
        readings: The hourly readings as reported, when present.
        serial: Meter serial the portal attributed the readings to.
        message: Portal status message, ``Ok`` for a normal day.
        available: False when the portal returned no readings for the day yet.
    """

    day: date
    litres: int
    readings: tuple[int, ...]
    serial: str | None
    message: str | None
    available: bool


@dataclass
class _MfaForm:
    """Parsed state of the ``PortalMFALoginFlow`` Visualforce form."""

    form_id: str
    hidden: dict[str, str] = field(default_factory=dict)
    submit_buttons: dict[str, str] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    otp_boxes: list[str] = field(default_factory=list)

    @property
    def viewstate(self) -> dict[str, str]:
        """Return only the JSF ViewState fields, which must be echoed on every post."""
        return {k: v for k, v in self.hidden.items() if k.startswith(_VIEWSTATE_PREFIX)}

    def button_named(self, label: str) -> tuple[str, str] | None:
        """Return the ``(name, value)`` of the submit button whose label matches, if any."""
        for name, value in self.submit_buttons.items():
            if label.lower() in value.lower():
                return name, value
        return None


class _FormParser(HTMLParser):
    """Collect ``<form>`` and ``<input>`` elements from a Visualforce page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str, str]] = []
        self.inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record form ids/actions and every input's attributes."""
        attributes = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self.forms.append(
                (
                    attributes.get("id") or attributes.get("name") or "",
                    attributes.get("action", ""),
                )
            )
        elif tag == "input":
            self.inputs.append(attributes)


def _parse_mfa_form(page: str) -> _MfaForm:
    """Parse the MFA page into the fields the next form post needs.

    Args:
        page: HTML (or the a4j XML wrapper around it) of ``PortalMFALoginFlow``.

    Returns:
        The parsed form state.

    Raises:
        SewProtocolError: If the page does not contain the MFA form.
    """
    parser = _FormParser()
    parser.feed(page)
    form_id = next((fid for fid, action in parser.forms if MFA_FORM_PATH in action), None)
    if form_id is None:
        raise SewProtocolError("MFA form not found on page")
    form = _MfaForm(form_id=form_id)
    for inp in parser.inputs:
        name, kind, value = (
            inp.get("name", ""),
            inp.get("type", "text"),
            inp.get("value", ""),
        )
        if not name:
            continue
        if kind == "hidden":
            form.hidden[name] = value
        elif kind == "submit":
            form.submit_buttons[name] = value
        elif kind == "radio" and name == "channel":
            form.channels.append(value)
        elif kind == "text" and name.lower().startswith("otpbox"):
            form.otp_boxes.append(name)
    if not form.viewstate:
        raise SewProtocolError("MFA form has no ViewState")
    return form


def _parse_aura_context(page: str, expected_app: str | None = None) -> AuraContext:
    """Extract the Aura context from a portal page.

    The context is URL-encoded inside ``/s/sfsites/l/{...}/bootstrap.js`` style script URLs.

    Args:
        page: HTML of a portal page.
        expected_app: When given, only a context for this app is accepted.

    Returns:
        The parsed context.

    Raises:
        SewProtocolError: If no usable context is present.
    """
    for match in _AURA_CONTEXT_RE.finditer(page):
        try:
            ctx = json.loads(unquote(match.group(1)))
        except ValueError:
            continue
        fwuid, app, loaded = ctx.get("fwuid"), ctx.get("app"), ctx.get("loaded")
        if not (fwuid and app and isinstance(loaded, dict)):
            continue
        if expected_app and app != expected_app:
            continue
        return AuraContext(app=app, fwuid=fwuid, loaded={str(k): str(v) for k, v in loaded.items()})
    raise SewProtocolError("Aura context not found on page")


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header given in seconds; dates are ignored."""
    if value is None:
        return None
    try:
        return max(float(value.strip()), 0.0)
    except ValueError:
        return None


def _records(value: Any) -> list[dict[str, Any]]:
    """Return the record list from an Apex return value, which arrives JSON-encoded as a string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict):
        value = value.get("records", [value])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _find_frontdoor(node: Any) -> str | None:
    """Return the first string in a JSON structure that looks like a frontdoor URL."""
    if isinstance(node, str):
        return node if _FRONTDOOR_MARKER in node else None
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for child in children:
        if (found := _find_frontdoor(child)) is not None:
            return found
    return None


def _redact(envelope: dict[str, Any]) -> str:
    """Serialise an Aura envelope for a debug log with session IDs masked."""
    return _SID_RE.sub(r"\1<redacted>", json.dumps(envelope, separators=(",", ":"))[:2000])


class SewClient:
    """Async client for the South East Water portal.

    The client owns no network resources; pass an ``aiohttp.ClientSession`` whose cookie jar will
    hold the portal session. Persist the jar between runs with ``export_cookies`` / ``import_cookies``
    so that MFA is only needed when the session finally expires.
    """

    def __init__(self, session: aiohttp.ClientSession, base_url: str = DEFAULT_BASE_URL) -> None:
        """Create a client.

        Args:
            session: HTTP session; its cookie jar carries the portal login.
            base_url: Portal origin, without a trailing slash.
        """
        self._session = session
        self._base = URL(base_url.rstrip("/"))
        self._context: AuraContext | None = None
        self._token: str | None = None
        self._mfa_form: _MfaForm | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        """The HTTP session whose cookie jar carries the portal login."""
        return self._session

    # ------------------------------------------------------------------ helpers

    def _url(self, path: str) -> URL:
        """Build an absolute portal URL."""
        return self._base.with_path(path, keep_query=False)

    async def _request(self, method: str, url: URL | str, **kwargs: Any) -> tuple[aiohttp.ClientResponse, str]:
        """Perform a request and return the response together with its body text.

        Raises:
            SewConnectionError: On network failure or a 5xx response.
        """
        headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
        try:
            async with self._session.request(method, url, headers=headers, **kwargs) as resp:
                text = await resp.text()
                if resp.status in (429, 503):
                    raise SewBusyError(
                        f"Portal is busy (HTTP {resp.status}) for {resp.url.path}",
                        retry_after=_retry_after_seconds(resp.headers.get("Retry-After")),
                    )
                if resp.status >= 500:
                    raise SewConnectionError(f"Portal returned HTTP {resp.status} for {resp.url.path}")
                return resp, text
        except aiohttp.ClientError as err:
            raise SewConnectionError(f"Request to portal failed: {err}") from err

    async def _aura(
        self,
        actions: list[dict[str, Any]],
        context: AuraContext,
        token: str | None,
        page_uri: str,
        *,
        resync: bool = True,
    ) -> list[dict[str, Any]]:
        """Send one Aura message and return its list of action results.

        See ``_aura_envelope`` for the arguments and errors.
        """
        envelope = await self._aura_envelope(actions, context, token, page_uri, resync=resync)
        return cast(list[dict[str, Any]], envelope["actions"])

    async def _aura_envelope(
        self,
        actions: list[dict[str, Any]],
        context: AuraContext,
        token: str | None,
        page_uri: str,
        *,
        resync: bool = True,
    ) -> dict[str, Any]:
        """Send one Aura message and return the whole response envelope.

        Args:
            actions: Fully formed action objects (``descriptor``, ``params``...). IDs are assigned here.
            context: Aura context to send.
            token: Aura CSRF token; ``None`` for anonymous (login page) calls.
            page_uri: Value for ``aura.pageURI``.
            resync: When the portal reports the framework context is stale (after a Salesforce
                release), reload the home page for a fresh one and retry once.

        Raises:
            SewAuthError: If the session is invalid.
            SewBusyError: If the portal is throttling.
            SewProtocolError: If the response is not an Aura envelope.
        """
        message = {"actions": [{"id": f"{i + 1};a", **action} for i, action in enumerate(actions)]}
        data = {
            "aura.context": json.dumps(context.as_payload(), separators=(",", ":")),
            "aura.pageURI": page_uri,
            "aura.token": token if token is not None else "null",
            "message": json.dumps(message, separators=(",", ":")),
        }
        # The query string only labels the request in server logs; it mirrors what the browser sends.
        labels = {a["descriptor"].rsplit("$", 1)[-1]: "1" for a in actions}
        url = self._url(AURA_PATH).with_query({"r": str(len(actions)), **labels})
        headers = {
            "Origin": str(self._base),
            "Referer": str(self._url(page_uri)),
            "X-SFDC-Page-Scope-Id": "",
        }
        resp, text = await self._request("POST", url, data=data, headers=headers)
        if resp.status == 401 or _AUTH_PATH_RE.search(resp.url.path):
            raise SewAuthError("Portal session is no longer valid")
        try:
            envelope = json.loads(text)
        except ValueError as err:
            raise SewProtocolError("Aura response is not JSON") from err
        if envelope.get("exceptionEvent"):
            descriptor = str(envelope.get("event", {}).get("descriptor", ""))
            detail = str(envelope.get("message") or "")
            if "invalidSession" in descriptor:
                raise SewAuthError(f"Aura rejected the session ({descriptor})")
            if "clientOutOfSync" in descriptor:
                if not resync or token is None:
                    raise SewProtocolError("Aura framework context is out of sync")
                _LOGGER.debug("Aura context out of sync; reloading the home page")
                if not await self._load_home():
                    raise SewAuthError("Portal session is no longer valid")
                assert self._context is not None and self._token is not None
                return await self._aura_envelope(actions, self._context, self._token, page_uri, resync=False)
            if _BUSY_RE.search(detail):
                raise SewBusyError(f"Portal is throttling requests: {detail}")
            raise SewProtocolError(f"Aura exception event: {descriptor or detail}")
        results = envelope.get("actions")
        if not isinstance(results, list) or len(results) != len(actions):
            raise SewProtocolError("Aura response has an unexpected number of actions")
        return cast(dict[str, Any], envelope)

    @staticmethod
    def _action_value(result: dict[str, Any]) -> Any:
        """Return an action's return value or raise on an error state."""
        if result.get("state") != "SUCCESS":
            errors = result.get("error") or []
            message = errors[0].get("message") if errors and isinstance(errors[0], dict) else result.get("state")
            if _BUSY_RE.search(str(message)):
                raise SewBusyError(f"Portal is throttling requests: {message}")
            raise SewProtocolError(f"Aura action failed: {message}")
        return result.get("returnValue")

    async def _load_home(self) -> bool:
        """Load ``/s/`` and refresh the Aura context and token from it.

        Returns:
            True when the page is the logged-in home page, False when it bounced to authentication.
        """
        resp, page = await self._request("GET", self._url(HOME_PATH))
        return self._absorb_home(resp, page)

    def _absorb_home(self, resp: aiohttp.ClientResponse, page: str) -> bool:
        """Take the Aura token and context from a response that should be the home page.

        Returns:
            True when the response is the logged-in home page, False when it is an authentication page.

        Raises:
            SewProtocolError: If the page looks logged in but carries no token.
        """
        if _AUTH_PATH_RE.search(resp.url.path):
            self._token = None
            return False
        cookie_name = _TOKEN_COOKIE_NAME_RE.search(page)
        token_cookie = resp.cookies.get(cookie_name.group(1)) if cookie_name else None
        if token_cookie is None:
            # Fall back to any ERIC token cookie issued by this response.
            token_cookie = next((c for n, c in resp.cookies.items() if n.startswith("__Host-ERIC")), None)
        if token_cookie is None:
            raise SewProtocolError("Home page did not issue an Aura token")
        self._token = token_cookie.value
        self._context = _parse_aura_context(page, AURA_APP_COMMUNITY)
        return True

    async def _ensure_home(self) -> tuple[AuraContext, str]:
        """Return a valid (context, token) pair, loading the home page if needed.

        Raises:
            SewAuthError: If the session is not logged in.
        """
        if self._token is None or self._context is None:
            if not await self._load_home():
                raise SewAuthError("Not logged in")
        assert self._context is not None and self._token is not None
        return self._context, self._token

    # -------------------------------------------------------------------- login

    async def async_login(self, username: str, password: str) -> LoginResult:
        """Submit credentials and advance to the MFA step.

        Args:
            username: Portal login email.
            password: Portal password.

        Returns:
            Whether MFA is required and which channels are offered.

        Raises:
            SewAuthError: If the credentials are rejected.
            SewConnectionError: If the portal is unreachable.
            SewProtocolError: If the portal's responses do not match the known flow.
        """
        self._token = None
        self._context = None
        self._mfa_form = None
        _, login_page = await self._request("GET", self._url(LOGIN_PATH))
        login_ctx = _parse_aura_context(login_page, AURA_APP_LOGIN)
        action = {
            "callingDescriptor": "UNKNOWN",
            "descriptor": "apex://cm_LoginAURA/ACTION$login",
            "params": {"password": password, "startUrl": "/", "username": username},
        }
        envelope = await self._aura_envelope([action], login_ctx, None, LOGIN_PATH)
        (result,) = envelope["actions"]
        value = self._action_value(result)
        # The controller normally returns the frontdoor URL as the action's value; fall back to a
        # redirect carried elsewhere in the envelope (``aura.redirect`` puts it in an event).
        redirect = value if isinstance(value, str) and _FRONTDOOR_MARKER in value else _find_frontdoor(envelope)
        if redirect is None:
            if isinstance(value, str) and value:
                _LOGGER.debug("Login rejected: %s", value)
                raise SewAuthError(value)
            _LOGGER.debug("Login gave neither a redirect nor a reason; response: %s", _redact(envelope))
            raise SewLoginUnexplainedError("The portal returned no login redirect and no rejection message")
        frontdoor = URL(redirect)
        if not frontdoor.is_absolute():
            frontdoor = self._base.join(frontdoor)
        resp, page = await self._request("GET", frontdoor)
        if (scripted := _SCRIPT_REDIRECT_RE.search(page)) and _FRONTDOOR_MARKER in resp.url.path:
            # The session cookies are set by this response; the page itself only redirects.
            _LOGGER.debug("Following the frontdoor script redirect")
            resp, page = await self._request("GET", self._base.join(URL(scripted.group(1))))
        if MFA_PAGE_PATH.lower() in resp.url.path.lower() or MFA_FORM_PATH in page:
            self._mfa_form = _parse_mfa_form(page)
            return LoginResult(mfa_required=True, channels=tuple(self._mfa_form.channels))
        if resp.url.path == HOME_PATH and self._absorb_home(resp, page):
            return LoginResult(mfa_required=False, channels=())
        raise SewProtocolError(f"Unexpected page after login: {resp.url.path}")

    async def async_request_code(self, channel: str) -> None:
        """Ask the portal to send a one-time code.

        Args:
            channel: One of the channels returned by ``async_login`` (``Email`` or ``SMS``).

        Raises:
            SewProtocolError: If called before ``async_login`` or the portal did not show the code form.
        """
        form = self._mfa_form
        if form is None:
            raise SewProtocolError("Call async_login before requesting a code")
        if channel not in form.channels:
            raise SewProtocolError(f"Unknown MFA channel {channel!r}; portal offers {form.channels}")
        button = form.button_named("send")
        if button is None:
            raise SewProtocolError("Send-code button not found on MFA form")
        data = {
            "AJAXREQUEST": "_viewRoot",
            form.form_id: form.form_id,
            "channel": channel,
            f"{form.form_id}:channelRadio": channel,
            **form.viewstate,
            button[0]: button[1],
        }
        page = await self._post_mfa(data)
        new_form = _parse_mfa_form(page)
        if not new_form.otp_boxes:
            raise SewProtocolError("Portal did not show the code entry form")
        self._mfa_form = new_form

    async def async_submit_code(self, code: str) -> None:
        """Verify the one-time code and complete the login.

        Args:
            code: The digits the user received.

        Raises:
            SewAuthError: If the portal rejects the code.
            SewProtocolError: If called out of order or the portal responds unexpectedly.
        """
        form = self._mfa_form
        if form is None or not form.otp_boxes:
            raise SewProtocolError("Call async_request_code before submitting a code")
        digits = re.sub(r"\D", "", code)
        if len(digits) != len(form.otp_boxes):
            raise SewAuthError(f"Code must be {len(form.otp_boxes)} digits")
        button = form.button_named("verify")
        if button is None:
            raise SewProtocolError("Verify button not found on MFA form")
        data = {
            "AJAXREQUEST": "_viewRoot",
            form.form_id: form.form_id,
            f"{form.form_id}:otpHidden": digits,
            **dict(zip(form.otp_boxes, digits, strict=True)),
            **form.viewstate,
            button[0]: button[1],
        }
        resp, page = await self._request("POST", self._url(MFA_FORM_PATH), data=data, headers=self._mfa_headers())
        meta_location = _META_LOCATION_RE.search(page)
        redirected = resp.headers.get("Location") or (meta_location.group(1) if meta_location else None)
        if not redirected and "otpbox" in page.lower():
            error = _MFA_ERROR_RE.search(re.sub(r"<[^>]+>", " ", page))
            self._mfa_form = _parse_mfa_form(page)
            raise SewAuthError(error.group(0).strip() if error else "Code rejected")
        self._mfa_form = None
        if not await self._load_home():
            raise SewAuthError("Portal did not accept the code")

    def _mfa_headers(self) -> dict[str, str]:
        """Headers the a4j form posts expect."""
        return {
            "Accept": "*/*",
            "Origin": str(self._base),
            "Referer": str(self._url(MFA_PAGE_PATH).with_query(retURL="/")),
        }

    async def _post_mfa(self, data: dict[str, str]) -> str:
        """Post the MFA form and return the page the portal sent back."""
        resp, page = await self._request("POST", self._url(MFA_FORM_PATH), data=data, headers=self._mfa_headers())
        if resp.status != 200:
            raise SewProtocolError(f"MFA form post returned HTTP {resp.status}")
        return page

    # ------------------------------------------------------------------ session

    def export_cookies(self) -> list[dict[str, Any]]:
        """Serialise the portal cookies so the session can be restored later.

        Returns:
            A JSON-serialisable list, one entry per cookie.
        """
        host = self._base.host or ""
        cookies: list[dict[str, Any]] = []
        for morsel in self._session.cookie_jar:
            domain = morsel["domain"] or host
            if not (domain.lstrip(".") == host or host.endswith(domain.lstrip("."))):
                continue
            cookies.append(
                {
                    "domain": domain,
                    "expires": morsel["expires"] or None,
                    "httponly": bool(morsel["httponly"]),
                    "name": morsel.key,
                    "path": morsel["path"] or "/",
                    "secure": bool(morsel["secure"]),
                    "value": morsel.value,
                }
            )
        return cookies

    def import_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Restore cookies previously produced by ``export_cookies``.

        Args:
            cookies: The serialised cookie list.
        """
        self._token = None
        self._context = None
        for cookie in cookies:
            domain = str(cookie.get("domain") or self._base.host or "")
            origin = URL.build(
                scheme="https",
                host=domain.lstrip("."),
                path=str(cookie.get("path") or "/"),
            )
            morsel: SimpleCookie = SimpleCookie()
            morsel[cookie["name"]] = cookie["value"]
            item = morsel[cookie["name"]]
            item["path"] = str(cookie.get("path") or "/")
            if domain.startswith("."):
                item["domain"] = domain
            if cookie.get("secure"):
                item["secure"] = True
            if cookie.get("httponly"):
                item["httponly"] = True
            if cookie.get("expires"):
                item["expires"] = str(cookie["expires"])
            self._session.cookie_jar.update_cookies(morsel, origin)

    async def async_is_alive(self) -> bool:
        """Check whether the current cookies still represent a logged-in session.

        Returns:
            True if the portal home page loads with a fresh Aura token.
        """
        try:
            return await self._load_home()
        except SewProtocolError:
            return False

    # --------------------------------------------------------------------- data

    async def async_discover_ids(self) -> AccountIds:
        """Find the billing account and meter record IDs for the logged-in customer.

        Mirrors the portal's own start-up calls: ``retrieveBillingAccounts`` gives the billing account
        and its property; ``retrieveSObject`` on ``Meter_Details__c`` filtered by that property and the
        digital-meter flags gives the meter. The first account and first digital meter are used.

        Returns:
            The IDs required by ``async_fetch_usage``.

        Raises:
            SewAuthError: If not logged in.
            SewProtocolError: If the IDs cannot be found.
        """
        context, token = await self._ensure_home()
        accounts_action = {
            "callingDescriptor": "markup://c:PortalDataHub",
            "descriptor": f"apex://{APEX_PORTAL_CLASSNAME}/ACTION$retrieveBillingAccounts",
            "params": {"fieldsToRetrieve": BILLING_ACCOUNT_FIELDS},
        }
        (result,) = await self._aura([accounts_action], context, token, HOME_PATH)
        accounts = _records(self._action_value(result))
        account = next((a for a in accounts if isinstance(a.get("Id"), str)), None)
        if account is None:
            raise SewProtocolError("No billing account found for this login")
        billing_account_id = str(account["Id"])
        property_id = account.get("Property__c")
        if not isinstance(property_id, str) or not property_id:
            raise SewProtocolError("Billing account has no property")
        meter_action = {
            "callingDescriptor": "markup://c:PortalDataHub",
            "descriptor": f"apex://{APEX_PORTAL_CLASSNAME}/ACTION$retrieveSObject",
            "params": {
                "fieldsToRetrieve": METER_FIELDS,
                "objectToReturn": "Meter_Details__c",
                "whereClause": f"Property__c IN ('{property_id}') AND {METER_DIGITAL_FILTER}",
            },
        }
        (result,) = await self._aura([meter_action], context, token, HOME_PATH)
        meters = _records(self._action_value(result))
        meter = next((m for m in meters if isinstance(m.get("Id"), str)), None)
        if meter is None:
            raise SewProtocolError("No digital meter found for the property")
        serial = meter.get("Name")
        return AccountIds(
            billing_account_id=billing_account_id,
            meter_id=str(meter["Id"]),
            meter_serial=str(serial) if serial else None,
        )

    async def async_fetch_usage(self, ids: AccountIds, date_from: date, date_to: date) -> list[DailyUsage]:
        """Fetch daily water usage for an inclusive date range.

        One Aura action per day is batched into POSTs of ``USAGE_BATCH_SIZE`` actions.

        Args:
            ids: Record IDs from ``async_discover_ids``.
            date_from: First day, inclusive.
            date_to: Last day, inclusive.

        Returns:
            One entry per day in the range, in chronological order.

        Raises:
            SewAuthError: If the session is invalid.
            SewProtocolError: If the portal returns malformed data.
        """
        if date_to < date_from:
            raise ValueError("date_to must not be before date_from")
        context, token = await self._ensure_home()
        days = [date_from + timedelta(days=n) for n in range((date_to - date_from).days + 1)]
        usage: list[DailyUsage] = []
        for start in range(0, len(days), USAGE_BATCH_SIZE):
            chunk = days[start : start + USAGE_BATCH_SIZE]
            actions = [self._usage_action(ids, day) for day in chunk]
            results = await self._aura(actions, context, token, HOME_PATH)
            for day, result in zip(chunk, results, strict=True):
                usage.append(self._parse_usage(day, self._action_value(result)))
        return usage

    @staticmethod
    def _usage_action(ids: AccountIds, day: date) -> dict[str, Any]:
        """Build the ``getUsageData`` action for one day."""
        return {
            "callingDescriptor": "UNKNOWN",
            "descriptor": "aura://ApexActionController/ACTION$execute",
            "params": {
                "cacheable": False,
                "classname": APEX_USAGE_CLASSNAME,
                "isContinuation": False,
                "method": APEX_USAGE_METHOD,
                "namespace": "",
                "params": {
                    "baId": ids.billing_account_id,
                    "dateFrom": day.isoformat(),
                    "dateTo": day.isoformat(),
                    "meterId": ids.meter_id,
                    "resolution": USAGE_RESOLUTION,
                },
            },
        }

    @staticmethod
    def _parse_usage(day: date, value: Any) -> DailyUsage:
        """Turn one ``getUsageData`` return value into a ``DailyUsage``."""
        # ApexActionController wraps the Apex return value one level deeper than plain Apex actions.
        if isinstance(value, dict) and "returnValue" in value:
            value = value["returnValue"]
        entry: dict[str, Any] | None = None
        if isinstance(value, list):
            entry = next((e for e in value if isinstance(e, dict)), None)
        elif isinstance(value, dict):
            entry = value
        if entry is None:
            return DailyUsage(
                day=day,
                litres=0,
                readings=(),
                serial=None,
                message=None,
                available=False,
            )
        raw_readings = entry.get("readings") or []
        if not isinstance(raw_readings, list):
            raise SewProtocolError(f"Unexpected readings for {day}: {raw_readings!r}")
        readings = tuple(int(round(float(r or 0))) for r in raw_readings)
        return DailyUsage(
            day=day,
            litres=sum(readings),
            readings=readings,
            serial=entry.get("serialNo"),
            message=entry.get("message"),
            available=bool(readings) and entry.get("status", 200) == 200,
        )
