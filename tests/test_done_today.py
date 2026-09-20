"""What got ticked off today, on two lists that answer very differently.

Bring records no completion time at all, so the eighteen completed items
on the real shopping list are eighteen items ticked off at some unknown
point over some unknown number of days. `local_todo` stamps each one,
because iCalendar has a field for it.

The failure that matters is the same in both directions: a section headed
"today" showing something that did not happen today. For Bring that means
the first read after a restart must be a CENSUS and not a day's work --
get that wrong and the panel opens with a fortnight of shopping under
today's heading. For a list that does stamp, the stamp has to be believed
over the moment we happened to look, or a restart re-dates the morning's
work to the afternoon.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.home_signals.const import DOMAIN
from custom_components.home_signals.todo_done import TodoDoneTodaySensor

LIST = "todo.shopping"


def _item(uid: str, summary: str, completed: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"uid": uid, "summary": summary, "status": "completed"}
    if completed is not None:
        item["completed"] = completed
    return item


class _Lists:
    """Stands in for todo.get_items, which is a service call with a response.

    Only the response is faked. Everything else -- the state change that
    provokes a read, the midnight timer, the restore -- runs for real.
    """

    def __init__(self, hass: HomeAssistant, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.calls = 0
        hass.services.async_register(
            "todo",
            "get_items",
            self._handle,
            supports_response="only",
        )

    async def _handle(self, call) -> dict[str, Any]:
        self.calls += 1
        return {LIST: {"items": list(self.items)}}


async def _sensor(
    hass: HomeAssistant,
    items: list[dict[str, Any]],
    outstanding: str = "3",
) -> tuple[TodoDoneTodaySensor, _Lists]:
    lists = _Lists(hass, items)
    hass.states.async_set(LIST, outstanding)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    sensor = TodoDoneTodaySensor(entry, LIST, "Shopping")
    sensor.hass = hass
    sensor.entity_id = "sensor.shopping_done_today"
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return sensor, lists


def _names(sensor: TodoDoneTodaySensor) -> list[str]:
    return [row["summary"] for row in sensor.extra_state_attributes["items"]]


async def _tick(hass: HomeAssistant, lists: _Lists, item: dict[str, Any]) -> None:
    """Tick something off: it joins the completed set and the count moves."""
    lists.items.append(item)
    state = hass.states.get(LIST)
    hass.states.async_set(LIST, str(int(state.state) - 1))
    await hass.async_block_till_done()


async def test_a_list_with_no_timestamps_starts_empty_not_full(
    hass: HomeAssistant,
) -> None:
    """The Bring case, and the one that would embarrass the panel.

    Eighteen items are already completed when the integration loads. Not
    one of them carries a time, and none of them happened because of
    anything we saw. Stamping them "now" would be inventing a day's work.
    """
    sensor, _ = await _sensor(
        hass, [_item(f"u{n}", f"Item {n}") for n in range(18)]
    )

    assert _names(sensor) == [], (
        "the first read after a restart was counted as today's work"
    )
    assert sensor.native_value == 0


async def test_and_then_records_what_is_actually_ticked(
    hass: HomeAssistant,
) -> None:
    sensor, lists = await _sensor(hass, [_item("old", "Bought last week")])
    await _tick(hass, lists, _item("new", "Milk"))

    assert _names(sensor) == ["Milk"], (
        "a tick after the census was not recorded, or the census leaked into it"
    )


async def test_an_untick_takes_it_back_off_the_day(hass: HomeAssistant) -> None:
    """Putting something back on the list is as real an act as ticking it."""
    sensor, lists = await _sensor(hass, [])
    await _tick(hass, lists, _item("u1", "Milk"))
    assert _names(sensor) == ["Milk"]

    lists.items = []
    hass.states.async_set(LIST, "4")
    await hass.async_block_till_done()

    assert _names(sensor) == [], "an un-ticked item stayed in today's record"


async def test_a_stamped_item_is_believed_over_when_we_looked(
    hass: HomeAssistant,
) -> None:
    """The local_todo case.

    An item completed at nine this morning, read for the first time at
    two in the afternoon because that is when Home Assistant restarted,
    belongs under today -- at nine. The census rule must not swallow it,
    and the time recorded must be its own.
    """
    morning = dt_util.now().replace(hour=9, minute=0, second=0, microsecond=0)
    sensor, _ = await _sensor(
        hass,
        [
            _item("stamped", "Hang the washing", morning.isoformat()),
            _item("bare", "Something from who knows when"),
        ],
    )

    rows = sensor.extra_state_attributes["items"]
    assert [row["summary"] for row in rows] == ["Hang the washing"], (
        "a stamped item from today was lost to the census rule, "
        "or an unstamped one was let through it"
    )
    recorded = dt_util.parse_datetime(rows[0]["completed"])
    assert dt_util.as_local(recorded) == morning, (
        "the item was re-dated to when we read it rather than when it was done"
    )


async def test_a_stamp_from_yesterday_is_not_today(hass: HomeAssistant) -> None:
    yesterday = dt_util.now() - timedelta(days=1)
    sensor, _ = await _sensor(
        hass, [_item("old", "Yesterday's job", yesterday.isoformat())]
    )

    assert _names(sensor) == [], "yesterday's work was counted as today's"


async def test_midnight_empties_the_section(hass: HomeAssistant) -> None:
    """The section is about a day, so it ends when the day does."""
    sensor, lists = await _sensor(hass, [])
    await _tick(hass, lists, _item("u1", "Milk"))
    assert _names(sensor) == ["Milk"]

    midnight = (dt_util.now() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    async_fire_time_changed(hass, midnight + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert _names(sensor) == [], "the section did not empty at midnight"


async def test_a_restart_keeps_the_morning(hass: HomeAssistant) -> None:
    """Restarting at lunchtime must not lose what was done before it.

    The record is restored, and the census that follows must not then
    drop it back out again for having no stamp of its own.
    """
    earlier = dt_util.utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    mock_restore_cache(
        hass,
        (
            State(
                "sensor.shopping_done_today",
                "1",
                {
                    "items": [
                        {
                            "uid": "u1",
                            "summary": "Milk",
                            "description": "",
                            "status": "completed",
                            "completed": earlier.isoformat(),
                        }
                    ]
                },
            ),
        ),
    )
    sensor, _ = await _sensor(hass, [_item("u1", "Milk")])

    assert _names(sensor) == ["Milk"], (
        "the morning's work was lost across a restart"
    )


async def test_a_restored_day_that_has_since_turned_is_dropped(
    hass: HomeAssistant,
) -> None:
    """Nothing was running at midnight, so no timer cleared it.

    Restoring must therefore filter as well, or the panel comes up after
    an overnight reboot showing yesterday under today's heading.
    """
    yesterday = dt_util.utcnow() - timedelta(days=1)
    mock_restore_cache(
        hass,
        (
            State(
                "sensor.shopping_done_today",
                "1",
                {
                    "items": [
                        {
                            "uid": "u1",
                            "summary": "Yesterday's milk",
                            "status": "completed",
                            "completed": yesterday.isoformat(),
                        }
                    ]
                },
            ),
        ),
    )
    sensor, _ = await _sensor(hass, [_item("u1", "Yesterday's milk")])

    assert _names(sensor) == [], (
        "an overnight restart brought yesterday back under today's heading"
    )


async def test_an_unreadable_list_says_nothing_rather_than_zero(
    hass: HomeAssistant,
) -> None:
    """A list that cannot be read has no answer, and must not invent one.

    Specifically it must not treat "no response" as "nothing is completed"
    and wipe the day's record.
    """
    sensor, lists = await _sensor(hass, [])
    await _tick(hass, lists, _item("u1", "Milk"))
    assert _names(sensor) == ["Milk"]

    hass.services.async_remove("todo", "get_items")
    hass.states.async_set(LIST, "9")
    await hass.async_block_till_done()

    assert _names(sensor) == ["Milk"], (
        "a failed read was taken as proof that nothing is done"
    )
