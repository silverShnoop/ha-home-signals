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

It is normally worked out from the domain and device class, but an entity
may **declare its own `kind`** as an attribute and be believed — as long as
it names one of those five, because an unrecognised kind would render as no
icon at all. A timestamp sensor is the case that needs it: a ZHA button has
no event entity to be watched (ZHA creates none), so a press only reaches
the feed by being stamped onto a sensor, and by domain and device class
that sensor says *when* something happened and nothing about what.

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

## `sensor.washing_machine_cycle` (and the tumble dryer)

Whether an appliance is running, worked out from nothing but the watts its
plug reports.

- **State** — `off` (no power at the plug), `idle`, or `running`.
- **`pending`** — the loads that have finished and not been hung up, one
  entry each, keyed to the cycle that produced them. Always empty on a
  dryer, which has no such state.
- **`drum_full`** — whether there is still washing inside. The door is
  what empties it, with one exception: a machine started again on a full
  drum is washing that load a second time, so it is running rather than
  full, and this cycle refills it. The claim is only parked: if the run
  never becomes a wash — cut short, the plug pulled, or Home Assistant
  restarted mid-cycle, which never resumes one — the fullness goes back
  exactly as it was.
- **`queues_loads`** — whether a finished load leaves a job behind after
  the drum is emptied. True for the washer, false for the dryer.
- **`finished` / `finished_today`** — completed cycles with their duration
  and energy, for a card to list.
- **`longest_lull_seconds`, `peak_watts`** — what the last cycle actually
  looked like, for tuning the thresholds against a real wash.

### Three facts, not one

They are tracked separately because three different things answer them:

| Fact | Answered by | Cleared by |
| --- | --- | --- |
| Is it running | the plug's power sensor | the draw dropping and staying down |
| Is there washing inside | the door contact | opening the door |
| Is there washing to hang | a person | the wall button, the Needs you row, or the load going back in for another wash |

Emptying the drum never clears the hanging list, and hanging never empties
the drum. Conflating them is the obvious simplification and it is wrong: you
carry the washing to the airer in one trip and hang it in another.

### The dryer is the same machine, one state shorter

Both machines run a cycle, both end it with a full drum, and on both the
door empties it. The washer has a **second** state after that one: washing
out of the drum still has to be hung, on a rack in another room, where the
machine cannot see it happen. So its loads queue and wait to be told.

A dry load is finished the moment it leaves the drum, and leaving the drum
*is* opening the door. There is nothing left to tell it, so a dryer queues
nothing and is given no button — `queues_loads: false` is that one
difference, and it is the only place the two are configured apart.

Queueing a dry load would put a row in `Needs you` that nothing in the
house could clear.

### Enter fast, leave slow

A cycle is not a continuous draw. A machine heats at 2kW, agitates at 200W,
rests at nothing, agitates, rests, soaks for minutes, then spins. Read
instantaneously, one wash looks like a dozen short cycles.

So the two thresholds are deliberately asymmetric:

- **Running** the instant the draw passes `start_watts` (8W). Nothing else
  on that plug draws 8W, and entering is cheap to get wrong.
- **Idle** only once the draw has stayed under `idle_watts` (4W) for
  `idle_minutes` **unbroken**. Leaving is what creates a load of laundry, so
  it is the one that has to be sure.

The gap between the two thresholds is the hysteresis: in that band, whatever
state it is already in wins, so a machine hovering around one number cannot
chatter.

### The door is what says a short run was not a wash

Unloading the machine draws about ten watts for four seconds as the
interlock lets go — over `start_watts`, so that blip used to be read as a
cycle starting, and "leave slow" then held the card on **Running** for the
full five-minute floor with the door standing open. Every unload, every
time. It also wiped the phase strip, which is kept after a cycle precisely
so somebody walking over can see what the wash did.

A washing machine cannot run with its door open, because the door is
interlocked. So a door that opens during a run proves that run is not a
wash in progress, and nothing starts a run while the door is open.

The guard is on **length**, and that is what makes it safe. A run already
past `min_minutes` is left alone — that is a real wash sitting in its quiet
wait with its record not yet written, and abandoning it would throw the
laundry away. A shorter run is one that would have been discarded anyway,
so this changes *when* the card stops saying "running" and never *whether*
anything is recorded. An abandoned run also hands back the fullness and the
timeline it borrowed, leaving the machine exactly as it found it.

A completed cycle only counts as laundry if it ran for `min_minutes` and used
`min_kwh` — a drain-only run, or somebody nudging the dial, is not a wash. And
a cycle interrupted by the plug going off is **abandoned, not finished**:
otherwise the leak automation cutting power would leave you a reminder to hang
up a load sitting in six inches of water.

### The idle floor is a guess, once

Five minutes is a starting value, not a measurement. Every cycle records its
own `longest_lull_seconds`, so after one real wash the number the floor has
to clear is something you read off the sensor rather than something somebody
picked. It is an option, so tuning it is a settings change.

### A wet leak sensor does not mean the power is off

Two independent facts, reported independently. A leak pad stays damp long
after the floor has been dealt with, and the cycle still has to be finished —
so `leak` and `powered` are never inferred from each other, and nothing here
stops power being restored while the sensor is still wet.

## `sensor.cleaning_status`

The same three colours as `security_status`, for the same reason: a tab on a
wall panel can be a colour before anybody reads a word of it.

- **red** — water on the floor.
- **amber** — a job: a drum to empty, washing to hang, or a machine left
  without power.
- **green** — nothing waiting.

## Laundry in `Needs you`

Every action an appliance can ask of you is a Needs you row, and only a Needs
you row. The card states facts and offers one optional control; it never
carries a to-do. Two loads are two rows, so hanging one leaves the other.

`home_signals.laundry_hung` clears one load — with a `load_id` for a specific
one, or without for the oldest, which is what the wall button sends. It is
deliberately **not** a dismissal: a dismissal is card-side memory that hides
a row while the thing behind it carries on being true, and the card would go
on saying "2 to hang" next to a list that had forgotten them. This clears the
load in the one place that counts them.

## `sensor.<list>_done_today`

What got ticked off a to-do list since midnight, one sensor per list. The
state is how many; `items` carries the rows in the same shape the list
itself hands the card, so the panel draws the completed section with the
same row renderer as the list above it.

### Why this is not a filter over the list

A to-do entity remembers **what** was completed and, mostly, not **when**.
`local_todo` writes a `completed` timestamp onto each item because
iCalendar has a field for it. Bring has none — the eighteen completed
items on the shopping list are eighteen items bought at some unknown point
over some unknown number of days.

So for half the lists in this house there is nothing to filter on, and
"what did we get done today" has to be watched as it happens and written
down. The sensor is a `RestoreEntity` for the same reason the activity
feed is: losing the record on every restart would empty the section
exactly when somebody wants to look at it.

It is watched rather than polled. A to-do entity's state is its
outstanding count, so ticking something off moves it, and that move is the
cue to re-read the completed items and see which ones are new.

### The first read after a restart is a census

This is the rule that matters, and getting it wrong is loud: stamp every
already-completed item with the moment you first read it, and the panel
opens with a fortnight of shopping under a heading that says today.

So an item with no timestamp of its own only counts if it **arrives** in
the completed set while the sensor is watching. An item that carries its
own timestamp is believed over the moment we happened to look — which is
also what lets a list that records one answer for the part of today that
happened before the restart.

Un-ticking removes the row again. Putting something back on the list is as
real an act as ticking it off was.

### The day ends at midnight, twice over

A timer clears the record at local midnight. The rows are *also* filtered
on the way out, because nothing was running at midnight after an overnight
reboot and that timer never fired — without the second check the panel
comes up showing yesterday under today's heading.

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
