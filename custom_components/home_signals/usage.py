"""The long run, read from the statistics Home Assistant already keeps.

Everything else in this integration is built from entity states. This one
module is built from **long-term statistics**, and the reason is a hard
limit rather than a preference: a week against the week before, a month
against last month, and what a socket used over seven days are all
questions about the past, and the source sensors hold only the present.
Octopus publishes one settled day at a time; a plug publishes a running
total that resets when it is re-paired.

Statistics are the right store for that and already exist. Home Assistant
keeps them for ever, downsamples nothing below the hour, survives a purge
of the states they came from, and -- unlike anything this integration could
accumulate in an attribute -- they are already correct for the days before
it was installed.

**Which statistics, though, is not ours to guess.** The obvious move is to
construct the ids from the Octopus naming, and this deliberately does not:
nothing else here knows what a tariff provider is called, and a house that
changes supplier should not need a code change. Instead it reads the
**Energy dashboard's own preferences** -- the grid consumption and cost
statistics, and the named device consumption list. That is the householder
having already declared "this is my meter, and these are the things I am
monitoring", in the one place Home Assistant asks them to.

The pleasant consequence: adding a monitoring socket to the Energy
dashboard adds it to the breakdown. No option to set, nothing to redeploy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Meters:
    """What the Energy dashboard says the house is measuring.

    `grid_cost` is allowed to be absent while `grid_kwh` is present: a house
    can meter its consumption without having told Home Assistant a price.
    Everything downstream treats money and units separately for that reason.
    """

    grid_kwh: str | None = None
    grid_cost: str | None = None
    # (name, statistic id), in the order the dashboard lists them, because
    # that order is the householder's own and re-sorting it would lose it.
    devices: tuple[tuple[str, str], ...] = ()

    @property
    def usable(self) -> bool:
        return self.grid_kwh is not None


async def async_meters(hass: HomeAssistant) -> Meters:
    """Read the Energy dashboard's preferences, or an empty answer.

    Empty rather than raising, for the reason every read in this integration
    is forgiving: a house that has never opened the Energy dashboard is a
    house with no long-run figures, which the cards already render as
    nothing. It is not a broken house.
    """
    try:
        from homeassistant.components.energy.data import async_get_manager
    except ImportError:  # pragma: no cover - energy is a default component
        LOGGER.debug("No energy component; no long-run figures")
        return Meters()

    try:
        manager = await async_get_manager(hass)
    except Exception:  # noqa: BLE001 - see the docstring
        LOGGER.debug("Could not read the energy preferences", exc_info=True)
        return Meters()

    data = getattr(manager, "data", None)
    if not data:
        return Meters()

    grid_kwh: str | None = None
    grid_cost: str | None = None
    for source in data.get("energy_sources") or []:
        if source.get("type") != "grid":
            continue
        # A grid source can carry several flows. The first that names a
        # consumption statistic is the house's import; export and return
        # are somebody else's question.
        for flow in source.get("flow_from") or []:
            if flow.get("stat_energy_from"):
                grid_kwh = grid_kwh or flow.get("stat_energy_from")
                grid_cost = grid_cost or flow.get("stat_cost")
        # The flat form, which is what a 2026.9 config actually looks like.
        if source.get("stat_energy_from"):
            grid_kwh = grid_kwh or source.get("stat_energy_from")
            grid_cost = grid_cost or source.get("stat_cost")
        if grid_kwh:
            break

    devices: list[tuple[str, str]] = []
    for device in data.get("device_consumption") or []:
        stat = device.get("stat_consumption")
        if not stat:
            continue
        # The dashboard's own label where it has one, else the entity id --
        # which is ugly on a card and still better than an unnamed slice.
        devices.append((str(device.get("name") or stat), stat))

    return Meters(grid_kwh=grid_kwh, grid_cost=grid_cost, devices=tuple(devices))


async def async_totals(
    hass: HomeAssistant,
    statistic_ids: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, float]:
    """How much each statistic moved between two instants.

    One number per id, which is what a window wants. Asks the recorder for
    `change` rather than differencing cumulative sums by hand: a meter that
    resets -- a plug re-paired, a counter rolling over -- makes a naive
    difference negative, and the recorder already knows how to bridge that
    because it is the thing that recorded the reset.
    """
    buckets = await async_buckets(hass, statistic_ids, start, end, "hour")
    return {
        stat: round(sum(value for _at, value in rows), 3)
        for stat, rows in buckets.items()
    }


async def async_buckets(
    hass: HomeAssistant,
    statistic_ids: list[str],
    start: datetime,
    end: datetime,
    period: str,
) -> dict[str, list[tuple[datetime, float]]]:
    """Per-period movement for each statistic, oldest first.

    `period` is the recorder's own vocabulary -- hour, day, week, month --
    so a week card and a month card differ by a string rather than by
    arithmetic done here. Letting the recorder bucket it is also the only
    version that gets the clocks going back right.
    """
    if not statistic_ids:
        return {}
    try:
        from homeassistant.components.recorder import get_instance, statistics
    except ImportError:  # pragma: no cover - recorder is optional
        LOGGER.debug("No recorder; no long-run figures")
        return {}

    def _read() -> dict[str, list[Any]]:
        return statistics.statistics_during_period(
            hass,
            start,
            end,
            set(statistic_ids),
            period,
            None,
            {"change"},
        )

    try:
        raw = await get_instance(hass).async_add_executor_job(_read)
    except Exception:  # noqa: BLE001 - a missing statistic is not a fault
        LOGGER.debug("Could not read statistics for %s", statistic_ids, exc_info=True)
        return {}

    out: dict[str, list[tuple[datetime, float]]] = {}
    for stat, rows in (raw or {}).items():
        series: list[tuple[datetime, float]] = []
        for row in rows or []:
            change = row.get("change")
            when = row.get("start")
            if change is None or when is None:
                continue
            at = when if isinstance(when, datetime) else None
            if at is None:
                # The recorder hands back epoch floats on some versions.
                try:
                    at = datetime.fromtimestamp(float(when), tz=start.tzinfo)
                except (TypeError, ValueError, OSError):
                    continue
            try:
                value = float(change)
            except (TypeError, ValueError):
                continue
            # A negative change is a meter that went backwards, which is a
            # reset rather than a house that generated electricity. Zero is
            # the honest reading of "we cannot tell", and it keeps a
            # breakdown's parts from exceeding its whole.
            series.append((at, max(0.0, value)))
        series.sort(key=lambda row: row[0])
        out[stat] = series
    return out
