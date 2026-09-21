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

from custom_components.home_signals.const import DOMAIN
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


# --- something was on overnight ---------------------------------------
#
# The row exists because the floor is the one figure no tariff change
# touches and no price chart would ever have shown. It is also LATE -- one
# or two days, without a live meter -- and the tests below are mostly about
# that: the row has to name the night it is about rather than implying last
# night, and it must not fire on a day so old it is no longer news.

ENERGY = "sensor.energy_day"


def _publish_night(
    hass: HomeAssistant,
    *,
    watts: int = 420,
    norm: int = 280,
    excess: int = 50,
    day: str = "2026-09-19",
    label: str = "Sat 19 Sep",
    stale: bool = False,
) -> None:
    """The day sensor's shape, as `_energy_entity` looks for it."""
    hass.states.async_set(
        ENERGY,
        "3.99",
        {
            "for_day": day,
            "for_date": label,
            "stale": stale,
            "baseline_watts": watts,
            "baseline_norm": norm,
            "baseline_excess_pct": excess,
        },
        force_update=True,
    )


async def _energy(hass: HomeAssistant, **kwargs) -> NeedsYouSensor:
    _publish_night(hass, **kwargs)
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    return sensor


def _night_row(sensor: NeedsYouSensor) -> dict | None:
    return next(
        (r for r in _rows(sensor) if str(r["id"]).startswith("baseline_")), None
    )


async def test_a_night_well_over_the_usual_floor_is_a_row(
    hass: HomeAssistant,
) -> None:
    row = _night_row(await _energy(hass))
    assert row is not None, [r["id"] for r in _rows(await _energy(hass))]
    assert "overnight" in row["title"].lower()


async def test_the_row_names_the_night_because_it_is_late(
    hass: HomeAssistant,
) -> None:
    """The lag is shown, not hidden.

    Without a live meter this arrives one or two days behind. "Something is
    on now" is a claim the data cannot support; "something was on, on
    Saturday" is one it can, and the difference is the whole reason the
    detail carries the date.
    """
    row = _night_row(await _energy(hass))
    assert "Sat 19 Sep" in row["detail"]
    assert "420" in row["detail"] and "280" in row["detail"]


async def test_an_ordinary_night_is_not_a_row(hass: HomeAssistant) -> None:
    row = _night_row(await _energy(hass, watts=290, excess=3))
    assert row is None


async def test_a_night_under_the_threshold_is_not_a_row(
    hass: HomeAssistant,
) -> None:
    """20% over is a house having an evening, not a job.

    The threshold is what keeps the row rare, and a row that is not rare is
    one nobody reads.
    """
    assert _night_row(await _energy(hass, watts=336, excess=20)) is None
    assert _night_row(await _energy(hass, watts=420, excess=50)) is not None


async def test_a_stale_day_is_not_news_about_last_night(
    hass: HomeAssistant,
) -> None:
    """Octopus stopped delivering. A week-old night is not a job.

    The sensor already drops its own state when the data goes stale; the row
    has to drop for the same reason, or the panel spends a week asking about
    one Saturday.
    """
    assert _night_row(await _energy(hass, stale=True)) is None


async def test_no_usual_yet_means_no_row(hass: HomeAssistant) -> None:
    """Before there are enough nights the sensor publishes no excess.

    A row on the strength of a norm that does not exist yet would fire on
    the house's first week and teach somebody to ignore it.
    """
    hass.states.async_set(
        ENERGY,
        "3.99",
        {
            "for_day": "2026-09-19",
            "for_date": "Sat 19 Sep",
            "stale": False,
            "baseline_watts": 420,
            "baseline_norm": None,
            "baseline_excess_pct": None,
        },
        force_update=True,
    )
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    assert _night_row(sensor) is None


async def test_answering_for_one_night_does_not_silence_the_next(
    hass: HomeAssistant,
) -> None:
    """"I know what that was" is a real answer, for that night only.

    Guests, a wash left running, the oven on late. The occurrence key is
    what makes the answer specific -- the same rule the bins row follows.
    """
    sensor = await _energy(hass)
    row = _night_row(sensor)
    assert row["action_label"] == "Dismiss"

    sensor.suppress(row["id"])
    assert _night_row(sensor) is None, "the dismissal did not take"

    _publish_night(hass, day="2026-09-20", label="Sun 20 Sep")
    await hass.async_block_till_done()
    sensor._recompute()  # noqa: SLF001

    again = _night_row(sensor)
    assert again is not None, "answering for Saturday silenced Sunday too"
    assert again["id"] == "baseline_2026-09-20"
