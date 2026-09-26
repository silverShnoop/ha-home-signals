"""The two derived signals that answer questions about the house as a whole.

Both exist as entities rather than as card logic for the same reason: a card
is only true while somebody is looking at it. `Needs you` has to remember a
dismissal across a restart, and both have to answer an agent asking "what
needs doing?" or "what is broken?" — and an agent never looks at a card.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    EVENT_STATE_CHANGED,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
    split_entity_id,
)
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    APPLIANCE_RUNNING,
    ATTR_HOURS,
    ATTR_ITEM_ID,
    ATTR_LOAD_ID,
    ATTR_SOURCE,
    ACCENT_INFO,
    LEVEL_ATTENTION,
    LEVEL_CRITICAL,
    LEVEL_LOUDNESS,
    LEVEL_WAITING,
    CONF_BATTERY_THRESHOLD,
    CONF_BIN_SENSOR,
    CONF_PEOPLE,
    CONF_PRESENCE_GRACE_MINUTES,
    DEFAULT_PRESENCE_GRACE_MINUTES,
    CONF_IGNORE_UNAVAILABLE,
    CONF_SALT_BOTH_THRESHOLD,
    CONF_SALT_ONE_THRESHOLD,
    CONF_SALT_SENSORS,
    CONF_SECURITY_GRACE_MINUTES,
    CONF_SECURITY_LOCKS,
    CONF_SECURITY_OPENINGS,
    CONF_TASKS_SENSOR,
    DEFAULT_BATTERY_THRESHOLD,
    DEFAULT_SALT_BOTH_THRESHOLD,
    DEFAULT_SALT_ONE_THRESHOLD,
    DEFAULT_SECURITY_GRACE_MINUTES,
    DOMAIN,
    SECURITY_AMBER,
    SECURITY_GREEN,
    SECURITY_RED,
    SERVICE_DISMISS,
    SERVICE_LAUNDRY_HUNG,
    SOURCE_UI,
    SERVICE_SNOOZE,
)

LOGGER = logging.getLogger(__name__)

_NOT_A_READING = {STATE_UNKNOWN, STATE_UNAVAILABLE, None}

# Recomputed on a timer as well as on state changes: "bin out tonight" turns
# true because the clock moved, and a snooze expires the same way.
SCAN_INTERVAL = timedelta(minutes=5)

# Past this many, a list of errands becomes one job.
BATTERY_ROWS_MAX = 2

# Diagnostic entities go unavailable constantly and nobody acts on them.
_NOISY_DOMAINS = {"update", "button", "scene", "script", "automation"}


def _area_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """An entity's area, via its device where it has none itself."""
    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get(entity_id)
    if entry is None:
        return None
    area_id = entry.area_id
    if area_id is None and entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        area_id = device.area_id if device else None
    if area_id is None:
        return None
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area else None


def _name_of(state: State) -> str:
    return state.attributes.get(ATTR_FRIENDLY_NAME, state.entity_id)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _battery_label(state: State) -> str:
    """The device's name, without the word the row is about to add.

    Most battery entities are already called "<device> Battery", and
    "Kitchen Button Battery battery" reads like a typo.
    """
    name = _name_of(state)
    for suffix in (" Battery Level", " Battery"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _salt_detail(readings: list[tuple[str, float]]) -> str:
    """ "Left 30%, Right 0%" -- the same line wherever the salt is a row."""
    return ", ".join(f"{side} {level:.0f}%" for side, level in readings)


def _salt_side(state: State) -> str:
    """Which cylinder a salt sensor is reading, for the row's detail line.

    A heuristic on purpose. "My Water Softener Salt left side percentage" is
    accurate and unreadable in a one-line row, and the only part worth
    keeping is the side. When a name says neither left nor right — a
    single-cylinder machine, or another language — the full name is used, so
    this degrades to verbose rather than to wrong.
    """
    name = _name_of(state).lower()
    if "left" in name:
        return "Left"
    if "right" in name:
        return "Right"
    return _name_of(state)


def _dismiss(item_id: str) -> dict[str, Any]:
    """The action that clears a row for good."""
    return {"service": f"{DOMAIN}.{SERVICE_DISMISS}", "data": {ATTR_ITEM_ID: item_id}}


def _hung(load_id: str) -> dict[str, Any]:
    """The action that says a load of washing is up.

    Deliberately NOT a dismissal. A dismissal is card-side memory: it hides a
    row while the thing that produced it carries on being true, and the
    washing machine card would still be showing "2 to hang" next to a Needs
    you list that had forgotten about them. This clears the load at source,
    in the one place that counts them, so the wall button, this row and the
    card cannot disagree.

    `source` is stated rather than left to the default. This row is pressed
    on a screen, and a screen is not a place: the panel is in the kitchen
    but a phone is wherever its owner is. Saying so here keeps the activity
    feed's answer to "where are people" honest.
    """
    return {"service": f"{DOMAIN}.{SERVICE_LAUNDRY_HUNG}",
            "data": {ATTR_LOAD_ID: load_id, ATTR_SOURCE: SOURCE_UI}}


def _snooze(item_id: str, hours: int = 8) -> dict[str, Any]:
    """The action that hides a row for a while.

    A row carries its own action rather than the card deriving one, because
    the sensor is the thing that knows whether an item can be finished or
    only postponed. Bins get Done; a flat battery gets Snooze, since tapping
    it does not charge anything.
    """
    return {"service": f"{DOMAIN}.{SERVICE_SNOOZE}",
            "data": {ATTR_ITEM_ID: item_id, ATTR_HOURS: hours}}


class _Derived(SensorEntity):
    """Shared plumbing: recompute on a timer, publish a count plus rows."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._items: list[dict[str, Any]] = []

    def _option(self, key: str, default: Any) -> Any:
        return self._entry.options.get(key, self._entry.data.get(key, default))

    def _watched(self) -> list[str]:
        """Entities worth reacting to the instant they change.

        A full scan on every state change would be wasteful in a house with
        hundreds of entities, so batteries and offline entities ride the
        timer. These two are specific, cheap, and the ones a person expects
        to respond immediately.
        """
        return [
            entity_id
            for entity_id in (
                self._option(CONF_BIN_SENSOR, None),
                self._option(CONF_TASKS_SENSOR, None),
            )
            if entity_id
        ]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, SCAN_INTERVAL)
        )
        if watched := self._watched():
            self.async_on_remove(
                async_track_state_change_event(self.hass, watched, self._async_changed)
            )
        # A first scan during startup sees a half-built state machine: Hue has
        # not pushed battery levels and nothing has been marked unavailable
        # yet, so everything reads clean. Scanning again once Home Assistant
        # has finished starting is what stops the band being wrong — and
        # reassuringly wrong — for the first few minutes after a restart.
        self.async_on_remove(async_at_started(self.hass, self._async_started))
        self._recompute()

    @callback
    def _async_started(self, _hass: HomeAssistant) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _async_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self._recompute()
        self.async_write_ha_state()

    def _recompute(self) -> None:
        raise NotImplementedError

    @property
    def native_value(self) -> int:
        return len(self._items)

    def _ignored(self) -> set[str]:
        """Entities the house has decided not to hear about.

        Some battery sensors do not measure a battery. A Hue button that has
        read exactly 1% for four months is not a battery at 1%, and a row
        that can never be cleared is worse than no row.
        """
        return set(self._option(CONF_IGNORE_UNAVAILABLE, []) or [])

    def _battery_readings(self) -> list[tuple[State, float]]:
        """Every battery sensor with a reading, worst first."""
        ignored = self._ignored()
        found: list[tuple[State, float]] = []
        for state in self.hass.states.async_all("sensor"):
            if state.attributes.get(ATTR_DEVICE_CLASS) != "battery":
                continue
            if state.state in _NOT_A_READING:
                continue
            try:
                level = float(state.state)
            except ValueError:
                continue
            if state.entity_id in ignored:
                continue
            found.append((state, level))
        found.sort(key=lambda pair: pair[1])
        return found

    def _salt_low(self) -> list[tuple[str, float]]:
        """Every side's reading while the softener needs filling, else none.

        The one place the salt rule lives. Needs you raises its row from it
        and System health raises the Maintenance tab's level from it, and
        two copies of a threshold is how a tab comes to go yellow for a
        softener the list has stopped mentioning -- or the reverse.

        A twin-cylinder softener alternates: one side works while the other
        regenerates, so a single side running out is normal and survivable,
        and both running down together is not. That is why there are two
        thresholds rather than one. Every side at or below the first is the
        real warning; any single side at or below the second is the earlier,
        sharper one.
        """
        entity_ids = self._option(CONF_SALT_SENSORS, []) or []
        readings: list[tuple[str, float]] = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _NOT_A_READING:
                continue
            try:
                readings.append((_salt_side(state), float(state.state)))
            except (TypeError, ValueError):
                continue

        # No usable reading is not the same as a full tank. Say nothing
        # rather than claim the softener is fine.
        if not readings:
            return []

        both = float(self._option(CONF_SALT_BOTH_THRESHOLD, DEFAULT_SALT_BOTH_THRESHOLD))
        one = float(self._option(CONF_SALT_ONE_THRESHOLD, DEFAULT_SALT_ONE_THRESHOLD))

        levels = [level for _, level in readings]
        all_low = all(level <= both for level in levels)
        any_low = any(level <= one for level in levels)
        return readings if (all_low or any_low) else []

    def _batteries_below(self, threshold: int) -> list[tuple[State, float]]:
        """Battery sensors under the threshold, worst first."""
        return [
            (state, level)
            for state, level in self._battery_readings()
            if level <= threshold
        ]

    def _unavailable(self) -> list[State]:
        """Entities that have gone away, minus the ones nobody acts on.

        A device that is merely offline reports `unavailable`; `unknown` means
        it is present and has not decided yet, which is not a fault.
        """
        ignored = self._ignored()
        out: list[State] = []
        for state in self.hass.states.async_all():
            if state.state != STATE_UNAVAILABLE:
                continue
            if state.domain in _NOISY_DOMAINS:
                continue
            if state.entity_id in ignored:
                continue
            entry = er.async_get(self.hass).async_get(state.entity_id)
            if entry is not None and entry.entity_category is not None:
                continue
            out.append(state)
        out.sort(key=_name_of)
        return out


class NeedsYouSensor(_Derived, RestoreEntity):
    """What a human has to do, and nothing that is merely true.

    The governing rule from the spec: status is ambient and permanent, actions
    are conditional and dismissable, and never both. The Bins tile says
    "Tomorrow · Garden waste" all week; this says "put the bins out" for one
    evening, and clears when you do.

    On a good day this is zero and the band disappears entirely. A dashboard
    that is permanently red stops being read.
    """

    _attr_name = "Needs you"
    _attr_icon = "mdi:hand-wave"
    _attr_native_unit_of_measurement = "items"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_needs_you"
        # id -> when it becomes actionable again. A dismissal is a snooze with
        # no end, so one structure covers both.
        self._suppressed: dict[str, datetime | None] = {}
        # person -> when their trackers went quiet. Held here rather than
        # read off `last_changed`, because a person's `last_changed` is
        # reset by a restart -- so a grace period measured from it would
        # start again at every reboot and a tracker quiet since breakfast
        # would never get past it.
        self._dark_since: dict[str, datetime] = {}
        # Set by the platform. The door is decided in one place -- the
        # grace, the jam, the blip-proof clock -- and this only reads it.
        self.security: SecurityStatusSensor | None = None

    async def async_added_to_hass(self) -> None:
        """Restore suppressions, then recompute so a restart does not un-dismiss.

        The restore machinery is set up by RestoreEntity's own
        async_added_to_hass, so it has to run first — which means the base
        class has already computed once without the suppressions. Recomputing
        afterwards is what makes the restored dismissals take effect.
        """
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is None:
            return
        dark = last.attributes.get("dark_since")
        if isinstance(dark, dict):
            for entity_id, when in dark.items():
                parsed = dt_util.parse_datetime(when) if when else None
                if parsed is not None:
                    self._dark_since[entity_id] = parsed

        restored = last.attributes.get("suppressed")
        if isinstance(restored, dict):
            for item_id, until in restored.items():
                if until is None:
                    self._suppressed[item_id] = None
                    continue
                parsed = dt_util.parse_datetime(until)
                if parsed is not None:
                    self._suppressed[item_id] = parsed
        self._recompute()

    @callback
    def suppress(self, item_id: str, hours: float | None = None) -> None:
        """Dismiss (no hours) or snooze an item by its stable id."""
        self._suppressed[item_id] = (
            None if hours is None else dt_util.utcnow() + timedelta(hours=hours)
        )
        self._recompute()
        self.async_write_ha_state()

    @callback
    def refresh(self) -> None:
        """Recompute now, for a change no subscription could have caught.

        The appliance sensors keep their pending loads in memory rather than
        in an entity this could watch, and they are created after this is —
        so they call in here instead of being subscribed to.
        """
        if self.hass is None:
            return
        self._recompute()
        self.async_write_ha_state()

    @callback
    def reset(self) -> None:
        """Bring everything back — the escape hatch when a rule misfires."""
        self._suppressed.clear()
        self._recompute()
        self.async_write_ha_state()

    def _is_suppressed(self, item_id: str) -> bool:
        if item_id not in self._suppressed:
            return False
        until = self._suppressed[item_id]
        if until is None:
            return True
        if dt_util.utcnow() >= until:
            # Expired snoozes are dropped rather than kept as history; the
            # item simply becomes actionable again.
            del self._suppressed[item_id]
            return False
        return True

    def _recompute(self) -> None:
        candidates: list[dict[str, Any]] = []
        candidates.extend(self._security())
        candidates.extend(self._bins())
        candidates.extend(self._tasks())
        candidates.extend(self._batteries())
        candidates.extend(self._salt())
        # No overnight-baseline row. It reported a night that had already
        # happened, with no action beyond Dismiss, which is the one thing
        # a Needs-you row may not be: it did not need doing. The figures
        # stay where they always were, on sensor.energy_day, where the
        # Electricity card reads them -- what leaves is the claim that
        # they were a job.
        candidates.extend(self._appliances())
        candidates.extend(self._people())
        candidates.extend(self._offline())

        # A dismissal only clears the occurrence it was made against, so
        # "bin out" returns next week rather than never coming back. That is
        # what the date in the id is doing.
        self._items = [
            c for c in candidates
            if c.get("sticky") or not self._is_suppressed(c["id"])
        ]

        # Clean up suppressions whose item is gone, so the dict cannot grow
        # without bound across months of restarts.
        live = {c["id"] for c in candidates}
        for stale in [k for k in self._suppressed if k not in live]:
            del self._suppressed[stale]

    # --- the providers ------------------------------------------------

    def _security(self) -> list[dict[str, Any]]:
        """A door left unlocked or open, as a job rather than a banner.

        It used to be a separate red alert card above this list, which
        broke two rules at once: a job that lived somewhere other than
        here, and red from the first second -- the loudest colour in the
        house on somebody carrying the shopping in. Now it is a row like
        any other, `waiting` inside the grace and `critical` past it, and
        the level is read off `Security status` so the row, the card and
        the tab can never disagree about which it is.

        No snooze. A door does not keep; the row clears when it shuts.
        """
        if self.security is None:
            return []
        level = self.security.level
        if level is None:
            return []
        rows: list[dict[str, Any]] = []
        for door in self.security.unlocked:
            since = dt_util.parse_datetime(door["since"])
            when = dt_util.as_local(since).strftime("%H:%M") if since else None
            jammed = door.get("jammed")
            rows.append({
                "id": f"unlocked_{door['entity_id']}",
                "title": f"{door['name']} {'jammed' if jammed else 'unlocked'}",
                "detail": f"Unlocked since {when}" if when else "Unlocked",
                "icon": door["icon"],
                "level": level,
                "sticky": True,
                "action_label": "Lock",
                "action": {"service": "lock.lock",
                           "target": {"entity_id": door["entity_id"]}},
            })
        for door in self.security.opened:
            since = dt_util.parse_datetime(door["since"])
            when = dt_util.as_local(since).strftime("%H:%M") if since else None
            rows.append({
                "id": f"open_{door['entity_id']}",
                "title": f"{door['name']} open",
                "detail": f"Open since {when}" if when else "Open",
                "icon": door["icon"],
                "level": level,
                "sticky": True,
            })
        return rows

    def _bins(self) -> list[dict[str, Any]]:
        """The day before a collection, because that is when they go out.

        The tile says "Garden · Out tonight" all week; this row is the job,
        and it appears for the whole of the day before rather than only the
        evening. You decide to do it when you think of it, not at five.

        The bin sensor is expected to carry `daysTo` and to read as the
        stream — `Garden`, `Refuse + Food`.
        """
        entity_id = self._option(CONF_BIN_SENSOR, None)
        if not entity_id:
            return []
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _NOT_A_READING:
            return []
        days = state.attributes.get("daysTo")
        if days not in (0, 1):
            return []

        now = dt_util.now()
        collection = now.date() + timedelta(days=int(days))
        title = "Bins out tonight" if days == 1 else "Bins out now"
        detail = f"{state.state} collected " + ("tomorrow" if days == 1 else "today")
        return [{
            # Keyed to the collection date, so marking tonight's done does
            # not silence next week's.
            "id": f"bin_{collection.isoformat()}",
            "title": title,
            "detail": detail,
            "icon": "mdi:trash-can-outline",
            "level": LEVEL_ATTENTION,
            "action_label": "Done",
            "action": _dismiss(f"bin_{collection.isoformat()}"),
        }]

    def _tasks(self) -> list[dict[str, Any]]:
        """Overdue chores only — the full list lives in its own pop-up."""
        entity_id = self._option(CONF_TASKS_SENSOR, None)
        if not entity_id:
            return []
        state = self.hass.states.get(entity_id)
        if state is None or state.state != STATE_ON:
            return []
        return [{
            "id": "tasks_overdue",
            "title": "Overdue chores",
            "detail": _name_of(state),
            "icon": "mdi:clipboard-alert-outline",
            "level": LEVEL_ATTENTION,
            "action_label": "Snooze",
            "action": _snooze("tasks_overdue"),
        }]

    def _salt(self) -> list[dict[str, Any]]:
        """One row when the softener needs filling -- see `_salt_low`.

        Always one row, never one per side. Filling the machine is a single
        errand whichever cylinder prompted it, and two rows for one bag of
        salt is the noise this sensor exists to avoid.
        """
        readings = self._salt_low()
        if not readings:
            return []

        detail = _salt_detail(readings)
        return [{
            "id": "softener_salt",
            "title": "Water softener needs salt",
            "detail": detail,
            "icon": "mdi:shaker-outline",
            # Ochre, whichever rule raised it. It was terracotta when
            # every side was low and ochre when only one was, which made
            # the colour report the SHOPPING -- a bag to buy rather than
            # a bag in the garage -- and not the urgency. Nobody reads a
            # colour that way.
            #
            # And terracotta on this panel is for something going wrong
            # now: water on the floor, the house left unlocked. A
            # softener running low is slow, recoverable, and fixed by an
            # errand. Ochre already means exactly that -- something wants
            # doing, and there is a row for it -- which is what this is.
            #
            # The row appearing at all is the signal. Splitting it into
            # two colours spent the loudest one in the house on a chore.
            "level": LEVEL_ATTENTION,
            # No snooze, and not suppressible at all.
            #
            # Everything else on this list can be put off because
            # putting it off costs nothing: the bins come round again,
            # the washing waits. Salt does not wait -- it runs out,
            # and then the softener is passing hard water through the
            # house until somebody notices limescale. The row is only
            # ever true when there is a bag to fetch or a bag to buy,
            # and it clears itself the moment the level comes back up.
            #
            # `sticky` rather than just dropping the button, because
            # the button is not the only way in: the service is there
            # for anything to call, and a row that cannot be cleared
            # by hand should not be clearable by a stale suppression
            # either.
            "sticky": True,
        }]

    def _batteries(self) -> list[dict[str, Any]]:
        """A row each while that is still a list, one row once it is a job.

        One flat battery is an errand. Seven is an afternoon, and seven rows
        would bury everything else on the band — the same reasoning that
        keeps offline to a single row.
        """
        threshold = int(self._option(CONF_BATTERY_THRESHOLD, DEFAULT_BATTERY_THRESHOLD))
        flat = self._batteries_below(threshold)
        if not flat:
            return []

        if len(flat) > BATTERY_ROWS_MAX:
            names = ", ".join(_battery_label(state) for state, _ in flat[:3])
            return [{
                "id": "batteries",
                "action": _snooze("batteries", 24),
                "title": f"{len(flat)} low batteries",
                "detail": f"{names} and {len(flat) - 3} more"
                          if len(flat) > 3 else names,
                "icon": "mdi:battery-alert-variant-outline",
                "level": LEVEL_ATTENTION,
                "action_label": "Snooze",
            }]

        rows: list[dict[str, Any]] = []
        for state, level in flat:
            area = _area_of(self.hass, state.entity_id)
            rows.append({
                "id": f"battery_{state.entity_id}",
                "action": _snooze(f"battery_{state.entity_id}", 24),
                "title": f"{_battery_label(state)} battery",
                "detail": f"{level:.0f}%" + (f" · {area}" if area else ""),
                "icon": "mdi:battery-alert-variant-outline",
                "level": LEVEL_ATTENTION,
                "action_label": "Snooze",
            })
        return rows

    def _watched(self) -> list[str]:
        """Also the appliance sensors, so a finished wash appears at once.

        Overridden here rather than added to the base: `System health` and
        `Security status` share that base and have no business knowing what
        an appliance is. Reaching into the subclass from the base crashed
        both of them on setup, which is a whole entity missing from the
        house for a line that belongs one level down.
        """
        return (
            super()._watched()
            + self._appliance_entities()
            + list(self._option(CONF_PEOPLE, []) or [])
        )

    def _energy_entity(self) -> str | None:
        """The day sensor this integration publishes, found by its shape.

        Looked up rather than injected, for the reasons the appliance lookup
        gives: setup order stops mattering, and this reads the same state a
        card reads.

        Matched on the two keys the day sensor publishes whatever state it
        is in. A signature built from `baseline_watts` would stop matching
        the moment the data went stale -- which happens to give the right
        answer, by failing to find the sensor at all, and would leave the
        staleness check below as dead code that only looked like the reason.
        """
        for state in self.hass.states.async_all("sensor"):
            attrs = state.attributes
            if "for_day" in attrs and "days_late" in attrs:
                return state.entity_id
        return None

    def _appliance_entities(self) -> list[str]:
        """The cycle sensors this integration publishes, found by their id.

        Looked up rather than injected so the rows keep working if the
        entities are set up in a different order, and so this reads the same
        state a card does — one source of truth, checked the same way.
        """
        found = []
        for state in self.hass.states.async_all("sensor"):
            if state.attributes.get("slug") and "pending_count" in state.attributes:
                found.append(state.entity_id)
        return found

    def _appliances(self) -> list[dict[str, Any]]:
        """Water on the floor, a machine left dead, a full drum, washing to hang.

        Four urgencies from one sensor, and the last two are sequential
        rather than alternatives: a finished load is in the drum until the
        door is opened, and a WASHED load is then still to be hung. A dryer
        stops after the first of those, which is the whole difference
        between the two machines.

        Only the leak and the dead machine are offered a snooze. The other
        two are cleared by doing the thing -- the door for the drum, the
        button for the hanging.
        """
        rows: list[dict[str, Any]] = []
        for entity_id in self._appliance_entities():
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _NOT_A_READING:
                continue
            attrs = state.attributes
            name = _name_of(state)
            slug = attrs.get("slug") or entity_id

            # `leak_alarm`, not `leak`. Once somebody has switched the plug
            # back on over a wet pad they have looked at the floor and
            # decided to finish the wash; the pad staying damp after that
            # is a fact for the card, not a job. Older states without the
            # key fall back to the raw sensor rather than going quiet.
            if attrs.get("leak_alarm", attrs.get("leak")):
                powered = attrs.get("powered", True)
                rows.append({
                    "id": f"leak_{slug}",
                    "title": f"{name} is leaking",
                    # Never claim the cut: this row also fires when the
                    # cutoff has not happened, which is the worse case.
                    "detail": (
                        "Power still on \u00b7 check the floor"
                        if powered
                        else "Power cut at the plug \u00b7 check the floor"
                    ),
                    "icon": "mdi:water-alert",
                    "level": LEVEL_CRITICAL,
                    "action_label": "Snooze",
                    "action": _snooze(f"leak_{slug}", hours=1),
                })

            elif attrs.get("leak"):
                # Power restored over a wet pad. The person has decided about
                # the floor, but the cutoff only fires on the pad GOING wet,
                # so until it dries a second leak would cut nothing. That is
                # a real job -- dry the pad -- and it keeps, so attention.
                # Cleared by the pad drying, which is the only true answer.
                rows.append({
                    "id": f"leak_wet_{slug}",
                    "title": f"{name} leak sensor still wet",
                    "detail": "Won't cut the power again until it dries",
                    "icon": "mdi:water-alert",
                    "level": LEVEL_ATTENTION,
                })

            # Deliberately independent of the leak: the sensor stays wet long
            # after the floor is dealt with, and the cycle still has to be
            # finished. "It is off" stays true and stays worth saying.
            if not attrs.get("powered", True):
                rows.append({
                    "id": f"unpowered_{slug}",
                    "title": f"{name} has no power",
                    "detail": "Switched off at the plug",
                    "icon": "mdi:power-plug-off",
                    # Waiting, not attention. A machine without power
                    # mid-cycle is wet washing and a clock running: the
                    # activity is paused until somebody acts, which is
                    # exactly what the middle level is for. It shared a
                    # colour with "bins tomorrow" before there was one.
                    "level": LEVEL_WAITING,
                    "action_label": "Snooze",
                    "action": _snooze(f"unpowered_{slug}", hours=4),
                })

            # There is washing sitting in the drum. True of both machines
            # and cleared the same way on both -- by the door, which they
            # can see for themselves, so this row is offered no button. A
            # job you finish by doing the obvious physical thing should not
            # also have a way to be marked done from a screen; two ways to
            # clear one row is how the row and the world drift apart.
            if attrs.get("drum_full"):
                rows.append({
                    "id": f"drum_{slug}",
                    "title": f"{name} needs emptying",
                    "detail": self._drum_detail(attrs),
                    "icon": "mdi:door-open",
                    "level": LEVEL_ATTENTION,
                })

            # One row per load, keyed to the cycle that produced it, so
            # clearing one leaves the other alone and next week's wash is
            # never silenced by last week's dismissal.
            pending = attrs.get("pending")
            if not isinstance(pending, list):
                continue
            for load in pending:
                if not isinstance(load, dict) or not load.get("id"):
                    continue
                rows.append({
                    "id": load["id"],
                    "title": "Laundry needs hanging",
                    "detail": self._load_detail(load),
                    "icon": "mdi:hanger",
                    "level": LEVEL_ATTENTION,
                    "action_label": "Hung",
                    "action": _hung(load["id"]),
                })
        return rows

    @staticmethod
    def _drum_detail(attrs: dict[str, Any]) -> str:
        finished = attrs.get("last_finished_at")
        parsed = dt_util.parse_datetime(finished) if finished else None
        when = "Finished " + dt_util.as_local(parsed).strftime("%H:%M") if parsed else "Finished"
        return f"{when} \u00b7 clears when the door is opened"

    @staticmethod
    def _load_detail(load: dict[str, Any]) -> str:
        finished = load.get("finished_at")
        parsed = dt_util.parse_datetime(finished) if finished else None
        if parsed is None:
            return "Finished"
        return "Finished " + dt_util.as_local(parsed).strftime("%H:%M")

    def _people(self) -> list[dict[str, Any]]:
        """A person nobody can locate at all.

        Not "away" -- that is a reading, and a perfectly good one. This
        is the absence of any reading: no tracker of theirs is
        reporting, so every presence automation in the house is now
        guessing. The card draws it in the warning colour, and on this
        panel yellow is a promise that something wants doing, so the
        job has to exist here or the colour is a lie.

        A grace period, because a phone can be in a tunnel, on a plane
        or rebooting and none of those is a job. Measured from when we
        first saw them go dark and held across a restart, for the
        reason given where `_dark_since` is declared.
        """
        watched = list(self._option(CONF_PEOPLE, []) or [])
        if not watched:
            return []
        grace = timedelta(
            minutes=float(
                self._option(
                    CONF_PRESENCE_GRACE_MINUTES, DEFAULT_PRESENCE_GRACE_MINUTES
                )
            )
        )
        now = dt_util.utcnow()
        rows: list[dict[str, Any]] = []
        for entity_id in watched:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                # A positive reading of any kind -- home, away, a zone --
                # is the tracker working. Forget that it ever was not.
                self._dark_since.pop(entity_id, None)
                continue
            since = self._dark_since.setdefault(entity_id, state.last_changed)
            if now - since < grace:
                continue
            rows.append({
                "id": f"dark_{entity_id}",
                "title": f"{_name_of(state)} cannot be located",
                "detail": (
                    "No tracker reporting \u00b7 "
                    f"quiet since {dt_util.as_local(since).strftime('%H:%M')}"
                ),
                "icon": "mdi:map-marker-question",
                "level": LEVEL_ATTENTION,
                "action_label": "Snooze",
                "action": _snooze(f"dark_{entity_id}", hours=12),
            })
        return rows

    def _offline(self) -> list[dict[str, Any]]:
        """One row for all of them, not one each, counted as devices.

        Twenty-seven unavailable entities is one problem -- an integration
        is down -- and twenty-seven rows would bury everything else. It is
        counted the way the Devices card counts, from the same scan, so the
        row and the card say the same number: this row used to count
        entities and read "31 offline" beside a card saying 7.
        """
        # Imported here: the devices module builds on this one.
        from .devices import OFFLINE, scan_devices

        problems, _ = scan_devices(self.hass, self._ignored())
        if not problems:
            return []
        off = sum(1 for p in problems if p["state"] == OFFLINE)
        part = len(problems) - off
        if off and part:
            title = f"{_count(off, 'device')} offline, {part} partly"
        elif off:
            title = f"{_count(off, 'device')} offline"
        else:
            title = f"{_count(part, 'device')} partly offline"
        names = ", ".join(p["name"] for p in problems[:3])
        if len(problems) > 3:
            names += f" and {len(problems) - 3} more"
        return [{
            "id": "offline",
            "action": _snooze("offline", 12),
            "title": title,
            "detail": names,
            "icon": "mdi:lan-disconnect",
            # Nothing is accruing damage and nothing is paused waiting
            # for a person: a quiet device is an investigation for today
            # or tomorrow. It was the loudest thing on the panel once and
            # pushed the Maintenance tile red every morning, which is how
            # a red stops meaning anything.
            "level": LEVEL_ATTENTION,
            "action_label": "Snooze",
        }]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "items": list(self._items),
            "suppressed": {
                item_id: (until.isoformat() if until else None)
                for item_id, until in self._suppressed.items()
            },
            # Persisted for the same reason it is not read off
            # `last_changed`: a restart would otherwise restart the
            # grace period, and a tracker quiet since breakfast would
            # never get past it on a box that reboots twice a day.
            "dark_since": {
                entity_id: when.isoformat()
                for entity_id, when in self._dark_since.items()
            },
        }


class SystemHealthSensor(_Derived):
    """What is wrong with the house's plumbing, as opposed to its jobs.

    Separate from `Needs you` on purpose: this is ambient status and stays
    true for as long as it is true. It carries the raw lists as well as the
    rows, because an agent asking "what is offline?" wants entity ids, not a
    sentence assembled for a card.
    """

    _attr_name = "System health"
    _attr_icon = "mdi:heart-pulse"
    _attr_native_unit_of_measurement = "issues"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_system_health"
        self._batteries: list[dict[str, Any]] = []
        self._all_batteries: list[dict[str, Any]] = []
        self._threshold = DEFAULT_BATTERY_THRESHOLD
        self._offline: list[dict[str, Any]] = []
        self._updates: list[dict[str, Any]] = []
        self._salt: list[tuple[str, float]] = []

    def _recompute(self) -> None:
        threshold = int(self._option(CONF_BATTERY_THRESHOLD, DEFAULT_BATTERY_THRESHOLD))
        self._threshold = threshold
        # Every battery, not just the flat ones, so the Batteries card can
        # say how the rest stand. `low` is decided here rather than by the
        # card comparing against a threshold of its own: two places holding
        # the line is how a card comes to call a battery fine that Needs
        # you is asking somebody to change.
        self._all_batteries = [
            {
                "entity_id": state.entity_id,
                "name": _battery_label(state),
                "area": _area_of(self.hass, state.entity_id),
                "percent": percent,
                "low": percent <= threshold,
            }
            for state, percent in self._battery_readings()
        ]
        self._batteries = [
            {
                "entity_id": state.entity_id,
                "name": _name_of(state),
                "area": _area_of(self.hass, state.entity_id),
                # "percent", not "level". A level is now one of the three
                # names a job can carry, and a dict published to a card
                # with a level key meaning 41.0 is a trap waiting for the
                # first person who renders low_batteries as rows.
                "percent": percent,
            }
            for state, percent in self._batteries_below(threshold)
        ]
        self._offline = [
            {
                "entity_id": state.entity_id,
                "name": _name_of(state),
                "area": _area_of(self.hass, state.entity_id),
            }
            for state in self._unavailable()
        ]
        self._updates = [
            {
                "entity_id": state.entity_id,
                "name": _name_of(state),
                "installed": state.attributes.get("installed_version"),
                "latest": state.attributes.get("latest_version"),
            }
            for state in sorted(self.hass.states.async_all("update"), key=_name_of)
            if state.state == STATE_ON
        ]

        rows: list[dict[str, Any]] = []
        if self._batteries:
            worst = self._batteries[0]
            rows.append({
                "id": "batteries",
                "name": "Low batteries",
                "sub": f"{worst['name']} at {worst['percent']:.0f}%",
                "value": str(len(self._batteries)),
                "icon": "mdi:battery-alert-variant-outline",
                "level": LEVEL_ATTENTION,
            })
        # The softener lives on the Maintenance tab, so its salt is part of
        # what this sensor says about the tab: a row here, and the level the
        # tab's rail button wears. The Water softener card's outline reads
        # `salt_level` below. All three come from `_salt_low`, which is also
        # where the Needs you row comes from, so card, tab and row agree --
        # the same three-way obligation the batteries already keep.
        self._salt = self._salt_low()
        if self._salt:
            rows.append({
                "id": "softener_salt",
                "name": "Softener salt",
                "sub": _salt_detail(self._salt),
                "value": f"{min(level for _, level in self._salt):.0f}%",
                "icon": "mdi:shaker-outline",
                "level": LEVEL_ATTENTION,
            })
        # The row counts devices, from the same scan as the Devices card
        # and the Needs you row; `offline` below stays the raw entity list,
        # because an agent asking "what is offline?" wants entity ids.
        from .devices import scan_devices  # the devices module builds on this one

        if devices := scan_devices(self.hass, self._ignored())[0]:
            rows.append({
                "id": "offline",
                "name": "Offline",
                "sub": devices[0]["name"],
                "value": str(len(devices)),
                "icon": "mdi:lan-disconnect",
                "level": LEVEL_ATTENTION,
            })
        if self._updates:
            rows.append({
                "id": "updates",
                "name": "Updates pending",
                "sub": self._updates[0]["name"],
                "value": str(len(self._updates)),
                "icon": "mdi:package-up",
                # No level, deliberately. An update pending needs no
                # doing today or tomorrow, nothing is paused on it, and
                # nothing is accruing -- so it fails every one of the
                # three timelines. It is information on a card, and it
                # takes a decorative accent like any other fact.
                "accent": ACCENT_INFO,
            })
        self._items = rows

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "items": list(self._items),
            # The worst thing in the list, so a tab tile can wear the
            # level of what is actually there instead of a fixed one.
            # The Maintenance tile was hardcoded ochre and so was yellow
            # on a morning with nothing wrong -- and yellow is now one of
            # the three colours that promise something wants doing.
            #
            # None when nothing carries a level, so the tile goes back to
            # its own accent rather than to the quietest alarm.
            "level": self._worst(),
            "low_batteries": list(self._batteries),
            "batteries": list(self._all_batteries),
            "battery_threshold": self._threshold,
            # The level the Batteries card wears, named rather than left
            # for the card to derive from a count. It is exactly the level
            # the batteries row above carries, and the Needs you rows for
            # the same batteries carry, so card, tab and row agree.
            "battery_level": LEVEL_ATTENTION if self._batteries else None,
            # The level the Water softener card wears, for the same reason
            # and on the same terms: exactly the level of the salt row above
            # and of the Needs you row for the same softener.
            "salt_level": LEVEL_ATTENTION if self._salt else None,
            "offline": list(self._offline),
            "updates_pending": list(self._updates),
            "battery_count": len(self._batteries),
            "offline_count": len(self._offline),
            "update_count": len(self._updates),
        }

    def _worst(self) -> str | None:
        """The most serious level among the rows, or None for none.

        Rows carrying no level are skipped rather than ranked last: a
        card full of information is a card with nothing wrong, and a
        tile that colours for it is a tile that is always on.
        """
        worst = None
        for row in self._items:
            level = row.get("level")
            if level not in LEVEL_LOUDNESS:
                continue
            if worst is None or LEVEL_LOUDNESS[level] > LEVEL_LOUDNESS[worst]:
                worst = level
        return worst


@callback
def _is_lock_change(data: EventStateChangedData) -> bool:
    return split_entity_id(data["entity_id"])[0] == "lock"


class SecurityStatusSensor(_Derived, RestoreEntity):
    """Is the house shut, as one of three colours.

    Ambient status, like `System health`. The job itself -- lock the door --
    is a `Needs you` row, built from what this decides, so the list, the
    Security card and its tab wear one level between them. What this adds
    is the thing a list cannot say from across the room — a colour.

    Amber covers the honest minute: a door is open because somebody is
    walking through it. Red is that same door still open once nobody could
    reasonably still be carrying anything.
    """

    _attr_name = "Security status"
    _attr_icon = "mdi:shield-home"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [SECURITY_GREEN, SECURITY_AMBER, SECURITY_RED]

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_security_status"
        self._status = SECURITY_GREEN
        self._detail = "All secure"
        # When the house last stopped being shut. None while it is shut.
        self._since: datetime | None = None
        # Per entity, when IT stopped being shut. Held across a blip for
        # the same reason `_since` is; see `_since_for`.
        self._row_since: dict[str, datetime] = {}
        self._unlocked: list[dict[str, Any]] = []
        self._open: list[dict[str, Any]] = []
        self._unreadable: list[dict[str, Any]] = []
        self._cancel_grace: CALLBACK_TYPE | None = None
        # Anything with a `refresh()` -- `Needs you` -- to tell after a
        # write, the way the appliances do. The grace expiring is a change
        # no state subscription elsewhere could see.
        self._listeners: list[Any] = []

    def add_listener(self, listener: Any) -> None:
        self._listeners.append(listener)

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()
        for listener in self._listeners:
            listener.refresh()

    @property
    def level(self) -> str | None:
        return self._level()

    @property
    def unlocked(self) -> list[dict[str, Any]]:
        return list(self._unlocked)

    @property
    def opened(self) -> list[dict[str, Any]]:
        return list(self._open)

    async def async_added_to_hass(self) -> None:
        """Restore when the house stopped being shut, so red survives a restart.

        Without this a restart resets every lock's `last_changed` to boot
        time, and a door that has been open all afternoon would come back
        amber — reassuring and wrong, which is the one thing a security
        light must never be.
        """
        await super().async_added_to_hass()
        self.async_on_remove(self._stop_grace)
        if not self._option(CONF_SECURITY_LOCKS, []):
            # "Every lock" cannot be a list of entity ids: at setup the lock
            # integrations have mostly not loaded, so the list was empty and
            # the only thing that ever noticed the front door was the
            # five-minute tick. The panel said "All secure" for three and a
            # half minutes with the door unlocked. Listen to the domain.
            self.async_on_remove(
                self.hass.bus.async_listen(
                    EVENT_STATE_CHANGED,
                    self._async_changed,
                    event_filter=_is_lock_change,
                )
            )

        if (last := await self.async_get_last_state()) is None:
            return
        # The rows as well as the card. Restoring only the card's clock
        # left every row saying "just now" after a restart while the card
        # said red, and a row that contradicts its own card is worse than
        # a row that is merely stale. Only the rows that were actually
        # insecure: an unreadable row's stamp is when it went unreadable,
        # which is not the same claim.
        for key in ("unlocked", "open"):
            for row in last.attributes.get(key) or []:
                if (at := dt_util.parse_datetime(row.get("since") or "")) and row.get(
                    "entity_id"
                ):
                    self._row_since[row["entity_id"]] = at

        if (stamp := last.attributes.get("since")) and (
            parsed := dt_util.parse_datetime(stamp)
        ):
            self._since = parsed
        if self._since is not None or self._row_since:
            self._recompute()

    def _grace(self) -> timedelta:
        return timedelta(
            minutes=float(
                self._option(
                    CONF_SECURITY_GRACE_MINUTES, DEFAULT_SECURITY_GRACE_MINUTES
                )
            )
        )

    def _locks(self) -> list[str]:
        """The locks that count, defaulting to every lock in the house.

        Defaulting to all of them means a new lock is covered without anyone
        remembering to come back here. It is re-read on every recompute, and
        a listener on the whole `lock` domain drives those recomputes, so a
        lock that loads after this sensor is heard the instant it changes.
        """
        chosen = list(self._option(CONF_SECURITY_LOCKS, []) or [])
        if chosen:
            return chosen
        return sorted(state.entity_id for state in self.hass.states.async_all("lock"))

    def _openings(self) -> list[str]:
        """Door and window contacts, opt-in only.

        No default: a house's binary sensors include the fridge, the boiler
        and the washing machine door, and a security light that goes red
        because somebody is making a sandwich teaches people to ignore it.
        """
        return list(self._option(CONF_SECURITY_OPENINGS, []) or [])

    def _watched(self) -> list[str]:
        """The chosen locks and openings. Defaulted locks ride a domain
        listener instead; see `async_added_to_hass`."""
        return list(self._option(CONF_SECURITY_LOCKS, []) or []) + self._openings()

    def _since_for(self, state: State, insecure: bool) -> datetime:
        """When this entity stopped being shut, held across a blip.

        Its own `last_changed` is the honest answer exactly once. A Nuki
        that drops to `unavailable` and comes back carries the blip as its
        `last_changed`, so a door open since two o'clock reports itself
        open for ten seconds. The card already refuses to believe that;
        the row used to believe it, and said "10s ago" in red.

        An entity that cannot be read keeps whatever it was remembered as,
        because being unreadable is the blip. Only a positive report of
        shut forgets.
        """
        if insecure:
            return self._row_since.setdefault(state.entity_id, state.last_changed)
        # Unreadable. Its stamp is when it went unreadable, which is a
        # different claim from when it stopped being shut -- and the
        # memory of the latter stays put.
        return state.last_changed

    def _shut(self, entity_id: str) -> None:
        """A positive report of shut. The only thing that forgets."""
        self._row_since.pop(entity_id, None)

    def _row(
        self, state: State, value: str, icon: str, *, insecure: bool = True
    ) -> dict[str, Any]:
        return {
            "entity_id": state.entity_id,
            "name": _name_of(state),
            "area": _area_of(self.hass, state.entity_id),
            "value": value,
            "since": self._since_for(state, insecure).isoformat(),
            "icon": icon,
        }

    def _recompute(self) -> None:
        now = dt_util.utcnow()
        unlocked: list[dict[str, Any]] = []
        opened: list[dict[str, Any]] = []
        unreadable: list[dict[str, Any]] = []

        for entity_id in self._locks():
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _NOT_A_READING:
                if state is not None:
                    unreadable.append(
                        self._row(
                            state, "Unknown", "mdi:lock-question", insecure=False
                        )
                    )
                continue
            if state.state != "locked":
                # The word is "Unlocked" whether the bolt failed to throw
                # or was never asked to. A jam is not a third state of the
                # door -- it is the mechanism failing to reach one of the
                # two -- and the honest reading of a jammed lock is that
                # the door is not locked. The reason rides alongside as a
                # flag, for the card to show as a chip.
                jammed = state.state == "jammed"
                row = self._row(
                    state,
                    "Unlocked",
                    "mdi:lock-alert" if jammed else "mdi:lock-open-variant",
                )
                row["jammed"] = jammed
                unlocked.append(row)
            else:
                self._shut(entity_id)

        for entity_id in self._openings():
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _NOT_A_READING:
                if state is not None:
                    unreadable.append(
                        self._row(state, "Unknown", "mdi:door-closed", insecure=False)
                    )
                continue
            if state.state == STATE_ON:
                opened.append(self._row(state, "Open", "mdi:door-open"))
            else:
                self._shut(entity_id)

        self._unlocked, self._open, self._unreadable = unlocked, opened, unreadable

        insecure = unlocked + opened
        if not insecure and not unreadable:
            # Everything positively reported shut. This is the only branch
            # allowed to forget when the house stopped being secure.
            self._since = None
            self._status = SECURITY_GREEN
        elif not insecure and not self._row_since:
            # Unreadable, and nothing was insecure the last time we could
            # read it. Not proof of a problem, so it never goes red — and
            # not proof of safety, so it never shows green.
            self._status = SECURITY_AMBER
        else:
            # Off `_row_since`, not off `last_changed`. The Nuki drops to
            # `unavailable` about once a day -- nine times in the ten days
            # of history this was checked against, and three times inside
            # thirteen minutes on one of those nights -- and on the way back its
            # `last_changed` is the blip rather than the moment the door
            # was opened, so reading it here restarted the grace period
            # every time: a door left open never went red as long as the
            # lock blipped more often than every five minutes.
            earliest = min(self._row_since.values())
            self._since = earliest if self._since is None else min(self._since, earliest)
            # A jam skips the grace entirely. The five minutes exist to
            # cover a door somebody is using -- carrying shopping in, seeing
            # someone out -- and a door that will shortly be shut by the
            # person who opened it. A jammed lock is the opposite: nobody is
            # coming back to finish it, because the mechanism already tried
            # and failed. Waiting five minutes to say so is five minutes of
            # a green-looking house with a door that will not lock.
            jammed = any(row.get("jammed") for row in unlocked)
            self._status = (
                SECURITY_RED
                if jammed or now - self._since >= self._grace()
                else SECURITY_AMBER
            )

        self._detail = self._summarise()
        self._items = [
            {
                "id": row["entity_id"],
                "name": row["name"],
                "value": row["value"],
                "sub": row["area"] or "No area",
                "since": row["since"],
                "icon": row["icon"],
                "jammed": bool(row.get("jammed")),
                # Red is a door left open past the grace period, which
                # is accruing risk now. Amber is inside the grace: the
                # house is waiting on somebody to shut it.
                "level": LEVEL_CRITICAL
                if self._status == SECURITY_RED
                else LEVEL_WAITING,
            }
            for row in (unlocked + opened + unreadable)
        ]
        self._arm_grace(now)

    def _summarise(self) -> str:
        """One line for the tab under the label, where three words fit."""
        parts: list[str] = []
        if self._unlocked:
            parts.append(
                self._unlocked[0]["name"]
                if len(self._unlocked) == 1
                else f"{len(self._unlocked)} unlocked"
            )
        if self._open:
            parts.append(
                f"{self._open[0]['name']} open"
                if len(self._open) == 1
                else f"{len(self._open)} open"
            )
        if parts:
            return ", ".join(parts)
        if self._unreadable:
            return f"{len(self._unreadable)} not reporting"
        return "All secure"

    @callback
    def _stop_grace(self) -> None:
        if self._cancel_grace is not None:
            self._cancel_grace()
            self._cancel_grace = None

    @callback
    def _arm_grace(self, now: datetime) -> None:
        """Flip to red on the minute it is earned, not on the next tick.

        The five-minute scan is fine for a bin day. It is not fine for a
        five-minute grace period, where it could mean waiting ten.
        """
        self._stop_grace()
        if self._status != SECURITY_AMBER or self._since is None:
            return
        due = self._since + self._grace()
        if due <= now:
            return
        self._cancel_grace = async_track_point_in_time(
            self.hass, self._async_grace_expired, due
        )

    @callback
    def _async_grace_expired(self, _now: datetime) -> None:
        self._cancel_grace = None
        self._recompute()
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Green, amber or red — the whole point of the entity."""
        return self._status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "items": list(self._items),
            "detail": self._detail,
            # The same level the rows carry, hoisted so the tab tile can
            # wear it. `system_health` already does this; the three-colour
            # state cannot stand in for it, because green/amber/red are
            # this sensor's own vocabulary and a dock button that maps
            # them itself is a second place the levels have to be kept
            # right. That second place is exactly what drifted: the map
            # was written when the decorative accents 1 and 2 were the
            # orange and the yellow, and when those hues moved out of the
            # palette it went on naming the slots -- so an open door
            # painted the tab bone-white and a red one painted it tan.
            #
            # None on green, because a house that is shut asks nothing.
            "level": self._level(),
            "since": self._since.isoformat() if self._since else None,
            "unlocked": list(self._unlocked),
            "open": list(self._open),
            "not_reporting": list(self._unreadable),
            "unlocked_count": len(self._unlocked),
            "open_count": len(self._open),
            "grace_minutes": self._grace().total_seconds() / 60,
        }

    def _level(self) -> str | None:
        """Critical past the grace, waiting inside it, nothing when shut.

        Read off `_status` rather than off the rows, so the colour and the
        word can never disagree: the same branch decides both.
        """
        if self._status == SECURITY_RED:
            return LEVEL_CRITICAL
        if self._status == SECURITY_AMBER:
            return LEVEL_WAITING
        return None
