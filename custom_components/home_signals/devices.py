"""How many of the house's devices are answering, and which are not.

Counting *entities* that have gone away is the wrong number for a person
standing in a room: a car that loses its cloud connection is eighteen
entities and one car. This counts devices, and says for each one that is
not answering where it is, what is missing, and since when.

"Since when" is the part Home Assistant cannot answer by itself. Every
state's `last_changed` is reset by a restart, so on this Green a bulb dead
for a week reads as having died when the house last rebooted. The first
time this sees a device go quiet it writes the time down and restores it
across restarts, the same way `Needs you` remembers when a phone went dark.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import LEVEL_ATTENTION
from .derived import _NOISY_DOMAINS, _Derived

# The networks worth a bar of their own, in the order the card draws them.
# Everything else talks to Home Assistant over the house Wi-Fi or a vendor's
# cloud, and one bar for all of it says as much as a bar each would.
NETWORKS = {"hue": "Hue", "zha": "Zigbee", "tado": "Tado", "cast": "Cast"}
OTHER_NETWORK = "Wi-Fi & cloud"
NETWORK_ORDER = [*NETWORKS.values(), OTHER_NETWORK]

# Devices in the registry that are not things. A Hue room or zone is a
# group of bulbs the bridge happens to describe as a device, and a Cast
# group is a set of speakers; counting either would report one dead bulb
# three times, or a speaker group as offline because one speaker is.
#
# Room and zone are only skipped for Hue. Tado also calls each room a
# "Zone", but a Tado zone is the room's heating control: when it goes
# unavailable nothing else on the card says so, and skipping it hid the
# Gym's heating being uncontrollable.
_GROUP_WORD = "group"
_HUE_GROUP_WORDS = ("room", "zone")

OFFLINE = "offline"
PARTIAL = "partial"


def _is_group(device: dr.DeviceEntry) -> bool:
    words = (device.model or "").lower().split()
    if _GROUP_WORD in words:
        return True
    return _network_of(device) == NETWORKS["hue"] and any(
        word in words for word in _HUE_GROUP_WORDS
    )


def _network_of(device: dr.DeviceEntry) -> str:
    # Only the first element of an identifier is promised. Some
    # integrations register three-part identifiers, and unpacking two
    # took the whole sensor down on the real house.
    for identifier in device.identifiers:
        if identifier and identifier[0] in NETWORKS:
            return NETWORKS[identifier[0]]
    return OTHER_NETWORK


def _detail(
    missing: list[er.RegistryEntry],
    members: list[tuple[er.RegistryEntry, bool]],
) -> str:
    """What a partly-answering device has stopped reporting.

    One missing reading is named, because "No temperature" tells you
    what to check; several are counted, because a list of seven entity
    names on a wall panel tells you nothing.
    """
    if len(missing) == 1:
        label = missing[0].name or missing[0].original_name
        if label:
            return f"No {label.lower()}"
        return "1 reading missing"
    return f"{len(missing)} of {len(members)} missing"


def scan_devices(
    hass: HomeAssistant, ignored: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every device not fully answering, and a tally per network.

    One function because two things report it: the Devices card and the
    offline row in Needs you. They used to count differently -- the row
    in entities, the card in devices -- and said "31 offline" beside "7
    offline" about the same house on the same panel.
    """
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    areas = ar.async_get(hass)

    # Every entity that says something about its device answering:
    # not a button or an update, and not one the house has chosen to
    # stop hearing about. Diagnostics are kept apart rather than
    # dropped. A bulb's signal-strength reading going quiet is not the
    # bulb going quiet, so where a device has ordinary entities they
    # are what decide. But a ZHA button has none at all -- its presses
    # are events, never entities -- and its battery and signal readings
    # are the only way to know it is still there. Dropping them took a
    # button the house uses every day out of the count entirely.
    primary: dict[str, list[tuple[er.RegistryEntry, bool]]] = {}
    diagnostic: dict[str, list[tuple[er.RegistryEntry, bool]]] = {}
    for entry in entities.entities.values():
        if entry.device_id is None or entry.disabled_by is not None:
            continue
        if entry.domain in _NOISY_DOMAINS or entry.entity_id in ignored:
            continue
        if (state := hass.states.get(entry.entity_id)) is None:
            continue
        bucket = diagnostic if entry.entity_category is not None else primary
        bucket.setdefault(entry.device_id, []).append(
            (entry, state.state == STATE_UNAVAILABLE)
        )
    by_device = {**diagnostic, **primary}

    tally = {name: {"name": name, "online": 0, OFFLINE: 0, PARTIAL: 0}
             for name in NETWORK_ORDER}
    problems: list[dict[str, Any]] = []
    for device_id, members in by_device.items():
        device = devices.async_get(device_id)
        if device is None or device.entry_type is dr.DeviceEntryType.SERVICE:
            continue
        if _is_group(device):
            continue
        network = _network_of(device)
        missing = [entry for entry, gone in members if gone]
        if not missing:
            tally[network]["online"] += 1
            continue
        status = OFFLINE if len(missing) == len(members) else PARTIAL
        tally[network][status] += 1
        area = areas.async_get_area(device.area_id) if device.area_id else None
        problems.append({
            "device_id": device_id,
            "name": device.name_by_user or device.name or device_id,
            "area": area.name if area else None,
            "network": network,
            "state": status,
            "detail": None if status == OFFLINE else _detail(missing, members),
        })

    problems.sort(key=lambda p: ((p["area"] or "~").lower(), p["name"].lower()))
    networks = [
        tally[name] for name in NETWORK_ORDER
        if tally[name]["online"] or tally[name][OFFLINE] or tally[name][PARTIAL]
    ]
    return problems, networks


class DevicesSensor(_Derived, RestoreEntity):
    """Devices connected, offline and partly offline, by room and network."""

    _attr_name = "Devices"
    _attr_icon = "mdi:lan-connect"
    _attr_native_unit_of_measurement = "devices"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_devices"
        self._counts = {"connected": 0, OFFLINE: 0, PARTIAL: 0}
        self._networks: list[dict[str, Any]] = []
        # device_id -> when it stopped answering, or None when it was
        # already quiet the first time this looked and the real time is
        # unknowable. None is kept rather than guessed: "since the last
        # restart" is exactly the false answer this exists to replace.
        self._since: dict[str, datetime | None] = {}
        # Whether the first real scan has happened. Until it has, every
        # quiet device predates this sensor and gets None, not "now".
        self._baselined = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is None:
            return
        restored = last.attributes.get("since")
        if isinstance(restored, dict):
            self._baselined = True
            for device_id, when in restored.items():
                self._since[device_id] = dt_util.parse_datetime(when) if when else None
        self._recompute()

    def _recompute(self) -> None:
        hass = self.hass
        problems, self._networks = scan_devices(hass, self._ignored())

        for problem in problems:
            device_id = problem["device_id"]
            if device_id not in self._since:
                self._since[device_id] = (
                    dt_util.utcnow() if self._baselined and hass.is_running else None
                )
            since = self._since[device_id]
            problem["since"] = since.isoformat() if since else None

        # Only once Home Assistant is running do absences mean anything:
        # during startup half the house is briefly unavailable, and a
        # device that recovers is forgotten, so a clock started then would
        # simply be thrown away -- but the baseline must not be taken then.
        if hass.is_running:
            seen = {p["device_id"] for p in problems}
            for device_id in list(self._since):
                if device_id not in seen:
                    del self._since[device_id]
            self._baselined = True

        self._items = problems
        offline = sum(1 for p in problems if p["state"] == OFFLINE)
        self._counts = {
            "connected": sum(n["online"] for n in self._networks),
            OFFLINE: offline,
            PARTIAL: len(problems) - offline,
        }

    @staticmethod
    def _network_of(device: dr.DeviceEntry) -> str:
        return _network_of(device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **self._counts,
            "total": sum(self._counts.values()),
            "problems": list(self._items),
            "networks": list(self._networks),
            # A device that has stopped answering is already a Needs you
            # row (the offline row), so the card may wear its level.
            "level": LEVEL_ATTENTION if self._items else None,
            "since": {
                device_id: when.isoformat() if when else None
                for device_id, when in self._since.items()
            },
        }
