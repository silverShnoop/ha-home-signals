"""Give a bare Needs you the cards' own sensors, as the platform does."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.home_signals.derived import (
    BatteriesStatusSensor,
    BinsStatusSensor,
    NeedsYouSensor,
    PeopleStatusSensor,
    SoftenerStatusSensor,
    TasksStatusSensor,
)


_IDS = {
    BinsStatusSensor: "sensor.bins_status",
    TasksStatusSensor: "sensor.tasks_status",
    PeopleStatusSensor: "sensor.people_status",
    SoftenerStatusSensor: "sensor.softener_status",
    BatteriesStatusSensor: "sensor.batteries_status",
}


def attach(hass: HomeAssistant, needs: NeedsYouSensor, *extra: Any) -> list[Any]:
    """The owners the platform would create, wired to `needs`.

    `extra` owners (the door, prep, devices) go first, as they need their
    own set-up in the tests that use them.
    """
    entry = needs._entry  # noqa: SLF001
    owners = [
        *extra,
        BinsStatusSensor(entry),
        TasksStatusSensor(entry),
        PeopleStatusSensor(entry),
        SoftenerStatusSensor(entry),
        BatteriesStatusSensor(entry),
    ]
    for i, owner in enumerate(owners):
        owner.hass = hass
        if not owner.entity_id:
            owner.entity_id = _IDS.get(type(owner), f"sensor.owner_{i}")
        owner.add_listener(needs)
    needs.owners = owners
    return owners


def owner(needs: NeedsYouSensor, kind: type) -> Any:
    return next(o for o in needs.owners if isinstance(o, kind))
