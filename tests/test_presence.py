"""A person nobody can locate, and why that is a job at all.

The card draws "Unknown" in the warning colour, and on this panel
yellow is a promise that something wants doing -- the job itself living
in `Needs you`, never on the card. So either the row exists or the
colour is a lie. This is the row.

"Unknown" is not "away". Away is a reading, and a good one. Unknown is
the absence of any reading: no tracker of theirs is reporting, so every
presence automation in the house is now guessing.

The two ways this goes wrong are opposite and both are tested: firing
for a phone that was in a tunnel for five minutes, and never firing at
all because the grace period restarts every time Home Assistant does.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.home_signals.const import (
    ACCENT_INFO,
    DOMAIN,
    LEVEL_ATTENTION,
    LEVEL_CRITICAL,
    LEVEL_WAITING,
)
from custom_components.home_signals.derived import NeedsYouSensor

JAINA = "person.jaina"


_WATCHING = {"people": [JAINA], "presence_grace_minutes": 60}


def _sensor(hass: HomeAssistant, options: dict | None = None) -> NeedsYouSensor:
    # `options or _WATCHING` would be wrong: {} is falsy, and {} is
    # exactly the case "watching nobody" needs to pass in.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options=_WATCHING if options is None else options,
    )
    entry.add_to_hass(hass)
    sensor = NeedsYouSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.needs_you"
    return sensor


def _titles(sensor: NeedsYouSensor) -> list[str]:
    return [row["title"] for row in sensor.extra_state_attributes["items"]]


async def _dark_for(hass: HomeAssistant, minutes: float) -> State:
    """Put Jaina's tracker into silence, starting `minutes` ago."""
    hass.states.async_set(JAINA, "unknown", {"friendly_name": "Jaina"})
    await hass.async_block_till_done()
    state = hass.states.get(JAINA)
    return State(
        JAINA, "unknown", {"friendly_name": "Jaina"},
        last_changed=dt_util.utcnow() - timedelta(minutes=minutes),
        last_updated=state.last_updated,
    )


async def test_a_phone_in_a_tunnel_is_not_a_job(hass: HomeAssistant) -> None:
    """Five minutes of silence is a tunnel, a reboot, or a lift."""
    hass.states.async_set(JAINA, "unknown", {"friendly_name": "Jaina"})
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()

    assert _titles(sensor) == [], (
        "a phone quiet for seconds already counted as a job"
    )


async def test_but_an_hour_of_silence_is(hass: HomeAssistant) -> None:
    hass.states.async_set(JAINA, "unknown", {"friendly_name": "Jaina"})
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    # Backdate the moment we first saw them go quiet, which is what the
    # sensor measures from -- see the restart test for why it is not
    # read off the entity.
    sensor._dark_since[JAINA] = dt_util.utcnow() - timedelta(minutes=61)  # noqa: SLF001
    sensor._recompute()  # noqa: SLF001

    assert _titles(sensor) == ["Jaina cannot be located"], _titles(sensor)


async def test_away_is_a_reading_and_not_a_job(hass: HomeAssistant) -> None:
    """The distinction the whole thing rests on."""
    hass.states.async_set(JAINA, "not_home", {"friendly_name": "Jaina"})
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    sensor._dark_since[JAINA] = dt_util.utcnow() - timedelta(hours=9)  # noqa: SLF001
    sensor._recompute()  # noqa: SLF001

    assert _titles(sensor) == [], "being out was treated as being untrackable"


async def test_coming_back_forgets_that_they_were_ever_dark(
    hass: HomeAssistant,
) -> None:
    """Any positive reading is the tracker working again."""
    hass.states.async_set(JAINA, "unknown", {"friendly_name": "Jaina"})
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    sensor._dark_since[JAINA] = dt_util.utcnow() - timedelta(hours=4)  # noqa: SLF001
    sensor._recompute()  # noqa: SLF001
    assert _titles(sensor) == ["Jaina cannot be located"]

    hass.states.async_set(JAINA, "home", {"friendly_name": "Jaina"})
    await hass.async_block_till_done()
    sensor._recompute()  # noqa: SLF001

    assert _titles(sensor) == [], "the row survived the tracker coming back"
    assert JAINA not in sensor._dark_since, (  # noqa: SLF001
        "the memory was kept, so the next blip would fire immediately"
    )


async def test_a_restart_does_not_restart_the_grace_period(
    hass: HomeAssistant,
) -> None:
    """The trap this is built around.

    A person's `last_changed` is reset when Home Assistant comes back,
    so a grace period measured from it starts again at every reboot --
    and on a box that restarts twice a day a tracker quiet since
    breakfast would never once get past it. Which is exactly the case
    the row exists for.
    """
    quiet_since = dt_util.utcnow() - timedelta(hours=6)
    mock_restore_cache(
        hass,
        (
            State(
                "sensor.needs_you",
                "0",
                {"items": [], "dark_since": {JAINA: quiet_since.isoformat()}},
            ),
        ),
    )
    # Home Assistant has just come back: the entity is brand new and its
    # last_changed is NOW, which is the lie.
    hass.states.async_set(JAINA, "unknown", {"friendly_name": "Jaina"})
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()

    assert _titles(sensor) == ["Jaina cannot be located"], (
        "the restart reset the grace period, so six hours of silence "
        "read as none"
    )


async def test_watching_nobody_costs_nothing(hass: HomeAssistant) -> None:
    hass.states.async_set(JAINA, "unknown", {"friendly_name": "Jaina"})
    sensor = _sensor(hass, options={})
    await sensor.async_added_to_hass()
    sensor._dark_since[JAINA] = dt_util.utcnow() - timedelta(hours=9)  # noqa: SLF001
    sensor._recompute()  # noqa: SLF001

    assert _titles(sensor) == []


async def test_the_row_can_be_snoozed(hass: HomeAssistant) -> None:
    """A tracker you already know about should not nag all evening."""
    hass.states.async_set(JAINA, "unknown", {"friendly_name": "Jaina"})
    sensor = _sensor(hass)
    await sensor.async_added_to_hass()
    sensor._dark_since[JAINA] = dt_util.utcnow() - timedelta(hours=4)  # noqa: SLF001
    sensor._recompute()  # noqa: SLF001
    row = sensor.extra_state_attributes["items"][0]
    assert row["action_label"] == "Snooze", row

    sensor.suppress(row["id"], hours=12)
    assert _titles(sensor) == [], "snoozing did not clear the row"


# --- The tile wears what is actually there ---------------------------
#
# Same rule from the other end. The Maintenance tile was hardcoded
# ochre, so it was yellow on a morning with nothing wrong -- promising
# a job that did not exist. It now takes its colour from the worst
# thing System health is carrying.

from custom_components.home_signals.derived import SystemHealthSensor  # noqa: E402


def _health(hass: HomeAssistant, rows: list[dict]) -> SystemHealthSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    sensor = SystemHealthSensor(entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.system_health"
    sensor._items = rows  # noqa: SLF001
    return sensor


async def test_nothing_wrong_is_not_a_colour(hass: HomeAssistant) -> None:
    assert _health(hass, []).extra_state_attributes["level"] is None, (
        "the tile would be coloured on a morning with nothing wrong"
    )


async def test_information_alone_is_not_a_level(hass: HomeAssistant) -> None:
    """Seven pending updates is worth knowing and is not a job.

    It carries a decorative accent and no level at all, so the tile has
    nothing to wear -- which is the point. A row that needs no doing
    must not be able to colour a tab.
    """
    sensor = _health(hass, [{"id": "updates", "accent": ACCENT_INFO}])
    assert sensor.extra_state_attributes["level"] is None, (
        "information coloured the tile, so it was ranked rather than skipped"
    )


async def test_the_worst_thing_wins_not_the_last_one(
    hass: HomeAssistant,
) -> None:
    sensor = _health(hass, [
        {"id": "leak", "level": LEVEL_CRITICAL},
        {"id": "updates", "accent": ACCENT_INFO},
        {"id": "batteries", "level": LEVEL_ATTENTION},
    ])
    assert sensor.extra_state_attributes["level"] == LEVEL_CRITICAL, (
        "a critical row was drowned out by what was listed after it"
    )


async def test_loudness_is_the_meaning_not_the_name(
    hass: HomeAssistant,
) -> None:
    """The trap the numbers used to set, in its new clothes.

    Ordered any incidental way -- alphabetically, say -- "attention"
    comes first and would outrank "waiting". The order has to come from
    what the levels mean.
    """
    sensor = _health(hass, [
        {"id": "batteries", "level": LEVEL_ATTENTION},
        {"id": "unpowered", "level": LEVEL_WAITING},
    ])
    assert sensor.extra_state_attributes["level"] == LEVEL_WAITING, (
        "attention outranked waiting, so something other than the "
        "meaning was being compared"
    )
