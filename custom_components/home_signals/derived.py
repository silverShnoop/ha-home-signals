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
    ATTR_HOURS,
    ATTR_ITEM_ID,
    ACCENT_INFO,
    ACCENT_WARN,
    CONF_BATTERY_THRESHOLD,
    CONF_BIN_SENSOR,
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
        restored = last.attributes.get("suppressed")
        if not isinstance(restored, dict):
            return
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
        candidates.extend(self._offline())

        # A dismissal only clears the occurrence it was made against, so
        # "bin out" returns next week rather than never coming back. That is
        # what the date in the id is doing.
        self._items = [c for c in candidates if not self._is_suppressed(c["id"])]

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
            # Every side low is the trip to buy a bag; one side low can wait
            # for the bag already in the garage.
            "accent": ACCENT_ALERT if all_low else ACCENT_WARN,
            "action_label": "Snooze",
            "action": _snooze("softener_salt", hours=24),
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
            "low_batteries": list(self._batteries),
            "offline": list(self._offline),
            "updates_pending": list(self._updates),
            "battery_count": len(self._batteries),
            "offline_count": len(self._offline),
            "update_count": len(self._updates),
        }


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
        if (stamp := last.attributes.get("since")) and (
            parsed := dt_util.parse_datetime(stamp)
        ):
            self._since = parsed
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

    def _row(self, state: State, value: str, icon: str) -> dict[str, Any]:
        return {
            "entity_id": state.entity_id,
            "name": _name_of(state),
            "area": _area_of(self.hass, state.entity_id),
            "value": value,
            "since": state.last_changed.isoformat(),
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
                    unreadable.append(self._row(state, "Unknown", "mdi:lock-question"))
                continue
            if state.state != "locked":
                unlocked.append(
                    self._row(state, "Unlocked", "mdi:lock-open-variant")
                )

        for entity_id in self._openings():
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _NOT_A_READING:
                if state is not None:
                    unreadable.append(self._row(state, "Unknown", "mdi:door-closed"))
                continue
            if state.state == STATE_ON:
                opened.append(self._row(state, "Open", "mdi:door-open"))

        self._unlocked, self._open, self._unreadable = unlocked, opened, unreadable

        insecure = unlocked + opened
        if not insecure:
            self._since = None
            # A lock that cannot be read might be either. That is not proof of
            # a problem, so it never goes red — but it is not proof of safety
            # either, so it never shows green.
            self._status = SECURITY_AMBER if unreadable else SECURITY_GREEN
        else:
            earliest = min(
                dt_util.parse_datetime(row["since"]) or now for row in insecure
            )
            self._since = earliest if self._since is None else min(self._since, earliest)
            self._status = (
                SECURITY_RED if now - self._since >= self._grace() else SECURITY_AMBER
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
