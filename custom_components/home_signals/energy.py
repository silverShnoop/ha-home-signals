"""What the house's electricity cost, reduced once.

Octopus publishes the previous complete day as a single sensor whose
`charges` attribute carries all forty-eight half-hours of it. Everything a
card or an assistant wants to know about that day -- what it cost, what it
used, what the house was drawing while everybody was asleep -- is already in
there. Reducing it here rather than in a card is the difference between one
Python function and a Jinja template per tile.

Three things about this data decide the shape of everything below.

**It is not "yesterday".** The sensor is called `previous_accumulative_cost`
and the obvious reading is yesterday, but the reads land when Octopus gets
them -- one day behind, and quite often two. So nothing here ever says the
word. It reports the date it is actually describing, taken off the charges
themselves, and publishes how late that is so a card can go quiet rather
than show Saturday's total on Thursday.

**There is no "now".** A live house-wide figure needs an Octopus Home Mini
or Home Pro. Without one the API has nothing for today at all, and no amount
of shaping here invents it -- so `today_*` is an optional pair of *inputs*
rather than something this computes. Point them at the Home Mini's
accumulative sensors and today appears; leave them empty and it does not.

**Today against a whole yesterday is a trap.** At nine in the morning,
"today £1.20, yesterday £3.99" reads as a good day and means nothing at all.
So when today is available it is compared against the settled day *up to the
same time of day* -- which is only possible because the half-hours are here
to be cut. That comparison is the reason this module reduces the array
rather than passing it through.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    BASELINE_UNTIL_HOUR,
    CONF_ENERGY_COST_SENSOR,
    CONF_ENERGY_TODAY_COST,
    CONF_ENERGY_TODAY_KWH,
    ENERGY_HISTORY_DAYS,
    ENERGY_MIN_DAYS_FOR_AVERAGE,
    ENERGY_MIN_DAYS_FOR_NORM,
    ENERGY_MONTH_DAYS,
    ENERGY_NORM_DAYS,
    ENERGY_SAME_PCT,
    ENERGY_SERIES_DAYS,
    ENERGY_STALE_DAYS,
    ENERGY_WEEK_DAYS,
)
from .money import money

LOGGER = logging.getLogger(__name__)

_NOT_A_READING = {STATE_UNKNOWN, STATE_UNAVAILABLE, None}

# Half an hour, because that is the granularity of everything underneath.
# The settled day moves when Octopus delivers, and today's figures arrive on
# their own state changes -- so the only thing this timer exists for is the
# two things that move on the clock alone: how late the data is, and where
# the same-time-of-day cut falls. Neither changes faster than a slot.
SCAN_INTERVAL = timedelta(minutes=30)

# One half-hourly slot, in hours. Named because it appears in the middle of
# arithmetic where `0.5` would read as a fudge.
SLOT_HOURS = 0.5


def _as_float(value: Any) -> float | None:
    """A number, or None for anything that is not one.

    None rather than zero throughout. A figure Octopus has not delivered and
    a figure of nothing are different claims, and only one of them is ever
    true of a house's electricity.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _reading(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A numeric state, or None when there is genuinely no reading."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _NOT_A_READING:
        return None
    return _as_float(state.state)


def _day_label(day: date) -> str:
    """`Sat 19 Sep` -- the weekday and the date, both.

    The same argument the panel already makes about `format: since`: the
    weekday is the half you recognise, and the date is the half you can check
    against your own memory of the week. `%-d` is not portable, hence the
    assembly by hand.
    """
    return f"{day:%a} {day.day} {day:%b}"


def _median(values: list[float]) -> float | None:
    """The middle value, which is the right average for a floor.

    A mean would be moved by one odd night -- guests, a wash left running,
    an evening of the oven on -- and the whole point of the norm is to be the
    thing an odd night is measured AGAINST. One unusual night should not
    quietly raise the bar it is meant to fail.
    """
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _pct(value: float, against: float) -> int | None:
    """How far off `against` the value is, as a whole percent."""
    if not against:
        return None
    return round((value - against) / against * 100)


def _comparison(pct: int | None, noun: str) -> str | None:
    """`8% above average`, `12% under average`, or `about average`.

    The band matters more than the wording. Without it a perfectly ordinary
    day reads as "3% down", and a comparison that always has something to say
    is a comparison nobody reads.
    """
    if pct is None:
        return None
    if abs(pct) <= ENERGY_SAME_PCT:
        return f"about {noun}"
    if pct > 0:
        return f"{pct}% above {noun}"
    return f"{abs(pct)}% under {noun}"


class EnergyDaySensor(SensorEntity, RestoreEntity):
    """The settled day's electricity, and today's beside it where it exists.

    The state is the cost of the day being reported, which is *not*
    necessarily yesterday -- see the module docstring. It goes to None rather
    than stale, because the panel renders nothing as nothing and renders a
    wrong number as a right one.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Energy day"
    _attr_icon = "mdi:flash"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_suggested_display_precision = 2

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_energy_day"
        self._day: dict[str, Any] | None = None
        # Settled days, newest first, kept across a restart. This is the only
        # thing here that cannot be recomputed from the source sensor: the
        # source only ever holds one day, so the house's own recent average
        # has to be remembered as the days go past. Losing it on a restart
        # would mean losing the one comparison that works without a Home
        # Mini, every time Home Assistant updates.
        self._history: list[dict[str, Any]] = []

    def _option(self, key: str, default: Any = None) -> Any:
        return self._entry.options.get(key, self._entry.data.get(key, default))

    def _watched(self) -> list[str]:
        return [
            entity_id
            for entity_id in (
                self._option(CONF_ENERGY_COST_SENSOR),
                self._option(CONF_ENERGY_TODAY_COST),
                self._option(CONF_ENERGY_TODAY_KWH),
            )
            if entity_id
        ]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._restore(last.attributes)
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, SCAN_INTERVAL)
        )
        if watched := self._watched():
            self.async_on_remove(
                async_track_state_change_event(self.hass, watched, self._async_changed)
            )
        # The first read during startup can see an Octopus integration that
        # has not fetched anything yet, which is indistinguishable from an
        # account with no data. Reading again once Home Assistant has
        # finished starting is what stops the cell being absent for the first
        # few minutes after every restart.
        self.async_on_remove(async_at_started(self.hass, self._async_started))
        self._recompute()

    def _restore(self, attrs: dict[str, Any]) -> None:
        """Bring the recent days back.

        The one thing here that cannot be recomputed. The source sensor only
        ever holds a single day, so the house's own recent average has to be
        accumulated as the days go past -- and losing it on every restart
        would mean losing the one comparison that works without a Home Mini,
        every time Home Assistant updates.

        Restored from the published attribute rather than through
        `ExtraStoredData`, which is how `pending` and `finished` already come
        back on the appliances: the rows are worth publishing anyway, so
        there is no second copy to keep in step.
        """
        rows = attrs.get("recent_days")
        if not isinstance(rows, list):
            return
        self._history = [
            dict(row)
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("day"), str)
            # All four or none. A row missing one figure would be skipped by
            # that series and kept by the labels, drawing every bar after it
            # against the wrong day -- and a chart off by one is worse than
            # a chart one day shorter. Rows like this only come from a
            # version before the figure existed, so this self-heals.
            and all(
                _as_float(row.get(key)) is not None
                for key in ("cost", "kwh", "baseline_watts")
            )
        ][:ENERGY_HISTORY_DAYS]

    @callback
    def _async_started(self, _hass: HomeAssistant) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _async_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self._recompute()
        self.async_write_ha_state()

    # --- reading the day ----------------------------------------------

    def _slots(self) -> list[tuple[datetime, float, float]] | None:
        """The day's half-hours as `(local start, kWh, cost)`, in clock order.

        Anything malformed is skipped rather than raised on. Octopus owns the
        shape of its own sensor and is free to change it; a house whose panel
        goes quiet when that happens is behaving correctly, and one that
        throws on every state change is not.
        """
        source = self._option(CONF_ENERGY_COST_SENSOR)
        if not source:
            return None
        state = self.hass.states.get(source)
        if state is None or state.state in _NOT_A_READING:
            return None
        charges = state.attributes.get("charges")
        if not isinstance(charges, list):
            return None
        slots: list[tuple[datetime, float, float]] = []
        for slot in charges:
            if not isinstance(slot, dict):
                continue
            start = dt_util.parse_datetime(str(slot.get("start", "")))
            kwh = _as_float(slot.get("consumption"))
            if start is None or kwh is None:
                continue
            # `cost` is Octopus's own rounded pennies and `raw_cost` is the
            # unrounded figure. Summing the rounded one accumulates its
            # rounding forty-eight times, which is how a day's total ends up
            # a few pence away from the total the same sensor is reporting.
            cost = _as_float(slot.get("raw_cost"))
            if cost is None:
                cost = _as_float(slot.get("cost")) or 0.0
            slots.append((dt_util.as_local(start), kwh, cost))
        if not slots:
            return None
        slots.sort(key=lambda row: row[0])
        return slots

    def _read_day(self) -> dict[str, Any] | None:
        """Reduce the settled day to the handful of figures anybody wants."""
        slots = self._slots()
        if slots is None:
            return None
        state = self.hass.states.get(self._option(CONF_ENERGY_COST_SENSOR))
        if state is None:
            return None
        attrs = state.attributes

        day = slots[0][0].date()
        kwh = sum(row[1] for row in slots)
        standing = _as_float(attrs.get("standing_charge"))
        usage = _as_float(attrs.get("total_without_standing_charge"))
        if usage is None:
            usage = sum(row[2] for row in slots)
        total = _as_float(attrs.get("total"))
        if total is None:
            total = usage + (standing or 0.0)

        # What the house draws with everybody asleep. The floor under every
        # other figure on the card, and the one no tariff change touches.
        night = [row[1] for row in slots if row[0].hour < BASELINE_UNTIL_HOUR]
        baseline_watts: int | None = None
        baseline_share: int | None = None
        if night:
            baseline_watts = round(sum(night) / (len(night) * SLOT_HOURS) * 1000)
            if kwh:
                baseline_share = round(baseline_watts * 24 / 1000 / kwh * 100)

        peak = max(slots, key=lambda row: row[1])
        same_kwh, same_cost = self._same_time(slots)

        return {
            "day": day,
            # This day up to the current time of day, cut here rather than
            # when the attributes are read: the cut only moves when the clock
            # crosses a slot, and the timer that recomputes runs at exactly
            # that granularity. Reading the attributes must not mean parsing
            # forty-eight entries again.
            "same_time_kwh": round(same_kwh, 3),
            "same_time_cost": round(same_cost, 2),
            "cost": round(total, 2),
            "kwh": round(kwh, 3),
            "usage": round(usage, 2),
            "standing_p": None if standing is None else round(standing * 100),
            "baseline_watts": baseline_watts,
            "baseline_share": baseline_share,
            "peak_slot": f"{peak[0]:%H:%M}",
            "peak_kwh": round(peak[1], 3),
            # Forty-eight on an ordinary day; forty-six or fifty when the
            # clocks go; fewer means Octopus delivered a partial day, which
            # is worth being able to see rather than wondering why the total
            # looks low.
            "slots": len(slots),
        }

    def _same_time(
        self, slots: list[tuple[datetime, float, float]]
    ) -> tuple[float, float]:
        """The settled day's totals up to this time of day.

        The whole point of holding the half-hours. Comparing a morning
        against a complete day is the mistake that makes every energy
        dashboard say you are doing well until the evening.
        """
        now = dt_util.now()
        cut = now.hour + now.minute / 60.0
        kwh = 0.0
        cost = 0.0
        for start, slot_kwh, slot_cost in slots:
            if start.hour + start.minute / 60.0 >= cut:
                break
            kwh += slot_kwh
            cost += slot_cost
        return kwh, cost

    def _remember(self, day: dict[str, Any]) -> None:
        """Keep the settled day, once, so an average can be had from it."""
        stamp = day["day"].isoformat()
        for row in self._history:
            if row["day"] == stamp:
                # Octopus revises a day occasionally. The later figure is the
                # better one, and two rows for one day would weight it twice.
                row["cost"] = day["cost"]
                row["kwh"] = day["kwh"]
                row["baseline_watts"] = day["baseline_watts"]
                return
        self._history.insert(0, {
            "day": stamp,
            "cost": day["cost"],
            "kwh": day["kwh"],
            # Kept per night, not just per day, because "what does this house
            # draw asleep" is the question a single night cannot answer and a
            # fortnight can.
            "baseline_watts": day["baseline_watts"],
        })
        self._history.sort(key=lambda row: row["day"], reverse=True)
        del self._history[ENERGY_HISTORY_DAYS:]

    @callback
    def _recompute(self) -> None:
        day = self._read_day()
        if day is not None:
            self._remember(day)
        self._day = day

    # --- what it publishes --------------------------------------------

    @property
    def native_value(self) -> float | None:
        day = self._day
        if day is None:
            return None
        if self._days_late(day) > ENERGY_STALE_DAYS:
            # Deliberately nothing. A figure that has stopped being updated
            # is indistinguishable from one that is current, so the cell
            # disappears instead -- which the panel already does for free.
            return None
        return day["cost"]

    def _days_late(self, day: dict[str, Any]) -> int:
        return (dt_util.now().date() - day["day"]).days

    def _window(self, days: int, excluding: str) -> dict[str, Any]:
        """The trailing `days` settled days, averaged, without the day itself.

        Two windows rather than one blended average, because the question a
        person actually asks has two forms -- "is this a normal week for us"
        and "is this a normal month" -- and they answer differently exactly
        when it matters: a cold snap moves the week and leaves the month
        alone, and that gap IS the information.

        A day included in its own average is measured partly against itself,
        which flattens the comparison the figure exists to make.

        `days` is how many of them it reports, because until the history has
        filled a month a "month average" is the mean of whatever there is,
        and something has to say so rather than the label implying thirty.
        """
        rows = sorted(
            (row for row in self._history if row["day"] != excluding),
            key=lambda row: row["day"],
            reverse=True,
        )[:days]
        costs = [c for row in rows if (c := _as_float(row.get("cost"))) is not None]
        kwhs = [k for row in rows if (k := _as_float(row.get("kwh"))) is not None]
        if len(costs) < ENERGY_MIN_DAYS_FOR_AVERAGE:
            return {"cost": None, "kwh": None, "days": len(costs)}
        return {
            "cost": round(sum(costs) / len(costs), 2),
            "kwh": round(sum(kwhs) / len(kwhs), 3) if kwhs else None,
            "days": len(costs),
        }

    def _baseline_norm(self, excluding: str) -> int | None:
        """What this house usually draws asleep, from the other nights.

        Excludes the night being judged for the same reason the cost average
        does: a night in its own norm is partly measured against itself, and
        that is exactly the comparison this exists to make.

        Needs more nights than the cost average does. A floor is the quietest
        number the house produces, so a norm built from three of them is one
        odd night away from being wrong -- and this figure's whole job is to
        be the thing an odd night fails against.
        """
        watts = self._nights(excluding)[:ENERGY_NORM_DAYS]
        if len(watts) < ENERGY_MIN_DAYS_FOR_NORM:
            return None
        norm = _median(watts)
        return None if norm is None else round(norm)

    def _nights(self, excluding: str | None = None) -> list[float]:
        """Every night's floor that is known, newest first."""
        return [
            w
            for row in sorted(
                self._history, key=lambda row: row["day"], reverse=True
            )
            if row["day"] != excluding
            and (w := _as_float(row.get("baseline_watts"))) is not None
        ]

    def _baseline_trend(self) -> int | None:
        """Is the floor creeping -- last week of nights against the week before.

        A different question from the norm, and the norm cannot answer it: a
        trailing norm follows a slow drift upwards and keeps calling it
        normal, which is right for catching a spike and useless for catching
        a creep. A creep is a fridge seal going, a pump starting to fail,
        something plugged in during the summer that never got switched off.

        Both halves are medians, for the reason the norm is one.
        """
        nights = self._nights()
        if len(nights) < ENERGY_WEEK_DAYS * 2:
            return None
        recent = _median(nights[:ENERGY_WEEK_DAYS])
        before = _median(nights[ENERGY_WEEK_DAYS : ENERGY_WEEK_DAYS * 2])
        if recent is None or before is None:
            return None
        return _pct(recent, before)

    def _series(self, key: str, digits: int) -> list[float]:
        """The last `ENERGY_SERIES_DAYS` days of one figure, OLDEST first.

        Oldest first because that is the direction a chart is read in, and
        the marshaller hands the array straight to the card -- so the order
        is decided here rather than by whoever writes the dashboard.
        """
        rows = sorted(self._history, key=lambda row: row["day"])[-ENERGY_SERIES_DAYS:]
        # No filter: `_remember` writes every figure and `_restore` drops any
        # row missing one, so this is the same length as the labels by
        # construction rather than by luck.
        return [round(_as_float(row.get(key)) or 0.0, digits) for row in rows]

    def _series_labels(self) -> list[str]:
        """Weekday initials for the series, in the same order and length."""
        out = []
        for row in sorted(self._history, key=lambda row: row["day"])[
            -ENERGY_SERIES_DAYS:
        ]:
            parsed = dt_util.parse_datetime(f"{row['day']}T00:00:00")
            out.append(f"{parsed:%a}"[0] if parsed else "")
        return out

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        day = self._day
        if day is None:
            return {"days_of_history": len(self._history)}

        late = self._days_late(day)
        stamp = day["day"].isoformat()
        recent_nights = self._nights()[:ENERGY_SERIES_DAYS]
        week = self._window(ENERGY_WEEK_DAYS, stamp)
        month = self._window(ENERGY_MONTH_DAYS, stamp)
        vs_week = (
            _pct(day["cost"], week["cost"]) if week["cost"] is not None else None
        )
        vs_month = (
            _pct(day["cost"], month["cost"]) if month["cost"] is not None else None
        )

        baseline_norm = self._baseline_norm(stamp)
        baseline_excess: int | None = None
        baseline_text: str | None = None
        watts = day["baseline_watts"]
        if watts is not None:
            baseline_text = f"{watts} W overnight"
            if baseline_norm:
                baseline_excess = _pct(watts, baseline_norm)
                # Only the excess is worth a sentence. A night AT the usual
                # floor is the house working, and saying "3% under usual"
                # every morning is how a figure stops being read.
                if baseline_excess is not None and baseline_excess > ENERGY_SAME_PCT:
                    baseline_text = (
                        f"{watts} W overnight against a usual {baseline_norm} W"
                    )

        out: dict[str, Any] = {
            # The date this is about, and how stale that makes it. Both,
            # always: the label is what a person checks against their memory
            # of the week, and the number is what anything automated checks
            # before believing the rest.
            "for_day": day["day"].isoformat(),
            "for_date": _day_label(day["day"]),
            "days_late": late,
            "stale": late > ENERGY_STALE_DAYS,
            "cost_text": money(day["cost"]),
            "kwh": day["kwh"],
            "usage": day["usage"],
            "standing_p": day["standing_p"],
            # What the house draws doing nothing, and how much of the day
            # that accounts for. No price chart would ever have shown this,
            # and on a flat tariff it is the only figure that can be acted
            # on at all.
            "baseline_watts": day["baseline_watts"],
            "baseline_share": day["baseline_share"],
            "peak_slot": day["peak_slot"],
            "peak_kwh": day["peak_kwh"],
            "slots": day["slots"],
            # What the floor usually is, and how far this night sat above it.
            # The pair is what turns a number nobody has a feel for into one
            # anybody can act on: 286 W means nothing on its own, and "420 W
            # against a usual 286" means something was left running.
            "baseline_norm": baseline_norm,
            "baseline_excess_pct": baseline_excess,
            "baseline_text": baseline_text,
            # The rows behind the average. Published because they are what
            # an assistant asked "what have we been spending" actually wants,
            # and because the restore reads them back -- one copy, not two.
            "recent_days": list(self._history),
            # The two windows a person compares a day against, each with the
            # number of days that actually went into it -- until the history
            # fills, "month" is the mean of what there is and says so.
            "week_cost": week["cost"],
            "week_kwh": week["kwh"],
            "week_days": week["days"],
            "vs_week_pct": vs_week,
            "vs_week_text": _comparison(vs_week, "the week"),
            "month_cost": month["cost"],
            "month_kwh": month["kwh"],
            "month_days": month["days"],
            "vs_month_pct": vs_month,
            "vs_month_text": _comparison(vs_month, "the month"),
            # Is the floor creeping, as opposed to having spiked once.
            "baseline_trend_pct": self._baseline_trend(),
            "baseline_high": max(recent_nights) if recent_nights else None,
            # Plain arrays, oldest first, for a chart to read straight off.
            # Shaped here rather than in the card for the reason nothing in
            # this house is shaped in a card.
            "cost_series": self._series("cost", 2),
            "kwh_series": self._series("kwh", 3),
            "baseline_series": self._series("baseline_watts", 0),
            "series_labels": self._series_labels(),
            # How much of an average there is to have had. Zero days and a
            # quiet average look the same on a card and should not to an
            # assistant asked why there is no comparison yet.
            "days_of_history": len(self._history),
        }

        today_cost = _reading(self.hass, self._option(CONF_ENERGY_TODAY_COST))
        today_kwh = _reading(self.hass, self._option(CONF_ENERGY_TODAY_KWH))
        if today_cost is None and today_kwh is None:
            # No Home Mini, no today. Absent rather than null-and-nothing, so
            # that a card referencing it renders a hole and an assistant can
            # tell "not measured" from "measured as nothing".
            return out

        out["today_cost"] = None if today_cost is None else round(today_cost, 2)
        out["today_kwh"] = None if today_kwh is None else round(today_kwh, 3)
        if today_cost is not None:
            out["today_cost_text"] = money(today_cost)

        if today_cost is not None:
            # Against the settled day UP TO NOW, never against the whole of
            # it. A morning measured against a complete day is the mistake
            # that has every energy dashboard congratulating you until tea
            # time.
            then_cost = day["same_time_cost"]
            pct = _pct(today_cost, then_cost)
            out["same_time_cost"] = then_cost
            out["same_time_kwh"] = day["same_time_kwh"]
            out["today_vs_pct"] = pct
            # Named for the day it is actually comparing against, because on
            # a two-day lag "vs yesterday" would be wrong twice a week.
            out["today_vs_text"] = _comparison(pct, _day_label(day["day"]))
        return out
