"""Rooms are facts; radiators are jobs.

Two rules are asserted here over and over, because they are the two this
module exists to keep.

**The House climate card carries no level.** A cold room, an open window, a
zone on manual — all true, none of them a promise that something wants
doing. The moment one becomes a job it is a `Needs you` row and it is built
somewhere else.

**A room is read off the system that controls it.** Not averaged with
whatever else happens to be in the room. The tests below never introduce a
second thermometer, because the code must never be asked to choose between
two.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.rooms import (
    HouseClimateSensor,
    absolute_humidity,
    dew_point_c,
    read_zones,
    ventilation,
    zone_companions,
    zone_rows,
)
from custom_components.home_signals.const import (
    DOMAIN,
    LEVEL_ATTENTION,
    LEVEL_WAITING,
)
from custom_components.home_signals.derived import NeedsYouSensor

ZONE = "climate.kitchen"
WINDOW = "binary_sensor.kitchen_window"
OVERLAY = "binary_sensor.kitchen_overlay"
HEATING = "sensor.kitchen_heating"

OPTIONS = {
    "climate_zones": [ZONE],
    "climate_stuck_minutes": 30,
    "climate_stuck_rise": 0.2,
    "climate_manual_hours": 24,
}


# --- the house ---------------------------------------------------------


def _register(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """One Tado zone device, with its companions on it.

    The object ids are deliberately NOT `<zone>_window` and friends. Nothing
    promises that convention — an entity renamed in the UI keeps its unique
    id and loses its suffix — so discovery has to work from what each
    entity is.
    """
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("tado", "kitchen")},
        name="Tado Kitchen",
    )
    entities = er.async_get(hass)
    entities.async_get_or_create(
        "climate", "tado", "zone 6 377347",
        device_id=device.id, suggested_object_id="kitchen",
    )
    entities.async_get_or_create(
        "binary_sensor", "tado", "open window 6 377347",
        device_id=device.id, suggested_object_id="kitchen_window",
        original_device_class="window",
    )
    entities.async_get_or_create(
        "binary_sensor", "tado", "overlay 6 377347",
        device_id=device.id, suggested_object_id="kitchen_overlay",
        original_device_class="power",
    )
    entities.async_get_or_create(
        "sensor", "tado", "heating 6 377347",
        device_id=device.id, suggested_object_id="kitchen_heating",
    )
    # A humidity sensor on the same device, wearing the same percent sign as
    # the heating power. Telling them apart is what the device class is for.
    entities.async_get_or_create(
        "sensor", "tado", "humidity 6 377347",
        device_id=device.id, suggested_object_id="kitchen_humidity",
        original_device_class="humidity",
    )


def _set(
    hass: HomeAssistant,
    *,
    temperature: float = 18.0,
    humidity: float = 50.0,
    target: float | None = 20.0,
    heating: float = 0.0,
    window: bool = False,
    manual: bool = False,
    state: str = "heat",
) -> None:
    hass.states.async_set(ZONE, state, {
        "friendly_name": "Kitchen",
        "current_temperature": temperature,
        "current_humidity": humidity,
        "temperature": target,
        "hvac_action": "heating" if heating else "idle",
    })
    hass.states.async_set(HEATING, str(heating), {"unit_of_measurement": "%"})
    hass.states.async_set(
        "sensor.kitchen_humidity", str(humidity),
        {"unit_of_measurement": "%", "device_class": "humidity"},
    )
    hass.states.async_set(WINDOW, "on" if window else "off")
    hass.states.async_set(OVERLAY, "on" if manual else "off")


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    config = MockConfigEntry(domain=DOMAIN, data={}, options=OPTIONS)
    config.add_to_hass(hass)
    _register(hass, config)
    return config


@pytest.fixture
async def needs_you(hass: HomeAssistant, entry: MockConfigEntry) -> NeedsYouSensor:
    sensor = NeedsYouSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    return sensor


def _climate_rows(sensor: NeedsYouSensor) -> list[dict]:
    return [
        row for row in sensor.extra_state_attributes["items"]
        if row["id"].startswith("climate_")
    ]


# --- the physics -------------------------------------------------------


def test_dew_point_is_the_number_behind_damp() -> None:
    assert dew_point_c(21.0, 60.0) == pytest.approx(12.9, abs=0.2)
    assert dew_point_c(15.0, 60.0) == pytest.approx(7.3, abs=0.2)


def test_the_same_relative_humidity_is_not_the_same_air() -> None:
    """60% in a warm room and 60% in a cold one differ by five degrees of dew.

    Which is the whole reason the dew point is computed at all: relative
    humidity cannot be compared between two rooms at different temperatures,
    and comparing rooms is what this is for.
    """
    assert dew_point_c(21.0, 60.0) - dew_point_c(15.0, 60.0) > 5


def test_a_reading_the_formula_cannot_answer_for_is_not_answered() -> None:
    assert dew_point_c(21.0, 0.0) is None
    assert dew_point_c(None, 60.0) is None
    assert absolute_humidity(21.0, None) is None


def test_outside_can_be_drier_at_a_higher_percentage() -> None:
    """70% outside at 8° holds less water than 55% inside at 21°.

    Read the two percentages alone and you would shut the window. This is
    the one comparison that says otherwise, and the reason ventilation is
    answered in grams rather than percent.
    """
    assert absolute_humidity(8.0, 70.0) < absolute_humidity(21.0, 55.0)


# --- the rooms ---------------------------------------------------------


async def test_companions_are_found_by_what_they_are(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _set(hass)
    found = zone_companions(hass, ZONE)
    assert found["window"] == WINDOW
    assert found["overlay"] == OVERLAY
    # And not the humidity sensor, which wears the same percent sign.
    assert found["heating"] == HEATING


async def test_rooms_sort_by_shortfall_not_by_temperature(
    hass: HomeAssistant,
) -> None:
    """A 17° hall nobody heats is not a problem; a 17° study asked for 21° is."""
    readings = [
        {"entity_id": "climate.hall", "name": "Hall", "temperature": 17.0,
         "humidity": 50.0, "target": 17.5, "dew_point": None,
         "absolute_humidity": None, "heating": 0, "calling": False,
         "off": False, "manual": False, "manual_since": None,
         "window_open": False, "window_since": None},
        {"entity_id": "climate.study", "name": "Study", "temperature": 17.0,
         "humidity": 50.0, "target": 21.0, "dew_point": None,
         "absolute_humidity": None, "heating": 60, "calling": True,
         "off": False, "manual": False, "manual_since": None,
         "window_open": False, "window_since": None},
    ]
    rows = zone_rows(readings, 15, 25)
    assert [row["name"] for row in rows] == ["Study", "Hall"]


async def test_the_scale_is_fixed_and_clamps(hass: HomeAssistant) -> None:
    """Two screenshots a week apart have to be the same picture.

    A scale fitted to the day's own spread would put the coldest room at the
    same place on the bar every morning, whatever it read.
    """
    readings = [
        {"entity_id": "climate.a", "name": "A", "temperature": 30.0,
         "humidity": None, "target": None, "dew_point": None,
         "absolute_humidity": None, "heating": None, "calling": False,
         "off": False, "manual": False, "manual_since": None,
         "window_open": False, "window_since": None},
        {"entity_id": "climate.b", "name": "B", "temperature": 20.0,
         "humidity": None, "target": None, "dew_point": None,
         "absolute_humidity": None, "heating": None, "calling": False,
         "off": False, "manual": False, "manual_since": None,
         "window_open": False, "window_since": None},
    ]
    rows = {row["name"]: row for row in zone_rows(readings, 15, 25)}
    assert rows["B"]["bar"]["pct"] == 50
    assert rows["A"]["bar"]["pct"] == 100


async def test_a_room_carries_no_level(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Not even the cold one with its window open.

    A level is a promise that something wants doing, and this card is a
    statement of what is true. The promise is made in `Needs you` or it is
    not made at all.
    """
    _set(hass, temperature=14.0, window=True, heating=80)
    sensor = HouseClimateSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.house_climate"
    sensor._recompute()
    rooms = sensor.extra_state_attributes["rooms"]
    assert rooms
    assert all("level" not in room for room in rooms)
    # The window is still stated -- as a fact, in the room's own line.
    assert "window open" in rooms[0]["sub"]


async def test_an_off_zone_reports_no_target(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Tado's off is a frost setting of 5°, not a temperature anybody asked for."""
    _set(hass, state="off", target=5.0, temperature=19.0)
    reading = read_zones(hass, [ZONE])[0]
    assert reading["off"] is True
    assert reading["target"] is None
    assert zone_rows([reading], 15, 25)[0]["sub"].startswith("Off")


async def test_a_zone_that_cannot_be_read_is_not_a_room_at_zero(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    hass.states.async_set(ZONE, "unavailable")
    assert read_zones(hass, [ZONE]) == []


# --- ventilation -------------------------------------------------------


def test_airing_the_dampest_room_is_the_question_asked() -> None:
    indoor = [
        {"name": "Kitchen", "absolute_humidity": 11.0},
        {"name": "Study", "absolute_humidity": 8.0},
    ]
    answer = ventilation(indoor, 6.0)
    assert answer["room"] == "Kitchen"
    assert answer["direction"] == "drier"


def test_a_muggy_evening_says_so() -> None:
    answer = ventilation([{"name": "Study", "absolute_humidity": 9.0}], 13.0)
    assert answer["direction"] == "damper"


def test_no_outdoor_reading_means_no_answer() -> None:
    """Absent rather than guessed: a forecast for the region is another place."""
    assert ventilation([{"name": "Study", "absolute_humidity": 9.0}], None) is None


def test_a_difference_too_small_to_matter_is_not_a_direction() -> None:
    answer = ventilation([{"name": "Study", "absolute_humidity": 9.0}], 8.8)
    assert answer["direction"] == "no odds"


# --- the jobs ----------------------------------------------------------


async def test_heating_an_open_window_is_waiting(
    hass: HomeAssistant, needs_you: NeedsYouSensor
) -> None:
    """The gas is going out of the window now, and will until somebody shuts it."""
    _set(hass, window=True, heating=70)
    needs_you._recompute()
    rows = _climate_rows(needs_you)
    assert len(rows) == 1
    assert rows[0]["level"] == LEVEL_WAITING
    assert "open window" in rows[0]["title"]


async def test_an_open_window_on_its_own_is_not_a_row(
    hass: HomeAssistant, needs_you: NeedsYouSensor
) -> None:
    """A window is open for good reasons half the year."""
    _set(hass, window=True, heating=0)
    needs_you._recompute()
    assert _climate_rows(needs_you) == []


async def test_the_window_row_can_be_put_off_for_an_airing(
    hass: HomeAssistant, needs_you: NeedsYouSensor
) -> None:
    _set(hass, window=True, heating=70)
    needs_you._recompute()
    row = _climate_rows(needs_you)[0]
    assert row["action_label"] == "Airing"
    assert row["action"]["data"]["hours"] == 1


async def test_a_manual_override_keeps_for_a_day(
    hass: HomeAssistant, needs_you: NeedsYouSensor, freezer
) -> None:
    """An afternoon somebody meant is not a schedule that has stopped."""
    _set(hass, manual=True)
    freezer.tick(timedelta(hours=8))
    needs_you._recompute()
    assert _climate_rows(needs_you) == []


async def test_a_manual_override_held_longer_is_a_row(
    hass: HomeAssistant, needs_you: NeedsYouSensor, freezer
) -> None:
    _set(hass, manual=True, target=23.0)
    freezer.tick(timedelta(hours=30))
    needs_you._recompute()
    rows = _climate_rows(needs_you)
    assert len(rows) == 1
    assert rows[0]["level"] == LEVEL_ATTENTION
    assert "23.0°" in rows[0]["detail"]


async def test_the_override_row_hands_the_zone_back_to_its_schedule(
    hass: HomeAssistant, needs_you: NeedsYouSensor, freezer
) -> None:
    """The same press as the schedule button on the room's own card."""
    _set(hass, manual=True)
    freezer.tick(timedelta(hours=30))
    needs_you._recompute()
    action = _climate_rows(needs_you)[0]["action"]
    assert action["service"] == "climate.set_hvac_mode"
    assert action["data"]["hvac_mode"] == "auto"
    assert action["target"]["entity_id"] == ZONE


async def test_a_radiator_calling_without_moving_the_room(
    hass: HomeAssistant, needs_you: NeedsYouSensor, freezer
) -> None:
    """Air in the radiator, a seized pin, a valve reporting an open it has not.

    All three burn gas, none of them appears anywhere in Home Assistant, and
    the only evidence is that the room did not change.
    """
    _set(hass, temperature=18.0, heating=60)
    needs_you._recompute()
    assert _climate_rows(needs_you) == []

    freezer.tick(timedelta(minutes=35))
    _set(hass, temperature=18.1, heating=60)
    needs_you._recompute()

    rows = _climate_rows(needs_you)
    assert len(rows) == 1
    assert rows[0]["level"] == LEVEL_WAITING
    assert "stuck" in rows[0]["title"]


async def test_a_room_that_is_warming_raises_nothing(
    hass: HomeAssistant, needs_you: NeedsYouSensor, freezer
) -> None:
    _set(hass, temperature=18.0, heating=60)
    needs_you._recompute()
    freezer.tick(timedelta(minutes=35))
    _set(hass, temperature=18.6, heating=60)
    needs_you._recompute()
    assert _climate_rows(needs_you) == []


async def test_an_open_window_excuses_the_radiator(
    hass: HomeAssistant, needs_you: NeedsYouSensor, freezer
) -> None:
    """One cold room, one row. The window already explains it."""
    _set(hass, temperature=18.0, heating=60, window=True)
    needs_you._recompute()
    freezer.tick(timedelta(minutes=35))
    _set(hass, temperature=18.0, heating=60, window=True)
    needs_you._recompute()

    rows = _climate_rows(needs_you)
    assert len(rows) == 1
    assert "window" in rows[0]["title"]


async def test_the_run_restarts_when_the_radiator_stops(
    hass: HomeAssistant, needs_you: NeedsYouSensor, freezer
) -> None:
    """A radiator that reached its target and went off has not been stuck."""
    _set(hass, temperature=18.0, heating=60)
    needs_you._recompute()
    freezer.tick(timedelta(minutes=20))
    _set(hass, temperature=18.0, heating=0)
    needs_you._recompute()
    freezer.tick(timedelta(minutes=20))
    _set(hass, temperature=18.0, heating=60)
    needs_you._recompute()
    assert _climate_rows(needs_you) == []


async def test_no_zones_configured_means_no_rows(hass: HomeAssistant) -> None:
    """Every check here is optional, and an empty one contributes nothing."""
    config = MockConfigEntry(domain=DOMAIN, data={}, options={})
    config.add_to_hass(hass)
    sensor = NeedsYouSensor(config)
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    sensor._recompute()
    assert _climate_rows(sensor) == []


async def test_a_room_at_zero_is_still_the_coldest(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Freezing is a measurement, not a missing one.

    Zero is the one temperature where "which room is coldest" matters most,
    and it is also the one a falsy default swallows.
    """
    _set(hass, temperature=0.0)
    sensor = HouseClimateSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.house_climate"
    sensor._recompute()
    assert sensor.extra_state_attributes["coldest"] == "Kitchen"
