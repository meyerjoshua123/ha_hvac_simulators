"""Binary sensors for HVAC Simulators."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HvacSimConfigEntry
from .appliances import Appliance
from .effectiveness import rating_rank
from .entity import ApplianceEntity, HvacSimEntity
from .manager import HvacSimulatorManager


async def async_setup_entry(
    hass: HomeAssistant, entry: HvacSimConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    entities: list[BinarySensorEntity] = [NeedsFeedbackSensor(manager), OpeningsSensor(manager)]
    if manager.heat_sources:
        entities.append(ApplianceGainsSensor(manager))
    entities.extend(HeatSourceRunningSensor(manager, s) for s in manager.heat_sources)
    for appliance in manager.simulated:
        entities.append(ApplianceRunningSensor(manager, appliance))
        if manager.sims[appliance.id].effectiveness:
            entities.append(FilterAttentionSensor(manager, appliance))
    async_add_entities(entities)


class NeedsFeedbackSensor(HvacSimEntity, BinarySensorEntity):
    """On when an active suggestion is uncertain: confirm or correct it to teach."""

    _attr_name = "Needs feedback"
    _attr_icon = "mdi:help-circle-outline"

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "needs_feedback")

    @callback
    def _refresh(self) -> None:
        self._attr_is_on = self.manager.needs_feedback
        p = self.manager.prediction
        self._attr_extra_state_attributes = {
            "suggestion": self.manager.label_name(p.label) if p else None,
        }


class OpeningsSensor(HvacSimEntity, BinarySensorEntity):
    """Any configured door or window open."""

    _attr_name = "Door or window open"
    _attr_device_class = BinarySensorDeviceClass.OPENING

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "openings")

    @callback
    def _refresh(self) -> None:
        open_now = self.manager.openings_open()
        self._attr_is_on = bool(open_now)
        self._attr_extra_state_attributes = {"open": open_now}


class ApplianceRunningSensor(ApplianceEntity, BinarySensorEntity):
    """Whether the appliance is on (active or idle between cycles).

    PowerCalc can use this with a fixed wattage; the State sensor splits on into
    active and idle.
    """

    _attr_name = "On"
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "running")

    @callback
    def _refresh(self) -> None:
        sim = self.sim
        self._attr_is_on = sim.is_on
        self._attr_extra_state_attributes = {"mode": sim.mode, "status": sim.status}


class FilterAttentionSensor(ApplianceEntity, BinarySensorEntity):
    """On when effectiveness has dropped to the notification threshold or worse."""

    _attr_name = "Filter needs attention"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:air-filter"

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "filter_attention")

    @callback
    def _refresh(self) -> None:
        rating = self.sim.rating
        self._attr_is_on = rating is not None and rating_rank(rating) >= rating_rank(
            self.manager.notify_rating
        )
        self._attr_extra_state_attributes = {"rating": rating}


class ApplianceGainsSensor(HvacSimEntity, BinarySensorEntity):
    """On while plugged-in appliances (dryer, oven...) are noticeably changing the air.

    Use it as a condition so e.g. a humidifier/dehumidifier automation doesn't
    react to a tumble dryer.
    """

    _attr_name = "Appliance heat or moisture"
    _attr_icon = "mdi:tumble-dryer"

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "appliance_gains")

    @callback
    def _refresh(self) -> None:
        f = self.manager.features
        tuning = self.manager.base_tuning
        gain_t = f.gain_t if f else 0.0
        gain_h = f.gain_h if f else 0.0
        self._attr_is_on = abs(gain_t) >= tuning.stable_rate or abs(gain_h) >= 1.0
        self._attr_extra_state_attributes = {
            "heat_c_per_h": round(gain_t, 3),
            "moisture_pct_per_h": round(gain_h, 2),
            "running": [s.name for s in self.manager.heat_sources if self.manager.source_power.get(s.id)],
        }


class HeatSourceRunningSensor(ApplianceEntity, BinarySensorEntity):
    """Heat source drawing more than its on-threshold."""

    _attr_name = "Running"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "running")

    @callback
    def _refresh(self) -> None:
        self._attr_is_on = bool(self.manager.source_power.get(self.appliance.id))
