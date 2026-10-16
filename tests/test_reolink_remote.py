"""A Reolink camera's remote access switch.

The switch borrows the core integration's connection, so what is pinned
here is the borrowing: that it finds the camera through the core entry,
sends the camera's own command through that entry's API object, reads the
camera's reply as the camera means it, follows a change made elsewhere, and
goes unavailable rather than lying when the core integration is not
connected.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.home_signals.const import DOMAIN
from custom_components.home_signals.reolink_remote import ReolinkError


class FakeApi:
    """What the switch uses of reolink_aio's Host: `send`, and nothing else."""

    def __init__(self, enabled: int = 1) -> None:
        self.enabled = enabled
        self.sent: list[list[dict]] = []
        self.fail: Exception | None = None
        self.refuse = False

    async def send(self, body, param=None, expected_response_type="json", retry=3):
        self.sent.append(body)
        if self.fail is not None:
            raise self.fail
        cmd = body[0]["cmd"]
        if cmd == "GetP2p":
            return [{"cmd": cmd, "code": 0,
                     "value": {"P2p": {"enable": self.enabled, "uid": "9527TESTUID"}}}]
        if cmd == "SetP2p":
            if self.refuse:
                return [{"cmd": cmd, "code": 1,
                         "error": {"rspCode": -9, "detail": "not supported"}}]
            self.enabled = body[0]["param"]["P2p"]["enable"]
            return [{"cmd": cmd, "code": 0, "value": {"rspCode": 200}}]
        raise AssertionError(f"unexpected command {cmd}")


_CAMERAS: list[MockConfigEntry] = []


@pytest.fixture(autouse=True)
def _put_cameras_back(hass: HomeAssistant):
    """Leave no fake Reolink entry marked loaded when Home Assistant stops.

    The entries claim the real `reolink` domain, because that is what the
    switch looks for. A loaded entry is unloaded at shutdown, which imports
    the real integration -- and its import thread outlives the test, which
    the harness rightly refuses.
    """
    yield
    for camera in _CAMERAS:
        camera.mock_state(hass, ConfigEntryState.NOT_LOADED)
    _CAMERAS.clear()


def _camera(hass: HomeAssistant, api: FakeApi | None, *, loaded: bool = True) -> MockConfigEntry:
    """A core Reolink entry as the switch finds it, loaded or still retrying."""
    entry = MockConfigEntry(domain="reolink", title="Riley's Room",
                            data={"host": "192.0.2.10"})
    entry.add_to_hass(hass)
    if api is not None:
        entry.runtime_data = SimpleNamespace(host=SimpleNamespace(api=api))
    entry.mock_state(hass, ConfigEntryState.LOADED if loaded else ConfigEntryState.SETUP_RETRY)
    _CAMERAS.append(entry)
    return entry


async def _start(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _switch(hass: HomeAssistant, camera: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"reolink_remote_access_{camera.entry_id}")
    assert entity_id is not None, "no switch was made for the camera"
    return entity_id


async def test_a_camera_gets_a_switch_that_reads_the_camera(hass: HomeAssistant) -> None:
    api = FakeApi(enabled=1)
    camera = _camera(hass, api)
    await _start(hass)

    entity_id = _switch(hass, camera)
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["friendly_name"] == "Riley's Room remote access"
    assert api.sent == [[{"cmd": "GetP2p", "action": 0, "param": {}}]], (
        "the first read should be one GetP2p and nothing else"
    )


async def test_no_cameras_means_no_switches(hass: HomeAssistant) -> None:
    await _start(hass)
    assert hass.states.async_entity_ids("switch") == []


async def test_turning_it_off_sends_the_cameras_own_command(hass: HomeAssistant) -> None:
    api = FakeApi(enabled=1)
    camera = _camera(hass, api)
    await _start(hass)
    entity_id = _switch(hass, camera)

    # A polled switch is read back after every service call, so the set is
    # followed by a get: look for the command, not at the last thing sent.
    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "off"
    assert [{"cmd": "SetP2p", "action": 0, "param": {"P2p": {"enable": 0}}}] in api.sent
    assert api.enabled == 0, "the camera itself was not changed"

    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "on"
    assert [{"cmd": "SetP2p", "action": 0, "param": {"P2p": {"enable": 1}}}] in api.sent
    assert api.enabled == 1


async def test_a_change_made_in_the_client_shows_up(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The desktop Client can flip the same setting; the switch follows it."""
    api = FakeApi(enabled=1)
    camera = _camera(hass, api)
    await _start(hass)
    entity_id = _switch(hass, camera)
    assert hass.states.get(entity_id).state == "on"

    api.enabled = 0
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "off"


async def test_a_refused_change_is_an_error_not_a_lie(hass: HomeAssistant) -> None:
    """The camera says no in a normal reply, so a normal reply has to be read."""
    api = FakeApi(enabled=1)
    camera = _camera(hass, api)
    await _start(hass)
    entity_id = _switch(hass, camera)

    api.refuse = True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "on", "the switch moved on a refusal"

    api.refuse = False
    api.fail = ReolinkError("no route to host")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "on"


async def test_a_camera_the_core_integration_has_not_reached_is_unavailable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A camera still connecting at startup gets a switch that waits for it.

    This is the router-restart case: the core entry sits in retry, with no
    API object at all, until the camera is found again. The switch must not
    claim a state it has not read, and must come good on its own once the
    camera is back, without a restart of this integration.
    """
    camera = _camera(hass, None, loaded=False)
    await _start(hass)
    entity_id = _switch(hass, camera)
    assert hass.states.get(entity_id).state == "unavailable"

    api = FakeApi(enabled=0)
    camera.runtime_data = SimpleNamespace(host=SimpleNamespace(api=api))
    camera.mock_state(hass, ConfigEntryState.LOADED)
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "off"


async def test_a_camera_that_stops_answering_goes_unavailable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    api = FakeApi(enabled=1)
    camera = _camera(hass, api)
    await _start(hass)
    entity_id = _switch(hass, camera)

    api.fail = ReolinkError("connection error")
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "unavailable"
