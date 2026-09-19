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

# Who said the washing was hung. The activity feed answers "where are
# people", and only one of these is evidence of a body in the room: the
# button is screwed to the wall beside the machine, while a screen could
# be the panel in the kitchen or a phone on a train.
ATTR_SOURCE = "source"
SOURCE_BUTTON = "button"
SOURCE_UI = "ui"

# Accent roles, by meaning rather than colour. 1 alerts, 2 warnings,
# 3 positive, 5 secondary series.
ACCENT_ALERT = 1
ACCENT_WARN = 2
ACCENT_OK = 3
ACCENT_INFO = 5

SERVICE_DISMISS = "dismiss"
SERVICE_SNOOZE = "snooze"
SERVICE_RESET = "reset"

ATTR_ITEM_ID = "item_id"
ATTR_HOURS = "hours"

