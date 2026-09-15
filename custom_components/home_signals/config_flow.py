"""Config flow for Home Signals."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_ENTITIES, CONF_MAX_EVENTS, DEFAULT_MAX_EVENTS, DOMAIN

TITLE = "Home Signals"

# Anything that can mark a moment: motion and door contacts, locks, and the
# button/remote events that prove a person rather than a pet.
_TRACKABLE_DOMAINS = ["binary_sensor", "event", "lock", "device_tracker"]


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_ENTITIES, default=defaults.get(CONF_ENTITIES, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=_TRACKABLE_DOMAINS, multiple=True
                )
            ),
            vol.Optional(
                CONF_MAX_EVENTS,
                default=defaults.get(CONF_MAX_EVENTS, DEFAULT_MAX_EVENTS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5, max=100, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
    )


class HomeSignalsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Home Signals config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the entities the feed should watch."""
        if user_input is not None:
            return self.async_create_entry(title=TITLE, data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HomeSignalsOptionsFlow:
        """Return the options flow."""
        return HomeSignalsOptionsFlow()


class HomeSignalsOptionsFlow(OptionsFlow):
    """Let the tracked entity list be edited after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
