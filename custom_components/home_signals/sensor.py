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
    ATTR_APPLIANCE,
    ATTR_HOURS,
    ATTR_ITEM_ID,
    ATTR_LOAD_ID,
    ATTR_SOURCE,
    CONF_DRYER_DOOR,
    CONF_DRYER_ENERGY,
    CONF_DRYER_PLUG,
    CONF_DRYER_POWER,
    CONF_IDLE_MINUTES,
    CONF_IDLE_WATTS,
    CONF_MIN_KWH,
    CONF_MIN_MINUTES,
    CONF_START_WATTS,
    CONF_WASHER_DOOR,
    CONF_WASHER_ENERGY,
    CONF_WASHER_LEAK,
    CONF_WASHER_PLUG,
    CONF_WASHER_POWER,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_IDLE_WATTS,
    DEFAULT_MIN_KWH,
    DEFAULT_MIN_MINUTES,
    DEFAULT_START_WATTS,
    SERVICE_LAUNDRY_HUNG,
    SOURCE_BUTTON,
    SOURCE_UI,
    CONF_DONE_LISTS,
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
from .appliance import (
    AppliancePressSensor,
    ApplianceCycleSensor,
    CleaningStatusSensor,
)
from .derived import NeedsYouSensor, SecurityStatusSensor, SystemHealthSensor
from .todo_done import TodoDoneTodaySensor

LOGGER = logging.getLogger(__name__)

# States that mean "no reading", not "something happened".
_IGNORED = {STATE_UNKNOWN, STATE_UNAVAILABLE, None}

_MOTION_CLASSES = {
    BinarySensorDeviceClass.MOTION,
    BinarySensorDeviceClass.OCCUPANCY,
    BinarySensorDeviceClass.PRESENCE,
}
# The kinds an entity is allowed to claim for itself. A closed set, because
# the frontend draws an icon from it: an unknown kind would silently render
# as nothing.
_KINDS = {KIND_BUTTON, KIND_LOCK, KIND_MOTION, KIND_DOOR, KIND_OTHER}

_DOOR_CLASSES = {
    BinarySensorDeviceClass.DOOR,
    BinarySensorDeviceClass.GARAGE_DOOR,
    BinarySensorDeviceClass.OPENING,
    BinarySensorDeviceClass.WINDOW,
}


def _appliance_specs(entry: ConfigEntry) -> list[dict[str, Any]]:
    """The appliances that have been given a power sensor, and only those.

    Two slots rather than an open-ended list because the config flow has no
    way to draw a repeating record, and because two is the real number. The
    code below never counts them, so a third is a schema entry rather than a
    rewrite.
    """

    def option(key: str, default: Any = None) -> Any:
        return entry.options.get(key, entry.data.get(key, default))

    shared = {
        "start_watts": option(CONF_START_WATTS, DEFAULT_START_WATTS),
        "idle_watts": option(CONF_IDLE_WATTS, DEFAULT_IDLE_WATTS),
        "idle_minutes": option(CONF_IDLE_MINUTES, DEFAULT_IDLE_MINUTES),
        "min_minutes": option(CONF_MIN_MINUTES, DEFAULT_MIN_MINUTES),
        "min_kwh": option(CONF_MIN_KWH, DEFAULT_MIN_KWH),
    }

    candidates = [
        {
            "slug": "washing_machine",
            "name": "Washing machine",
            "power_sensor": option(CONF_WASHER_POWER),
            "plug": option(CONF_WASHER_PLUG),
            "door": option(CONF_WASHER_DOOR),
            "leak": option(CONF_WASHER_LEAK),
            "energy_sensor": option(CONF_WASHER_ENERGY),
            "icon": "mdi:washing-machine",
            # Washing comes out of here wet and has to be hung somewhere
            # else, which the machine cannot watch happen -- so a finished
            # load becomes a standing job and waits to be told it is done.
            "queues_loads": True,
            # The phase bands were measured on THIS machine, off one
            # wash. They are not a general fact about appliances, so
            # they are switched on where they were measured and nowhere
            # else. See PHASE_BANDS in appliance.py.
            "tracks_phases": True,
            **shared,
        },
        {
            "slug": "tumble_dryer",
            "name": "Tumble dryer",
            "power_sensor": option(CONF_DRYER_POWER),
            "plug": option(CONF_DRYER_PLUG),
            "door": option(CONF_DRYER_DOOR),
            "leak": None,
            "energy_sensor": option(CONF_DRYER_ENERGY),
            "icon": "mdi:tumble-dryer",
            # A dry load is finished the moment it leaves the drum, and
            # leaving the drum is opening the door -- which this can see.
            # So there is nothing to queue and nothing to press.
            "queues_loads": False,
            # No phase strip. A dryer does not fill and does not spin,
            # and its heat runs at a different power to the washer's,
            # so the washer's bands would label every dryer cycle
            # confidently and wrongly. It needs its own trace first.
            "tracks_phases": False,
            **shared,
        },
    ]
    return [c for c in candidates if c["power_sensor"]]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the derived signal sensors."""
    needs_you = NeedsYouSensor(entry)
    entities: list[SensorEntity] = [
        ActivityFeedSensor(entry),
        needs_you,
        SystemHealthSensor(entry),
        SecurityStatusSensor(entry),
    ]

    specs = _appliance_specs(entry)
    cycles = [ApplianceCycleSensor(entry, spec) for spec in specs]
    # A press sensor exists to carry a wall button into the activity feed,
    # and the button exists to clear the hang queue. An appliance with no
    # queue has no button, and inventing the entity anyway would leave a
    # timestamp that never moves looking like a flat battery.
    presses = [
        AppliancePressSensor(entry, spec)
        for spec in specs
        if spec.get("queues_loads", True)
    ]
    if cycles:
        for cycle in cycles:
            cycle.add_listener(needs_you)
        entities.extend(cycles)
        entities.extend(presses)
        entities.append(CleaningStatusSensor(entry, cycles))

    entities.extend(_done_today_sensors(hass, entry))

    async_add_entities(entities)
    _async_register_services(hass, needs_you, cycles, presses)


def _done_today_sensors(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[SensorEntity]:
    """One "done today" sensor per list that was asked for.

    Named from the list's friendly name so the entity id reads
    `sensor.phoenix_done_today` rather than carrying the to-do entity's
    own id twice over.
    """
    lists = entry.options.get(CONF_DONE_LISTS, entry.data.get(CONF_DONE_LISTS, []))
    sensors: list[SensorEntity] = []
    for list_entity in lists:
        if not isinstance(list_entity, str) or not list_entity:
            continue
        state = hass.states.get(list_entity)
        name = list_entity.split(".", 1)[-1].replace("_", " ")
        if state is not None:
            friendly = state.attributes.get(ATTR_FRIENDLY_NAME)
            if isinstance(friendly, str) and friendly.strip():
                name = friendly.strip()
        sensors.append(TodoDoneTodaySensor(entry, list_entity, name))
    return sensors


@callback
def _async_register_services(
    hass: HomeAssistant,
    needs_you: NeedsYouSensor,
    cycles: list[ApplianceCycleSensor] | None = None,
    presses: list[AppliancePressSensor] | None = None,
) -> None:
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

    if not cycles:
        return

    @callback
    def _laundry_hung(call: ServiceCall) -> None:
        """One load is up. Called by the wall button and by the Needs you row.

        A press of the WALL button is recorded whether or not it cleared
        anything. A button that does nothing when there is nothing to do is
        correct, but it should still be visible in the feed as somebody
        having pressed it — otherwise a flat battery looks exactly like an
        empty list.

        A tap on a screen is not recorded at all. The activity feed answers
        "where are people in the house", and the press sensor is how a
        kitchen button reaches it; stamping it from the UI put "Kitchen ·
        button" in the feed when somebody cleared the row on the panel — and
        would have done the same for a phone on a train. The wall button is
        the only caller whose location is known.
        """
        wanted = call.data.get(ATTR_APPLIANCE)
        load_id = call.data.get(ATTR_LOAD_ID)
        targets = [c for c in cycles if wanted in (None, c.slug)]
        if load_id is not None:
            # An id names exactly one load on exactly one machine, so the
            # appliance argument is redundant and the id wins.
            targets = cycles

        for cycle in targets:
            if cycle.hung(load_id):
                break

        if call.data.get(ATTR_SOURCE) == SOURCE_BUTTON:
            for press in presses or []:
                if wanted in (None, press.slug):
                    press.record()

        needs_you.refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_LAUNDRY_HUNG, _laundry_hung,
        schema=vol.Schema({
            vol.Optional(ATTR_LOAD_ID): cv.string,
            vol.Optional(ATTR_APPLIANCE): cv.string,
            # Defaults to `ui`, which is the safe direction: a caller that
            # forgets to say where it is loses a row from the feed, where
            # the opposite default invents a person standing in the kitchen.
            vol.Optional(ATTR_SOURCE, default=SOURCE_UI):
                vol.In([SOURCE_BUTTON, SOURCE_UI]),
        }),
    )


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
        kind = self._kind(
            entity_id,
            new_state.attributes.get(ATTR_DEVICE_CLASS),
            new_state.attributes.get("kind"),
        )

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
    def _kind(
        entity_id: str, device_class: str | None, declared: str | None = None
    ) -> str:
        """Classify an event by what it proves, not by what fired it.

        The rail's icon is the kind, and a button press is the interesting one
        because it proves a person rather than a cat.

        An entity that declares its own `kind` is believed. A timestamp
        sensor is the case that needs it: by domain and device class it
        says only *when* something happened, so without this the feed
        would have to guess from the entity id — and "ends in _button" is
        the kind of guess that works until somebody renames something.
        """
        if declared in _KINDS:
            return declared
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
