"""`by_area`: the last hour per room, for a floor plan.

The rail's twenty rows are about ten minutes of an ordinary evening. A plan
that fades over an hour read off them would show a busy house as quiet the
moment the hall sensor had filled the list -- so the plan gets its own
record, bounded by age rather than by length.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.home_signals.const import DOMAIN

HALL = "binary_sensor.hall_motion"
STUDY = "event.study_button"
NOWHERE = "binary_sensor.shed_motion"


def _place(hass: HomeAssistant, entity_id: str, area: str | None) -> None:
    domain, _, object_id = entity_id.partition(".")
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(domain, "test", object_id, suggested_object_id=object_id)
    if area is not None:
        area_id = ar.async_get(hass).async_get_or_create(area).id
        registry.async_update_entity(entry.entity_id, area_id=area_id)


async def _start(hass: HomeAssistant) -> None:
    _place(hass, HALL, "Hall")
    _place(hass, STUDY, "Study")
    _place(hass, NOWHERE, None)
    hass.states.async_set(HALL, "off", {"device_class": "motion"})
    hass.states.async_set(STUDY, "unknown")
    hass.states.async_set(NOWHERE, "off", {"device_class": "motion"})
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"entities": [HALL, STUDY, NOWHERE]}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _by_area(hass: HomeAssistant) -> dict:
    return hass.states.get("sensor.activity_feed").attributes.get("by_area") or {}


async def _trip(hass: HomeAssistant, entity_id: str = HALL) -> None:
    hass.states.async_set(entity_id, "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    hass.states.async_set(entity_id, "off", {"device_class": "motion"})
    await hass.async_block_till_done()


async def test_each_room_keeps_every_trip_not_just_the_last(hass: HomeAssistant) -> None:
    """A heat map is a count as well as an age."""
    await _start(hass)
    for _ in range(3):
        await _trip(hass)
    hass.states.async_set(STUDY, "2026-09-24T20:00:00.000+00:00")
    await hass.async_block_till_done()

    rooms = _by_area(hass)
    assert len(rooms["Hall"]["times"]) == 3, rooms
    assert rooms["Hall"]["kind"] == "motion"
    assert rooms["Study"]["kind"] == "button"
    assert len(rooms["Study"]["times"]) == 1


async def test_it_outlives_the_rails_cap(hass: HomeAssistant) -> None:
    """Thirty trips is more than the rail keeps, and all of them are the hour."""
    await _start(hass)
    for _ in range(30):
        await _trip(hass)

    feed = hass.states.get("sensor.activity_feed")
    assert len(feed.attributes["events"]) == 20
    assert len(_by_area(hass)["Hall"]["times"]) == 30


async def test_an_entity_with_no_room_has_nowhere_to_be_drawn(hass: HomeAssistant) -> None:
    await _start(hass)
    await _trip(hass, NOWHERE)
    assert _by_area(hass) == {}
    events = hass.states.get("sensor.activity_feed").attributes["events"]
    assert events and events[0]["entity_id"] == NOWHERE, "the rail still has it"


async def test_an_hour_later_the_room_is_forgotten(hass: HomeAssistant) -> None:
    await _start(hass)
    await _trip(hass)
    later = dt_util.utcnow() + timedelta(minutes=61)
    with patch("homeassistant.util.dt.utcnow", return_value=later):
        hass.states.async_set(STUDY, "2026-09-24T20:00:00.000+00:00")
        await hass.async_block_till_done()

    rooms = _by_area(hass)
    assert "Hall" not in rooms, rooms
    assert "Study" in rooms


async def test_a_restart_brings_the_hour_back(hass: HomeAssistant) -> None:
    now = int(dt_util.utcnow().timestamp())
    mock_restore_cache(hass, [State(
        "sensor.activity_feed", dt_util.utcnow().isoformat(),
        {
            "events": [],
            "by_area": {
                "Hall": {"kind": "motion", "at": "x", "times": [now - 60, now - 120]},
                "Stale": {"kind": "motion", "at": "x", "times": [now - 7200]},
            },
        },
    )])
    await _start(hass)

    rooms = _by_area(hass)
    assert rooms["Hall"]["times"] == [now - 60, now - 120]
    assert "Stale" not in rooms, "a restore must not bring back more than the hour"
