"""The long run: which statistics, and what the breakdown does with them.

The recorder itself is not mocked here. What is tested is everything that
decides what a card shows -- which statistics get read, how a window is
divided up, and every case where the honest answer is nothing. Those are
where the mistakes live; `statistics_during_period` is Home Assistant's and
already has its own tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.home_signals.usage import Meters, async_meters

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=LONDON)

GRID = "octopus_energy:electricity_meter_previous_accumulative_consumption"
GRID_COST = "octopus_energy:electricity_meter_previous_accumulative_cost"
WASHER = "sensor.washing_machine_plug_summation_delivered"
DRYER = "sensor.sonoff_s60zbtpg_summation_delivered"


class FakeManager:
    """The energy component's manager, as far as this reads it."""

    def __init__(self, data) -> None:
        self.data = data


def _prefs(**kwargs):
    """The Energy dashboard's preferences, in the 2026.9 flat grid shape."""
    return {
        "energy_sources": [{
            "type": "grid",
            "stat_energy_from": kwargs.get("grid", GRID),
            "stat_cost": kwargs.get("grid_cost", GRID_COST),
        }],
        "device_consumption": kwargs.get("devices", [
            {"stat_consumption": WASHER, "name": "Washing machine"},
            {"stat_consumption": DRYER, "name": "Tumble dryer"},
        ]),
    }


async def _meters(hass: HomeAssistant, monkeypatch, data) -> Meters:
    async def _get(_hass):
        return FakeManager(data)

    monkeypatch.setattr(
        "homeassistant.components.energy.data.async_get_manager", _get
    )
    return await async_meters(hass)


# --- which statistics, and who decided -------------------------------
#
# Not constructed from the Octopus naming, deliberately. Nothing else in
# this integration knows what a tariff provider is called, and a house that
# changes supplier should not need a code change.


async def test_the_meters_come_from_the_energy_dashboard(
    hass: HomeAssistant, monkeypatch
) -> None:
    """The householder has already said which meter is theirs, once."""
    meters = await _meters(hass, monkeypatch, _prefs())

    assert meters.grid_kwh == GRID
    assert meters.grid_cost == GRID_COST
    assert meters.usable is True


async def test_the_devices_keep_the_dashboard_s_names_and_order(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Which is why a new monitoring socket needs no code change at all.

    The order is the householder's own, so it is not re-sorted.
    """
    meters = await _meters(hass, monkeypatch, _prefs())

    assert meters.devices == (
        ("Washing machine", WASHER),
        ("Tumble dryer", DRYER),
    )


async def test_a_device_with_no_name_still_gets_a_slice(
    hass: HomeAssistant, monkeypatch
) -> None:
    """An entity id is ugly on a card and better than an unnamed wedge."""
    meters = await _meters(hass, monkeypatch, _prefs(
        devices=[{"stat_consumption": WASHER}]
    ))

    assert meters.devices == ((WASHER, WASHER),)


async def test_the_nested_flow_shape_is_read_too(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Older configs put the grid's statistics inside `flow_from`.

    Both shapes are live in the wild, and a house that upgraded through the
    change should not lose its figures.
    """
    meters = await _meters(hass, monkeypatch, {
        "energy_sources": [{
            "type": "grid",
            "flow_from": [{"stat_energy_from": GRID, "stat_cost": GRID_COST}],
        }],
        "device_consumption": [],
    })

    assert meters.grid_kwh == GRID
    assert meters.grid_cost == GRID_COST


async def test_metering_without_a_price_is_still_usable(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A house can measure its units without having told HA a tariff.

    Money and units are kept apart downstream for exactly this reason.
    """
    meters = await _meters(hass, monkeypatch, _prefs(grid_cost=None))

    assert meters.grid_kwh == GRID
    assert meters.grid_cost is None
    assert meters.usable is True


async def test_no_grid_source_means_no_long_run_figures(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A house that never opened the Energy dashboard is not a broken house.

    The cards render nothing, which is the truth.
    """
    meters = await _meters(hass, monkeypatch, {
        "energy_sources": [{"type": "solar", "stat_energy_from": "sensor.pv"}],
        "device_consumption": [],
    })

    assert meters.grid_kwh is None
    assert meters.usable is False


async def test_unreadable_preferences_are_empty_not_fatal(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Every read in this integration is forgiving, and this one twice over.

    It runs on a timer behind a card. An exception here would be a card
    nobody can see taking the sensor down with it.
    """
    async def _boom(_hass):
        raise RuntimeError("no energy component today")

    monkeypatch.setattr(
        "homeassistant.components.energy.data.async_get_manager", _boom
    )
    meters = await async_meters(hass)

    assert meters == Meters()
    assert meters.usable is False


async def test_empty_preferences_are_empty(
    hass: HomeAssistant, monkeypatch
) -> None:
    meters = await _meters(hass, monkeypatch, None)
    assert meters.usable is False
