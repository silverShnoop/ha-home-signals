"""The security traffic light: green has to mean locked, red has to be on time.

This is the one thing in the house a person trusts from across a room
without going to check, so it is the one thing that must not be only ever
tried by hand. Every case below is a way of being wrong in the
*reassuring* direction -- green or amber when it should be red -- which is
the only kind of wrong that matters here. Being noisy is a nuisance; being
quiet is the failure.

Ported from a hand-rolled stub harness onto the real one. Two assertions
got stronger in the move and are the reason it was worth doing:

  * the grace period is no longer "a timer was armed for the right time".
    The clock is moved past it and the sensor has to go red BY ITSELF.
    A wired callback that never fires would have passed the old test.
  * the restart case no longer pokes `_since` directly. It restores a real
    state through `async_added_to_hass`, which is the path the house
    actually takes after a reboot.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.home_signals.const import (
    SECURITY_AMBER,
    SECURITY_GREEN,
    SECURITY_RED,
)
from custom_components.home_signals.derived import SecurityStatusSensor

FRONT = "lock.front_door"
BACK = "binary_sensor.back_door"

LEVEL_WAITING = "waiting"
LEVEL_CRITICAL = "critical"


class FakeEntry:
    entry_id = "test_entry"
    data: dict = {}

    def __init__(self, **options) -> None:
        self.options = {
            "security_locks": [],
            "security_openings": [],
            "security_grace_minutes": 5,
            **options,
        }


@pytest.fixture
def clock(freezer):
    """A clock that starts somewhere definite and can be pushed."""
    freezer.move_to("2026-09-17 12:00:00+00:00")
    return freezer


def make(hass: HomeAssistant, **options) -> SecurityStatusSensor:
    sensor = SecurityStatusSensor(FakeEntry(**options))
    sensor.hass = hass
    sensor.entity_id = "sensor.security_status"
    return sensor


def set_lock(hass: HomeAssistant, state: str, *, name: str = "Front door") -> None:
    hass.states.async_set(FRONT, state, {"friendly_name": name})


def set_door(hass: HomeAssistant, state: str, *, name: str = "Back door") -> None:
    hass.states.async_set(BACK, state, {"friendly_name": name})


# --- shut, and saying so ----------------------------------------------


async def test_all_locked_is_green_and_says_nothing_else(
    hass: HomeAssistant, clock
) -> None:
    set_lock(hass, "locked")
    await hass.async_block_till_done()

    sensor = make(hass)
    sensor._recompute()

    assert sensor.native_value == SECURITY_GREEN
    assert sensor._detail == "All secure"
    assert sensor._since is None
    assert sensor._items == []


# --- the grace period -------------------------------------------------


async def test_just_unlocked_is_amber_not_red(hass: HomeAssistant, clock) -> None:
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()

    sensor = make(hass)
    sensor._recompute()

    assert sensor.native_value == SECURITY_AMBER
    assert sensor._detail == "Front door"
    assert sensor._since == dt_util.utcnow()
    assert len(sensor._items) == 1
    assert sensor._items[0]["value"] == "Unlocked"
    assert sensor._items[0]["level"] == LEVEL_WAITING


async def test_still_amber_a_minute_before_the_grace_is_up(
    hass: HomeAssistant, clock
) -> None:
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()

    clock.tick(timedelta(minutes=4))
    sensor._recompute()

    assert sensor.native_value == SECURITY_AMBER


async def test_it_goes_red_on_its_own_when_the_grace_expires(
    hass: HomeAssistant, clock
) -> None:
    """The assertion the stub harness could not make.

    The old test checked that a callback had been scheduled for the right
    moment. That passes even if the callback never runs -- and a grace
    period that silently never expires is the exact failure this sensor
    exists to prevent. Here nothing calls `_recompute`: the clock moves,
    Home Assistant fires what is due, and the sensor has to have gone red
    by itself.
    """
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()
    assert sensor.native_value == SECURITY_AMBER

    clock.tick(timedelta(minutes=5, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert sensor.native_value == SECURITY_RED, (
        "the grace period was armed but never actually expired"
    )
    assert sensor._items[0]["level"] == LEVEL_CRITICAL


async def test_a_one_minute_grace_is_honoured_on_both_sides(
    hass: HomeAssistant, clock
) -> None:
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass, security_grace_minutes=1)
    sensor._recompute()
    assert sensor.native_value == SECURITY_AMBER

    clock.tick(timedelta(minutes=1))
    sensor._recompute()
    assert sensor.native_value == SECURITY_RED


# --- going back to shut, and out again --------------------------------


async def test_locking_it_again_clears_the_clock(hass: HomeAssistant, clock) -> None:
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()

    clock.tick(timedelta(minutes=6))
    set_lock(hass, "locked")
    await hass.async_block_till_done()
    sensor._recompute()

    assert sensor.native_value == SECURITY_GREEN
    assert sensor._since is None


async def test_a_second_unlock_starts_its_own_clock(hass: HomeAssistant, clock) -> None:
    """Otherwise the second episode inherits the first one's age.

    `_since` is deliberately kept across a recompute (it is what survives a
    restart), so the bug this catches is the obvious implementation: never
    clearing it. A door opened for ten seconds would then be red because a
    different door was open an hour ago.
    """
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()

    clock.tick(timedelta(minutes=6))
    set_lock(hass, "locked")
    await hass.async_block_till_done()
    sensor._recompute()

    clock.tick(timedelta(minutes=4))
    reopened = dt_util.utcnow()
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor._recompute()

    assert sensor.native_value == SECURITY_AMBER, "it inherited the first episode"
    assert sensor._since == reopened


# --- surviving a restart ----------------------------------------------


async def test_a_door_open_since_before_a_reboot_comes_back_red(
    hass: HomeAssistant, clock
) -> None:
    """A restart resets every `last_changed` to boot time.

    Without the restored stamp the house comes back amber and starts the
    grace period again, so a door left open all afternoon reads as one
    just opened -- reassuring, and wrong. This restores through the real
    `async_added_to_hass` rather than assigning `_since`, so it covers the
    parsing and the recompute as well as the decision.
    """
    opened_at = dt_util.utcnow()
    clock.tick(timedelta(minutes=30))

    mock_restore_cache(
        hass,
        (
            State(
                "sensor.security_status",
                SECURITY_RED,
                {"since": opened_at.isoformat()},
            ),
        ),
    )

    # last_changed is "now" -- the reboot -- which is the whole trap.
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()

    sensor = make(hass)
    await sensor.async_added_to_hass()

    assert sensor._since == opened_at, "the restored stamp was not used"
    assert sensor.native_value == SECURITY_RED


# --- what it must not claim to know -----------------------------------


async def test_a_lock_it_cannot_read_is_never_green_and_never_red(
    hass: HomeAssistant, clock
) -> None:
    """Unknown is not proof of a problem, and not proof of safety either.

    So it holds amber indefinitely: going red would cry wolf at a flat
    battery, and going green would claim a door is locked when nobody
    knows. It must also never arm a grace timer, having no moment to count
    from.
    """
    set_lock(hass, "unavailable")
    await hass.async_block_till_done()

    sensor = make(hass)
    sensor._recompute()

    assert sensor.native_value == SECURITY_AMBER
    assert sensor._detail == "1 not reporting"
    assert sensor._since is None

    clock.tick(timedelta(hours=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    sensor._recompute()

    assert sensor.native_value == SECURITY_AMBER, "an unreadable lock went red"


# --- more than one way to be open -------------------------------------


async def test_a_contact_sensor_counts_and_the_earliest_sets_the_clock(
    hass: HomeAssistant, clock
) -> None:
    """Two things open, and the grace runs from the one that went first.

    Taking the latest instead would let a house stay amber indefinitely by
    opening something new every four minutes.
    """
    first = dt_util.utcnow()
    set_door(hass, "on")
    await hass.async_block_till_done()

    clock.tick(timedelta(minutes=6))
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()

    clock.tick(timedelta(minutes=2))
    sensor = make(hass, security_openings=[BACK])
    sensor._recompute()

    assert sensor._since == first, "the clock started from the later one"
    assert sensor.native_value == SECURITY_RED
    assert sensor._detail == "Front door, Back door open"
    assert sensor.extra_state_attributes["unlocked_count"] == 1
    assert sensor.extra_state_attributes["open_count"] == 1


async def test_an_unlisted_contact_sensor_is_ignored(
    hass: HomeAssistant, clock
) -> None:
    """Openings are opt-in, and this is why.

    A house's binary sensors include the fridge and the washing machine
    door. A security light that goes red because somebody is making a
    sandwich is one people learn to ignore, which costs more than it saves.
    """
    set_lock(hass, "locked")
    set_door(hass, "on")
    await hass.async_block_till_done()

    sensor = make(hass)  # BACK deliberately not configured
    sensor._recompute()

    assert sensor.native_value == SECURITY_GREEN


# --- the blip ---------------------------------------------------------


async def test_a_blip_while_unlocked_does_not_restart_the_grace(
    hass: HomeAssistant, clock
) -> None:
    """The Nuki drops to `unavailable` about once a day.

    While it is unreadable there is nothing in `insecure`, so `_since` is
    cleared -- and when the lock comes back its `last_changed` is the blip,
    not the moment the door was opened. The grace period restarts.

    A door left open could therefore never go red, as long as the lock
    blips more often than every five minutes.
    """
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()
    assert sensor.native_value == SECURITY_AMBER

    clock.tick(timedelta(minutes=3))
    set_lock(hass, "unavailable")
    await hass.async_block_till_done()
    sensor._recompute()

    clock.tick(timedelta(seconds=10))
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor._recompute()

    clock.tick(timedelta(minutes=3))
    sensor._recompute()

    assert sensor.native_value == SECURITY_RED, (
        "the door has been unlocked for over six minutes and is still amber "
        "-- a blip reset the grace"
    )


async def test_the_row_says_the_same_thing_the_card_does_after_a_blip(
    hass: HomeAssistant, clock
) -> None:
    """The card going red is only half of it.

    The Needs-you row carries its own `since`, read off the entity's
    `last_changed` -- the very value the blip destroys. So the card would
    correctly say red while its own row said the door had been open for
    ten seconds, which is the kind of contradiction that teaches people
    the panel is broken.
    """
    opened_at = dt_util.utcnow()
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()
    assert sensor._items[0]["since"] == opened_at.isoformat()

    clock.tick(timedelta(minutes=3))
    set_lock(hass, "unavailable")
    await hass.async_block_till_done()
    sensor._recompute()

    clock.tick(timedelta(seconds=10))
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor._recompute()

    assert sensor._items[0]["since"] == opened_at.isoformat(), (
        "the row is dating the door from the blip, so it reads 10s while "
        "the card reads three minutes"
    )


async def test_locking_the_door_forgets_the_clock_for_good(
    hass: HomeAssistant, clock
) -> None:
    """The held stamp must not outlive the thing it is about.

    Holding a stamp across a blip is only safe if a positive report of
    shut clears it. Otherwise the first unlock of the day is remembered
    for ever and every later one is dated to it.
    """
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()

    clock.tick(timedelta(minutes=10))
    set_lock(hass, "locked")
    await hass.async_block_till_done()
    sensor._recompute()
    assert sensor.native_value == SECURITY_GREEN
    assert sensor._since is None

    clock.tick(timedelta(minutes=10))
    unlocked_again = dt_util.utcnow()
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor._recompute()

    assert sensor.native_value == SECURITY_AMBER, (
        "a fresh unlock came back red -- the morning's stamp was never let go"
    )
    assert sensor._items[0]["since"] == unlocked_again.isoformat()


async def test_an_unreadable_lock_that_was_never_open_does_not_start_a_clock(
    hass: HomeAssistant, clock
) -> None:
    """Amber, and it stays amber. Unreadable on its own is not a countdown."""
    set_lock(hass, "unavailable")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()
    assert sensor.native_value == SECURITY_AMBER

    clock.tick(timedelta(minutes=30))
    sensor._recompute()

    assert sensor.native_value == SECURITY_AMBER, (
        "a lock that has only ever been unreadable went red -- being "
        "unreadable is not evidence the door is open"
    )
    assert sensor._since is None


async def test_a_restart_restores_the_rows_not_just_the_card(
    hass: HomeAssistant, clock
) -> None:
    """After a reboot every lock's `last_changed` is boot time.

    The card already survived that by restoring its own `since`. The rows
    did not, so the panel came back saying red beside "just now".
    """
    opened_at = dt_util.utcnow() - timedelta(hours=3)
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.security_status",
                SECURITY_RED,
                {
                    "since": opened_at.isoformat(),
                    "unlocked": [
                        {"entity_id": FRONT, "since": opened_at.isoformat()}
                    ],
                    "open": [],
                },
            )
        ],
    )
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()

    sensor = make(hass)
    sensor.entity_id = "sensor.security_status"
    await sensor.async_added_to_hass()

    assert sensor.native_value == SECURITY_RED
    assert sensor._since == opened_at
    assert sensor._items[0]["since"] == opened_at.isoformat(), (
        "the row was re-dated to boot time"
    )


async def test_a_door_known_open_goes_red_even_if_the_lock_never_comes_back(
    hass: HomeAssistant, clock
) -> None:
    """Losing sight of an open door does not make it a shut one.

    A blip is short and the escalation catches up on the far side of it.
    A lock that drops out and stays out is the case that needs the clock
    to keep running while nothing can be read at all -- otherwise the
    house sits amber indefinitely on a door it last saw standing open.
    """
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()
    assert sensor.native_value == SECURITY_AMBER

    clock.tick(timedelta(minutes=1))
    set_lock(hass, "unavailable")
    await hass.async_block_till_done()
    sensor._recompute()

    clock.tick(timedelta(minutes=30))
    sensor._recompute()

    assert sensor.native_value == SECURITY_RED, (
        "the lock dropped out an hour ago showing unlocked and the house "
        "is still amber -- losing the reading was treated as reassurance"
    )


# --- a jam is not a slow unlock ---------------------------------------


async def test_a_jammed_lock_is_red_immediately(hass: HomeAssistant, clock) -> None:
    """The grace covers a door somebody is using. A jam is the opposite.

    Five minutes exist for carrying shopping in or seeing someone out --
    a door that will shortly be shut by the person who opened it. Nobody
    is coming back to finish a jam: the mechanism already tried and
    failed. Waiting it out is five minutes of a green-looking house with
    a door that will not lock.
    """
    set_lock(hass, "jammed")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()

    assert sensor.native_value == SECURITY_RED, (
        "a jammed lock is sitting in the grace period waiting to become a "
        "problem it already is"
    )


async def test_a_jam_says_unlocked_and_carries_the_reason_separately(
    hass: HomeAssistant, clock
) -> None:
    """The word is what the door IS; the jam is why.

    A jammed lock is not a third state of the door. Putting "Jammed" in
    the row's value would make it one, and the card's hero reads from the
    same vocabulary -- so the fault has to travel beside the state, not
    inside it.
    """
    set_lock(hass, "jammed")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()

    assert len(sensor._items) == 1
    row = sensor._items[0]
    assert row["value"] == "Unlocked", row["value"]
    assert row["jammed"] is True
    assert row["level"] == LEVEL_CRITICAL
    assert "jam" not in row["value"].lower()


async def test_an_ordinary_unlock_is_not_flagged_as_a_jam(
    hass: HomeAssistant, clock
) -> None:
    """The flag has to be false, not absent, or the card cannot test it."""
    set_lock(hass, "unlocked")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()

    assert sensor.native_value == SECURITY_AMBER
    assert sensor._items[0]["jammed"] is False


async def test_a_jam_clearing_lets_the_house_go_green(
    hass: HomeAssistant, clock
) -> None:
    """Red on sight must not be red for ever.

    Skipping the grace is a shortcut into red, not a latch. A lock that
    jams and is then worked until it throws is a shut door, and the panel
    has to say so.
    """
    set_lock(hass, "jammed")
    await hass.async_block_till_done()
    sensor = make(hass)
    sensor._recompute()
    assert sensor.native_value == SECURITY_RED

    clock.tick(timedelta(seconds=40))
    set_lock(hass, "locked")
    await hass.async_block_till_done()
    sensor._recompute()

    assert sensor.native_value == SECURITY_GREEN, "the jam latched red"
    assert sensor._since is None
    assert sensor._items == []
