"""What got ticked off a to-do list today.

A to-do entity remembers WHAT was completed and, mostly, not WHEN. Home
Assistant's own `local_todo` writes a `completed` timestamp onto each item
because iCalendar has a field for it; Bring has no such field, so the
eighteen completed items on the shopping list are eighteen items ticked
off at some unknown point over the last however many days.

That is the whole reason this sensor exists. "What did we get done today"
cannot be answered by filtering the list, because for half the lists in
this house there is nothing to filter on. It has to be watched as it
happens and written down.

Watched rather than polled: a to-do entity's state is its outstanding
count, so ticking something off moves it, and that is the cue to re-read
the completed items and see which ones are new. The record survives a
restart (RestoreEntity) and empties at local midnight, because the
question is about a day.

Watching only knows what happened while it was watching, though, and
the first morning of anything is the morning it knows nothing about. So
where the integration publishes an ACTIVITY entity -- Bring's
`event.<list>_activities` names the exact items in each change -- the
recorder is read back to local midnight and the part of today that
happened before we were looking is filled in. That is the answer to
"does Home Assistant already store this": not for the to-do entity,
whose items are not attributes and so are never recorded, but yes for
the activity entity, whose items are.

Two traps in that history, both of which would put the wrong time on
the right item:

  * the event's own timestamp is its STATE, not `last_changed`. A
    restart replays the entity, so `last_changed` is when Home
    Assistant came back and the state is when the shopping happened.
  * the same event therefore appears more than once. Deduplicated on
    the item's uuid, keeping the earliest.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

LOGGER = logging.getLogger(__name__)

_IGNORED = {STATE_UNKNOWN, STATE_UNAVAILABLE, None}


def _today(when: datetime | None) -> bool:
    """Is this local-today? A day, not the last 24 hours."""
    if when is None:
        return False
    return dt_util.as_local(when).date() == dt_util.now().date()


def _completed_at(item: dict[str, Any]) -> datetime | None:
    """The item's own completion time, where the integration records one."""
    raw = item.get("completed")
    if not isinstance(raw, str):
        return None
    return dt_util.parse_datetime(raw)


class TodoDoneTodaySensor(SensorEntity, RestoreEntity):
    """The items ticked off one list since midnight.

    The state is how many, and `items` carries them in the same shape the
    list itself hands the card, so one row renderer draws both sections.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_icon = "mdi:check-all"

    def __init__(
        self,
        entry: ConfigEntry,
        list_entity: str,
        name: str,
        activity: str | None = None,
    ) -> None:
        """Track one to-do list, and the activity feed for it if it has one."""
        self._entry = entry
        self._list = list_entity
        self._activity = activity
        self._attr_name = f"{name} done today"
        self._attr_unique_id = f"{entry.entry_id}_done_today_{list_entity}"
        # uid -> the row as it will be rendered, carrying `completed`.
        self._done: dict[str, dict[str, Any]] = {}
        # Every uid currently sitting in the list's completed set, today's
        # or not. What makes a tick a tick is ARRIVING here.
        self._seen: set[str] = set()
        self._seeded = False

    @property
    def native_value(self) -> int:
        """How many things got done today."""
        return len(self._rows())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The rows, newest first."""
        return {"list": self._list, "items": self._rows()}

    def _rows(self) -> list[dict[str, Any]]:
        """Today's rows, newest first.

        Filtered on the way out as well as cleared at midnight: a restart
        that spans midnight restores yesterday's record, and the timer that
        should have cleared it never fired because nothing was running.
        """
        # The ONLY place the day is enforced. A second check when
        # recording looked like prudence and was redundant -- nothing can
        # observe the difference, because an out-of-date row is filtered
        # here whether it was ever stored or not -- so it went, rather
        # than sit in the file as a rule no test could hold to account.
        rows = [
            row for row in self._done.values()
            if _today(dt_util.parse_datetime(row.get("completed") or ""))
        ]
        rows.sort(key=lambda row: row.get("completed") or "", reverse=True)
        return rows

    async def async_added_to_hass(self) -> None:
        """Restore today's record, then watch the list."""
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is not None:
            restored = last.attributes.get("items")
            if isinstance(restored, list):
                for row in restored:
                    if isinstance(row, dict) and isinstance(row.get("uid"), str):
                        self._done[row["uid"]] = dict(row)

        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._list], self._changed)
        )
        # The day ends at midnight and the section is about a day, so it is
        # cleared on the clock rather than only when something next happens.
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._midnight, hour=0, minute=0, second=0
            )
        )
        await self._sync()
        await self._backfill()

    async def _backfill(self) -> None:
        """Fill in the part of today that happened before we were watching.

        Only items that are STILL completed are taken: an item bought
        this morning and put back on the list since is not done today,
        and the live list is the authority on that. The name comes from
        the list too -- the activity feed carries Bring's catalogue id,
        which is in German for anything added from their suggestions.
        """
        if not self._activity:
            return
        rows = await self._activity_today()
        if not rows:
            return
        items = await self._completed_items()
        if items is None:
            return
        live = {
            str(item.get("uid")).lower(): item
            for item in items
            if isinstance(item.get("uid"), str)
        }

        changed = False
        for uid, when in rows.items():
            item = live.get(uid)
            if item is None:
                continue
            real = str(item.get("uid"))
            # An item that stamps itself has already answered, and its
            # own answer beats anybody's reconstruction of it. Only a
            # list with no stamps ever gets here with something to add,
            # which is the case this exists for -- but the guard is
            # cheap and the alternative is a rule that happens to hold.
            if _completed_at(item) is not None:
                continue
            self._done[real] = {
                "uid": real,
                "summary": item.get("summary"),
                "description": item.get("description"),
                "status": "completed",
                "completed": dt_util.as_utc(when).isoformat(),
            }
            changed = True
        if changed:
            self.async_write_ha_state()

    async def _activity_today(self) -> dict[str, datetime]:
        """Item uuid -> when it was taken off the list, since midnight.

        Read from the recorder, which keeps an entity's attributes as
        well as its state -- and the activity entity's attributes are
        the only place the WHICH and the WHEN sit together.
        """
        start = dt_util.start_of_local_day()
        try:
            states = await get_instance(self.hass).async_add_executor_job(
                history.state_changes_during_period,
                self.hass,
                start,
                dt_util.utcnow(),
                self._activity,
                False,
                True,
            )
        except Exception:  # noqa: BLE001 - no history is not an error
            LOGGER.warning("Could not read history for %s", self._activity)
            return {}

        found: dict[str, datetime] = {}
        for state in (states or {}).get(self._activity, []):
            if state.state in _IGNORED:
                continue
            if state.attributes.get("event_type") != "list_items_removed":
                continue
            # The STATE is when it happened. `last_changed` is when Home
            # Assistant last republished it, which after a restart is
            # the restart -- and that is how a morning's shopping ends
            # up dated to the afternoon.
            when = dt_util.parse_datetime(state.state)
            if when is None or not _today(when):
                continue
            for item in state.attributes.get("items") or []:
                if not isinstance(item, dict):
                    continue
                uuid = item.get("uuid")
                if not isinstance(uuid, str) or not uuid:
                    continue
                key = uuid.lower()
                # The LATEST removal, not the first. An item bought in
                # the morning, put back on the list at lunchtime and
                # bought again at three was done at three -- the first
                # one is a completion that was undone, and dating the
                # row to it would be recording a fact that stopped
                # being true. (A restart replays the same event with
                # the same state, so the ordinary duplicate resolves to
                # itself either way.)
                if key not in found or when > found[key]:
                    found[key] = when
        return found

    @callback
    def _changed(self, event: Event[EventStateChangedData]) -> None:
        """The outstanding count moved, so the list behind it moved too."""
        new_state = event.data["new_state"]
        if new_state is None or new_state.state in _IGNORED:
            return
        self.hass.async_create_task(self._sync())

    @callback
    def _midnight(self, _now: datetime) -> None:
        """A new day, so the section starts empty again."""
        self._done.clear()
        self.async_write_ha_state()

    async def _sync(self) -> None:
        """Re-read the completed items and record what is newly among them."""
        items = await self._completed_items()
        if items is None:
            return

        now = dt_util.utcnow()
        present: set[str] = set()
        changed = False

        for item in items:
            uid = item.get("uid")
            if not isinstance(uid, str) or not uid:
                continue
            present.add(uid)

            # An item's own timestamp is better than the moment we noticed
            # it: it is right across a restart, and it is what lets a list
            # that records one answer for the part of today we missed.
            stamped = _completed_at(item)
            if uid in self._seen and stamped is None:
                continue
            if stamped is None:
                # The first read after a restart is a census, not a day's
                # work. Without this every item Bring has ever kept would
                # be stamped "now" and the section would open with a
                # fortnight of shopping in it.
                if not self._seeded:
                    continue
                stamped = now
            if uid in self._done:
                continue
            self._done[uid] = {
                "uid": uid,
                "summary": item.get("summary"),
                "description": item.get("description"),
                "status": "completed",
                "completed": dt_util.as_utc(stamped).isoformat(),
            }
            changed = True

        # Put something back on the list and it is no longer done today --
        # the un-tick is as real an act as the tick was.
        for uid in [uid for uid in self._done if uid not in present]:
            del self._done[uid]
            changed = True

        self._seen = present
        self._seeded = True
        if changed:
            self.async_write_ha_state()

    async def _completed_items(self) -> list[dict[str, Any]] | None:
        """The list's completed items, or None if it could not be read."""
        try:
            response = await self.hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": self._list, "status": "completed"},
                blocking=True,
                return_response=True,
            )
        except Exception:  # noqa: BLE001 - any failure here is "no reading"
            LOGGER.warning("Could not read completed items from %s", self._list)
            return None
        payload = (response or {}).get(self._list) or {}
        items = payload.get("items")
        return items if isinstance(items, list) else None
