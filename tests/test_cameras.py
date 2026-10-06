"""When a camera last saw something, and not when its sensor last moved.

A Reolink in privacy mode makes every detection sensor unavailable, and
opening the lens brings them back as `off`. Read off `last_changed`, that
told the panel "Crying 5s ago" in a house where nobody had cried. These
pin the only two moments that count -- a sighting starting and ending --
and the record surviving a restart.
"""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.home_signals.cameras import (
    CameraSightingsSensor,
    camera_detections,
)
from custom_components.home_signals.const import DOMAIN

CRY = "binary_sensor.nursery_crying"
PERSON = "binary_sensor.nursery_person"
DOOR = "binary_sensor.front_door"


def _house(hass: HomeAssistant) -> None:
    """A camera with two detections, and a door sensor on its own device."""
    source = MockConfigEntry(domain="reolink")
    source.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    cam = devices.async_get_or_create(config_entry_id=source.entry_id, identifiers={("reolink", "cam")})
    door = devices.async_get_or_create(config_entry_id=source.entry_id, identifiers={("zha", "door")})
    registry.async_get_or_create("camera", "reolink", "cam_fluent", device_id=cam.id,
                                 suggested_object_id="nursery_fluent")
    for uid, obj, dev in (("cry", "nursery_crying", cam), ("person", "nursery_person", cam),
                          ("door", "front_door", door)):
        registry.async_get_or_create("binary_sensor", "reolink", uid, device_id=dev.id,
                                     suggested_object_id=obj)
    for entity_id in (CRY, PERSON, DOOR):
        hass.states.async_set(entity_id, "off")


async def _sensor(hass: HomeAssistant) -> CameraSightingsSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    sensor = CameraSightingsSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.camera_sightings"
    sensor.async_write_ha_state = lambda: None
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return sensor


def _seen(sensor: CameraSightingsSensor) -> dict:
    return sensor.extra_state_attributes["sightings"]


async def test_only_a_camera_s_own_sensors_are_detections(hass: HomeAssistant) -> None:
    _house(hass)
    assert camera_detections(er.async_get(hass)) == [CRY, PERSON]


async def test_coming_back_from_privacy_is_not_a_sighting(hass: HomeAssistant) -> None:
    """The bug: unavailable, then off, read as 'Crying 5s ago'."""
    _house(hass)
    sensor = await _sensor(hass)
    for state in ("unavailable", "off", "unavailable", "off"):
        hass.states.async_set(CRY, state)
        await hass.async_block_till_done()
    assert CRY not in _seen(sensor)
    assert sensor.native_value is None


async def test_a_sighting_is_dated_by_when_it_ended(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    _house(hass)
    sensor = await _sensor(hass)
    start = dt_util.utcnow()
    hass.states.async_set(PERSON, "on")
    await hass.async_block_till_done()
    row = _seen(sensor)[PERSON]
    assert row["on"] is True
    assert row["since"] == row["started"] == start.isoformat()

    freezer.tick(timedelta(seconds=40))
    hass.states.async_set(PERSON, "off")
    await hass.async_block_till_done()
    row = _seen(sensor)[PERSON]
    assert row["on"] is False
    assert row["started"] == start.isoformat()
    assert row["since"] == (start + timedelta(seconds=40)).isoformat()

    # Privacy on and off again afterwards changes nothing about it.
    freezer.tick(timedelta(hours=2))
    for state in ("unavailable", "off"):
        hass.states.async_set(PERSON, state)
        await hass.async_block_till_done()
    assert _seen(sensor)[PERSON]["since"] == (start + timedelta(seconds=40)).isoformat()


async def test_the_lens_shutting_mid_sighting_ends_it(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    _house(hass)
    sensor = await _sensor(hass)
    hass.states.async_set(PERSON, "on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=5))
    shut = dt_util.utcnow()
    hass.states.async_set(PERSON, "unavailable")
    await hass.async_block_till_done()
    assert _seen(sensor)[PERSON] == {**_seen(sensor)[PERSON], "on": False, "since": shut.isoformat()}


async def test_a_restart_keeps_the_record(hass: HomeAssistant) -> None:
    """Home Assistant comes back with every sensor freshly 'off', changed now."""
    seen = (dt_util.utcnow() - timedelta(hours=3)).isoformat()
    mock_restore_cache(hass, (State("sensor.camera_sightings", seen, {"sightings": {
        PERSON: {"on": False, "since": seen, "started": seen},
    }}),))
    _house(hass)
    sensor = await _sensor(hass)
    assert _seen(sensor)[PERSON]["since"] == seen
    assert CRY not in _seen(sensor)
    assert sensor.native_value.isoformat() == seen


async def test_a_door_is_not_a_camera(hass: HomeAssistant) -> None:
    _house(hass)
    sensor = await _sensor(hass)
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    assert DOOR not in _seen(sensor)
