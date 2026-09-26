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
    CONF_INDOOR_HUMIDITY,
    CONF_INDOOR_TEMP,
    CONF_INVERTER,
    CONF_LEARN_FROM_MANUAL,
    CONF_MAX_IDLE_MINUTES,
    CONF_MAX_LEARNED_WEIGHT,
    CONF_MAX_SETPOINT,
    CONF_MEMBERS,
    CONF_MIN_CONFIDENCE,
    CONF_MIN_SETPOINT,
    CONF_MODES,
    CONF_NOTIFY_RATING,
    CONF_NOTIFY_SERVICE,
    CONF_ON_THRESHOLD_W,
    CONF_OPENINGS,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMP,
    CONF_POWER_ENTITY,
    CONF_SLEEP_END,
    CONF_SLEEP_START,
    CONF_SOLAR_THRESHOLD,
    CONF_STABLE_RATE,
    CONF_STANDBY_W,
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
    DEFAULT_SOLAR_THRESHOLD,
    DEFAULT_STABLE_RATE,
    DEFAULT_VOLUME,
    DEFAULT_WINDOW_MINUTES,
    DOMAIN,
    SENSOR_KEYS,
)
from .effectiveness import RATING_NAMES, RATINGS

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
        }
    )


def _appliance_type_schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_APPLIANCE_NAME, default=current.get(CONF_APPLIANCE_NAME, "")): str,
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
        fields[vol.Required(CONF_POWER_ENTITY, description=_suggest(current, CONF_POWER_ENTITY))] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power"))
        )
        fields[vol.Required(CONF_ON_THRESHOLD_W, default=current.get(CONF_ON_THRESHOLD_W, 10))] = _num(
            0, 5000, 1, "W"
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
    fields[vol.Required(CONF_CALCULATE_ENERGY, default=current.get(CONF_CALCULATE_ENERGY, True))] = (
        selector.BooleanSelector()
    )
    return vol.Schema(fields)


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
        self._draft = {}
        self._loaded = False

    def _load(self) -> None:
        if not self._loaded:
            self._appliances = [dict(a) for a in self.config_entry.options.get(CONF_APPLIANCES, [])]
            self._loaded = True

    @property
    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    def _finish(self, updates: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self.config_entry.options, CONF_APPLIANCES: self._appliances, **updates}
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._load()
        options = ["sensors", "tuning", "add_appliance"]
        if self._appliances:
            options += ["edit_appliance", "remove_appliance"]
        return self.async_show_menu(
            step_id="init",
            menu_options=options,
            description_placeholders={
                "appliances": ", ".join(a[CONF_APPLIANCE_NAME] for a in self._appliances) or "none yet"
            },
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
            return await self._next_after_type()
        return self.async_show_form(
            step_id="rename_appliance",
            data_schema=vol.Schema(
                {vol.Required(CONF_APPLIANCE_NAME, default=self._draft[CONF_APPLIANCE_NAME]): str}
            ),
        )

    async def async_step_remove_appliance(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._appliances = [
                a for a in self._appliances if a[CONF_APPLIANCE_ID] != user_input[CONF_APPLIANCE_ID]
            ]
            return self._finish({})
        return self.async_show_form(step_id="remove_appliance", data_schema=self._appliance_choice())
