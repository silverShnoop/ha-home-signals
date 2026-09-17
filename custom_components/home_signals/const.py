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

