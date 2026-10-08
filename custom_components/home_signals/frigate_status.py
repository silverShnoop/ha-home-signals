"""What a Frigate camera is asking a person to do: `sensor.camera_status`.

A camera card states facts -- what it can see, what it saw, what Frigate
made of it. Two things it sees are jobs, and a job is a Needs you row:

- **A parcel left in view** (`attention`). Frigate keeps tracking an object
  that has stopped moving, so its `package` occupancy stays on for as long
  as the parcel is there. The row is that occupancy and nothing else: it
  appears when a parcel is seen and clears itself when it is gone. There
  is no Done, because pressing a button does not bring a parcel in.
- **A camera that has stopped** (`waiting`). Frigate reports the frames it
  is receiving from each camera. None for five minutes is footage that is
  not being recorded until somebody looks at the camera, which is exactly
  the promise `waiting` makes. A blip shorter than that is a camera
  rebooting itself, and not news.

The cameras are found rather than configured: every camera the Frigate
integration has made, from its entity registry. A camera added to Frigate
is picked up on the next scan without anyone coming back to the options.

The card owns its level, so each camera's level and jobs are published
under `cameras`, keyed by Frigate's own name for the camera. A gate card
reads its own entry and does not go yellow for a parcel at the back door.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import ATTR_HOURS, ATTR_ITEM_ID, DOMAIN, LEVEL_ATTENTION, LEVEL_WAITING, SERVICE_SNOOZE
from .derived import _Owner, loudest

FRIGATE = "frigate"

# How long a camera may send nothing before it is a job. A Reolink or a
# Dahua rebooting itself is gone for a minute or two; five is a camera that
# is not coming back on its own.
STOPPED_AFTER = timedelta(minutes=5)

_SILENT = {STATE_UNKNOWN, STATE_UNAVAILABLE, None}


def frigate_cameras(registry: er.EntityRegistry) -> list[tuple[str, str, str]]:
    """(config entry, Frigate's camera name, camera entity) for each camera.

    Read off the camera's unique id, `<entry>:camera:<name>`, rather than its
    state. The state's `camera_name` attribute is gone whenever the camera
    is unavailable, and an unavailable camera is one of the two things this
    sensor exists to notice.
    """
    found = []
    for entry in registry.entities.values():
        if entry.platform != FRIGATE or entry.domain != "camera":
            continue
        parts = str(entry.unique_id).split(":", 2)
        if len(parts) == 3 and parts[1] == "camera" and parts[2]:
            found.append((parts[0], parts[2], entry.entity_id))
    return sorted(found, key=lambda c: c[1])


def _entity(registry: er.EntityRegistry, domain: str, unique_id: str) -> str | None:
    return registry.async_get_entity_id(domain, FRIGATE, unique_id)


def _clock(when: datetime) -> str:
    return dt_util.as_local(when).strftime("%H:%M")


def _snooze(item_id: str, hours: int) -> dict[str, Any]:
    return {"service": f"{DOMAIN}.{SERVICE_SNOOZE}",
            "data": {ATTR_ITEM_ID: item_id, ATTR_HOURS: hours}}


class CameraStatusSensor(_Owner):
    """The camera cards' sensor: parcels waiting, cameras that stopped."""

    tab = "security"
    _attr_name = "Camera status"
    _attr_icon = "mdi:cctv"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_camera_status"
        self._by_camera: dict[str, dict[str, Any]] = {}
        self._tracked: list[str] = []
        self._untrack: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._retrack()
        # Frigate's entities arrive after this one on a restart, and a new
        # camera arrives whenever Frigate is told about it. Each is watched
        # the moment it is registered, so its first parcel is not left
        # waiting for the five-minute scan.
        self.async_on_remove(self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed))
        self.async_on_remove(async_at_started(self.hass, self._started))
        self.async_on_remove(self._stop_tracking)

    @callback
    def _started(self, _hass: HomeAssistant) -> None:
        self._retrack()

    @callback
    def _registry_changed(self, _event: Event) -> None:
        self._retrack()
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _stop_tracking(self) -> None:
        if self._untrack:
            self._untrack()
            self._untrack = None

    @callback
    def _retrack(self) -> None:
        wanted = self._frigate_entities()
        if wanted == self._tracked:
            return
        self._stop_tracking()
        self._tracked = wanted
        if wanted:
            self._untrack = async_track_state_change_event(self.hass, wanted, self._async_changed)

    def _watched(self) -> list[str]:
        """Nothing for the shared plumbing: this sensor keeps its own list."""
        return []

    def _frigate_entities(self) -> list[str]:
        """The parcel and frame-rate sensors, so a parcel shows at once."""
        if self.hass is None:
            return []
        registry = er.async_get(self.hass)
        watched = []
        for entry_id, cam, _camera in frigate_cameras(registry):
            for domain, uid in (("binary_sensor", f"{entry_id}:occupancy_sensor:{cam}_package"),
                                ("sensor", f"{entry_id}:sensor_fps:{cam}_camera")):
                if entity_id := _entity(registry, domain, uid):
                    watched.append(entity_id)
        return watched

    def _name(self, cam: str, camera_entity: str) -> str:
        """The camera as the house names it: its device, else Frigate's name."""
        registry = er.async_get(self.hass)
        entry = registry.async_get(camera_entity)
        if entry and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            if device and (device.name_by_user or device.name):
                return str(device.name_by_user or device.name)
        return cam.replace("_", " ").title()

    def _camera_jobs(self, entry_id: str, cam: str, camera_entity: str) -> list[dict[str, Any]]:
        registry = er.async_get(self.hass)
        name = self._name(cam, camera_entity)
        jobs: list[dict[str, Any]] = []

        fps_id = _entity(registry, "sensor", f"{entry_id}:sensor_fps:{cam}_camera")
        fps = self.hass.states.get(fps_id) if fps_id else None
        if fps is not None and self._stopped(fps):
            item_id = f"camera_stopped_{cam}"
            jobs.append({
                "id": item_id,
                "title": f"{name} camera has stopped",
                "detail": f"No picture since {_clock(fps.last_changed)}",
                "since": fps.last_changed.isoformat(),
                "icon": "mdi:cctv-off",
                "level": LEVEL_WAITING,
                "tab": self.tab,
                "card": "camera",
                "camera": cam,
                # A snooze rather than a Done: the row clears itself the
                # moment frames arrive again, and pressing a button here
                # restarts nothing.
                "action_label": "Snooze",
                "action": _snooze(item_id, 4),
                "snooze_only": True,
            })
            # A camera sending nothing sees no parcels either, and its
            # occupancy sensors hold whatever they last said. A parcel row
            # beside a dead camera would be a guess dressed as a sighting.
            return jobs

        parcel_id = _entity(registry, "binary_sensor", f"{entry_id}:occupancy_sensor:{cam}_package")
        parcel = self.hass.states.get(parcel_id) if parcel_id else None
        if parcel is not None and parcel.state == "on":
            item_id = f"parcel_{cam}"
            jobs.append({
                "id": item_id,
                "title": "Bring the parcel in",
                "detail": f"Seen by the {name} camera since {_clock(parcel.last_changed)}",
                "since": parcel.last_changed.isoformat(),
                "icon": "mdi:package-variant-closed",
                "level": LEVEL_ATTENTION,
                "tab": self.tab,
                "card": "camera",
                "camera": cam,
                # Snooze only. The parcel being gone is what clears it; a
                # Done that hid the row while the parcel sat in the rain
                # would be the card and the row disagreeing.
                "action_label": "Snooze",
                "action": _snooze(item_id, 2),
                "snooze_only": True,
            })
        return jobs

    def _stopped(self, fps: State) -> bool:
        """No frames, unbroken, for longer than a reboot takes."""
        silent = fps.state in _SILENT
        if not silent:
            try:
                silent = float(fps.state) <= 0
            except (TypeError, ValueError):
                silent = True
        return silent and dt_util.utcnow() - fps.last_changed >= STOPPED_AFTER

    def _jobs_now(self) -> list[dict[str, Any]]:
        if self.hass is None:
            return []
        registry = er.async_get(self.hass)
        jobs: list[dict[str, Any]] = []
        by_camera: dict[str, dict[str, Any]] = {}
        for entry_id, cam, camera_entity in frigate_cameras(registry):
            own = self._camera_jobs(entry_id, cam, camera_entity)
            by_camera[cam] = {
                "name": self._name(cam, camera_entity),
                "entity": camera_entity,
                "level": loudest(job.get("level") for job in own),
                "jobs": own,
            }
            jobs.extend(own)
        self._by_camera = by_camera
        return jobs

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["cameras"] = self._by_camera
        return attrs
