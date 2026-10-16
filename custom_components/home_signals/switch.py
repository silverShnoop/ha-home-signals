"""Switches.

The one place this integration changes a device rather than describing
one: a Reolink camera's remote access, which no other integration offers a
control for. See `reolink_remote` for why it exists and why it borrows the
core integration's connection.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .reolink_remote import reolink_remote_access_switches

# A read is one small request to the camera. Five minutes is quick enough
# that a camera which was still connecting at startup, or a change made in
# Reolink's own Client, shows up before anyone goes looking for it.
SCAN_INTERVAL = timedelta(minutes=5)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches."""
    async_add_entities(reolink_remote_access_switches(hass), update_before_add=True)
