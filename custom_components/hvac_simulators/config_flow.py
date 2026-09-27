"""Config and options flow for HVAC Simulators."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import selector

from .appliances import (
    APPLIANCE_TYPE_NAMES,
    APPLIANCE_TYPES,
    DEFAULT_FAN_AIRFLOW_M3H,
    DEFAULT_POWER,
    FUNCTION_AIRCON,
    FUNCTION_HEAT,
    FUNCTION_NAMES,
    HVAC_FUNCTIONS,
    MODE_COOL,
    MODE_HEAT,
    TYPE_AIRCON,
    TYPE_EXTRACTOR_FAN,
    TYPE_HEAT_SOURCE,
    TYPE_HEATER,
    modes_for_functions,
)
from .const import (
    CONF_ACTIVE_ABOVE_W,
    CONF_ACTIVE_RATE,
    CONF_AIR_QUALITY,
    CONF_AIRFLOW,
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCE_TYPE,
    CONF_APPLIANCES,
    CONF_AQ_IMPROVE_PCT,
    CONF_AQ_LOWER_IS_BETTER,
    CONF_CALCULATE_ENERGY,
    CONF_CO2,
    CONF_CO2_OUTDOOR,
    CONF_COUNT,
    CONF_DEHUMID_RATE,
    CONF_FUNCTIONS,
    CONF_HOLD_BAND,
    CONF_HOLD_FACTOR,
    CONF_IDLE_FAN_W,
    CONF_INDOOR_HUMIDITY,
    CONF_INDOOR_TEMP,
    CONF_INVERTER,
    CONF_KIND,
    CONF_LEARN_FROM_MANUAL,
    CONF_MAIN_AIR_GAP_NOTES,
    CONF_MAIN_AIR_GAPS,
    CONF_MAIN_AREA,
    CONF_MAIN_FLOWS_INTO,
    CONF_MAIN_TYPE,
    CONF_MANUAL_TIMEOUT,
    CONF_MAX_IDLE_MINUTES,
    CONF_MAX_LEARNED_WEIGHT,
    CONF_MAX_SETPOINT,
    CONF_MEMBERS,
    CONF_MIN_CONFIDENCE,
    CONF_MIN_SETPOINT,
    CONF_MODES,
    CONF_NOTIFY_RATING,
    CONF_NOTIFY_SERVICE,
    CONF_OFF_DETECT,
    CONF_ON_THRESHOLD_W,
    CONF_OPENINGS,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMP,
    CONF_POWER_ENTITY,
    CONF_ROOM,
    CONF_ROOMS,
    CONF_SLEEP_END,
    CONF_SLEEP_START,
    CONF_SOLAR_MARGIN,
    CONF_SOLAR_THRESHOLD,
    CONF_STABLE_RATE,
    CONF_STANDBY_W,
    CONF_UPDATE_INTERVAL,
    CONF_UPDATE_MODE,
    CONF_USE_VIRTUAL_OPENINGS,
    CONF_VOLUME,
    CONF_WINDOW_MINUTES,
    DEFAULT_ACTIVE_RATE,
    DEFAULT_AQ_IMPROVE_PCT,
    DEFAULT_CO2_OUTDOOR,
    DEFAULT_DEHUMID_RATE,
    DEFAULT_HOLD_BAND,
    DEFAULT_LEARN_FROM_MANUAL,
    DEFAULT_MAX_IDLE_MINUTES,
    DEFAULT_MAX_LEARNED_WEIGHT,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_NOTIFY_RATING,
    DEFAULT_SLEEP_END,
    DEFAULT_SLEEP_START,
    DEFAULT_SOLAR_MARGIN,
    DEFAULT_SOLAR_THRESHOLD,
    DEFAULT_STABLE_RATE,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_VOLUME,
    DEFAULT_WINDOW_MINUTES,
    DOMAIN,
    SENSOR_KEYS,
    UPDATE_MODE_INTERVAL,
    UPDATE_MODE_ON_CHANGE,
)
from .effectiveness import RATING_NAMES, RATINGS
from .plugs import KIND_GENERAL, KIND_NAMES, KINDS
from .room import AIR_GAPS, MAIN_ROOM, ROOM_TYPES

_OPTIONAL_ENTITY_KEYS = (
    CONF_INDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMP,
    CONF_OUTDOOR_HUMIDITY,
    CONF_AIR_QUALITY,
    CONF_CO2,
)


def _num(min_v: float, max_v: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        min=min_v, max=max_v, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _sensors_schema(current: dict[str, Any], with_name: bool) -> vol.Schema:
    def suggested(key: str) -> dict[str, Any]:
        value = current.get(key)
        return {"suggested_value": value} if value not in (None, "", []) else {}

    fields: dict[Any, Any] = {}
    if with_name:
        fields[vol.Required(CONF_NAME, default=current.get(CONF_NAME, "Home"))] = str
    fields.update(
        {
            vol.Required(CONF_INDOOR_TEMP, description=suggested(CONF_INDOOR_TEMP)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Optional(
                CONF_INDOOR_HUMIDITY, description=suggested(CONF_INDOOR_HUMIDITY)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="humidity")
            ),
            vol.Optional(
                CONF_OUTDOOR_TEMP, description=suggested(CONF_OUTDOOR_TEMP)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "weather"])),
            vol.Optional(
                CONF_OUTDOOR_HUMIDITY, description=suggested(CONF_OUTDOOR_HUMIDITY)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "weather"])),
            vol.Optional(CONF_CO2, description=suggested(CONF_CO2)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="carbon_dioxide")
            ),
            vol.Optional(CONF_AIR_QUALITY, description=suggested(CONF_AIR_QUALITY)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(
                CONF_AQ_LOWER_IS_BETTER, default=current.get(CONF_AQ_LOWER_IS_BETTER, True)
            ): selector.BooleanSelector(),
            vol.Optional(CONF_OPENINGS, default=current.get(CONF_OPENINGS) or []): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor", multiple=True)
            ),
            vol.Required(
                CONF_SOLAR_THRESHOLD, default=current.get(CONF_SOLAR_THRESHOLD, DEFAULT_SOLAR_THRESHOLD)
            ): _num(-10, 45, 0.5, "°C"),
            vol.Optional(CONF_MAIN_AREA, description=suggested(CONF_MAIN_AREA)): selector.AreaSelector(),
            vol.Required(
                CONF_MAIN_TYPE, default=current.get(CONF_MAIN_TYPE, "living")
            ): _room_type_selector(),
            vol.Required(
                CONF_MAIN_AIR_GAPS, default=current.get(CONF_MAIN_AIR_GAPS, "none")
            ): _air_gap_selector(),
            vol.Optional(CONF_MAIN_AIR_GAP_NOTES, description=suggested(CONF_MAIN_AIR_GAP_NOTES)): str,
            vol.Required(
                CONF_USE_VIRTUAL_OPENINGS, default=current.get(CONF_USE_VIRTUAL_OPENINGS, True)
            ): selector.BooleanSelector(),
        }
    )
    return vol.Schema(fields)


def _room_type_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=t, label=ROOM_TYPE_NAMES[t]) for t in ROOM_TYPES],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _air_gap_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=g, label=AIR_GAP_NAMES[g]) for g in AIR_GAPS],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


ROOM_TYPE_NAMES = {
    "living": "Living / open plan",
    "bedroom": "Bedroom",
    "bathroom": "Bathroom (enables shower detection)",
    "laundry": "Laundry",
    "kitchen": "Kitchen",
    "hallway": "Hallway",
    "office": "Office / study",
    "other": "Other",
}
AIR_GAP_NAMES = {
    "none": "None known (tight)",
    "small": "Small gaps (door undercut, old seals)",
    "large": "Large gaps (vents, gaps around windows, open chimney)",
}


def _room_schema(current: dict[str, Any], new: bool) -> vol.Schema:
    def suggested(key: str) -> dict[str, Any]:
        value = current.get(key)
        return {"suggested_value": value} if value not in (None, "", []) else {}

    fields: dict[Any, Any] = {}
    if new:
        fields[vol.Required("area_id")] = selector.AreaSelector()
    fields.update(
        {
            vol.Required("type", default=current.get("type", "other")): _room_type_selector(),
            vol.Optional("temperature", description=suggested("temperature")): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Optional("humidity", description=suggested("humidity")): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="humidity")
            ),
            vol.Optional("co2", description=suggested("co2")): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="carbon_dioxide")
            ),
            vol.Optional("air_quality", description=suggested("air_quality")): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional("openings", default=current.get("openings") or []): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor", multiple=True)
            ),
            vol.Required("volume_m3", default=current.get("volume_m3", 30)): _num(1, 5000, 1, "m³"),
            vol.Required("air_gaps", default=current.get("air_gaps", "none")): _air_gap_selector(),
            vol.Optional("air_gap_notes", description=suggested("air_gap_notes")): str,
            vol.Required(
                "use_virtual_openings", default=current.get("use_virtual_openings", True)
            ): selector.BooleanSelector(),
        }
    )
    return vol.Schema(fields)


def _clean_sensors(user_input: dict[str, Any]) -> dict[str, Any]:
    """Store cleared optional sensors as None so they override older values."""
    out = {k: user_input.get(k) for k in SENSOR_KEYS if k in user_input}
    for key in _OPTIONAL_ENTITY_KEYS:
        out.setdefault(key, None)
    out[CONF_OPENINGS] = user_input.get(CONF_OPENINGS) or []
    return out


def _tuning_schema(current: dict[str, Any]) -> vol.Schema:
    def d(key: str, default: Any) -> Any:
        value = current.get(key)
        return default if value is None else value

    return vol.Schema(
        {
            vol.Required(CONF_WINDOW_MINUTES, default=d(CONF_WINDOW_MINUTES, DEFAULT_WINDOW_MINUTES)): _num(
                5, 120, 1, "min"
            ),
            vol.Required(
                CONF_SOLAR_THRESHOLD, default=d(CONF_SOLAR_THRESHOLD, DEFAULT_SOLAR_THRESHOLD)
            ): _num(-10, 45, 0.5, "°C"),
            vol.Required(CONF_ACTIVE_RATE, default=d(CONF_ACTIVE_RATE, DEFAULT_ACTIVE_RATE)): _num(
                0.05, 5, 0.05, "°C/h"
            ),
            vol.Required(CONF_STABLE_RATE, default=d(CONF_STABLE_RATE, DEFAULT_STABLE_RATE)): _num(
                0.01, 2, 0.01, "°C/h"
            ),
            vol.Required(CONF_DEHUMID_RATE, default=d(CONF_DEHUMID_RATE, DEFAULT_DEHUMID_RATE)): _num(
                -30, -0.5, 0.5, "%/h"
            ),
            vol.Required(CONF_AQ_IMPROVE_PCT, default=d(CONF_AQ_IMPROVE_PCT, DEFAULT_AQ_IMPROVE_PCT)): _num(
                0.5, 100, 0.5, "%/h"
            ),
            vol.Required(CONF_MIN_CONFIDENCE, default=d(CONF_MIN_CONFIDENCE, DEFAULT_MIN_CONFIDENCE)): _num(
                0.1, 0.95, 0.05
            ),
            vol.Required(CONF_HOLD_BAND, default=d(CONF_HOLD_BAND, DEFAULT_HOLD_BAND)): _num(
                0.2, 5, 0.1, "°C"
            ),
            vol.Required(
                CONF_MAX_LEARNED_WEIGHT, default=d(CONF_MAX_LEARNED_WEIGHT, DEFAULT_MAX_LEARNED_WEIGHT)
            ): _num(0, 1, 0.05),
            vol.Required(
                CONF_LEARN_FROM_MANUAL, default=d(CONF_LEARN_FROM_MANUAL, DEFAULT_LEARN_FROM_MANUAL)
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_MAX_IDLE_MINUTES, default=d(CONF_MAX_IDLE_MINUTES, DEFAULT_MAX_IDLE_MINUTES)
            ): _num(5, 720, 5, "min"),
            vol.Required(CONF_VOLUME, default=d(CONF_VOLUME, DEFAULT_VOLUME)): _num(5, 5000, 1, "m³"),
            vol.Required(CONF_CO2_OUTDOOR, default=d(CONF_CO2_OUTDOOR, DEFAULT_CO2_OUTDOOR)): _num(
                300, 600, 5, "ppm"
            ),
            vol.Required(CONF_SLEEP_START, default=d(CONF_SLEEP_START, DEFAULT_SLEEP_START)): _num(
                0, 23, 1, "h"
            ),
            vol.Required(CONF_SLEEP_END, default=d(CONF_SLEEP_END, DEFAULT_SLEEP_END)): _num(0, 23, 1, "h"),
            vol.Required(
                CONF_NOTIFY_RATING, default=d(CONF_NOTIFY_RATING, DEFAULT_NOTIFY_RATING)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=r, label=RATING_NAMES[r]) for r in RATINGS[1:]],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_NOTIFY_SERVICE,
                description={"suggested_value": current.get(CONF_NOTIFY_SERVICE)}
                if current.get(CONF_NOTIFY_SERVICE)
                else {},
            ): str,
            vol.Required(CONF_SOLAR_MARGIN, default=d(CONF_SOLAR_MARGIN, DEFAULT_SOLAR_MARGIN)): _num(
                0, 15, 0.5, "°C"
            ),
            vol.Required(
                CONF_UPDATE_MODE, default=d(CONF_UPDATE_MODE, UPDATE_MODE_INTERVAL)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=UPDATE_MODE_INTERVAL, label="On a fixed interval"),
                        selector.SelectOptionDict(
                            value=UPDATE_MODE_ON_CHANGE, label="Every time a sensor updates"
                        ),
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_UPDATE_INTERVAL, default=d(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            ): _num(5, 600, 5, "s"),
        }
    )


def _appliance_type_schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_APPLIANCE_NAME, default=current.get(CONF_APPLIANCE_NAME, "")): str,
            vol.Optional(CONF_ROOM, description=_suggest(current, CONF_ROOM)): selector.AreaSelector(),
            vol.Required(
                CONF_APPLIANCE_TYPE, default=current.get(CONF_APPLIANCE_TYPE, TYPE_HEATER)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=t, label=APPLIANCE_TYPE_NAMES[t])
                        for t in APPLIANCE_TYPES
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
    )


def _functions_schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_FUNCTIONS, default=current.get(CONF_FUNCTIONS, [FUNCTION_HEAT, FUNCTION_AIRCON])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=f, label=FUNCTION_NAMES[f]) for f in HVAC_FUNCTIONS
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


def _appliance_details_schema(atype: str, current: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if atype == TYPE_HEAT_SOURCE:
        fields[vol.Required(CONF_KIND, default=current.get(CONF_KIND, KIND_GENERAL))] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=k, label=KIND_NAMES[k]) for k in KINDS],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        )
        fields[vol.Required(CONF_POWER_ENTITY, description=_suggest(current, CONF_POWER_ENTITY))] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power"))
        )
        fields[vol.Required(CONF_ON_THRESHOLD_W, default=current.get(CONF_ON_THRESHOLD_W, 1))] = _num(
            0, 5000, 0.5, "W"
        )
        fields[vol.Optional(CONF_ACTIVE_ABOVE_W, description=_suggest(current, CONF_ACTIVE_ABOVE_W))] = _num(
            0, 10000, 1, "W"
        )
        return vol.Schema(fields)

    if atype == TYPE_EXTRACTOR_FAN:
        fields[vol.Required(CONF_COUNT, default=current.get(CONF_COUNT, 1))] = _num(1, 20, 1)
        fields[vol.Optional(CONF_MEMBERS, description=_suggest(current, CONF_MEMBERS))] = str
        fields[vol.Required(CONF_AIRFLOW, default=current.get(CONF_AIRFLOW, DEFAULT_FAN_AIRFLOW_M3H))] = _num(
            5, 2000, 5, "m³/h"
        )

    if atype == TYPE_AIRCON:
        modes = modes_for_functions(current.get(CONF_FUNCTIONS, []))
    else:
        modes = list(DEFAULT_POWER[atype])
    for mode in modes:
        key = "power_" + mode
        fields[vol.Required(key, default=current.get(key, DEFAULT_POWER[atype][mode]))] = _num(
            0, 20000, 1, "W"
        )
    fields[vol.Required(CONF_STANDBY_W, default=current.get(CONF_STANDBY_W, 0))] = _num(0, 500, 0.5, "W")
    if MODE_HEAT in modes or MODE_COOL in modes:
        for key in (CONF_MIN_SETPOINT, CONF_MAX_SETPOINT):
            fields[vol.Optional(key, description=_suggest(current, key))] = _num(0, 40, 0.5, "°C")
        fields[vol.Required(CONF_INVERTER, default=current.get(CONF_INVERTER, atype == TYPE_AIRCON))] = (
            selector.BooleanSelector()
        )
        fields[vol.Required(CONF_HOLD_FACTOR, default=current.get(CONF_HOLD_FACTOR, 0.4))] = _num(
            0.05, 1, 0.05
        )
        fan_default = 30 if atype == TYPE_AIRCON else 0
        fields[vol.Required(CONF_IDLE_FAN_W, default=current.get(CONF_IDLE_FAN_W, fan_default))] = _num(
            0, 500, 1, "W"
        )
        fields[vol.Required(CONF_MANUAL_TIMEOUT, default=current.get(CONF_MANUAL_TIMEOUT, 0))] = _num(
            0, 1440, 5, "min"
        )
        fields[vol.Required(CONF_OFF_DETECT, default=current.get(CONF_OFF_DETECT, 45))] = _num(
            0, 720, 5, "min"
        )
    fields[vol.Required(CONF_CALCULATE_ENERGY, default=current.get(CONF_CALCULATE_ENERGY, True))] = (
        selector.BooleanSelector()
    )
    return vol.Schema(fields)


def _clean_room(user_input: dict[str, Any]) -> dict[str, Any]:
    """Room fields from a form, with cleared optional sensors stored as None."""
    out = {
        k: user_input.get(k)
        for k in ("type", "temperature", "humidity", "co2", "air_quality", "air_gaps", "air_gap_notes")
    }
    out["openings"] = user_input.get("openings") or []
    out["volume_m3"] = float(user_input.get("volume_m3") or 30)
    out["use_virtual_openings"] = bool(user_input.get("use_virtual_openings", True))
    return out


def _suggest(current: dict[str, Any], key: str) -> dict[str, Any]:
    value = current.get(key)
    return {"suggested_value": value} if value not in (None, "") else {}


def _validate_details(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    lo, hi = user_input.get(CONF_MIN_SETPOINT), user_input.get(CONF_MAX_SETPOINT)
    if lo is not None and hi is not None and lo >= hi:
        errors[CONF_MAX_SETPOINT] = "setpoint_range"
    if CONF_COUNT in user_input:
        user_input[CONF_COUNT] = int(user_input[CONF_COUNT])
    return errors


class _ApplianceSteps:
    """Shared add/edit appliance steps for the config and options flows."""

    _appliances: list[dict[str, Any]]
    _draft: dict[str, Any]

    def _save_draft(self, details: dict[str, Any]) -> None:
        appliance = {**self._draft, **details}
        for key in (CONF_MIN_SETPOINT, CONF_MAX_SETPOINT, CONF_MEMBERS):
            appliance.setdefault(key, None)
            if key not in details:
                appliance[key] = None
        self._appliances = [
            a for a in self._appliances if a[CONF_APPLIANCE_ID] != appliance[CONF_APPLIANCE_ID]
        ]
        self._appliances.append(appliance)

    async def async_step_add_appliance(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            name = (
                user_input[CONF_APPLIANCE_NAME].strip()
                or APPLIANCE_TYPE_NAMES[user_input[CONF_APPLIANCE_TYPE]]
            )
            self._draft = {
                CONF_APPLIANCE_ID: uuid.uuid4().hex[:8],
                CONF_APPLIANCE_NAME: name,
                CONF_APPLIANCE_TYPE: user_input[CONF_APPLIANCE_TYPE],
                CONF_ROOM: user_input.get(CONF_ROOM),
            }
            return await self._next_after_type()
        return self.async_show_form(step_id="add_appliance", data_schema=_appliance_type_schema({}))  # type: ignore[attr-defined]

    async def async_step_appliance_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_details(user_input)
            if not errors:
                self._save_draft(user_input)
                return await self._after_appliance_saved()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="appliance_details",
            data_schema=_appliance_details_schema(
                self._draft[CONF_APPLIANCE_TYPE], {**self._draft, **(user_input or {})}
            ),
            errors=errors,
            description_placeholders={
                "name": self._draft[CONF_APPLIANCE_NAME],
                "type": APPLIANCE_TYPE_NAMES[self._draft[CONF_APPLIANCE_TYPE]],
            },
        )

    async def _next_after_type(self) -> ConfigFlowResult:
        if self._draft[CONF_APPLIANCE_TYPE] == TYPE_AIRCON:
            return await self.async_step_hvac_functions()
        return await self.async_step_appliance_details()

    async def async_step_hvac_functions(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """HVAC units: pick what it can do (heat, ventilate, air condition)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_FUNCTIONS]:
                self._draft[CONF_FUNCTIONS] = user_input[CONF_FUNCTIONS]
                self._draft.pop(CONF_MODES, None)
                return await self.async_step_appliance_details()
            errors[CONF_FUNCTIONS] = "no_functions"
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="hvac_functions",
            data_schema=_functions_schema(self._draft),
            errors=errors,
            description_placeholders={"name": self._draft[CONF_APPLIANCE_NAME]},
        )

    async def _after_appliance_saved(self) -> ConfigFlowResult:
        raise NotImplementedError


class HvacSimulatorsConfigFlow(_ApplianceSteps, ConfigFlow, domain=DOMAIN):
    """Set up a space (a room or the whole home)."""

    VERSION = 1

    def __init__(self) -> None:
        self._appliances = []
        self._draft = {}
        self._data: dict[str, Any] = {}
        self._title = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._title = user_input[CONF_NAME]
            self._data = _clean_sensors(user_input)
            return await self.async_step_menu()
        return self.async_show_form(step_id="user", data_schema=_sensors_schema({}, with_name=True))

    async def async_step_menu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="menu",
            menu_options=["add_appliance", "finish"],
            description_placeholders={
                "appliances": ", ".join(a[CONF_APPLIANCE_NAME] for a in self._appliances) or "none yet"
            },
        )

    async def _after_appliance_saved(self) -> ConfigFlowResult:
        return await self.async_step_menu()

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        solar = self._data.pop(CONF_SOLAR_THRESHOLD, DEFAULT_SOLAR_THRESHOLD)
        return self.async_create_entry(
            title=self._title,
            data=self._data,
            options={CONF_SOLAR_THRESHOLD: solar, CONF_APPLIANCES: self._appliances},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HvacSimulatorsOptionsFlow()


class HvacSimulatorsOptionsFlow(_ApplianceSteps, OptionsFlow):
    """Change sensors, tuning and appliances."""

    def __init__(self) -> None:
        self._appliances = []
        self._rooms: list[dict[str, Any]] = []
        self._draft = {}
        self._room_draft: dict[str, Any] = {}
        self._loaded = False

    def _load(self) -> None:
        if not self._loaded:
            self._appliances = [dict(a) for a in self.config_entry.options.get(CONF_APPLIANCES, [])]
            self._rooms = [dict(r) for r in self.config_entry.options.get(CONF_ROOMS, [])]
            self._loaded = True

    @property
    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    def _finish(self, updates: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            data={
                **self.config_entry.options,
                CONF_APPLIANCES: self._appliances,
                CONF_ROOMS: self._rooms,
                **updates,
            }
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._load()
        options = ["sensors", "tuning", "add_room"]
        if self._rooms:
            options += ["edit_room", "remove_room"]
        options += ["room_links", "add_appliance"]
        if self._appliances:
            options += ["edit_appliance", "remove_appliance"]
        return self.async_show_menu(
            step_id="init",
            menu_options=options,
            description_placeholders={
                "appliances": ", ".join(a[CONF_APPLIANCE_NAME] for a in self._appliances) or "none yet",
                "rooms": ", ".join(r["name"] for r in self._rooms) or "none yet",
            },
        )

    # --- rooms ---------------------------------------------------------------

    def _area_name(self, area_id: str) -> str:
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else area_id

    def _room_choice(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("id"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=r["id"], label=r["name"]) for r in self._rooms
                        ]
                    )
                )
            }
        )

    async def async_step_add_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            area_id = user_input["area_id"]
            if any(r["id"] == area_id for r in self._rooms) or area_id == self._current.get(CONF_MAIN_AREA):
                errors["area_id"] = "room_exists"
            else:
                room = _clean_room(user_input)
                room.update(
                    {"id": area_id, "area_id": area_id, "name": self._area_name(area_id), "flows_into": []}
                )
                self._rooms.append(room)
                return self._finish({})
        return self.async_show_form(
            step_id="add_room", data_schema=_room_schema(user_input or {}, new=True), errors=errors
        )

    async def async_step_edit_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._room_draft = next(dict(r) for r in self._rooms if r["id"] == user_input["id"])
            return await self.async_step_room_details()
        return self.async_show_form(step_id="edit_room", data_schema=self._room_choice())

    async def async_step_room_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            room = {**self._room_draft, **_clean_room(user_input)}
            self._rooms = [room if r["id"] == room["id"] else r for r in self._rooms]
            return self._finish({})
        return self.async_show_form(
            step_id="room_details",
            data_schema=_room_schema(self._room_draft, new=False),
            description_placeholders={"name": self._room_draft.get("name", "")},
        )

    async def async_step_remove_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            gone = user_input["id"]
            self._rooms = [r for r in self._rooms if r["id"] != gone]
            for room in self._rooms:
                room["flows_into"] = [x for x in room.get("flows_into", []) if x != gone]
            return self._finish({})
        return self.async_show_form(step_id="remove_room", data_schema=self._room_choice())

    async def async_step_room_links(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Which rooms flow into each other (open doorways, hallways, gaps): they form zones."""
        everyone = [(MAIN_ROOM, self.config_entry.title)] + [(r["id"], r["name"]) for r in self._rooms]
        if user_input is not None:
            main_links = user_input.get(f"links_{MAIN_ROOM}", [])
            for room in self._rooms:
                room["flows_into"] = user_input.get(f"links_{room['id']}", [])
            return self._finish({CONF_MAIN_FLOWS_INTO: main_links})
        current = {MAIN_ROOM: self._current.get(CONF_MAIN_FLOWS_INTO) or []}
        current.update({r["id"]: r.get("flows_into", []) for r in self._rooms})
        fields: dict[Any, Any] = {}
        for rid, name in everyone:
            others = [selector.SelectOptionDict(value=o, label=n) for o, n in everyone if o != rid]
            fields[
                vol.Optional(f"links_{rid}", default=current.get(rid, []), description={"suffix": name})
            ] = selector.SelectSelector(selector.SelectSelectorConfig(options=others, multiple=True))
        return self.async_show_form(
            step_id="room_links",
            data_schema=vol.Schema(fields),
            description_placeholders={"rooms": ", ".join(n for _, n in everyone)},
        )

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._finish(_clean_sensors(user_input))
        return self.async_show_form(
            step_id="sensors", data_schema=_sensors_schema(self._current, with_name=False)
        )

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input.setdefault(CONF_NOTIFY_SERVICE, None)
            for key in (CONF_SLEEP_START, CONF_SLEEP_END):
                user_input[key] = int(user_input[key])
            return self._finish(user_input)
        return self.async_show_form(step_id="tuning", data_schema=_tuning_schema(self._current))

    async def _after_appliance_saved(self) -> ConfigFlowResult:
        return self._finish({})

    def _appliance_choice(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_APPLIANCE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=a[CONF_APPLIANCE_ID],
                                label=f"{a[CONF_APPLIANCE_NAME]} ({APPLIANCE_TYPE_NAMES[a[CONF_APPLIANCE_TYPE]]})",
                            )
                            for a in self._appliances
                        ]
                    )
                )
            }
        )

    async def async_step_edit_appliance(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._draft = next(
                dict(a) for a in self._appliances if a[CONF_APPLIANCE_ID] == user_input[CONF_APPLIANCE_ID]
            )
            return await self.async_step_rename_appliance()
        return self.async_show_form(step_id="edit_appliance", data_schema=self._appliance_choice())

    async def async_step_rename_appliance(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._draft[CONF_APPLIANCE_NAME] = (
                user_input[CONF_APPLIANCE_NAME].strip() or self._draft[CONF_APPLIANCE_NAME]
            )
            self._draft[CONF_ROOM] = user_input.get(CONF_ROOM)
            return await self._next_after_type()
        return self.async_show_form(
            step_id="rename_appliance",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APPLIANCE_NAME, default=self._draft[CONF_APPLIANCE_NAME]): str,
                    vol.Optional(
                        CONF_ROOM, description=_suggest(self._draft, CONF_ROOM)
                    ): selector.AreaSelector(),
                }
            ),
        )

    async def async_step_remove_appliance(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._appliances = [
                a for a in self._appliances if a[CONF_APPLIANCE_ID] != user_input[CONF_APPLIANCE_ID]
            ]
            return self._finish({})
        return self.async_show_form(step_id="remove_appliance", data_schema=self._appliance_choice())
