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

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ACCENT_ALERT,
    ACCENT_INFO,
    ACCENT_WARN,
    BIN_EVENING_HOUR,
    CONF_BATTERY_THRESHOLD,
    CONF_BIN_SENSOR,
    CONF_IGNORE_UNAVAILABLE,
    CONF_TASKS_SENSOR,
    DEFAULT_BATTERY_THRESHOLD,
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


class _Derived(SensorEntity):
    """Shared plumbing: recompute on a timer, publish a count plus rows."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._items: list[dict[str, Any]] = []

    def _option(self, key: str, default: Any) -> Any:
        return self._entry.options.get(key, self._entry.data.get(key, default))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, SCAN_INTERVAL)
        )
        self._recompute()

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
        """Only the evening before, which is when it is actionable."""
        entity_id = self._option(CONF_BIN_SENSOR, None)
        if not entity_id:
            return []
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _NOT_A_READING:
            return []
        days = state.attributes.get("daysTo")
        now = dt_util.now()
        if days == 1 and now.hour >= BIN_EVENING_HOUR:
            when = "tonight"
        elif days == 0:
            when = "this morning"
        else:
            return []
        return [{
            "id": f"bin_{(now.date() + timedelta(days=int(days))).isoformat()}",
            "title": f"Bins out {when}",
            "detail": state.state,
            "icon": "mdi:trash-can-outline",
            "accent": ACCENT_WARN,
            "action_label": "Done",
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
