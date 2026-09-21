"""The day's electricity, reduced from Octopus's half-hours.

The fixture is a real day: Saturday 19 September 2026, as the integration
actually reported it -- 14.164 kWh, £3.99, forty-eight slots, a mid-morning
peak and a flat 24.7788p tariff. Every expected figure below was worked out
from that array rather than from the code, which is the only way a reduction
test is worth anything.

Three of these are about refusing to answer, and they are the ones that
matter. A cost that is four days old, a today that does not exist, an
average of two days -- the tempting behaviour in all three cases is to
produce a number, and a number is exactly what must not appear.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.home_signals.energy import (
    EnergyDaySensor,
    _comparison,
    _day_label,
)

SOURCE = "sensor.octopus_previous_accumulative_cost"
TODAY_COST = "sensor.octopus_current_accumulative_cost"
TODAY_KWH = "sensor.octopus_current_accumulative_consumption"

RATE = 0.247788

#: Saturday 19 September 2026, half-hour by half-hour, as metered.
SATURDAY = [
    0.141, 0.135, 0.143, 0.150, 0.176, 0.161, 0.145, 0.139,
    0.132, 0.129, 0.132, 0.134, 0.137, 0.222, 0.484, 0.548,
    0.313, 0.394, 0.673, 0.349, 0.352, 0.685, 0.592, 0.331,
    0.377, 0.334, 0.320, 0.376, 0.373, 0.396, 0.377, 0.365,
    0.317, 0.332, 0.367, 0.358, 0.358, 0.298, 0.324, 0.260,
    0.243, 0.426, 0.377, 0.244, 0.155, 0.137, 0.117, 0.136,
]

STANDING = 0.47977125


def charges(day: str, consumption: list[float], *, offset: str = "+01:00") -> list[dict]:
    """The `charges` array the way Octopus publishes it.

    British Summer Time, because the real one is: the slots carry a +01:00
    offset and the hour-of-day arithmetic in the sensor has to resolve them
    against the house's own clock rather than against UTC. A fixture written
    in UTC would pass while the real thing was an hour out.
    """
    rows = []
    for index, kwh in enumerate(consumption):
        start_h, start_m = divmod(index * 30, 60)
        end_h, end_m = divmod((index + 1) * 30, 60)
        rows.append(
            {
                "start": f"{day}T{start_h:02d}:{start_m:02d}:00{offset}",
                "end": f"{day}T{end_h % 24:02d}:{end_m:02d}:00{offset}",
                "rate": RATE,
                "consumption": kwh,
                "cost": round(kwh * RATE, 2),
                "raw_cost": kwh * RATE,
            }
        )
    return rows


def publish(hass: HomeAssistant, day: str, consumption: list[float] | None = None) -> None:
    """Report a settled day, as the Octopus integration does."""
    rows = charges(day, SATURDAY if consumption is None else consumption)
    usage = sum(row["raw_cost"] for row in rows)
    hass.states.async_set(
        SOURCE,
        str(round(usage + STANDING, 2)),
        {
            "charges": rows,
            "standing_charge": STANDING,
            "total_without_standing_charge": round(usage, 2),
            "total": round(usage + STANDING, 2),
        },
        force_update=True,
    )


class Meter:
    """The sensor, and the clock it reads the calendar off."""

    def __init__(self, hass: HomeAssistant, sensor: EnergyDaySensor, freezer) -> None:
        self.hass = hass
        self.sensor = sensor
        self.freezer = freezer

    async def settle(self) -> None:
        await self.hass.async_block_till_done()

    @property
    def state(self):
        return self.sensor.native_value

    @property
    def attrs(self) -> dict:
        return self.sensor.extra_state_attributes


class FakeEntry:
    entry_id = "test_entry"
    data: dict = {}

    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}


async def _meter(hass: HomeAssistant, freezer, options: dict) -> Meter:
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to("2026-09-21 12:00:00+01:00")
    sensor = EnergyDaySensor(FakeEntry(options))
    sensor.hass = hass
    sensor.entity_id = "sensor.energy_day"
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return Meter(hass, sensor, freezer)


@pytest.fixture
async def meter(hass: HomeAssistant, freezer):
    """Monday lunchtime, with Saturday's data in -- which is the real case."""
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    publish(hass, "2026-09-19")
    await m.settle()
    return m


@pytest.fixture
async def metered(hass: HomeAssistant, freezer):
    """The same, with a Home Mini reporting today as well."""
    m = await _meter(
        hass,
        freezer,
        {
            "energy_cost_sensor": SOURCE,
            "energy_today_cost": TODAY_COST,
            "energy_today_kwh": TODAY_KWH,
        },
    )
    publish(hass, "2026-09-19")
    hass.states.async_set(TODAY_COST, "1.90")
    hass.states.async_set(TODAY_KWH, "7.4")
    await m.settle()
    return m


# --- the reduction ----------------------------------------------------


async def test_the_day_is_reduced_to_the_figures_a_card_wants(meter: Meter) -> None:
    """Forty-eight slots in, one line of a card out."""
    a = meter.attrs
    assert meter.state == pytest.approx(3.99)
    assert a["kwh"] == pytest.approx(14.164)
    assert a["usage"] == pytest.approx(3.51)
    assert a["standing_p"] == 48
    assert a["peak_slot"] == "10:30"
    assert a["peak_kwh"] == pytest.approx(0.685)
    assert a["slots"] == 48
    assert a["cost_text"] == "£3.99"


async def test_the_baseline_is_what_the_house_draws_asleep(meter: Meter) -> None:
    """286 W between midnight and six, and half the day's electricity.

    The figure the whole exercise turned out to be worth doing for. No tariff
    change touches it and no price chart would ever have shown it -- it falls
    out of the half-hours as a by-product.
    """
    a = meter.attrs
    assert a["baseline_watts"] == 286
    assert a["baseline_share"] == 48


async def test_it_reports_the_date_it_has_rather_than_yesterday(meter: Meter) -> None:
    """Monday, with Saturday's figures. Two days, not one.

    The reason nothing in this sensor says "yesterday": it is not, twice a
    week, and a label that silently means a different day each time it is
    read is worse than no label.
    """
    a = meter.attrs
    assert a["for_day"] == "2026-09-19"
    assert a["for_date"] == "Sat 19 Sep"
    assert a["days_late"] == 2
    assert a["stale"] is False


async def test_a_day_too_far_behind_goes_quiet(hass: HomeAssistant, freezer) -> None:
    """Octopus stopped delivering. The cell disappears rather than lying.

    A figure that has stopped being updated is indistinguishable from one
    that is current, which is the whole failure being designed against. The
    date and the lag stay in the attributes, so an assistant can still say
    why there is nothing on the card.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    publish(hass, "2026-09-14")  # a week before the frozen Monday
    await m.settle()

    assert m.attrs["days_late"] == 7
    assert m.attrs["stale"] is True
    assert m.state is None, "a week-old total was still being reported"


async def test_nothing_to_read_publishes_nothing(hass: HomeAssistant, freezer) -> None:
    """The Octopus integration has not fetched anything yet.

    Which is also what a restart looks like for the first minute, so this is
    the common case rather than the edge one.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    assert m.state is None
    assert "for_day" not in m.attrs


async def test_a_malformed_slot_is_skipped_rather_than_raised(
    hass: HomeAssistant, freezer
) -> None:
    """Octopus owns the shape of its own sensor and may change it.

    A house whose panel goes quiet when that happens is behaving correctly.
    One that throws on every state change is not, and a traceback per
    half-hour is how a log becomes useless.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    rows = charges("2026-09-19", SATURDAY[:4])
    rows.append({"start": "not a time", "consumption": 1.0})
    rows.append({"consumption": 2.0})
    rows.append("not even a dict")
    hass.states.async_set(
        SOURCE, "1.00", {"charges": rows, "standing_charge": STANDING}, force_update=True
    )
    await m.settle()

    assert m.attrs["slots"] == 4, "a malformed row was counted"
    assert m.attrs["kwh"] == pytest.approx(sum(SATURDAY[:4]))


# --- today, and the comparison that is a trap -------------------------


async def test_today_is_absent_without_a_live_meter(meter: Meter) -> None:
    """No Home Mini, no today -- and the keys are missing, not null.

    Missing so that a card referencing them renders a hole, and so an
    assistant can tell "not measured" from "measured as nothing". Octopus
    has no today at all without a Home Mini or a Home Pro; there is nothing
    here to derive it from and nothing is what should be said.
    """
    a = meter.attrs
    assert "today_cost" not in a
    assert "today_vs_pct" not in a
    # The comparison that DOES work without one is still there.
    assert "vs_average_text" in a


async def test_today_is_compared_against_the_same_time_of_day(metered: Meter) -> None:
    """Noon against noon, never against the whole of Saturday.

    This is the trap the module exists to avoid. Saturday cost £3.99 all in;
    by noon it had cost £1.68. Today has spent £1.90, which is 13% ABOVE
    Saturday at the same point -- while against the whole day it would read
    as 52% under, and the card would congratulate somebody for a day that is
    running hot.
    """
    a = metered.attrs
    assert a["today_cost"] == pytest.approx(1.90)
    assert a["today_kwh"] == pytest.approx(7.4)
    assert a["today_cost_text"] == "£1.90"
    assert a["same_time_cost"] == pytest.approx(1.68)
    assert a["same_time_kwh"] == pytest.approx(6.797)
    assert a["today_vs_pct"] == 13
    assert a["today_vs_pct"] > 0, "a day running hot read as a day running cold"


async def test_the_same_time_cut_moves_with_the_clock(metered: Meter) -> None:
    """At nine the cut is at nine, and the same spend is a different verdict.

    £1.90 by noon is slightly over; £1.90 by nine in the morning is twice
    what Saturday had spent by then. The cut has to follow the clock or the
    comparison is only right at the moment it was computed.
    """
    metered.freezer.move_to("2026-09-21 09:00:00+01:00")
    # A reading is what re-reads the clock in practice -- a Home Mini reports
    # every minute, so the cut is never more than one behind. The periodic
    # recompute is a backstop for the case where nothing is reporting at all,
    # and does the same thing.
    metered.hass.states.async_set(TODAY_COST, "1.90", force_update=True)
    await metered.settle()

    a = metered.attrs
    assert a["same_time_cost"] == pytest.approx(0.95)
    assert a["today_vs_pct"] == 100


async def test_the_comparison_is_named_for_the_day_it_uses(metered: Meter) -> None:
    """Not "vs yesterday", because on a two-day lag that is wrong."""
    assert "Sat 19 Sep" in metered.attrs["today_vs_text"]


# --- the average, which is the comparison that always works -----------


async def test_two_days_are_not_an_average(hass: HomeAssistant, freezer) -> None:
    """A mean of two numbers is two numbers.

    And a day is never in its own average, so the first day has nothing to
    be compared with at all. Publishing a percentage there would be a figure
    invented to fill a slot.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    for day in ("2026-09-17", "2026-09-18", "2026-09-19"):
        publish(hass, day)
        await m.settle()

    assert m.attrs["days_of_history"] == 3
    # Three days in, but the day being judged is excluded -- so only two are
    # left to average, which is not enough.
    assert m.attrs["average_cost"] is None
    assert m.attrs["vs_average_pct"] is None
    assert m.attrs["vs_average_text"] is None


async def test_a_day_is_judged_against_the_others_and_not_itself(
    hass: HomeAssistant, freezer
) -> None:
    """Three quiet days and one expensive one, and the expensive one shows.

    Included in its own average, a day is measured partly against itself,
    which flattens exactly the comparison the figure exists to make: four
    days of £1 and one of £2 puts the average at £1.20 and calls the spike
    67% up, when it is double.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    quiet = [0.1] * 48          # about 24p of electricity, plus standing
    for day in ("2026-09-16", "2026-09-17", "2026-09-18"):
        publish(hass, day, quiet)
        await m.settle()

    publish(hass, "2026-09-19")  # the real, much busier Saturday
    await m.settle()

    a = m.attrs
    assert a["days_of_history"] == 4
    assert a["average_cost"] == pytest.approx(round(0.1 * 48 * RATE + STANDING, 2), abs=0.02)
    assert a["vs_average_pct"] is not None and a["vs_average_pct"] > 100
    assert "above average" in a["vs_average_text"]


async def test_an_ordinary_day_is_about_average(hass: HomeAssistant, freezer) -> None:
    """Within the band it says "about average" and stops talking.

    Without a band every normal day reads as 3% up or 2% down, and a
    comparison that always has something to say is one nobody reads.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    for day in ("2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19"):
        publish(hass, day)
        await m.settle()

    assert m.attrs["vs_average_pct"] == 0
    assert m.attrs["vs_average_text"] == "about average"


async def test_a_revised_day_replaces_rather_than_doubles(
    hass: HomeAssistant, freezer
) -> None:
    """Octopus restates a day occasionally. It is still one day.

    Two rows for one date would weight it twice in the average and make the
    fortnight shorter than it says it is.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    publish(hass, "2026-09-19")
    await m.settle()

    publish(hass, "2026-09-19", [c * 2 for c in SATURDAY])
    await m.settle()

    rows = m.attrs["recent_days"]
    assert len(rows) == 1
    assert rows[0]["kwh"] == pytest.approx(round(sum(SATURDAY) * 2, 3))


async def test_the_recent_days_come_back_after_a_restart(
    hass: HomeAssistant, freezer
) -> None:
    """The one thing here that cannot be recomputed.

    The source sensor only ever holds a single day, so the house's own
    recent average is accumulated as the days go past. Losing it on every
    restart would lose the only comparison that works without a Home Mini,
    every time Home Assistant updates.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    for day in ("2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19"):
        publish(hass, day)
        await m.settle()
    kept = m.attrs["recent_days"]
    assert len(kept) == 4

    fresh = EnergyDaySensor(FakeEntry({"energy_cost_sensor": SOURCE}))
    fresh.hass = hass
    fresh._restore({"recent_days": kept})
    assert len(fresh._history) == 4
    assert fresh._average("2026-09-19")[0] is not None


# --- the wording ------------------------------------------------------


def test_a_day_carries_its_weekday_and_its_date() -> None:
    """The weekday is the half you recognise; the date is the half you check."""
    from datetime import date

    assert _day_label(date(2026, 9, 19)) == "Sat 19 Sep"
    assert _day_label(date(2026, 9, 1)) == "Tue 1 Sep"


def test_the_comparison_reads_as_a_sentence() -> None:
    assert _comparison(None, "average") is None
    assert _comparison(0, "average") == "about average"
    assert _comparison(4, "average") == "about average"
    assert _comparison(23, "average") == "23% above average"
    assert _comparison(-40, "Sat 19 Sep") == "40% under Sat 19 Sep"
