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

from datetime import date, timedelta

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
    assert "vs_week_text" in a


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
    # left to average, which is not enough for either window.
    assert m.attrs["week_cost"] is None
    assert m.attrs["vs_week_pct"] is None
    assert m.attrs["vs_week_text"] is None
    assert m.attrs["month_cost"] is None
    # The count is still published, so "why is there no comparison" has an
    # answer rather than a silence.
    assert m.attrs["week_days"] == 2


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
    assert a["week_cost"] == pytest.approx(round(0.1 * 48 * RATE + STANDING, 2), abs=0.02)
    assert a["vs_week_pct"] is not None and a["vs_week_pct"] > 100
    assert "above the week" in a["vs_week_text"]


async def test_an_ordinary_day_is_about_average(hass: HomeAssistant, freezer) -> None:
    """Within the band it says "about average" and stops talking.

    Without a band every normal day reads as 3% up or 2% down, and a
    comparison that always has something to say is one nobody reads.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    for day in ("2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19"):
        publish(hass, day)
        await m.settle()

    assert m.attrs["vs_week_pct"] == 0
    assert m.attrs["vs_week_text"] == "about the week"


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
    assert fresh._window(7, "2026-09-19")["cost"] is not None


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


# --- what the house draws asleep, and when that is news ---------------
#
# The floor is the figure this whole exercise turned out to be worth doing
# for, and a floor on its own means nothing: 286 W is a number nobody has a
# feel for. Against a usual 286 W, a night at 420 W means something was left
# running. So the norm is the thing being tested here.


def night(watts: float) -> list[float]:
    """Saturday's daytime, with the overnight floor set to `watts`.

    A half-hour slot at W watts is W * 0.5 / 1000 kWh, so this drives the
    baseline arithmetic through the same path the real charges do rather
    than poking the attribute.
    """
    slot = watts * 0.5 / 1000
    return [slot] * 12 + SATURDAY[12:]


async def _nights(hass: HomeAssistant, freezer, watts: list[float]) -> Meter:
    """One settled day per entry, oldest first, ending on 2026-09-19."""
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    start = 19 - len(watts) + 1
    for offset, w in enumerate(watts):
        publish(hass, f"2026-09-{start + offset:02d}", night(w))
        await m.settle()
    return m


async def test_four_nights_are_not_a_usual(hass: HomeAssistant, freezer) -> None:
    """A floor is the quietest number the house makes; three is not a norm.

    Deliberately stricter than the cost average. This figure's whole job is
    to be what an odd night fails against, so a norm one odd night away from
    being wrong is worse than no norm.
    """
    m = await _nights(hass, freezer, [280, 280, 280, 280])
    assert m.attrs["baseline_norm"] is None
    assert m.attrs["baseline_excess_pct"] is None
    # The night's own floor is still reported -- it is a fact either way.
    assert m.attrs["baseline_watts"] == 280


async def test_a_night_above_its_usual_floor_is_measured(
    hass: HomeAssistant, freezer
) -> None:
    """Five quiet nights, then one at half again. 50% over."""
    m = await _nights(hass, freezer, [280, 280, 280, 280, 280, 420])
    a = m.attrs
    assert a["baseline_watts"] == 420
    assert a["baseline_norm"] == 280
    assert a["baseline_excess_pct"] == 50
    assert a["baseline_text"] == "420 W overnight against a usual 280 W"


async def test_an_ordinary_night_says_only_what_it_drew(
    hass: HomeAssistant, freezer
) -> None:
    """At the usual floor there is nothing to compare, so it does not.

    A sentence that says "3% under usual" every single morning is how a
    figure stops being read at all.
    """
    m = await _nights(hass, freezer, [280, 280, 280, 280, 280, 284])
    assert m.attrs["baseline_excess_pct"] == 1
    assert m.attrs["baseline_text"] == "284 W overnight"


async def test_one_wild_night_does_not_raise_the_bar(
    hass: HomeAssistant, freezer
) -> None:
    """The norm is a median, and this is why.

    Guests, a wash left running, an evening of the oven on -- a mean would
    let one of those lift the very bar it should have failed against. Here
    five nights at 280 and one at 2000 leave the usual floor at 280, so the
    next bad night is still caught.
    """
    m = await _nights(hass, freezer, [280, 280, 2000, 280, 280, 420])
    assert m.attrs["baseline_norm"] == 280, m.attrs["recent_days"]
    assert m.attrs["baseline_excess_pct"] == 50


async def test_a_night_is_not_in_its_own_usual(hass: HomeAssistant, freezer) -> None:
    """Included, a night is partly measured against itself.

    Six nights at 280 and a seventh at 560: in its own norm the median rises
    and the excess understates. Excluded, it is cleanly double.
    """
    m = await _nights(hass, freezer, [280, 280, 280, 280, 280, 280, 560])
    assert m.attrs["baseline_norm"] == 280
    assert m.attrs["baseline_excess_pct"] == 100


async def test_the_floor_is_kept_per_night(hass: HomeAssistant, freezer) -> None:
    """The rows carry it, because one night cannot answer this and a
    fortnight can -- and the source sensor only ever holds one day."""
    m = await _nights(hass, freezer, [280, 300, 290])
    watts = [row["baseline_watts"] for row in m.attrs["recent_days"]]
    assert sorted(watts) == [280, 290, 300], m.attrs["recent_days"]


# --- the day against the week and the month --------------------------
#
# Two windows rather than one blended average, because they answer
# differently exactly when it matters: a cold snap moves the week and leaves
# the month alone, and that gap is the information.


async def _days(hass: HomeAssistant, freezer, costs: list[float]) -> Meter:
    """One settled day per entry, oldest first, ending on 2026-09-19.

    `costs` are relative consumption multipliers rather than pounds -- the
    day is driven through the real charges so the totals come out of the
    same arithmetic the card will read.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    last = date(2026, 9, 19)
    for offset, factor in enumerate(costs):
        day = last - timedelta(days=len(costs) - 1 - offset)
        publish(hass, day.isoformat(), [c * factor for c in SATURDAY])
        await m.settle()
    return m


async def test_the_week_and_the_month_can_disagree(
    hass: HomeAssistant, freezer
) -> None:
    """A quiet month, a heavy week, and a day that is normal for the week.

    Twenty quiet days, then a full heavy week, then a heavy day. Against the
    week it is unremarkable; against the month it is half again up -- and a
    single blended average would have split the difference and said neither.

    The week has to be SEVEN heavy days, not six: with six, the seventh day
    in the window is still a quiet one, the week average lands at £7.00
    against a £7.50 day, and the test would be asserting 7% while claiming
    to demonstrate 0.
    """
    m = await _days(hass, freezer, [1.0] * 20 + [2.0] * 7 + [2.0])
    a = m.attrs

    assert a["week_days"] == 7
    assert a["month_days"] == 27
    assert a["vs_week_pct"] == 0, a["week_cost"]
    assert a["vs_month_pct"] == 53
    assert a["vs_week_text"] == "about the week"
    assert "above the month" in a["vs_month_text"]


async def test_a_window_says_how_many_days_it_had(
    hass: HomeAssistant, freezer
) -> None:
    """Until the history fills, "month" is the mean of what there is.

    Something has to say so, or the label implies thirty days of evidence
    that do not exist yet.
    """
    m = await _days(hass, freezer, [1.0] * 5)
    a = m.attrs
    assert a["week_days"] == 4
    assert a["month_days"] == 4
    assert a["week_cost"] == a["month_cost"], "same days, so the same mean"


async def test_the_windows_carry_kwh_as_well_as_cost(
    hass: HomeAssistant, freezer
) -> None:
    """Both, because a tariff change moves one and not the other."""
    m = await _days(hass, freezer, [1.0] * 8)
    a = m.attrs
    assert a["week_kwh"] == pytest.approx(round(sum(SATURDAY), 3))
    assert a["month_kwh"] == pytest.approx(round(sum(SATURDAY), 3))


# --- is the floor creeping -------------------------------------------


async def test_a_creeping_floor_is_not_the_same_as_a_spike(
    hass: HomeAssistant, freezer
) -> None:
    """Seven nights at 280, then seven at 340.

    The norm follows the drift and stops calling it a spike -- which is
    right, and is why the norm cannot answer this. The trend compares the
    last week of nights against the week before and sees the creep.
    """
    m = await _nights(hass, freezer, [280] * 7 + [340] * 7)
    a = m.attrs
    assert a["baseline_trend_pct"] == 21
    # And the row's own test: against a fortnight's median the latest night
    # is nothing like a spike, so nothing fires.
    assert a["baseline_excess_pct"] is not None
    assert a["baseline_excess_pct"] < 40


async def test_a_steady_floor_has_no_trend(hass: HomeAssistant, freezer) -> None:
    m = await _nights(hass, freezer, [280] * 14)
    assert m.attrs["baseline_trend_pct"] == 0


async def test_a_fortnight_is_needed_before_a_trend(
    hass: HomeAssistant, freezer
) -> None:
    """Two weeks of nights, because the figure compares one week to another."""
    m = await _nights(hass, freezer, [280] * 13)
    assert m.attrs["baseline_trend_pct"] is None


# --- the arrays a chart reads ----------------------------------------


async def test_the_series_are_arrays_a_chart_can_read(
    hass: HomeAssistant, freezer
) -> None:
    """Plain arrays, oldest first, shaped here rather than in the card.

    Length is capped so a card draws a shape rather than a texture: thirty
    five bars read from a doorway is neither.
    """
    m = await _days(hass, freezer, [1.0] * 20)
    a = m.attrs

    assert len(a["cost_series"]) == 14
    assert len(a["kwh_series"]) == 14
    assert len(a["baseline_series"]) == 14
    assert len(a["series_labels"]) == 14
    assert all(isinstance(v, (int, float)) for v in a["cost_series"])


async def test_every_series_lines_up_with_its_labels(
    hass: HomeAssistant, freezer
) -> None:
    """The bug this guards against draws every bar against the wrong day.

    A row missing one figure would be skipped by that series and kept by the
    labels, shifting everything after it. `_restore` drops such rows on the
    way in, so the lengths match by construction.
    """
    m = await _days(hass, freezer, [1.0] * 6)
    a = m.attrs
    assert (
        len(a["cost_series"])
        == len(a["kwh_series"])
        == len(a["baseline_series"])
        == len(a["series_labels"])
        == 6
    )

    # A row from a version before the floor was recorded is dropped rather
    # than left to shift the chart.
    m.sensor._restore({"recent_days": [
        {"day": "2026-09-18", "cost": 3.99, "kwh": 14.164, "baseline_watts": 286},
        {"day": "2026-09-17", "cost": 3.50, "kwh": 13.0},
    ]})
    assert len(m.sensor._history) == 1


async def test_the_series_runs_oldest_first(hass: HomeAssistant, freezer) -> None:
    """The direction a chart is read in, decided here rather than in YAML."""
    m = await _days(hass, freezer, [1.0, 2.0, 3.0])
    costs = m.attrs["cost_series"]
    assert costs == sorted(costs), costs


# --- the figures a card puts in one row ------------------------------
#
# A three-up row of metrics that mixes units reads as three unrelated
# numbers. So the floor card's trio is three wattages, and the money is
# money all the way down its own column on a different card.


async def test_the_floor_reports_a_high_and_a_low(
    hass: HomeAssistant, freezer
) -> None:
    """So a card can show three wattages rather than two and a percentage."""
    m = await _nights(hass, freezer, [280, 420, 234, 300, 290, 286])
    a = m.attrs
    assert a["baseline_high"] == 420
    assert a["baseline_low"] == 234


async def test_the_verdict_leaves_the_number_to_the_hero(
    hass: HomeAssistant, freezer
) -> None:
    """`baseline_text` repeats the watts; `baseline_verdict` does not.

    Both are right in their place: the Needs-you row has no hero and needs
    the figure in the sentence, and a card whose hero has just said "420 W"
    must not say it again underneath.
    """
    m = await _nights(hass, freezer, [280, 280, 280, 280, 280, 420])
    a = m.attrs
    assert a["baseline_verdict"] == "50% above usual"
    assert "420" not in a["baseline_verdict"]
    assert "420" in a["baseline_text"], "the row still wants the figure"


async def test_an_ordinary_night_reads_as_about_usual(
    hass: HomeAssistant, freezer
) -> None:
    m = await _nights(hass, freezer, [280, 280, 280, 280, 280, 284])
    assert m.attrs["baseline_verdict"] == "about usual"


async def test_the_trend_reads_as_a_sentence(hass: HomeAssistant, freezer) -> None:
    m = await _nights(hass, freezer, [280] * 7 + [340] * 7)
    assert m.attrs["baseline_trend_text"] == "21% above last week"


async def test_the_windows_carry_money_the_way_people_say_it(
    hass: HomeAssistant, freezer
) -> None:
    """Formatted in the backend, like every other cost in this house.

    A card prefixing "£" onto a bare number would render 87p as "£0.87",
    and the switch at a pound is a rule about the number rather than
    something a suffix can express.
    """
    m = await _days(hass, freezer, [1.0] * 8)
    a = m.attrs
    assert a["week_cost_text"] == "£3.99"
    assert a["month_cost_text"] == "£3.99"

    # A very cheap week is said in pence, not pounds.
    m2 = await _days(hass, freezer, [0.1] * 8)
    assert m2.attrs["week_cost_text"].endswith("p"), m2.attrs["week_cost_text"]


async def test_no_window_means_no_money_text(hass: HomeAssistant, freezer) -> None:
    """Absent rather than "£0.00", which is a week that cost nothing."""
    m = await _days(hass, freezer, [1.0, 1.0])
    a = m.attrs
    assert a["week_cost"] is None
    assert a["week_cost_text"] is None
