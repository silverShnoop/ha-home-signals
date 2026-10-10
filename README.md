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
  `entity_id`, `name`, `area`, `kind`, `state` and `at`. Consecutive events
  from the same entity fold into one row — the cap counts rows, not
  happenings — and a folded row adds `count` and `first_at` (when the run
  began; `at` is the newest).
- **`tracked_count`** — how many entities are being watched, so a card can tell
  "nothing has happened" apart from "nothing is configured".
- **`by_area`** — the last hour, per room: `{"Hall": {"kind", "at", "times"}}`,
  where `times` is epoch seconds, newest first. It is what a floor plan
  reads. The rail's `events` are capped by length — twenty rows is about ten
  minutes of an ordinary evening — and a plan that fades over an hour cannot
  be drawn from ten minutes. So it has its own record, capped by **age**
  instead (60 minutes, at most 120 per room), in bare numbers so a busy hour
  stays a few kilobytes. An entity with no area is on the rail and not here:
  there is nowhere to draw it.

`kind` is one of `button`, `lock`, `motion`, `door`, `other`, or what a
camera saw: `person`, `animal`, `vehicle`, `crying`, `camera`. It is a contract
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
- **What a camera sees** — only the transition *to* on, and never
  configured. Every camera's detection sensors join the feed by being on a
  device with a camera, the same discovery as `sensor.camera_sightings`, so
  the card and the feed agree about what a camera noticed. The kind is
  read off the sensor's name (`Baby crying` is `crying`), and the row reads
  "Riley's Room · crying". A camera's plain **motion** sensor is left
  out: a person, an animal and a cry are said by name, and pixel motion on
  a camera is mostly the light changing and night vision switching over.
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
- **`items`** — the rows, each with `id`, `title`, `detail`, `icon`, `level`
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
a single errand whichever cylinder prompted it — and the row is **attention**
whichever rule raised it. Levelling the two rules differently made the
colour report the shopping (a bag to buy, or a bag already in the garage)
rather than the urgency, and spent the panel's loudest colour on a chore.
Critical here is for water on the floor and doors left unlocked. Both are set in the
integration's own options, alongside the battery threshold. With no sensors
configured, or none of them readable, the check contributes nothing — a
softener that cannot be read is not reported as full.

### A night that was not like the others

The floor is what the house draws with everybody asleep, so a night well
above it is something that was left running. `sensor.energy_day` works out
the excess; this turns it into a row past `baseline_excess_pct` (40% by
default, high enough to stay rare — a house has ordinary nights that run
10–20% over for no reason worth chasing).

**The row is late, and says so.** Without a live meter the settled day
arrives one or two days behind, so the detail names the night —
`Sat 19 Sep · 420 W against a usual 280 W` — rather than implying last
night. Hiding the lag would make it a worse row: *"something is on now"* is
a claim this data cannot support, and *"something was on, on Saturday"* is
one it can. A stale day produces no row at all, for the same reason the
sensor drops its own state.

It is dismissable and keyed to the night, because "I know what that was" is
a real answer to it — and answering for Saturday must not silence Sunday.
No snooze: a night is over, and there is nothing to come back to later.

### Salt can be snoozed, never dismissed

Salt does not wait. It runs out, and then the softener passes hard water
through the house until somebody notices the limescale. The row is only
ever true when there is a bag to fetch from the garage or a bag to buy,
and it clears itself the moment the level comes back up.

But the softener reports late: its salt reading moves a day or more after
the tank is filled, so the row went on asking for salt already in the
machine. It offers a 24-hour **Snooze** to ride out that lag — long
enough to cover it, short enough that a tank that really is low comes
straight back. Like every snoozed row it still colours the card and the
Maintenance tab, because the reading still says low.

It carries `snooze_only: True`, so a dismissal — from the `dismiss`
service, an automation, or a suppression restored from before — is
ignored. Only a timed snooze hides it, and the snooze always runs out.

### Dismissing

Three actions: `home_signals.dismiss`, `home_signals.snooze` (with `hours`)
and `home_signals.reset`. All take the row's `id`.

They are actions rather than state inside a card because the panel, a phone
and a wall button all have to clear the same row — a browser cannot be where
that memory lives. Dismissals survive a restart, and they are keyed to the
**occurrence**: dismissing `bin_2026-09-16` clears tonight's bins and lets
next week's come back.

## The levels

A card may state any fact it likes. But a **level** is a promise that
something wants doing, and on this panel the thing that wants doing lives
in `Needs you` and nowhere else.

| Level | The promise it makes |
| --- | --- |
| `attention` | needs doing today or tomorrow. Real, but it keeps. |
| `waiting` | something is paused or degrading until a person acts. |
| `critical` | damage or risk is accruing now. |
| `notice` | something you asked for is under way or has landed. Nothing gets worse while it waits. |

`notice` is the quietest, and the only one that promises no deadline. It
exists for work the house did on somebody's behalf — an AI task reading a
recipe off a page — where somebody still has to look at the answer. It is
blue. See [AI tasks](#sensorai_tasks).

The name is the test a new row has to pass. Before there were three, ten
of the twelve rows were the same "warning" whatever they meant, and the
two that were not spent the loudest colour in the house on a chore and on
thirty-one entities that had gone quiet.

**Every levelled thing must have a row behind it**, or the colour is a
lie: a job that exists only on the panel, that nobody can clear from a
phone, and that no amount of doing the thing will make go away. That rule
is why `sensor.people_status` exists — the card draws an unlocatable
person at a level, so the row has to be real — and why a tab tile wears
the level of what is actually there instead of a fixed colour.

`sensor.people_status` also carries **`presence`**: for each watched
person, where they are and since when. The Who's home card reads its
"3h ago" from here rather than from the person's own `last_changed`,
which Home Assistant resets on every restart. The time moves only when
the person does. A restart keeps it, and so does a spell of silence
shorter than `presence_grace_minutes` that ends in the same place.
Unknown takes the same "quiet since" time as the Needs you row.

**It cuts the other way too.** A thing that needs no doing takes no level.
An open appliance door used to be drawn in ochre and has no row and never
should: a machine spends half its life with the door open. That chip is
neutral now. So are pending updates, which `sensor.devices` lists with no
level at all — and `loudest()` skips anything that is not a level rather
than ranking it last, so a card full of information leaves the tile
uncoloured.

**A level is not an accent.** An accent is decorative: it says which tab a
card belongs to. The two used to be the same six numbers, which is how a
row came to claim an alarm by naming a hue. A Needs-you row publishes
`level` and never `accent`, and `tests/test_levels.py` asserts both halves
over every state the washer can be in.

### The card decides; the row and the tab follow

The card is the thing that owns a need, so its sensor is where the level is
worked out — once. Each card that can ask for something has its own sensor
publishing `level` (its state is that level, or `clear`), `tab`, and `jobs`
(the Needs-you rows behind it):

| Card | Its sensor |
| --- | --- |
| Washing machine, Tumble dryer, Dishwasher | `sensor.washing_machine`, `sensor.tumble_dryer`, `sensor.dishwasher` |
| Front door | `sensor.security_status` |
| Who's home | `sensor.people_status` |
| Bin calendar | `sensor.bins_status` |
| Home Tasks | `sensor.tasks_status` |
| Water softener | `sensor.softener_status` |
| A Frigate camera | `sensor.camera_status`, by its `cameras` map |
| Batteries | `sensor.batteries_status` |
| Devices | `sensor.devices` |
| Meals (prep) | the meal prep sensor |
| Whichever card started an AI task | `sensor.ai_tasks`, by its `cards` map |

The card's outline reads its sensor's `level`. `sensor.needs_you` collects
every card's `jobs` and works out no level of its own; it publishes
`tab_<tab>` — the loudest card `level` on each tab — and `summary_<tab>` —
the loudest job and how many more — for the rail buttons.
When each worked its own level out they drifted: the Cleaning tab skipped a
dead plug whenever a pad was wet, a silent lock coloured the door with no
row, and bins, overdue chores and a lost phone had rows that coloured
nothing.

**Snooze** is Needs you's business: it hides the row, and the card and tab
stay coloured because the thing is still true. **Done** goes to the card
that owns the job — "Bins out" tells `sensor.bins_status` — so the card, the
tab and the row clear together.

A lock or door that stops reporting is its own row, `silent_<entity>`, at
the level the door already wears.

### What left when the levels arrived

**The overnight-baseline row.** "Something was on overnight" reported a
night that had already happened, with no action beyond Dismiss — which is
the one thing a row may not be: it did not need doing. The figures are
still published on `sensor.energy_day`, where the Electricity card reads
them and always did. What left is the claim that they were a job.

## `sensor.ai_tasks`

Reading a recipe off a page takes a model the best part of a minute, and
splitting it into what can be done ahead takes another. The card used to
wait for both inside a sheet that said "Reading the page…" and nothing
else: close the sheet and the answer arrived to nobody, and nobody could
tell whether anything was happening at all.

So the card hands the work over with `home_signals.start_ai_task`, which
answers at once with a `task_id` and runs the action in the background:

```yaml
action: home_signals.start_ai_task
data:
  title: Recipe from a link
  action: script.recipe_import       # anything that answers with a response
  data: {url: "https://…"}
  card: meals                        # which card started it
  tab: kitchen                       # whose rail button it colours
  label: recipe                      # the answer key that names what came back
  open: true                         # the card can show the answer again (default)
  kind: import                       # what the answer is, for the card that opens it
  require: slug                      # the answer must have this, or it is Failed
  missing: No recipe found on that page   # ...and this is what the row then says
  then:                              # optional: a second action, fed from the first
    action: script.recipe_split
    pass: {recipe: slug}             # its field <- the first answer's key
    unless: already                  # skip it when the first answer says this
```

**Running is already blue.** The sensor's state is `running`, and the card,
the tab and a row — "Recipe from a link · Running · step 1 of 2" — wear
`notice` from the moment it starts, so it can be seen from anywhere that the
house is working on something you asked for. The row has Dismiss, which
quietens it while it runs; when it lands that is news again, and it comes
back as Done or Failed.

**Finished is a `notice`, on all three, for two minutes.** The card
(`cards: {meals: notice}`), the tab (`tab_kitchen` on Needs you) and a row —
"Recipe from a link · Done · Chicken pie". Two minutes after it lands, all
three go back to how they were on their own; a notice is news, not a chore.
Needs you sorts its rows loudest first — critical, waiting, attention,
notice — so blue is always at the bottom.

The row has two buttons. **Dismiss** (`secondary_action`) clears all three
at once. **Open** is there only when the answer can be shown again —
`open: true` when the task was started, and a card to show it on. It is not
a service: it carries `open_task`, `card` and `tab`, and the panel switches
to that tab and the card opens the answer, which it reads with
`home_signals.ai_task_result`. Opening it clears all three too. A task with
nothing to open has Dismiss alone, as its main button. `kind` says what the
answer is, for the card that opens it.

**Every finished task says which way it went** — `Done` or `Failed` at the
start of the row's detail, a tick or an alert for its icon, and `outcome:
success | failure` for anything reading the row. A failure is a notice
too: something you asked for did not happen, and only the person who asked
can try again. It has nothing to open, so it has Dismiss alone, and the
reason is in the row. Failing is not only raising: an answer that carries its
own `error`, or that lacks the `require` key (a photo with no recipe on it
answers with an empty name), found nothing, and is Failed too, saying
`missing`. A second step that answers with an error or `mode: error` leaves
the task Done, and says the second step did not finish. If the first action answered and only the second
failed, the task is done, and says so — the recipe is in the box, just not
split.

Answers are kept in storage, not in the state, for their two minutes, and
the recorder never holds a whole recipe. A task still running at a restart
comes back failed and says why: the call it was waiting on died with the
old instance.

## Where `sensor.system_health` went

It summarised the Maintenance tab: a level, a row per problem, and the raw
lists. Each piece now lives with the card it belongs to, so nothing works
out a Maintenance level twice:

- **The Batteries card's data** — every battery with `low` decided on the
  same threshold as the jobs, `low_batteries`, `battery_count`,
  `battery_threshold` — is on `sensor.batteries_status`.
- **Salt** is `sensor.softener_status`.
- **The raw lists for an agent** — `unavailable_entities` and
  `updates_pending` / `update_count` — are on `sensor.devices`. Pending
  updates are a fact, not a job: no level, no row.
- **The rail button** reads `tab_maintenance` and `summary_maintenance` on
  `sensor.needs_you`.

## `sensor.camera_sightings`

When each camera last saw something. The state is when any camera last
saw anything; `sightings` maps each detection sensor to
`{on, since, started}`, where `since` is the moment the sighting ended,
or the moment it began while it is still going on.

```yaml
since: {entity: sensor.camera_sightings, attribute: sightings,
        key: [binary_sensor.rileys_room_camera_person, since],
        format: relative}
```

A detection sensor's own `last_changed` is the last time it changed state
for any reason, and that is not the same thing. A Reolink in privacy mode
makes every detection sensor unavailable, and opening the lens brings them
back as `off`. Read off `last_changed`, the panel said "Crying 5s ago" in
a house where nobody had cried. A restart and an integration reload do the
same. So only the two edges are written down, a detection starting and a
detection ending, and the record is restored across restarts. A sensor
going unavailable while it was seeing something ends the sighting there;
a sensor coming back from unavailable as `off` is not a sighting of
anything. One that has never seen anything has no entry at all.

Which sensors count is not configured: a detection is a binary sensor on
a device that also has a camera, named for something seen (person, animal,
vehicle, crying, motion) or of a seeing device class (motion, occupancy,
presence, sound). The second half matters: a kiosk wall tablet has a camera
of its own, and its charging and connectivity sensors are not sightings. That is true of a Reolink now and of
Frigate's object sensors later, and a new camera is picked up when it
appears in the registry, without a restart.

## `sensor.devices`

How many of the house's devices are answering, counted as **things** rather
than entities. A car that loses its cloud connection is eighteen entities and
one car.

- **State** — how many devices are offline or partly offline.
- **`connected`, `offline`, `partial`, `total`** — the counts. A device is
  *offline* when every entity it has is unavailable, *partial* when some are.
- **`problems`** — one row per device that is not fully answering: `name`,
  `area`, `network`, `state`, `detail` (for a partial device, what is missing:
  `No temperature`, or `5 of 8 missing`) and `since`.
- **`networks`** — `online`/`offline`/`partial` per network: Hue, Zigbee,
  Tado, Cast, and everything else as `Wi-Fi & cloud`.
- **`level`** — `attention` while anything is not answering, else `null`. The
  same devices are already behind the offline row in `Needs you`.

**One count, three places.** The offline row in `Needs you`, the offline row in
`System health` and this sensor all come from the same device scan, so they
say the same number ("7 devices offline, 3 partly"). The row used to count
entities and read "31 entities offline" beside a card saying 7. System
health's raw `offline` list stays entity-level, for an agent asking which
entity ids are down.

**`since` is remembered, not read.** Home Assistant resets every `last_changed`
on a restart, so a bulb dead for a week would read as having died at the last
reboot. This writes the time down when a device first stops answering and
restores it across restarts. A device already down the first time it looks
gets `null` rather than a guess, and keeps it until it comes back.

Not counted: service devices (backups, AI models, the sun), and the registry
entries that are groups rather than things — Hue rooms and zones and Cast
speaker groups, which would report one dead bulb or speaker twice. A Tado
"Zone" is counted: it is the room's heating control, not a group, and when it
goes unavailable nothing else says so. Buttons and
updates, and anything under "Never report these", don't count towards a device
being unavailable. Diagnostic entities (signal strength, battery) only decide
for a device that has nothing else — a ZHA button, whose presses are events
rather than entities — so a bulb whose signal reading goes quiet is still a
working bulb, and a button that stops checking in still shows as offline.

## `sensor.energy_day`

What the house's electricity cost, reduced once. Octopus publishes the
previous complete day as a single sensor whose `charges` attribute carries
all forty-eight half-hours of it; everything a card or an assistant wants
about that day is already in there, and reducing it here rather than in a
card is the difference between one Python function and a template per tile.

- **State** — the cost of the day being reported. `device_class: monetary`.
- **`for_day`, `for_date`, `days_late`, `stale`** — which day that is.
- **`kwh`, `usage`, `standing_p`, `standing_cost_year`, `peak_slot`,
  `peak_kwh`, `slots`**.
- **`block_names`, `block_hours`, `block_kwh`, `block_cost`, `block_days`** —
  the day in four six-hour blocks, and the last seven days of them.
- **`baseline_watts`, `baseline_share`** — what the house draws asleep.
- **`week_*`, `month_*`, `vs_week_*`, `vs_month_*`** — the day against its
  own two windows; the comparison that works without a live meter.
- **`recent_days`** — the rolling five weeks the windows are built from.
- **`months`** — the last twelve calendar months, newest first, already
  shaped as list rows (`name`, `sub`, `value`) beside the raw `cost`, `kwh`,
  `days` and `days_in_month`. A month with nothing recorded is still listed,
  as "Not filled yet"; one short of its days says how many it has. Totalled
  from `recent_days` and kept past them, so a month outlives the five weeks
  of days it was made from.
- **`month_blocks`** — the same twelve months, oldest first, shaped like
  `block_days` so the stacked time-of-day chart draws them: each month's
  four blocks summed under the month's standing charge, which is the first
  segment, so the bar and the total under it are the bill; and a `note` on
  a part-month. An empty month keeps its column with no blocks.
  `month_block_names` names the stack, standing charge first.
- **`cost_series`, `kwh_series`, `baseline_series`, `series_labels`** — plain
  arrays, oldest first, for a chart to read straight off.
- **`baseline_norm`, `baseline_excess_pct`, `baseline_trend_pct`,
  `baseline_high`** — the floor, against its usual and against itself.
- **`today_cost`, `today_kwh`, `same_time_cost`, `today_vs_pct`,
  `today_vs_text`** — only where there is a live meter to read.

### It is not "yesterday", and must never say so

The source sensor is called `previous_accumulative_cost` and the obvious
reading is yesterday. The reads land when Octopus gets them: one day behind,
and quite often two — the first time this was looked at, on a Monday
lunchtime, the freshest complete day was **Saturday**.

So nothing here says the word. It reports the date it is actually
describing, taken off the charges themselves, and publishes `days_late`
beside it. Past `ENERGY_STALE_DAYS` the state goes to `unknown` and the cell
disappears, because a figure that has stopped being updated looks exactly
like one that is current — and that is the failure worth designing against,
not the missing data.

### What the house draws asleep

`baseline_watts` is the mean of the midnight-to-six slots, and
`baseline_share` is what that would be as a share of the whole day.

It is the figure this turned out to be worth doing for. On the first day
read, the house drew a steady **286 W** overnight and never dropped below
234 W — 6.86 kWh held over a day, **48% of everything it used**, about £1.70
a day. No tariff change touches that number and no price chart would ever
have shown it; it falls out of the half-hours as a by-product.

### There is no "now", and that is not something shaping can fix

A live house-wide figure needs an **Octopus Home Mini** (or a Home Pro).
Without one the API has nothing for today at all — the meter records
half-hourly, but the consumption endpoint only serves days Octopus has
already settled. Two smart plugs can say what the washing machine is doing;
nothing in the house can say what the house is doing.

So today is an optional pair of *inputs* rather than something this
computes. Point `energy_today_cost` and `energy_today_kwh` at the Home
Mini's accumulative sensors and today appears; leave them empty and the
`today_*` attributes are **absent** rather than null, so a card renders a
hole and an assistant can tell "not measured" from "measured as nothing".

### Today against a whole yesterday is a trap

At nine in the morning, *"today £1.20, yesterday £3.99"* reads as a good day
and means nothing whatsoever. Every partial day beats every complete one.

So today is compared against the settled day **up to the same time of day**,
which is only possible because the half-hours are here to be cut. On the
measured Saturday: £3.99 all in, but £1.68 by noon. A today sitting at £1.90
is 13% **above** that — while against the whole day it reads as 52% under,
and the card congratulates somebody for a day that is running hot.

The comparison is named for the day it actually used — `about Sat 19 Sep` —
because on a two-day lag "vs yesterday" would be wrong twice a week.

### Something was left on overnight

`baseline_watts` on its own is a number nobody has a feel for. Against a
`baseline_norm` — what this house usually draws asleep — it becomes the one
thing in here anybody can act on:

```
baseline_watts        420
baseline_norm         280
baseline_excess_pct    50
baseline_text         "420 W overnight against a usual 280 W"
```

Past a threshold it also becomes a `Needs you` row. Three rules keep it
honest.

**The norm is a median, not a mean.** Guests, a wash left running, an
evening of the oven on — a mean would let one such night lift the very bar
it should have failed against. The median leaves the bar where it was, so
the next bad night is still caught.

**A night is never in its own norm**, for the reason the cost average gives:
included, it is partly measured against itself and the excess understates.

**Five nights before there is a "usual" at all** — stricter than the cost
average's three. A floor is the quietest number the house produces, and this
figure's whole job is to be what an odd night fails against, so a norm one
odd night away from being wrong is worse than no norm. Below that,
`baseline_norm` and the excess are absent and no row can fire.

And `baseline_text` only mentions the norm when there is something to say. A
sentence reading "3% under usual" every single morning is how a figure stops
being read.

#### The floor in money, and the day split around it

A floor in watts is not a fact anybody can act on. What it *costs* is:

```
floor_kwh   6.9    floor_cost_text  "£1.70"   floor_cost_year  621
rest_kwh    7.3    rest_cost_text   "£1.81"
standing_p   48    standing_cost_year 175
```

The three sum to the day's total, to the penny. That is deliberate — a
table whose rows do not add up to the figure above them is a table nobody
trusts — so `rest` is the **remainder** rather than a second
multiplication, and any rounding penny lands there rather than going
missing.

Priced at the day's own average rate (`usage / kwh`) rather than at
whatever the tariff says now. On a flat tariff they are the same number; on
a variable one the day's own rate is the only one that can divide up the
day's own money.

The split is **absent when the floor projects to more than the day used** —
a perfectly flat day is the boundary. That means Octopus delivered a
partial day, and a split whose parts exceed the whole is fiction.

`floor_cost_year` exists because £1.70 a day is ignorable and £621 a year
is not, and they are the same fact. `standing_cost_year` is there so the
row beside it is the same kind of figure — and because the difference is
the point: the floor is a year somebody can go and reduce, the standing
charge is a year they cannot, which is worth knowing before starting the
hunt.

#### Going stale empties the attributes too, not just the state

Every card on the panel is built out of attributes — `cost_text`,
`week_cost_text`, `floor_cost_text` — and not one of them looks at the
state. So dropping the state past `days_late > 3` and leaving the
attributes populated would keep drawing Saturday's figures on Thursday
underneath a state nobody reads, which is precisely the failure the
staleness rule exists to prevent.

Past that point the sensor publishes only what *explains* the silence:
`for_day`, `for_date`, `days_late`, `stale`, and `months` — a finished
month does not go stale. Plus `recent_days`, which
nothing draws and the restore reads back — it is the one thing here that
cannot be recomputed from a source sensor holding a single day, so throwing
it away while Octopus is quiet would cost the house its history at the next
restart.

**Today is not stale, and does not go with it.** `today_cost` and
`today_kwh` come off a different meter, so they keep arriving when Octopus
stops — and they are the freshest figures in the house, which is no thing
to drop because another source went quiet. The *comparison* does go:
`today_vs_text` and `same_time_cost` are made of the settled day, so they
are exactly as stale as it is.

#### Against a night somebody remembers

`baseline_vs_prev_text` compares the floor to **the night before the one
being reported** — `20 W up on Fri` — with a two-watt band so it does not
read "1 W up" every morning.

Not against the median. A median is the right thing to fire a row off and
the wrong thing to hand a person, because nobody remembers their median.
And on a two-day lag the previous night is not last night, so it is named
rather than implied.

#### A spike and a creep are different questions

`baseline_excess_pct` measures the night against a **trailing fortnight**, so
a slow drift upwards moves the norm with it and stops firing the row. That is
right for catching a spike and useless for catching a creep — and a creep is
a fridge seal going, a pump starting to fail, something plugged in during the
summer that never got switched off again.

So `baseline_trend_pct` asks the other question: the median of the last week
of nights against the median of the week before. Seven nights at 280 W
followed by seven at 340 W is a 21% trend and *not* a spike, and the pair of
figures is what tells those apart. It needs a fortnight of nights before it
says anything.

#### Where the power went, and roughly when

A day's total says nothing about the day. Two days at the same total can be
a morning of laundry and an evening of the oven, and only one of those is
something anybody would change. So the day is also cut into four even
six-hour blocks:

```
block_names  ["Overnight", "Morning", "Afternoon", "Evening"]
block_kwh    [1.72, 5.08, 4.29, 3.08]
block_cost   [0.43, 1.26, 1.06, 0.76]
```

Even sixes rather than the hours a person would name — "morning" is not six
o'clock to everybody — because the four are meant to be **compared**, and
blocks of different length cannot be.

They **sum to the day's own usage**, by construction: they are the same
half-hours counted once each. Nothing is projected or apportioned, which is
the difference between these and `baseline_watts` — that takes the overnight
*rate* and asks what a whole day of it would cost. Both are true; only one
of them adds up, and only the one that adds up can be stacked.

`block_days` carries the last seven days the same way, oldest first, each
with its own totals already rendered. Seven rather than fourteen: every
column carries four segments and two lines of figures beneath it, and a
fortnight of those is a texture rather than a week you can read.

#### Recovering the days nobody was writing down

The source sensor holds one day at a time, so a week of blocks would
normally take a week to arrive — and every figure added here would start
empty on the day it shipped.

But Home Assistant has been recording that sensor's states all along,
attributes and all, and its attributes **are** the forty-eight half-hours.
So on startup the last `ENERGY_BACKFILL_DAYS` of its own history are read
back and put through the *same parser* that reads it live. A second reader
for old days would be a second thing to keep right.

Deliberately the sensor's own history rather than the statistics Octopus
also publishes: those would have to be addressed by a statistic id this
integration would have to know how to construct, and nothing here knows it
is talking to Octopus. This reads the entity it was already configured with.

It is best effort and runs off the startup path. No recorder, an excluded
entity, a purge that has already been past — each means fewer columns, which
the card already draws correctly, and none of them is worth a slow or broken
startup.

#### The arrays are shaped here, not in the card

`cost_series`, `kwh_series` and `baseline_series` are plain arrays, oldest
first, capped at fourteen days — a Spectra `chart` reads them with no
`auto-entities`, no `apexcharts-card` and no Jinja, which is the whole point
of the split.

Fourteen rather than thirty-five because a chart on a wall panel should draw
a shape rather than a texture. The history is longer than the series on
purpose: it exists to be averaged, and only part of it to be drawn.

Every series is the same length as `series_labels` **by construction**, not by
luck: `_remember` writes all four figures together, and `_restore` drops any
row missing one. A row missing a figure would be skipped by that series and
kept by the labels, drawing every bar after it against the wrong day — and a
chart off by one is worse than a chart one day shorter.

Three series, and the pair worth drawing together is **cost and the floor**.
Cost over kWh was the obvious pairing and is one fact drawn twice: on a flat
tariff cost *is* kWh × 24.78p, so the line and the bars have the same shape
and the second one says nothing. (On a tariff that varies, the gap between
them becomes the information, and the pairing earns itself back.) The floor
shares no axis with anything — it is a wattage, on its own scale, under bars
made of money — and it is the series that answers a question the bars cannot:
whether the thing underneath every day is creeping upwards.

## The long run, from statistics rather than from here

Everything else in this integration is built from entity states. The week,
the month and the breakdown are built from **long-term statistics**, and
that is a hard limit rather than a preference: a week against the week
before, a month against last month, and what a socket used over seven days
are all questions about the past, and the source sensors hold only the
present. Octopus publishes one settled day at a time; a plug publishes a
running total that resets when it is re-paired.

Statistics are the right store and already exist. Home Assistant keeps them
for ever, survives a purge of the states they came from, and — unlike
anything this integration could accumulate in an attribute — they are
already correct for the days before it was installed.

### Which statistics is not ours to guess

The obvious move is to construct the ids from the Octopus naming, and
`usage.py` deliberately does not: nothing else here knows what a tariff
provider is called, and a house that changes supplier should not need a
code change.

Instead it reads the **Energy dashboard's own preferences** — the grid
consumption and cost statistics, and the named device-consumption list.
That is the householder having already declared *"this is my meter, and
these are the things I am monitoring"*, in the one place Home Assistant
asks them to. Both the flat and the nested `flow_from` grid shapes are
read, because both are live in the wild.

**The pleasant consequence:** adding a monitoring socket to the Energy
dashboard adds it to the breakdown. No option to set, nothing to redeploy.

### Nothing is reported before its window is full

```
week7_kwh / week7_cost_text        the last seven days
prev7_kwh / prev7_cost_text        the seven before those
week7_vs_prev_text                 "12% above the week before"
avg_week_cost_text / avg_weeks     the average week, and how many it had
avg_week_note                      "over 6 weeks", or "over the year"
breakdown / breakdown_metered_pct  the week's wedges, and how much is watched
```

Each key is **absent until its window is genuinely full**. A four-day
"week" under a seven-day heading is the figure somebody quotes back at you
a month later, and a card renders nothing perfectly well.

- Fewer than `ENERGY_MIN_WEEKS_FOR_AVERAGE` whole weeks and there is no
  average, only `avg_weeks` saying how few there were. The **current** week
  is never averaged in — it is partial by definition and would drag every
  average down.
- `avg_week_note` says what it actually averaged. "A 7-day average" implies
  a year of evidence that does not exist in the first month.

Months are **not** among them. `_fold_months` already totals the days by
calendar month and does it better than a statistics query could: it
survives a day Octopus revises, keeps the fuller count as a month's days
age out of the five-week history, carries the four blocks per month, and
needs no Energy dashboard configured. A second source for one figure is
two figures waiting to disagree.

The refresh runs on the half-hourly timer, not when the attributes are
read. Every figure here is a recorder query and **a card must never be the
thing that runs one**. It is also wrapped: a card nobody is looking at must
not be able to take the sensor down.

### The breakdown's honest part is the remainder

```
Washing machine     2.00 kWh   50p    3.9%
Tumble dryer        4.43 kWh   £1.10  8.6%
Everything else    44.99 kWh   £11.14 87.5%
```

Two plugs account for a tenth of this house, so the slice that matters is
the one nothing is watching — and it is **named** rather than left as the
gap between a total and some parts. `breakdown_metered_pct` says the same
thing as a number.

Each device is capped at the grid total and the remainder floored at zero:
a plug and a meter are different instruments with different clocks, and a
breakdown whose parts exceed its whole is worse than no breakdown. The
remainder also takes the **remaining money** rather than its own
multiplication, so the wedges sum to the week's total to the penny —
rounding each independently put them a penny over.

### The week and the month, which can disagree

Two trailing windows rather than one blended average:

```
week_cost  7.50   week_kwh  28.3   week_days   7   vs_week_pct   0
month_cost 4.90   month_kwh 18.5   month_days 27   vs_month_pct 53
```

They exist as a pair because they answer differently exactly when it
matters. A cold snap moves the week and leaves the month alone; a new
appliance moves both. One blended figure splits the difference and says
neither. Both exclude the day being judged, for the reason given below.

`week_days` and `month_days` are published because until the history has
filled a month, a "month average" is the mean of whatever there is — and
something has to say so rather than the label implying thirty days of
evidence that do not exist yet.

The history keeps **five weeks**, which is a month plus room for the days
Octopus delivers late or not at all.

### The average is the comparison that always works

Without a Home Mini there is no today, but "is this day normal?" is still
answerable: the settled day against the house's own recent average.

`recent_days` is a rolling fortnight, kept across a restart. It is the one
thing here that cannot be recomputed — the source sensor only ever holds a
single day, so the average has to be accumulated as the days go past, and
losing it on every restart would lose the only comparison available, every
time Home Assistant updates. It is restored from the published attribute
rather than through `ExtraStoredData`, the same way `pending` and `finished`
come back on the appliances: the rows are worth publishing anyway, so there
is no second copy to keep in step.

Two rules keep it honest. **A day is never in its own average** — included,
it is measured partly against itself, and four days of £1 beside one of £2
puts the average at £1.20 and calls the spike 67% up when it is double. And
**fewer than `ENERGY_MIN_DAYS_FOR_AVERAGE` days is not an average**, it is
some days; below that the comparison is absent rather than invented.

There is also a band, `ENERGY_SAME_PCT`. Without it a perfectly ordinary day
reads as "3% down", and a comparison that always has something to say is one
nobody reads.

## `sensor.washing_machine_cycle` (and the tumble dryer, and the dishwasher)

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
  and energy, for a card to list. Each entry in `finished_today` also
  carries **`hanging`**, whether that load is still in the queue: the card
  marks the row, and `pending` is a separate attribute a row has no way of
  asking about itself.
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

### `finished_today` is a day, plus anything still to hang

Midnight is a fact about the clock, not about the washing. A wash that ended
at 23:40 and is still on the floor at 00:10 has not stopped needing hanging,
so it stays on the list past its own day — otherwise the card would say
"1 to hang" over a list with nothing in it, and *which load* is the one
question the list answers.

What takes it off is being hung, not the date rolling over again: the
moment the queue lets go of it, an out-of-date load leaves the list. A load
hung on the day it ran stays, because today's list is still today's list.

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

What *does* change when power is restored is whether the leak is still a
job. `leak_alarm` is the pad being wet **and** nobody having switched the
plug back on since it went wet; that, not `leak`, raises the critical
`Needs you` row and puts the Cleaning tab at `critical`. Only a real off → on counts,
so a plug the cutoff never reached keeps the alarm — and the row then says
the power is still on rather than claiming a cut. The pad going wet again
re-arms it.

`leak_since` is when the pad went wet, kept across a restart and cleared
when it dries. Both leak rows lead with how long — "Wet for 12 min" — because
how long water may have been on the floor is what decides how worried to be.

Stood down is not the same as quiet. The cutoff fires only on the pad
*going* wet, so while it stays wet a second leak would cut nothing. Until it
dries there is an `attention` row, "leak sensor still wet", and the Cleaning
tab is at `attention` too. It clears itself when the pad dries.

### A dryer only tumbles, and says so

The washer's phase bands were measured on the washer, off one wash, so
the dryer does not use them — its element runs at a different power and
they would label every dryer cycle confidently and wrongly.

But *"it is doing the only thing it does"* is still an answer to what it
is doing, and a running dryer showed nothing at all. So it reports one
phase, `tumble`, opened when the run opens and grown for as long as the
run lasts — the same shape a classified phase has, so the card needs to
know nothing about the difference.

`tracks_phases` picks the classifier; `only_phase` names the single
phase for a machine that has just the one. A machine sets one or the
other, never both.

### The dishwasher is the dryer again, with one split it can make

`sensor.dishwasher` is the same machine a third time, configured from the
`dishwasher_*` options. It is the dryer's shape: clean dishes are put away
straight out of the rack, and the rack coming out *is* the door opening, so
`queues_loads` is false and there is no button.

**No door, no full drum.** A finished load fills the drum only where a door
is configured, because a drum nothing can empty would raise a `Needs you`
row that nothing could ever clear. Without the contact sensor the card says
when it ran and what it cost, and no more; the day a door sensor is set in
the options, "Dishwasher needs emptying" starts appearing on its own. (The
config flow always said a missing door meant never full. Now the code does
too, for every machine.)

**Heating or washing, and nothing finer.** A dishwasher's draw tells exactly
two things apart on any machine: the element, in kilowatts, and the pump, in
tens of watts. So it reports `heat` and `wash` and nothing else, split at
600 W — the geometric mean of a generous pump (200 W) and a small element
(1.8 kW), the same rule the washer's bands use. Pre-wash, main wash and
rinse are all the pump, and naming them would be reading the order things
happened in as evidence. There is no `fill`: it fills at the same draw as
everything after it. `phase_bands`, `base_phase` and `opening_fill` are the
spec keys that say this; the washer keeps the class defaults.

**A higher idle floor.** A dishwasher goes quiet mid-programme — a soak, a
pause before the rinse, a passive dry — for longer than a washer ever does,
so its idle floor is at least 20 minutes whatever the shared option says. It
is a guess until the first load: `longest_lull_seconds` is the number to set
it from.

### What the wash cost

The plug already counts the kilowatt-hours. Given a sensor that says what a
kilowatt-hour costs — `rate_sensor`, which Octopus publishes as
`..._current_rate` — a finished cycle also records what it cost.

```
cost       0.33          # £, for arithmetic and for anything that is not a card
cost_text  "33p"         # how a person says it
```

Both sit on each entry in `finished` and `finished_today`, with
`last_cost`/`last_cost_text` for the most recent one and
`cost_so_far`/`cost_so_far_text` while a cycle is in flight. The two forms
are the same split `system_health` already makes between its rendered rows
and its raw lists: an agent asked what the wash cost wants `0.33`, and a
panel read from three metres away wants `33p`. One is derived from the other
on a single line, so they cannot drift.

The formatting is here rather than in the card because the switch at a pound
is a *rule about the number*, and the card's value language has no room for
one — a `suffix` cannot change its mind at 100p.

**This is the figure worth having**, more than any house total. "£3.99
yesterday" is a number about an abstraction; "that wash cost 33p" is a number
about the thing somebody is standing in front of with a basket. It also
answers the question people actually have about a washing machine, which is
not *what does it cost* but *is it worth putting a half load on* — and two
cycles a week apart answer that.

#### Priced as it goes, not at the end

A wash spans four or five half-hours. On a tariff that moves between them
there is no single rate the cycle ran at, so multiplying the finished total
by whatever the price happens to be when the drum stops would be a number
about the end of the wash wearing the label of the whole thing. Each reading
is priced as it arrives instead, at the rate in force then, and the slices
are added up.

On a flat tariff that is the same arithmetic done more often and lands on the
same figure. On Agile it is the only version that is true. Writing it this
way now means nothing — not this sensor, not the card — changes on the day
the tariff does.

One approximation is left, deliberately: if the plug stops reporting for a
while, the kilowatt-hours that arrive when it comes back are priced at the
rate current then, because nothing recorded which of the rates inside the gap
applied to which part of it. Bounded by how long the plug was out, and on a
single-rate tariff not an error at all.

#### A cost that cannot be known is absent, not zero

If any part of a cycle passes through unpriced — no `rate_sensor`
configured, the tariff sensor `unavailable` while the integration reloads,
the plug's own total resetting mid-wash because it was re-paired — the
cost is **dropped for that cycle** rather than reported short.

The tempting behaviour is the wrong one. Half a wash priced and reported as
the whole thing reads as a cheap wash, and there is nothing on the card to
tell the two apart. A missing figure leaves a hole, which the panel already
knows how to render and which is the truth. `unknown` and `unavailable`
become nothing, not the words.

There is no configuration for this and no override. A panel earns the right
to be believed about money by never being nearly right.

## Where `sensor.cleaning_status` went

It gave the Cleaning tab a level and a line of text, worked out from the
washing machines alone — so bins on the same tab never showed. The rail
button now reads `tab_cleaning` (the loudest card level on the tab) and
`summary_cleaning` (the loudest job, and how many more) on
`sensor.needs_you`, which covers every card on the tab.

## Laundry in `Needs you`

Every action an appliance can ask of you is a Needs you row, and only a Needs
you row. The card states facts and offers one optional control; it never
carries a to-do. Waiting loads are one row per machine — "3 loads need
hanging" — and its Hung clears the oldest, so the count drops by one.

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

### Backfilling the morning from the recorder

Watching only knows what happened while it was watching, and the first
morning of anything is the morning it knows nothing about.

Home Assistant records state history for everything, but a to-do list's
**items are not attributes** — they come from a service call — so the
recorder has never seen them. What it *has* seen is the **activity
entity**: Bring publishes `event.<list>_activities`, whose attributes
name the exact items in each change, and those are recorded like any
others. So on startup the history is read back to local midnight and the
part of today that happened before we were looking is filled in.

The entity is found by slug rather than configured — `todo.phoenix` →
`event.phoenix_activities` — and confirmed against the state machine, so
a list without one simply has no backfill and reads no history at all.

Two traps in that history, both of which put the wrong time on the right
item:

- **The event's time is its state, not `last_changed`.** A restart
  republishes the entity, so `last_changed` is when Home Assistant came
  back and the state is when the shopping happened. Take the wrong one
  and the morning is dated to the reboot.
- **The same event therefore appears twice**, so items are deduplicated
  on uuid.

And two rules about which time is the true one:

- Where an item was removed **more than once** today — bought, put back,
  bought again — the row is dated to the **last** one. The first is a
  completion that was undone.
- An item that **stamps itself** keeps its own timestamp. Its own answer
  beats any reconstruction from an activity feed.

The name comes from the **list**, not the feed: Bring's `itemId` is its
catalogue id, which is in German for anything added from their
suggestions — "Milch" on a card that says Milk everywhere else.

Only items that are **still completed** are taken. The history records
what once happened; the list is the current fact, and it wins.

### The day ends at midnight, twice over

A timer clears the record at local midnight. The rows are *also* filtered
on the way out, because nothing was running at midnight after an overnight
reboot and that timer never fired — without the second check the panel
comes up showing yesterday under today's heading.

## `sensor.camera_status`

What a Frigate camera is asking somebody to do. A camera card states facts:
what it can see, what it saw, what Frigate made of it. Two things it sees
are jobs, and this is where their rows come from.

- **Bring the parcel in** (`attention`). Frigate keeps tracking an object
  that has stopped moving, so a camera's `package` occupancy stays on for
  as long as the parcel is there. The row is that occupancy and nothing
  else: it appears when a parcel is seen and clears itself when the parcel
  is gone. Snooze only. Pressing a button does not bring a parcel in, and
  a Done that hid the row while it sat in the rain would leave the card
  and the row disagreeing.
- **<Camera> camera has stopped** (`waiting`). No frames from the camera,
  unbroken, for five minutes, read off Frigate's camera fps sensor (or
  that sensor being unavailable, which is Frigate itself gone). Footage is
  not being recorded until somebody looks, which is the promise `waiting`
  makes. A shorter gap is a camera rebooting itself, and not news. While a
  camera is stopped it raises no parcel row: its occupancy sensors hold
  whatever they last said, and a row from them would be a guess.

The cameras are **found, not configured**: every camera the Frigate
integration has registered, read off its unique id (`<entry>:camera:<name>`)
rather than its state, because an unavailable camera loses its attributes
and an unavailable camera is half of what this is for. A camera added to
Frigate is watched the moment it is registered.

The state is the loudest level, or `clear`, with `level`, `tab`
(`security`) and `jobs` as every card's sensor has. **`cameras`** maps
Frigate's name for each camera to its own `name`, `entity`, `level` and
`jobs`, so a card reads its own camera and a parcel at the back door does
not colour the gate:

```yaml
outline: {entity: sensor.camera_status, attribute: cameras, key: [front_gate, level]}
```

The parcel's time is the occupancy sensor's `last_changed`, which a
restart resets. A parcel there before a restart reads as arriving at it.

## `sensor.security_status`

Is the house shut, as one of three colours: `green`, `amber` or `red`. It
exists so a tab can be a colour rather than a sentence — the panel is read
from across the room, and "is the house shut?" is one question.

- **State** — `green` (every watched lock locked and every watched contact
  closed), `amber` (something is open or unlocked, and has been for less
  than the grace period), `red` (the same, for longer).
- **`detail`** — one line for a tab summary: "All secure", "Front door",
  "2 unlocked, 1 open".
- **`level`** — `critical` past the grace period, `waiting` inside it, and
  `null` when the house is shut, so the tab tile wears a level rather than
  translating the colour itself. It is read off the same `_status` branch
  the rows are, so the tile and the list cannot disagree.
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

## Writing recipes to Mealie

`home_signals.save_recipe` and `home_signals.delete_recipe` do what Home
Assistant's own Mealie integration cannot: create a recipe, change one, and
delete one. The integration reads recipes and imports one from a link, and
that is all. So a family recipe with no web page, or a quantity that needs
fixing after an import, meant opening Mealie's own interface. The meal card
exists so that nobody has to.

```yaml
action: home_signals.save_recipe
data:
  name: Nana's curry          # leave out `recipe` to create a new one
  servings: 4
  total_time: 1 hour
  ingredients: |
    2 onions
    1 tin chopped tomatoes
  method: |
    Fry the onions until soft.
    Add the tomatoes and simmer for 40 minutes.
response_variable: saved      # {slug, recipe_id, name}
```

Name an existing recipe with `recipe` (its slug or id) to change it. Only the
fields you send are touched: a save that leaves out `method` leaves the
method alone, and one that sends an empty method clears it.

**The address and token are borrowed from the Mealie integration**, not set
up again here. A second copy of the token is a second place for it to go
stale, and Home Assistant has already been told where Mealie is.

**An ingredient line that has not changed keeps Mealie's parse of it.** On
import Mealie works out the food, the unit and the quantity of each line,
which is what its shopping lists add up. An edited line becomes plain text.
Rewriting every line as text on every save would throw that parse away the
first time anybody fixed a typo in the method.

**Pasted numbering is dropped.** Mealie numbers the method itself, so a
pasted "1. Heat the oil" would otherwise show as "1. 1. Heat the oil".
Bullets go the same way. A quantity at the start of an ingredient ("1.5 kg
potatoes") is not a list number and stays.

A rename moves the recipe to a new slug, so the answer is read from what
Mealie sent back rather than from what was asked for.

## Finding a meal: the recipe index, tags, favourites, last made

`home_signals.recipe_index` answers every recipe in Mealie with what a
picker filters on:

```yaml
recipes:
  - recipe_id: 464a3de1-…
    slug: chicken-fajitas
    name: Chicken fajitas
    total_time: 45 minutes
    image: pfLf                  # set when Mealie has a photo
    tags: [Dinner, Quick, Chicken, Mexican]
    ingredients: [500g chicken thighs, 2 peppers, …]
    last_made: 2026-09-01        # local date, or null
    date_added: 2026-08-20
    favourite: true              # the token user's favourite
    source: https://...          # where it was imported from, when it was
tags: [Chicken, Dinner, Mexican, Quick]
```

Mealie's recipe list carries no ingredients, so each full recipe is read
once and kept until Mealie's `updatedAt` for it changes. A repeat call reads
only the list.

`save_recipe` also takes `tags` (names; replaces the recipe's tags, creating
any Mealie does not have, matched ignoring case) and `favourite` (true or
false, for the Mealie user whose token the integration uses). Either works
on its own: `{recipe: chicken-fajitas, favourite: true}` changes nothing
else.

Tags are matched by slug, as Mealie matches them, so "quick", "Quick" and
"'Quick'" are one tag; a tag found under another spelling is renamed to the
one asked for. `home_signals.prune_tags` deletes every tag no recipe uses and
answers `{deleted: [names]}`.

`home_signals.mark_made` records that a recipe was eaten on a day (today
unless `date` is given). It never moves the date backwards, so marking an
older meal after a newer one is harmless. The meal scripts call it each
night for the day that has just gone.

## Prep ahead: noting what can be done early

`save_recipe` takes an optional `prep`, which says which of a recipe's steps
can be done ahead of time. Prepping is optional, so the method itself is
never moved or rewritten: the notes sit beside it, by step number.

- `{"mode": "split", "steps": [...]}` -- one note per step that can go
  ahead: `n` (its number in the method, from 1), `ahead_max` and
  `ahead_min` (hours), `minutes`, `keeps` (e.g. "Fridge") and `source`
  ("page" or "house"). A step that does two things names its halves:
  `ahead` (done early) and `cook` (left for the stove). What only applies
  when it was made ahead is two lines, because it is said at two times:
  `store`, how to keep it, said at the prep ("Cover and chill"), and
  `if_ahead`, what that changes on the night, said at the stove ("Take it
  out 20 mins before cooking"). A part of the dish made ahead as a whole
  can carry `reheat`, and `reheat_at`, the step it takes the place of on
  the night; its storing goes in the `store` of its last step.
- `sections` (a separate field) titles the method's groups of steps, the
  way Mealie keeps them: `[{"n": 1, "title": "The chicken"}, {"n": 3,
  "title": "The sauce"}]`, each title on the step its group starts at. The
  titles given are all there are; `[]` clears them. The index carries them
  back as each recipe's `sections`.
- `{"mode": "none"}` -- looked at, and there is nothing worth doing ahead.
- `{"mode": "order"}` -- take the notes off again.

They live in the recipe's `extras` as `prep`, and the recipe index carries
them back as each recipe's `prep`. A split saved before this, with no `n`,
moved its prep steps to the front of the method; it is still read, by its
order.

## Where a recipe came from

Each recipe keeps, in `extras` as `provenance`, where it came from and what
AI did to it. `save_recipe` takes both:

- `source`: `{kind, url, from}`, kind being `page`, `video`, `photo`,
  `said`, `typed` or `written` (by AI, from a name). A new recipe with
  nothing said is `typed`. `import_recipe` records the page or video it read,
  and a video's steps are marked as read by Mealie's own AI.
- `ai`: `{what, by, model, note, mark, steps}` records one event -- `read`,
  `wrote`, `split`, `tagged` or `checked` -- with the time. `mark`
  (`interpreted`, `created` or `enhanced`) goes on the steps listed, or all
  of them. A step a person rewrites loses its mark.

The index carries it as each recipe's `provenance`: `{source, events,
marks}`, marks keyed by step number.

## Prep sessions: `sensor.meal_prep`

A session is one sitting in which the prep for one or more meals is done,
and it is exactly **one Home Tasks item** with a deadline, named "Prep:" and
the meals, with each meal's steps in its description. The house plans to do
prep in as few sittings as it can, so one item per session rather than one
per meal.

| Action | What it does |
| --- | --- |
| `home_signals.save_prep_session` | Create or change a session (`id`, `due`, `items`) and its task. No items removes it. |
| `home_signals.remove_prep_session` | Forget a session and remove its task. |
| `home_signals.prep_done` | Tick the session's task. |
| `home_signals.prep_settings` | Meal times (default 07:00, 12:00, 17:00), prep times (Sunday 16:00, weekday evenings 19:30) and the list. |

The task is the job; the sensor keeps the plan behind it and reads the
task's status back, so ticking it on a phone clears everything. Deleting the
task counts as done too -- somebody has answered it.

A session raises a `Needs you` row, and the sensor's `level`, only while it
matters:

- `attention` -- due today and not done;
- `waiting` -- past due, not done, and a meal it was for is still ahead.

The row belongs to the Kitchen tab and its meals card, so it is published
as `tab_kitchen` and `card_meals` on `sensor.needs_you` like any other row.

Once its meals have passed it takes no level at all, and a day later it is
forgotten (the task stays on the list).

## Recipe photos and saved photos

**`GET /api/home_signals/recipe_image/<recipe_id>/<size>`** passes a recipe's
photo through from Mealie, with `size` one of `tiny`, `min` or `original`. The
meal cards cannot reach Mealie themselves, because the app sits behind
ingress. The path needs Home Assistant's authentication. An `<img>` cannot
send a token, so the card signs the path first with `auth/sign_path` and
uses the signed URL. The browser keeps each photo for a day.

**`home_signals.save_photo`** keeps a photo sent by a card (base64 or a data
URL, JPEG, PNG or WebP, under 3 MB) in local media under
`home_signals/<folder>/`. It answers with the `media_content_id` an
`ai_task.generate_data` attachment takes. Only the newest twelve per folder
are kept.

**`save_recipe` takes `image`**, the recipe's own photo in the same form
(base64 or a data URL, JPEG, PNG or WebP, under 3 MB), and puts it on the
recipe in Mealie in place of any photo it had. A photo that is not one is
refused before anything is written, so a new recipe is never left half made.

## Setup

Install through HACS, restart once so Home Assistant picks up the new
component, then add **Home Signals** from Settings → Devices & Services and
choose the entities to watch, point Needs you at your bin and chore sensors, and pick the locks and door contacts the security light should hold to account. Everything is editable afterwards via Configure.

### What to point at Octopus

Four of the settings expect a tariff integration, and nothing here knows it
is Octopus — any sensor of the right shape will do, which is why they are
configured rather than found.

| Setting | Octopus entity | Without it |
| --- | --- | --- |
| `rate_sensor` | `..._current_rate` | A cycle records its kWh and no cost |
| `energy_cost_sensor` | `..._previous_accumulative_cost` | No `sensor.energy_day` at all |
| `energy_today_cost` | `..._current_accumulative_cost` | No today; the average still works |
| `energy_today_kwh` | `..._current_accumulative_consumption` | As above |

The last two need an **Octopus Home Mini** or Home Pro. The first two work
on any account.

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
