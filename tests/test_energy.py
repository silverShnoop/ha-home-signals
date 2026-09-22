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


async def test_a_stale_day_publishes_nothing_a_card_could_draw(
    hass: HomeAssistant, freezer
) -> None:
    """The state going quiet is not enough -- every card reads attributes.

    `cost_text`, `week_cost_text`, `floor_cost_text`: the three Maintenance
    cards are built out of these, and not one of them looks at the state. So
    an empty state beside full attributes would keep drawing Saturday's
    figures on Thursday under a state nobody reads, which is exactly the
    failure the staleness rule exists to prevent.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    publish(hass, "2026-09-14")
    await m.settle()

    a = m.attrs
    assert a["for_date"] == "Mon 14 Sep", "the date that explains the silence"
    for key in (
        "cost_text",
        "kwh",
        "week_cost_text",
        "month_cost_text",
        "vs_week_text",
        "floor_cost_text",
        "rest_cost_text",
        "baseline_watts",
        "baseline_text",
        "cost_series",
        "series_labels",
    ):
        assert key not in a, f"a stale day was still publishing {key}"


async def test_a_stale_day_still_keeps_the_history(
    hass: HomeAssistant, freezer
) -> None:
    """Because `recent_days` is what the restore reads back.

    Nothing draws it, so it is not a figure going stale -- and it is the one
    thing here that cannot be recomputed from the source sensor, which only
    ever holds a single day. Dropping it while Octopus was quiet would throw
    the house's accumulated history away on the next restart.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    publish(hass, "2026-09-14")
    await m.settle()

    a = m.attrs
    assert a["days_of_history"] == 1
    assert [row["day"] for row in a["recent_days"]] == ["2026-09-14"]


async def test_a_stale_day_does_not_take_today_with_it(
    hass: HomeAssistant, freezer
) -> None:
    """Today comes from a different meter and is not stale.

    A Home Mini reports today; Octopus reports settled days. When Octopus
    stops delivering, the freshest figure in the house is still arriving --
    and it is the one worth having. Only the comparison goes, because that
    is made of the day that went quiet.
    """
    m = await _meter(
        hass,
        freezer,
        {
            "energy_cost_sensor": SOURCE,
            "energy_today_cost": TODAY_COST,
            "energy_today_kwh": TODAY_KWH,
        },
    )
    publish(hass, "2026-09-14")
    hass.states.async_set(TODAY_COST, "1.90")
    hass.states.async_set(TODAY_KWH, "7.4")
    await m.settle()

    a = m.attrs
    assert a["stale"] is True
    assert a["today_cost_text"] == "£1.90"
    assert a["today_kwh"] == 7.4
    for key in ("today_vs_text", "today_vs_pct", "same_time_cost"):
        assert key not in a, f"a stale day was still comparing against {key}"


async def test_a_meter_with_no_settled_day_at_all_still_reports_today(
    hass: HomeAssistant, freezer
) -> None:
    """Which is every restart, for the first minute, and a new account.

    Octopus has fetched nothing yet. The live meter has not stopped, so
    there is no reason for the card to be empty.
    """
    m = await _meter(
        hass,
        freezer,
        {
            "energy_cost_sensor": SOURCE,
            "energy_today_cost": TODAY_COST,
            "energy_today_kwh": TODAY_KWH,
        },
    )
    hass.states.async_set(TODAY_COST, "1.90")
    await m.settle()

    a = m.attrs
    assert "for_day" not in a
    assert a["today_cost_text"] == "£1.90"


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


# --- the floor in money, and the day split around it -----------------
#
# A floor in watts is not a fact anybody can act on. What it costs, and how
# much of the day was NOT it, are -- the floor is the part you change by
# finding something and unplugging it, the rest is the part you changed by
# living in the house.


async def test_the_day_splits_into_floor_rest_and_standing(meter: Meter) -> None:
    """And the three add up to the day's total, to the penny.

    A table whose rows do not sum to the figure above them is a table
    nobody trusts -- so `rest` is the remainder rather than a second
    multiplication, and the rounding penny lands there rather than
    vanishing.
    """
    a = meter.attrs
    assert a["floor_kwh"] == pytest.approx(6.9)
    assert a["rest_kwh"] == pytest.approx(7.3)
    assert a["floor_cost"] == pytest.approx(1.70)
    assert a["rest_cost"] == pytest.approx(1.81)

    total = a["floor_cost"] + a["rest_cost"] + a["standing_p"] / 100
    assert total == pytest.approx(meter.state), (
        f"{a['floor_cost']} + {a['rest_cost']} + {a['standing_p']}p "
        f"!= {meter.state}"
    )


async def test_the_floor_is_priced_at_the_days_own_rate(meter: Meter) -> None:
    """`usage / kwh`, not whatever the tariff says now.

    On a flat tariff they are the same number. On a variable one the day's
    own average is the only rate that can divide up the day's own money.
    """
    a = meter.attrs
    implied = a["floor_cost"] / a["floor_kwh"]
    assert implied == pytest.approx(RATE, abs=0.005)


async def test_the_floor_says_what_a_year_of_it_costs(meter: Meter) -> None:
    """The figure that makes somebody actually go and look for the cause.

    £1.70 a day is ignorable. £621 a year is not, and it is the same fact.
    """
    assert meter.attrs["floor_cost_year"] == 621


# --- where the power went, and roughly when --------------------------
#
# A day's total says nothing about the day. Two days at the same total can
# be a morning of laundry and an evening of the oven, and only one of those
# is a thing anybody would change.


async def test_the_day_cuts_into_four_six_hour_blocks(meter: Meter) -> None:
    """And they sum to the day, because they are its own slots counted once.

    That is what makes a stacked column honest: nothing here is projected
    or apportioned, unlike `baseline_watts`, which takes the overnight rate
    and asks what a whole day of it would be.
    """
    a = meter.attrs
    assert a["block_names"] == ["Overnight", "Morning", "Afternoon", "Evening"]
    assert a["block_hours"] == 6
    assert len(a["block_kwh"]) == 4
    assert sum(a["block_kwh"]) == pytest.approx(a["kwh"], abs=0.01)
    assert sum(a["block_cost"]) == pytest.approx(a["usage"], abs=0.02)


async def test_the_blocks_are_the_real_measured_saturday(meter: Meter) -> None:
    """Worked out from the fixture array, not from the code under test."""
    expected_kwh = [
        round(sum(SATURDAY[i * 12:(i + 1) * 12]), 3) for i in range(4)
    ]
    assert meter.attrs["block_kwh"] == pytest.approx(expected_kwh, abs=0.001)
    # The overnight block is the baseline's own six hours, so the two agree.
    assert meter.attrs["block_kwh"][0] == pytest.approx(
        meter.attrs["baseline_watts"] * 6 / 1000, abs=0.01
    )


async def test_a_day_of_blocks_carries_its_own_totals(meter: Meter) -> None:
    """The card prints them under each column rather than computing them."""
    row = meter.attrs["block_days"][-1]
    assert row["day"] == "2026-09-19"
    assert row["label"] == "Sat"
    assert row["total_cost"] == pytest.approx(sum(row["cost"]), abs=0.01)
    assert row["total_cost_text"].startswith("£")
    assert len(row["cost"]) == 4 and len(row["kwh"]) == 4


async def test_days_without_blocks_are_dropped_not_zero_filled(
    hass: HomeAssistant, freezer
) -> None:
    """A column of four empty segments under a real date is a lie.

    The house never used nothing. Such rows only come from a version before
    this figure existed, or a day the backfill could not reach, and a gap is
    the truth about both.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    m.sensor._history = [
        {"day": "2026-09-17", "cost": 3.0, "kwh": 12.0, "baseline_watts": 280},
        {"day": "2026-09-18", "cost": 3.2, "kwh": 12.5, "baseline_watts": 281,
         "block_cost": [0.4, 0.9, 1.0, 0.9], "block_kwh": [1.6, 3.6, 4.0, 3.6]},
    ]
    publish(hass, "2026-09-19")
    await m.settle()

    days = [row["day"] for row in m.attrs["block_days"]]
    assert days == ["2026-09-18", "2026-09-19"], "a blockless day was drawn"


async def test_the_block_card_shows_at_most_a_week(
    hass: HomeAssistant, freezer
) -> None:
    """Seven columns, each with four segments and two lines of text.

    A fortnight of those is a texture rather than a week you can read.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    m.sensor._history = [
        {"day": f"2026-09-{day:02d}", "cost": 3.0, "kwh": 12.0,
         "baseline_watts": 280, "block_cost": [0.4, 0.9, 1.0, 0.9],
         "block_kwh": [1.6, 3.6, 4.0, 3.6]}
        for day in range(6, 19)
    ]
    publish(hass, "2026-09-19")
    await m.settle()

    rows = m.attrs["block_days"]
    assert len(rows) == 7
    assert rows[-1]["day"] == "2026-09-19", "oldest first, newest last"
    assert rows[0]["day"] < rows[-1]["day"]


# --- recovering the days nobody was writing down ---------------------


async def test_an_old_day_reads_through_the_same_parser(meter: Meter) -> None:
    """Which is the whole design of the backfill.

    The recorder kept the source sensor's past states, attributes and all,
    and its attributes ARE the forty-eight half-hours. So an old day is
    recovered by handing those attributes to the same reader that handles a
    live one -- not by a second parser that would be a second thing to keep
    right.
    """
    attrs = {
        "charges": charges("2026-09-12", SATURDAY),
        "standing_charge": STANDING,
        "total_without_standing_charge": round(
            sum(row["raw_cost"] for row in charges("2026-09-12", SATURDAY)), 2
        ),
    }
    day = meter.sensor._day_from(attrs)

    assert day is not None
    assert day["day"].isoformat() == "2026-09-12"
    assert day["kwh"] == pytest.approx(14.164, abs=0.001)
    assert len(day["block_cost"]) == 4
    assert day["baseline_watts"] == 286


async def test_a_recovered_day_joins_the_history(meter: Meter) -> None:
    """And is then indistinguishable from one that arrived live."""
    before = meter.attrs["days_of_history"]
    attrs = {"charges": charges("2026-09-12", SATURDAY),
             "standing_charge": STANDING}
    meter.sensor._remember(meter.sensor._day_from(attrs))

    rows = meter.attrs["block_days"]
    assert meter.attrs["days_of_history"] == before + 1
    assert [r["day"] for r in rows] == ["2026-09-12", "2026-09-19"]
    assert all(len(r["cost"]) == 4 for r in rows)


async def test_rubbish_in_the_recorder_is_skipped_not_raised_on(
    meter: Meter,
) -> None:
    """A purged day, a half-written row, an attribute that changed shape.

    The backfill walks whatever the database hands it, so every one of
    these has to produce None rather than an exception -- a broken startup
    is a worse outcome than a shorter chart.
    """
    for attrs in ({}, {"charges": None}, {"charges": []},
                  {"charges": "nope"}, {"charges": [{"start": "x"}]}):
        assert meter.sensor._day_from(attrs) is None, attrs


async def test_the_standing_charge_says_its_year_too(meter: Meter) -> None:
    """Beside the floor's year, so the two rows are the same kind of figure.

    48p a day is background noise; £175 a year is a line on a bill. The
    difference from the floor is the point of putting them side by side --
    that one is a year somebody can go and reduce and this one is not.
    """
    a = meter.attrs
    assert a["standing_p"] == 48
    assert a["standing_cost_year"] == 175


async def test_a_floor_bigger_than_its_day_publishes_no_split(
    hass: HomeAssistant, freezer
) -> None:
    """A partial day from Octopus, where the parts would exceed the whole.

    A perfectly flat day is the boundary: every slot equal means the floor
    projected over twenty-four hours is exactly the day, and there is no
    "rest" to speak of. Anything at or past that is not a day this can
    divide up, so it does not pretend to.
    """
    m = await _meter(hass, freezer, {"energy_cost_sensor": SOURCE})
    publish(hass, "2026-09-19", [0.1] * 48)
    await m.settle()

    a = m.attrs
    assert a["baseline_watts"] == 200
    assert "floor_kwh" not in a
    assert "rest_cost_text" not in a


# --- against a night somebody remembers ------------------------------


async def test_the_floor_is_compared_to_the_night_before(
    hass: HomeAssistant, freezer
) -> None:
    """Not against the median -- nobody remembers their median.

    A median is the right thing to fire a Needs-you row off and the wrong
    thing to hand a person. "Twenty watts up on Friday" is a sentence about
    a night they were there for.
    """
    m = await _nights(hass, freezer, [280, 300])
    a = m.attrs
    assert a["prev_baseline_watts"] == 280
    assert a["baseline_vs_prev_watts"] == 20
    assert a["baseline_vs_prev_text"] == "20 W up on Fri"


async def test_a_floor_that_fell_says_so(hass: HomeAssistant, freezer) -> None:
    m = await _nights(hass, freezer, [300, 280])
    assert m.attrs["baseline_vs_prev_text"] == "20 W down on Fri"


async def test_a_floor_that_barely_moved_is_level(
    hass: HomeAssistant, freezer
) -> None:
    """Two watts is a meter, not a change.

    Without a band this reads "1 W up on Friday" every single morning,
    which is the same failure the average's band exists to prevent.
    """
    m = await _nights(hass, freezer, [280, 281])
    assert m.attrs["baseline_vs_prev_text"] == "level with Fri"


async def test_one_night_has_nothing_to_compare_against(
    hass: HomeAssistant, freezer
) -> None:
    m = await _nights(hass, freezer, [280])
    a = m.attrs
    assert "baseline_vs_prev_text" not in a
    assert a["baseline_watts"] == 280, "the night itself is still reported"
