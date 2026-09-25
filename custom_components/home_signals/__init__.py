"""Home Signals.

Derived, house-wide signals that no single integration owns: what has been
happening (the activity feed), what needs a human, and appliance state
machines. Everything here is computed from entities other integrations
already provide — it talks to no hardware of its own. The one exception is
writing recipes to Mealie, which the core integration cannot do.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .recipes import async_register_recipe_services

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Signals from a config entry."""
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    async_register_recipe_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the tracked entity list changes."""
    await hass.config_entries.async_reload(entry.entry_id)
