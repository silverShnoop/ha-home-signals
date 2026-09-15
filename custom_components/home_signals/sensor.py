"""Derived signals about the house.

`sensor.activity_feed` merges motion, door, lock and button events from across
the house into one feed. It is deliberately a single entity with a list
attribute rather than one entity per source: the thing being modelled is "what
has been happening", which is a sequence, and a rail reading twenty entities
cannot sort them.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_HOURS,
    ATTR_ITEM_ID,
    CONF_ENTITIES,
    CONF_MAX_EVENTS,
    DEFAULT_MAX_EVENTS,
    KIND_BUTTON,
    KIND_DOOR,
    KIND_LOCK,
    KIND_MOTION,
    KIND_OTHER,
    DOMAIN,
    SERVICE_DISMISS,
    SERVICE_RESET,
    SERVICE_SNOOZE,
)
from .derived import NeedsYouSensor, SystemHealthSensor

LOGGER = logging.getLogger(__name__)

# States that mean "no reading", not "something happened".
_IGNORED = {STATE_UNKNOWN, STATE_UNAVAILABLE, None}

_MOTION_CLASSES = {
    BinarySensorDeviceClass.MOTION,
    BinarySensorDeviceClass.OCCUPANCY,
    BinarySensorDeviceClass.PRESENCE,
}
_DOOR_CLASSES = {
    BinarySensorDeviceClass.DOOR,
    BinarySensorDeviceClass.GARAGE_DOOR,
    BinarySensorDeviceClass.OPENING,
    BinarySensorDeviceClass.WINDOW,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the derived signal sensors."""
    needs_you = NeedsYouSensor(entry)
    async_add_entities([ActivityFeedSensor(entry), needs_you, SystemHealthSensor(entry)])
    _async_register_services(hass, needs_you)


@callback
def _async_register_services(hass: HomeAssistant, needs_you: NeedsYouSensor) -> None:
    """Dismiss and snooze, so a row can be cleared from anywhere.

    These are actions rather than card-local state on purpose: the panel, a
    phone and a wall button all have to clear the same row, and a browser
    cannot be the place that memory lives.
    """

    @callback
    def _dismiss(call: ServiceCall) -> None:
        needs_you.suppress(call.data[ATTR_ITEM_ID])

    @callback
    def _snooze(call: ServiceCall) -> None:
        needs_you.suppress(call.data[ATTR_ITEM_ID], float(call.data.get(ATTR_HOURS, 8)))

    @callback
    def _reset(_call: ServiceCall) -> None:
        needs_you.reset()

    hass.services.async_register(
        DOMAIN, SERVICE_DISMISS, _dismiss,
        schema=vol.Schema({vol.Required(ATTR_ITEM_ID): cv.string}),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SNOOZE, _snooze,
        schema=vol.Schema({
            vol.Required(ATTR_ITEM_ID): cv.string,
            vol.Optional(ATTR_HOURS, default=8): vol.Coerce(float),
        }),
    )
    hass.services.async_register(DOMAIN, SERVICE_RESET, _reset, schema=vol.Schema({}))


class ActivityFeedSensor(SensorEntity, RestoreEntity):
    """One merged, newest-first feed of things that happened in the house.

    The state is when anything last happened anywhere, which is what a rail
    header wants ("Quiet 2m"); `events` carries the rows.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Activity feed"
    _attr_icon = "mdi:timeline-clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialise the feed."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_activity_feed"
        self._events: deque[dict[str, Any]] = deque(maxlen=self._max_events)
        self._last: datetime | None = None

    @property
    def _max_events(self) -> int:
        return int(
            self._entry.options.get(
                CONF_MAX_EVENTS, self._entry.data.get(CONF_MAX_EVENTS, DEFAULT_MAX_EVENTS)
            )
        )

    @property
    def _tracked(self) -> list[str]:
        return list(
            self._entry.options.get(
                CONF_ENTITIES, self._entry.data.get(CONF_ENTITIES, [])
            )
        )

    async def async_added_to_hass(self) -> None:
        """Restore the previous feed and start listening."""
        await super().async_added_to_hass()

        # The feed is a log, so losing it on every restart would make the rail
        # useless exactly when you most want to know what happened.
        if (last := await self.async_get_last_state()) is not None:
            if last.state not in _IGNORED:
                self._last = dt_util.parse_datetime(last.state)
            restored = last.attributes.get("events")
            if isinstance(restored, list):
                self._events.extend(
                    row for row in restored[: self._max_events] if isinstance(row, dict)
                )

        tracked = self._tracked
        if not tracked:
            LOGGER.warning(
                "Home Signals has no entities configured; the activity feed will "
                "stay empty until some are selected"
            )
            return

        self.async_on_remove(
            async_track_state_change_event(self.hass, tracked, self._handle_event)
        )

    @callback
    def _handle_event(self, event: Event[EventStateChangedData]) -> None:
        """Record one state change, if it counts as activity."""
        new_state = event.data["new_state"]
        old_state = event.data["old_state"]
        if new_state is None or new_state.state in _IGNORED:
            return
        # A restart replays every entity as a fresh state with no previous one.
        # Those are not events, they are Home Assistant waking up.
        if old_state is None:
            return
        if new_state.state == old_state.state:
            return

        entity_id = new_state.entity_id
        kind = self._kind(entity_id, new_state.attributes.get(ATTR_DEVICE_CLASS))

        # Motion clearing is not an event. A button or a lock changing is.
        if kind in (KIND_MOTION, KIND_DOOR) and new_state.state != STATE_ON:
            return

        now = dt_util.utcnow()
        self._events.appendleft(
            {
                "entity_id": entity_id,
                "name": new_state.attributes.get(ATTR_FRIENDLY_NAME, entity_id),
                "area": self._area_name(entity_id),
                "kind": kind,
                "state": new_state.state,
                "at": now.isoformat(),
            }
        )
        self._last = now
        self.async_write_ha_state()

    @staticmethod
    def _kind(entity_id: str, device_class: str | None) -> str:
        """Classify an event by what it proves, not by what fired it.

        The rail's icon is the kind, and a button press is the interesting one
        because it proves a person rather than a cat.
        """
        domain = entity_id.partition(".")[0]
        if domain == "event":
            return KIND_BUTTON
        if domain == "lock":
            return KIND_LOCK
        if device_class in _MOTION_CLASSES:
            return KIND_MOTION
        if device_class in _DOOR_CLASSES:
            return KIND_DOOR
        return KIND_OTHER

    def _area_name(self, entity_id: str) -> str | None:
        """Resolve an entity's area, via its device where it has no area itself."""
        entity_registry = er.async_get(self.hass)
        if (entry := entity_registry.async_get(entity_id)) is None:
            return None
        area_id = entry.area_id
        if area_id is None and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            area_id = device.area_id if device else None
        if area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else None

    @property
    def native_value(self) -> datetime | None:
        """When anything last happened anywhere."""
        return self._last

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The feed itself, newest first."""
        return {
            "events": list(self._events),
            "tracked_count": len(self._tracked),
        }
