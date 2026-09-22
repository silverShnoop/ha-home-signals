"""Constants for Home Signals."""

DOMAIN = "home_signals"

CONF_ENTITIES = "entities"
CONF_MAX_EVENTS = "max_events"

DEFAULT_MAX_EVENTS = 20

# Event kinds. The rail draws its icon from the kind, so these are a contract
# with the frontend, not free text.
KIND_BUTTON = "button"
KIND_LOCK = "lock"
KIND_MOTION = "motion"
KIND_DOOR = "door"
KIND_OTHER = "other"

# --- Needs you -------------------------------------------------------
#
# `items` is a contract with the frontend: a Spectra `list` renders each row
# straight from this shape, so the keys are not free to drift.
CONF_BATTERY_THRESHOLD = "battery_threshold"
CONF_BIN_SENSOR = "bin_sensor"
CONF_TASKS_SENSOR = "tasks_sensor"
CONF_IGNORE_UNAVAILABLE = "ignore_unavailable"

DEFAULT_BATTERY_THRESHOLD = 20

# A twin-cylinder water softener runs one side while the other regenerates,
# so the two readings mean different things together than apart. Both sides
# low is a shopping trip; one side low is a refill you can do from the bag
# already in the garage. Two thresholds, because one number cannot say both.
CONF_SALT_SENSORS = "salt_sensors"
CONF_SALT_BOTH_THRESHOLD = "salt_both_threshold"
CONF_SALT_ONE_THRESHOLD = "salt_one_threshold"

DEFAULT_SALT_BOTH_THRESHOLD = 40
DEFAULT_SALT_ONE_THRESHOLD = 25

# --- Done today ------------------------------------------------------
#
# A to-do entity remembers what was completed and, mostly, not when:
# `local_todo` stamps each item because iCalendar has a field for it, Bring
# does not. So "what did we get done today" has to be watched as it happens
# rather than filtered out of the list afterwards.
CONF_DONE_LISTS = "done_lists"

# --- Presence --------------------------------------------------------
#
# A person reads "Unknown" when no tracker of theirs is reporting at all
# -- not "away", which is a reading, but nothing. The card draws that in
# the warning colour, and on this panel yellow is a promise that
# something wants doing. So the job has to exist, or the colour is a lie.
#
# A grace period because a phone can be in a tunnel, on a plane, or
# rebooting, and none of those is a job.
CONF_PEOPLE = "people"
CONF_PRESENCE_GRACE_MINUTES = "presence_grace_minutes"

DEFAULT_PRESENCE_GRACE_MINUTES = 60

# --- Security --------------------------------------------------------
#
# A traffic light rather than a list, because the panel is read from across
# the room and "is the house shut?" is one question. Amber is the grace a
# real house needs — somebody is carrying the shopping in — and red is the
# same fact once that excuse has expired.
CONF_SECURITY_LOCKS = "security_locks"
CONF_SECURITY_OPENINGS = "security_openings"
CONF_SECURITY_GRACE_MINUTES = "security_grace_minutes"

DEFAULT_SECURITY_GRACE_MINUTES = 5

SECURITY_GREEN = "green"
SECURITY_AMBER = "amber"
SECURITY_RED = "red"

# --- Appliances ------------------------------------------------------
#
# A washing machine and a tumble dryer are the same problem twice: a plug
# that reports watts, and a person who wants to know whether the thing is
# running and whether it has left them a job. So one spec, configured twice,
# rather than two features that drift apart.
CONF_APPLIANCES = "appliances"
CONF_WASHER_POWER = "washer_power"
CONF_WASHER_PLUG = "washer_plug"
CONF_WASHER_DOOR = "washer_door"
CONF_WASHER_LEAK = "washer_leak"
CONF_WASHER_ENERGY = "washer_energy"
CONF_DRYER_POWER = "dryer_power"
CONF_DRYER_PLUG = "dryer_plug"
CONF_DRYER_DOOR = "dryer_door"
CONF_DRYER_ENERGY = "dryer_energy"

# What a kWh costs, right now. One sensor for the house rather than one per
# machine: the price of electricity is not a property of the washing machine,
# and two copies of it would be two things to point at a new tariff.
#
# Octopus publishes it as `sensor.octopus_energy_electricity_<meter>_current_rate`
# in GBP/kWh, but nothing here knows that -- any sensor reading money per unit
# will do, which is the whole reason it is configured rather than found.
CONF_RATE_SENSOR = "rate_sensor"

# Enter fast, leave slow. Crossing START is decisive and instant; dropping
# below IDLE only counts once it has held for IDLE_MINUTES, because the gaps
# inside a wash are minutes long and are not the end of anything.
CONF_START_WATTS = "appliance_start_watts"
CONF_IDLE_WATTS = "appliance_idle_watts"
CONF_IDLE_MINUTES = "appliance_idle_minutes"
CONF_MIN_MINUTES = "appliance_min_minutes"
CONF_MIN_KWH = "appliance_min_kwh"

DEFAULT_START_WATTS = 8
DEFAULT_IDLE_WATTS = 4
# Five rather than ten: a late "done" is harmless, but so is a wrong one if
# it can be corrected, and waiting ten minutes to be told the wash finished
# is its own kind of wrong. Every cycle records its own longest lull, so
# this becomes a measured number after the first real load.
DEFAULT_IDLE_MINUTES = 5
DEFAULT_MIN_MINUTES = 10
DEFAULT_MIN_KWH = 0.05

APPLIANCE_OFF = "off"
APPLIANCE_IDLE = "idle"
APPLIANCE_RUNNING = "running"

CLEANING_GREEN = "green"
CLEANING_AMBER = "amber"
CLEANING_RED = "red"

SERVICE_LAUNDRY_HUNG = "laundry_hung"
ATTR_LOAD_ID = "load_id"
ATTR_APPLIANCE = "appliance"

# What a machine is doing right now, read off the draw. The bands come
# from a real wash measured end to end on 19 Sep 2026 -- see PHASE_BANDS
# in appliance.py for the trace they were taken from.
PHASE_FILL = "fill"
PHASE_HEAT = "heat"
PHASE_SPIN = "spin"
PHASE_TUMBLE = "tumble"

# Who said the washing was hung. The activity feed answers "where are
# people", and only one of these is evidence of a body in the room: the
# button is screwed to the wall beside the machine, while a screen could
# be the panel in the kitchen or a phone on a train.
ATTR_SOURCE = "source"
SOURCE_BUTTON = "button"
SOURCE_UI = "ui"

# The three levels a job can be at, and the only colours on the panel
# that mean the house is asking a person for something.
#
# Named rather than numbered because they are ORDERED, and because the
# numbers they replace were accent slots -- decorative roles that said
# which tab a card belonged to. Sharing one namespace is how a row came
# to claim an alarm by naming a hue, and how repainting a decorative
# slot would have silently repainted a leak.
#
# The name is a promise about a timeline, and that is the whole test a
# new row has to pass:
#
#   ATTENTION  needs doing today or tomorrow. Real, but it keeps.
#   WAITING    something is paused or degrading until a person acts.
#   CRITICAL   damage or risk is accruing now.
#
# A thing that needs no doing at all takes no level. It is information,
# it belongs on a card, and it is not a Needs-you row.
LEVEL_ATTENTION = "attention"
LEVEL_WAITING = "waiting"
LEVEL_CRITICAL = "critical"

# How loud each level is, for picking the worst in a list. Ordered by
# what the level means, never by anything incidental about its name.
LEVEL_LOUDNESS = {
    LEVEL_ATTENTION: 1,
    LEVEL_WAITING: 2,
    LEVEL_CRITICAL: 3,
}

# Accent roles, by meaning rather than colour. These are DECORATIVE and
# may never carry a level: 3 positive, 5 secondary series.
ACCENT_OK = 3
ACCENT_INFO = 5

SERVICE_DISMISS = "dismiss"
SERVICE_SNOOZE = "snooze"
SERVICE_RESET = "reset"

ATTR_ITEM_ID = "item_id"
ATTR_HOURS = "hours"


# --- What the day cost ------------------------------------------------
#
# Octopus publishes the previous complete day as one sensor whose `charges`
# attribute carries every half-hour of it. That array is reduced once, here,
# rather than by a template per tile.
#
# Two facts about the data decide the shape of everything downstream:
#
# It is not "yesterday". The reads land when Octopus gets them -- one day
# behind, sometimes two -- so the sensor reports the date it is actually
# describing and how late that is, and never the word.
#
# And there is no "now". A live house-wide figure needs an Octopus Home Mini
# or Home Pro; without one the API has nothing for today at all. So today is
# an optional pair of inputs rather than something this computes: point them
# at the Home Mini's accumulative sensors and today appears, leave them empty
# and it does not.
CONF_ENERGY_COST_SENSOR = "energy_cost_sensor"
CONF_ENERGY_TODAY_COST = "energy_today_cost"
CONF_ENERGY_TODAY_KWH = "energy_today_kwh"

# The hours the house is asleep, for the baseline. Whatever it is drawing
# between midnight and six is the floor under every other figure, and it is
# the one number in this whole integration that no tariff change touches.
BASELINE_UNTIL_HOUR = 6

# Past this, the reported day is stale and the sensor goes quiet rather than
# showing Saturday's total on Thursday. A card that has stopped being updated
# looks exactly like a card that is working, which is the failure worth
# designing against -- and the panel already knows how to render nothing.
ENERGY_STALE_DAYS = 3

# Days of settled history kept, and the fewest that may be called an average.
# A mean of two days is not an average, it is two days.
#
# Five weeks rather than two, so a month window has a month to average over
# and still has room for the days Octopus delivers late or not at all.
ENERGY_HISTORY_DAYS = 35
ENERGY_MIN_DAYS_FOR_AVERAGE = 3

# The two windows a person actually compares a day against: "is this a normal
# week for us" and "is this a normal month". Both are trailing and both
# exclude the day being judged -- see _window.
ENERGY_WEEK_DAYS = 7
ENERGY_MONTH_DAYS = 30

# How many days the card's chart draws. Shorter than the history on purpose:
# thirty-five bars across a card read from a doorway is a texture, not a
# shape, and the history exists to be averaged rather than drawn.
ENERGY_SERIES_DAYS = 14

# The floor's norm looks at the trailing fortnight, not the whole history.
# A norm over five weeks would absorb a slow creep and keep reporting it as
# normal; over a fortnight it tracks the drift, so only a real spike fires
# the row. The creep itself is a different question, answered by comparing
# the last week of nights against the week before -- see `baseline_trend_pct`.
ENERGY_NORM_DAYS = 14

# Within this, the day is "about the same" rather than up or down. Without a
# band, a normal day reads as 3% down and the comparison becomes noise that
# always says something.
ENERGY_SAME_PCT = 5

# --- Something was left on overnight ----------------------------------
#
# The baseline is the floor under every other figure, so a night whose floor
# is well above the usual one is a thing that was left running. This is the
# one signal in the house that no tariff change touches and no price chart
# would ever have shown.
#
# It is deliberately LATE and says so. Without a live meter the settled day
# arrives one or two days behind, so the row names the night it is about
# rather than implying "now" -- see the row's detail. Once a today source
# exists (a Home Mini, or Hildebrand's Usage Today read at six in the
# morning) the same comparison becomes near-live with no change here.
CONF_BASELINE_EXCESS_PCT = "baseline_excess_pct"
DEFAULT_BASELINE_EXCESS_PCT = 40

# Nights needed before there is a "usual" at all. More than the cost average
# wants: a floor is the quietest number the house produces, so a norm built
# from three of them is one odd night away from being wrong.
ENERGY_MIN_DAYS_FOR_NORM = 5

# --- Climate ----------------------------------------------------------
#
# One source, and it is the one that controls the heating. Where two
# thermometers in the same room disagree — and in this house they disagree
# by different amounts in different rooms — the reading that matters is the
# one the loop acts on, because that is the number the radiator obeys. A
# mean of two sensors with unequal, unknown offsets is a figure nobody can
# trace back to anything.
CONF_CLIMATE_ZONES = "climate_zones"

# The bar every room is drawn against. Fixed rather than fitted to the day's
# own spread: a scale that moves with the data makes the coldest room look
# identical every morning and two screenshots impossible to compare. Rooms
# outside it clamp, which is honest because the row states the number too.
CONF_CLIMATE_SCALE_MIN = "climate_scale_min"
CONF_CLIMATE_SCALE_MAX = "climate_scale_max"

DEFAULT_CLIMATE_SCALE_MIN = 15
DEFAULT_CLIMATE_SCALE_MAX = 25

# A radiator calling for heat this long without the room moving is not
# heating the room. Air in the radiator, a valve that has seized shut, or a
# pin stuck down — all of them burn gas and none of them says so anywhere.
#
# The rise is deliberately small. This is not asking whether the room got
# warm; it is asking whether it moved at all.
CONF_CLIMATE_STUCK_MINUTES = "climate_stuck_minutes"
CONF_CLIMATE_STUCK_RISE = "climate_stuck_rise"

DEFAULT_CLIMATE_STUCK_MINUTES = 30
DEFAULT_CLIMATE_STUCK_RISE = 0.2

# Tado holds a manual override until somebody ends it. An override set
# without a timer is not a setting, it is a schedule that has stopped
# running, and a day is long enough to be sure it was not a deliberate
# afternoon.
CONF_CLIMATE_MANUAL_HOURS = "climate_manual_hours"

DEFAULT_CLIMATE_MANUAL_HOURS = 24

# Outside, measured here rather than forecast for the region. Optional: with
# no sensors the indoor rows are unchanged and the ventilation answer is
# absent rather than guessed.
CONF_OUTDOOR_TEMP = "outdoor_temperature"
CONF_OUTDOOR_HUMIDITY = "outdoor_humidity"

# Below this difference in absolute humidity, opening a window moves no
# meaningful amount of water either way, and the honest answer is "it makes
# no odds" rather than a direction. Grams per cubic metre.
VENTILATION_BAND = 0.5
