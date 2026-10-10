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

import pytest
from homeassistant.core import Context, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
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
from custom_components.home_signals.ai_tasks import MAX_DATA_BYTES, MAX_RUNNING
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
        if call.data.get("url") == "empty":
            return {"recipe": "", "slug": ""}
        if call.data.get("url") == "refused":
            return {"error": "That page wants a login"}
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


async def test_running_is_blue_on_all_three(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    task_id = await _start(hass)
    await hass.async_block_till_done(wait_background_tasks=False)

    state = hass.states.get(TASKS)
    assert state.state == "running"
    assert state.attributes["running"] == 1
    assert state.attributes["cards"] == {"meals": LEVEL_NOTICE}
    needs = hass.states.get(NEEDS).attributes
    assert needs["tab_kitchen"] == LEVEL_NOTICE
    [row] = [r for r in needs["items"] if r["id"] == task_id]
    assert row["level"] == LEVEL_NOTICE
    assert row["outcome"] == "running"
    assert row["detail"] == "Running · step 1 of 2"
    assert row["action_label"] == "Dismiss"

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_dismissing_a_running_task_quietens_it_until_it_lands(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    task_id = await _start(hass)
    await hass.async_block_till_done(wait_background_tasks=False)

    await hass.services.async_call(DOMAIN, "dismiss", {"item_id": task_id}, blocking=True)
    needs = hass.states.get(NEEDS).attributes
    assert not [r for r in needs["items"] if r["id"] == task_id]
    assert needs["tab_kitchen"] is None
    assert hass.states.get(TASKS).attributes["cards"] == {}

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    [row] = [r for r in hass.states.get(NEEDS).attributes["items"] if r["id"] == task_id]
    assert row["outcome"] == "success", "landing was not news again"


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


async def _row_for(hass: HomeAssistant, url: str, **extra: Any) -> dict[str, Any]:
    release = await _setup(hass)
    out = await hass.services.async_call(
        DOMAIN, "start_ai_task",
        {"title": "Recipe from a link", "action": "script.recipe_import",
         "data": {"url": url}, "card": "recipes", **extra},
        blocking=True, return_response=True,
    )
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    [row] = [r for r in hass.states.get(NEEDS).attributes["items"] if r["id"] == out["task_id"]]
    return row


async def test_an_answer_without_what_was_asked_for_is_a_failure(hass: HomeAssistant) -> None:
    """A model that finds no recipe answers cleanly, with an empty name.

    That is not Done: the card says no recipe could be read, so the row
    must say Failed too, with the caller's reason.
    """
    row = await _row_for(hass, "empty", require="slug", missing="No recipe found on that page")
    assert row["outcome"] == "failure"
    assert row["detail"] == "Failed \u00b7 No recipe found on that page"
    assert row["action_label"] == "Dismiss"


async def test_without_require_an_empty_answer_still_counts(hass: HomeAssistant) -> None:
    row = await _row_for(hass, "empty")
    assert row["outcome"] == "success"


async def test_an_answer_that_carries_its_own_error_is_a_failure(hass: HomeAssistant) -> None:
    row = await _row_for(hass, "refused")
    assert row["outcome"] == "failure"
    assert row["detail"] == "Failed \u00b7 That page wants a login"


async def test_a_second_step_that_answers_with_an_error_is_partial(hass: HomeAssistant) -> None:
    release = await _setup(hass)

    async def _split_error(call: ServiceCall) -> dict[str, Any]:
        return {"mode": "error", "skipped": True}

    hass.services.async_register(
        "script", "recipe_split", _split_error, supports_response=SupportsResponse.ONLY
    )
    task_id = await _start(hass)
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    [row] = [r for r in hass.states.get(NEEDS).attributes["items"] if r["id"] == task_id]
    assert row["outcome"] == "success"
    assert row["detail"].endswith("the second step did not finish")


# --- who runs it, what may run, and how much at once ---------------------


async def test_the_action_runs_as_whoever_asked(hass: HomeAssistant) -> None:
    """The caller's context goes through to the action, so Home Assistant
    applies that person's permissions and the logbook names them."""
    release = await _setup(hass)
    seen: list[Context] = []

    async def _whoami(call: ServiceCall) -> dict[str, Any]:
        seen.append(call.context)
        return {"recipe": "Soup", "slug": "soup"}

    hass.services.async_register(
        "script", "whoami", _whoami, supports_response=SupportsResponse.ONLY
    )
    asked = Context(user_id="a-user")
    await hass.services.async_call(
        DOMAIN, "start_ai_task",
        {
            "title": "Who", "action": "script.whoami", "card": "meals",
            "then": {"action": "script.recipe_split", "pass": {"recipe": "slug"}},
        },
        blocking=True, return_response=True, context=asked,
    )
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert seen == [asked], "the first step did not run as the caller"
    (split,) = [c for c in hass.data["ai_calls"] if c[0] == "split"]
    assert split[1] == {"recipe": "soup"}


async def test_only_a_script_an_ai_task_or_frigate_may_be_run(hass: HomeAssistant) -> None:
    """A lock, a shell command or a cover is not slow AI work."""
    await _setup(hass)
    for bad in ("lock.lock", "shell_command.run", "homeassistant.restart"):
        with pytest.raises(ServiceValidationError, match="ai_task, frigate, script"):
            await hass.services.async_call(
                DOMAIN, "start_ai_task",
                {"title": "No", "action": bad},
                blocking=True, return_response=True,
            )
    # The second step is held to the same list.
    with pytest.raises(ServiceValidationError, match="ai_task, frigate, script"):
        await hass.services.async_call(
            DOMAIN, "start_ai_task",
            {
                "title": "No", "action": "script.recipe_import",
                "then": {"action": "lock.unlock"},
            },
            blocking=True, return_response=True,
        )
    assert hass.states.get(TASKS).attributes["tasks"] == [], "a refused task was kept"


async def test_a_fifth_task_at_once_is_told_to_wait(hass: HomeAssistant) -> None:
    release = await _setup(hass)
    for _ in range(MAX_RUNNING):
        await _start(hass)
    await hass.async_block_till_done(wait_background_tasks=False)
    assert hass.states.get(TASKS).attributes["running"] == MAX_RUNNING

    with pytest.raises(ServiceValidationError, match="already running; try again shortly"):
        await _start(hass)

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(TASKS).attributes["running"] == 0
    # Room again once they have landed.
    await _start(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_task_cannot_carry_more_than_the_cap(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError, match="over 8 MB"):
        await hass.services.async_call(
            DOMAIN, "start_ai_task",
            {
                "title": "Big", "action": "script.recipe_import",
                "data": {"photo": "x" * (MAX_DATA_BYTES + 1)},
            },
            blocking=True, return_response=True,
        )
    # The same again under the cap, carried in the second step, is fine.
    with pytest.raises(ServiceValidationError, match="over 8 MB"):
        await hass.services.async_call(
            DOMAIN, "start_ai_task",
            {
                "title": "Big", "action": "script.recipe_import",
                "then": {"action": "script.recipe_split", "data": {"photo": "x" * MAX_DATA_BYTES}},
            },
            blocking=True, return_response=True,
        )
    assert hass.states.get(TASKS).attributes["tasks"] == []
