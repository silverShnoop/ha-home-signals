"""What can be put off, and what cannot.

Every row on `Needs you` can be dismissed or snoozed, because putting a
job off is a real answer to it -- the bins come round again, the washing
waits. Salt is the exception, and this file exists to keep it one.

The softener row is only ever true when there is a bag to fetch from the
garage or a bag to buy, and it clears itself the moment the level comes
back up. Snoozing it does not make the softener wait: it passes hard
water through the house until somebody notices the limescale.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import (
    DOMAIN,
    LEVEL_ATTENTION,
    LEVEL_CRITICAL,
    LEVEL_WAITING,
)
from custom_components.home_signals.derived import NeedsYouSensor

LEFT = "sensor.softener_left"
RIGHT = "sensor.softener_right"

OPTIONS = {
    "salt_sensors": [LEFT, RIGHT],
    "salt_both_threshold": 40,
    "salt_one_threshold": 25,
}


def _sensor(hass: HomeAssistant) -> NeedsYouSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=OPTIONS)
    entry.add_to_hass(hass)
    sensor = NeedsYouSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    return sensor


def _rows(sensor: NeedsYouSensor) -> list[dict]:
    return sensor.extra_state_attributes["items"]


def _salt(sensor: NeedsYouSensor) -> dict | None:
    return next((r for r in _rows(sensor) if r["id"] == "softener_salt"), None)


async def _low(hass: HomeAssistant) -> NeedsYouSensor:
    hass.states.async_set(LEFT, "30")
    hass.states.async_set(RIGHT, "0")
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return sensor


async def test_low_salt_is_a_row(hass: HomeAssistant) -> None:
    sensor = await _low(hass)
    row = _salt(sensor)
    assert row is not None, _rows(sensor)
    assert "salt" in row["title"].lower()


async def test_it_offers_no_way_to_put_it_off(hass: HomeAssistant) -> None:
    """No button. Snoozing does not make the softener wait."""
    row = _salt(await _low(hass))

    assert "action" not in row, row.get("action")
    assert "action_label" not in row, row.get("action_label")


async def test_and_snoozing_it_anyway_does_nothing(hass: HomeAssistant) -> None:
    """The button is not the only way in.

    The service is there for anything to call -- an automation, a voice
    command, a stale suppression restored from before the button went.
    A row that cannot be cleared by hand must not be clearable by any
    of those either, or "you cannot snooze it" is only true of the card.
    """
    sensor = await _low(hass)
    assert _salt(sensor) is not None

    sensor.suppress("softener_salt", hours=24)
    assert _salt(sensor) is not None, "the salt row was snoozed away"

    sensor.suppress("softener_salt")  # a dismissal: no hours, forever
    assert _salt(sensor) is not None, "the salt row was dismissed away"


async def test_topping_it_up_is_what_clears_it(hass: HomeAssistant) -> None:
    """The only thing that should, and the reason no button is needed."""
    sensor = await _low(hass)
    assert _salt(sensor) is not None

    hass.states.async_set(LEFT, "90")
    hass.states.async_set(RIGHT, "95")
    await hass.async_block_till_done()
    sensor._recompute()  # noqa: SLF001

    assert _salt(sensor) is None, "the row survived the softener being filled"


async def test_other_rows_can_still_be_put_off(hass: HomeAssistant) -> None:
    """Sticky is the exception, not the new rule.

    Without this, making salt permanent by breaking suppression for
    everything would pass every assertion above.
    """
    hass.states.async_set(LEFT, "30")
    hass.states.async_set(RIGHT, "0")
    entry = MockConfigEntry(
        domain=DOMAIN, data={},
        options={**OPTIONS, "tasks_sensor": "binary_sensor.chores"},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("binary_sensor.chores", "on")
    sensor = NeedsYouSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()

    chores = next((r for r in _rows(sensor) if r["id"] == "tasks_overdue"), None)
    assert chores is not None, [r["id"] for r in _rows(sensor)]

    sensor.suppress("tasks_overdue", hours=8)
    assert not any(r["id"] == "tasks_overdue" for r in _rows(sensor)), (
        "snoozing stopped working for everything, not just salt"
    )
    assert _salt(sensor) is not None


async def test_salt_is_attention_whichever_rule_raised_it(
    hass: HomeAssistant,
) -> None:
    """The level reports the urgency, not the shopping.

    It used to be terracotta when every side was low and ochre when
    only one was -- which made the loudest colour in the house mean
    "the bag is not in the garage". Nobody reads a colour that way.
    A softener running low is an errand: today or tomorrow, which is
    what attention means.

    Both rules are exercised, because a version that simply swapped the
    two would pass a test that only checked one of them.
    """
    # Every side under 40: the trip out to buy a bag.
    row = _salt(await _low(hass))
    assert row["level"] == LEVEL_ATTENTION, row

    # Only one side under 25, the other comfortable: the earlier warning.
    hass.states.async_set(LEFT, "60")
    hass.states.async_set(RIGHT, "20")
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()

    row = _salt(sensor)
    assert row is not None, _rows(sensor)
    assert row["level"] == LEVEL_ATTENTION, row

# --- something was on overnight is NOT a row ---------------------------
#
# It reported a night that had already happened, with no action beyond
# Dismiss -- which is the one thing a Needs-you row may not be: it did
# not need doing. The figures are still published; what left is the
# claim that they were a job.

ENERGY = "sensor.energy_day"


def _publish_night(
    hass: HomeAssistant,
    day: str = "2026-09-19",
    watts: int = 420,
    norm: int = 280,
    excess: float = 50.0,
    label: str = "Sat 19 Sep",
    stale: bool = False,
) -> None:
    """The day sensor's shape, as the integration publishes it."""
    hass.states.async_set(
        ENERGY,
        "3.99",
        {
            "for_day": day,
            "for_date": label,
            "days_late": 7 if stale else 2,
            "stale": stale,
            "baseline_watts": watts,
            "baseline_norm": norm,
            "baseline_excess_pct": excess,
        },
        force_update=True,
    )


async def test_a_night_over_the_usual_floor_raises_nothing(
    hass: HomeAssistant,
) -> None:
    """The loudest version of the case that used to raise a row.

    50% over the usual floor, fresh data, everything the old rule wanted.
    A row here would be the panel asking about a Saturday that is over.
    """
    _publish_night(hass)
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    assert not [r for r in _rows(sensor) if str(r["id"]).startswith("baseline_")], (
        _rows(sensor)
    )


async def test_the_overnight_figures_are_still_published(
    hass: HomeAssistant,
) -> None:
    """Dropping the row must not drop the data behind it.

    The Electricity card reads these off sensor.energy_day and always
    did -- the row was a second copy of them wearing a colour. Asserted
    so that "it is not a job" cannot quietly become "it is not reported".
    """
    _publish_night(hass)
    attrs = hass.states.get(ENERGY).attributes
    assert attrs["baseline_watts"] == 420
    assert attrs["baseline_norm"] == 280
    assert attrs["baseline_excess_pct"] == 50.0
