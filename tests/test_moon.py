"""The moon, against instants that were observed rather than computed.

Every expected time below is a published one, and three of them are fixed
by an eclipse, which happens at a new or full moon and so pins it to the
minute: the total solar eclipse of 8 April 2024, the partial lunar eclipse
of 18 September 2024 and the total solar eclipse of 12 August 2026. A test
that compared the code with itself would pass whatever the series said.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import DOMAIN
from custom_components.home_signals.moon import (
    FIRST_QUARTER,
    FULL_MOON,
    LAST_QUARTER,
    NEW_MOON,
    WANING_CRESCENT,
    WAXING_CRESCENT,
    WAXING_GIBBOUS,
    illuminated,
    moon,
)

MINUTE = timedelta(minutes=2)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("before", "kind", "expected"),
    [
        # Solar eclipse: new moon 18:21 UTC.
        ("2024-04-01T00:00", "next_new_moon", "2024-04-08T18:21"),
        # Lunar eclipse: full moon 02:34 UTC.
        ("2024-09-10T00:00", "next_full_moon", "2024-09-18T02:34"),
        # Solar eclipse over Spain: new moon 17:37 UTC.
        ("2026-08-01T00:00", "next_new_moon", "2026-08-12T17:37"),
        # A full moon with no eclipse, from the almanac: 06:55 UTC.
        ("2025-03-01T00:00", "next_full_moon", "2025-03-14T06:55"),
    ],
)
def test_event_instants(before: str, kind: str, expected: str) -> None:
    assert abs(moon(at(before))[kind] - at(expected)) <= MINUTE


def test_lit_fraction_at_the_extremes() -> None:
    """Dark at an eclipse new moon, all but whole at an eclipse full moon."""
    assert illuminated(at("2024-04-08T18:21")) < 0.001
    assert illuminated(at("2024-09-18T02:34")) > 0.999


def test_the_name_holds_half_a_day_either_side() -> None:
    new = at("2024-04-08T18:21")
    assert moon(new - timedelta(hours=11))["phase"] == NEW_MOON
    assert moon(new + timedelta(hours=11))["phase"] == NEW_MOON
    assert moon(new - timedelta(hours=13))["phase"] == WANING_CRESCENT
    assert moon(new + timedelta(hours=13))["phase"] == WAXING_CRESCENT

    full = at("2024-09-18T02:34")
    assert moon(full - timedelta(hours=11))["phase"] == FULL_MOON
    # Fourteen hours out the disc is over 99% lit and still not "full".
    before = moon(full - timedelta(hours=14))
    assert before["phase"] == WAXING_GIBBOUS
    assert before["illumination"] > 99


def test_quarters() -> None:
    # First quarter 15 April 2024 19:13 UTC; last quarter 2 April 03:15 UTC.
    first = moon(at("2024-04-15T19:13"))
    assert first["phase"] == FIRST_QUARTER
    assert abs(first["illumination"] - 50) < 2
    assert first["waxing"]
    last = moon(at("2024-04-02T03:15"))
    assert last["phase"] == LAST_QUARTER
    assert not last["waxing"]


def test_cycle_and_age_follow_the_lunation() -> None:
    new = at("2024-04-08T18:21")
    week = moon(new + timedelta(days=7))
    assert 6.9 < week["age"] < 7.1
    assert 0.2 < week["cycle"] < 0.3
    assert week["waxing"]
    late = moon(new + timedelta(days=22))
    assert 0.7 < late["cycle"] < 0.8
    assert not late["waxing"]


def test_cycle_draws_the_stated_fraction() -> None:
    """The card draws the terminator from `cycle` and prints `illumination`
    beside it. They must describe the same moon."""
    from math import cos, pi

    for day in range(0, 30, 3):
        state = moon(at("2026-10-01T00:00") + timedelta(days=day))
        drawn = (1 - cos(2 * pi * state["cycle"])) / 2 * 100
        assert abs(drawn - state["illumination"]) < 0.1


async def test_sensor_is_created(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.moon")
    assert state is not None
    assert state.state in state.attributes["options"]
    assert 0 <= state.attributes["illumination"] <= 100
    assert state.attributes["icon"].startswith("mdi:moon-")
    for key in ("next_new_moon", "next_full_moon"):
        assert datetime.fromisoformat(state.attributes[key])
