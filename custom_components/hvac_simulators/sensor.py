"""Sensors for HVAC Simulators."""

from __future__ import annotations

import time

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HvacSimConfigEntry
from .appliances import TYPE_AIRCON, Appliance
from .const import CONF_OUTDOOR_TEMP
from .effectiveness import RATING_NAMES, RATINGS
from .entity import ApplianceEntity, HvacSimEntity
from .manager import HvacSimulatorManager
from .plugs import KIND_NAMES, PLUG_STATUSES
from .room import Room, RoomConfig
from .simulator import STATUSES

_MAX_PROBS_IN_ATTRS = 5


async def async_setup_entry(
    hass: HomeAssistant, entry: HvacSimConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    has_outdoor = bool(manager.config.get(CONF_OUTDOOR_TEMP))
    entities: list[SensorEntity] = []
    for room in manager.rooms.values():
        rc = room.config
        if rc.sensed:
            entities += [
                SuggestedModeSensor(manager, room),
                ConfidenceSensor(manager, room),
                LearningSensor(manager, room),
            ]
            entities.append(
                TrendSensor(
                    manager,
                    room,
                    "temperature_trend",
                    "Temperature trend",
                    "r_t",
                    "°C/h",
                    "mdi:thermometer-lines",
                )
            )
            entities += [EfficiencySensor(manager, room, *spec) for spec in EFFICIENCY_SENSORS]
        if rc.humidity:
            entities.append(
                TrendSensor(
                    manager, room, "humidity_trend", "Humidity trend", "r_h", "%/h", "mdi:water-percent"
                )
            )
        if rc.air_quality:
            entities.append(
                TrendSensor(
                    manager,
                    room,
                    "air_quality_trend",
                    "Air quality trend",
                    "r_aq",
                    "%/h",
                    "mdi:air-filter",
                    scale=100.0,
                )
            )
        if rc.co2:
            entities += [
                TrendSensor(manager, room, "co2_trend", "CO2 trend", "r_co2", "ppm/h", "mdi:molecule-co2"),
                TrendSensor(
                    manager,
                    room,
                    "air_changes",
                    "Air changes per hour",
                    "ach_obs",
                    "1/h",
                    "mdi:weather-windy",
                ),
                OccupancySensor(manager, room),
            ]
        # Rate of change since the last reading and over 5/10/15 minutes (hidden by default).
        for attr, label, unit, present in (
            ("t_in", "Temperature", "°C/h", rc.temperature),
            ("rh_in", "Humidity", "%/h", rc.humidity),
            ("co2", "CO2", "ppm/h", rc.co2),
        ):
            if present:
                entities += [RateSensor(manager, room, attr, label, unit, window) for window in RATE_KEYS]
        if has_outdoor:
            entities += [DifferentialSensor(manager, room, *d) for d in OUTDOOR_DIFFS if _has(rc, d[0])]
        if not rc.is_main and manager.main.config.sensed:
            entities += [RoomDifferenceSensor(manager, room, *d) for d in ROOM_DIFFS if _has(rc, d[0])]
        if room.heat_sources:
            entities += [
                DifferentialSensor(
                    manager,
                    room,
                    "temperature_rate_raw",
                    "Temperature trend (raw)",
                    "°C/h",
                    "mdi:thermometer-lines",
                ),
                TrendSensor(
                    manager,
                    room,
                    "appliance_heat",
                    "Appliance heat effect",
                    "gain_t",
                    "°C/h",
                    "mdi:heat-wave",
                ),
            ]
            if rc.humidity:
                entities += [
                    DifferentialSensor(
                        manager, room, "humidity_rate_raw", "Humidity trend (raw)", "%/h", "mdi:water-percent"
                    ),
                    TrendSensor(
                        manager,
                        room,
                        "appliance_moisture",
                        "Appliance moisture effect",
                        "gain_h",
                        "%/h",
                        "mdi:water-plus",
                    ),
                ]
    for source in manager.heat_sources:
        entities += [HeatSourceEffectSensor(manager, source), PlugStatusSensor(manager, source)]
    for appliance in manager.simulated:
        if appliance.is_fan_group:
            entities.append(FansOnSensor(manager, appliance))
        entities.append(AppliancePowerSensor(manager, appliance))
        entities.append(ApplianceModeSensor(manager, appliance))
        entities.append(ApplianceStateSensor(manager, appliance))
        if appliance.calculate_energy:
            entities.append(ApplianceEnergySensor(manager, appliance))
        if appliance.has_setpoint:
            entities.append(ApplianceSetpointSensor(manager, appliance))
        if manager.sims[appliance.id].effectiveness:
            entities.append(EffectivenessSensor(manager, appliance))
    async_add_entities(entities)


RATE_KEYS = ("last", "5m", "10m", "15m")
OUTDOOR_DIFFS = (
    ("temperature", "Indoor-outdoor temperature difference", "°C", "mdi:thermometer-chevron-up"),
    ("humidity", "Indoor-outdoor humidity difference", PERCENTAGE, "mdi:water-percent"),
    ("absolute_humidity_in", "Indoor absolute humidity", "g/m³", "mdi:water"),
    ("absolute_humidity_out", "Outdoor absolute humidity", "g/m³", "mdi:water-outline"),
    ("absolute_humidity", "Indoor-outdoor absolute humidity difference", "g/m³", "mdi:water-sync"),
    ("co2_excess", "CO2 above outdoor", "ppm", "mdi:molecule-co2"),
)
ROOM_DIFFS = (
    ("temperature", "Temperature difference to main room", "°C", "mdi:home-thermometer"),
    ("humidity", "Humidity difference to main room", PERCENTAGE, "mdi:water-percent"),
    ("absolute_humidity", "Absolute humidity difference to main room", "g/m³", "mdi:water-sync"),
)
# key, name, unit, icon, source ("today" field or summary key)
EFFICIENCY_SENSORS = (
    ("heat_loss_coefficient", "Heat loss coefficient", "1/h", "mdi:home-thermometer-outline"),
    ("heat_lost_ch", "Heat lost today", "°C·h", "mdi:thermometer-minus"),
    ("heat_leaked_in_ch", "Heat leaked in today", "°C·h", "mdi:thermometer-plus"),
    ("solar_gain_ch", "Solar gain today", "°C·h", "mdi:white-balance-sunny"),
    ("appliance_heat_ch", "Appliance heat today", "°C·h", "mdi:heat-wave"),
    ("air_changes", "Ventilation air changes today", "changes", "mdi:weather-windy"),
)


def _has(rc: RoomConfig, key: str) -> bool:
    """Whether a room has the sensors a differential needs."""
    if key in ("temperature",):
        return rc.temperature is not None
    if key == "co2_excess":
        return rc.co2 is not None
    if key.startswith("absolute_humidity"):
        return rc.temperature is not None and rc.humidity is not None
    return rc.humidity is not None


class SuggestedModeSensor(HvacSimEntity, SensorEntity):
    """What the engine thinks is going on right now."""

    _attr_name = "Suggested mode"
    _attr_icon = "mdi:hvac"

    def __init__(self, manager: HvacSimulatorManager, room: Room) -> None:
        super().__init__(manager, "suggested_mode", room)

    @callback
    def _refresh(self) -> None:
        m = self.room
        p = m.prediction
        if p is None:
            self._attr_native_value = "Collecting data"
            self._attr_extra_state_attributes = {
                "label": None,
                "reason": f"Needs about {int(m.window_s // 60 * 0.3) + 1} minutes of sensor history",
            }
            return
        top = sorted(p.probs.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_PROBS_IN_ATTRS]
        self._attr_native_value = m.label_name(p.label)
        self._attr_extra_state_attributes = {
            "label": p.label,
            "confidence": round(p.confidence * 100, 1),
            "needs_feedback": m.needs_feedback,
            "reasons": p.reasons,
            "candidates": {m.label_name(k): round(v * 100, 1) for k, v in top},
            "learned_weight": round(p.learned_weight * 100, 1),
            "openings_open": self.manager.openings_open(m.id),
            "opening_estimate": None if m.has_opening_sensors else m.opening_estimate,
            "features": m.features.as_dict() if m.features else None,
            "compressor_cycling": m.cycle,
        }


class ConfidenceSensor(HvacSimEntity, SensorEntity):
    _attr_name = "Suggestion confidence"
    _attr_icon = "mdi:gauge"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager: HvacSimulatorManager, room: Room) -> None:
        super().__init__(manager, "confidence", room)

    @callback
    def _refresh(self) -> None:
        p = self.room.prediction
        self._attr_native_value = None if p is None else round(p.confidence * 100, 1)


class TrendSensor(HvacSimEntity, SensorEntity):
    """Rate of change of an indoor reading."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        manager: HvacSimulatorManager,
        room: Room,
        key: str,
        name: str,
        feature: str,
        unit: str,
        icon: str,
        scale: float = 1.0,
    ) -> None:
        super().__init__(manager, key, room)
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._feature = feature
        self._scale = scale

    @callback
    def _refresh(self) -> None:
        f = self.room.features
        value = None if f is None else getattr(f, self._feature)
        self._attr_native_value = None if value is None else round(value * self._scale, 2)


class LearningSensor(HvacSimEntity, SensorEntity):
    """How much the integration has been taught."""

    _attr_name = "Learned examples"
    _attr_icon = "mdi:school"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, manager: HvacSimulatorManager, room: Room) -> None:
        super().__init__(manager, "learned_examples", room)

    @callback
    def _refresh(self) -> None:
        learner = self.room.learner
        accuracy = learner.accuracy
        self._attr_native_value = len(learner.samples)
        self._attr_extra_state_attributes = {
            "confirmed": learner.confirmed,
            "corrected": learner.corrected,
            "accuracy": None if accuracy is None else round(accuracy * 100, 1),
            "learned_weight": round(learner.learned_weight() * 100, 1),
            "passive_rate_closed": learner.k_closed,
            "passive_rate_open": learner.k_open,
            "last_taught": self.room.label_name(self.room.last_taught),
        }


class AppliancePowerSensor(ApplianceEntity, SensorEntity):
    """Estimated power draw. Point PowerCalc or the Energy dashboard at this."""

    _attr_name = "Power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "power")

    @callback
    def _refresh(self) -> None:
        self._attr_native_value = round(self.sim.power_w, 1)


class ApplianceEnergySensor(ApplianceEntity, SensorEntity):
    """Energy integrated from the estimated power."""

    _attr_name = "Energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "energy")

    @callback
    def _refresh(self) -> None:
        self._attr_native_value = round(self.sim.energy_kwh, 4)


class DifferentialSensor(HvacSimEntity, SensorEntity):
    """Indoor-outdoor differences and raw (uncompensated) rates."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        manager: HvacSimulatorManager,
        room: Room,
        key: str,
        name: str,
        unit: str,
        icon: str,
    ) -> None:
        super().__init__(manager, f"diff_{key}", room)
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        # No temperature device class: a difference must not be unit-converted like a reading.
        self._attr_suggested_display_precision = 2

    @callback
    def _refresh(self) -> None:
        value = self.room.differentials().get(self._key)
        self._attr_native_value = None if value is None else round(value, 3)


class HeatSourceEffectSensor(ApplianceEntity, SensorEntity):
    """How much a plugged-in heat source is warming (and humidifying) the space."""

    _attr_name = "Heat effect"
    _attr_icon = "mdi:heat-wave"
    _attr_native_unit_of_measurement = "°C/h"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "heat_effect")

    @callback
    def _refresh(self) -> None:
        model = self.room.gains.sources.get(self.appliance.id)
        effect_t, effect_h = self.room.source_effects(time.time()).get(self.appliance.id, (0.0, 0.0))
        self._attr_native_value = round(effect_t, 3)
        self._attr_extra_state_attributes = {
            "moisture_effect_pct_h": round(effect_h, 2),
            "power_w": self.manager.source_power.get(self.appliance.id),
            "learned_c_per_kw_h": None if model is None else round(model.per_kw_t, 3),
            "learned_rh_per_kw_h": None if model is None else round(model.per_kw_h, 2),
            "samples": 0 if model is None else len(model.samples),
            "room": self.room.name,
        }


class FansOnSensor(ApplianceEntity, SensorEntity):
    """How many fans of a group are running, and which (once each is calibrated)."""

    _attr_name = "Fans on"
    _attr_icon = "mdi:fan"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "fans_on")

    @callback
    def _refresh(self) -> None:
        sim = self.sim
        group = self.room.fan_groups[self.appliance.id]
        estimate = self.room.fan_estimates.get(self.appliance.id) or {}
        self._attr_native_value = sim.units_on
        self._attr_extra_state_attributes = {
            "of": self.appliance.count,
            "likely_on": sim.units_on_members,
            "co2_estimate": estimate.get("count"),
            "excess_air_changes": None
            if estimate.get("excess_ach") is None
            else round(estimate["excess_ach"], 3),
            "per_fan_air_changes": {m: round(group.member_ach(m), 3) for m in self.appliance.members},
            "calibrations": len(group.calibrations),
        }


class OccupancySensor(HvacSimEntity, SensorEntity):
    """People estimated from CO2 generation."""

    _attr_name = "Estimated people"
    _attr_icon = "mdi:account-group"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "people"

    def __init__(self, manager: HvacSimulatorManager, room: Room) -> None:
        super().__init__(manager, "occupancy", room)

    @callback
    def _refresh(self) -> None:
        est = self.room.occupancy_estimate
        if est is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {"activity": self.manager.activity}
            return
        self._attr_native_value = max(0, round(est["people_smoothed"]))
        self._attr_extra_state_attributes = {
            "estimate": round(est["people_smoothed"], 2),
            "instant": round(est["people"], 2),
            "range_if_exercising": round(est["people_min"], 1),
            "range_if_sleeping": round(est["people_max"], 1),
            "activity": est["activity"],
            "ventilation": est["vent_state"],
            "air_changes_per_hour": round(est["ach"], 2),
            "co2_generation_ppm_h": round(est["generation_ppm_h"], 1),
            "reliable": est["reliable"],
            "calibrations": est["calibrations"],
        }


class ApplianceStateSensor(ApplianceEntity, SensorEntity):
    """Active (running), idle (on, resting at setpoint between cycles) or off."""

    _attr_name = "State"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATUSES)
    _attr_icon = "mdi:power-cycle"

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "state")

    @callback
    def _refresh(self) -> None:
        sim = self.sim
        duty = sim.duty_cycle
        self._attr_native_value = sim.status
        self._attr_extra_state_attributes = {
            "mode": sim.mode,
            "idle_reason": sim.idle_reason,
            "thermostat_offset": sim.thermostat_offset,
            "cycles": sim.cycles,
            "duty_cycle": None if duty is None else round(duty * 100, 1),
            "manual_expires_at": sim.manual_expires_at,
            "observed_in_room": self.room.name,
        }


class EffectivenessSensor(ApplianceEntity, SensorEntity):
    """Heating/cooling effectiveness vs. clean-filter baseline, adjusted for weather."""

    _attr_name = "Effectiveness"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(RATINGS)
    _attr_icon = "mdi:air-filter"

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "effectiveness")

    @callback
    def _refresh(self) -> None:
        sim = self.sim
        self._attr_native_value = sim.rating
        attrs = {}
        for mode, tracker in sim.effectiveness.items():
            ratio = tracker.ratio()
            rating = tracker.rating()
            attrs[mode] = {
                "rating": RATING_NAMES.get(rating) if rating else ("learning" if tracker.learning else None),
                "performance_percent": None if ratio is None else round(ratio * 100, 1),
                "samples": len(tracker.samples),
                "last_filter_event": tracker.filter_events[-1] if tracker.filter_events else None,
            }
        self._attr_extra_state_attributes = attrs


class ApplianceModeSensor(ApplianceEntity, SensorEntity):
    """Current operating mode (heat/cool/dry/fan/on/off); stays put while cycling."""

    _attr_name = "Mode"

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "mode")
        self._attr_icon = "mdi:air-conditioner" if appliance.type == TYPE_AIRCON else "mdi:state-machine"

    @callback
    def _refresh(self) -> None:
        sim = self.sim
        self._attr_native_value = sim.mode
        self._attr_extra_state_attributes = {
            "status": sim.status,
            "manual_mode": sim.manual_mode,
            "setpoint": sim.setpoint,
            "cycles": sim.cycles,
        }


class ApplianceSetpointSensor(ApplianceEntity, SensorEntity):
    """Setpoint from cycling turn points and calibrations (or entered manually)."""

    _attr_name = "Detected setpoint"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "setpoint")

    @callback
    def _refresh(self) -> None:
        sim = self.sim
        self._attr_native_value = sim.setpoint
        self._attr_extra_state_attributes = {
            "source": "manual" if sim.manual_setpoint is not None else "detected/calibrated",
            "detected": sim.detected_setpoint(),
            "calibrated": sim.calibrated_setpoint(),
            "turn_points": sim.turn_points,
            "calibrations": sum(1 for c in sim.calibrations if c.get("setpoint") is not None),
            "min_setpoint": self.appliance.min_setpoint,
            "max_setpoint": self.appliance.max_setpoint,
        }


class RateSensor(HvacSimEntity, SensorEntity):
    """Rate of change since the last reading, or over a fixed window."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:chart-line-variant"

    def __init__(
        self, manager: HvacSimulatorManager, room: Room, attr: str, label: str, unit: str, window: str
    ) -> None:
        super().__init__(manager, f"rate_{attr}_{window}", room)
        self._attr = attr
        self._window = window
        suffix = "since last reading" if window == "last" else f"over {window[:-1]} min"
        self._attr_name = f"{label} rate {suffix}"
        self._attr_native_unit_of_measurement = unit

    @callback
    def _refresh(self) -> None:
        value = self.room.rates(self._attr, time.time()).get(self._window)
        self._attr_native_value = None if value is None else round(value, 3)


class RoomDifferenceSensor(HvacSimEntity, SensorEntity):
    """This room minus the main room (e.g. laundry vs living room)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(
        self, manager: HvacSimulatorManager, room: Room, key: str, name: str, unit: str, icon: str
    ) -> None:
        super().__init__(manager, f"vs_main_{key}", room)
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    @callback
    def _refresh(self) -> None:
        value = self.room.differentials(self.manager.main).get(self._key)
        self._attr_native_value = None if value is None else round(value, 3)


class EfficiencySensor(HvacSimEntity, SensorEntity):
    """Building efficiency record: heat lost, leaked in, solar gain... after removing HVAC and appliances."""

    def __init__(
        self, manager: HvacSimulatorManager, room: Room, key: str, name: str, unit: str, icon: str
    ) -> None:
        super().__init__(manager, f"efficiency_{key}", room)
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_suggested_display_precision = 3
        if key == "heat_loss_coefficient":
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            # Resets at local midnight; the recorder keeps the daily history.
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @callback
    def _refresh(self) -> None:
        tracker = self.room.efficiency
        summary = tracker.summary()
        if self._key == "heat_loss_coefficient":
            self._attr_native_value = summary.get("heat_loss_coefficient")
            self._attr_extra_state_attributes = {
                "meaning": "°C per hour the room drifts per °C of indoor-outdoor difference, everything off and closed. Lower is better insulated.",
                "last_30_days": summary,
                "air_gaps": self.room.config.air_gaps,
                "air_gap_notes": self.room.config.air_gap_notes or None,
                "volume_m3": self.room.config.volume_m3,
            }
            return
        today = tracker.today(dt_util.now().date().isoformat())
        self._attr_native_value = today.get(self._key)
        self._attr_extra_state_attributes = {
            "average_per_day_30d": (summary.get("per_day") or {}).get(self._key)
        }


class PlugStatusSensor(ApplianceEntity, SensorEntity):
    """Active / idle / off from the appliance's smart plug (fridge cycles, dryer running...)."""

    _attr_name = "Status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(PLUG_STATUSES)
    _attr_icon = "mdi:power-plug"

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "plug_status")

    @callback
    def _refresh(self) -> None:
        plug = self.manager.plugs[self.appliance.id]
        duty = plug.duty_cycle
        self._attr_native_value = plug.status
        self._attr_extra_state_attributes = {
            "kind": KIND_NAMES.get(plug.kind, plug.kind),
            "power_w": plug.power_w,
            "cycles": plug.cycles,
            "duty_cycle_6h": None if duty is None else round(duty * 100, 1),
            "running": plug.running,
            "last_active_min": None if plug.last_active_s is None else round(plug.last_active_s / 60, 1),
            "last_run_min": None if plug.last_run_s is None else round(plug.last_run_s / 60, 1),
            "active_above_w": plug.active_above_w,
            "off_below_w": plug.off_below_w,
        }
