"""An unlocked door is a Needs you row, and it only goes red when it has earned it.

It used to be a separate alert card, red from the first second. Now it is
a row like every other job: `waiting` while somebody could still be using
the door, `critical` once the grace is up -- read off `Security status`
so the row and the Security tab can never disagree.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from homeassistant.util import dt as dt_util

from custom_components.home_signals.const import (
    DOMAIN,
    LEVEL_CRITICAL,
    LEVEL_WAITING,
)
from custom_components.home_signals.derived import (
    NeedsYouSensor,
    SecurityStatusSensor,
)

FRONT = "lock.front_door"
BACK = "binary_sensor.back_door"


@pytest.fixture
def clock(freezer):
    freezer.move_to("2026-09-17 12:00:00+00:00")
    return freezer


async def _pair(hass: HomeAssistant) -> tuple[NeedsYouSensor, SecurityStatusSensor]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            "security_locks": [FRONT],
            "security_openings": [BACK],
            "security_grace_minutes": 5,
        },
    )
    entry.add_to_hass(hass)
    needs = NeedsYouSensor(entry)
    needs.hass = hass
    needs.entity_id = "sensor.needs_you"
    security = SecurityStatusSensor(entry)
    security.hass = hass
    security.entity_id = "sensor.security_status"
    security.add_listener(needs)
    needs.security = security
    await needs.async_added_to_hass()
    await security.async_added_to_hass()
    await hass.async_block_till_done()
    return needs, security


def _row(needs: NeedsYouSensor, item_id: str) -> dict | None:
    return next(
        (r for r in needs.extra_state_attributes["items"] if r["id"] == item_id),
        None,
    )


async def test_locked_raises_nothing(hass: HomeAssistant, clock) -> None:
    hass.states.async_set(FRONT, "locked", {"friendly_name": "Front door"})
    hass.states.async_set(BACK, "off", {"friendly_name": "Back door"})
    needs, _ = await _pair(hass)
    assert _row(needs, f"unlocked_{FRONT}") is None


async def test_just_unlocked_is_waiting_then_critical_by_itself(
    hass: HomeAssistant, clock
) -> None:
    hass.states.async_set(FRONT, "locked", {"friendly_name": "Front door"})
    hass.states.async_set(BACK, "off", {"friendly_name": "Back door"})
    needs, _ = await _pair(hass)

    hass.states.async_set(FRONT, "unlocked", {"friendly_name": "Front door"})
    await hass.async_block_till_done()

    row = _row(needs, f"unlocked_{FRONT}")
    assert row is not None
    assert row["title"] == "Front door unlocked"
    assert row["level"] == LEVEL_WAITING
    assert row["action"] == {
        "service": "lock.lock", "target": {"entity_id": FRONT},
    }

    clock.tick(timedelta(minutes=4))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert _row(needs, f"unlocked_{FRONT}")["level"] == LEVEL_WAITING

    # Nothing pokes either sensor: the grace timer has to carry it.
    clock.tick(timedelta(minutes=1, seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert _row(needs, f"unlocked_{FRONT}")["level"] == LEVEL_CRITICAL


async def test_locking_clears_the_row(hass: HomeAssistant, clock) -> None:
    hass.states.async_set(FRONT, "unlocked", {"friendly_name": "Front door"})
    hass.states.async_set(BACK, "off", {"friendly_name": "Back door"})
    needs, _ = await _pair(hass)
    assert _row(needs, f"unlocked_{FRONT}") is not None

    hass.states.async_set(FRONT, "locked", {"friendly_name": "Front door"})
    await hass.async_block_till_done()
    assert _row(needs, f"unlocked_{FRONT}") is None


async def test_a_jam_is_critical_at_once(hass: HomeAssistant, clock) -> None:
    hass.states.async_set(FRONT, "jammed", {"friendly_name": "Front door"})
    hass.states.async_set(BACK, "off", {"friendly_name": "Back door"})
    needs, _ = await _pair(hass)
    row = _row(needs, f"unlocked_{FRONT}")
    assert row["title"] == "Front door jammed"
    assert row["level"] == LEVEL_CRITICAL


async def test_an_open_door_is_a_row_with_no_button(
    hass: HomeAssistant, clock
) -> None:
    hass.states.async_set(FRONT, "locked", {"friendly_name": "Front door"})
    hass.states.async_set(BACK, "on", {"friendly_name": "Back door"})
    needs, _ = await _pair(hass)
    row = _row(needs, f"open_{BACK}")
    assert row["title"] == "Back door open"
    assert row["level"] == LEVEL_WAITING
    assert "action" not in row


async def test_it_cannot_be_snoozed_away(hass: HomeAssistant, clock) -> None:
    hass.states.async_set(FRONT, "unlocked", {"friendly_name": "Front door"})
    hass.states.async_set(BACK, "off", {"friendly_name": "Back door"})
    needs, _ = await _pair(hass)
    needs.async_write_ha_state = lambda: None
    needs.suppress(f"unlocked_{FRONT}", 8)
    assert _row(needs, f"unlocked_{FRONT}") is not None


async def test_a_lock_that_loads_after_the_sensor_is_heard_at_once(
    hass: HomeAssistant, clock
) -> None:
    """The live bug: every lock by default, and the Nuki not loaded yet.

    The sensor subscribed to an empty list, so an unlocked door stayed
    green until the five-minute tick. No tick is fired here.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        "security_locks": [], "security_openings": [],
        "security_grace_minutes": 5,
    })
    entry.add_to_hass(hass)
    security = SecurityStatusSensor(entry)
    security.hass = hass
    security.entity_id = "sensor.security_status"
    writes: list[str] = []
    security.async_write_ha_state = lambda: writes.append(security.native_value)
    await security.async_added_to_hass()
    await hass.async_block_till_done()
    assert security.native_value == "green"

    hass.states.async_set(FRONT, "locked", {"friendly_name": "Front door"})
    await hass.async_block_till_done()
    hass.states.async_set(FRONT, "unlocked", {"friendly_name": "Front door"})
    await hass.async_block_till_done()

    assert security.native_value == "amber"
    assert writes[-1] == "amber"
    # And back: locking has to reach green without waiting for a tick,
    # even past the grace, when the panel is red.
    clock.tick(timedelta(minutes=6))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert security.native_value == "red"

    hass.states.async_set(FRONT, "locked", {"friendly_name": "Front door"})
    await hass.async_block_till_done()
    assert security.native_value == "green"
    assert writes[-1] == "green"
