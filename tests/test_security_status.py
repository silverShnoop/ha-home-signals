"""The security traffic light's state machine, exercised without Home Assistant.

Home Assistant is not pip-installable into a plain checkout, and this logic is
the one piece here that a person trusts from across a room: green has to mean
locked, and red has to arrive on time. So `tests/stubs/` carries just enough of
the Home Assistant surface for `derived.py` to import and run — a state
machine, a clock that can be moved, and a scheduler that records what was
armed rather than waiting for it.

Run it with `python3 tests/test_security_status.py`. No dependencies.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent / "stubs"))
sys.path.insert(0, str(ROOT / "custom_components"))

from homeassistant.core import HomeAssistant, State
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import event as ev
from homeassistant.util import dt as dt_util
from home_signals.derived import SecurityStatusSensor

UTC = timezone.utc
T0 = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)

def at(mins):
    dt_util.NOW[0] = T0 + timedelta(minutes=mins)

def make(grace=5, openings=None):
    hass = HomeAssistant()
    entry = ConfigEntry(options={
        "security_locks": [],
        "security_openings": openings or [],
        "security_grace_minutes": grace,
    })
    s = SecurityStatusSensor(entry)
    s.hass = hass
    return s, hass

def lock(hass, eid, state, changed):
    hass.states.set(State(eid, state, {"friendly_name": "Front door"}, changed))

def door(hass, eid, state, changed):
    hass.states.set(State(eid, state, {"friendly_name": "Back door"}, changed))

fails = []
def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + f"{label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok: fails.append(label)

print("1. All locked -> green")
at(0); s, h = make(); lock(h, "lock.front_door", "locked", T0)
s._recompute()
check("state", s.native_value, "green")
check("detail", s._detail, "All secure")
check("since", s._since, None)
check("items", s._items, [])

print("\n2. Unlocked just now -> amber, and a red timer is armed")
at(0); s, h = make(); lock(h, "lock.front_door", "unlocked", T0)
ev.SCHEDULED.clear()
s._recompute()
check("state", s.native_value, "amber")
check("detail", s._detail, "Front door")
check("since", s._since, T0)
check("row count", len(s._items), 1)
check("row value", s._items[0]["value"], "Unlocked")
check("row accent (warn)", s._items[0]["accent"], 2)
check("timer armed for", ev.SCHEDULED[0][0] if ev.SCHEDULED else None, T0 + timedelta(minutes=5))

print("\n3. Four minutes later -> still amber")
at(4); s._recompute()
check("state", s.native_value, "amber")

print("\n4. Five minutes -> red, no further timer")
at(5); ev.SCHEDULED.clear(); s._recompute()
check("state", s.native_value, "red")
check("row accent (alert)", s._items[0]["accent"], 1)
check("timers pending", len(ev.SCHEDULED), 0)

print("\n5. Locked again -> green, since cleared")
at(6); lock(h, "lock.front_door", "locked", T0 + timedelta(minutes=6))
s._recompute()
check("state", s.native_value, "green")
check("since", s._since, None)

print("\n6. Unlocked again -> amber restarts from the new moment, not the old one")
at(10); lock(h, "lock.front_door", "unlocked", T0 + timedelta(minutes=10))
s._recompute()
check("state", s.native_value, "amber")
check("since", s._since, T0 + timedelta(minutes=10))

print("\n7. Restored stamp from before a restart keeps it red")
at(30); s2, h2 = make()
lock(h2, "lock.front_door", "unlocked", T0 + timedelta(minutes=30))  # last_changed reset by reboot
s2._since = T0                                                       # restored from attributes
s2._recompute()
check("state", s2.native_value, "red")
check("since", s2._since, T0)

print("\n8. A lock that cannot be read holds amber, never red")
at(0); s3, h3 = make()
lock(h3, "lock.front_door", "unavailable", T0)
ev.SCHEDULED.clear(); s3._recompute()
check("state", s3.native_value, "amber")
check("detail", s3._detail, "1 not reporting")
check("since", s3._since, None)
check("no red timer", len(ev.SCHEDULED), 0)
at(60); s3._recompute()
check("still amber after an hour", s3.native_value, "amber")

print("\n9. A door contact counts too, and the earliest one sets the clock")
at(10); s4, h4 = make(openings=["binary_sensor.back_door"])
lock(h4, "lock.front_door", "unlocked", T0 + timedelta(minutes=8))
door(h4, "binary_sensor.back_door", "on", T0 + timedelta(minutes=2))
s4._recompute()
check("state", s4.native_value, "red")
check("since = earliest", s4._since, T0 + timedelta(minutes=2))
check("detail", s4._detail, "Front door, Back door open")
check("counts", (s4.extra_state_attributes["unlocked_count"], s4.extra_state_attributes["open_count"]), (1, 1))

print("\n10. A one-minute grace is honoured")
at(0); s5, h5 = make(grace=1); lock(h5, "lock.front_door", "unlocked", T0)
s5._recompute(); check("at 0 min", s5.native_value, "amber")
at(1); s5._recompute(); check("at 1 min", s5.native_value, "red")

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
