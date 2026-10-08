"""A Frigate camera's jobs: a parcel left in view, a camera gone quiet.

Everything else a camera sees is a fact for its card. These pin the two
rows, that each clears itself, that one camera's parcel does not colour
another camera's card, and that a camera Frigate adds later is heard at
once rather than at the next scan.
"""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import DOMAIN
from custom_components.home_signals.derived import NeedsYouSensor
from custom_components.home_signals.frigate_status import CameraStatusSensor, frigate_cameras

from .owners import attach


def _camera(hass: HomeAssistant, frigate: MockConfigEntry, cam: str, name: str) -> tuple[str, str]:
    """A Frigate camera with its parcel and frame-rate sensors."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=frigate.entry_id, identifiers={("frigate", cam)}, name=name)
    registry = er.async_get(hass)
    registry.async_get_or_create("camera", "frigate", f"{frigate.entry_id}:camera:{cam}",
                                 device_id=device.id, suggested_object_id=cam)
    parcel = registry.async_get_or_create(
        "binary_sensor", "frigate", f"{frigate.entry_id}:occupancy_sensor:{cam}_package",
        device_id=device.id, suggested_object_id=f"{cam}_package_occupancy").entity_id
    fps = registry.async_get_or_create(
        "sensor", "frigate", f"{frigate.entry_id}:sensor_fps:{cam}_camera",
        device_id=device.id, suggested_object_id=f"{cam}_camera_fps").entity_id
    hass.states.async_set(parcel, "off")
    hass.states.async_set(fps, "5.1")
    return parcel, fps


def _frigate(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="frigate", entry_id="fr1")
    entry.add_to_hass(hass)
    return entry


async def _sensor(hass: HomeAssistant) -> CameraStatusSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    sensor = CameraStatusSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.camera_status"
    sensor.async_write_ha_state = lambda: None
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return sensor


def _jobs(sensor: CameraStatusSensor) -> list[dict]:
    return sensor.extra_state_attributes["jobs"]


async def test_cameras_are_found_by_their_unique_id(hass: HomeAssistant) -> None:
    frigate = _frigate(hass)
    _camera(hass, frigate, "front_gate", "Front Gate")
    # A camera on another integration is not Frigate's.
    er.async_get(hass).async_get_or_create("camera", "reolink", "rileys_room", suggested_object_id="rileys_room")
    assert frigate_cameras(er.async_get(hass)) == [("fr1", "front_gate", "camera.front_gate")]


async def test_a_parcel_is_a_job_until_it_is_gone(hass: HomeAssistant) -> None:
    frigate = _frigate(hass)
    parcel, _fps = _camera(hass, frigate, "gate", "Gate")
    sensor = await _sensor(hass)
    assert sensor.native_value == "clear"

    hass.states.async_set(parcel, "on")
    await hass.async_block_till_done()
    [job] = _jobs(sensor)
    assert job["id"] == "parcel_gate"
    assert job["title"] == "Bring the parcel in"
    assert job["detail"].startswith("Seen by the Gate camera since ")
    assert job["level"] == "attention"
    assert job["tab"] == "security"
    # Snooze only: a button does not bring a parcel in.
    assert job["snooze_only"] is True
    assert job["action"]["service"] == f"{DOMAIN}.snooze"
    assert "accent" not in job
    assert sensor.native_value == "attention"
    assert sensor.extra_state_attributes["cameras"]["gate"]["level"] == "attention"

    hass.states.async_set(parcel, "off")
    await hass.async_block_till_done()
    assert _jobs(sensor) == []
    assert sensor.extra_state_attributes["cameras"]["gate"]["level"] is None


async def test_a_camera_is_stopped_after_five_quiet_minutes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    frigate = _frigate(hass)
    parcel, fps = _camera(hass, frigate, "gate", "Gate")
    sensor = await _sensor(hass)

    hass.states.async_set(fps, "0")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=4))
    assert _jobs(sensor) == [], "a reboot is not news"

    freezer.tick(timedelta(minutes=2))
    [job] = _jobs(sensor)
    assert job["id"] == "camera_stopped_gate"
    assert job["title"] == "Gate camera has stopped"
    assert job["level"] == "waiting"

    # A camera sending nothing sees no parcels; its sensor is stale.
    hass.states.async_set(parcel, "on")
    await hass.async_block_till_done()
    assert [j["id"] for j in _jobs(sensor)] == ["camera_stopped_gate"]

    hass.states.async_set(fps, "5.0")
    await hass.async_block_till_done()
    assert [j["id"] for j in _jobs(sensor)] == ["parcel_gate"]


async def test_frigate_gone_counts_as_stopped(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    frigate = _frigate(hass)
    _parcel, fps = _camera(hass, frigate, "gate", "Gate")
    sensor = await _sensor(hass)
    hass.states.async_set(fps, "unavailable")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=6))
    assert [j["id"] for j in _jobs(sensor)] == ["camera_stopped_gate"]


async def test_one_camera_s_parcel_is_not_another_s(hass: HomeAssistant) -> None:
    frigate = _frigate(hass)
    _camera(hass, frigate, "gate", "Gate")
    back, _ = _camera(hass, frigate, "back_door", "Back Door")
    sensor = await _sensor(hass)
    hass.states.async_set(back, "on")
    await hass.async_block_till_done()
    cams = sensor.extra_state_attributes["cameras"]
    assert cams["back_door"]["level"] == "attention"
    assert cams["gate"]["level"] is None
    assert cams["gate"]["entity"] == "camera.gate"


async def test_a_camera_added_later_is_heard_at_once(hass: HomeAssistant) -> None:
    frigate = _frigate(hass)
    sensor = await _sensor(hass)
    assert _jobs(sensor) == []
    parcel, _ = _camera(hass, frigate, "gate", "Gate")
    await hass.async_block_till_done()
    writes = []
    sensor.async_write_ha_state = lambda: writes.append(sensor._jobs)  # noqa: SLF001
    hass.states.async_set(parcel, "on")
    await hass.async_block_till_done()
    assert writes and [j["id"] for j in writes[-1]] == ["parcel_gate"]


async def test_the_row_reaches_needs_you_and_colours_security(hass: HomeAssistant) -> None:
    frigate = _frigate(hass)
    parcel, _ = _camera(hass, frigate, "gate", "Gate")
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    needs = NeedsYouSensor(entry)
    needs.hass = hass
    needs.entity_id = "sensor.needs_you"
    cams = CameraStatusSensor(entry)
    attach(hass, needs, cams)
    hass.states.async_set(parcel, "on")
    needs._recompute()  # noqa: SLF001
    attrs = needs.extra_state_attributes
    assert any(row["id"] == "parcel_gate" for row in attrs["items"])
    assert attrs["tab_security"] == "attention"
