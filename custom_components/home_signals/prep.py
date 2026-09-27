"""Meal prep sessions: when the ahead-of-time half of the week's meals gets done.

A recipe can be split into the steps that can be done ahead and the steps
that have to happen at the stove (see `recipes.py`). This file is the other
half: a *session* is one sitting in which the prep for one or more meals is
done, and it is exactly one Home Tasks item with a deadline.

One item per session rather than one per meal, because the house plans to
do its prep in as few sittings as it can -- a Sunday afternoon that covers
three dinners is one job, and three rows saying so would be the noise the
list exists to avoid.

The Home Tasks item is the job; this keeps the plan behind it. The item can
be ticked from a phone, the Tasks card or a Needs you row and this follows,
because it reads the item's status rather than keeping its own. If somebody
deletes the item outright, that is an answer too: the session counts as
done rather than re-raising a job somebody has thrown away.

Levels, and only these two (see CLAUDE.md -- a level is a promise):

- `attention` -- the prep is due today and not done.
- `waiting` -- it is past due, not done, and a meal it was for is still
  ahead. The meal is degrading into a cook-from-scratch until someone acts.

Once every meal a session was for has passed, it takes no level at all:
there is nothing left that doing the prep would change.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import logging
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DUE,
    ATTR_ITEMS,
    ATTR_MEAL_TIMES,
    ATTR_PREP_TIMES,
    ATTR_SESSION_ID,
    ATTR_TODO,
    DEFAULT_PREP_TODO,
    DOMAIN,
    LEVEL_ATTENTION,
    LEVEL_LOUDNESS,
    LEVEL_WAITING,
    SERVICE_PREP_DONE,
    SERVICE_PREP_SETTINGS,
    SERVICE_REMOVE_PREP,
    SERVICE_SAVE_PREP,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
TICK = timedelta(minutes=5)
# A session is kept a day past its last meal, so the card can still say
# "prepped" about last night, then forgotten. The Home Tasks item stays.
KEEP_AFTER = timedelta(days=1)

DEFAULT_MEAL_TIMES = {"breakfast": "07:00", "lunch": "12:00", "dinner": "17:00"}
# Weekday numbers are Python's: Monday 0 ... Sunday 6.
DEFAULT_PREP_TIMES = [
    {"label": "Sunday afternoon", "days": [6], "time": "16:00"},
    {"label": "Weekday evening", "days": [0, 1, 2, 3, 4], "time": "19:30"},
]
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_HHMM = vol.Match(r"^([01]\d|2[0-3]):[0-5]\d$")

ITEM_SCHEMA = vol.Schema(
    {
        vol.Required("date"): cv.date,
        vol.Required("entry_type"): cv.string,
        vol.Required("name"): cv.string,
        vol.Optional("recipe_id"): vol.Any(None, cv.string),
        vol.Optional("slug"): vol.Any(None, cv.string),
        vol.Optional("steps", default=list): [cv.string],
        vol.Optional("minutes"): vol.Any(None, vol.Coerce(int)),
    },
    extra=vol.REMOVE_EXTRA,
)

SAVE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_SESSION_ID): cv.string,
        vol.Required(ATTR_DUE): cv.datetime,
        vol.Required(ATTR_ITEMS): [ITEM_SCHEMA],
        vol.Optional("title"): cv.string,
    }
)
ID_SCHEMA = vol.Schema({vol.Required(ATTR_SESSION_ID): cv.string})
SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MEAL_TIMES): {cv.string: _HHMM},
        vol.Optional(ATTR_PREP_TIMES): [
            vol.Schema(
                {
                    vol.Optional("label"): cv.string,
                    vol.Required("days"): [vol.All(vol.Coerce(int), vol.Range(0, 6))],
                    vol.Required("time"): _HHMM,
                }
            )
        ],
        vol.Optional(ATTR_TODO): cv.entity_id,
    }
)


def _hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _local(value: datetime) -> datetime:
    """A naive time from a card is house time, not UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_local(value)


def session_title(items: list[dict[str, Any]]) -> str:
    """'Prep: Chilli and Fajitas' -- the meals, once each, in order."""
    names: list[str] = []
    for item in sorted(items, key=lambda i: (str(i["date"]), i["entry_type"])):
        if item["name"] not in names:
            names.append(item["name"])
    if not names:
        return "Prep"
    if len(names) == 1:
        return f"Prep: {names[0]}"
    return f"Prep: {', '.join(names[:-1])} and {names[-1]}"


def _meal_label(item: dict[str, Any]) -> str:
    day = date.fromisoformat(str(item["date"]))
    return f"{DAY_NAMES[day.weekday()]} {item['entry_type']}"


def session_description(items: list[dict[str, Any]]) -> str:
    """What the task says when it is opened: each meal and its steps."""
    blocks = []
    for item in sorted(items, key=lambda i: (str(i["date"]), i["entry_type"])):
        head = f"For {_meal_label(item)} · {item['name']}"
        if item.get("minutes"):
            head += f" · about {item['minutes']} min"
        lines = [head] + [f"- {step}" for step in item.get("steps") or []]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class MealPrepSensor(SensorEntity):
    """The week's prep sessions, whether each is done, and how late it is."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Meal prep"
    _attr_icon = "mdi:knife"
    _attr_native_unit_of_measurement = "sessions"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_meal_prep"
        self._store: Store | None = None
        self._sessions: list[dict[str, Any]] = []
        self._settings: dict[str, Any] = {}
        # task uid -> status, as last read from the list. None until read.
        self._status: dict[str, str] | None = None
        self._listeners: list[Any] = []
        self._unwatch: Any = None

    # --- plumbing -----------------------------------------------------

    def add_listener(self, listener: Any) -> None:
        self._listeners.append(listener)

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()
        for listener in self._listeners:
            listener.refresh()

    @property
    def todo(self) -> str:
        return self._settings.get(ATTR_TODO) or DEFAULT_PREP_TODO

    @property
    def meal_times(self) -> dict[str, str]:
        return {**DEFAULT_MEAL_TIMES, **(self._settings.get(ATTR_MEAL_TIMES) or {})}

    @property
    def prep_times(self) -> list[dict[str, Any]]:
        return self._settings.get(ATTR_PREP_TIMES) or DEFAULT_PREP_TIMES

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._store = Store(self.hass, STORE_VERSION, f"{DOMAIN}.meal_prep")
        data = await self._store.async_load() or {}
        self._sessions = list(data.get("sessions") or [])
        self._settings = dict(data.get("settings") or {})
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, TICK)
        )
        self._watch()
        self.async_on_remove(lambda: self._unwatch and self._unwatch())
        await self.async_sync()

    @callback
    def _watch(self) -> None:
        """Follow the list, so a tick on a phone clears the row at once."""
        if self._unwatch is not None:
            self._unwatch()
        self._unwatch = async_track_state_change_event(
            self.hass, [self.todo], self._async_list_changed
        )

    async def _async_list_changed(self, _event: Event[EventStateChangedData]) -> None:
        await self.async_sync()

    async def _async_tick(self, _now: datetime) -> None:
        await self.async_sync()

    async def _save(self) -> None:
        if self._store is not None:
            await self._store.async_save(
                {"sessions": self._sessions, "settings": self._settings}
            )

    async def _read_list(self) -> dict[str, dict[str, Any]] | None:
        """uid -> item from the list, or None when it cannot be read."""
        if self.hass.states.get(self.todo) is None:
            return None
        try:
            response = await self.hass.services.async_call(
                "todo",
                "get_items",
                {"status": ["needs_action", "completed"]},
                target={"entity_id": self.todo},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Could not read %s: %s", self.todo, err)
            return None
        items = (response or {}).get(self.todo, {}).get("items", [])
        return {i["uid"]: i for i in items if i.get("uid")}

    async def async_sync(self) -> None:
        """Re-read which tasks are done, drop old sessions, publish."""
        listed = await self._read_list()
        if listed is not None:
            self._status = {uid: i.get("status", "") for uid, i in listed.items()}
        cutoff = dt_util.now() - KEEP_AFTER
        kept = [s for s in self._sessions if self._last_meal(s) >= cutoff]
        if len(kept) != len(self._sessions):
            self._sessions = kept
            await self._save()
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    # --- the facts ------------------------------------------------------

    def meal_at(self, item: dict[str, Any]) -> datetime:
        times = self.meal_times
        when = times.get(item["entry_type"]) or times["dinner"]
        day = date.fromisoformat(str(item["date"]))
        return datetime.combine(day, _hhmm(when), dt_util.get_default_time_zone())

    def _last_meal(self, session: dict[str, Any]) -> datetime:
        meals = [self.meal_at(i) for i in session.get("items") or []]
        return max(meals) if meals else _local(dt_util.parse_datetime(session["due"]))

    def done(self, session: dict[str, Any]) -> bool:
        """Ticked, or deleted from the list -- both are somebody's answer."""
        uid = session.get("task_uid")
        if not uid or self._status is None:
            return False
        return self._status.get(uid, "completed") == "completed"

    def level(self, session: dict[str, Any], now: datetime | None = None) -> str | None:
        if self.done(session):
            return None
        now = now or dt_util.now()
        if not any(self.meal_at(i) > now for i in session.get("items") or []):
            return None
        due = _local(dt_util.parse_datetime(session["due"]))
        if now >= due:
            return LEVEL_WAITING
        if due.date() == now.date():
            return LEVEL_ATTENTION
        return None

    def _public(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            **session,
            "done": self.done(session),
            "level": self.level(session),
        }

    @property
    def native_value(self) -> int:
        now = dt_util.now()
        return sum(
            1 for s in self._sessions
            if not self.done(s) and any(self.meal_at(i) > now for i in s["items"])
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sessions = [self._public(s) for s in sorted(self._sessions, key=lambda s: s["due"])]
        levels = [s["level"] for s in sessions if s["level"]]
        return {
            "sessions": sessions,
            "level": max(levels, key=LEVEL_LOUDNESS.get) if levels else None,
            ATTR_MEAL_TIMES: self.meal_times,
            ATTR_PREP_TIMES: self.prep_times,
            ATTR_TODO: self.todo,
        }

    def needs_you_rows(self) -> list[dict[str, Any]]:
        """A row for each session that is due today or late with a meal ahead."""
        rows = []
        for session in sorted(self._sessions, key=lambda s: s["due"]):
            level = self.level(session)
            if level is None:
                continue
            due = _local(dt_util.parse_datetime(session["due"]))
            first = min(session["items"], key=self.meal_at)
            when = f"By {due:%H:%M}" if level == LEVEL_ATTENTION else f"Was due {due:%a %H:%M}"
            rows.append({
                "id": f"prep_{session['id']}_{due.date().isoformat()}",
                "title": session["title"],
                "detail": f"{when} · for {_meal_label(first)}",
                "icon": "mdi:knife",
                "level": level,
                "action_label": "Done",
                "action": {
                    "service": f"{DOMAIN}.{SERVICE_PREP_DONE}",
                    "data": {ATTR_SESSION_ID: session["id"]},
                },
            })
        return rows

    # --- the changes ----------------------------------------------------

    def _find(self, session_id: str) -> dict[str, Any] | None:
        return next((s for s in self._sessions if s["id"] == session_id), None)

    async def _todo(self, service: str, data: dict[str, Any]) -> None:
        await self.hass.services.async_call(
            "todo", service, data, target={"entity_id": self.todo}, blocking=True
        )

    async def async_save_session(self, data: dict[str, Any]) -> dict[str, Any]:
        items = [
            {**i, "date": i["date"].isoformat()} for i in data[ATTR_ITEMS]
        ]
        session_id = data.get(ATTR_SESSION_ID)
        existing = self._find(session_id) if session_id else None
        if not items:
            if existing is not None:
                await self.async_remove_session(existing["id"])
            return {ATTR_SESSION_ID: session_id, "removed": True}

        due = _local(data[ATTR_DUE]).replace(second=0, microsecond=0)
        title = data.get("title") or session_title(items)
        description = session_description(items)
        listed = await self._read_list() or {}

        uid = existing.get("task_uid") if existing else None
        if uid and uid in listed:
            update: dict[str, Any] = {
                "item": uid,
                "rename": title,
                "due_datetime": due.isoformat(),
                "description": description,
            }
            # A meal added to a session somebody already ticked is new prep.
            old = {(i["date"], i["entry_type"], i["name"]) for i in existing["items"]}
            if any((i["date"], i["entry_type"], i["name"]) not in old for i in items):
                update["status"] = "needs_action"
            await self._todo("update_item", update)
        else:
            await self._todo(
                "add_item",
                {"item": title, "due_datetime": due.isoformat(), "description": description},
            )
            after = await self._read_list() or {}
            fresh = [u for u, i in after.items() if u not in listed and i.get("summary") == title]
            uid = fresh[0] if fresh else None

        session = {
            "id": existing["id"] if existing else (session_id or uuid.uuid4().hex[:10]),
            "due": due.isoformat(),
            "title": title,
            "task_uid": uid,
            "items": items,
        }
        if existing is not None:
            self._sessions[self._sessions.index(existing)] = session
        else:
            self._sessions.append(session)
        await self._save()
        await self.async_sync()
        return {ATTR_SESSION_ID: session["id"], "task_uid": uid, "title": title}

    async def async_remove_session(self, session_id: str) -> None:
        session = self._find(session_id)
        if session is None:
            return
        uid = session.get("task_uid")
        if uid and uid in (await self._read_list() or {}):
            await self._todo("remove_item", {"item": [uid]})
        self._sessions.remove(session)
        await self._save()
        await self.async_sync()

    async def async_mark_done(self, session_id: str) -> None:
        session = self._find(session_id)
        if session is None:
            raise HomeAssistantError(f"No prep session {session_id}")
        uid = session.get("task_uid")
        if uid and uid in (await self._read_list() or {}):
            await self._todo("update_item", {"item": uid, "status": "completed"})
        await self.async_sync()

    async def async_set_settings(self, data: dict[str, Any]) -> None:
        moved = ATTR_TODO in data and data[ATTR_TODO] != self.todo
        self._settings.update(data)
        await self._save()
        if moved:
            self._watch()
        self.async_on_remove(lambda: self._unwatch and self._unwatch())
        await self.async_sync()


def async_register_prep_services(hass: HomeAssistant, prep: MealPrepSensor) -> None:
    """Registered afresh each setup, so a reload points them at the new sensor."""

    async def _save(call: ServiceCall) -> ServiceResponse:
        return await prep.async_save_session(dict(call.data))

    async def _remove(call: ServiceCall) -> None:
        await prep.async_remove_session(call.data[ATTR_SESSION_ID])

    async def _done(call: ServiceCall) -> None:
        await prep.async_mark_done(call.data[ATTR_SESSION_ID])

    async def _settings(call: ServiceCall) -> None:
        await prep.async_set_settings(dict(call.data))

    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_PREP, _save,
        schema=SAVE_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_PREP, _remove, schema=ID_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PREP_DONE, _done, schema=ID_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_PREP_SETTINGS, _settings, schema=SETTINGS_SCHEMA
    )
