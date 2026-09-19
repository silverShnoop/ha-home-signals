"""Whether an appliance is running, from nothing but its plug.

A washing machine has no idea it is a washing machine, and the plug it is
sitting on only reports watts. Everything here is the work of turning that
one number into the three facts a person actually wants:

- is it running,
- is there still washing inside it,
- and is there washing waiting to be hung up.

They are tracked separately because three different things answer them. The
plug answers the first. The door answers the second. Only a person can answer
the third, which is why it is the only one with a button.

## Why the thresholds are asymmetric

The hard part is that a cycle is not a continuous draw. A machine heats
(2kW), agitates (200W), rests (2W), agitates, rests, soaks for several
minutes at nothing at all, then spins (600W). Read instantaneously, a wash
looks like a dozen short cycles with gaps between them.

So the machine enters RUNNING the instant it draws real power, and leaves it
only after the draw has stayed down for an unbroken stretch. Entering is
cheap to get wrong and easy to undo; leaving is what creates a load of
laundry, so it is the one that has to be sure.

The floor is a guess until a real wash has run, which is why every cycle
records its own longest lull. After one load the right value is a number you
can read off the sensor rather than one somebody picked.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    KIND_BUTTON,
    APPLIANCE_IDLE,
    APPLIANCE_OFF,
    APPLIANCE_RUNNING,
    CLEANING_AMBER,
    CLEANING_GREEN,
    CLEANING_RED,
    DOMAIN,
    PHASE_FILL,
    PHASE_HEAT,
    PHASE_SPIN,
    PHASE_TUMBLE,
)

LOGGER = logging.getLogger(__name__)

_NOT_A_READING = {STATE_UNKNOWN, STATE_UNAVAILABLE, None}

# A backstop only. The interesting transition — the end of a cycle — is
# scheduled for the exact moment it is earned, because a five-minute tick
# enforcing a five-minute floor could mean waiting ten.
SCAN_INTERVAL = timedelta(minutes=5)

# How many finished cycles to keep. Enough for the card's "finished today"
# on the heaviest imaginable day, and bounded so a year of restores cannot
# grow without limit.
MAX_HISTORY = 12


def _number(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A numeric reading, or None when there is genuinely no reading.

    None and zero are different answers and the difference matters: a plug
    that has stopped reporting is not a machine drawing nothing. Everywhere
    below, None means "hold the current state", never "idle".
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _NOT_A_READING:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _is_on(hass: HomeAssistant, entity_id: str | None, default: bool) -> bool:
    """A binary reading, with an explicit answer for "cannot tell"."""
    if not entity_id:
        return default
    state = hass.states.get(entity_id)
    if state is None or state.state in _NOT_A_READING:
        return default
    return state.state == STATE_ON


class ApplianceCycleSensor(SensorEntity, RestoreEntity):
    """One appliance: off, idle or running, plus what it has left behind.

    The state is only ever the cycle. Whether the drum still holds washing,
    and whether that washing has been hung, are attributes — they outlive the
    cycle that created them and they are cleared by different things.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [APPLIANCE_OFF, APPLIANCE_IDLE, APPLIANCE_RUNNING]

    def __init__(self, entry: ConfigEntry, spec: dict[str, Any]) -> None:
        self._entry = entry
        self._spec = spec
        self._slug = spec["slug"]
        self._attr_name = spec["name"]
        # Per appliance, not per class. A dryer wearing a washing machine is
        # the kind of wrong that is invisible on the card -- which sets its
        # own icon -- and obvious everywhere the entity speaks for itself.
        self._attr_icon = spec.get("icon", "mdi:washing-machine")
        self._attr_unique_id = f"{entry.entry_id}_{self._slug}_cycle"

        self._state = APPLIANCE_IDLE
        self._started_at: datetime | None = None
        self._energy_at_start: float | None = None
        # When the draw first dropped below the idle threshold during this
        # cycle. The cycle ENDED here, not when the floor finally expired —
        # otherwise every wash is reported minutes longer than it ran.
        self._quiet_since: datetime | None = None
        self._longest_lull = 0.0
        self._peak_watts = 0.0
        self._cancel_quiet: CALLBACK_TYPE | None = None
        # What the machine has been doing this cycle, oldest first. One
        # entry per run of a phase, so a wash that heats twice has two
        # heat entries rather than one merged one -- they were two
        # different things the machine did.
        self._phases: list[dict[str, Any]] = []
        self._phase_pending: str | None = None
        self._phase_pending_since: datetime | None = None
        self._phase_pending_low: float = 0.0
        self._phase_pending_high: float = 0.0
        # What each kind has actually been seen drawing, across every
        # cycle rather than this one, and kept over a restart. One wash
        # is not much to fit two thresholds to; this is how the second
        # and the tenth get counted without anybody watching a plug.
        self._phase_evidence: dict[str, dict[str, float]] = {}

        self._drum_full = False
        self._pending: list[dict[str, Any]] = []
        self._history: list[dict[str, Any]] = []
        self._door_was_open = False
        # Things to poke when this changes. Needs you and the cleaning light
        # are both derived from `pending`, which lives in here rather than in
        # any entity they could subscribe to — a state subscription would
        # only ever see the sensors that already existed when they started.
        self._listeners: list[Any] = []

    # --- configuration ------------------------------------------------

    @property
    def slug(self) -> str:
        return self._slug

    @callback
    def add_listener(self, listener: Any) -> None:
        """Register something with a `refresh()` to call after a change."""
        self._listeners.append(listener)

    def _publish(self) -> None:
        self.async_write_ha_state()
        for listener in self._listeners:
            listener.refresh()

    def _cfg(self, key: str, default: Any) -> Any:
        value = self._spec.get(key)
        return default if value in (None, "") else value

    def _watched(self) -> list[str]:
        return [
            e
            for e in (
                self._spec.get("power_sensor"),
                self._spec.get("plug"),
                self._spec.get("door"),
                self._spec.get("leak"),
                self._spec.get("energy_sensor"),
            )
            if e
        ]

    # --- lifecycle ----------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._stop_quiet_timer)
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, SCAN_INTERVAL)
        )
        if watched := self._watched():
            self.async_on_remove(
                async_track_state_change_event(self.hass, watched, self._async_changed)
            )
        self.async_on_remove(async_at_started(self.hass, self._async_started))

        if (last := await self.async_get_last_state()) is not None:
            self._restore(last.attributes)

    def _restore(self, attrs: dict[str, Any]) -> None:
        """Bring back what a restart would otherwise quietly forget.

        Two loads waiting to be hung must survive a Home Assistant update, or
        the reminder disappears exactly when nobody is watching for it. Same
        for a full drum: the door has already been shut, so nothing would
        ever tell us again.
        """
        pending = attrs.get("pending")
        if isinstance(pending, list):
            self._pending = [p for p in pending if isinstance(p, dict) and p.get("id")]
        history = attrs.get("finished")
        if isinstance(history, list):
            self._history = [h for h in history if isinstance(h, dict)][:MAX_HISTORY]
        self._drum_full = bool(attrs.get("drum_full"))
        phases = attrs.get("phases")
        if isinstance(phases, list):
            self._phases = [p for p in phases if isinstance(p, dict) and p.get("kind")]
        evidence = attrs.get("phase_evidence")
        if isinstance(evidence, dict):
            self._phase_evidence = {
                str(kind): dict(seen)
                for kind, seen in evidence.items()
                if isinstance(seen, dict) and "low" in seen and "high" in seen
            }
        # A cycle in flight across a restart is NOT resumed. We cannot know
        # what the machine did while we were not looking, and inventing a
        # start time would put a fictional duration on a real load.

    @callback
    def _async_started(self, _hass: HomeAssistant) -> None:
        self._sync_door()
        self._evaluate()
        self._publish()

    @callback
    def _async_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._sync_door()
        self._evaluate()
        self._publish()

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self._sync_door()
        self._evaluate()
        self._publish()

    # --- the door -----------------------------------------------------

    def _sync_door(self) -> None:
        """Opening the door empties the drum. Closing it does not fill it.

        Only a finished cycle fills the drum, so a door opened and shut to
        throw one more sock in leaves the machine empty, which it is.
        """
        door = self._spec.get("door")
        if not door:
            return
        open_now = _is_on(self.hass, door, default=self._door_was_open)
        if open_now and not self._door_was_open:
            self._drum_full = False
        self._door_was_open = open_now

    # --- the state machine --------------------------------------------

    def _evaluate(self) -> None:
        now = dt_util.utcnow()
        start_watts = float(self._cfg("start_watts", 8))
        idle_watts = float(self._cfg("idle_watts", 4))
        watts = _number(self.hass, self._spec.get("power_sensor"))

        if not _is_on(self.hass, self._spec.get("plug"), default=True):
            # Power pulled. A cycle in flight did not finish, it was
            # interrupted — by the leak automation, or by a person. Either
            # way there is no clean laundry at the end of it.
            self._abandon()
            self._state = APPLIANCE_OFF
            return

        if self._state == APPLIANCE_OFF:
            self._state = APPLIANCE_IDLE

        if watts is None:
            # The plug has stopped reporting. Hold: silence is not zero.
            return

        self._peak_watts = max(self._peak_watts, watts)

        if watts < start_watts:
            # Whatever was waiting to become a phase has to earn it again.
            # A soak can sit under the floor for ten minutes; without this
            # the reading either side of it would look like one unbroken
            # run and commit a phase dated to before the silence.
            self._forget_pending()

        if watts >= start_watts:
            self._note_lull_ended(now)
            if self._state != APPLIANCE_RUNNING:
                self._begin(now)
            # After _begin, so the opening reading lands in a fresh
            # timeline rather than the end of the last cycle's.
            if self._cfg("tracks_phases", False):
                self._note_phase(now, watts)
            return

        if watts >= idle_watts:
            # The band between the two thresholds belongs to whichever state
            # we are already in. That gap IS the hysteresis: without it a
            # machine hovering either side of one number would chatter.
            self._note_lull_ended(now)
            return

        # Below the idle floor.
        if self._state != APPLIANCE_RUNNING:
            return
        if self._quiet_since is None:
            self._quiet_since = now
            self._arm_quiet_timer(now)

    # --- what it is doing, from the draw ------------------------------

    # PHASE_BANDS, and the reasoning behind them.
    #
    # Measured off one complete wash, 20:37 to 21:26 on 19 Sep 2026, at
    # the plug's own 5-second reporting interval:
    #
    #     fill    20:37:04-20:38:46    6-36 W
    #     heat    20:38:51-20:42:41    2214-2263 W
    #     tumble  20:42:46-20:47:34    12-57 W
    #     heat    20:47:40-20:48:20    2221-2245 W
    #     tumble  20:48:25-21:00:18    12-84 W
    #     spin    21:00:23-21:02:03    ramp to 302 W
    #     tumble  21:02-21:09          12-90 W
    #     spin    21:09:15-21:11:02    ramp to 319 W
    #     tumble  21:11-21:15          12-74 W
    #     spin    21:15:46-21:20:17    ramp to 390 W, held ~1 min
    #     quiet   21:21:27 onward      5 W, then 0
    #
    # What the trace settles, and what it does not.
    #
    # It settles the SHAPE. Heat is unmistakable at 2.2 kW, an order of
    # magnitude clear of everything else. Spin is a sustained ramp into
    # the hundreds, while the tumble band throws single readings of
    # 107-120 W -- which is why a run also has to last
    # MIN_PHASE_SECONDS, and why the duration guard, not the threshold,
    # is what actually catches those readings.
    #
    # It does NOT settle the two NUMBERS, and one wash is not much
    # evidence, so the numbers are not picked by eye. Any spin floor
    # between 120 and 203 W, and any heat floor between 390 W and
    # 2.2 kW, replays this cycle identically -- the trace cannot choose
    # inside those gaps. So each boundary is put at the GEOMETRIC MEAN
    # of the two bands it separates, which is the maximum-margin
    # estimate from one sample, and multiplicative because that is how
    # a draw varies: a half load, a different programme and a stiffer
    # motor all scale it rather than shift it.
    #
    #     spin floor  sqrt(120 x 203)   =  156 W   1.3x clear each side
    #     heat floor  sqrt(390 x 2214)  =  929 W   2.4x clear each side
    #
    # The heat floor had been 1500 W, which sat 3.9x above the highest
    # spin and only 1.5x below the lowest heat -- all the margin on the
    # side that did not need it. 929 W is the same evidence, read
    # without a thumb on it.
    #
    # The spin floor's 1.3x is thin, and it is thin because the gap
    # really is: a lurching drum reaches 120 W and a slow spin starts
    # at 203 W. That is what MIN_PHASE_SECONDS is for, and between them
    # the two guards cover each other.
    #
    # Fill is the one the power cannot tell at all. It sits at 6-36 W,
    # inside tumble's own 12-90 W band, and the only thing separating
    # them in that trace is position: the fill came first. So fill is
    # defined positionally -- the opening run of a cycle -- and it is
    # the weakest claim here by a distance. MAX_FILL_SECONDS is the
    # limit of it: a machine fills in a minute or two, so an opening
    # run still going after five is not a fill we failed to see the end
    # of, it is a cold wash tumbling, and the label is TAKEN BACK
    # rather than stretched. Better a correct tumble than a confident
    # fill.
    #
    # Every run also records the watt range it was actually seen at,
    # and `phase_evidence` accumulates those ranges across cycles and
    # survives a restart. After ten washes the floors above can be
    # re-derived from the sensor by the same sqrt rule, from real
    # readings rather than from one evening.
    PHASE_BANDS = ((929.0, PHASE_HEAT), (156.0, PHASE_SPIN))
    MIN_PHASE_SECONDS = 20.0
    MAX_FILL_SECONDS = 300.0

    def _classify(self, watts: float) -> str:
        """Which band this reading falls in. Tumble is the fallthrough.

        There is deliberately no floor under tumble. This is only ever
        asked about a reading the plug has already put at or above
        `start_watts` -- the machine is doing SOMETHING, and the quietest
        something it does is tumble. A second floor here would be a copy
        of that one, free to drift away from it.
        """
        for floor, kind in self.PHASE_BANDS:
            if watts >= floor:
                return kind
        return PHASE_TUMBLE

    def _note_phase(self, now: datetime, watts: float) -> None:
        """Fold this reading into the phase timeline.

        A kind has to be seen twice AND hold for MIN_PHASE_SECONDS
        before it is committed, so the single 107-120 W readings
        scattered through a tumble never become a spin. Until then it is
        pending: real enough to track, not yet real enough to show, and
        dropped outright if the machine goes quiet underneath it.
        """
        kind = self._classify(watts)

        # A fill that has run too long was never a fill. Taking the label
        # back is the point: positional evidence is weak, and five
        # minutes of it is weaker than admitting we cannot tell. Once it
        # is a tumble the readings below simply carry on into it.
        self._reconsider_fill(now)

        # The opening run of a cycle is the fill, and it lasts until the
        # machine does something ELSE -- not until the draw wobbles inside
        # its own band. Anchoring it to "no phases yet" ended the fill at
        # the first commit, 30 seconds in, and filed the rest of the same
        # unbroken low-power run as tumble. See PHASE_BANDS.
        opening = not self._phases or (
            len(self._phases) == 1 and self._phases[0]["kind"] == PHASE_FILL
        )
        if kind == PHASE_TUMBLE and opening:
            kind = PHASE_FILL

        current = self._phases[-1]["kind"] if self._phases else None
        if kind == current:
            run = self._phases[-1]
            run["seconds"] = round(
                (now - dt_util.parse_datetime(run["started_at"])).total_seconds()
            )
            run["low"] = min(run.get("low", watts), watts)
            run["high"] = max(run.get("high", watts), watts)
            self._note_evidence(kind, watts)
            self._forget_pending()
            return

        if kind != self._phase_pending:
            self._phase_pending = kind
            self._phase_pending_since = now
            self._phase_pending_low = watts
            self._phase_pending_high = watts
            return

        self._phase_pending_low = min(self._phase_pending_low, watts)
        self._phase_pending_high = max(self._phase_pending_high, watts)
        held = (now - self._phase_pending_since).total_seconds()
        if held < self.MIN_PHASE_SECONDS:
            return
        self._phases.append({
            "kind": kind,
            "started_at": self._phase_pending_since.isoformat(),
            "seconds": round(held),
            "low": round(self._phase_pending_low),
            "high": round(self._phase_pending_high),
        })
        self._note_evidence(kind, self._phase_pending_low, opened=True)
        self._note_evidence(kind, self._phase_pending_high)
        self._forget_pending()

    def _forget_pending(self) -> None:
        self._phase_pending = None
        self._phase_pending_since = None
        self._phase_pending_low = 0.0
        self._phase_pending_high = 0.0

    def _reconsider_fill(self, now: datetime) -> None:
        """Withdraw a fill label the evidence no longer supports.

        Only the opening run is ever called a fill, and only because it
        came first -- the watts are indistinguishable from tumbling. A
        real fill is over in a minute or two. One still going after
        MAX_FILL_SECONDS is a cold wash, so the entry is relabelled
        rather than left to grow into a forty-minute "fill".
        """
        if len(self._phases) != 1 or self._phases[0]["kind"] != PHASE_FILL:
            return
        started = dt_util.parse_datetime(self._phases[0]["started_at"])
        if started is None:
            return
        if (now - started).total_seconds() < self.MAX_FILL_SECONDS:
            return
        self._phases[0]["kind"] = PHASE_TUMBLE
        if self._phase_pending == PHASE_TUMBLE:
            self._forget_pending()

    def _note_evidence(self, kind: str, watts: float, *, opened: bool = False) -> None:
        """Widen what this kind has been seen drawing, across all cycles."""
        seen = self._phase_evidence.setdefault(
            kind, {"runs": 0, "low": watts, "high": watts}
        )
        seen["low"] = min(seen["low"], watts)
        seen["high"] = max(seen["high"], watts)
        if opened:
            seen["runs"] = seen.get("runs", 0) + 1

    def _begin(self, now: datetime) -> None:
        self._state = APPLIANCE_RUNNING
        self._started_at = now
        self._energy_at_start = _number(self.hass, self._spec.get("energy_sensor"))
        self._longest_lull = 0.0
        self._peak_watts = 0.0
        self._quiet_since = None
        self._phases = []
        # `_phase_evidence` is NOT cleared. It is what the machine has
        # been seen doing across every wash, and clearing it per cycle
        # would leave it saying exactly as much as one load does.
        self._forget_pending()
        self._stop_quiet_timer()

    def _note_lull_ended(self, now: datetime) -> None:
        """The draw came back, so whatever gap we were in was only a pause.

        Its length is recorded even though nothing acts on it. It is the
        measurement that replaces the guess: after one real wash, the longest
        lull is what the idle floor has to clear.
        """
        if self._quiet_since is None:
            return
        self._longest_lull = max(
            self._longest_lull, (now - self._quiet_since).total_seconds()
        )
        self._quiet_since = None
        self._stop_quiet_timer()

    def _arm_quiet_timer(self, now: datetime) -> None:
        self._stop_quiet_timer()
        minutes = float(self._cfg("idle_minutes", 5))
        self._cancel_quiet = async_track_point_in_time(
            self.hass, self._async_quiet_expired, now + timedelta(minutes=minutes)
        )

    def _stop_quiet_timer(self) -> None:
        if self._cancel_quiet is not None:
            self._cancel_quiet()
            self._cancel_quiet = None

    @callback
    def _async_quiet_expired(self, _now: datetime) -> None:
        self._cancel_quiet = None
        if self._state != APPLIANCE_RUNNING or self._quiet_since is None:
            return
        self._finish()
        self._publish()

    def _abandon(self) -> None:
        """A cycle that stopped without finishing. No laundry comes of it."""
        self._state = APPLIANCE_IDLE
        self._started_at = None
        self._energy_at_start = None
        self._quiet_since = None
        self._stop_quiet_timer()

    def _finish(self) -> None:
        """The draw stayed down long enough. Decide whether that was a wash."""
        ended = self._quiet_since or dt_util.utcnow()
        started = self._started_at or ended
        minutes = (ended - started).total_seconds() / 60.0

        energy = 0.0
        if self._energy_at_start is not None:
            now_kwh = _number(self.hass, self._spec.get("energy_sensor"))
            if now_kwh is not None and now_kwh >= self._energy_at_start:
                energy = now_kwh - self._energy_at_start

        self._state = APPLIANCE_IDLE
        self._quiet_since = None
        self._started_at = None
        self._stop_quiet_timer()

        # A drain, a rinse-only, or somebody nudging the dial is not a load
        # of washing, and inventing one means a reminder nobody can satisfy.
        if minutes < float(self._cfg("min_minutes", 10)):
            return
        min_kwh = float(self._cfg("min_kwh", 0.05))
        # Only enforced where there is an energy meter to enforce it with;
        # a missing meter must not silently swallow every cycle.
        if self._energy_at_start is not None and energy < min_kwh:
            return

        record = {
            "id": f"{self._slug}_{ended.isoformat(timespec='seconds')}",
            "finished_at": ended.isoformat(),
            "duration_minutes": round(minutes),
            "energy_kwh": round(energy, 2),
            "peak_watts": round(self._peak_watts),
            "longest_lull_seconds": round(self._longest_lull),
        }
        # Both machines end a cycle with a full drum, and on both the door
        # empties it. The washer has a SECOND state after that one: washing
        # out of the drum still has to be hung, on a rack in another room,
        # where the machine cannot see it happen -- so that job is queued
        # and waits to be told. A dryer's load is finished the moment it
        # comes out, so queueing one would invent a reminder nothing can
        # satisfy.
        if self._spec.get("queues_loads", True):
            self._pending.append(record)
        self._history.insert(0, record)
        del self._history[MAX_HISTORY:]
        self._drum_full = True

    # --- what a person does to it --------------------------------------

    @callback
    def hung(self, load_id: str | None = None) -> bool:
        """Mark one load as hung up. Returns whether there was one to mark.

        No argument means the oldest, which is what the wall button sends:
        standing at the machine with an armful of washing, "which of the two
        loads is this" is not a question anybody is in a position to answer.
        """
        if not self._pending:
            return False
        if load_id is None:
            self._pending.pop(0)
        else:
            before = len(self._pending)
            self._pending = [p for p in self._pending if p.get("id") != load_id]
            if len(self._pending) == before:
                return False
        self._publish()
        return True

    # --- what it reports -------------------------------------------------

    @property
    def native_value(self) -> str:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        watts = _number(self.hass, self._spec.get("power_sensor"))
        leak = _is_on(self.hass, self._spec.get("leak"), default=False)
        door_open = _is_on(self.hass, self._spec.get("door"), default=False)
        powered = _is_on(self.hass, self._spec.get("plug"), default=True)

        today = dt_util.now().date()
        finished_today = [
            h
            for h in self._history
            if (parsed := dt_util.parse_datetime(h.get("finished_at", "")))
            and dt_util.as_local(parsed).date() == today
        ]

        return {
            "slug": self._slug,
            # Whether a finished load leaves a second job behind it after
            # the drum is emptied. Needs you reads state attributes rather
            # than these objects, so the flag has to travel with them.
            "queues_loads": bool(self._spec.get("queues_loads", True)),
            "power_w": watts,
            "powered": powered,
            "leak": leak,
            "door_open": door_open,
            # Whether there is washing in the drum. Not the same question as
            # whether there is washing to hang, and cleared by a different
            # thing — the door, rather than a person.
            "drum_full": self._drum_full,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "pending": list(self._pending),
            "pending_count": len(self._pending),
            "finished": list(self._history),
            "finished_today": finished_today,
            "last_finished_at": self._history[0]["finished_at"] if self._history else None,
            # What it has done this cycle, and what it is doing now. Kept
            # after the cycle ends so the card can still show it while the
            # washing is sitting in the drum.
            "phases": list(self._phases),
            "phase": self._phases[-1]["kind"] if self._phases else None,
            # What every kind has been seen drawing, across all cycles.
            # The thresholds were fitted to one wash; this is the
            # evidence to re-fit them from once there are ten.
            "phase_evidence": {
                kind: dict(seen) for kind, seen in self._phase_evidence.items()
            },
            # Diagnostics for tuning the idle floor against a real wash
            # rather than against a guess.
            "peak_watts": round(self._peak_watts) if self._state == APPLIANCE_RUNNING else None,
            "longest_lull_seconds": round(self._longest_lull),
            "idle_minutes": float(self._cfg("idle_minutes", 5)),
            "start_watts": float(self._cfg("start_watts", 8)),
            "idle_watts": float(self._cfg("idle_watts", 4)),
        }


class AppliancePressSensor(SensorEntity, RestoreEntity):
    """When the appliance's own button was last pressed.

    It exists so a press can reach the activity feed, which watches entities
    and cannot see a `zha_event`. Every press is recorded, including the ones
    that clear nothing — somebody pressing a button and nothing happening is
    still a thing that happened in the kitchen.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:gesture-tap-button"

    def __init__(self, entry: ConfigEntry, spec: dict[str, Any]) -> None:
        self._slug = spec["slug"]
        self._attr_name = f"{spec['name']} button"
        self._attr_unique_id = f"{entry.entry_id}_{self._slug}_button"
        self._pressed_at: datetime | None = None

    @property
    def slug(self) -> str:
        return self._slug

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is None:
            return
        if last.state not in _NOT_A_READING:
            self._pressed_at = dt_util.parse_datetime(last.state)

    @callback
    def record(self) -> None:
        self._pressed_at = dt_util.utcnow()
        self.async_write_ha_state()

    @property
    def native_value(self) -> datetime | None:
        return self._pressed_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Say what kind of event this is rather than leaving it guessable.

        The activity feed classifies by domain and device class, and by
        those it is a timestamp sensor -- which says when something
        happened and nothing about what. Declaring the kind here keeps the
        guess out of the feed: it does not have to know that a sensor
        whose id ends in `_button` is a button.
        """
        return {"kind": KIND_BUTTON}


class CleaningStatusSensor(SensorEntity):
    """Is anything in the utility corner asking for attention, as a colour.

    The same shape as `security_status`, and for the same reason: a tab on a
    wall panel can be a colour long before anybody reads a word of it. Red is
    water on the floor. Amber is a job — washing to hang, or a machine left
    without power.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Cleaning status"
    _attr_icon = "mdi:washing-machine"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [CLEANING_GREEN, CLEANING_AMBER, CLEANING_RED]

    def __init__(self, entry: ConfigEntry, sensors: list[ApplianceCycleSensor]) -> None:
        self._entry = entry
        self._sensors = sensors
        self._attr_unique_id = f"{entry.entry_id}_cleaning_status"
        for sensor in sensors:
            sensor.add_listener(self)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, SCAN_INTERVAL)
        )

    @callback
    def refresh(self) -> None:
        """An appliance changed. Everything this reads is read on demand."""
        if self.hass is not None:
            self.async_write_ha_state()

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    def _read(self) -> tuple[str, str]:
        leaking: list[str] = []
        unpowered: list[str] = []
        full: list[str] = []
        waiting = 0
        for sensor in self._sensors:
            attrs = sensor.extra_state_attributes
            if attrs.get("leak"):
                leaking.append(sensor.name or sensor.slug)
            elif not attrs.get("powered", True):
                # Only worth saying while there is no leak: with water on the
                # floor, "it has no power" is the automation working, not a
                # second problem.
                unpowered.append(sensor.name or sensor.slug)
            waiting += int(attrs.get("pending_count") or 0)
            # Every appliance has this one, and the door clears it on both.
            if attrs.get("drum_full"):
                full.append(sensor.name or sensor.slug)

        if leaking:
            return CLEANING_RED, f"{leaking[0]} leaking"
        if unpowered:
            return CLEANING_AMBER, f"{unpowered[0]} has no power"
        if waiting:
            plural = "s" if waiting > 1 else ""
            return CLEANING_AMBER, f"{waiting} load{plural} to hang"
        if full:
            return CLEANING_AMBER, f"{full[0]} to empty"
        return CLEANING_GREEN, "Nothing waiting"

    @property
    def native_value(self) -> str:
        return self._read()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status, detail = self._read()
        return {"detail": detail, "status": status}
