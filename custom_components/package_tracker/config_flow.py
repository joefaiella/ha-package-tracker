"""Config flow: Gmail address + app password; options; reauth."""

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util.ssl import client_context
import probatio

from . import imap_client
from .address import clean_entries
from .const import (
    CONF_ADDRESSES,
    CONF_INFORMED_DELIVERY,
    CONF_KEEP_DELIVERED_DAYS,
    CONF_LOOKBACK_DAYS,
    CONF_SCAN_MINUTES,
    CONF_SHOW_UNKNOWN,
    DEFAULT_KEEP_DELIVERED_DAYS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    MAX_ADDRESSES,
)

_LOGGER = logging.getLogger(__name__)

_PASSWORD = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        probatio.Required(CONF_PASSWORD): _PASSWORD,
    }
)
REAUTH_SCHEMA = probatio.Schema({probatio.Required(CONF_PASSWORD): _PASSWORD})


def _num(lo: int, hi: int, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=lo, max=hi, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement=unit
        )
    )


OPTIONS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_SCAN_MINUTES, default=DEFAULT_SCAN_MINUTES): _num(5, 120, "min"),
        probatio.Required(CONF_LOOKBACK_DAYS, default=DEFAULT_LOOKBACK_DAYS): _num(1, 30, "d"),
        probatio.Required(CONF_KEEP_DELIVERED_DAYS, default=DEFAULT_KEEP_DELIVERED_DAYS): _num(
            1, 14, "d"
        ),
        # No default: an emptied list must stay empty (the form then leaves the key out).
        probatio.Optional(CONF_ADDRESSES): TextSelector(TextSelectorConfig(multiple=True)),
        probatio.Required(CONF_SHOW_UNKNOWN, default=True): BooleanSelector(),
        probatio.Required(CONF_INFORMED_DELIVERY, default=True): BooleanSelector(),
    }
)
_NUMBERS = (CONF_SCAN_MINUTES, CONF_LOOKBACK_DAYS, CONF_KEEP_DELIVERED_DAYS)


def _norm_email(value: str) -> str:
    return value.strip().lower()


def _norm_password(value: str) -> str:
    return "".join(value.split())  # Google shows app passwords in groups of four


async def _validate(email: str, password: str) -> str | None:
    try:
        async with asyncio.timeout(60):
            await imap_client.check_login(email, password, client_context())
    except imap_client.GmailAuthError:
        return "invalid_auth"
    except imap_client.GmailError as err:
        return "not_gmail" if str(err) == "not_gmail" else "cannot_connect"
    except (TimeoutError, OSError):
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error checking the Gmail login")
        return "unknown"
    return None


class PackageTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = _norm_email(user_input[CONF_EMAIL])
            password = _norm_password(user_input[CONF_PASSWORD])
            await self.async_set_unique_id(email)
            self._abort_if_unique_id_configured()
            if (error := await _validate(email, password)) is None:
                return self.async_create_entry(
                    title=email, data={CONF_EMAIL: email, CONF_PASSWORD: password}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_EMAIL: user_input[CONF_EMAIL]} if user_input else {}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            password = _norm_password(user_input[CONF_PASSWORD])
            if (error := await _validate(entry.data[CONF_EMAIL], password)) is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: password}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "PackageTrackerOptionsFlow":
        return PackageTrackerOptionsFlow()


class PackageTrackerOptionsFlow(OptionsFlowWithReload):
    """Reloads the entry when options change."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"entry": ""}
        if user_input is not None:
            addresses, error, bad = clean_entries(user_input.get(CONF_ADDRESSES), MAX_ADDRESSES)
            if error is None:
                return self.async_create_entry(
                    data={
                        **{k: int(user_input[k]) for k in _NUMBERS},
                        CONF_ADDRESSES: addresses,  # always written: [] means "show everything"
                        CONF_SHOW_UNKNOWN: bool(user_input.get(CONF_SHOW_UNKNOWN, True)),
                        CONF_INFORMED_DELIVERY: bool(user_input.get(CONF_INFORMED_DELIVERY, True)),
                    }
                )
            errors[CONF_ADDRESSES], placeholders["entry"] = error, bad
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, user_input if user_input is not None else self.config_entry.options
            ),
            errors=errors,
            description_placeholders=placeholders,
        )
