"""The three levels, and the wall between them and the decorative accents.

A level is a promise about a timeline, and the promise is the whole test
a row has to pass:

    attention   needs doing today or tomorrow
    waiting     something is paused or degrading until a person acts
    critical    damage or risk is accruing now

These drifted before, because there was no middle tier and no test: ten
of twelve rows were the same "warning" whatever they meant, and the two
that were not were the loudest colour in the house spent on a chore and
on thirty-one entities that had gone quiet. So each one is asserted by
name here rather than left to the row that raises it.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import (
    DOMAIN,
    LEVEL_ATTENTION,
    LEVEL_CRITICAL,
    LEVEL_LOUDNESS,
    LEVEL_WAITING,
)
from custom_components.home_signals.derived import NeedsYouSensor

WASHER = "sensor.washing_machine"


def _sensor(hass: HomeAssistant) -> NeedsYouSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    sensor = NeedsYouSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    return sensor


def _machine(hass: HomeAssistant, **attrs: Any) -> None:
    """A cycle sensor in the shape `_appliance_entities` looks for.

    `slug` and `pending_count` are the signature, so both are always
    here: they are how the rows find the machine at all, and a fixture
    missing one would make every test below pass for the wrong reason.
    """
    base = {
        "friendly_name": "Washing machine",
        "slug": "washing_machine",
        "pending_count": 0,
        "powered": True,
        "drum_full": False,
        "leak": False,
        "pending": [],
    }
    base.update(attrs)
    hass.states.async_set(WASHER, "idle", base, force_update=True)


async def _rows(hass: HomeAssistant) -> list[dict]:
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return sensor.extra_state_attributes["items"]


async def _row(hass: HomeAssistant, prefix: str) -> dict | None:
    rows = await _rows(hass)
    return next((r for r in rows if str(r["id"]).startswith(prefix)), None)


# --- each row, by name -------------------------------------------------


async def test_a_leak_is_critical(hass: HomeAssistant) -> None:
    """Water on the floor is the one thing accruing damage right now."""
    _machine(hass, leak=True)
    assert (await _row(hass, "leak_"))["level"] == LEVEL_CRITICAL


async def test_a_leak_somebody_restored_power_over_is_no_row(
    hass: HomeAssistant,
) -> None:
    """The pad is still wet; the person at the machine has decided."""
    _machine(hass, leak=True, leak_alarm=False, powered=True)
    assert await _row(hass, "leak_") is None


async def test_a_leak_row_never_claims_a_cut_that_did_not_happen(
    hass: HomeAssistant,
) -> None:
    _machine(hass, leak=True, leak_alarm=True, powered=True)
    row = await _row(hass, "leak_")
    assert row["level"] == LEVEL_CRITICAL
    assert "cut" not in row["detail"].lower()


async def test_a_dead_plug_is_waiting_not_an_errand(
    hass: HomeAssistant,
) -> None:
    """Wet washing and a clock running.

    The activity is paused until somebody acts, which is exactly the
    middle level. It used to share a colour with "bins tomorrow", back
    when there was no middle level to give it.
    """
    _machine(hass, powered=False)
    assert (await _row(hass, "unpowered_"))["level"] == LEVEL_WAITING


async def test_a_full_drum_is_attention(hass: HomeAssistant) -> None:
    """The washing keeps. Today or tomorrow."""
    _machine(hass, drum_full=True)
    assert (await _row(hass, "drum_"))["level"] == LEVEL_ATTENTION


async def test_a_leak_and_a_dead_plug_are_separate_rows_at_separate_levels(
    hass: HomeAssistant,
) -> None:
    """Neither fact is inferred from the other, and nor is either level.

    The pad stays damp long after the floor has been dealt with, so a
    version that collapsed these would keep claiming damage was accruing
    while somebody stood there finishing the wash.
    """
    _machine(hass, leak=True, powered=False)
    assert (await _row(hass, "leak_"))["level"] == LEVEL_CRITICAL
    assert (await _row(hass, "unpowered_"))["level"] == LEVEL_WAITING


# --- the wall ----------------------------------------------------------


@pytest.mark.parametrize(
    "attrs",
    [
        {"leak": True},
        {"powered": False},
        {"drum_full": True},
        {"leak": True, "powered": False, "drum_full": True},
    ],
)
async def test_every_row_carries_a_real_level_and_no_accent(
    hass: HomeAssistant, attrs: dict
) -> None:
    """The invariant, checked over whatever the machine is doing.

    Two halves, and the second is the one that matters: a Needs-you row
    may not carry an `accent` at all. An accent is decorative -- it says
    which tab a card belongs to -- and a row that named one would be
    claiming an alarm by asking for a hue, which is the drift the split
    exists to stop.
    """
    _machine(hass, **attrs)
    rows = await _rows(hass)
    assert rows, "the fixture raised nothing, so this asserted nothing"
    for row in rows:
        assert row.get("level") in LEVEL_LOUDNESS, row
        assert "accent" not in row, row


async def test_a_quiet_machine_raises_nothing(hass: HomeAssistant) -> None:
    """The control for the test above: it must be possible to raise none.

    Without this, a bug that raised a row unconditionally would make
    every assertion above pass while the panel shouted all day.
    """
    _machine(hass)
    assert await _rows(hass) == []
