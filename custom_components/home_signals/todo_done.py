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
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

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

    def __init__(self, entry: ConfigEntry, list_entity: str, name: str) -> None:
        """Track one to-do list."""
        self._entry = entry
        self._list = list_entity
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
