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


class Tab:
    """What a tab's rail button reads: its level and its words.

    Stands where the old per-tab status sensors stood in the tests, so the
    assertions about what the Cleaning tab says carry straight over.
    """

    def __init__(self, hass: HomeAssistant, tab: str = "cleaning") -> None:
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        from custom_components.home_signals.const import DOMAIN

        entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
        entry.add_to_hass(hass)
        self.needs = NeedsYouSensor(entry)
        self.needs.hass = hass
        self.needs.entity_id = "sensor.needs_you"
        attach(hass, self.needs)
        self.tab = tab

    def _attrs(self) -> dict[str, Any]:
        self.needs._recompute()  # noqa: SLF001
        return self.needs.extra_state_attributes

    @property
    def native_value(self) -> str:
        return self._attrs()[f"tab_{self.tab}"] or "clear"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._attrs()
        return {
            "level": attrs[f"tab_{self.tab}"],
            "detail": attrs[f"summary_{self.tab}"],
        }
