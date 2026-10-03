"""AI tasks: running is a fact, finished is a notice, and all three clear together.

Driven through the real config entry and the real services, because the
whole point is the wiring: a task started by a card has to colour that
card (`cards`), its tab's rail button (`tab_<tab>` on Needs you) and put a
row in Needs you -- and Done on the row has to clear every one of them.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.home_signals.const import (
    DOMAIN,
    LEVEL_ATTENTION,
    LEVEL_CRITICAL,
    LEVEL_NOTICE,
    LEVEL_WAITING,
)
from custom_components.home_signals.derived import NeedsYouSensor, loudest

TASKS = "sensor.ai_tasks"
NEEDS = "sensor.needs_you"


async def _setup(hass: HomeAssistant) -> asyncio.Event:
    """The integration, and two fake scripts: an import and a split.

    The import waits on the returned event, so a test can look at the
    house while the task is still running.
    """
    release = asyncio.Event()
    calls: list[tuple[str, dict[str, Any]]] = []
    hass.data["ai_calls"] = calls

    async def _import(call: ServiceCall) -> dict[str, Any]:
        calls.append(("import", dict(call.data)))
        await release.wait()
        if call.data.get("url") == "bad":
            raise ValueError("No recipe on that page")
        return {"recipe": "Chicken pie", "slug": "chicken-pie",
                "already": call.data.get("url") == "again"}

    async def _split(call: ServiceCall) -> dict[str, Any]:
        calls.append(("split", dict(call.data)))
        return {"mode": "split", "prep": [{"text": "Make the pastry"}]}

    hass.services.async_register(
        "script", "recipe_import", _import, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        "script", "recipe_split", _split, supports_response=SupportsResponse.ONLY
    )
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return release


async def _start(hass: HomeAssistant, url: str = "https://x") -> str:
    out = await hass.services.async_call(
        DOMAIN, "start_ai_task",
        {
            "title": "Recipe from a link",
            "action": "script.recipe_import",
            "data": {"url": url},
            "card": "meals",
            "tab": "kitchen",
            "label": "recipe",
            "then": {
                "action": "script.recipe_split",
                "pass": {"recipe": "slug"},
                "unless": "already",
            },
        },
        blocking=True, return_response=True,
    )
    return out["task_id"]


async def test_running_is_a_fact_not_a_job(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    task_id = await _start(hass)
    await hass.async_block_till_done(wait_background_tasks=False)

    state = hass.states.get(TASKS)
    assert state.state == "running"
    assert state.attributes["running"] == 1
    assert state.attributes["tasks"][0]["id"] == task_id
    assert state.attributes["cards"] == {}, "a running task coloured its card"
    needs = hass.states.get(NEEDS).attributes
    assert needs["items"] == [], "a running task raised a row"
    assert needs["tab_kitchen"] is None, "a running task coloured the rail"

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_finished_is_a_notice_on_all_three(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    task_id = await _start(hass)
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(TASKS)
    assert state.state == LEVEL_NOTICE
    assert state.attributes["cards"] == {"meals": LEVEL_NOTICE}
    needs = hass.states.get(NEEDS).attributes
    assert needs["tab_kitchen"] == LEVEL_NOTICE
    [row] = [r for r in needs["items"] if r["id"] == task_id]
    assert row["level"] == LEVEL_NOTICE
    assert row["detail"] == "Done · Chicken pie"
    assert row["outcome"] == "success"
    # Two buttons: Open shows the answer, Dismiss clears it everywhere.
    assert row["action_label"] == "Open"
    assert row["action"] == {"open_task": task_id, "card": "meals", "tab": "kitchen"}
    assert row["secondary_label"] == "Dismiss"
    assert row["secondary_action"] == {
        "service": f"{DOMAIN}.dismiss", "data": {"item_id": task_id},
    }

    # The split ran, fed the import's slug.
    assert hass.data["ai_calls"][-1] == ("split", {"recipe": "chicken-pie"})
    got = await hass.services.async_call(
        DOMAIN, "ai_task_result", {"task_id": task_id},
        blocking=True, return_response=True,
    )
    assert got["result"]["slug"] == "chicken-pie"
    assert got["then"]["mode"] == "split"
    assert "result" not in state.attributes["tasks"][0], "an answer leaked into the state"


async def test_done_clears_card_rail_and_row_together(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    task_id = await _start(hass)
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    await hass.services.async_call(DOMAIN, "dismiss", {"item_id": task_id}, blocking=True)
    await hass.async_block_till_done()

    state = hass.states.get(TASKS)
    assert state.state == "clear"
    assert state.attributes["cards"] == {}
    needs = hass.states.get(NEEDS).attributes
    assert needs["tab_kitchen"] is None
    assert not [r for r in needs["items"] if r["id"] == task_id]


async def test_a_snooze_hides_the_row_but_the_tab_stays_blue(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    task_id = await _start(hass)
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    await hass.services.async_call(
        DOMAIN, "snooze", {"item_id": task_id, "hours": 1}, blocking=True
    )
    needs = hass.states.get(NEEDS).attributes
    assert not [r for r in needs["items"] if r["id"] == task_id]
    assert needs["tab_kitchen"] == LEVEL_NOTICE


async def test_a_split_that_fails_keeps_the_import_and_says_so(hass: HomeAssistant) -> None:
    release = await _setup(hass)

    async def _broken(call: ServiceCall) -> dict[str, Any]:
        raise ValueError("model said nothing")

    hass.services.async_register(
        "script", "recipe_split", _broken, supports_response=SupportsResponse.ONLY
    )
    task_id = await _start(hass)
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    needs = hass.states.get(NEEDS).attributes
    [row] = [r for r in needs["items"] if r["id"] == task_id]
    assert row["outcome"] == "success"
    assert row["detail"] == "Done · Chicken pie · the second step did not finish"


async def test_an_already_saved_recipe_is_not_split_again(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    task_id = await _start(hass, "again")
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [kind for kind, _ in hass.data["ai_calls"]] == ["import"]
    got = await hass.services.async_call(
        DOMAIN, "ai_task_result", {"task_id": task_id},
        blocking=True, return_response=True,
    )
    assert got["then"] is None


async def test_a_failure_says_why_and_has_only_dismiss(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    task_id = await _start(hass, "bad")
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    needs = hass.states.get(NEEDS).attributes
    [row] = [r for r in needs["items"] if r["id"] == task_id]
    assert row["level"] == LEVEL_NOTICE
    assert row["detail"] == "Failed · No recipe on that page"
    assert row["outcome"] == "failure"
    # Nothing to open: the reason is in the row.
    assert row["action_label"] == "Dismiss"
    assert row["action"]["service"] == f"{DOMAIN}.dismiss"
    assert "secondary_action" not in row
    got = await hass.services.async_call(
        DOMAIN, "ai_task_result", {"task_id": task_id},
        blocking=True, return_response=True,
    )
    assert got["task"]["error"] == "No recipe on that page"
    assert hass.states.get(TASKS).attributes["tasks"][0]["state"] == "failed"
    assert hass.states.get(TASKS).attributes["cards"] == {"meals": LEVEL_NOTICE}


def test_notice_is_the_quietest_level() -> None:
    assert loudest([LEVEL_NOTICE, LEVEL_ATTENTION]) == LEVEL_ATTENTION
    assert loudest([None, LEVEL_NOTICE]) == LEVEL_NOTICE


async def test_two_minutes_later_everything_resets(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    task_id = await _start(hass)
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(NEEDS).attributes["tab_kitchen"] == LEVEL_NOTICE

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
    await hass.async_block_till_done()
    assert hass.states.get(TASKS).attributes["cards"] == {"meals": LEVEL_NOTICE}, (
        "it went before its two minutes were up"
    )

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2, seconds=5))
    await hass.async_block_till_done()
    state = hass.states.get(TASKS)
    assert state.state == "clear"
    assert state.attributes["cards"] == {}
    needs = hass.states.get(NEEDS).attributes
    assert needs["tab_kitchen"] is None
    assert not [r for r in needs["items"] if r["id"] == task_id]


async def test_a_task_with_nothing_to_show_gets_only_dismiss(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    await hass.services.async_call(
        DOMAIN, "start_ai_task",
        {"title": "Plan the week", "action": "script.recipe_import",
         "data": {"url": "x"}, "card": "meals", "open": False},
        blocking=True, return_response=True,
    )
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    [row] = hass.states.get(NEEDS).attributes["items"]
    assert row["action_label"] == "Dismiss"
    assert "secondary_action" not in row


async def test_needs_you_is_loudest_first(hass: HomeAssistant) -> None:
    """Red, orange, yellow, blue -- whatever order the cards came in."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    needs = NeedsYouSensor(entry)
    needs.hass = hass
    needs.entity_id = NEEDS

    class _Owner:
        tab = "kitchen"
        owner_level = None

        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows

        def needs_you_rows(self) -> list[dict[str, Any]]:
            return list(self._rows)

    needs.owners = [_Owner([
        {"id": "b", "level": LEVEL_NOTICE},
        {"id": "y1", "level": LEVEL_ATTENTION},
        {"id": "r", "level": LEVEL_CRITICAL},
        {"id": "o", "level": LEVEL_WAITING},
        {"id": "y2", "level": LEVEL_ATTENTION},
    ])]
    needs._recompute()  # noqa: SLF001
    order = [i["id"] for i in needs.extra_state_attributes["items"]]
    assert order == ["r", "o", "y1", "y2", "b"]
