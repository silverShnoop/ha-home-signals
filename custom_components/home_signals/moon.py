"""The moon: its phase, how much of it is lit, and when it is next new or full.

Worked out here rather than read from anywhere, because nothing in Home
Assistant says it. Core's `moon` integration names eight phases and stops:
no fraction lit, no age, no next full moon. The weather card draws the moon
at its real shape, and a picture of a crescent needs to know how thick the
crescent is.

Two of Jean Meeus's methods (*Astronomical Algorithms*, 2nd ed.), both in
plain arithmetic, so nothing is installed and nothing is fetched:

- **Chapter 49, the instants of new and full moon.** A mean lunation plus the
  periodic terms for each. Without the fourteen small planetary terms the
  book adds last, it is good to about a minute, which is better than anything
  that will ever read it needs.
- **Chapter 48, the illuminated fraction**, from the phase angle, itself from
  three mean anomalies and six periodic terms. Good to a fraction of a percent.

Where the moon actually is in the sky -- rising, setting, altitude -- is
deliberately not here. The card places the moon on the night as a clock,
from sunset to sunrise, because a moon drawn where it really is would be
missing on most evenings and up in the daytime on the rest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import acos, cos, pi, radians, sin

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

#: Phase names, the same eight states core's `moon` integration uses, so an
#: automation written against either reads the same.
NEW_MOON = "new_moon"
WAXING_CRESCENT = "waxing_crescent"
FIRST_QUARTER = "first_quarter"
WAXING_GIBBOUS = "waxing_gibbous"
FULL_MOON = "full_moon"
WANING_GIBBOUS = "waning_gibbous"
LAST_QUARTER = "last_quarter"
WANING_CRESCENT = "waning_crescent"
PHASES = [
    NEW_MOON, WAXING_CRESCENT, FIRST_QUARTER, WAXING_GIBBOUS,
    FULL_MOON, WANING_GIBBOUS, LAST_QUARTER, WANING_CRESCENT,
]

#: The mean synodic month, in days. Only used to step between lunations;
#: every instant reported is the true one, not the mean.
SYNODIC = 29.530588861

#: How long a new or full moon is "today". The instant is a moment, but the
#: name is read as a description of tonight, so it holds for half a day
#: either side of it.
_EVENT_WINDOW = timedelta(hours=12)
#: The same idea for the quarters, in percentage points of lit disc. Near a
#: quarter the fraction moves about twelve points a day, so six either side
#: is the same half a day.
_QUARTER_BAND = 6.0

_J2000 = 2451545.0
_UNIX_EPOCH_JD = 2440587.5
#: Terrestrial minus universal time. Meeus's instants are in TT; the clock on
#: the wall is UT. Sixty-nine seconds through the 2020s, and it moves by
#: about a second a decade, so a constant is honest at this precision.
_DELTA_T = timedelta(seconds=69)


def _jd(when: datetime) -> float:
    return when.timestamp() / 86400.0 + _UNIX_EPOCH_JD


def _from_jde(jde: float) -> datetime:
    return datetime.fromtimestamp((jde - _UNIX_EPOCH_JD) * 86400.0, UTC) - _DELTA_T


def _event(k: float) -> datetime:
    """The instant of lunation `k`: an integer is a new moon, k + 0.5 a full one.

    Meeus 49.1 and the periodic terms that follow it. k = 0 is the new moon
    of 6 January 2000.
    """
    t = k / 1236.85
    jde = (
        2451550.09766 + SYNODIC * k + 0.00015437 * t**2
        - 0.000000150 * t**3 + 0.00000000073 * t**4
    )
    e = 1 - 0.002516 * t - 0.0000074 * t**2
    m = radians(2.5534 + 29.10535670 * k - 0.0000014 * t**2 - 0.00000011 * t**3)
    mp = radians(
        201.5643 + 385.81693528 * k + 0.0107582 * t**2
        + 0.00001238 * t**3 - 0.000000058 * t**4
    )
    f = radians(
        160.7108 + 390.67050284 * k - 0.0016118 * t**2
        - 0.00000227 * t**3 + 0.000000011 * t**4
    )
    om = radians(124.7746 - 1.56375588 * k + 0.0020672 * t**2 + 0.00000215 * t**3)

    full = k % 1 != 0
    # The two tables differ only in their first seven coefficients.
    a = (
        (-0.40614, 0.17302, 0.01614, 0.01043, 0.00734, -0.00515, 0.00209)
        if full
        else (-0.40720, 0.17241, 0.01608, 0.01039, 0.00739, -0.00514, 0.00208)
    )
    jde += (
        a[0] * sin(mp)
        + a[1] * e * sin(m)
        + a[2] * sin(2 * mp)
        + a[3] * sin(2 * f)
        + a[4] * e * sin(mp - m)
        + a[5] * e * sin(mp + m)
        + a[6] * e * e * sin(2 * m)
        - 0.00111 * sin(mp - 2 * f)
        - 0.00057 * sin(mp + 2 * f)
        + 0.00056 * e * sin(2 * mp + m)
        - 0.00042 * sin(3 * mp)
        + 0.00042 * e * sin(m + 2 * f)
        + 0.00038 * e * sin(m - 2 * f)
        - 0.00024 * e * sin(2 * mp - m)
        - 0.00017 * sin(om)
        - 0.00007 * sin(mp + 2 * m)
        + 0.00004 * sin(2 * mp - 2 * f)
        + 0.00004 * sin(3 * m)
        + 0.00003 * sin(mp + m - 2 * f)
        + 0.00003 * sin(2 * mp + 2 * f)
        - 0.00003 * sin(mp + m + 2 * f)
        + 0.00003 * sin(mp - m + 2 * f)
        - 0.00002 * sin(mp - m - 2 * f)
        - 0.00002 * sin(3 * mp + m)
        + 0.00002 * sin(4 * mp)
    )
    return _from_jde(jde)


def _around(when: datetime, offset: float) -> tuple[datetime, datetime]:
    """The last event of one kind at or before `when`, and the next after it.

    `offset` is 0 for new moons and 0.5 for full. Starts from the mean
    lunation and steps, because a true instant can sit up to fourteen hours
    either side of the mean one.
    """
    k = round((_jd(when) - 2451550.09766) / SYNODIC - offset) + offset
    while _event(k) > when:
        k -= 1
    while _event(k + 1) <= when:
        k += 1
    return _event(k), _event(k + 1)


def illuminated(when: datetime) -> float:
    """The fraction of the disc lit, 0 to 1. Meeus 48.4 and 48.1."""
    t = (_jd(when) + _DELTA_T.total_seconds() / 86400.0 - _J2000) / 36525.0
    d = 297.8501921 + 445267.1114034 * t - 0.0018819 * t**2 + t**3 / 545868 - t**4 / 113065000
    m = 357.5291092 + 35999.0502909 * t - 0.0001536 * t**2 + t**3 / 24490000
    mp = 134.9633964 + 477198.8675055 * t + 0.0087414 * t**2 + t**3 / 69699 - t**4 / 14712000
    d, m, mp = radians(d), radians(m), radians(mp)
    i = (
        180 - d * 180 / pi
        - 6.289 * sin(mp)
        + 2.100 * sin(m)
        - 1.274 * sin(2 * d - mp)
        - 0.658 * sin(2 * d)
        - 0.214 * sin(2 * mp)
        - 0.110 * sin(d)
    )
    return (1 + cos(radians(i))) / 2


def moon(when: datetime) -> dict:
    """Everything the sensor reports, for one instant."""
    last_new, next_new = _around(when, 0.0)
    last_full, next_full = _around(when, 0.5)
    waxing = last_new > last_full
    lit = illuminated(when)
    percent = lit * 100

    if min(abs(when - last_new), abs(next_new - when)) <= _EVENT_WINDOW:
        name = NEW_MOON
    elif min(abs(when - last_full), abs(next_full - when)) <= _EVENT_WINDOW:
        name = FULL_MOON
    elif abs(percent - 50) <= _QUARTER_BAND:
        name = FIRST_QUARTER if waxing else LAST_QUARTER
    elif waxing:
        name = WAXING_CRESCENT if percent < 50 else WAXING_GIBBOUS
    else:
        name = WANING_CRESCENT if percent < 50 else WANING_GIBBOUS

    # Where in the cycle the shape is, 0 new to 0.5 full and back to 1. Taken
    # from the lit fraction rather than from the age, so that a card drawing
    # the terminator from it draws exactly the fraction stated beside it.
    half = acos(max(-1.0, min(1.0, 1 - 2 * lit))) / (2 * pi)
    cycle = half if waxing else 1 - half

    return {
        "phase": name,
        "illumination": round(percent, 1),
        "age": round((when - last_new).total_seconds() / 86400, 2),
        "cycle": round(cycle, 4),
        "waxing": waxing,
        "next_new_moon": next_new,
        "next_full_moon": next_full,
    }


#: The disc changes by at most half a percentage point an hour, and the name
#: at most twice a week. Ten minutes is far finer than either.
SCAN_INTERVAL = timedelta(minutes=10)

_ICONS = {
    NEW_MOON: "mdi:moon-new",
    WAXING_CRESCENT: "mdi:moon-waxing-crescent",
    FIRST_QUARTER: "mdi:moon-first-quarter",
    WAXING_GIBBOUS: "mdi:moon-waxing-gibbous",
    FULL_MOON: "mdi:moon-full",
    WANING_GIBBOUS: "mdi:moon-waning-gibbous",
    LAST_QUARTER: "mdi:moon-last-quarter",
    WANING_CRESCENT: "mdi:moon-waning-crescent",
}


class MoonSensor(SensorEntity):
    """`sensor.moon`: tonight's phase by name, with the numbers behind it.

    Nothing to configure and nothing to restore: every value is a function
    of the clock, so a restart recomputes it exactly.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Moon"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PHASES

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_moon"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, SCAN_INTERVAL)
        )
        self._update()

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self._update()
        self.async_write_ha_state()

    def _update(self) -> None:
        now = moon(dt_util.utcnow())
        self._attr_native_value = now["phase"]
        self._attr_icon = _ICONS[now["phase"]]
        self._attr_extra_state_attributes = {
            "illumination": now["illumination"],
            "age": now["age"],
            "cycle": now["cycle"],
            "waxing": now["waxing"],
            "next_new_moon": dt_util.as_local(now["next_new_moon"]).isoformat(),
            "next_full_moon": dt_util.as_local(now["next_full_moon"]).isoformat(),
        }
