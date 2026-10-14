"""Chores on a Trello To-do board: which ask for something, and how loudly.

A chore due today is ochre until its time and orange after it. One carried
over from an earlier day stays orange until somebody ticks it. A chore not
due until tomorrow, or already ticked, asks for nothing. The row's Done
ticks the card, the same as ticking it on the panel.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.home_signals.const import (
    DOMAIN,
    LEVEL_ATTENTION,
    LEVEL_WAITING,
)
from custom_components.home_signals.derived import (
    NeedsYouSensor,
    TasksStatusSensor,
    chore_rows,
)
from tests.owners import attach, owner

TODO = "todo.chores"
TZ = ZoneInfo("Europe/London")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """October 2026: the 14th is a Wednesday."""
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def card(cid: str, due: datetime | None, done: bool = False, members=(), routine=True):
    return {
        "id": cid, "name": cid.replace("_", " ").capitalize(), "list": "l1",
        "members": list(members), "labels": [],
        "due": due.isoformat() if due else None, "done": done,
        "routine": [0, 1, 2, 3, 4, 5, 6] if routine else None,
        "desc": "", "url": "",
    }


def board(*cards):
    return {
        "done": "l1", "default": "l1", "lists": [{"id": "l1", "name": "Todo"}],
        "members": [{"id": "m1", "name": "Casey"}, {"id": "m2", "name": "Morgan"}],
        "labels": [], "cards": list(cards),
    }


@pytest.fixture(autouse=True)
async def _london(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/London")


def _rows(hass: HomeAssistant, now: datetime, *cards) -> list[dict]:
    hass.states.async_set(TODO, str(len(cards)), {"board": board(*cards)})
    return chore_rows(hass.states.get(TODO), now)


async def test_due_today_is_ochre_until_its_time_then_orange(hass: HomeAssistant) -> None:
    dishes = card("dishwasher", at(14, 19), members=["m1"])
    (row,) = _rows(hass, at(14, 18, 30), dishes)
    assert row["level"] == LEVEL_ATTENTION
    assert row["detail"] == "By 19:00 · Casey"

    (row,) = _rows(hass, at(14, 19, 54), dishes)
    assert row["level"] == LEVEL_WAITING
    assert row["detail"] == "Was due 19:00 · Casey"


async def test_carried_over_stays_orange_and_says_which_day(hass: HomeAssistant) -> None:
    (row,) = _rows(hass, at(15, 9), card("bins", at(12, 19)))
    assert row["level"] == LEVEL_WAITING
    assert row["detail"] == "Was due Mon 19:00"


async def test_ticked_tomorrow_and_undated_ask_for_nothing(hass: HomeAssistant) -> None:
    rows = _rows(
        hass, at(14, 20),
        card("ticked", at(14, 19), done=True),
        card("tomorrow", at(15, 19)),
        card("undated", None, routine=False),
    )
    assert rows == []


async def test_rows_are_soonest_first_and_keyed_by_day(hass: HomeAssistant) -> None:
    rows = _rows(hass, at(14, 8), card("late", at(14, 21)), card("early", at(14, 9)))
    assert [r["title"] for r in rows] == ["Early", "Late"]
    # A dismissal of today's row must not hide tomorrow's.
    assert rows[0]["id"] == "chore_early_2026-10-14"


async def test_done_ticks_the_card(hass: HomeAssistant) -> None:
    (row,) = _rows(hass, at(14, 20), card("dishwasher", at(14, 19)))
    assert row["action_label"] == "Done"
    assert row["action"] == {
        "service": "todo.update_item",
        "target": {"entity_id": TODO},
        "data": {"item": "dishwasher", "status": "completed"},
    }


async def test_a_board_less_list_raises_nothing(hass: HomeAssistant) -> None:
    hass.states.async_set(TODO, "3", {})
    assert chore_rows(hass.states.get(TODO), at(14, 20)) == []


def _needs(hass: HomeAssistant) -> NeedsYouSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"tasks_sensor": TODO})
    entry.add_to_hass(hass)
    needs = NeedsYouSensor(entry)
    needs.hass = hass
    needs.entity_id = "sensor.needs_you"
    attach(hass, needs)
    return needs


async def test_card_outline_tab_and_needs_you_follow(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(at(14, 20))
    hass.states.async_set(TODO, "1", {"board": board(card("dishwasher", at(14, 19)))})
    needs = _needs(hass)
    tasks = owner(needs, TasksStatusSensor)

    assert tasks.native_value == LEVEL_WAITING
    needs._recompute()  # noqa: SLF001
    attrs = needs.extra_state_attributes
    assert attrs["tab_lists"] == LEVEL_WAITING
    assert any(r["id"] == "chore_dishwasher_2026-10-14" for r in attrs["items"])


async def test_turns_orange_at_the_deadline_not_at_the_next_scan(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(at(14, 18, 58))
    hass.states.async_set(TODO, "1", {"board": board(card("dishwasher", at(14, 19)))})
    needs = _needs(hass)
    tasks = owner(needs, TasksStatusSensor)
    await tasks.async_added_to_hass()
    await hass.async_block_till_done()
    assert tasks.native_value == LEVEL_ATTENTION

    # Two minutes on: past the deadline, well inside the five-minute scan.
    freezer.move_to(at(14, 19, 0, ) + timedelta(seconds=2))
    async_fire_time_changed(hass, at(14, 19) + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert tasks.state == LEVEL_WAITING


async def test_ticking_it_clears_the_row(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(at(14, 20))
    hass.states.async_set(TODO, "1", {"board": board(card("dishwasher", at(14, 19)))})
    needs = _needs(hass)
    tasks = owner(needs, TasksStatusSensor)
    assert tasks.native_value == LEVEL_WAITING

    hass.states.async_set(TODO, "0", {"board": board(card("dishwasher", at(14, 19), done=True))})
    assert tasks.native_value == "clear"
