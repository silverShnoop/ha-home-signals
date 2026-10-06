"""When each camera last saw something, held so a restart cannot reset it.

A camera's detection sensors are binary sensors, and their `last_changed`
is the last time they changed state for any reason -- not the last time
they saw anything. A Reolink in privacy mode makes every one of them
unavailable, and opening the lens brings them back as `off`: each of those
is a change, so a card reading `last_changed` said "Crying 5s ago" in a
house where nobody had cried at all. A restart and an integration reload
do the same.

So this writes down the two moments that are actually news -- a detection
starting, and a detection ending -- and nothing else. A sensor going
unavailable while it was seeing something ends the sighting there; a
sensor coming back from unavailable as `off` is not a sighting of anything.
The record is restored across restarts, the same way `people_status` keeps
when somebody arrived.

Which sensors count is not configured. A detection is a binary sensor on
a device that also has a camera, which is true of a Reolink today and of
Frigate's object sensors later, without anybody listing them.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    KIND_ANIMAL,
    KIND_CAMERA,
    KIND_CRYING,
    KIND_MOTION,
    KIND_PERSON,
    KIND_VEHICLE,
)

LOGGER = logging.getLogger(__name__)

# Words in a detection's name, and the kind each one means. First match
# wins, so crying is found before a "baby" could be taken for a person.
# Reolink names them "Person", "Animal", "Pet", "Vehicle", "Baby crying";
# Frigate's are "<camera> Person occupancy", "<camera> Dog occupancy".
_KIND_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("crying", "cry"), KIND_CRYING),
    (("person", "people", "face"), KIND_PERSON),
    (("animal", "pet", "dog", "cat", "bird"), KIND_ANIMAL),
    (("vehicle", "car", "bicycle", "motorcycle"), KIND_VEHICLE),
    (("motion",), KIND_MOTION),
)

_NO_READING = {STATE_UNKNOWN, STATE_UNAVAILABLE}


# Device classes that are something a camera saw, for a detection whose
# name says nothing we know.
_SEEING_CLASSES = {"motion", "occupancy", "presence", "sound"}


def _is_detection(entry: er.RegistryEntry) -> bool:
    """Whether a binary sensor on a camera's device is something it saw.

    Not every binary sensor on such a device is. A wall tablet running a
    kiosk app shows up with a camera of its own, and its charging and
    connectivity sensors were being filed as sightings -- "Hall · camera"
    every time the panel was plugged in.
    """
    if camera_kind(entry) != KIND_CAMERA:
        return True
    return (entry.device_class or entry.original_device_class) in _SEEING_CLASSES


def camera_detections(registry: er.EntityRegistry) -> list[str]:
    """Every binary sensor that shares a device with a camera and is
    something the camera saw."""
    cameras = {
        entry.device_id
        for entry in registry.entities.values()
        if entry.domain == "camera" and entry.device_id and not entry.disabled
    }
    return sorted(
        entry.entity_id
        for entry in registry.entities.values()
        if entry.domain == "binary_sensor"
        and entry.device_id in cameras
        and not entry.disabled
        and _is_detection(entry)
    )


def camera_kind(entry: er.RegistryEntry) -> str:
    """What a camera's detection sensor detects, as an activity kind."""
    text = " ".join(
        str(part) for part in (
            entry.translation_key, entry.original_name, entry.name, entry.entity_id,
        ) if part
    ).lower()
    words = set(text.replace(".", " ").replace("_", " ").split())
    for keys, kind in _KIND_WORDS:
        if words.intersection(keys):
            return kind
    return KIND_CAMERA


class CameraSightingsSensor(SensorEntity, RestoreEntity):
    """The camera card's sensor: what each camera last saw, and when.

    The state is when any camera last saw anything. `sightings` maps each
    detection sensor to `{"on", "since", "started"}`: `since` is when it
    last saw something -- the moment the sighting ended, or the moment it
    began while it is still going on.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Camera sightings"
    _attr_icon = "mdi:cctv"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_camera_sightings"
        self._sightings: dict[str, dict[str, Any]] = {}
        self._last: datetime | None = None
        self._watched: list[str] = []
        self._unwatch: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state not in _NO_READING:
                self._last = dt_util.parse_datetime(last.state)
            held = last.attributes.get("sightings")
            if isinstance(held, dict):
                for entity_id, row in held.items():
                    restored = self._restore_row(row)
                    if restored is not None:
                        self._sightings[str(entity_id)] = restored
        # Watched once Home Assistant has started, so the cameras' own
        # entities are in the registry; and again whenever the registry
        # changes, so a new camera is picked up without a restart.
        self.async_on_remove(async_at_started(self.hass, self._async_watch))
        self.async_on_remove(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_registry_changed
            )
        )
        self.async_on_remove(self._stop_watching)

    @staticmethod
    def _restore_row(row: Any) -> dict[str, Any] | None:
        if not isinstance(row, dict):
            return None
        since = dt_util.parse_datetime(row["since"]) if row.get("since") else None
        if since is None:
            return None
        started = dt_util.parse_datetime(row["started"]) if row.get("started") else None
        return {"on": bool(row.get("on")), "since": since, "started": started or since}

    @callback
    def _stop_watching(self) -> None:
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None

    @callback
    def _async_registry_changed(self, _event: Event) -> None:
        if self.hass.is_running:
            self._async_watch(self.hass)

    @callback
    def _async_watch(self, _hass: Any = None) -> None:
        watched = camera_detections(er.async_get(self.hass))
        if watched == self._watched and self._unwatch is not None:
            return
        self._stop_watching()
        self._watched = watched
        # A sensor that is no longer a detection takes its record with it.
        for entity_id in [e for e in self._sightings if e not in watched]:
            del self._sightings[entity_id]
        if watched:
            self._unwatch = async_track_state_change_event(
                self.hass, watched, self._async_changed
            )
        # A detection that is on right now was on before anybody was
        # listening; one that was on in the record and is not any more
        # ended while Home Assistant was not looking.
        now = dt_util.utcnow()
        for entity_id in watched:
            state = self.hass.states.get(entity_id)
            seeing = state is not None and state.state == STATE_ON
            self._note(entity_id, seeing, now)
        self.async_write_ha_state()

    @callback
    def _async_changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None:
            return
        if self._note(new.entity_id, new.state == STATE_ON, dt_util.utcnow()):
            self.async_write_ha_state()

    def _note(self, entity_id: str, seeing: bool, now: datetime) -> bool:
        """Record a sighting starting or ending. True if anything changed.

        Only the edges count. Not seeing, after not seeing, is nothing --
        however the sensor got there, through unavailable or a reload --
        and that is the whole point of this sensor.
        """
        row = self._sightings.get(entity_id)
        was = bool(row and row["on"])
        if seeing == was:
            return False
        if seeing:
            self._sightings[entity_id] = {"on": True, "since": now, "started": now}
        else:
            # Ending: going off, or going unavailable mid-sighting. Either
            # way this is the last moment it was seeing something.
            self._sightings[entity_id] = {**row, "on": False, "since": now}
        self._last = now
        return True

    @property
    def native_value(self) -> datetime | None:
        return self._last

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "sightings": {
                entity_id: {
                    "on": row["on"],
                    "since": row["since"].isoformat(),
                    "started": row["started"].isoformat(),
                }
                for entity_id, row in sorted(self._sightings.items())
            },
        }
