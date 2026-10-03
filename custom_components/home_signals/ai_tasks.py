"""AI tasks: slow work a card asked for, run here so it outlives the card.

Reading a recipe off a web page takes a model the best part of a minute,
and splitting it into what can be done ahead takes another. The card used
to wait for both inside a sheet that said "Reading the page..." and nothing
else: close the sheet, or walk away from the panel, and the answer arrived
to nobody. Nobody could tell whether anything was happening at all.

So the card hands the work over and this runs it. While it runs it is a
FACT -- a line on the card that started it, and no row anywhere: nothing
needs doing yet. When it finishes it becomes a job, at the one level that
promises no deadline:

    notice   something you asked for is ready to look at

and, like every level, it is all three or none: the card (by its `card`
key in `cards`), its tab's rail button and a `Needs you` row. It is the
quietest level, so it sorts below every other row.

It is news for two minutes, then all three go back to how they were on
their own. Before that, the row has two buttons: Dismiss, which clears all
three at once, and Open -- when there is an answer to show and a card to
show it -- which opens it on that card and clears it the same way. This
sensor owns the row, so Needs you hands the dismissal here.

Every finished task says which way it went -- `Done` or `Failed`, in the
row's words, its icon and its `outcome` -- and a failure is a notice too:
the reason is in the row, and it has only Dismiss.

The work itself is any action that answers -- a script that calls
`ai_task.generate_data` and stops with a response, usually. A second
action can follow it (`then`), fed from the first one's answer, so an
import and its split are one task rather than two a person has to chain.

Finished tasks, and their answers, are kept in storage for their two
minutes, so a restart in that window does not lose one. A task still running at
a restart cannot be resumed -- the call it was waiting on died with the
old instance -- so it comes back failed, and says why.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ACTION,
    ATTR_CARD,
    ATTR_DATA,
    ATTR_LABEL,
    ATTR_OPEN,
    ATTR_PASS,
    ATTR_TAB,
    ATTR_TASK_ID,
    ATTR_THEN,
    ATTR_TITLE,
    ATTR_UNLESS,
    CLEANING_CLEAR as CLEAR,
    DOMAIN,
    LEVEL_NOTICE,
    SERVICE_AI_TASK_RESULT,
    SERVICE_DISMISS,
    SERVICE_START_AI_TASK,
    TABS,
)

LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1

RUNNING = "running"
DONE = "done"
FAILED = "failed"

# A finished task is news for two minutes, then it is history: the card,
# the tab and the row all go back to how they were. It is a notice, not a
# chore -- the recipe is in the box either way, and a blue row that sits
# there all afternoon is a blue that stops being read.
KEEP_FOR = timedelta(minutes=2)
# And never more than this many, however busy the kitchen has been.
KEEP_MAX = 12

# A model that has not answered in this long is not going to.
TIMEOUT = timedelta(minutes=10)

_ID_PREFIX = "ai_"

THEN_SCHEMA = vol.Schema({
    vol.Required(ATTR_ACTION): cv.string,
    vol.Optional(ATTR_DATA, default=dict): dict,
    # field in the second action's data <- key in the first one's answer
    vol.Optional(ATTR_PASS, default=dict): {cv.string: cv.string},
    # skip the second action when the first answer carries this, truthy
    vol.Optional(ATTR_UNLESS): cv.string,
})

START_SCHEMA = vol.Schema({
    vol.Required(ATTR_TITLE): cv.string,
    vol.Required(ATTR_ACTION): cv.string,
    vol.Optional(ATTR_DATA, default=dict): dict,
    vol.Optional(ATTR_CARD, default=""): cv.string,
    vol.Optional(ATTR_TAB, default="kitchen"): vol.In(TABS),
    # the key in the answer that names what came back, for the row
    vol.Optional(ATTR_LABEL): cv.string,
    vol.Optional(ATTR_THEN): THEN_SCHEMA,
    # Whether the card can show the answer. A task with nothing to show
    # -- a plan written straight onto the week -- gets Dismiss and no Open.
    vol.Optional(ATTR_OPEN, default=True): cv.boolean,
})

RESULT_SCHEMA = vol.Schema({vol.Required(ATTR_TASK_ID): cv.string})


def _split(action: str) -> tuple[str, str]:
    domain, _, service = action.partition(".")
    if not domain or not service:
        raise ServiceValidationError(f"{action!r} is not an action")
    return domain, service


class AiTasksSensor(SensorEntity):
    """Every AI task a card started, and what each one came back with."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "AI tasks"
    _attr_icon = "mdi:creation-outline"

    # Rows can land on any tab -- the card that started the task says which
    # -- so this owner reports per tab (`by_tab`) rather than having one.
    tab = ""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ai_tasks"
        self._store: Store | None = None
        self._tasks: list[dict[str, Any]] = []
        # task id -> {"result": ..., "then": ...}. Kept apart from the tasks
        # so the state attributes stay small: an answer can be a whole
        # recipe, and the recorder has no business keeping that.
        self._results: dict[str, dict[str, Any]] = {}
        self._listeners: list[Any] = []
        self._unexpire: Any = None

    # --- plumbing -----------------------------------------------------

    @callback
    def add_listener(self, listener: Any) -> None:
        self._listeners.append(listener)

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()
        for listener in self._listeners:
            listener.refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._store = Store(self.hass, STORE_VERSION, f"{DOMAIN}.ai_tasks")
        data = await self._store.async_load() or {}
        self._tasks = [dict(t) for t in data.get("tasks") or [] if isinstance(t, dict)]
        self._results = dict(data.get("results") or {})
        changed = False
        for task in self._tasks:
            if task.get("state") == RUNNING:
                task["state"] = FAILED
                task["error"] = "Home Assistant restarted while this was running"
                task["finished"] = dt_util.utcnow().isoformat()
                changed = True
        if self._prune() or changed:
            await self._save()
        self.async_on_remove(self._cancel_expiry)
        self._arm_expiry()

    @callback
    def _cancel_expiry(self) -> None:
        if self._unexpire is not None:
            self._unexpire()
            self._unexpire = None

    @callback
    def _arm_expiry(self) -> None:
        """Wake when the oldest finished task stops being news."""
        self._cancel_expiry()
        due = [
            when + KEEP_FOR
            for t in self._tasks
            if t.get("state") != RUNNING
            and (when := dt_util.parse_datetime(t.get("finished") or "")) is not None
        ]
        if due and self.hass is not None:
            self._unexpire = async_track_point_in_time(self.hass, self._async_expire, min(due))

    async def _async_expire(self, now: datetime) -> None:
        self._unexpire = None
        if self._prune(now):
            await self._save()
            self.async_write_ha_state()
        self._arm_expiry()

    async def _save(self) -> None:
        if self._store is not None:
            await self._store.async_save({"tasks": self._tasks, "results": self._results})

    def _prune(self, now: datetime | None = None) -> bool:
        """Drop finished tasks once they stop being news, and any past the cap."""
        now = now or dt_util.utcnow()
        before = len(self._tasks)

        def fresh(task: dict[str, Any]) -> bool:
            if task.get("state") == RUNNING:
                return True
            when = dt_util.parse_datetime(task.get("finished") or "")
            return when is None or now - when < KEEP_FOR

        kept = [t for t in self._tasks if fresh(t)]
        finished = [t for t in kept if t.get("state") != RUNNING]
        if len(finished) > KEEP_MAX:
            drop = {t["id"] for t in finished[: len(finished) - KEEP_MAX]}
            kept = [t for t in kept if t["id"] not in drop]
        self._tasks = kept
        live = {t["id"] for t in kept}
        self._results = {k: v for k, v in self._results.items() if k in live}
        return len(kept) != before

    def _find(self, task_id: str) -> dict[str, Any] | None:
        return next((t for t in self._tasks if t["id"] == task_id), None)

    # --- the work -----------------------------------------------------

    async def async_start(self, data: dict[str, Any]) -> dict[str, Any]:
        """Take the work and answer at once with its id."""
        _split(data[ATTR_ACTION])
        if (then := data.get(ATTR_THEN)) is not None:
            _split(then[ATTR_ACTION])
        task = {
            "id": f"{_ID_PREFIX}{uuid.uuid4().hex[:12]}",
            "title": data[ATTR_TITLE],
            "card": data.get(ATTR_CARD) or "",
            "tab": data.get(ATTR_TAB) or "kitchen",
            "state": RUNNING,
            "started": dt_util.utcnow().isoformat(),
            "finished": None,
            "step": 1,
            "steps": 2 if then else 1,
            "label": None,
            "error": None,
            "open": bool(data.get(ATTR_OPEN, True)),
        }
        self._tasks.append(task)
        self._prune()
        await self._save()
        self.async_write_ha_state()
        self.hass.async_create_background_task(
            self._run(task, dict(data)), f"{DOMAIN} {task['id']}"
        )
        return {ATTR_TASK_ID: task["id"]}

    async def _call(self, action: str, data: dict[str, Any]) -> Any:
        domain, service = _split(action)
        async with asyncio.timeout(TIMEOUT.total_seconds()):
            return await self.hass.services.async_call(
                domain, service, data, blocking=True, return_response=True
            )

    async def _run(self, task: dict[str, Any], data: dict[str, Any]) -> None:
        result: Any = None
        then_result: Any = None
        try:
            result = await self._call(data[ATTR_ACTION], data.get(ATTR_DATA) or {})
            label_key = data.get(ATTR_LABEL)
            if label_key and isinstance(result, dict) and result.get(label_key):
                task["label"] = str(result[label_key])
            then = data.get(ATTR_THEN)
            if then and self._wants_then(then, result):
                task["step"] = 2
                self.async_write_ha_state()
                fed = {
                    field: result[key]
                    for field, key in (then.get(ATTR_PASS) or {}).items()
                }
                then_result = await self._call(
                    then[ATTR_ACTION], {**(then.get(ATTR_DATA) or {}), **fed}
                )
            task["state"] = DONE
        except TimeoutError:
            task["state"] = FAILED
            task["error"] = "No answer after ten minutes"
        except (HomeAssistantError, vol.Invalid, ValueError, KeyError) as err:
            task["state"] = FAILED
            task["error"] = str(err) or type(err).__name__
        except Exception as err:  # noqa: BLE001 -- a task must always finish
            LOGGER.exception("AI task %s failed", task["id"])
            task["state"] = FAILED
            task["error"] = str(err) or type(err).__name__
        # A first step that answered is worth keeping even when the second
        # fell over: the recipe is in the box, it is only not split.
        if task["state"] == FAILED and result is not None:
            task["state"] = DONE
            task["partial"] = True
        task["finished"] = dt_util.utcnow().isoformat()
        if self._find(task["id"]) is None:
            # Dismissed while it ran: somebody has already moved on.
            return
        self._results[task["id"]] = {"result": result, "then": then_result}
        await self._save()
        self.async_write_ha_state()
        self._arm_expiry()

    @staticmethod
    def _wants_then(then: dict[str, Any], result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        unless = then.get(ATTR_UNLESS)
        if unless and result.get(unless):
            return False
        return all(key in result for key in (then.get(ATTR_PASS) or {}).values())

    def result(self, task_id: str) -> dict[str, Any]:
        task = self._find(task_id)
        if task is None:
            raise ServiceValidationError(f"No AI task {task_id!r}")
        return {"task": dict(task), **self._results.get(task_id, {})}

    # --- what Needs you reads ------------------------------------------

    def owns(self, item_id: str) -> bool:
        return item_id.startswith(_ID_PREFIX)

    @callback
    def dismiss(self, item_id: str) -> None:
        """Done: the answer has been seen. Card, tab and row clear together."""
        self._tasks = [t for t in self._tasks if t["id"] != item_id]
        self._results.pop(item_id, None)
        self.hass.async_create_task(self._save())
        self.async_write_ha_state()
        self._arm_expiry()

    def needs_you_rows(self) -> list[dict[str, Any]]:
        rows = []
        for task in self._tasks:
            if task["state"] == RUNNING:
                continue
            # Every finished task says which way it went, in words and in
            # its icon, before anything else: "Done" or "Failed".
            ok = task["state"] == DONE
            dismiss = {
                "service": f"{DOMAIN}.{SERVICE_DISMISS}",
                "data": {"item_id": task["id"]},
            }
            if ok:
                detail = f"Done \u00b7 {task['label']}" if task.get("label") else "Done"
                if task.get("partial"):
                    detail += " \u00b7 the second step did not finish"
            else:
                detail = f"Failed \u00b7 {task.get('error') or 'no answer'}"
            rows.append({
                "id": task["id"],
                "title": task["title"],
                "detail": detail,
                "outcome": "success" if ok else "failure",
                "icon": "mdi:check-circle-outline" if ok else "mdi:alert-circle-outline",
                "level": LEVEL_NOTICE,
                "tab": task["tab"],
                "card": task["card"],
                "expires": self._expires(task),
                # Two buttons. Dismiss clears it everywhere; Open shows the
                # answer, and is there only when there is one to show and a
                # card to show it. Opening it is a thing only a screen can
                # do, so Open carries what the card needs to find it rather
                # than a service.
                **(
                    {
                        "action_label": "Open",
                        "action": {"open_task": task["id"], "card": task["card"], "tab": task["tab"]},
                        "secondary_label": "Dismiss",
                        "secondary_action": dismiss,
                    }
                    if ok and task.get("open", True) and task["card"]
                    else {"action_label": "Dismiss", "action": dismiss}
                ),
            })
        return rows

    @staticmethod
    def _expires(task: dict[str, Any]) -> str | None:
        when = dt_util.parse_datetime(task.get("finished") or "")
        return (when + KEEP_FOR).isoformat() if when else None

    def by_tab(self) -> list[tuple[str, str | None, list[dict[str, Any]]]]:
        """(tab, level, jobs) per tab with a finished task on it."""
        tabs: dict[str, list[dict[str, Any]]] = {}
        for row in self.needs_you_rows():
            tabs.setdefault(row["tab"], []).append(row)
        return [(tab, LEVEL_NOTICE, rows) for tab, rows in tabs.items()]

    @property
    def owner_level(self) -> str | None:
        return LEVEL_NOTICE if self.needs_you_rows() else None

    # --- the entity ---------------------------------------------------

    @property
    def native_value(self) -> str:
        if any(t["state"] == RUNNING for t in self._tasks):
            return RUNNING
        return self.owner_level or CLEAR

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        jobs = self.needs_you_rows()
        cards: dict[str, str] = {}
        for row in jobs:
            if row["card"]:
                cards[row["card"]] = LEVEL_NOTICE
        return {
            "level": self.owner_level,
            "jobs": jobs,
            # Each card's level, by the `card` key it started the task with,
            # for the card's outline. Only ever `notice`: kept as a map so a
            # card reads its own and nobody else's.
            "cards": cards,
            "tasks": [
                {k: t.get(k) for k in (
                    "id", "title", "card", "tab", "state", "started",
                    "finished", "step", "steps", "label", "error", "partial", "open",
                )} | {"expires": self._expires(t)}
                for t in self._tasks
            ],
            "running": sum(1 for t in self._tasks if t["state"] == RUNNING),
        }


def async_register_ai_task_services(hass: HomeAssistant, tasks: AiTasksSensor) -> None:
    """Registered afresh each setup, so a reload points them at the new sensor."""

    async def _start(call: ServiceCall) -> ServiceResponse:
        return await tasks.async_start(dict(call.data))

    async def _result(call: ServiceCall) -> ServiceResponse:
        return tasks.result(call.data[ATTR_TASK_ID])

    hass.services.async_register(
        DOMAIN, SERVICE_START_AI_TASK, _start,
        schema=START_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_AI_TASK_RESULT, _result,
        schema=RESULT_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
