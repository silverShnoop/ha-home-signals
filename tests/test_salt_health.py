"""Salt on the Maintenance tab: the card decides, the row and the tab follow.

The Water softener card's own sensor decides its level and raises the job;
Needs you shows the job and the Maintenance rail button wears the level.
Tested against the same readings, because the way this goes wrong is two
copies of one threshold drifting apart.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import DOMAIN, LEVEL_ATTENTION
from tests.owners import attach, owner
from custom_components.home_signals.derived import NeedsYouSensor, SoftenerStatusSensor

LEFT = "sensor.softener_salt_left_side_percentage"
RIGHT = "sensor.softener_salt_right_side_percentage"

OPTIONS = {
    "salt_sensors": [LEFT, RIGHT],
    "salt_both_threshold": 40,
    "salt_one_threshold": 25,
}


def _needs(hass: HomeAssistant) -> NeedsYouSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=OPTIONS)
    entry.add_to_hass(hass)
    sensor = NeedsYouSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    attach(hass, sensor)
    sensor._recompute()  # noqa: SLF001
    return sensor


def _salt(hass: HomeAssistant, left: str, right: str) -> None:
    hass.states.async_set(LEFT, left, {"friendly_name": "Softener Salt left side percentage"})
    hass.states.async_set(RIGHT, right, {"friendly_name": "Softener Salt right side percentage"})


async def test_low_salt_outlines_the_card_and_the_tab(hass: HomeAssistant) -> None:
    _salt(hass, "30", "0")
    needs = _needs(hass)
    card = owner(needs, SoftenerStatusSensor).extra_state_attributes
    attrs = needs.extra_state_attributes

    assert card["level"] == LEVEL_ATTENTION
    assert attrs["tab_maintenance"] == LEVEL_ATTENTION, "the rail button stayed quiet"
    row = next(r for r in attrs["items"] if r["id"] == "softener_salt")
    assert row["level"] == LEVEL_ATTENTION
    assert attrs["summary_maintenance"] == row["title"]


async def test_full_salt_says_nothing(hass: HomeAssistant) -> None:
    _salt(hass, "80", "60")
    needs = _needs(hass)
    attrs = needs.extra_state_attributes

    assert owner(needs, SoftenerStatusSensor).owner_level is None
    assert attrs["tab_maintenance"] is None, "a full softener coloured the tab"
    assert attrs["summary_maintenance"] == "Nothing waiting"


async def test_no_reading_is_not_low(hass: HomeAssistant) -> None:
    _salt(hass, "unavailable", "unknown")
    needs = _needs(hass)
    assert owner(needs, SoftenerStatusSensor).owner_level is None
    assert not any(r["id"] == "softener_salt" for r in needs.extra_state_attributes["items"])


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
    needs = _needs(hass)
    attrs = needs.extra_state_attributes
    row = any(r["id"] == "softener_salt" for r in attrs["items"])

    assert (owner(needs, SoftenerStatusSensor).owner_level == LEVEL_ATTENTION) is row
    assert (attrs["tab_maintenance"] == LEVEL_ATTENTION) is row
