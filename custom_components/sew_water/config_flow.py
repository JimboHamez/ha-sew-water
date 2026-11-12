"""Config flow for the South East Water integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)
from homeassistant.util import dt as dt_util

# Home Assistant 2026.10 runs voluptuous as Probatio but types schemas as Probatio's, so calls that pass a schema
# carry an arg-type ignore.
import voluptuous as vol

from .const import (
    CONF_BILLING_ACCOUNT_ID,
    CONF_COOKIES,
    CONF_IMPORT_FROM,
    CONF_METER_ID,
    CONF_METER_SERIAL,
    CONF_METERS,
    CONF_MFA_CHANNEL,
    CONF_MFA_CODE,
    CONF_POLL_TIME,
    CONF_PROPERTY,
    CONF_SCAN_INTERVAL,
    DEFAULT_POLL_TIME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENTRY_TITLE,
    MIN_SCAN_INTERVAL,
)
from .coordinator import SewConfigEntry, meters_to_data
from .sew_client import (
    SewAuthError,
    SewClient,
    SewConnectionError,
    SewError,
    SewLoginUnexplainedError,
    SewProperty,
)

_LOGGER = logging.getLogger(__name__)

# Deliberately loose: one "@" with something either side and a dot in the domain. It catches typos
# and pasted junk before a round trip to the portal without rejecting unusual but valid addresses.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def mask_email(address: str) -> str:
    """Mask the login email for display, keeping its first character and domain.

    Args:
        address: The login email.

    Returns:
        The address as ``u***@example.com``, or ``***`` when it has no domain.
    """
    local, at, domain = address.partition("@")
    if not at or not local:
        return "***"
    return f"{local[0]}***@{domain}"


USERNAME_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))
STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): USERNAME_SELECTOR,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)
STEP_CODE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MFA_CODE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code")
        ),
    }
)
STEP_HISTORY_SCHEMA = vol.Schema({vol.Optional(CONF_IMPORT_FROM): DateSelector()})


class SewConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk the user through login, choosing a code channel and entering the code."""

    VERSION = 2
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialise flow state."""
        self._session: aiohttp.ClientSession | None = None
        self._client: SewClient | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._channels: tuple[str, ...] = ()
        self._entry_data: dict[str, Any] | None = None
        # Held only while the flow is open, for the property picker; the addresses are never stored.
        self._properties: list[SewProperty] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SewConfigEntry) -> SewOptionsFlow:
        """Return the options flow handler."""
        return SewOptionsFlow()

    @callback
    def async_remove(self) -> None:
        """Release the flow's private HTTP session when the flow ends for any reason.

        ``detach()`` closes the session but leaves Home Assistant's shared connector alone; ``close()`` on a
        session made by ``async_create_clientsession`` is a stub that only logs a warning.
        """
        if self._session is not None and not self._session.closed:
            self._session.detach()

    # ------------------------------------------------------------------- helpers

    def _new_client(self) -> SewClient:
        """Create a client with a fresh cookie jar for this flow."""
        if self._session is None or self._session.closed:
            self._session = async_create_clientsession(
                hass=self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
            )
        self._client = SewClient(self._session, setup=True)
        return self._client

    async def _async_login(self, errors: dict[str, str]) -> ConfigFlowResult | None:
        """Log in with the stored credentials and move to the channel step.

        Returns:
            The next step's result, or ``None`` (with ``errors`` filled) when the user must retry.
        """
        assert self._username is not None and self._password is not None
        client = self._new_client()
        try:
            result = await client.async_login(self._username, self._password)
        except SewAuthError:
            errors["base"] = "invalid_auth"
        except SewConnectionError:
            errors["base"] = "cannot_connect"
        except SewLoginUnexplainedError:
            _LOGGER.warning("The portal neither accepted the login nor said why; enable debug logging for the response")
            errors["base"] = "login_unexplained"
        except SewError:
            _LOGGER.exception("Unexpected portal response during login")
            errors["base"] = "unknown"
        else:
            if not result.mfa_required:
                return await self._async_finish()
            self._channels = result.channels
            if not self._channels:
                errors["base"] = "no_mfa_channel"
                return None
            return await self.async_step_mfa_channel()
        return None

    async def _async_finish(self) -> ConfigFlowResult:
        """List the login's properties, then update the entry or move on to choosing a property."""
        assert self._client is not None and self._username is not None
        await self.async_set_unique_id(self._username.lower())
        reauth = self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE)
        if reauth:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
        else:
            self._abort_if_unique_id_configured()
        try:
            properties = await self._client.async_list_properties()
        except SewAuthError:
            return self.async_abort(reason="invalid_auth")
        except SewConnectionError:
            return self.async_abort(reason="cannot_connect")
        except SewError:
            _LOGGER.exception("Could not discover the billing account or meter")
            return self.async_abort(reason="unknown")
        if reauth:
            entry = self._get_reauth_entry() if self.source == SOURCE_REAUTH else self._get_reconfigure_entry()
            session_data = self._session_data()
            if CONF_METERS in entry.data:
                # Keep the property chosen at setup. The portal's order is no guide to it, so falling back to
                # the first property could silently switch a home to an investment property.
                stored = entry.data[CONF_BILLING_ACCOUNT_ID]
                chosen = next((p for p in properties if p.billing_account_id == stored), None)
                if chosen is None:
                    return self.async_abort(reason="account_not_found")
                session_data |= self._meter_data(chosen)
            # Otherwise the entry is from before 2.3 and may hold the recycled meter, whose readings then sit in
            # the mains statistic. Its meter keys are left for the coordinator's one-time upgrade after the
            # reload, which lists that account's meters and repairs the statistic.
            return self.async_update_reload_and_abort(entry, data_updates=session_data)
        if len(properties) == 1:
            return await self._async_use_property(properties[0])
        self._properties = properties
        return await self.async_step_property()

    def _session_data(self) -> dict[str, Any]:
        """Return the credentials and the session's cookies for the entry."""
        assert self._client is not None
        return {
            CONF_COOKIES: self._client.export_cookies(),
            CONF_PASSWORD: self._password,
            CONF_USERNAME: self._username,
        }

    @staticmethod
    def _meter_data(chosen: SewProperty) -> dict[str, Any]:
        """Return the entry keys naming a property's billing account and meters, without its address."""
        # Mains first, so CONF_METER_ID keeps naming the mains meter as it always has.
        mains = chosen.meters[0]
        return {
            CONF_BILLING_ACCOUNT_ID: chosen.billing_account_id,
            CONF_METER_ID: mains.meter_id,
            CONF_METER_SERIAL: mains.meter_serial,
            CONF_METERS: meters_to_data(chosen.meters),
        }

    async def _async_use_property(self, chosen: SewProperty) -> ConfigFlowResult:
        """Remember the chosen property's IDs for the new entry and ask for the history start."""
        self._properties = []
        self._entry_data = self._session_data() | self._meter_data(chosen)
        return await self.async_step_history()

    # --------------------------------------------------------------------- steps

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the portal credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            if not _EMAIL_RE.match(self._username):
                errors[CONF_USERNAME] = "invalid_email"
            elif (result := await self._async_login(errors)) is not None:
                return result
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)  # type: ignore[arg-type]

    async def async_step_mfa_channel(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose where the portal should send the one-time code."""
        errors: dict[str, str] = {}
        assert self._client is not None
        if user_input is not None:
            # Option keys are lowercase for translations; the portal wants its own spelling back.
            channel = next(c for c in self._channels if c.lower() == user_input[CONF_MFA_CHANNEL])
            try:
                await self._client.async_request_code(channel)
            except SewConnectionError:
                errors["base"] = "cannot_connect"
            except SewError:
                _LOGGER.exception("Portal did not accept the code request")
                errors["base"] = "unknown"
            else:
                return await self.async_step_mfa_code()
        schema = vol.Schema(
            {
                vol.Required(CONF_MFA_CHANNEL, default=self._channels[0].lower()): SelectSelector(
                    SelectSelectorConfig(
                        options=[c.lower() for c in self._channels],
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_MFA_CHANNEL,
                    )
                )
            }
        )
        return self.async_show_form(step_id="mfa_channel", data_schema=schema, errors=errors)  # type: ignore[arg-type]

    async def async_step_mfa_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Verify the one-time code the user received."""
        errors: dict[str, str] = {}
        assert self._client is not None
        if user_input is not None:
            try:
                await self._client.async_submit_code(user_input[CONF_MFA_CODE])
            except SewAuthError:
                errors["base"] = "invalid_code"
            except SewConnectionError:
                errors["base"] = "cannot_connect"
            except SewError:
                _LOGGER.exception("Portal did not accept the code")
                errors["base"] = "unknown"
            else:
                return await self._async_finish()
        return self.async_show_form(step_id="mfa_code", data_schema=STEP_CODE_SCHEMA, errors=errors)  # type: ignore[arg-type]

    async def async_step_property(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which property to read when the login holds more than one with a digital meter."""
        if user_input is not None:
            return await self._async_use_property(self._properties[int(user_input[CONF_PROPERTY])])
        # Option values are list positions, so no record ID reaches the browser.
        options = [
            SelectOptionDict(value=str(index), label=prop.address or f"Property {index + 1}")
            for index, prop in enumerate(self._properties)
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_PROPERTY): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN, sort=False)
                )
            }
        )
        return self.async_show_form(step_id="property", data_schema=schema)  # type: ignore[arg-type]

    async def async_step_history(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Optionally take the meter's installation date; the history is imported after setup."""
        errors: dict[str, str] = {}
        assert self._entry_data is not None and self._username is not None
        if user_input is not None:
            data = dict(self._entry_data)
            if raw := user_input.get(CONF_IMPORT_FROM):
                if (start := dt_util.parse_date(raw)) is None:
                    errors[CONF_IMPORT_FROM] = "invalid_date"
                elif start >= dt_util.now().date():
                    errors[CONF_IMPORT_FROM] = "start_in_future"
                else:
                    data[CONF_IMPORT_FROM] = start.isoformat()
            if not errors:
                return self.async_create_entry(title=ENTRY_TITLE, data=data)
        return self.async_show_form(step_id="history", data_schema=STEP_HISTORY_SCHEMA, errors=errors)  # type: ignore[arg-type]

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign in again with possibly changed credentials and replace the stored session."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            if not _EMAIL_RE.match(self._username):
                errors[CONF_USERNAME] = "invalid_email"
            elif (result := await self._async_login(errors)) is not None:
                return result
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): USERNAME_SELECTOR,
                vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)  # type: ignore[arg-type]

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after the stored session expired."""
        self._username = entry_data[CONF_USERNAME]
        self._password = entry_data[CONF_PASSWORD]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Explain that a new code is needed, then log in and continue to the channel step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if (result := await self._async_login(errors)) is not None:
                return result
            if errors.get("base") == "invalid_auth":
                # The stored password no longer works; let the user enter a new one.
                return await self.async_step_reauth_password()
        assert self._username is not None
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),  # type: ignore[arg-type]
            description_placeholders={CONF_USERNAME: mask_email(self._username)},
            errors=errors,
        )

    async def async_step_reauth_password(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect a replacement password during reauthentication."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            if (result := await self._async_login(errors)) is not None:
                return result
        schema = vol.Schema(
            {
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                )
            }
        )
        assert self._username is not None
        return self.async_show_form(
            step_id="reauth_password",
            data_schema=schema,  # type: ignore[arg-type]
            description_placeholders={CONF_USERNAME: mask_email(self._username)},
            errors=errors,
        )


class SewOptionsFlow(OptionsFlowWithReload):
    """Let the user change the poll interval and time; the entry reloads automatically on save."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and save the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_POLL_TIME: user_input[CONF_POLL_TIME],
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                }
            )
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        step=1,
                        unit_of_measurement=UnitOfTime.MINUTES,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_POLL_TIME, default=options.get(CONF_POLL_TIME, DEFAULT_POLL_TIME)): TimeSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)  # type: ignore[arg-type]
