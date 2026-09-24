"""Salt on the Maintenance tab: the card, the rail button and the row agree.

A level is a three-way obligation. While `Needs you` carries the softener
row, the Water softener card is outlined (from `salt_level`) and the
Maintenance tab's rail button wears the same level (from `level`). Once the
row clears, all three go quiet together. Both halves are tested here against
the same readings as the Needs you row, because the way this goes wrong is
two copies of one threshold drifting apart.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import DOMAIN, LEVEL_ATTENTION
from custom_components.home_signals.derived import NeedsYouSensor, SystemHealthSensor

LEFT = "sensor.softener_salt_left_side_percentage"
RIGHT = "sensor.softener_salt_right_side_percentage"

OPTIONS = {
    "salt_sensors": [LEFT, RIGHT],
    "salt_both_threshold": 40,
    "salt_one_threshold": 25,
}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=OPTIONS)
    entry.add_to_hass(hass)
    return entry


def _health(hass: HomeAssistant) -> dict:
    sensor = SystemHealthSensor(_entry(hass))
    sensor.hass = hass
    sensor.entity_id = "sensor.system_health"
    sensor._recompute()  # noqa: SLF001
    return sensor.extra_state_attributes


def _needs_you_has_salt(hass: HomeAssistant) -> bool:
    sensor = NeedsYouSensor(_entry(hass))
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    sensor._recompute()  # noqa: SLF001
    return any(r["id"] == "softener_salt" for r in sensor.extra_state_attributes["items"])


def _salt(hass: HomeAssistant, left: str, right: str) -> None:
    hass.states.async_set(LEFT, left, {"friendly_name": "Softener Salt left side percentage"})
    hass.states.async_set(RIGHT, right, {"friendly_name": "Softener Salt right side percentage"})


async def test_low_salt_outlines_the_card_and_the_tab(hass: HomeAssistant) -> None:
    _salt(hass, "30", "0")
    attrs = _health(hass)

    assert attrs["salt_level"] == LEVEL_ATTENTION
    assert attrs["level"] == LEVEL_ATTENTION, "the rail button stayed quiet"
    row = next(r for r in attrs["items"] if r["id"] == "softener_salt")
    assert row["level"] == LEVEL_ATTENTION
    assert row["sub"] == "Left 30%, Right 0%"
    assert row["value"] == "0%"


async def test_full_salt_says_nothing(hass: HomeAssistant) -> None:
    _salt(hass, "80", "60")
    attrs = _health(hass)

    assert attrs["salt_level"] is None
    assert attrs["level"] is None, "a full softener coloured the tab"
    assert not any(r["id"] == "softener_salt" for r in attrs["items"])


async def test_no_reading_is_not_low(hass: HomeAssistant) -> None:
    _salt(hass, "unavailable", "unknown")
    attrs = _health(hass)

    assert attrs["salt_level"] is None
    assert not any(r["id"] == "softener_salt" for r in attrs["items"])


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("30", "0"),     # one side under the sharper line
        ("35", "38"),    # both under the gentler line
        ("50", "26"),    # neither rule: one side just above 25
        ("41", "39"),    # neither rule: one side just above 40
        ("25", "90"),    # exactly on the one-side line
        ("40", "40"),    # exactly on the both-sides line
    ],
)
async def test_card_tab_and_row_agree(
    hass: HomeAssistant, left: str, right: str
) -> None:
    _salt(hass, left, right)
    row = _needs_you_has_salt(hass)
    attrs = _health(hass)

    assert (attrs["salt_level"] == LEVEL_ATTENTION) is row
    assert (attrs["level"] == LEVEL_ATTENTION) is row
