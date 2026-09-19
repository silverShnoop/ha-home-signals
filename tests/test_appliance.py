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


# --- what the machine is doing, from the draw -------------------------


async def test_the_real_cycle_reads_as_the_phases_it_actually_was(machine) -> None:
    """Replayed from the wash measured end to end on 19 Sep 2026.

    Not invented numbers. The bands were derived from this trace, so the
    trace is what has to come back out of them -- otherwise the thresholds
    are fitted to nothing and the first real cycle disagrees with the card.

    The shape that matters: heat and spin each happen MORE THAN ONCE, with
    tumble between them. This is not a four-step sequence with a finish
    line; it is a list of things the machine has done, and a card that
    drew it as a progress bar would be inventing a promise.
    """
    m = machine
    await m.draw(10, for_minutes=0.5)
    await m.draw(28, for_minutes=1.2)        # fill
    await m.draw(2234, for_minutes=1)
    await m.draw(2256, for_minutes=2.8)      # heat
    await m.draw(43, for_minutes=1)
    await m.draw(45, for_minutes=3.8)        # tumble
    await m.draw(2233, for_minutes=0.7)      # heat again
    await m.draw(48, for_minutes=2)
    await m.draw(62, for_minutes=4)
    await m.draw(118, for_minutes=0.1)       # the drum lurching, still tumble
    await m.draw(57, for_minutes=5.7)        # tumble
    await m.draw(203, for_minutes=0.5)
    await m.draw(302, for_minutes=1.2)       # spin
    await m.draw(54, for_minutes=7)          # tumble
    await m.draw(253, for_minutes=1.8)       # spin again

    kinds = [p["kind"] for p in m.sensor.extra_state_attributes["phases"]]
    assert kinds == [
        "fill", "heat", "tumble", "heat", "tumble", "spin", "tumble", "spin"
    ], kinds
    assert m.sensor.extra_state_attributes["phase"] == "spin"


async def test_a_brief_lurch_into_the_spin_band_is_not_a_spin(machine) -> None:
    """A run has to LAST before it is a phase, and this pins that.

    Two guards stand between a twitch and a phantom phase. The first is
    that a lone reading commits nothing: a kind needs a second reading to
    confirm it. That one is not enough on its own, because at the plug's
    five-second interval a brief excursion easily spans two readings. So
    a run must also last MIN_PHASE_SECONDS.

    The watts here are chosen to sit just over the spin floor rather than
    copied from the trace -- the trace's own lurches peak at 107-120 W and
    are caught by the threshold long before they reach this guard. A
    heavy wet load slapping the drum is the case this stands against: a
    strip that grew a phase every time that happened would be unreadable.
    """
    m = machine
    await m.draw(30, for_minutes=2)
    await m.draw(50, for_minutes=4)
    await m.draw(188, for_minutes=0.1)       # over the floor, reading one
    await m.draw(204, for_minutes=0.1)       # still over it, 6s later
    await m.draw(46, for_minutes=3)

    kinds = [p["kind"] for p in m.sensor.extra_state_attributes["phases"]]
    assert "spin" not in kinds, kinds


async def test_the_tumble_ceiling_held_is_still_a_tumble(machine) -> None:
    """The spin floor sits above the highest tumble ever measured, on purpose.

    This is the one guard the trace does NOT settle by itself. Tumble ran
    12-90 W and lurched to 120; spin ramped to 203-390. Any floor between
    120 and 203 replays that cycle identically, so 150 is a chosen margin,
    not a fitted one -- drop it to 100 and the measured wash still comes
    out right.

    What the margin buys is the case the trace never showed: a heavy load
    that tumbles at the top of its band and STAYS there, long past the
    duration guard. That must read as a tumble, and only the threshold
    can make it so.
    """
    m = machine
    await m.draw(30, for_minutes=2)
    await m.draw(50, for_minutes=4)
    await m.draw(118, for_minutes=1)
    await m.draw(112, for_minutes=1.5)

    kinds = [p["kind"] for p in m.sensor.extra_state_attributes["phases"]]
    assert "spin" not in kinds, kinds


async def test_a_hard_spin_is_not_mistaken_for_heat(machine) -> None:
    """The heat floor is the other chosen margin, and the same care applies.

    Measured spins topped out at 390 W, so anything from there to 2.2 kW
    replays this cycle identically -- the trace cannot pick the number.
    The module's own description of a wash puts a spin at 600 W, and a
    600 W spin read as a heat would put a kettle icon on the strip while
    the drum was flinging water out.
    """
    m = machine
    await m.draw(30, for_minutes=2)
    await m.draw(50, for_minutes=4)
    await m.draw(580, for_minutes=1)
    await m.draw(640, for_minutes=1)

    kinds = [p["kind"] for p in m.sensor.extra_state_attributes["phases"]]
    assert kinds[-1] == "spin", kinds


async def test_a_soak_does_not_let_a_pending_phase_span_it(machine) -> None:
    """A run that stops being drawn stops accruing, and this is the seam.

    A wash soaks: minutes at a few watts, under the plug's own start
    threshold. A phase WAITING to be confirmed must not treat the reading
    before the soak and the reading after it as one unbroken run -- that
    commits a spin off two readings ten minutes apart and back-dates it
    to before the silence.

    A phase already committed is the opposite case and stays that way:
    the measured trace records tumble as 21:02-21:09 including its lulls,
    so the soak counts towards the tumble it interrupted.
    """
    m = machine
    await m.draw(30, for_minutes=2)
    await m.draw(50, for_minutes=4)
    await m.draw(2200, for_minutes=3)        # heat
    await m.draw(60, for_minutes=3)          # tumble
    await m.draw(212, for_minutes=0)         # one reading in the spin band
    await m.draw(5, for_minutes=10)          # soaking, under start_watts
    await m.draw(206, for_minutes=0)         # one reading, ten minutes on
    await m.draw(58, for_minutes=3)

    phases = m.sensor.extra_state_attributes["phases"]
    kinds = [p["kind"] for p in phases]
    assert kinds == ["fill", "heat", "tumble"], kinds
    assert phases[-1]["seconds"] >= 900, phases[-1]


async def test_a_sustained_ramp_is_a_spin(machine) -> None:
    """The other side of it: the guard must not swallow a real spin.

    The measured spins ran 100 seconds to four minutes and climbed to
    300-390 W. A threshold that filtered those out would be worse than
    no strip at all.
    """
    m = machine
    await m.draw(30, for_minutes=2)
    await m.draw(50, for_minutes=4)
    await m.draw(203, for_minutes=0.6)
    await m.draw(302, for_minutes=1.2)

    kinds = [p["kind"] for p in m.sensor.extra_state_attributes["phases"]]
    assert kinds[-1] == "spin", kinds


async def test_heat_is_never_mistaken_for_spin(machine) -> None:
    """2.2 kW against a few hundred. The one classification with room."""
    m = machine
    await m.draw(20, for_minutes=2)
    await m.draw(2250, for_minutes=4)

    kinds = [p["kind"] for p in m.sensor.extra_state_attributes["phases"]]
    assert kinds[-1] == "heat", kinds


async def test_the_timeline_survives_the_wash_ending(machine) -> None:
    """The washing sits in the drum afterwards, and the card still shows it.

    Clearing the phases when the cycle ends would empty the strip at
    exactly the moment somebody walks over to find out what happened.
    """
    m = machine
    await m.draw(30, for_minutes=2)
    await m.draw(2250, for_minutes=4)
    await m.draw(50, for_minutes=8)
    await m.draw(0, for_minutes=0)
    await m.advance(6)

    assert m.sensor.state == APPLIANCE_IDLE
    kinds = [p["kind"] for p in m.sensor.extra_state_attributes["phases"]]
    assert kinds, "the strip emptied the moment the wash finished"
    assert "heat" in kinds


async def test_a_restart_does_not_blank_the_strip(machine) -> None:
    """The washing is still in the drum afterwards, and so is its story.

    A cycle in flight is deliberately not resumed across a restart -- we
    cannot know what the machine did while we were not looking. The
    timeline of a wash that already FINISHED is a different thing: it is
    a record, not a guess, and blanking it would empty the card at the
    one moment somebody is walking over to read it. A new wash clears it
    anyway, because `_begin` starts a fresh list.
    """
    m = machine
    await m.draw(30, for_minutes=2)
    await m.draw(2250, for_minutes=4)
    await m.draw(50, for_minutes=8)
    await m.draw(0, for_minutes=0)
    await m.advance(6)
    before = m.sensor.extra_state_attributes["phases"]
    assert [p["kind"] for p in before] == ["fill", "heat", "tumble"], before

    after_restart = ApplianceCycleSensor(FakeEntry(), dict(SPEC))
    after_restart.hass = m.hass
    after_restart.entity_id = "sensor.washing_machine_cycle"
    after_restart._restore(m.sensor.extra_state_attributes)

    assert after_restart.extra_state_attributes["phases"] == before
    assert after_restart.extra_state_attributes["phase"] == "tumble"


async def test_a_new_wash_starts_a_new_timeline(machine) -> None:
    """Last week's phases are not this wash's."""
    m = machine
    await m.draw(30, for_minutes=2)
    await m.draw(2250, for_minutes=4)
    await m.draw(50, for_minutes=8)
    await m.draw(0, for_minutes=0)
    await m.advance(6)
    first = [p["kind"] for p in m.sensor.extra_state_attributes["phases"]]
    assert "heat" in first

    await m.draw(40, for_minutes=3)
    kinds = [p["kind"] for p in m.sensor.extra_state_attributes["phases"]]
    assert "heat" not in kinds, f"the new wash inherited the old one: {kinds}"
    assert kinds == ["fill"], kinds
