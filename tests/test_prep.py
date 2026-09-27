"""Prep sessions: one Home Tasks item each, and a level only while it matters.

The list is stood in for by services over a plain dict, because what is under
test is what this asks the list to do and how it reads the answer back --
not a todo integration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import (
    DOMAIN,
    LEVEL_ATTENTION,
    LEVEL_WAITING,
)
from custom_components.home_signals.derived import NeedsYouSensor
from custom_components.home_signals.prep import (
    MealPrepSensor,
    async_register_prep_services,
    session_title,
)

TODO = "todo.home_tasks"
LONDON = ZoneInfo("Europe/London")


class FakeList:
    def __init__(self, hass: HomeAssistant) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.n = 0
        hass.states.async_set(TODO, "0")

        def _get(call: ServiceCall) -> dict:
            return {TODO: {"items": list(self.items.values())}}

        def _add(call: ServiceCall) -> None:
            self.n += 1
            uid = f"uid{self.n}"
            self.items[uid] = {
                "uid": uid,
                "summary": call.data["item"],
                "status": "needs_action",
                "due": call.data.get("due_datetime"),
                "description": call.data.get("description"),
            }

        def _update(call: ServiceCall) -> None:
            item = self.items[call.data["item"]]
            if "rename" in call.data:
                item["summary"] = call.data["rename"]
            for key, to in (("status", "status"), ("due_datetime", "due"), ("description", "description")):
                if key in call.data:
                    item[to] = call.data[key]

        def _remove(call: ServiceCall) -> None:
            for uid in call.data["item"]:
                self.items.pop(uid, None)

        hass.services.async_register(
            "todo", "get_items", _get, supports_response=SupportsResponse.ONLY
        )
        hass.services.async_register("todo", "add_item", _add)
        hass.services.async_register("todo", "update_item", _update)
        hass.services.async_register("todo", "remove_item", _remove)


async def _setup(hass: HomeAssistant) -> tuple[MealPrepSensor, NeedsYouSensor, FakeList]:
    await hass.config.async_set_time_zone("Europe/London")
    todo = FakeList(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    prep = MealPrepSensor(entry)
    prep.hass = hass
    prep.entity_id = "sensor.meal_prep"
    needs = NeedsYouSensor(entry)
    needs.hass = hass
    needs.entity_id = "sensor.needs_you"
    needs.prep = prep
    await prep.async_added_to_hass()
    await needs.async_added_to_hass()
    async_register_prep_services(hass, prep)
    return prep, needs, todo


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=LONDON)


CHILLI = {"date": "2026-09-29", "entry_type": "dinner", "name": "Chilli", "steps": ["Chop onions"], "minutes": 15}
FAJITAS = {"date": "2026-09-30", "entry_type": "dinner", "name": "Fajitas", "steps": ["Slice peppers"]}


async def _save(hass: HomeAssistant, **data: Any) -> dict:
    return await hass.services.async_call(
        DOMAIN, "save_prep_session", data, blocking=True, return_response=True
    )


def test_the_title_names_each_meal_once() -> None:
    assert session_title([CHILLI]) == "Prep: Chilli"
    assert session_title([FAJITAS, CHILLI]) == "Prep: Chilli and Fajitas"
    third = {**CHILLI, "date": "2026-10-01", "name": "Curry"}
    assert session_title([CHILLI, FAJITAS, third, CHILLI]) == "Prep: Chilli, Fajitas and Curry"


async def test_a_session_is_one_task_with_a_deadline(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_at("2026-09-27T10:00"))
    prep, _needs, todo = await _setup(hass)

    out = await _save(hass, due="2026-09-27 16:00", items=[CHILLI, FAJITAS])

    assert len(todo.items) == 1
    task = todo.items[out["task_uid"]]
    assert task["summary"] == "Prep: Chilli and Fajitas"
    assert task["due"].startswith("2026-09-27T16:00")
    assert "For Tue dinner · Chilli · about 15 min\n- Chop onions" in task["description"]

    # Saving it again moves the same task rather than adding another.
    again = await _save(hass, id=out["id"], due="2026-09-28 19:30", items=[CHILLI])
    assert again["task_uid"] == out["task_uid"]
    assert len(todo.items) == 1
    assert todo.items[out["task_uid"]]["summary"] == "Prep: Chilli"
    assert prep.extra_state_attributes["sessions"][0]["due"].startswith("2026-09-28T19:30")


async def test_due_today_is_attention_late_with_a_meal_ahead_is_waiting(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_at("2026-09-26T10:00"))
    prep, needs, _todo = await _setup(hass)
    out = await _save(hass, due="2026-09-27 16:00", items=[CHILLI])

    def rows() -> list[dict]:
        needs._recompute()
        return [r for r in needs.extra_state_attributes["items"] if r["id"].startswith("prep_")]

    # The day before: a fact, not a job.
    assert prep.extra_state_attributes["level"] is None
    assert rows() == []

    freezer.move_to(_at("2026-09-27T09:00"))
    await prep.async_sync()
    assert prep.extra_state_attributes["level"] == LEVEL_ATTENTION
    (row,) = rows()
    assert row["level"] == LEVEL_ATTENTION
    assert row["detail"] == "By 16:00 · for Tue dinner"
    assert row["action"]["data"] == {"id": out["id"]}

    freezer.move_to(_at("2026-09-28T12:00"))
    await prep.async_sync()
    assert rows()[0]["level"] == LEVEL_WAITING

    # Once the meal has been and gone, doing the prep changes nothing.
    freezer.move_to(_at("2026-09-29T18:00"))
    await prep.async_sync()
    assert rows() == []
    assert prep.extra_state_attributes["level"] is None


async def test_ticking_the_task_anywhere_clears_it(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_at("2026-09-27T17:00"))
    prep, _needs, todo = await _setup(hass)
    out = await _save(hass, due="2026-09-27 16:00", items=[CHILLI])
    assert prep.extra_state_attributes["level"] == LEVEL_WAITING

    await hass.services.async_call(DOMAIN, "prep_done", {"id": out["id"]}, blocking=True)
    assert todo.items[out["task_uid"]]["status"] == "completed"
    assert prep.extra_state_attributes["sessions"][0]["done"] is True
    assert prep.extra_state_attributes["level"] is None

    # A meal added afterwards is new prep, so the task opens again.
    await _save(hass, id=out["id"], due="2026-09-28 19:30", items=[CHILLI, FAJITAS])
    assert todo.items[out["task_uid"]]["status"] == "needs_action"


async def test_a_deleted_task_is_an_answer_not_a_job(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_at("2026-09-27T17:00"))
    prep, _needs, todo = await _setup(hass)
    out = await _save(hass, due="2026-09-27 16:00", items=[CHILLI])
    todo.items.clear()
    await prep.async_sync()
    assert prep.extra_state_attributes["sessions"][0]["done"] is True
    assert prep.extra_state_attributes["level"] is None
    assert out["id"]


async def test_removing_takes_the_task_with_it_and_old_sessions_go(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_at("2026-09-27T10:00"))
    prep, _needs, todo = await _setup(hass)
    one = await _save(hass, due="2026-09-27 16:00", items=[CHILLI])
    await _save(hass, due="2026-09-28 19:30", items=[FAJITAS])
    assert len(todo.items) == 2

    await hass.services.async_call(DOMAIN, "remove_prep_session", {"id": one["id"]}, blocking=True)
    assert len(todo.items) == 1
    assert len(prep.extra_state_attributes["sessions"]) == 1

    # An empty save is a removal too.
    other = prep.extra_state_attributes["sessions"][0]["id"]
    await _save(hass, id=other, due="2026-09-28 19:30", items=[])
    assert todo.items == {}

    await _save(hass, due="2026-09-27 16:00", items=[CHILLI])
    freezer.move_to(_at("2026-10-01T10:00"))
    await prep.async_sync()
    assert prep.extra_state_attributes["sessions"] == []


async def test_settings_move_meal_times(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_at("2026-09-29T17:30"))
    prep, _needs, _todo = await _setup(hass)
    await _save(hass, due="2026-09-29 16:00", items=[CHILLI])
    # Dinner at 17:00 has passed, so there is nothing left to prep for.
    assert prep.extra_state_attributes["level"] is None

    await hass.services.async_call(
        DOMAIN, "prep_settings", {"meal_times": {"dinner": "18:30"}}, blocking=True
    )
    attrs = prep.extra_state_attributes
    assert attrs["meal_times"]["dinner"] == "18:30"
    assert attrs["meal_times"]["breakfast"] == "07:00"
    assert attrs["level"] == LEVEL_WAITING
    assert attrs["prep_times"][0]["days"] == [6]
