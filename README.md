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

## `sensor.needs_you`

What a human has to do, and nothing that is merely true.

The governing rule: **status is ambient and permanent, actions are
conditional and dismissable, and never both.** The Bins tile says "Tomorrow ·
Garden waste" all week; this says "bins out tonight" for one evening, and
clears when you do it. On a good day it is zero and the band disappears —
a dashboard that is permanently red stops being read.

- **State** — how many things need doing.
- **`items`** — the rows, each with `id`, `title`, `detail`, `icon`, `accent`
  and `action_label`. A Spectra `list` renders these directly, so the shape
  is a contract and not free to drift.

What it reports, each optional and off unless configured: bins out (only the
evening before, when it is actionable), overdue chores, batteries under a
threshold one row each, water softener salt, and everything offline as a
**single** row — twenty-seven unavailable entities is one problem, an
integration being down, and twenty-seven rows would bury everything else.

### Water softener salt

A twin-cylinder softener alternates: one side works while the other
regenerates. So a single side running down is normal, and both running down
together is not — one number cannot express both, which is why there are two
thresholds.

- **Alert when every side is at or below** (default 40%) — the trip to buy a
  bag. Raised as an alert.
- **Alert when any one side is at or below** (default 25%) — the earlier,
  sharper warning, answerable with the bag already in the garage.

Either rule produces **one** row, never one per side: filling the machine is
a single errand whichever cylinder prompted it. Both are set in the
integration's own options, alongside the battery threshold. With no sensors
configured, or none of them readable, the check contributes nothing — a
softener that cannot be read is not reported as full.

### Dismissing

Three actions: `home_signals.dismiss`, `home_signals.snooze` (with `hours`)
and `home_signals.reset`. All take the row's `id`.

They are actions rather than state inside a card because the panel, a phone
and a wall button all have to clear the same row — a browser cannot be where
that memory lives. Dismissals survive a restart, and they are keyed to the
**occurrence**: dismissing `bin_2026-09-16` clears tonight's bins and lets
next week's come back.

## `sensor.system_health`

What is wrong with the house's plumbing, as opposed to its jobs. Ambient
status, so it stays true for as long as it is true and is never dismissable.

- **State** — how many kinds of problem there are.
- **`items`** — rows for a card.
- **`low_batteries`, `offline`, `updates_pending`** — the raw lists, with
  entity ids and areas, plus a count of each.

The raw lists are the point. An agent asking "what is offline?" wants entity
ids, not a sentence assembled for a card — and an agent never looks at a
card. That is the whole reason both of these are entities rather than card
logic: **a card is only true while somebody is watching it.**

Entities in an entity category, and the `update`, `button`, `scene`, `script`
and `automation` domains, are excluded from the offline count. They go
unavailable constantly and nobody acts on it. Anything else noisy can be
listed under "Never report these as offline".

## `sensor.security_status`

Is the house shut, as one of three colours: `green`, `amber` or `red`. It
exists so a tab can be a colour rather than a sentence — the panel is read
from across the room, and "is the house shut?" is one question.

- **State** — `green` (every watched lock locked and every watched contact
  closed), `amber` (something is open or unlocked, and has been for less
  than the grace period), `red` (the same, for longer).
- **`detail`** — one line for a tab summary: "All secure", "Front door",
  "2 unlocked, 1 open".
- **`since`** — when the house last stopped being shut, or `null`.
- **`items`** — rows for a card, each carrying its own `since` so the card
  can render a live "unlocked for 12m" without this sensor updating.
- **`unlocked`, `open`, `not_reporting`** — the raw lists, plus counts.

Amber is deliberate rather than a rounding error. A door is open because
somebody is walking through it; going straight to red would make red mean
"somebody came home", and a colour that cries wolf is a colour nobody looks
at. Red is that same door still open once nobody could plausibly still be
carrying anything in.

The flip from amber to red is scheduled for the exact moment it is earned,
not left to the five-minute scan. A five-minute grace enforced by a
five-minute tick could mean waiting ten.

A lock that cannot be read is neither proof of a problem nor proof of
safety, so it holds the light at amber and never drives it red. It appears
under `not_reporting`.

**Locks** default to every lock in the house, so a new one is covered
without anyone remembering to come back to the options. **Door and window
contacts** are opt-in only: a house's binary sensors include the fridge, the
boiler and the washing machine door, and a light that goes red because
somebody is making a sandwich teaches people to ignore it.

This does not replace the "Front door unlocked" alert card, which is the
thing that asks somebody to do something about it. Status is ambient;
actions are actions.

## Setup

Install through HACS, restart once so Home Assistant picks up the new
component, then add **Home Signals** from Settings → Devices & Services and
choose the entities to watch, point Needs you at your bin and chore sensors, and pick the locks and door contacts the security light should hold to account. Everything is editable afterwards via Configure.

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
