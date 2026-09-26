"""Sensors for HVAC Simulators."""

from __future__ import annotations

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

from . import HvacSimConfigEntry
from .appliances import TYPE_AIRCON, Appliance
from .const import CONF_CO2
from .effectiveness import RATING_NAMES, RATINGS
from .entity import ApplianceEntity, HvacSimEntity
from .manager import HvacSimulatorManager
from .simulator import STATUSES

_MAX_PROBS_IN_ATTRS = 5


async def async_setup_entry(
    hass: HomeAssistant, entry: HvacSimConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    entities: list[SensorEntity] = [
        SuggestedModeSensor(manager),
        ConfidenceSensor(manager),
        TrendSensor(
            manager, "temperature_trend", "Temperature trend", "r_t", "°C/h", "mdi:thermometer-lines"
        ),
        TrendSensor(manager, "humidity_trend", "Humidity trend", "r_h", "%/h", "mdi:water-percent"),
        TrendSensor(
            manager, "air_quality_trend", "Air quality trend", "r_aq", "%/h", "mdi:air-filter", scale=100.0
        ),
        TrendSensor(manager, "co2_trend", "CO2 trend", "r_co2", "ppm/h", "mdi:molecule-co2"),
        TrendSensor(manager, "air_changes", "Air changes per hour", "ach_obs", "1/h", "mdi:weather-windy"),
        LearningSensor(manager),
    ]
    if manager.config.get(CONF_CO2):
        entities.append(OccupancySensor(manager))
    diffs = [
        ("temperature", "Indoor-outdoor temperature difference", "°C", "mdi:thermometer-chevron-up"),
        ("humidity", "Indoor-outdoor humidity difference", PERCENTAGE, "mdi:water-percent"),
        ("absolute_humidity_in", "Indoor absolute humidity", "g/m³", "mdi:water"),
        ("absolute_humidity_out", "Outdoor absolute humidity", "g/m³", "mdi:water-outline"),
        ("absolute_humidity", "Indoor-outdoor absolute humidity difference", "g/m³", "mdi:water-sync"),
        ("co2_excess", "CO2 above outdoor", "ppm", "mdi:molecule-co2"),
    ]
    entities.extend(DifferentialSensor(manager, *d) for d in diffs)
    if manager.heat_sources:
        entities += [
            DifferentialSensor(
                manager, "temperature_rate_raw", "Temperature trend (raw)", "°C/h", "mdi:thermometer-lines"
            ),
            DifferentialSensor(
                manager, "humidity_rate_raw", "Humidity trend (raw)", "%/h", "mdi:water-percent"
            ),
            TrendSensor(
                manager, "appliance_heat", "Appliance heat effect", "gain_t", "°C/h", "mdi:heat-wave"
            ),
            TrendSensor(
                manager, "appliance_moisture", "Appliance moisture effect", "gain_h", "%/h", "mdi:water-plus"
            ),
        ]
    for source in manager.heat_sources:
        entities.append(HeatSourceEffectSensor(manager, source))
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


class SuggestedModeSensor(HvacSimEntity, SensorEntity):
    """What the engine thinks is going on right now."""

    _attr_name = "Suggested mode"
    _attr_icon = "mdi:hvac"

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "suggested_mode")

    @callback
    def _refresh(self) -> None:
        m = self.manager
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
            "openings_open": m.openings_open(),
            "features": m.features.as_dict() if m.features else None,
        }


class ConfidenceSensor(HvacSimEntity, SensorEntity):
    _attr_name = "Suggestion confidence"
    _attr_icon = "mdi:gauge"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "confidence")

    @callback
    def _refresh(self) -> None:
        p = self.manager.prediction
        self._attr_native_value = None if p is None else round(p.confidence * 100, 1)


class TrendSensor(HvacSimEntity, SensorEntity):
    """Rate of change of an indoor reading."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        manager: HvacSimulatorManager,
        key: str,
        name: str,
        feature: str,
        unit: str,
        icon: str,
        scale: float = 1.0,
    ) -> None:
        super().__init__(manager, key)
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._feature = feature
        self._scale = scale

    @callback
    def _refresh(self) -> None:
        f = self.manager.features
        value = None if f is None else getattr(f, self._feature)
        self._attr_native_value = None if value is None else round(value * self._scale, 2)


class LearningSensor(HvacSimEntity, SensorEntity):
    """How much the integration has been taught."""

    _attr_name = "Learned examples"
    _attr_icon = "mdi:school"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "learned_examples")

    @callback
    def _refresh(self) -> None:
        learner = self.manager.learner
        accuracy = learner.accuracy
        self._attr_native_value = len(learner.samples)
        self._attr_extra_state_attributes = {
            "confirmed": learner.confirmed,
            "corrected": learner.corrected,
            "accuracy": None if accuracy is None else round(accuracy * 100, 1),
            "learned_weight": round(learner.learned_weight() * 100, 1),
            "passive_rate_closed": learner.k_closed,
            "passive_rate_open": learner.k_open,
            "last_taught": self.manager.label_name(self.manager.last_taught),
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
        key: str,
        name: str,
        unit: str,
        icon: str,
    ) -> None:
        super().__init__(manager, f"diff_{key}")
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        # No temperature device class: a difference must not be unit-converted like a reading.
        self._attr_suggested_display_precision = 2

    @callback
    def _refresh(self) -> None:
        value = self.manager.differentials.get(self._key)
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
        model = self.manager.gains.sources.get(self.appliance.id)
        effect_t, effect_h = self.manager.source_effects().get(self.appliance.id, (0.0, 0.0))
        self._attr_native_value = round(effect_t, 3)
        self._attr_extra_state_attributes = {
            "moisture_effect_pct_h": round(effect_h, 2),
            "power_w": self.manager.source_power.get(self.appliance.id),
            "learned_c_per_kw_h": None if model is None else round(model.per_kw_t, 3),
            "learned_rh_per_kw_h": None if model is None else round(model.per_kw_h, 2),
            "samples": 0 if model is None else len(model.samples),
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
        group = self.manager.fan_groups[self.appliance.id]
        estimate = self.manager.fan_estimates.get(self.appliance.id) or {}
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

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "occupancy")

    @callback
    def _refresh(self) -> None:
        est = self.manager.occupancy_estimate
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
            "cycles": sim.cycles,
            "duty_cycle": None if duty is None else round(duty * 100, 1),
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
