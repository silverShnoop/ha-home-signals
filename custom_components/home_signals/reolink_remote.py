"""A Reolink camera's remote access, as a switch.

Every Reolink camera has a UID, and while the UID is enabled the camera
keeps an outbound connection to Reolink's servers so that the Reolink app
can reach it from anywhere, with nothing opened on the router. That is
convenient, and it is also the one thing about a bedroom camera a person
might reasonably want off. The phone app offers no control for it; the
desktop Client does; and Home Assistant's own Reolink integration reads the
UID but never writes the setting.

The camera's API has the command, so this sends it. It goes through the
connection the core integration already holds rather than opening a second
session, because a Reolink allows only a few sessions at once and a second
login has been known to knock the first one out -- which would take the
camera off the panel to flip a switch about the camera.

Turning it off also stops the Reolink app's push notifications, which ride
the same channel. Home Assistant talks to the camera on the LAN and is not
affected, and neither is the camera's time sync or firmware check, which
reach Reolink's servers on their own.

The switch is read back from the camera on a schedule, so a change made in
the Client shows here within a few minutes, and it is unavailable whenever
the core integration is not connected -- there is nothing to flip then.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

LOGGER = logging.getLogger(__name__)

REOLINK_DOMAIN = "reolink"

try:
    from reolink_aio.exceptions import ReolinkError
except ImportError:  # pragma: no cover - the library ships with Home Assistant

    class ReolinkError(Exception):  # type: ignore[no-redef]
        """Stand-in where reolink_aio is not installed, such as the tests."""


_GET = [{"cmd": "GetP2p", "action": 0, "param": {}}]


def _api(camera: ConfigEntry) -> Any | None:
    """The core integration's live API object for a camera, or None.

    `runtime_data` is only set while the entry is loaded, and is replaced on
    every reload, so it is looked up each time rather than kept.
    """
    if camera.state is not ConfigEntryState.LOADED:
        return None
    host = getattr(getattr(camera, "runtime_data", None), "host", None)
    return getattr(host, "api", None)


def reolink_remote_access_switches(
    hass: HomeAssistant,
) -> list[ReolinkRemoteAccessSwitch]:
    """One switch per configured Reolink host.

    Every host, not just the loaded ones: a camera that is still connecting
    when this integration starts gets a switch that is unavailable until the
    first read succeeds, rather than no switch until the next restart.
    """
    return [
        ReolinkRemoteAccessSwitch(camera)
        for camera in hass.config_entries.async_entries(REOLINK_DOMAIN)
        if camera.disabled_by is None
    ]


class ReolinkRemoteAccessSwitch(SwitchEntity):
    """Whether a Reolink camera can be reached through Reolink's servers."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cloud-lock-outline"
    _attr_should_poll = True

    def __init__(self, camera: ConfigEntry) -> None:
        self._camera = camera
        self._attr_unique_id = f"reolink_remote_access_{camera.entry_id}"
        self._attr_name = f"{camera.title} remote access"
        self._attr_available = False
        self._attr_is_on = None

    async def async_update(self) -> None:
        """Read the setting back from the camera."""
        api = _api(self._camera)
        if api is None:
            self._attr_available = False
            return
        try:
            response = await api.send(_GET)
            enabled = response[0]["value"]["P2p"]["enable"]
        except (ReolinkError, LookupError, TypeError) as err:
            LOGGER.debug("%s: could not read remote access: %s", self._camera.title, err)
            self._attr_available = False
            return
        self._attr_available = True
        self._attr_is_on = bool(enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, enabled: bool) -> None:
        api = _api(self._camera)
        if api is None:
            raise HomeAssistantError(f"{self._camera.title} is not connected")
        body = [
            {"cmd": "SetP2p", "action": 0, "param": {"P2p": {"enable": int(enabled)}}}
        ]
        try:
            response = await api.send(body, {"cmd": "SetP2p"})
        except ReolinkError as err:
            raise HomeAssistantError(
                f"Could not change remote access on {self._camera.title}: {err}"
            ) from err
        # The camera answers a rejected setting with a normal reply carrying
        # an error code, not with an HTTP error, so the reply has to be read.
        first = response[0] if response else {}
        if first.get("code") != 0 or first.get("value", {}).get("rspCode") != 200:
            raise HomeAssistantError(
                f"{self._camera.title} refused the remote access change: {first}"
            )
        self._attr_available = True
        self._attr_is_on = enabled
        self.async_write_ha_state()
