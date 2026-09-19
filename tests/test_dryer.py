"""The tumble dryer: the same machine, one state shorter.

Both appliances end a cycle with a full drum, and on both the door empties
it. The washer has a SECOND state after that one -- washing out of the drum
still has to be hung, somewhere the machine cannot see -- so its loads queue
and wait to be told. A dry load is finished the moment it leaves the drum.

That one difference is the whole of the dryer, so it is the whole of this
file: the shared half must behave identically, and the extra half must not
appear. A queued dry load would be a reminder nothing can ever satisfy.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.home_signals.appliance import (
    ApplianceCycleSensor,
    CleaningStatusSensor,
)
from custom_components.home_signals.const import (
    APPLIANCE_IDLE,
    APPLIANCE_RUNNING,
    CLEANING_AMBER,
    CLEANING_GREEN,
)

DRYER_POWER = "sensor.dryer_power"
DRYER_PLUG = "switch.dryer_plug"
DRYER_DOOR = "binary_sensor.dryer_door"
DRYER_ENERGY = "sensor.dryer_energy"

DRYER_SPEC = {
    "slug": "tumble_dryer",
    "name": "Tumble dryer",
    "power_sensor": DRYER_POWER,
    "plug": DRYER_PLUG,
    "door": DRYER_DOOR,
    "leak": None,
    "energy_sensor": DRYER_ENERGY,
    "queues_loads": False,
    "start_watts": 8,
    "idle_watts": 4,
    "idle_minutes": 5,
    "min_minutes": 10,
    "min_kwh": 0.05,
}


class FakeEntry:
    entry_id = "test_entry"
    data: dict = {}
    options: dict = {}


class Dryer:
    """A dryer you can drive, and a clock you can push.

    The same shape as the washer's harness in test_appliance.py, and
    deliberately not shared with it: the point of these tests is that two
    machines behave the same where they should, which a common driver
    would assume rather than demonstrate.
    """

    def __init__(self, hass, sensor, freezer) -> None:
        self.hass = hass
        self.sensor = sensor
        self.freezer = freezer
        self._kwh = 0.0

    def set(self, entity_id: str, value) -> None:
        self.hass.states.async_set(entity_id, str(value))

    async def draw(self, watts: float, *, for_minutes: float = 0) -> None:
        self.set(DRYER_POWER, watts)
        await self.hass.async_block_till_done()
        if for_minutes:
            self._kwh += watts * (for_minutes / 60.0) / 1000.0
            self.set(DRYER_ENERGY, round(self._kwh, 6))
            await self.advance(for_minutes)

    async def advance(self, minutes: float) -> None:
        """Frozen clock, ticked -- see the note in test_appliance.py."""
        self.freezer.tick(timedelta(minutes=minutes))
        async_fire_time_changed(self.hass)
        await self.hass.async_block_till_done()

    async def open_door(self) -> None:
        self.set(DRYER_DOOR, "on")
        await self.hass.async_block_till_done()

    async def run_a_load(self) -> None:
        await self.draw(2200, for_minutes=45)
        await self.draw(0, for_minutes=6)

    @property
    def state(self) -> str:
        return self.sensor.native_value

    @property
    def attrs(self) -> dict:
        return self.sensor.extra_state_attributes


@pytest.fixture
async def dryer(hass: HomeAssistant, freezer):
    freezer.move_to("2026-09-19 09:00:00+00:00")
    sensor = ApplianceCycleSensor(FakeEntry(), dict(DRYER_SPEC))
    sensor.hass = hass
    sensor.entity_id = "sensor.tumble_dryer_cycle"

    hass.states.async_set(DRYER_PLUG, "on")
    hass.states.async_set(DRYER_DOOR, "off")
    hass.states.async_set(DRYER_POWER, "0")
    hass.states.async_set(DRYER_ENERGY, "0")
    await hass.async_block_till_done()

    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return Dryer(hass, sensor, freezer)


# --- the half that must behave exactly like the washer ----------------


async def test_it_reports_running_and_then_idle(dryer: Dryer) -> None:
    await dryer.draw(2200, for_minutes=45)
    assert dryer.state == APPLIANCE_RUNNING

    await dryer.draw(0, for_minutes=6)
    assert dryer.state == APPLIANCE_IDLE


async def test_a_finished_load_fills_the_drum(dryer: Dryer) -> None:
    await dryer.run_a_load()
    assert dryer.attrs["drum_full"] is True


async def test_the_door_empties_it(dryer: Dryer) -> None:
    """The dryer's only way of being told the job is done."""
    await dryer.run_a_load()
    assert dryer.attrs["drum_full"] is True

    await dryer.open_door()
    assert dryer.attrs["drum_full"] is False, "the door did not empty the drum"


async def test_a_tumble_too_short_to_be_a_load_is_not_one(dryer: Dryer) -> None:
    """Somebody running it for two minutes to shake out a shirt."""
    await dryer.draw(2200, for_minutes=2)
    await dryer.draw(0, for_minutes=6)
    assert dryer.attrs["drum_full"] is False
    assert dryer.attrs["finished"] == []


async def test_it_keeps_a_history_to_list(dryer: Dryer) -> None:
    """The recent-loads list on the card reads this, same as the washer."""
    await dryer.run_a_load()
    history = dryer.attrs["finished"]
    assert len(history) == 1
    assert history[0]["duration_minutes"] >= 45
    assert history[0]["energy_kwh"] > 0
    assert dryer.attrs["finished_today"] == history


# --- the half that must NOT appear ------------------------------------


async def test_a_dry_load_is_never_queued(dryer: Dryer) -> None:
    """The whole difference between the two machines.

    A queued load waits to be told it is done, and the only thing that can
    tell it is the hang button -- which a dryer does not have. Queueing one
    here would put a row in Needs you that nothing in the house can clear.
    """
    await dryer.run_a_load()
    assert dryer.attrs["pending"] == []
    assert dryer.attrs["pending_count"] == 0


async def test_it_says_it_does_not_queue(dryer: Dryer) -> None:
    """Needs you reads attributes, not objects, so the flag has to travel."""
    assert dryer.attrs["queues_loads"] is False


async def test_two_loads_do_not_accumulate_anything_to_clear(dryer: Dryer) -> None:
    """Run it twice without opening the door: still one full drum, no queue."""
    await dryer.run_a_load()
    await dryer.run_a_load()
    assert dryer.attrs["pending_count"] == 0
    assert dryer.attrs["drum_full"] is True
    assert len(dryer.attrs["finished"]) == 2, "the second load was not recorded"


# --- the cleaning light -----------------------------------------------


async def test_a_full_dryer_turns_the_cleaning_light_amber(
    hass: HomeAssistant, dryer: Dryer
) -> None:
    """Without this the dryer is invisible from the rail.

    The washer reaches the light through its pending count. A dryer has no
    pending count and never will, so a light that only reads that one would
    stay green with a full drum sitting in the garage.
    """
    status = CleaningStatusSensor(FakeEntry(), [dryer.sensor])
    assert status.native_value == CLEANING_GREEN

    await dryer.run_a_load()
    assert status.native_value == CLEANING_AMBER
    assert "Tumble dryer" in status.extra_state_attributes["detail"]
    assert "empty" in status.extra_state_attributes["detail"]

    await dryer.open_door()
    assert status.native_value == CLEANING_GREEN
