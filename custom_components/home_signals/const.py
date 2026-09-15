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
