"""Every battery, for the card that shows how the rest stand.

`low_batteries` answers "what needs changing". The Batteries card also
wants the ones that do not, so it can draw them small underneath -- and
it must draw the same line Needs you does, which is why `low` is decided
here and not by the card.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import (
    CONF_BATTERY_THRESHOLD,
    CONF_IGNORE_UNAVAILABLE,
    DOMAIN,
    LEVEL_ATTENTION,
)
from custom_components.home_signals.derived import SystemHealthSensor


def _health(hass: HomeAssistant, **options) -> SystemHealthSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    sensor = SystemHealthSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.system_health"
    sensor._recompute()  # noqa: SLF001
    return sensor


def _battery(hass: HomeAssistant, object_id: str, name: str, state: str) -> None:
    hass.states.async_set(
        f"sensor.{object_id}", state,
        {"device_class": "battery", "friendly_name": name},
    )


async def test_every_battery_is_listed_worst_first(hass: HomeAssistant) -> None:
    _battery(hass, "hall", "Hall Sensor Battery", "71")
    _battery(hass, "door", "Front Door Battery", "12")
    _battery(hass, "softener", "Water Softener Battery", "30")
    attrs = _health(hass).extra_state_attributes

    assert [b["percent"] for b in attrs["batteries"]] == [12, 30, 71]
    assert [b["low"] for b in attrs["batteries"]] == [True, False, False]
    # The word the card is about to add is not repeated in the name.
    assert attrs["batteries"][0]["name"] == "Front Door"
    assert attrs["battery_threshold"] == 20


async def test_low_follows_the_configured_line(hass: HomeAssistant) -> None:
    _battery(hass, "softener", "Water Softener Battery", "30")
    attrs = _health(hass, **{CONF_BATTERY_THRESHOLD: 35}).extra_state_attributes

    assert attrs["batteries"][0]["low"] is True
    assert attrs["battery_level"] == LEVEL_ATTENTION
    assert len(attrs["low_batteries"]) == 1, (
        "the card and the low list disagree about where the line is"
    )


async def test_no_flat_battery_is_no_level(hass: HomeAssistant) -> None:
    _battery(hass, "hall", "Hall Sensor Battery", "71")
    assert _health(hass).extra_state_attributes["battery_level"] is None, (
        "the card would be yellow with nothing to change"
    )


async def test_ignored_and_unreadable_batteries_are_left_out(
    hass: HomeAssistant,
) -> None:
    """A Hue button stuck at 1% is not a battery at 1%, and unknown is not 0."""
    _battery(hass, "button", "Kitchen Button Battery", "1")
    _battery(hass, "car", "Peugeot Service battery", "unknown")
    _battery(hass, "hall", "Hall Sensor Battery", "71")
    attrs = _health(
        hass, **{CONF_IGNORE_UNAVAILABLE: ["sensor.button"]}
    ).extra_state_attributes

    assert [b["entity_id"] for b in attrs["batteries"]] == ["sensor.hall"]
    assert attrs["battery_level"] is None
