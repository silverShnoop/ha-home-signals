"""The dishwasher: the dryer's shape, with the one split a plug can make.

It ends a load with clean dishes in the rack and the door is what says they
came out -- so, like the dryer, nothing queues behind it. What is its own:

- it has no door sensor yet, and a drum nothing can empty must never be
  reported full, because the row it raised could never be cleared;
- its draw tells two things apart and no more -- the element heating and the
  pump washing -- so it reports those, and never a fill or a tumble;
- it goes quiet mid-programme for longer than a washer does, so its idle
  floor is higher.

The spec is taken from `_appliance_specs` rather than written out here, so
these test what the house actually runs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.home_signals.appliance import ApplianceCycleSensor
from custom_components.home_signals.const import APPLIANCE_IDLE, APPLIANCE_RUNNING
from custom_components.home_signals.derived import appliance_jobs
from custom_components.home_signals.sensor import _appliance_specs

POWER = "sensor.dishwasher_plug_power"
PLUG = "switch.dishwasher_plug"
DOOR = "binary_sensor.dishwasher_door"
ENERGY = "sensor.dishwasher_plug_summation_delivered"


class FakeEntry:
    entry_id = "test_entry"

    def __init__(self, options: dict) -> None:
        self.data: dict = {}
        self.options = options


def _spec(*, door: bool) -> dict:
    options = {
        "dishwasher_power": POWER,
        "dishwasher_plug": PLUG,
        "dishwasher_energy": ENERGY,
    }
    if door:
        options["dishwasher_door"] = DOOR
    (spec,) = [s for s in _appliance_specs(FakeEntry(options)) if s["slug"] == "dishwasher"]
    return spec


class Dishwasher:
    def __init__(self, hass, sensor, freezer) -> None:
        self.hass = hass
        self.sensor = sensor
        self.freezer = freezer
        self._kwh = 0.0

    def set(self, entity_id: str, value) -> None:
        self.hass.states.async_set(entity_id, str(value))

    async def draw(self, watts: float, *, for_minutes: float = 0) -> None:
        self.set(POWER, watts)
        await self.hass.async_block_till_done()
        # Readings every half-minute, as the plug reports them, so a
        # phase has the readings it needs to be committed.
        steps = int(for_minutes * 2)
        for _ in range(steps):
            self._kwh += watts * 0.5 / 60.0 / 1000.0
            self.set(ENERGY, round(self._kwh, 6))
            await self.advance(0.5)
            self.hass.states.async_set(POWER, str(watts), force_update=True)
            await self.hass.async_block_till_done()

    async def advance(self, minutes: float) -> None:
        self.freezer.tick(timedelta(minutes=minutes))
        async_fire_time_changed(self.hass)
        await self.hass.async_block_till_done()

    async def run_a_load(self) -> None:
        await self.draw(60, for_minutes=10)    # pre-wash
        await self.draw(2000, for_minutes=15)  # heat
        await self.draw(80, for_minutes=30)    # main wash
        await self.draw(2000, for_minutes=10)  # heat for the rinse
        await self.draw(70, for_minutes=10)    # rinse
        await self.draw(0, for_minutes=25)     # longer than its idle floor

    @property
    def state(self) -> str:
        return self.sensor.native_value

    @property
    def attrs(self) -> dict:
        return self.sensor.extra_state_attributes


async def _make(hass, freezer, *, door: bool) -> Dishwasher:
    freezer.move_to("2026-10-06 19:00:00+00:00")
    sensor = ApplianceCycleSensor(FakeEntry({}), _spec(door=door))
    sensor.hass = hass
    sensor.entity_id = "sensor.dishwasher"
    hass.states.async_set(PLUG, "on")
    hass.states.async_set(POWER, "0")
    hass.states.async_set(ENERGY, "0")
    if door:
        hass.states.async_set(DOOR, "off")
    await hass.async_block_till_done()
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return Dishwasher(hass, sensor, freezer)


@pytest.fixture
async def doorless(hass: HomeAssistant, freezer) -> Dishwasher:
    return await _make(hass, freezer, door=False)


@pytest.fixture
async def with_door(hass: HomeAssistant, freezer) -> Dishwasher:
    return await _make(hass, freezer, door=True)


# --- what it is ---------------------------------------------------------


def test_it_is_configured_as_a_dishwasher() -> None:
    spec = _spec(door=False)
    assert spec["name"] == "Dishwasher"
    assert spec["icon"] == "mdi:dishwasher"
    assert spec["queues_loads"] is False, "clean dishes have nothing to hang"
    assert spec["idle_minutes"] >= 20


def test_a_higher_shared_floor_is_still_respected() -> None:
    """The dishwasher's floor only ever raises the shared one."""
    options = {"dishwasher_power": POWER, "appliance_idle_minutes": 40}
    (spec,) = [s for s in _appliance_specs(FakeEntry(options)) if s["slug"] == "dishwasher"]
    assert spec["idle_minutes"] == 40


async def test_a_load_runs_and_finishes(doorless: Dishwasher) -> None:
    await doorless.draw(60, for_minutes=5)
    assert doorless.state == APPLIANCE_RUNNING
    await doorless.run_a_load()
    assert doorless.state == APPLIANCE_IDLE
    assert len(doorless.attrs["finished"]) == 1
    assert doorless.attrs["pending"] == []


async def test_a_long_quiet_soak_does_not_split_the_load(doorless: Dishwasher) -> None:
    """Ten silent minutes mid-programme would end a washer's cycle."""
    await doorless.draw(2000, for_minutes=15)
    await doorless.draw(0, for_minutes=12)
    assert doorless.state == APPLIANCE_RUNNING
    await doorless.draw(80, for_minutes=15)
    await doorless.draw(0, for_minutes=25)
    assert len(doorless.attrs["finished"]) == 1


# --- phases ---------------------------------------------------------------


async def test_it_says_heating_or_washing_and_nothing_else(doorless: Dishwasher) -> None:
    await doorless.draw(60, for_minutes=5)
    assert doorless.attrs["phase"] == "wash", "the opening pump run is not a fill"
    await doorless.draw(2000, for_minutes=5)
    assert doorless.attrs["phase"] == "heat"
    await doorless.draw(80, for_minutes=5)
    assert doorless.attrs["phase"] == "wash"
    kinds = {p["kind"] for p in doorless.attrs["phases"]}
    assert kinds == {"wash", "heat"}, kinds


async def test_a_long_opening_run_is_never_relabelled(doorless: Dishwasher) -> None:
    """The washer's fill rule must not reach the dishwasher in any form."""
    await doorless.draw(60, for_minutes=10)
    assert [p["kind"] for p in doorless.attrs["phases"]] == ["wash"]


# --- the door, and the lack of one ---------------------------------------


async def test_without_a_door_it_is_never_full(doorless: Dishwasher) -> None:
    """Nothing could empty it, so nothing may fill it."""
    await doorless.run_a_load()
    assert doorless.attrs["drum_full"] is False
    assert doorless.attrs["jobs"] == []
    assert doorless.attrs["level"] is None


async def test_a_restored_full_drum_without_a_door_is_dropped(
    hass: HomeAssistant, freezer
) -> None:
    sensor = ApplianceCycleSensor(FakeEntry({}), _spec(door=False))
    sensor.hass = hass
    sensor._restore({"drum_full": True})  # noqa: SLF001
    assert sensor._drum_full is False  # noqa: SLF001


async def test_with_a_door_a_finished_load_is_full_until_opened(
    with_door: Dishwasher,
) -> None:
    await with_door.run_a_load()
    assert with_door.attrs["drum_full"] is True
    titles = [j["title"] for j in with_door.attrs["jobs"]]
    assert titles == ["Dishwasher needs emptying"], titles

    with_door.set(DOOR, "on")
    await with_door.hass.async_block_till_done()
    assert with_door.attrs["drum_full"] is False
    assert with_door.attrs["jobs"] == []


def test_the_row_offers_no_button() -> None:
    """Cleared by the door, like the dryer's, so nothing to press."""
    rows = appliance_jobs("Dishwasher", "sensor.dishwasher",
                          {"slug": "dishwasher", "drum_full": True})
    assert len(rows) == 1 and "action" not in rows[0]
