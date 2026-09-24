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
    BREAKDOWN_OTHER,
    ENERGY_AVG_MAX_WEEKS,
    ENERGY_BREAKDOWN_DAYS,
    ENERGY_MIN_MONTHS,
    ENERGY_MIN_WEEKS_FOR_AVERAGE,
    ENERGY_MONTHS_SHOWN,
    ENERGY_WEEK_HOURS,
    BLOCK_HOURS,
    BLOCK_NAMES,
    ENERGY_BACKFILL_DAYS,
    ENERGY_BLOCK_DAYS,
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
from .usage import Meters, async_buckets, async_meters, async_totals

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
        # The long run, refreshed on the timer rather than worked out when
        # the attributes are read: each of these is a recorder query, and
        # reading a card must not touch the database. None means "not asked
        # yet", which a card renders as nothing -- the same as "no data".
        self._long: dict[str, Any] = {}

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
        # Off the startup path on purpose: reading a week of one entity's
        # recorded states is a database round trip, and nothing on the panel
        # should wait on it. The card renders whatever days it has and grows
        # a moment later.
        self.hass.async_create_task(self._async_backfill())
        self.hass.async_create_task(self._async_tick_long())

    async def _async_backfill(self) -> None:
        """Recover the days that went past before anybody was writing them down.

        The source sensor holds a single day, so history here is normally
        accumulated one day at a time and a week of blocks takes a week to
        arrive. But Home Assistant has been recording that sensor's own
        states all along, attributes and all -- and its attributes are the
        forty-eight half-hours. So the past is recoverable by reading the
        same sensor's earlier states and putting them through the same
        parser that reads it now.

        Deliberately the sensor's own history rather than the statistics
        Octopus also publishes: those would have to be addressed by a
        statistic id this integration would have to know how to construct,
        and nothing here knows it is talking to Octopus. This reads the
        entity it was already configured with.

        Best effort throughout. No recorder, an excluded entity, a purge
        that has already been past -- all of them mean fewer columns, which
        the card already renders correctly, and none of them is worth a
        broken startup.
        """
        source = self._option(CONF_ENERGY_COST_SENSOR)
        if not source:
            return
        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:  # pragma: no cover - recorder is optional
            LOGGER.debug("No recorder; skipping the backfill")
            return

        start = dt_util.now() - timedelta(days=ENERGY_BACKFILL_DAYS)
        try:
            states = await get_instance(self.hass).async_add_executor_job(
                lambda: history.state_changes_during_period(
                    self.hass, start, dt_util.now(), source, include_start_time_state=True
                )
            )
        except Exception:  # noqa: BLE001 - see the docstring; never fatal
            LOGGER.debug("Could not read %s's history for a backfill", source,
                         exc_info=True)
            return

        found = 0
        for state in states.get(source, []):
            day = self._day_from(state.attributes)
            if day is None:
                continue
            stamp = day["day"].isoformat()
            known = next((r for r in self._history if r["day"] == stamp), None)
            # A day already carrying blocks is left alone: it was read live,
            # from the same attributes, and re-reading it changes nothing.
            if known is not None and isinstance(known.get("block_cost"), list):
                continue
            self._remember(day)
            found += 1

        if found:
            LOGGER.info(
                "Recovered %s earlier day(s) of half-hours from the recorder", found
            )
            self.async_write_ha_state()

    @callback

    @callback
    def _async_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self._recompute()
        self.async_write_ha_state()
        self.hass.async_create_task(self._async_tick_long())

    async def _async_tick_long(self) -> None:
        """Refresh the long run, and never let it take the sensor down.

        Half-hourly is far more often than a week's total can change, and
        that is the point: it is the cheapest schedule that needs no
        reasoning about when a day lands, a month rolls over, or the clocks
        go back.
        """
        try:
            await self._async_refresh_long()
        except Exception:  # noqa: BLE001 - a card is not worth an exception
            LOGGER.debug("Could not refresh the long-run figures", exc_info=True)
            return
        self.async_write_ha_state()

    # --- reading the day ----------------------------------------------

    def _slots_from(
        self, attrs: Any
    ) -> list[tuple[datetime, float, float]] | None:
        """The day's half-hours as `(local start, kWh, cost)`, in clock order.

        Takes the attributes rather than reading the state itself, so a day
        recovered from the recorder goes through exactly the same parse as a
        day arriving live -- see `_async_backfill`. A second reader for old
        days would be a second thing to keep right.

        Anything malformed is skipped rather than raised on. Octopus owns the
        shape of its own sensor and is free to change it; a house whose panel
        goes quiet when that happens is behaving correctly, and one that
        throws on every state change is not.
        """
        charges = attrs.get("charges") if hasattr(attrs, "get") else None
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
        state = self.hass.states.get(self._option(CONF_ENERGY_COST_SENSOR))
        if state is None or state.state in _NOT_A_READING:
            return None
        return self._day_from(state.attributes)

    def _day_from(self, attrs: Any) -> dict[str, Any] | None:
        """The same reduction, over any copy of those attributes.

        What the backfill puts old days through -- see `_async_backfill`.
        """
        slots = self._slots_from(attrs)
        if slots is None:
            return None

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
        block_kwh, block_cost = self._blocks(slots)

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
            "block_kwh": block_kwh,
            "block_cost": block_cost,
            "peak_slot": f"{peak[0]:%H:%M}",
            "peak_kwh": round(peak[1], 3),
            # Forty-eight on an ordinary day; forty-six or fifty when the
            # clocks go; fewer means Octopus delivered a partial day, which
            # is worth being able to see rather than wondering why the total
            # looks low.
            "slots": len(slots),
        }

    def _blocks(
        self, slots: list[tuple[datetime, float, float]]
    ) -> tuple[list[float], list[float]]:
        """The day as four six-hour blocks: kWh and cost, in clock order.

        The reason this is worth having at all is that a day's total says
        nothing about the day. Two days at the same total can be a morning
        of laundry and an evening of the oven, and only one of those is a
        thing anybody would change.

        Blocks sum to the day's own usage by construction, because they are
        the same slots counted once each -- which is what makes a stacked
        column honest. Nothing is projected or apportioned here; that is the
        difference between this and `baseline_watts`, which takes the
        overnight RATE and asks what a day of it would be.
        """
        kwh = [0.0] * len(BLOCK_NAMES)
        cost = [0.0] * len(BLOCK_NAMES)
        for start, slot_kwh, slot_cost in slots:
            index = min(start.hour // BLOCK_HOURS, len(BLOCK_NAMES) - 1)
            kwh[index] += slot_kwh
            cost[index] += slot_cost
        return (
            [round(v, 3) for v in kwh],
            [round(v, 2) for v in cost],
        )

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
                row["block_kwh"] = day["block_kwh"]
                row["block_cost"] = day["block_cost"]
                return
        self._history.insert(0, {
            "day": stamp,
            "cost": day["cost"],
            "kwh": day["kwh"],
            # Kept per night, not just per day, because "what does this house
            # draw asleep" is the question a single night cannot answer and a
            # fortnight can.
            "baseline_watts": day["baseline_watts"],
            # The four blocks, kept per day for the same reason the floor
            # is: the source holds one day, so a week of them can only be
            # had by writing each one down as it goes past.
            "block_kwh": day["block_kwh"],
            "block_cost": day["block_cost"],
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

    async def _async_refresh_long(self) -> None:
        """The week, the week before, the long average, the months, the split.

        One pass, on the timer, because every figure here is a recorder
        query and a card must never be the thing that runs one.

        Each is gated on having a genuinely full window. A four-day "week"
        under a seven-day heading is the figure somebody quotes back at you
        a month later, and nothing is a perfectly good thing for a card to
        show -- the panel already renders it.
        """
        meters = await async_meters(self.hass)
        if not meters.usable:
            self._long = {}
            return

        now = dt_util.now()
        out: dict[str, Any] = {}
        ids = [meters.grid_kwh] + ([meters.grid_cost] if meters.grid_cost else [])

        # --- this week against the one before
        week = timedelta(hours=ENERGY_WEEK_HOURS)
        this_start, prev_start = now - week, now - week * 2
        this_totals = await async_totals(self.hass, ids, this_start, now)
        prev_totals = await async_totals(self.hass, ids, prev_start, this_start)

        def _pair(totals: dict[str, float], prefix: str) -> None:
            kwh = totals.get(meters.grid_kwh)
            if kwh:
                out[f"{prefix}_kwh"] = round(kwh, 1)
            cost = totals.get(meters.grid_cost) if meters.grid_cost else None
            if cost:
                out[f"{prefix}_cost"] = round(cost, 2)
                out[f"{prefix}_cost_text"] = money(cost)

        _pair(this_totals, "week7")
        _pair(prev_totals, "prev7")
        this_cost = out.get("week7_cost")
        prev_cost = out.get("prev7_cost")
        if this_cost is not None and prev_cost:
            pct = _pct(this_cost, prev_cost)
            out["week7_vs_prev_pct"] = pct
            out["week7_vs_prev_text"] = _comparison(pct, "the week before")

        # --- the average week, over however many whole weeks there are
        weeks = await async_buckets(
            self.hass,
            [meters.grid_kwh] + ([meters.grid_cost] if meters.grid_cost else []),
            now - timedelta(weeks=ENERGY_AVG_MAX_WEEKS),
            now,
            "week",
        )
        # The current week is partial by definition and would drag every
        # average down, so it is dropped rather than averaged in.
        whole = {
            stat: [value for at, value in rows if at + week <= now]
            for stat, rows in weeks.items()
        }
        counted = whole.get(meters.grid_kwh) or []
        if len(counted) >= ENERGY_MIN_WEEKS_FOR_AVERAGE:
            out["avg_week_kwh"] = round(sum(counted) / len(counted), 1)
            out["avg_weeks"] = len(counted)
            costs = whole.get(meters.grid_cost) if meters.grid_cost else None
            if costs:
                mean = sum(costs) / len(costs)
                out["avg_week_cost"] = round(mean, 2)
                out["avg_week_cost_text"] = money(mean)
            # Says what it actually averaged. "A 7-day average" implies a
            # year of evidence that does not exist in the first month.
            out["avg_week_note"] = (
                f"over {len(counted)} weeks" if len(counted) < ENERGY_AVG_MAX_WEEKS
                else "over the year"
            )
        else:
            out["avg_weeks"] = len(counted)

        out.update(await self._async_months(meters, now))
        out.update(await self._async_breakdown(meters, now))
        self._long = out

    async def _async_months(
        self, meters: Meters, now: datetime
    ) -> dict[str, Any]:
        """Whole months, oldest first, shaped for the same card the days use.

        The current month is never drawn. It is always the short bar and
        would always read as an improvement, right up to the last day.
        """
        rows = await async_buckets(
            self.hass,
            [meters.grid_kwh] + ([meters.grid_cost] if meters.grid_cost else []),
            now - timedelta(days=31 * ENERGY_MONTHS_SHOWN),
            now,
            "month",
        )
        kwhs = rows.get(meters.grid_kwh) or []
        costs = dict(rows.get(meters.grid_cost) or []) if meters.grid_cost else {}
        months = []
        for at, kwh in kwhs:
            if at.year == now.year and at.month == now.month:
                continue
            cost = costs.get(at)
            months.append({
                "day": f"{at:%Y-%m}",
                "label": f"{at:%b}",
                # One segment, because a month IS the whole. The card that
                # draws the four blocks of a day draws this unchanged.
                "cost": [round(cost, 2) if cost else round(kwh, 2)],
                "kwh": [round(kwh, 3)],
                "total_cost": round(cost, 2) if cost else None,
                "total_cost_text": money(cost) if cost else None,
                "total_kwh": round(kwh, 1),
            })
        months = months[-ENERGY_MONTHS_SHOWN:]
        if len(months) < ENERGY_MIN_MONTHS:
            # One month is not a trend, and a card with one bar is a stat
            # tile that has been made to look like a chart.
            return {"month_rows": [], "months_known": len(months)}
        return {"month_rows": months, "months_known": len(months)}

    async def _async_breakdown(
        self, meters: Meters, now: datetime
    ) -> dict[str, Any]:
        """What the week's electricity went on, as far as anything is metered.

        The remainder is the honest part. Two plugs account for a tenth of
        this house, so the slice that matters is the one nothing is watching
        -- and it is named rather than left as the gap between a total and
        some parts.

        Each device is capped at the grid total and the remainder floored at
        zero: a plug and a meter are different instruments with different
        clocks, and a breakdown whose parts exceed its whole is worse than
        no breakdown.
        """
        if not meters.devices:
            return {"breakdown": []}
        start = now - timedelta(days=ENERGY_BREAKDOWN_DAYS)
        ids = [meters.grid_kwh] + [stat for _name, stat in meters.devices]
        totals = await async_totals(self.hass, ids, start, now)
        grid = totals.get(meters.grid_kwh) or 0.0
        if grid <= 0:
            return {"breakdown": []}

        rate = None
        spend = None
        if meters.grid_cost:
            spend = (
                await async_totals(self.hass, [meters.grid_cost], start, now)
            ).get(meters.grid_cost)
            if spend:
                # The week's own average rate, which is the only rate that
                # can divide up the week's own money.
                rate = spend / grid

        slices: list[dict[str, Any]] = []
        named = 0.0
        priced = 0.0
        for name, stat in meters.devices:
            kwh = min(totals.get(stat) or 0.0, grid)
            if kwh <= 0:
                continue
            named += kwh
            wedge = self._slice(name, kwh, grid, rate)
            priced += wedge.get("cost") or 0.0
            slices.append(wedge)

        rest = max(0.0, grid - named)
        if rest > 0:
            wedge = self._slice(BREAKDOWN_OTHER, rest, grid, rate)
            if spend is not None and rate is not None:
                # The remainder takes the remaining money rather than its
                # own multiplication, so the wedges add up to the week's
                # total to the penny. Rounding each slice independently put
                # them a penny over, and a pie whose parts exceed the figure
                # printed beside it is a pie nobody believes.
                left = round(spend - priced, 2)
                wedge["cost"] = left
                wedge["cost_text"] = money(left)
            slices.append(wedge)

        out: dict[str, Any] = {
            "breakdown": slices,
            "breakdown_days": ENERGY_BREAKDOWN_DAYS,
            "breakdown_kwh": round(grid, 1),
            # How much of the house is actually watched. Today it is a
            # tenth, and that is the finding rather than a shortcoming of
            # the picture.
            "breakdown_metered_pct": round(named / grid * 100),
        }
        if spend:
            out["breakdown_cost"] = round(spend, 2)
            out["breakdown_cost_text"] = money(spend)
        return out

    @staticmethod
    def _slice(
        name: str, kwh: float, whole: float, rate: float | None
    ) -> dict[str, Any]:
        """One wedge, with its own figures already written out.

        The share and the money ride along because the card prints them: a
        slice of four percent cannot be read as a shape, and a number
        beside it is the whole reason the picture is allowed to be a pie.
        """
        out: dict[str, Any] = {
            "name": name,
            "kwh": round(kwh, 2),
            "share": round(kwh / whole * 100, 1),
        }
        if rate is not None:
            cost = kwh * rate
            out["cost"] = round(cost, 2)
            out["cost_text"] = money(cost)
        return out

    def _block_days(self) -> list[dict[str, Any]]:
        """The last few days as four blocks each, oldest first.

        Shaped here rather than in the card, like every other array this
        publishes: the card draws a column per entry and prints the two
        totals underneath, and knows nothing about what a block is.

        Days without blocks are dropped rather than zero-filled. A column of
        four empty segments under a real date reads as a day the house used
        nothing, which is never true -- and those rows only exist from
        before this figure did, or from a day the backfill could not reach.
        """
        rows = [
            row
            for row in sorted(self._history, key=lambda r: r["day"])
            if isinstance(row.get("block_cost"), list)
            and isinstance(row.get("block_kwh"), list)
            and len(row["block_cost"]) == len(BLOCK_NAMES)
        ][-ENERGY_BLOCK_DAYS:]
        out = []
        for row in rows:
            cost = [_as_float(v) or 0.0 for v in row["block_cost"]]
            kwh = [_as_float(v) or 0.0 for v in row["block_kwh"]]
            parsed = dt_util.parse_datetime(f"{row['day']}T00:00:00")
            total = round(sum(cost), 2)
            out.append({
                "day": row["day"],
                "label": f"{parsed:%a}" if parsed else "",
                "cost": [round(v, 2) for v in cost],
                "kwh": [round(v, 3) for v in kwh],
                "total_cost": total,
                "total_cost_text": money(total),
                "total_kwh": round(sum(kwh), 1),
            })
        return out

    def _split(self, day: dict[str, Any]) -> dict[str, Any]:
        """The day as floor, everything else, and the standing charge.

        The floor in watts is not a fact anybody can act on. What it costs,
        and how much of the day was NOT it, are -- because the floor is the
        part you change by finding something and unplugging it, and the rest
        is the part you changed by living in the house today.

        Priced at the day's OWN average rate (`usage / kwh`) rather than at
        whatever the tariff says now. On a flat tariff they are the same
        number; on a variable one the day's own rate is the only one that
        can divide up the day's own money.

        The three add up to the day's total, deliberately: a table whose
        rows do not sum to the figure above them is a table nobody trusts.
        So `rest` is the remainder rather than a second multiplication, and
        any rounding penny lands there rather than going missing.

        Absent when the floor projects to more than the day actually used.
        That means Octopus delivered a partial day, and a split whose parts
        exceed the whole is fiction.
        """
        watts = day["baseline_watts"]
        kwh = day["kwh"]
        usage = day["usage"]
        if watts is None or not kwh or usage is None:
            return {}
        floor_kwh = watts * 24 / 1000
        if floor_kwh >= kwh:
            return {}
        rate = usage / kwh
        floor_cost = round(floor_kwh * rate, 2)
        return {
            "floor_kwh": round(floor_kwh, 1),
            "floor_cost": floor_cost,
            "floor_cost_text": money(floor_cost),
            # What a floor held all year costs, which is the figure that
            # makes anybody go and look for the thing causing it.
            "floor_cost_year": round(floor_kwh * rate * 365),
            "rest_kwh": round(kwh - floor_kwh, 1),
            "rest_cost": round(usage - floor_cost, 2),
            "rest_cost_text": money(round(usage - floor_cost, 2)),
        }

    def _vs_last_night(self, day: dict[str, Any]) -> dict[str, Any]:
        """The floor against the night before the one being reported.

        Not against the median: a median is the right thing to fire a row
        off and the wrong thing to hand a person, because nobody remembers
        their median. "Four watts up on Friday" is a sentence about a night
        you were there for.

        The night before the REPORTED one, which on a two-day lag is not
        last night -- so it is named rather than implied.
        """
        others = [
            row
            for row in sorted(self._history, key=lambda r: r["day"], reverse=True)
            if row["day"] < day["day"].isoformat()
            and _as_float(row.get("baseline_watts")) is not None
        ]
        watts = day["baseline_watts"]
        if not others or watts is None:
            return {}
        prev = _as_float(others[0]["baseline_watts"])
        parsed = dt_util.parse_datetime(f"{others[0]['day']}T00:00:00")
        label = f"{parsed:%a}" if parsed else "the night before"
        delta = round(watts - prev)
        if abs(delta) <= 2:
            text = f"level with {label}"
        elif delta > 0:
            text = f"{delta} W up on {label}"
        else:
            text = f"{abs(delta)} W down on {label}"
        return {
            "prev_night": others[0]["day"],
            "prev_baseline_watts": round(prev),
            "baseline_vs_prev_watts": delta,
            "baseline_vs_prev_text": text,
        }

    def _today(self) -> dict[str, Any]:
        """Today's own figures, which do not come from the settled day.

        A separate meter reports these -- a Home Mini, or Hildebrand's
        half-hourly feed -- so they are live whatever Octopus is doing with
        its settled days. That is why they are assembled apart from the day
        and survive it going stale: the freshest figure in the house must
        not be dropped because a different source stopped delivering.

        The *comparison* is another matter and stays with the day, because
        it is made of it.

        Absent rather than null-and-nothing when there is no meter, so that
        a card renders a hole and an assistant can tell "not measured" from
        "measured as nothing".
        """
        cost = _reading(self.hass, self._option(CONF_ENERGY_TODAY_COST))
        kwh = _reading(self.hass, self._option(CONF_ENERGY_TODAY_KWH))
        if cost is None and kwh is None:
            return {}
        out: dict[str, Any] = {
            "today_cost": None if cost is None else round(cost, 2),
            "today_kwh": None if kwh is None else round(kwh, 3),
        }
        if cost is not None:
            out["today_cost_text"] = money(cost)
        return out

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        day = self._day
        if day is None:
            return {"days_of_history": len(self._history), **self._today()}

        late = self._days_late(day)
        if late > ENERGY_STALE_DAYS:
            # The state has already gone to None here, and the attributes
            # have to follow it. Every card on the panel reads attributes --
            # `cost_text`, `week_cost_text`, `floor_cost_text` -- so leaving
            # them populated would keep rendering Saturday's figures on
            # Thursday under a state nobody looks at. That is precisely the
            # failure the staleness rule exists to prevent.
            #
            # What stays is what explains the silence rather than filling it:
            # the date, how late it is, and the flag a row already checks.
            # `recent_days` stays too, and is not rendered by anything -- it
            # is what `_restore` reads back, and dropping it would throw the
            # house's accumulated history away on the next restart, which is
            # the one thing here that cannot be recomputed.
            return {
                "for_day": day["day"].isoformat(),
                "for_date": _day_label(day["day"]),
                "days_late": late,
                "stale": True,
                "recent_days": list(self._history),
                "days_of_history": len(self._history),
                # Today is not stale. It comes from a different meter and
                # is the freshest thing here; only the comparison against
                # the settled day goes, because that is made of it.
                **self._today(),
            }
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
            # Always False by this point -- a stale day returned above. Kept
            # so that one key answers "may I believe this" whichever branch
            # produced the attributes.
            "stale": False,
            "cost_text": money(day["cost"]),
            "kwh": day["kwh"],
            "usage": day["usage"],
            "standing_p": day["standing_p"],
            # The standing charge as a year, beside the floor's year. Both
            # rows on the "Where it went" card are then the same kind of
            # figure: a thing you pay for by the day, said in the units
            # anybody decides anything in. The difference between them is the
            # point -- the floor is a year you can go and reduce, and this is
            # a year you cannot, which is worth knowing before hunting for it.
            "standing_cost_year": (
                None
                if day["standing_p"] is None
                else round(day["standing_p"] * 365 / 100)
            ),
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
            "week_cost_text": None if week["cost"] is None else money(week["cost"]),
            "week_kwh": week["kwh"],
            "week_days": week["days"],
            "vs_week_pct": vs_week,
            "vs_week_text": _comparison(vs_week, "the week"),
            "month_cost": month["cost"],
            "month_cost_text": None if month["cost"] is None else money(month["cost"]),
            "month_kwh": month["kwh"],
            "month_days": month["days"],
            "vs_month_pct": vs_month,
            "vs_month_text": _comparison(vs_month, "the month"),
            # Is the floor creeping, as opposed to having spiked once.
            "baseline_trend_pct": self._baseline_trend(),
            # High and low as a pair, so the card's figures can be three
            # wattages rather than two wattages and a percentage. A row of
            # metrics that mixes units is read as three unrelated numbers.
            "baseline_high": max(recent_nights) if recent_nights else None,
            "baseline_low": min(recent_nights) if recent_nights else None,
            # The verdict without the number in it. `baseline_text` repeats
            # the watts, which is right in a Needs-you row that has no hero
            # and wrong under a hero that has just said it.
            "baseline_verdict": _comparison(baseline_excess, "usual"),
            "baseline_trend_text": _comparison(
                self._baseline_trend(), "last week"
            ),
            **self._split(day),
            **self._vs_last_night(day),
            # Plain arrays, oldest first, for a chart to read straight off.
            # Shaped here rather than in the card for the reason nothing in
            # this house is shaped in a card.
            # Where the power went and roughly when: a column per day, four
            # blocks each. The names ride along so the card does not have to
            # know that a block is six hours.
            "block_names": list(BLOCK_NAMES),
            "block_hours": BLOCK_HOURS,
            "block_kwh": day["block_kwh"],
            "block_cost": day["block_cost"],
            "block_days": self._block_days(),
            # The long run, from Home Assistant's own statistics. Each key
            # is absent until its window is genuinely full -- see
            # `_async_refresh_long` and usage.py.
            **self._long,
            "cost_series": self._series("cost", 2),
            "kwh_series": self._series("kwh", 3),
            "baseline_series": self._series("baseline_watts", 0),
            "series_labels": self._series_labels(),
            # How much of an average there is to have had. Zero days and a
            # quiet average look the same on a card and should not to an
            # assistant asked why there is no comparison yet.
            "days_of_history": len(self._history),
        }

        today = self._today()
        out.update(today)
        today_cost = today.get("today_cost")
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
