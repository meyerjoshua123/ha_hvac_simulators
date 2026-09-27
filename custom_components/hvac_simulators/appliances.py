"""Appliance models for HVAC Simulators (pure Python, no Home Assistant imports).

An appliance is something the engine can blame a change on: a heater, an HVAC
unit (whose modes follow from what it can do: heat, ventilate, air condition),
a group of extractor fans, a dehumidifier, a humidifier or an air purifier.
Each has rated power per mode so the integration can estimate power and energy,
or hand the power figure to PowerCalc.

Heat sources (fridge, dryer, oven...) are different: they are *measured* from
their smart plug, not inferred, and their learned effect on temperature and
humidity is subtracted before the engine decides anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .engine import (
    ACTIVE_CAUSES,
    CAUSE_AIR_FILTERING,
    CAUSE_COOLING,
    CAUSE_DEHUMIDIFY,
    CAUSE_HEATING,
    CAUSE_HUMIDIFY,
    CAUSE_NAMES,
    CAUSE_VENTILATION,
    PASSIVE_CAUSES,
)

TYPE_HEATER = "heater"
TYPE_AIRCON = "aircon"
TYPE_EXTRACTOR_FAN = "extractor_fan"
TYPE_DEHUMIDIFIER = "dehumidifier"
TYPE_HUMIDIFIER = "humidifier"
TYPE_AIR_PURIFIER = "air_purifier"
TYPE_HEAT_SOURCE = "heat_source"
APPLIANCE_TYPES = (
    TYPE_AIRCON,
    TYPE_HEATER,
    TYPE_EXTRACTOR_FAN,
    TYPE_DEHUMIDIFIER,
    TYPE_HUMIDIFIER,
    TYPE_AIR_PURIFIER,
    TYPE_HEAT_SOURCE,
)
APPLIANCE_TYPE_NAMES = {
    TYPE_AIRCON: "HVAC unit (heat pump / aircon / ventilation)",
    TYPE_HEATER: "Heater",
    TYPE_EXTRACTOR_FAN: "Extractor fan(s)",
    TYPE_DEHUMIDIFIER: "Dehumidifier",
    TYPE_HUMIDIFIER: "Humidifier",
    TYPE_AIR_PURIFIER: "Air purifier",
    TYPE_HEAT_SOURCE: "Heat source with a smart plug (fridge, dryer, oven...)",
}

MODE_OFF = "off"
MODE_HEAT = "heat"
MODE_COOL = "cool"
MODE_DRY = "dry"
MODE_VENTILATE = "ventilate"
MODE_FILTER = "filter"
MODE_ON = "on"

# What an HVAC unit can do (the H, V and AC) and the modes each unlocks.
FUNCTION_HEAT = "heat"
FUNCTION_VENTILATE = "ventilate"
FUNCTION_AIRCON = "aircon"
HVAC_FUNCTIONS = (FUNCTION_HEAT, FUNCTION_VENTILATE, FUNCTION_AIRCON)
FUNCTION_NAMES = {
    FUNCTION_HEAT: "Heat",
    FUNCTION_VENTILATE: "Ventilate (fresh air and indoor filter)",
    FUNCTION_AIRCON: "Air condition (cool and dehumidify)",
}
FUNCTION_MODES = {
    FUNCTION_HEAT: (MODE_HEAT,),
    FUNCTION_AIRCON: (MODE_COOL, MODE_DRY),
    FUNCTION_VENTILATE: (MODE_VENTILATE, MODE_FILTER),
}
MODE_NAMES = {
    MODE_OFF: "Off",
    MODE_HEAT: "Heat",
    MODE_COOL: "Cool",
    MODE_DRY: "Dehumidify (dry)",
    MODE_VENTILATE: "Ventilate (fresh air)",
    MODE_FILTER: "Filter (indoor recirculation)",
    MODE_ON: "On",
}

# Which operating mode an appliance uses to produce each cause.
_CAUSE_TO_MODE = {
    TYPE_HEATER: {CAUSE_HEATING: MODE_HEAT},
    TYPE_AIRCON: {
        CAUSE_HEATING: MODE_HEAT,
        CAUSE_COOLING: MODE_COOL,
        CAUSE_DEHUMIDIFY: MODE_DRY,
        # Fresh air lowers CO2; recirculating through the filter only cleans particulates.
        CAUSE_VENTILATION: MODE_VENTILATE,
        CAUSE_AIR_FILTERING: MODE_FILTER,
    },
    TYPE_EXTRACTOR_FAN: {CAUSE_VENTILATION: MODE_ON},
    TYPE_DEHUMIDIFIER: {CAUSE_DEHUMIDIFY: MODE_ON},
    TYPE_HUMIDIFIER: {CAUSE_HUMIDIFY: MODE_ON},
    TYPE_AIR_PURIFIER: {CAUSE_AIR_FILTERING: MODE_ON},
}

# Default rated power (W) by type and mode, used when the user leaves a field empty.
DEFAULT_POWER = {
    TYPE_HEATER: {MODE_HEAT: 2000.0},
    TYPE_AIRCON: {
        MODE_HEAT: 1200.0,
        MODE_COOL: 1000.0,
        MODE_DRY: 600.0,
        MODE_VENTILATE: 60.0,
        MODE_FILTER: 40.0,
    },
    TYPE_EXTRACTOR_FAN: {MODE_ON: 30.0},  # per fan
    TYPE_DEHUMIDIFIER: {MODE_ON: 250.0},
    TYPE_HUMIDIFIER: {MODE_ON: 30.0},
    TYPE_AIR_PURIFIER: {MODE_ON: 40.0},
    TYPE_HEAT_SOURCE: {},
}
DEFAULT_FAN_AIRFLOW_M3H = 90.0

# Old configs listed aircon modes directly.
_LEGACY_MODE_FUNCTION = {
    "heat": FUNCTION_HEAT,
    "cool": FUNCTION_AIRCON,
    "dry": FUNCTION_AIRCON,
    "fan": FUNCTION_VENTILATE,
}

LABEL_SEP = ":"


def modes_for_functions(functions: list[str]) -> list[str]:
    """HVAC modes unlocked by the selected functions."""
    order = (FUNCTION_HEAT, FUNCTION_AIRCON, FUNCTION_VENTILATE)
    return [m for f in order if f in functions for m in FUNCTION_MODES[f]]


@dataclass
class Appliance:
    """A configured appliance."""

    id: str
    name: str
    type: str
    power: dict[str, float] = field(default_factory=dict)
    modes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    standby_w: float = 0.0
    min_setpoint: float | None = None
    max_setpoint: float | None = None
    inverter: bool = True
    hold_factor: float = 0.4
    # Power while the compressor/element is off but the unit is on (fan only).
    idle_fan_w: float = 30.0
    # Manual mode/setpoint reverts to auto after this long (0 = never).
    manual_timeout_min: float = 0.0
    # In manual heat/cool, drifting the wrong way past the setpoint for this
    # long means the unit was switched off (0 = never assume).
    off_detect_min: float = 45.0
    calculate_energy: bool = True
    # Extractor fan groups
    count: int = 1
    members: list[str] = field(default_factory=list)
    airflow_m3h: float = DEFAULT_FAN_AIRFLOW_M3H
    # Heat sources (plugged-in appliances)
    power_entity: str | None = None
    on_threshold_w: float = 10.0
    kind: str = "general"
    active_above_w: float | None = None
    # Home Assistant area the appliance is in (None = the whole space).
    room: str | None = None

    @classmethod
    def from_config(cls, data: dict[str, Any]) -> Appliance:
        """Build from the stored options dict."""
        atype = data.get("type", TYPE_HEATER)
        defaults = DEFAULT_POWER.get(atype, {})
        functions: list[str] = []
        if atype == TYPE_AIRCON:
            if "functions" in data:
                functions = [f for f in data["functions"] if f in HVAC_FUNCTIONS]
            else:
                legacy = data.get("modes", ["heat", "cool", "dry", "fan"])
                functions = sorted({_LEGACY_MODE_FUNCTION[m] for m in legacy if m in _LEGACY_MODE_FUNCTION})
            modes = modes_for_functions(functions)
        else:
            modes = list(defaults)
        power = {m: float(data.get("power_" + m) or defaults[m]) for m in modes}
        count = max(1, int(data.get("count") or 1))
        members = [m.strip() for m in (data.get("members") or "").split(",") if m.strip()]
        if atype == TYPE_EXTRACTOR_FAN:
            members = (members + [f"Fan {i + 1}" for i in range(len(members), count)])[:count]
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or APPLIANCE_TYPE_NAMES.get(atype, atype)),
            type=atype,
            power=power,
            modes=modes,
            functions=functions,
            standby_w=float(data.get("standby_w") or 0.0),
            min_setpoint=_opt_float(data.get("min_setpoint")),
            max_setpoint=_opt_float(data.get("max_setpoint")),
            inverter=bool(data.get("inverter", True)),
            hold_factor=float(data.get("hold_factor") or 0.4),
            idle_fan_w=_idle_fan_default(atype, data),
            manual_timeout_min=float(data.get("manual_timeout_min") or 0.0),
            off_detect_min=float(data.get("off_detect_min", 45.0) or 0.0),
            calculate_energy=bool(data.get("calculate_energy", True)) and atype != TYPE_HEAT_SOURCE,
            count=count if atype == TYPE_EXTRACTOR_FAN else 1,
            members=members if atype == TYPE_EXTRACTOR_FAN else [],
            airflow_m3h=float(data.get("airflow_m3h") or DEFAULT_FAN_AIRFLOW_M3H),
            power_entity=data.get("power_entity") or None,
            on_threshold_w=float(data.get("on_threshold_w") or 10.0),
            kind=str(data.get("kind") or "general"),
            active_above_w=_opt_float(data.get("active_above_w")),
            room=data.get("room") or None,
        )

    @property
    def is_heat_source(self) -> bool:
        return self.type == TYPE_HEAT_SOURCE

    @property
    def is_fan_group(self) -> bool:
        return self.type == TYPE_EXTRACTOR_FAN

    @property
    def has_setpoint(self) -> bool:
        """Heaters and heating/cooling HVAC units hold a temperature."""
        return MODE_HEAT in self.modes or MODE_COOL in self.modes

    @property
    def capabilities(self) -> list[str]:
        """Causes this appliance can produce."""
        mapping = _CAUSE_TO_MODE.get(self.type, {})
        return [c for c, m in mapping.items() if m in self.modes]

    def mode_for_cause(self, cause: str) -> str | None:
        """Operating mode that produces ``cause``."""
        mode = _CAUSE_TO_MODE.get(self.type, {}).get(cause)
        return mode if mode in self.modes else None

    def cause_for_mode(self, mode: str) -> str | None:
        """Reverse of ``mode_for_cause``."""
        for cause, m in _CAUSE_TO_MODE.get(self.type, {}).items():
            if m == mode:
                return cause
        return None

    def clamp_setpoint(self, value: float) -> float:
        """Keep a setpoint within the user-entered range."""
        if self.min_setpoint is not None:
            value = max(self.min_setpoint, value)
        if self.max_setpoint is not None:
            value = min(self.max_setpoint, value)
        return value

    def power_for(self, mode: str, idle: bool = False, units_on: int = 1, satisfied: bool = False) -> float:
        """Estimated power draw (W) in ``mode``.

        ``idle`` means the unit is on but not driving the temperature:

        * ``satisfied`` (the room is already past the setpoint, e.g. 17 °C with
          cooling set to 18 °C): the compressor/element is off and only the
          fan runs, inverter or not.
        * otherwise it is holding the setpoint between cycles: an inverter keeps
          running at reduced output, a fixed-speed unit rests on its fan.

        ``units_on`` is how many fans of a group are running.
        """
        if mode == MODE_OFF or mode not in self.power:
            return self.standby_w
        rated = self.power[mode] * max(units_on, 1)
        if idle:
            if self.inverter and not satisfied:
                return max(self.standby_w, rated * self.hold_factor)
            return max(self.standby_w, self.idle_fan_w)
        return rated


def _idle_fan_default(atype: str, data: dict[str, Any]) -> float:
    """Fan-only power: as configured, else the unit's filter/fan mode power, else 30 W for HVAC units."""
    if data.get("idle_fan_w") not in (None, ""):
        return float(data["idle_fan_w"])
    for key in ("power_filter", "power_fan"):  # power_fan: configs from before HVAC functions
        if data.get(key) not in (None, ""):
            return float(data[key])
    return 30.0 if atype == TYPE_AIRCON else 0.0


def _opt_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- Labels ------------------------------------------------------------------
#
# A label is what the user teaches and what the classifier predicts. Passive
# causes are labels on their own ("solar_gain"). Active causes are tied to the
# appliance producing them ("heating:abc123"), or stay generic ("heating") when
# no capable appliance is configured.


def make_label(cause: str, appliance_id: str | None = None) -> str:
    """Build a label key."""
    return cause if appliance_id is None else cause + LABEL_SEP + appliance_id


def split_label(label: str) -> tuple:
    """Return ``(cause, appliance_id_or_None)``."""
    if LABEL_SEP in label:
        cause, appliance_id = label.split(LABEL_SEP, 1)
        return cause, appliance_id
    return label, None


def available_labels(appliances: list[Appliance]) -> list[str]:
    """Every label the user can teach for this set of appliances."""
    labels: list[str] = []
    for cause in ACTIVE_CAUSES:
        capable = [a for a in appliances if cause in a.capabilities]
        if capable:
            labels.extend(make_label(cause, a.id) for a in capable)
        else:
            labels.append(cause)
    labels.extend(PASSIVE_CAUSES)
    return labels


def label_name(label: str, appliances: list[Appliance]) -> str:
    """Human-readable label, e.g. ``Cooling - Lounge aircon``."""
    cause, appliance_id = split_label(label)
    base = CAUSE_NAMES.get(cause, cause)
    if appliance_id is None:
        return base
    for appliance in appliances:
        if appliance.id == appliance_id:
            return f"{base} - {appliance.name}"
    return base
