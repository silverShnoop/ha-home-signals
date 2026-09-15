# Home Signals

Derived, house-wide signals for Home Assistant — the things no single
integration owns because they are computed across all of them.

It talks to no hardware. Everything here is built from entities other
integrations already provide.

## Why an integration and not template sensors

These could be written as `template:` YAML, and the first draft was. A Python
integration is better for this particular job, and the reasons are specific
rather than stylistic:

- **A rolling log wants a real data structure.** A trigger-template builds its
  history by reading its own previous attribute back in, which works but is a
  trick. Here it is a `deque` with a bound.
- **Restoring across a restart is free.** `RestoreEntity` brings the feed back;
  a log that empties on every restart is useless exactly when you want to know
  what just happened.
- **Classification can read the registry.** Deciding "button or motion or lock"
  by string-matching an entity id is fragile. This reads the device class and
  resolves the area through the entity and device registries.
- **It installs through HACS.** No file copying, no `template.reload`.

## `sensor.activity_feed`

One merged, newest-first feed of things that happened in the house.

- **State** — when anything last happened anywhere. `device_class: timestamp`,
  so a card renders it as a relative time ("Quiet 2m") with no second sensor.
- **`events`** — the rows, newest first, capped by `max_events`. Each carries
  `entity_id`, `name`, `area`, `kind`, `state` and `at`.
- **`tracked_count`** — how many entities are being watched, so a card can tell
  "nothing has happened" apart from "nothing is configured".

`kind` is one of `button`, `lock`, `motion`, `door`, `other`. It is a contract
with the frontend: a rail draws its icon from the kind, and a button press is
the interesting one because it proves a person rather than a cat.

### What counts as an event

- **Motion and door** — only the transition *to* on. Motion clearing is not
  something that happened.
- **Buttons and locks** — any change to a real state. An `event` entity's state
  is the timestamp of the press, so every press is a change.
- **Never** — `unknown`, `unavailable`, or a state appearing with no previous
  state. That last one is Home Assistant waking up, not activity, and without
  the guard every restart would fabricate a burst of events.

## Setup

Install through HACS, restart once so Home Assistant picks up the new
component, then add **Home Signals** from Settings → Devices & Services and
choose the entities to watch. The list is editable afterwards via Configure.

### A note on choosing motion sensors

A Hue bridge exposes motion at two levels: the physical sensor (device
"Kitchen Sensor") and a grouped room or zone (device "Kitchen", model "Room").
Every physical trip also fires its room group, so selecting both posts every
event to the feed twice. Pick one level. The physical sensors are usually the
right choice for a feed, because they name exactly where something happened;
the grouped ones are the better source for presence logic.

## Installing

HACS → three-dot menu → Custom repositories → this repo, category
**Integration**.

## Licence

MIT
