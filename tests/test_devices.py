"""Devices, counted as things rather than entities, with a clock that survives.

A car that loses its cloud is eighteen entities and one car, and a bulb
dead for a week must not read as having died at the last reboot.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import (
    CONF_IGNORE_UNAVAILABLE,
    DOMAIN,
    LEVEL_ATTENTION,
)
from custom_components.home_signals.devices import DevicesSensor


def _device(
    hass: HomeAssistant,
    source: MockConfigEntry,
    name: str,
    readings: dict[str, str],
    *,
    domain: str = "hue",
    model: str = "Hue ambiance spot",
) -> str:
    """A device with one entity per reading, each set to the given state."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={(domain, name)},
        name=name,
        model=model,
    )
    registry = er.async_get(hass)
    for key, value in readings.items():
        entry = registry.async_get_or_create(
            "sensor", domain, f"{name}_{key}",
            device_id=device.id, original_name=key.title(),
            suggested_object_id=f"{name}_{key}".lower().replace(" ", "_"),
        )
        hass.states.async_set(entry.entity_id, value)
    return device.id


def _sensor(hass: HomeAssistant, **options) -> DevicesSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    sensor = DevicesSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.devices"
    sensor._recompute()  # noqa: SLF001
    return sensor


def _source(hass: HomeAssistant) -> MockConfigEntry:
    source = MockConfigEntry(domain="hue")
    source.add_to_hass(hass)
    return source


async def test_a_device_is_counted_once_however_many_entities(
    hass: HomeAssistant,
) -> None:
    src = _source(hass)
    _device(hass, src, "Car", {k: "unavailable" for k in "abcdefgh"}, domain="stellantis")
    _device(hass, src, "Hall 1", {"light": "on"})
    attrs = _sensor(hass).extra_state_attributes

    assert (attrs["connected"], attrs["offline"], attrs["partial"]) == (1, 1, 0)
    assert [p["name"] for p in attrs["problems"]] == ["Car"]
    assert attrs["problems"][0]["network"] == "Wi-Fi & cloud"
    assert attrs["level"] == LEVEL_ATTENTION


async def test_partly_answering_names_the_one_missing_reading(
    hass: HomeAssistant,
) -> None:
    src = _source(hass)
    _device(hass, src, "Living Room Sensor", {"motion": "off", "temperature": "unavailable"})
    _device(hass, src, "Atom Echo", {"a": "unavailable", "b": "unavailable", "c": "on"},
            domain="esphome")
    problems = {p["name"]: p for p in _sensor(hass).extra_state_attributes["problems"]}

    assert problems["Living Room Sensor"]["state"] == "partial"
    assert problems["Living Room Sensor"]["detail"] == "No temperature"
    assert problems["Atom Echo"]["detail"] == "2 of 3 missing"


async def test_groups_and_rooms_are_not_devices(hass: HomeAssistant) -> None:
    """A Hue room and a Cast group would report one dead thing twice."""
    src = _source(hass)
    _device(hass, src, "Kitchen", {"light": "unavailable"}, model="Room")
    _device(hass, src, "Upstairs", {"media": "unavailable"}, domain="cast",
            model="Google Cast Group")
    attrs = _sensor(hass).extra_state_attributes

    assert attrs["problems"] == []
    assert attrs["level"] is None


async def test_networks_are_tallied_in_a_fixed_order(hass: HomeAssistant) -> None:
    src = _source(hass)
    _device(hass, src, "Plug", {"power": "3"}, domain="zha")
    _device(hass, src, "Speaker", {"media": "unavailable"}, domain="cast")
    _device(hass, src, "Bulb", {"light": "on"})
    nets = _sensor(hass).extra_state_attributes["networks"]

    assert [n["name"] for n in nets] == ["Hue", "Zigbee", "Cast"]
    assert nets[2] == {"name": "Cast", "online": 0, "offline": 1, "partial": 0}


async def test_an_ignored_entity_does_not_make_its_device_partial(
    hass: HomeAssistant,
) -> None:
    src = _source(hass)
    _device(hass, src, "Button", {"battery": "unavailable", "press": "on"})
    attrs = _sensor(
        hass, **{CONF_IGNORE_UNAVAILABLE: ["sensor.button_battery"]}
    ).extra_state_attributes

    assert attrs["problems"] == []


async def test_the_offline_clock_starts_when_it_drops_and_ends_when_it_returns(
    hass: HomeAssistant,
) -> None:
    src = _source(hass)
    _device(hass, src, "Bulb", {"light": "on"})
    sensor = _sensor(hass)
    sensor._recompute()  # noqa: SLF001  (a second scan: now baselined)

    hass.states.async_set("sensor.bulb_light", "unavailable")
    before = dt_util.utcnow()
    sensor._recompute()  # noqa: SLF001
    since = dt_util.parse_datetime(sensor.extra_state_attributes["problems"][0]["since"])
    assert since is not None and since >= before - timedelta(seconds=1)

    hass.states.async_set("sensor.bulb_light", "on")
    sensor._recompute()  # noqa: SLF001
    assert sensor.extra_state_attributes["since"] == {}


async def test_a_device_already_offline_on_first_look_has_no_time(
    hass: HomeAssistant,
) -> None:
    """The first scan cannot know when it dropped, so it does not guess."""
    src = _source(hass)
    _device(hass, src, "Bulb", {"light": "unavailable"})
    sensor = _sensor(hass)

    assert sensor.extra_state_attributes["problems"][0]["since"] is None
    sensor._recompute()  # noqa: SLF001
    assert sensor.extra_state_attributes["problems"][0]["since"] is None, (
        "the second scan invented a time for a device that was already down"
    )


def test_a_three_part_identifier_does_not_take_the_sensor_down() -> None:
    """Some integrations register (domain, a, b); the live house has one."""

    class _Device:
        identifiers = {("legacy", "hub", "7"), ("zha", "00:11")}

    assert DevicesSensor._network_of(_Device()) == "Zigbee"  # noqa: SLF001


async def test_a_device_with_only_diagnostics_is_still_counted(
    hass: HomeAssistant,
) -> None:
    """A ZHA button's presses are events; its battery is how we know it is there."""
    src = _source(hass)
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=src.entry_id, identifiers={("zha", "button")},
        name="Washer Button", model="SNZB-01P",
    )
    battery = registry.async_get_or_create(
        "sensor", "zha", "button_battery", device_id=device.id,
        entity_category=er.EntityCategory.DIAGNOSTIC,
        suggested_object_id="washer_button_battery",
    )
    hass.states.async_set(battery.entity_id, "100")
    sensor = _sensor(hass)
    nets = {n["name"]: n for n in sensor.extra_state_attributes["networks"]}
    assert nets["Zigbee"]["online"] == 1, "a button with only a battery went uncounted"

    hass.states.async_set(battery.entity_id, "unavailable")
    sensor._recompute()  # noqa: SLF001
    assert [p["name"] for p in sensor.extra_state_attributes["problems"]] == ["Washer Button"]


async def test_a_quiet_diagnostic_does_not_fault_a_device_that_answers(
    hass: HomeAssistant,
) -> None:
    """A bulb whose signal reading is unavailable is still a working bulb."""
    src = _source(hass)
    device_id = _device(hass, src, "Bulb", {"light": "on"})
    signal = er.async_get(hass).async_get_or_create(
        "sensor", "hue", "bulb_rssi", device_id=device_id,
        entity_category=er.EntityCategory.DIAGNOSTIC,
        suggested_object_id="bulb_rssi",
    )
    hass.states.async_set(signal.entity_id, "unavailable")

    assert _sensor(hass).extra_state_attributes["problems"] == []


async def test_needs_you_counts_the_same_devices_as_the_card(
    hass: HomeAssistant,
) -> None:
    """The row said "31 entities offline" beside a card saying 7. One scan now."""
    from custom_components.home_signals.derived import NeedsYouSensor

    src = _source(hass)
    _device(hass, src, "Car", {k: "unavailable" for k in "abcdefgh"} | {"z": "on"},
            domain="stellantis")
    _device(hass, src, "Speaker", {"media": "unavailable"}, domain="cast")
    _device(hass, src, "Upstairs", {"media": "unavailable"}, domain="cast",
            model="Google Cast Group")
    _device(hass, src, "Bulb", {"light": "on"})

    card = _sensor(hass).extra_state_attributes
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    needs_you = NeedsYouSensor(entry)
    needs_you.hass = hass
    [row] = needs_you._offline()  # noqa: SLF001

    assert (card["offline"], card["partial"]) == (1, 1)
    assert row["title"] == "1 device offline, 1 partly", row["title"]
    assert row["detail"] == "Car, Speaker"
