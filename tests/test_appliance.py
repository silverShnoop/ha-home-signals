"""The cycle state machine, driven by a real clock.

Everything worth testing here is about TIME. "The draw stayed below four
watts for five unbroken minutes" is the whole design, and a test that fakes
the timer by calling it straight back proves the callback is wired rather
than that the floor holds. So these move Home Assistant's clock past the
scheduled callback and watch what the sensor decides.

The shape of each test is the shape of the bug it is guarding against. The
soak test is the reported problem: a wash whose drum rests for four minutes
must not be reported as two washes.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.home_signals.appliance import ApplianceCycleSensor
from custom_components.home_signals.const import (
    APPLIANCE_IDLE,
    APPLIANCE_OFF,
    APPLIANCE_RUNNING,
    CLEANING_AMBER,
    CLEANING_GREEN,
    CLEANING_RED,
)

POWER = "sensor.washer_power"
PLUG = "switch.washer_plug"
DOOR = "binary_sensor.washer_door"
LEAK = "binary_sensor.washer_leak"
ENERGY = "sensor.washer_energy"

SPEC = {
    "slug": "washing_machine",
    "name": "Washing machine",
    "power_sensor": POWER,
    "plug": PLUG,
    "door": DOOR,
    "leak": LEAK,
    "energy_sensor": ENERGY,
    "start_watts": 8,
    "idle_watts": 4,
    "idle_minutes": 5,
    "min_minutes": 10,
    "min_kwh": 0.05,
}


class FakeEntry:
    """Enough config entry for the sensors, without a config flow."""

    entry_id = "test_entry"
    data: dict = {}
    options: dict = {}


class Machine:
    """A washing machine you can drive, and a clock you can push."""

    def __init__(
        self, hass: HomeAssistant, sensor: ApplianceCycleSensor, freezer
    ) -> None:
        self.hass = hass
        self.sensor = sensor
        self.freezer = freezer
        self._kwh = 0.0

    def set(self, entity_id: str, value) -> None:
        self.hass.states.async_set(entity_id, str(value))

    async def draw(self, watts: float, *, for_minutes: float = 0) -> None:
        """Draw this many watts, then let that many minutes of it pass.

        Energy accrues while the power flows, as a real meter's would, so
        the minimum-energy guard is exercised rather than sidestepped.
        """
        self.set(POWER, watts)
        await self.hass.async_block_till_done()
        if for_minutes:
            self._kwh += watts * (for_minutes / 60.0) / 1000.0
            self.set(ENERGY, round(self._kwh, 6))
            await self.advance(for_minutes)

    async def advance(self, minutes: float) -> None:
        """Move the clock forward and let anything scheduled fire.

        The clock is FROZEN and then ticked, rather than the scheduler being
        poked at a future time while `utcnow()` stays put. Without that the
        sensor's own arithmetic still reads the real wall clock, so a wash
        driven across half an hour of test time measures as zero minutes and
        is discarded as too short — which is how the first run of this file
        failed eight tests against working code.
        """
        self.freezer.tick(timedelta(minutes=minutes))
        async_fire_time_changed(self.hass)
        await self.hass.async_block_till_done()

    @property
    def state(self) -> str:
        return self.sensor.native_value

    @property
    def attrs(self) -> dict:
        return self.sensor.extra_state_attributes

    @property
    def waiting(self) -> int:
        return self.attrs["pending_count"]


@pytest.fixture
async def machine(hass: HomeAssistant, freezer):
    """A washing machine plugged in, dry, door shut, drawing nothing."""
    freezer.move_to("2026-09-19 09:00:00+00:00")
    sensor = ApplianceCycleSensor(FakeEntry(), dict(SPEC))
    sensor.hass = hass
    sensor.entity_id = "sensor.washing_machine_cycle"

    hass.states.async_set(PLUG, "on")
    hass.states.async_set(DOOR, "off")
    hass.states.async_set(LEAK, "off")
    hass.states.async_set(POWER, "0")
    hass.states.async_set(ENERGY, "0")
    await hass.async_block_till_done()

    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return Machine(hass, sensor, freezer)


# --- the reported problem ---------------------------------------------


async def test_a_soak_is_not_the_end_of_the_cycle(machine: Machine) -> None:
    """Four minutes at nothing, mid-wash, is a drum resting.

    This is the bug the whole design exists to avoid: read instantaneously,
    a wash is a dozen short cycles with gaps between them, and each gap
    would be a load of laundry that does not exist.
    """
    await machine.draw(2000, for_minutes=6)
    assert machine.state == APPLIANCE_RUNNING

    await machine.draw(0, for_minutes=4)
    assert machine.state == APPLIANCE_RUNNING, "a four-minute soak ended the cycle"
    assert machine.waiting == 0

    await machine.draw(300, for_minutes=6)
    assert machine.state == APPLIANCE_RUNNING
    assert machine.waiting == 0, "the soak was counted as a finished load"


async def test_the_lull_is_measured_so_the_floor_can_stop_being_a_guess(
    machine: Machine,
) -> None:
    """Every pause is timed, which is what replaces picking a number."""
    await machine.draw(2000, for_minutes=6)
    await machine.draw(0, for_minutes=4)
    await machine.draw(300, for_minutes=1)

    lull = machine.attrs["longest_lull_seconds"]
    assert 230 <= lull <= 310, f"longest lull recorded as {lull}s, expected ~240s"


async def test_quiet_for_the_full_floor_finishes_the_cycle(machine: Machine) -> None:
    await machine.draw(2000, for_minutes=30)
    assert machine.state == APPLIANCE_RUNNING

    await machine.draw(0, for_minutes=6)
    assert machine.state == APPLIANCE_IDLE
    assert machine.waiting == 1, "a finished wash left nothing to hang"
    assert machine.attrs["drum_full"] is True


async def test_the_cycle_is_timed_to_when_it_went_quiet_not_when_we_noticed(
    machine: Machine,
) -> None:
    """Otherwise every wash is reported five minutes longer than it ran."""
    await machine.draw(2000, for_minutes=30)
    await machine.draw(0, for_minutes=6)

    ran = machine.attrs["finished"][0]["duration_minutes"]
    assert 28 <= ran <= 32, f"a 30-minute wash was recorded as {ran} minutes"


# --- things that are not laundry ---------------------------------------


async def test_a_two_minute_burst_is_not_a_load(machine: Machine) -> None:
    """Somebody nudging the dial, or a drain-only run."""
    await machine.draw(400, for_minutes=2)
    await machine.draw(0, for_minutes=6)

    assert machine.state == APPLIANCE_IDLE
    assert machine.waiting == 0, "a two-minute burst invented a load of washing"
    assert machine.attrs["drum_full"] is False


async def test_a_short_burst_of_real_power_is_not_a_load_either(
    machine: Machine,
) -> None:
    """Short, but drawing enough to clear the energy floor on its own.

    Without this the duration guard is never actually exercised: the
    two-minute burst above is rejected for using too little energy, so
    deleting the minimum-duration check entirely left every test passing.
    Three minutes at 3 kW is 0.15 kWh — comfortably over the energy floor,
    and still not a wash.
    """
    await machine.draw(3000, for_minutes=3)
    await machine.draw(0, for_minutes=6)

    assert machine.waiting == 0, "a three-minute burst was counted as a wash"
    assert machine.attrs["drum_full"] is False


async def test_a_long_run_drawing_almost_nothing_is_not_a_load(
    machine: Machine,
) -> None:
    """Long enough, but it cannot have washed anything on 9 watts."""
    await machine.draw(9, for_minutes=40)
    await machine.draw(0, for_minutes=6)

    assert machine.waiting == 0, "40 minutes at 9 W was counted as a wash"


async def test_pulling_the_plug_mid_cycle_aborts_rather_than_finishes(
    machine: Machine,
) -> None:
    """The leak automation cutting power must not produce laundry.

    This is the interaction that would otherwise be found the hard way: a
    flood, and then a reminder to hang up a wash that is sitting in six
    inches of water.
    """
    await machine.draw(2000, for_minutes=40)
    assert machine.state == APPLIANCE_RUNNING

    machine.set(PLUG, "off")
    await machine.hass.async_block_till_done()

    assert machine.state == APPLIANCE_OFF
    assert machine.waiting == 0, "an interrupted cycle was counted as a finished load"
    assert machine.attrs["powered"] is False


async def test_the_hysteresis_band_holds_whichever_state_it_is_in(
    machine: Machine,
) -> None:
    """Between the two thresholds, nothing changes. That gap IS the design."""
    await machine.draw(6, for_minutes=3)
    assert machine.state == APPLIANCE_IDLE, "6 W started a cycle on its own"

    await machine.draw(50, for_minutes=1)
    assert machine.state == APPLIANCE_RUNNING

    await machine.draw(6, for_minutes=10)
    assert machine.state == APPLIANCE_RUNNING, "6 W ended a running cycle"


async def test_a_plug_that_stops_reporting_holds_rather_than_reads_zero(
    machine: Machine,
) -> None:
    """Silence is not zero. A dead radio must not finish the wash."""
    await machine.draw(2000, for_minutes=20)
    machine.set(POWER, "unavailable")
    await machine.hass.async_block_till_done()
    await machine.advance(10)

    assert machine.state == APPLIANCE_RUNNING
    assert machine.waiting == 0


# --- the drum, and the washing ------------------------------------------


async def test_opening_the_door_empties_the_drum_but_not_the_hanging_list(
    machine: Machine,
) -> None:
    """Emptying and hanging are different facts, cleared by different things."""
    await machine.draw(2000, for_minutes=30)
    await machine.draw(0, for_minutes=6)
    assert machine.attrs["drum_full"] is True
    assert machine.waiting == 1

    machine.set(DOOR, "on")
    await machine.hass.async_block_till_done()

    assert machine.attrs["drum_full"] is False, "the door did not empty the drum"
    assert machine.waiting == 1, "unloading the machine hung the washing up"


async def test_two_loads_are_two_rows_and_the_button_clears_the_oldest(
    machine: Machine,
) -> None:
    await machine.draw(2000, for_minutes=30)
    await machine.draw(0, for_minutes=6)
    first = machine.attrs["pending"][0]["id"]

    await machine.draw(2000, for_minutes=30)
    await machine.draw(0, for_minutes=6)
    assert machine.waiting == 2

    assert machine.sensor.hung() is True
    assert machine.waiting == 1
    assert machine.attrs["pending"][0]["id"] != first, "the newest was cleared first"

    assert machine.sensor.hung() is True
    assert machine.waiting == 0
    assert machine.sensor.hung() is False, "a press with nothing waiting claimed a load"


async def test_a_named_load_clears_exactly_that_one(machine: Machine) -> None:
    """The Needs you row clears its own load, not whichever is oldest."""
    await machine.draw(2000, for_minutes=30)
    await machine.draw(0, for_minutes=6)
    oldest = machine.attrs["pending"][0]["id"]

    await machine.draw(2000, for_minutes=30)
    await machine.draw(0, for_minutes=6)
    newest = machine.attrs["pending"][1]["id"]

    assert machine.sensor.hung(newest) is True
    remaining = [p["id"] for p in machine.attrs["pending"]]
    assert remaining == [oldest]
    assert machine.sensor.hung(newest) is False, "clearing the same load twice worked"


# --- the leak is independent of the power ------------------------------


async def test_power_can_be_restored_while_the_sensor_is_still_wet(
    machine: Machine,
) -> None:
    """A wet sensor is not proof the machine is off.

    The pad stays damp long after the floor has been dealt with, and the
    cycle still has to be finished — so these two facts are reported
    separately and neither is inferred from the other.
    """
    machine.set(LEAK, "on")
    machine.set(PLUG, "off")
    await machine.hass.async_block_till_done()
    assert machine.attrs["leak"] is True
    assert machine.attrs["powered"] is False

    machine.set(PLUG, "on")
    await machine.hass.async_block_till_done()

    assert machine.attrs["leak"] is True, "restoring power was taken as drying out"
    assert machine.attrs["powered"] is True
    assert machine.state == APPLIANCE_IDLE, "the machine stayed off in its own report"


async def test_a_cycle_runs_normally_with_the_sensor_still_wet(
    machine: Machine,
) -> None:
    """Finishing the wash after a leak is a thing people do."""
    machine.set(LEAK, "on")
    await machine.hass.async_block_till_done()

    await machine.draw(2000, for_minutes=30)
    assert machine.state == APPLIANCE_RUNNING
    await machine.draw(0, for_minutes=6)
    assert machine.waiting == 1, "a wet sensor stopped the cycle being counted"


# --- the colour a tab can be -------------------------------------------


async def test_the_cleaning_light(hass: HomeAssistant, machine: Machine) -> None:
    from custom_components.home_signals.appliance import CleaningStatusSensor

    light = CleaningStatusSensor(FakeEntry(), [machine.sensor])
    light.hass = hass
    light.entity_id = "sensor.cleaning_status"

    assert light.native_value == CLEANING_GREEN

    await machine.draw(2000, for_minutes=30)
    await machine.draw(0, for_minutes=6)
    assert light.native_value == CLEANING_AMBER
    assert "1 load to hang" in light.extra_state_attributes["detail"]

    machine.set(LEAK, "on")
    await hass.async_block_till_done()
    assert light.native_value == CLEANING_RED, "water on the floor was not red"

    machine.set(PLUG, "off")
    await hass.async_block_till_done()
    assert light.native_value == CLEANING_RED, "the leak stopped being the headline"
    assert "leaking" in light.extra_state_attributes["detail"]
