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
    ACCENT_ALERT,
    APPLIANCE_RUNNING,
    ATTR_HOURS,
    ATTR_ITEM_ID,
    ATTR_LOAD_ID,
    ATTR_SOURCE,
    ACCENT_INFO,
    ACCENT_WARN,
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

    def _batteries_below(self, threshold: int) -> list[tuple[State, float]]:
        """Battery sensors under the threshold, worst first."""
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
            if state.entity_id in self._ignored():
                continue
            if level <= threshold:
                found.append((state, level))
        found.sort(key=lambda pair: pair[1])
        return found

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
        candidates.extend(self._bins())
        candidates.extend(self._tasks())
        candidates.extend(self._batteries())
        candidates.extend(self._salt())
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
            "accent": ACCENT_WARN,
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
            "accent": ACCENT_WARN,
            "action_label": "Snooze",
            "action": _snooze("tasks_overdue"),
        }]

    def _salt(self) -> list[dict[str, Any]]:
        """One row when the softener needs filling, under either of two rules.

        A twin-cylinder softener alternates: one side works while the other
        regenerates, so a single side running out is normal and survivable,
        and both running down together is not. That is why there are two
        thresholds rather than one. Every side at or below the first is the
        real warning; any single side at or below the second is the earlier,
        sharper one.

        Always one row, never one per side. Filling the machine is a single
        errand whichever cylinder prompted it, and two rows for one bag of
        salt is the noise this sensor exists to avoid.
        """
        entity_ids = self._option(CONF_SALT_SENSORS, []) or []
        if not entity_ids:
            return []

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
        if not (all_low or any_low):
            return []

        detail = ", ".join(f"{side} {level:.0f}%" for side, level in readings)
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
            "accent": ACCENT_WARN,
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
                "accent": ACCENT_WARN,
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
                "accent": ACCENT_WARN,
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

            if attrs.get("leak"):
                rows.append({
                    "id": f"leak_{slug}",
                    "title": f"{name} is leaking",
                    "detail": "Power cut at the plug \u00b7 check the floor",
                    "icon": "mdi:water-alert",
                    "accent": ACCENT_ALERT,
                    "action_label": "Snooze",
                    "action": _snooze(f"leak_{slug}", hours=1),
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
                    "accent": ACCENT_WARN,
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
                    "accent": ACCENT_WARN,
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
                    "accent": ACCENT_WARN,
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
                "accent": ACCENT_WARN,
                "action_label": "Snooze",
                "action": _snooze(f"dark_{entity_id}", hours=12),
            })
        return rows

    def _offline(self) -> list[dict[str, Any]]:
        """One row for all of them, not one each.

        Twenty-seven unavailable entities is one problem — an integration is
        down — and twenty-seven rows would bury everything else.
        """
        gone = self._unavailable()
        if not gone:
            return []
        names = ", ".join(_name_of(s) for s in gone[:3])
        if len(gone) > 3:
            names += f" and {len(gone) - 3} more"
        return [{
            "id": "offline",
            "action": _snooze("offline", 12),
            "title": f"{len(gone)} entities offline",
            "detail": names,
            "icon": "mdi:lan-disconnect",
            "accent": ACCENT_ALERT,
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
        self._offline: list[dict[str, Any]] = []
        self._updates: list[dict[str, Any]] = []

    def _recompute(self) -> None:
        threshold = int(self._option(CONF_BATTERY_THRESHOLD, DEFAULT_BATTERY_THRESHOLD))
        self._batteries = [
            {
                "entity_id": state.entity_id,
                "name": _name_of(state),
                "area": _area_of(self.hass, state.entity_id),
                "level": level,
            }
            for state, level in self._batteries_below(threshold)
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
                "sub": f"{worst['name']} at {worst['level']:.0f}%",
                "value": str(len(self._batteries)),
                "icon": "mdi:battery-alert-variant-outline",
                "accent": ACCENT_WARN,
            })
        if self._offline:
            rows.append({
                "id": "offline",
                "name": "Offline",
                "sub": self._offline[0]["name"],
                "value": str(len(self._offline)),
                "icon": "mdi:lan-disconnect",
                "accent": ACCENT_ALERT,
            })
        if self._updates:
            rows.append({
                "id": "updates",
                "name": "Updates pending",
                "sub": self._updates[0]["name"],
                "value": str(len(self._updates)),
                "icon": "mdi:package-up",
                "accent": ACCENT_INFO,
            })
        self._items = rows

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "items": list(self._items),
            # The worst thing in the list, so a tab tile can wear the
            # colour of what is actually there instead of a fixed one.
            # The Maintenance tile was hardcoded ochre and so was yellow
            # on a morning with nothing wrong -- and on this panel
            # yellow is a promise that something wants doing.
            "accent": self._worst(),
            "low_batteries": list(self._batteries),
            "offline": list(self._offline),
            "updates_pending": list(self._updates),
            "battery_count": len(self._batteries),
            "offline_count": len(self._offline),
            "update_count": len(self._updates),
        }

    def _worst(self) -> int | None:
        """The most serious accent among the rows, or None for none.

        Ordered by how loud the role is rather than by its number:
        1 alerts, 2 warns, 5 is just information. Numeric order would
        make information the worst thing in the house.
        """
        loudness = {ACCENT_ALERT: 3, ACCENT_WARN: 2, ACCENT_INFO: 1}
        worst = None
        for row in self._items:
            accent = row.get("accent")
            if accent not in loudness:
                continue
            if worst is None or loudness[accent] > loudness[worst]:
                worst = accent
        return worst


class SecurityStatusSensor(_Derived, RestoreEntity):
    """Is the house shut, as one of three colours.

    Ambient status, like `System health`, and deliberately not a `Needs you`
    row: the alert card already asks somebody to lock the front door, and a
    second copy of the same sentence is nagging rather than informing. What
    this adds is the thing a list cannot say from across the room — a colour.

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

    async def async_added_to_hass(self) -> None:
        """Restore when the house stopped being shut, so red survives a restart.

        Without this a restart resets every lock's `last_changed` to boot
        time, and a door that has been open all afternoon would come back
        amber — reassuring and wrong, which is the one thing a security
        light must never be.
        """
        await super().async_added_to_hass()
        self.async_on_remove(self._stop_grace)

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
        remembering to come back here. The cost is that the default only
        rescans on restart; the five-minute tick still catches a lock added
        since, just not the instant it appears.
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
        return self._locks() + self._openings()

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
                "accent": ACCENT_ALERT
                if self._status == SECURITY_RED
                else ACCENT_WARN,
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
            "since": self._since.isoformat() if self._since else None,
            "unlocked": list(self._unlocked),
            "open": list(self._open),
            "not_reporting": list(self._unreadable),
            "unlocked_count": len(self._unlocked),
            "open_count": len(self._open),
            "grace_minutes": self._grace().total_seconds() / 60,
        }
