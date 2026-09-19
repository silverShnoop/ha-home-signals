"""Setting the integration up for real, through its own config entry.

The unit tests drive the sensor classes directly, which proves the state
machine and nothing about the wiring around it. This proves the wiring:
that the options actually produce the entities, that the service exists and
reaches the right appliance, and that clearing a load through the service
is visible to Needs you.

That last one is the coupling most likely to rot. The pending loads live in
the cycle sensor's memory rather than in an entity Needs you could
subscribe to, and the cycle sensors are created after it — so they call in
to it instead. Nothing in the unit tests would notice if that link were
dropped, and the symptom in the house would be a row that stays on the list
after the button has been pressed.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import DOMAIN, SERVICE_LAUNDRY_HUNG

OPTIONS = {
    "washer_power": "sensor.washer_power",
    "washer_plug": "switch.washer_plug",
    "washer_door": "binary_sensor.washer_door",
    "washer_leak": "binary_sensor.washer_leak",
    "washer_energy": "sensor.washer_energy",
}


DRYER = {
    "dryer_power": "sensor.dryer_power",
    "dryer_plug": "switch.dryer_plug",
    "dryer_door": "binary_sensor.dryer_door",
    "dryer_energy": "sensor.dryer_energy",
}


async def _start(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    hass.states.async_set("switch.dryer_plug", "on")
    hass.states.async_set("binary_sensor.dryer_door", "off")
    hass.states.async_set("sensor.dryer_power", "0")
    hass.states.async_set("sensor.dryer_energy", "0")
    hass.states.async_set("switch.washer_plug", "on")
    hass.states.async_set("binary_sensor.washer_door", "off")
    hass.states.async_set("binary_sensor.washer_leak", "off")
    hass.states.async_set("sensor.washer_power", "0")
    hass.states.async_set("sensor.washer_energy", "0")

    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_configured_washer_produces_its_entities(hass: HomeAssistant) -> None:
    await _start(hass, OPTIONS)

    assert hass.states.get("sensor.washing_machine") is not None, (
        "the cycle sensor did not appear"
    )
    assert hass.states.get("sensor.cleaning_status") is not None
    assert hass.states.get("sensor.washing_machine_button") is not None
    assert hass.services.has_service(DOMAIN, SERVICE_LAUNDRY_HUNG)


async def test_no_power_sensor_means_no_appliance_at_all(hass: HomeAssistant) -> None:
    """An unconfigured machine contributes nothing, rather than an empty one.

    A cleaning light that is permanently green because it is watching
    nothing is worse than no light: it answers the question wrongly.
    """
    await _start(hass, {})

    assert hass.states.get("sensor.washing_machine") is None
    assert hass.states.get("sensor.cleaning_status") is None, (
        "a cleaning light appeared with no appliance behind it"
    )
    assert not hass.services.has_service(DOMAIN, SERVICE_LAUNDRY_HUNG)


async def test_clearing_a_load_reaches_needs_you(hass: HomeAssistant) -> None:
    """The link that no unit test would notice breaking.

    Pending loads live in the cycle sensor's memory, not in an entity Needs
    you can watch, and the cycle sensors are built after it. If the listener
    link is ever dropped the row simply stays on the list after the button
    is pressed, which is exactly the failure a person would blame on the
    button.
    """
    await _start(hass, OPTIONS)

    cycle = next(
        (e for e in hass.data["entity_components"]["sensor"].entities
         if getattr(e, "slug", None) == "washing_machine" and hasattr(e, "hung")),
        None,
    )
    assert cycle is not None, "no washing machine cycle entity was registered"

    # Planted rather than washed: how a load gets into `_pending` is the
    # unit tests' business, and running a 30-minute cycle here would test
    # the clock again instead of the wiring. `_publish` is the real path a
    # finished cycle takes, so the notification is not faked.
    cycle._pending.append({  # noqa: SLF001 - planting a fixture
        "id": "washing_machine_2026-09-19T20:55:00",
        "finished_at": "2026-09-19T20:55:00+00:00",
        "duration_minutes": 118,
        "energy_kwh": 1.12,
    })
    cycle._publish()  # noqa: SLF001
    await hass.async_block_till_done()

    needs = hass.states.get("sensor.needs_you")
    assert needs is not None
    titles = [i["title"] for i in needs.attributes.get("items", [])]
    assert "Laundry needs hanging" in titles, (
        f"a finished load did not reach Needs you: {titles}"
    )

    await hass.services.async_call(
        DOMAIN, SERVICE_LAUNDRY_HUNG, {"appliance": "washing_machine"}, blocking=True
    )
    await hass.async_block_till_done()

    needs = hass.states.get("sensor.needs_you")
    titles = [i["title"] for i in needs.attributes.get("items", [])]
    assert "Laundry needs hanging" not in titles, (
        "the row survived the button being pressed"
    )
    assert hass.states.get("sensor.washing_machine").attributes["pending_count"] == 0


async def test_a_press_reaches_the_activity_feed_as_a_button(
    hass: HomeAssistant,
) -> None:
    """The feed watches entities, and a ZHA button is not one.

    ZHA creates no event entities at all, so the only way a press reaches
    the feed is by being stamped onto a timestamp sensor. By domain and
    device class that sensor says *when* something happened and nothing
    about what, so it declares its own kind — rather than the feed
    guessing from an entity id that ends in `_button`, which works right
    up until somebody renames it.
    """
    await _start(hass, {**OPTIONS, "entities": ["sensor.washing_machine_button"]})

    await hass.services.async_call(DOMAIN, SERVICE_LAUNDRY_HUNG, {}, blocking=True)
    await hass.async_block_till_done()

    feed = hass.states.get("sensor.activity_feed")
    assert feed is not None
    events = feed.attributes.get("events") or []
    mine = [e for e in events if e.get("entity_id") == "sensor.washing_machine_button"]
    assert mine, f"the press never reached the feed: {events}"
    assert mine[0]["kind"] == "button", (
        f"a press was filed as {mine[0]['kind']!r}, not a button"
    )


async def test_a_kind_nobody_recognises_is_not_believed(hass: HomeAssistant) -> None:
    """The frontend draws an icon from the kind, so the set is closed.

    An entity claiming `kind: "explosion"` would render as nothing at all,
    which is worse than being filed as `other`.
    """
    await _start(hass, {**OPTIONS, "entities": ["binary_sensor.washer_door"]})

    hass.states.async_set(
        "binary_sensor.washer_door", "on",
        {"device_class": "door", "kind": "explosion"},
    )
    await hass.async_block_till_done()

    feed = hass.states.get("sensor.activity_feed")
    events = feed.attributes.get("events") or []
    mine = [e for e in events if e.get("entity_id") == "binary_sensor.washer_door"]
    assert mine, "the door never reached the feed"
    assert mine[0]["kind"] == "door", (
        f"an invented kind was believed: {mine[0]['kind']!r}"
    )


async def test_the_press_is_recorded_even_with_nothing_to_clear(
    hass: HomeAssistant,
) -> None:
    """A button that does nothing looks exactly like a flat battery."""
    await _start(hass, OPTIONS)

    before = hass.states.get("sensor.washing_machine_button").state
    await hass.services.async_call(
        DOMAIN, SERVICE_LAUNDRY_HUNG, {}, blocking=True
    )
    await hass.async_block_till_done()

    after = hass.states.get("sensor.washing_machine_button").state
    assert after != before, "a press with nothing waiting was not recorded"


# --- the tumble dryer -------------------------------------------------


def _cycle(hass: HomeAssistant, slug: str):
    return next(
        (e for e in hass.data["entity_components"]["sensor"].entities
         if getattr(e, "slug", None) == slug and hasattr(e, "hung")),
        None,
    )


async def test_a_dryer_gets_no_button_it_does_not_have(hass: HomeAssistant) -> None:
    """The press sensor carries a wall button into the activity feed.

    The dryer has no button, because it has no hang queue for one to
    clear. Creating the entity anyway would leave a timestamp that never
    moves — which on the feed is indistinguishable from a flat battery.
    """
    await _start(hass, {**OPTIONS, **DRYER})

    assert hass.states.get("sensor.tumble_dryer") is not None, "no dryer appeared"
    assert hass.states.get("sensor.washing_machine_button") is not None
    assert hass.states.get("sensor.tumble_dryer_button") is None, (
        "the dryer was given a button sensor with no button behind it"
    )


async def test_a_full_drum_reaches_needs_you_on_either_machine(
    hass: HomeAssistant,
) -> None:
    """The state both machines share, and the row neither had before.

    Planted rather than run: how a drum gets full is the unit tests'
    business, and driving two full cycles here would test the clock twice
    over instead of the wiring.
    """
    await _start(hass, {**OPTIONS, **DRYER})

    for slug in ("washing_machine", "tumble_dryer"):
        cycle = _cycle(hass, slug)
        assert cycle is not None, f"no cycle entity for {slug}"
        cycle._drum_full = True  # noqa: SLF001 - planting a fixture
        cycle._publish()  # noqa: SLF001
    await hass.async_block_till_done()

    items = hass.states.get("sensor.needs_you").attributes.get("items", [])
    titles = [i["title"] for i in items]
    assert "Washing machine needs emptying" in titles, titles
    assert "Tumble dryer needs emptying" in titles, titles


async def test_the_drum_row_offers_no_button(hass: HomeAssistant) -> None:
    """It is cleared by opening the door, which the machine can see.

    A second way to mark it done — from a screen, without touching the
    machine — is how the row and the world drift apart.
    """
    await _start(hass, {**OPTIONS, **DRYER})

    cycle = _cycle(hass, "tumble_dryer")
    cycle._drum_full = True  # noqa: SLF001
    cycle._publish()  # noqa: SLF001
    await hass.async_block_till_done()

    items = hass.states.get("sensor.needs_you").attributes.get("items", [])
    row = next(i for i in items if i["title"] == "Tumble dryer needs emptying")
    assert not row.get("action"), row
    assert not row.get("action_label"), row
    assert "door" in row["detail"], row["detail"]


async def test_opening_the_door_clears_the_row(hass: HomeAssistant) -> None:
    await _start(hass, {**OPTIONS, **DRYER})

    cycle = _cycle(hass, "tumble_dryer")
    cycle._drum_full = True  # noqa: SLF001
    cycle._publish()  # noqa: SLF001
    await hass.async_block_till_done()
    titles = [i["title"] for i
              in hass.states.get("sensor.needs_you").attributes["items"]]
    assert "Tumble dryer needs emptying" in titles

    hass.states.async_set("binary_sensor.dryer_door", "on")
    await hass.async_block_till_done()

    titles = [i["title"] for i
              in hass.states.get("sensor.needs_you").attributes["items"]]
    assert "Tumble dryer needs emptying" not in titles, (
        "the row survived the door being opened"
    )


async def test_the_washer_still_queues_its_hanging(hass: HomeAssistant) -> None:
    """The half the dryer does not have must survive the dryer existing."""
    await _start(hass, {**OPTIONS, **DRYER})

    cycle = _cycle(hass, "washing_machine")
    cycle._pending.append({  # noqa: SLF001
        "id": "washing_machine_2026-09-19T20:55:00",
        "finished_at": "2026-09-19T20:55:00+00:00",
        "duration_minutes": 118,
        "energy_kwh": 1.12,
    })
    cycle._publish()  # noqa: SLF001
    await hass.async_block_till_done()

    items = hass.states.get("sensor.needs_you").attributes.get("items", [])
    row = next((i for i in items if i["title"] == "Laundry needs hanging"), None)
    assert row is not None, [i["title"] for i in items]
    assert row["action_label"] == "Hung"
