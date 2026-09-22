"""What every room is actually like, from the system that controls the heating.

One source, deliberately. This house has a second thermometer in three of its
ten rooms, and the two disagree by 3.7° in the kitchen and 1.8° in the
toilet — same sign, different size, which is not an offset anything can
correct for generically. Averaging two sensors with unequal and unknown
biases produces a number that cannot be traced back to either of them, and
the room would then be reported as a temperature no device in it has ever
read.

So the reading is the zone's own, because that is the one the loop acts on.
A room is at its target when the thing holding the valve open thinks it is;
any other thermometer is describing a room the heating is not listening to.

**Facts only.** Nothing in here carries a level. A room being cold, a window
being open, a zone sitting on a manual override — those are things that are
true, and a card states them. The moment one of them becomes something a
person has to *do*, it is a `Needs you` row and it is built in `derived.py`,
where every other job in the house is built. That split is why this module
has no notion of `attention`, `waiting` or `critical` at all.

Named `rooms` rather than `climate` on purpose. A module named for a
Home Assistant platform is a module Home Assistant may one day decide to
load as one, and this is not a platform — it is a sensor that reads the
climate entities somebody else provides.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_CLIMATE_SCALE_MAX,
    CONF_CLIMATE_SCALE_MIN,
    CONF_CLIMATE_ZONES,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMP,
    DEFAULT_CLIMATE_SCALE_MAX,
    DEFAULT_CLIMATE_SCALE_MIN,
    VENTILATION_BAND,
)

LOGGER = logging.getLogger(__name__)

_NOT_A_READING = {STATE_UNKNOWN, STATE_UNAVAILABLE, None}

# Rooms move slowly. Five minutes matches the rest of the integration and is
# far faster than a radiator, and every reading here also arrives as a state
# change — the timer is only there so nothing sits stale when Tado's cloud
# goes quiet for a while.
SCAN_INTERVAL = timedelta(minutes=5)

# Magnus-Tetens. The coefficients are the ones for water above freezing; the
# formula is quoted everywhere with three or four different sets and they
# disagree in the second decimal, which is well under what a room sensor can
# claim anyway.
_MAGNUS_A = 17.62
_MAGNUS_B = 243.12


def _as_float(value: Any) -> float | None:
    """A number, or None for anything that is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _reading(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A numeric state, or None when there is genuinely no reading."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _NOT_A_READING:
        return None
    return _as_float(state.state)


def dew_point_c(temp_c: float | None, humidity: float | None) -> float | None:
    """The temperature at which this air starts leaving water on things.

    The number behind every damp problem in a house: a wall colder than this
    grows mould, and it is the only way to compare a warm humid room with a
    cold one — 60% at 21° and 60% at 15° are not the same air.

    None outside the range the formula is honest over, rather than a number
    that looks like an answer.
    """
    if temp_c is None or humidity is None:
        return None
    if not 0 < humidity <= 100:
        return None
    gamma = math.log(humidity / 100) + (_MAGNUS_A * temp_c) / (_MAGNUS_B + temp_c)
    if gamma >= _MAGNUS_A:
        return None
    return round((_MAGNUS_B * gamma) / (_MAGNUS_A - gamma), 1)


def absolute_humidity(temp_c: float | None, humidity: float | None) -> float | None:
    """Grams of water per cubic metre.

    Relative humidity cannot be compared between two places at different
    temperatures, and that is exactly the comparison worth making: 70%
    outside at 8° is drier air than 55% inside at 21°, so opening the window
    dries the house out. Read the two RH figures alone and you would conclude
    the opposite.
    """
    if temp_c is None or humidity is None:
        return None
    if not 0 <= humidity <= 100:
        return None
    saturation = 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))
    return round((saturation * humidity * 2.1674) / (273.15 + temp_c), 2)


def zone_companions(hass: HomeAssistant, zone: str) -> dict[str, str | None]:
    """The entities sitting on the same device as a heating zone.

    Read off the registry rather than assembled from the zone's entity id.
    `sensor.<zone>_heating` is true of this house today and is not a promise
    anything made: an entity renamed in the UI keeps its unique id and loses
    its suffix, and a room whose id was taken by a bulb never had the suffix
    to begin with.

    Each is identified by what it *is*, not by what it is called — a name is
    translated, and "Overlay" is a word Tado chose in one language:

    - the window contact is the binary sensor with the `window` device class
    - the override flag is the binary sensor whose unique id is the zone's
      overlay — Tado's own key, which is not translated
    - heating power is the only percentage on the device that is not a
      humidity or a battery
    """
    found: dict[str, str | None] = {"window": None, "overlay": None, "heating": None}
    registry = er.async_get(hass)
    entry = registry.async_get(zone)
    if entry is None or entry.device_id is None:
        return found

    device = dr.async_get(hass).async_get(entry.device_id)
    if device is None:
        return found

    for other in er.async_entries_for_device(registry, device.id):
        if other.entity_id == zone:
            continue
        unique = str(other.unique_id or "")
        if other.domain == "binary_sensor":
            if other.original_device_class == "window":
                found["window"] = other.entity_id
            elif unique.startswith("overlay"):
                found["overlay"] = other.entity_id
            continue
        if other.domain != "sensor" or found["heating"] is not None:
            continue
        if other.original_device_class in ("humidity", "battery", "temperature"):
            continue
        state = hass.states.get(other.entity_id)
        if state is not None and state.attributes.get("unit_of_measurement") == "%":
            found["heating"] = other.entity_id
    return found


def _since(hass: HomeAssistant, entity_id: str | None) -> datetime | None:
    """When a flag last became what it is, or None if it is not set."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state != STATE_ON:
        return None
    return state.last_changed


def read_zone(hass: HomeAssistant, zone: str) -> dict[str, Any] | None:
    """Everything known about one heating zone, or None if it cannot be read.

    None rather than a row of blanks: a zone whose integration is down has
    not told us the room is cold, and a card drawing it at 0° beside the
    others would be stating something nobody measured.
    """
    state = hass.states.get(zone)
    if state is None or state.state in _NOT_A_READING:
        return None

    companions = zone_companions(hass, zone)
    temperature = _as_float(state.attributes.get("current_temperature"))
    humidity = _as_float(state.attributes.get("current_humidity"))
    target = _as_float(state.attributes.get("temperature"))
    heating = _reading(hass, companions["heating"])
    window_since = _since(hass, companions["window"])
    manual_since = _since(hass, companions["overlay"])

    # A zone that is off has a target of 5 — Tado's frost setting, not a
    # temperature anybody asked for. Reporting it as one would put every off
    # room at the bottom of a comparison as if somebody wanted it there.
    off = state.state == "off"

    return {
        "entity_id": zone,
        "name": state.attributes.get("friendly_name", zone),
        "temperature": temperature,
        "humidity": humidity,
        "target": None if off else target,
        "dew_point": dew_point_c(temperature, humidity),
        "absolute_humidity": absolute_humidity(temperature, humidity),
        "heating": heating,
        # Two ways of knowing the same thing, and neither is reliable alone.
        # A wall thermostat reports no percentage at all, and a valve that
        # has just been asked for heat reports one before `hvac_action`
        # catches up.
        "calling": bool(heating) or state.attributes.get("hvac_action") == "heating",
        "off": off,
        "manual": manual_since is not None,
        "manual_since": manual_since,
        "window_open": window_since is not None,
        "window_since": window_since,
        "window_entity": companions["window"],
        "heating_entity": companions["heating"],
        "overlay_entity": companions["overlay"],
    }


def read_zones(hass: HomeAssistant, zones: list[str]) -> list[dict[str, Any]]:
    """Every configured zone that can be read, in configured order."""
    out = []
    for zone in zones:
        if (reading := read_zone(hass, zone)) is not None:
            out.append(reading)
    return out


def _bar_pct(temperature: float | None, low: float, high: float) -> int | None:
    """Where this room sits on the scale every other room is drawn against."""
    if temperature is None or high <= low:
        return None
    return max(0, min(100, round(((temperature - low) / (high - low)) * 100)))


def _value_text(reading: dict[str, Any]) -> str:
    """`21.0° · 52%`, and the half of it that exists when the other does not."""
    parts = []
    if reading["temperature"] is not None:
        parts.append(f"{reading['temperature']:.1f}°")
    if reading["humidity"] is not None:
        parts.append(f"{reading['humidity']:.0f}%")
    return " · ".join(parts)


def _sub_text(reading: dict[str, Any]) -> str:
    """What the zone is doing, under the name.

    The open window is in here as a **fact**. A window is open for perfectly
    good reasons half the summer, and a room that colours itself for one is a
    room that cries wolf. It becomes a job only when the heating is running
    into it, and that job is a Needs-you row, not a word on this card.
    """
    parts = []
    if reading["off"]:
        parts.append("Off")
    elif reading["target"] is not None:
        prefix = "Manual" if reading["manual"] else "Target"
        parts.append(f"{prefix} {reading['target']:.1f}°")
    elif reading["manual"]:
        parts.append("Manual")

    heating = reading["heating"]
    if heating:
        parts.append(f"heating {heating:.0f}%")
    elif reading["calling"]:
        parts.append("heating")

    if reading["window_open"]:
        parts.append("window open")
    return " · ".join(parts)


def zone_rows(
    readings: list[dict[str, Any]], low: float, high: float
) -> list[dict[str, Any]]:
    """The rooms as a Spectra `list` renders them, coldest against target first.

    Sorted by how far the room is from what was asked of it rather than by
    temperature, because that is the question a comparison is being read to
    answer: a 17° hall nobody heats is not a problem and a 17° study asked
    for 21° is. Rooms that are off have nothing to be short of, so they sort
    after the rest by their own temperature.
    """

    def order(reading: dict[str, Any]) -> tuple[int, float]:
        temperature = reading["temperature"]
        target = reading["target"]
        if temperature is None:
            return (2, 0.0)
        if target is None:
            return (1, temperature)
        return (0, temperature - target)

    rows = []
    for reading in sorted(readings, key=order):
        row = {
            "id": reading["entity_id"],
            "name": reading["name"],
            "sub": _sub_text(reading),
            "value": _value_text(reading),
            "temperature": reading["temperature"],
            "humidity": reading["humidity"],
            "target": reading["target"],
            "dew_point": reading["dew_point"],
            "heating": reading["heating"],
            "window_open": reading["window_open"],
            "manual": reading["manual"],
        }
        if (pct := _bar_pct(reading["temperature"], low, high)) is not None:
            row["bar"] = {"pct": pct}
        rows.append(row)
    return rows


def ventilation(
    indoor: list[dict[str, Any]], outdoor_g: float | None
) -> dict[str, Any] | None:
    """Would opening a window dry the house out, or wet it?

    Asked of the dampest room, because that is the window somebody would
    actually open — and answered in absolute humidity, which is the only way
    the two sides of a window can be compared at all.

    Absent rather than guessed when there is nothing outside to compare
    against. A ventilation answer with no outdoor reading would be a
    sentence about a measurement that was never taken.
    """
    if outdoor_g is None:
        return None
    candidates = [r for r in indoor if r["absolute_humidity"] is not None]
    if not candidates:
        return None
    dampest = max(candidates, key=lambda r: r["absolute_humidity"])
    indoor_g = dampest["absolute_humidity"]
    difference = round(indoor_g - outdoor_g, 2)

    if abs(difference) < VENTILATION_BAND:
        direction = "no odds"
        text = f"Airing {dampest['name']} would make no odds"
    elif difference > 0:
        direction = "drier"
        text = f"Airing {dampest['name']} would dry it out"
    else:
        direction = "damper"
        text = f"Airing {dampest['name']} would make it damper"

    return {
        "direction": direction,
        "text": text,
        "room": dampest["name"],
        "indoor": indoor_g,
        "outdoor": outdoor_g,
        "difference": difference,
    }


class HouseClimateSensor(SensorEntity):
    """Every room at once, for the one card that compares them.

    The per-room climate cards are controls: one room, its dial, its
    schedule. Comparing six of them means holding six numbers in your head.
    This is the other question — which room is the odd one — and it is a
    different card because it is a different question.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "House climate"
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_native_unit_of_measurement = "rooms"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_house_climate"
        self._rows: list[dict[str, Any]] = []
        self._extra: dict[str, Any] = {}

    def _option(self, key: str, default: Any) -> Any:
        return self._entry.options.get(key, self._entry.data.get(key, default))

    def _zones(self) -> list[str]:
        return list(self._option(CONF_CLIMATE_ZONES, []) or [])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, SCAN_INTERVAL)
        )
        # The zones themselves, plus every companion they turned out to
        # have. Subscribing to the zone alone would leave the window and the
        # heating percentage waiting up to five minutes to appear, and an
        # open window is the one thing on this card somebody might be
        # standing in front of.
        watched = set(self._zones())
        for zone in self._zones():
            watched.update(
                entity_id
                for entity_id in zone_companions(self.hass, zone).values()
                if entity_id
            )
        for entity_id in (
            self._option(CONF_OUTDOOR_TEMP, None),
            self._option(CONF_OUTDOOR_HUMIDITY, None),
        ):
            if entity_id:
                watched.add(entity_id)
        if watched:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, sorted(watched), self._async_changed
                )
            )
        # The companions are resolved once, above, against a registry that
        # is still filling up during startup. Recomputing once Home
        # Assistant has finished starting is what picks up a zone whose
        # device had not been written yet.
        self.async_on_remove(async_at_started(self.hass, self._async_started))
        self._recompute()

    @callback
    def _async_started(self, _hass: HomeAssistant) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _async_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self._recompute()
        self.async_write_ha_state()

    def _recompute(self) -> None:
        low = float(self._option(CONF_CLIMATE_SCALE_MIN, DEFAULT_CLIMATE_SCALE_MIN))
        high = float(self._option(CONF_CLIMATE_SCALE_MAX, DEFAULT_CLIMATE_SCALE_MAX))
        readings = read_zones(self.hass, self._zones())
        self._rows = zone_rows(readings, low, high)

        temperatures = [
            r["temperature"] for r in readings if r["temperature"] is not None
        ]
        outdoor_temp = _reading(self.hass, self._option(CONF_OUTDOOR_TEMP, None))
        outdoor_humidity = _reading(
            self.hass, self._option(CONF_OUTDOOR_HUMIDITY, None)
        )
        outdoor_g = absolute_humidity(outdoor_temp, outdoor_humidity)

        extra: dict[str, Any] = {
            "scale": {"min": low, "max": high},
            "heating_count": sum(1 for r in readings if r["calling"]),
            "windows_open": [r["name"] for r in readings if r["window_open"]],
            "manual": [r["name"] for r in readings if r["manual"]],
        }
        if temperatures:
            # Filtered rather than defaulted: `r["temperature"] or inf` reads
            # a room at exactly 0.0° as unmeasured, which is the one
            # temperature where a coldest-room answer matters most.
            measured = [r for r in readings if r["temperature"] is not None]
            extra["coldest"] = min(measured, key=lambda r: r["temperature"])["name"]
            extra["warmest"] = max(measured, key=lambda r: r["temperature"])["name"]
            # One decimal, because the spread of two one-decimal readings is
            # otherwise published as 3.6999999999999993.
            extra["spread"] = round(max(temperatures) - min(temperatures), 1)
        if outdoor_temp is not None:
            extra["outdoor"] = {
                "temperature": outdoor_temp,
                "humidity": outdoor_humidity,
                "dew_point": dew_point_c(outdoor_temp, outdoor_humidity),
                "absolute_humidity": outdoor_g,
            }
        if (answer := ventilation(readings, outdoor_g)) is not None:
            extra["ventilation"] = answer
        self._extra = extra

    @property
    def native_value(self) -> int:
        """How many rooms are being reported.

        Not a temperature. There is no one number for a house, and picking
        the mean of ten rooms would be a figure that is true of none of them
        — the same reason the activity feed publishes a count rather than a
        summary. A count also tells a card that ten zones are configured and
        none of them can be read, which is a different silence from an empty
        configuration.
        """
        return len(self._rows)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"rooms": list(self._rows), **self._extra}
