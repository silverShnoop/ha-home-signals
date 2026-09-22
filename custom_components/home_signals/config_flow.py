"""Config flow for Home Signals."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BASELINE_EXCESS_PCT,
    CONF_CLIMATE_MANUAL_HOURS,
    CONF_CLIMATE_SCALE_MAX,
    CONF_CLIMATE_SCALE_MIN,
    CONF_CLIMATE_STUCK_MINUTES,
    CONF_CLIMATE_STUCK_RISE,
    CONF_CLIMATE_ZONES,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMP,
    DEFAULT_CLIMATE_MANUAL_HOURS,
    DEFAULT_CLIMATE_SCALE_MAX,
    DEFAULT_CLIMATE_SCALE_MIN,
    DEFAULT_CLIMATE_STUCK_MINUTES,
    DEFAULT_CLIMATE_STUCK_RISE,
    CONF_BATTERY_THRESHOLD,
    CONF_BIN_SENSOR,
    CONF_DRYER_DOOR,
    CONF_DRYER_ENERGY,
    CONF_DRYER_PLUG,
    CONF_DRYER_POWER,
    CONF_IDLE_MINUTES,
    CONF_IDLE_WATTS,
    CONF_MIN_KWH,
    CONF_MIN_MINUTES,
    CONF_RATE_SENSOR,
    CONF_START_WATTS,
    CONF_WASHER_DOOR,
    CONF_WASHER_ENERGY,
    CONF_WASHER_LEAK,
    CONF_WASHER_PLUG,
    CONF_WASHER_POWER,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_IDLE_WATTS,
    DEFAULT_MIN_KWH,
    DEFAULT_MIN_MINUTES,
    DEFAULT_START_WATTS,
    CONF_SALT_BOTH_THRESHOLD,
    CONF_SALT_ONE_THRESHOLD,
    CONF_SALT_SENSORS,
    CONF_SECURITY_GRACE_MINUTES,
    CONF_SECURITY_LOCKS,
    CONF_SECURITY_OPENINGS,
    CONF_ENTITIES,
    CONF_IGNORE_UNAVAILABLE,
    CONF_MAX_EVENTS,
    CONF_DONE_LISTS,
    CONF_ENERGY_COST_SENSOR,
    CONF_ENERGY_TODAY_COST,
    CONF_ENERGY_TODAY_KWH,
    CONF_PEOPLE,
    CONF_PRESENCE_GRACE_MINUTES,
    DEFAULT_PRESENCE_GRACE_MINUTES,
    CONF_TASKS_SENSOR,
    DEFAULT_BASELINE_EXCESS_PCT,
    DEFAULT_BATTERY_THRESHOLD,
    DEFAULT_SALT_BOTH_THRESHOLD,
    DEFAULT_SALT_ONE_THRESHOLD,
    DEFAULT_SECURITY_GRACE_MINUTES,
    DEFAULT_MAX_EVENTS,
    DOMAIN,
)

TITLE = "Home Signals"

# Anything that can mark a moment: motion and door contacts, locks, and the
# button/remote events that prove a person rather than a pet.
#
# Timestamp sensors are in as a narrow fifth case, because that is what a
# ZHA button has to be represented as -- ZHA creates no event entities, so
# a press only reaches an entity by being stamped onto one. The device
# class keeps the picker from filling with every sensor in the house.
_TRACKABLE = [
    selector.EntityFilterSelectorConfig(
        domain=["binary_sensor", "event", "lock", "device_tracker"]
    ),
    selector.EntityFilterSelectorConfig(domain="sensor", device_class="timestamp"),
]


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_ENTITIES, default=defaults.get(CONF_ENTITIES, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(filter=_TRACKABLE, multiple=True)
            ),
            vol.Optional(
                CONF_MAX_EVENTS,
                default=defaults.get(CONF_MAX_EVENTS, DEFAULT_MAX_EVENTS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5, max=100, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            # Needs you. Each is optional: leave one empty and that provider
            # simply contributes nothing, rather than erroring.
            vol.Optional(
                CONF_BATTERY_THRESHOLD,
                default=defaults.get(CONF_BATTERY_THRESHOLD, DEFAULT_BATTERY_THRESHOLD),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=100, step=1, mode=selector.NumberSelectorMode.SLIDER
                )
            ),
            vol.Optional(
                CONF_BIN_SENSOR, default=defaults.get(CONF_BIN_SENSOR, vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_TASKS_SENSOR,
                default=defaults.get(CONF_TASKS_SENSOR, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            ),
            # Which to-do lists keep a "done today" record. Empty means
            # none: watching a list costs a service call per tick, and
            # most lists are not something anybody reviews at the end of
            # the day.
            vol.Optional(
                CONF_DONE_LISTS, default=defaults.get(CONF_DONE_LISTS, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="todo", multiple=True)
            ),
            # Who is worth a row when nobody can locate them. Empty
            # means none: a guest's phone dropping off is not a job.
            vol.Optional(
                CONF_PEOPLE, default=defaults.get(CONF_PEOPLE, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="person", multiple=True)
            ),
            vol.Optional(
                CONF_PRESENCE_GRACE_MINUTES,
                default=defaults.get(
                    CONF_PRESENCE_GRACE_MINUTES, DEFAULT_PRESENCE_GRACE_MINUTES
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5, max=720, step=5, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_SALT_SENSORS, default=defaults.get(CONF_SALT_SENSORS, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=True)
            ),
            vol.Optional(
                CONF_SALT_BOTH_THRESHOLD,
                default=defaults.get(
                    CONF_SALT_BOTH_THRESHOLD, DEFAULT_SALT_BOTH_THRESHOLD
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=100, step=1, mode=selector.NumberSelectorMode.SLIDER
                )
            ),
            vol.Optional(
                CONF_SALT_ONE_THRESHOLD,
                default=defaults.get(
                    CONF_SALT_ONE_THRESHOLD, DEFAULT_SALT_ONE_THRESHOLD
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=100, step=1, mode=selector.NumberSelectorMode.SLIDER
                )
            ),
            # Security. Leaving the locks empty means every lock in the
            # house; leaving the openings empty means none, because a
            # binary sensor is as likely to be a fridge as a front door.
            vol.Optional(
                CONF_SECURITY_LOCKS, default=defaults.get(CONF_SECURITY_LOCKS, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="lock", multiple=True)
            ),
            vol.Optional(
                CONF_SECURITY_OPENINGS,
                default=defaults.get(CONF_SECURITY_OPENINGS, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="binary_sensor",
                    device_class=["door", "garage_door", "opening", "window"],
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_SECURITY_GRACE_MINUTES,
                default=defaults.get(
                    CONF_SECURITY_GRACE_MINUTES, DEFAULT_SECURITY_GRACE_MINUTES
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=60, step=1, mode=selector.NumberSelectorMode.SLIDER
                )
            ),
            vol.Optional(
                CONF_IGNORE_UNAVAILABLE,
                default=defaults.get(CONF_IGNORE_UNAVAILABLE, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(multiple=True)
            ),
            # Appliances. The power sensor is the switch: leave it empty and
            # that machine contributes nothing at all — no cycle sensor, no
            # rows, no cleaning light. Everything else about it is optional,
            # and each missing part degrades on its own (no door means the
            # drum is never reported full; no leak sensor means never red).
            vol.Optional(
                CONF_WASHER_POWER, default=defaults.get(CONF_WASHER_POWER, vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            ),
            vol.Optional(
                CONF_WASHER_PLUG, default=defaults.get(CONF_WASHER_PLUG, vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")
            ),
            vol.Optional(
                CONF_WASHER_DOOR, default=defaults.get(CONF_WASHER_DOOR, vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            ),
            vol.Optional(
                CONF_WASHER_LEAK, default=defaults.get(CONF_WASHER_LEAK, vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="binary_sensor", device_class="moisture"
                )
            ),
            vol.Optional(
                CONF_WASHER_ENERGY,
                default=defaults.get(CONF_WASHER_ENERGY, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="energy")
            ),
            vol.Optional(
                CONF_DRYER_POWER, default=defaults.get(CONF_DRYER_POWER, vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            ),
            vol.Optional(
                CONF_DRYER_PLUG, default=defaults.get(CONF_DRYER_PLUG, vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")
            ),
            vol.Optional(
                CONF_DRYER_DOOR, default=defaults.get(CONF_DRYER_DOOR, vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            ),
            vol.Optional(
                CONF_DRYER_ENERGY,
                default=defaults.get(CONF_DRYER_ENERGY, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="energy")
            ),
            # The previous complete day, whose `charges` attribute carries
            # every half-hour of it -- Octopus publishes it as
            # `..._previous_accumulative_cost`. One sensor is enough: the
            # cost, the kWh, the standing charge and the shape of the day
            # are all in there.
            vol.Optional(
                CONF_ENERGY_COST_SENSOR,
                default=defaults.get(CONF_ENERGY_COST_SENSOR, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            # Today so far, which needs an Octopus Home Mini or Home Pro --
            # without one the API has nothing for today at all. Left empty,
            # the sensor reports the settled day against the house's own
            # recent average and says nothing about today, which is the
            # truth rather than a limitation being hidden.
            vol.Optional(
                CONF_ENERGY_TODAY_COST,
                default=defaults.get(CONF_ENERGY_TODAY_COST, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_ENERGY_TODAY_KWH,
                default=defaults.get(CONF_ENERGY_TODAY_KWH, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            # How far above its usual floor a night has to sit before it
            # becomes a job. High enough to stay rare: the row is only worth
            # having if seeing it means something, and a house has ordinary
            # nights that run 10-20% over for no reason worth chasing.
            vol.Optional(
                CONF_BASELINE_EXCESS_PCT,
                default=defaults.get(
                    CONF_BASELINE_EXCESS_PCT, DEFAULT_BASELINE_EXCESS_PCT
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10, max=200, step=5, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="%",
                )
            ),
            # --- Climate -------------------------------------------
            #
            # The heating zones, and only the heating zones. A room's
            # temperature is read off the thing that controls it rather
            # than off whatever else happens to be in the room, so this
            # takes climate entities and there is no second source to
            # configure. Leave it empty and House climate reports nothing
            # and raises no rows.
            vol.Optional(
                CONF_CLIMATE_ZONES, default=defaults.get(CONF_CLIMATE_ZONES, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate", multiple=True)
            ),
            # The two ends of the bar every room is drawn against. Fixed
            # rather than fitted to the day, so this morning's card and
            # last week's are the same picture.
            vol.Optional(
                CONF_CLIMATE_SCALE_MIN,
                default=defaults.get(
                    CONF_CLIMATE_SCALE_MIN, DEFAULT_CLIMATE_SCALE_MIN
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=25, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="°C",
                )
            ),
            vol.Optional(
                CONF_CLIMATE_SCALE_MAX,
                default=defaults.get(
                    CONF_CLIMATE_SCALE_MAX, DEFAULT_CLIMATE_SCALE_MAX
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=15, max=40, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="°C",
                )
            ),
            # How long a radiator may call for heat without the room
            # moving, and how little movement counts. Not "did the room get
            # warm" -- whether it moved at all.
            vol.Optional(
                CONF_CLIMATE_STUCK_MINUTES,
                default=defaults.get(
                    CONF_CLIMATE_STUCK_MINUTES, DEFAULT_CLIMATE_STUCK_MINUTES
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10, max=180, step=5, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Optional(
                CONF_CLIMATE_STUCK_RISE,
                default=defaults.get(
                    CONF_CLIMATE_STUCK_RISE, DEFAULT_CLIMATE_STUCK_RISE
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=2, step=0.1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="°C",
                )
            ),
            # How long an override has to have been held before it counts
            # as a schedule that has stopped running rather than an
            # afternoon somebody meant.
            vol.Optional(
                CONF_CLIMATE_MANUAL_HOURS,
                default=defaults.get(
                    CONF_CLIMATE_MANUAL_HOURS, DEFAULT_CLIMATE_MANUAL_HOURS
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=168, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="h",
                )
            ),
            # Outside, measured here. Optional: without it the rooms are
            # unchanged and the ventilation answer is absent rather than
            # estimated from a forecast for the region, which is a
            # different place.
            vol.Optional(
                CONF_OUTDOOR_TEMP,
                default=defaults.get(CONF_OUTDOOR_TEMP, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="temperature"
                )
            ),
            vol.Optional(
                CONF_OUTDOOR_HUMIDITY,
                default=defaults.get(CONF_OUTDOOR_HUMIDITY, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="humidity"
                )
            ),
            # What a kWh costs, in money per unit -- Octopus publishes it
            # as `..._current_rate`. Shared by both machines and by
            # anything else that prices energy, because the price of
            # electricity is a fact about the house and not about the
            # washing machine. Left empty, a cycle records its kWh and no
            # cost, and the card shows a hole where the money would be.
            vol.Optional(
                CONF_RATE_SENSOR,
                default=defaults.get(CONF_RATE_SENSOR, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            # Shared by both machines. A dryer's profile is flatter than a
            # washer's, so if one set stops fitting both, this is where it
            # splits — but starting with two copies of numbers nobody has
            # measured yet would be two things to get wrong.
            vol.Optional(
                CONF_START_WATTS,
                default=defaults.get(CONF_START_WATTS, DEFAULT_START_WATTS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=200, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="W",
                )
            ),
            vol.Optional(
                CONF_IDLE_WATTS,
                default=defaults.get(CONF_IDLE_WATTS, DEFAULT_IDLE_WATTS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=200, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="W",
                )
            ),
            vol.Optional(
                CONF_IDLE_MINUTES,
                default=defaults.get(CONF_IDLE_MINUTES, DEFAULT_IDLE_MINUTES),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Optional(
                CONF_MIN_MINUTES,
                default=defaults.get(CONF_MIN_MINUTES, DEFAULT_MIN_MINUTES),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=120, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Optional(
                CONF_MIN_KWH, default=defaults.get(CONF_MIN_KWH, DEFAULT_MIN_KWH),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=5, step=0.01, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="kWh",
                )
            ),
        }
    )


class HomeSignalsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Home Signals config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the entities the feed should watch."""
        if user_input is not None:
            return self.async_create_entry(title=TITLE, data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HomeSignalsOptionsFlow:
        """Return the options flow."""
        return HomeSignalsOptionsFlow()


class HomeSignalsOptionsFlow(OptionsFlow):
    """Let the tracked entity list be edited after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
