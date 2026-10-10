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
from tests.owners import attach, owner
from custom_components.home_signals.derived import (
    BinsStatusSensor,
    NeedsYouSensor,
    appliance_jobs,
    loudest,
)

WASHER = "sensor.washing_machine"


def _sensor(hass: HomeAssistant) -> NeedsYouSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    sensor = NeedsYouSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    attach(hass, sensor)
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


async def test_a_leak_somebody_restored_power_over_is_attention(
    hass: HomeAssistant,
) -> None:
    """The person has decided about the floor; the pad still has to dry.

    Until it does the cutoff cannot fire again, which is a job -- but one
    that keeps, so not critical, and not the leak row.
    """
    _machine(hass, leak=True, leak_alarm=False, powered=True)
    rows = await _rows(hass)
    ids = [r["id"] for r in rows]
    assert "leak_washing_machine" not in ids
    wet = next(r for r in rows if r["id"].startswith("leak_wet_"))
    assert wet["level"] == LEVEL_ATTENTION


async def test_a_dry_pad_leaves_no_leak_row(hass: HomeAssistant) -> None:
    _machine(hass, leak=False, leak_alarm=False)
    assert await _row(hass, "leak_") is None


async def test_a_leak_row_never_claims_a_cut_that_did_not_happen(
    hass: HomeAssistant,
) -> None:
    _machine(hass, leak=True, leak_alarm=True, powered=True)
    row = await _row(hass, "leak_")
    assert row["level"] == LEVEL_CRITICAL
    assert "cut" not in row["detail"].lower()


async def test_a_leak_row_says_how_long_the_pad_has_been_wet(
    hass: HomeAssistant,
) -> None:
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    since = (dt_util.utcnow() - timedelta(minutes=12)).isoformat()
    _machine(hass, leak=True, leak_alarm=True, powered=False, leak_since=since)
    assert (await _row(hass, "leak_"))["detail"].startswith("Wet for 12 min")


async def test_so_does_the_still_wet_row(hass: HomeAssistant) -> None:
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    since = (dt_util.utcnow() - timedelta(hours=2, minutes=5)).isoformat()
    _machine(hass, leak=True, leak_alarm=False, leak_since=since)
    assert (await _row(hass, "leak_wet_"))["detail"].startswith("Wet for 2h 5m")


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
        {"leak": True, "leak_alarm": False},
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
        # And it says where it is shown, or it colours nothing.
        assert row.get("tab") == "cleaning", row
        assert row.get("card") == "washing_machine", row


# --- the card decides; the row and the tab follow ----------------------


async def _tabs(hass: HomeAssistant, sensor: NeedsYouSensor | None = None) -> dict:
    sensor = sensor or _sensor(hass)
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    attrs = sensor.extra_state_attributes
    return {k: v for k, v in attrs.items() if k.startswith("tab_")}


def _publish(hass: HomeAssistant, level: str | None, **attrs: Any) -> None:
    """The machine as it now publishes itself: its own jobs and level."""
    jobs = appliance_jobs("Washing machine", WASHER, {
        "slug": "washing_machine", "powered": True, "leak": False,
        "drum_full": False, "pending": [], **attrs,
    })
    _machine(hass, jobs=jobs, level=level, tab="cleaning", **attrs)


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({"leak": True}, LEVEL_CRITICAL),
        ({"leak": True, "leak_alarm": False}, LEVEL_ATTENTION),
        ({"powered": False}, LEVEL_WAITING),
        # The one that drifted: a dead plug over a pad that is still wet
        # after the power came back. Rows are attention + waiting.
        ({"leak": True, "leak_alarm": False, "powered": False}, LEVEL_WAITING),
        ({"leak": True, "powered": False, "drum_full": True}, LEVEL_CRITICAL),
        ({"drum_full": True}, LEVEL_ATTENTION),
    ],
)
async def test_the_machine_decides_its_level_from_its_own_jobs(
    attrs: dict, expected: str
) -> None:
    """The card's level is the loudest of the jobs the machine raises."""
    jobs = appliance_jobs("Washing machine", WASHER, {"slug": "washing_machine", **attrs})
    assert loudest(j["level"] for j in jobs) == expected


async def test_the_tab_wears_the_card_level_not_the_rows(
    hass: HomeAssistant,
) -> None:
    """The rail button follows the card. Here the card says waiting while
    the only row it raises is attention -- the tab must say waiting."""
    _publish(hass, LEVEL_WAITING, drum_full=True)
    tabs = await _tabs(hass)
    assert tabs["tab_cleaning"] == LEVEL_WAITING


async def test_nothing_waiting_is_no_level_anywhere(hass: HomeAssistant) -> None:
    _publish(hass, None)
    tabs = await _tabs(hass)
    assert all(v is None for v in tabs.values()), tabs


async def test_a_snoozed_row_still_colours_the_tab(hass: HomeAssistant) -> None:
    """Snooze puts the reminder off; it does not make the thing untrue."""
    _publish(hass, LEVEL_WAITING, powered=False)
    sensor = _sensor(hass)
    await _tabs(hass, sensor)
    sensor.suppress("unpowered_washing_machine", hours=4)
    attrs = sensor.extra_state_attributes
    assert not any(r["id"] == "unpowered_washing_machine" for r in attrs["items"])
    assert attrs["tab_cleaning"] == LEVEL_WAITING


async def test_done_reaches_the_card_that_owns_the_job(hass: HomeAssistant) -> None:
    """"Done" goes to the card's own sensor, so card, tab and row clear together."""
    hass.states.async_set("sensor.bins", "Garden", {"daysTo": 1})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"bin_sensor": "sensor.bins"})
    entry.add_to_hass(hass)
    sensor = NeedsYouSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    attach(hass, sensor)
    bins = owner(sensor, BinsStatusSensor)
    tabs = await _tabs(hass, sensor)
    [row] = [r for r in sensor.extra_state_attributes["items"] if r["id"].startswith("bin_")]
    assert bins.owner_level == LEVEL_ATTENTION
    assert tabs["tab_cleaning"] == LEVEL_ATTENTION

    sensor.suppress(row["id"])
    await hass.async_block_till_done()
    sensor._recompute()  # noqa: SLF001
    attrs = sensor.extra_state_attributes
    assert bins.owner_level is None
    assert bins.extra_state_attributes["level"] is None
    assert not any(r["id"] == row["id"] for r in attrs["items"])
    assert attrs["tab_cleaning"] is None


async def test_a_quiet_machine_raises_nothing(hass: HomeAssistant) -> None:
    """The control for the test above: it must be possible to raise none.

    Without this, a bug that raised a row unconditionally would make
    every assertion above pass while the panel shouted all day.
    """
    _machine(hass)
    assert await _rows(hass) == []
