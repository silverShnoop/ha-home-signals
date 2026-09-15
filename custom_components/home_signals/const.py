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

