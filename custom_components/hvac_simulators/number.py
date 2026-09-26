"""Number entities for HVAC Simulators: manual setpoint per heater/aircon."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HvacSimConfigEntry
from .appliances import Appliance
from .const import CONF_CO2
from .entity import ApplianceEntity, HvacSimEntity
from .manager import HvacSimulatorManager


async def async_setup_entry(
    hass: HomeAssistant, entry: HvacSimConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    entities: list[NumberEntity] = [SetpointNumber(manager, a) for a in manager.simulated if a.has_setpoint]
    if manager.config.get(CONF_CO2):
        entities.append(PeopleCalibrationNumber(manager))
        entities.extend(FansCalibrationNumber(manager, a) for a in manager.simulated if a.is_fan_group)
    async_add_entities(entities)


class SetpointNumber(ApplianceEntity, NumberEntity):
    """Enter the setpoint you set on the unit. Shows the detected one until you do."""

    _attr_name = "Setpoint"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "setpoint_number")
        self._attr_native_min_value = appliance.min_setpoint if appliance.min_setpoint is not None else 5.0
        self._attr_native_max_value = appliance.max_setpoint if appliance.max_setpoint is not None else 35.0

    @callback
    def _refresh(self) -> None:
        self._attr_native_value = self.sim.setpoint

    async def async_set_native_value(self, value: float) -> None:
        self.manager.set_manual(self.appliance.id, None, value)


class PeopleCalibrationNumber(HvacSimEntity, NumberEntity):
    """Enter how many people are home right now to calibrate the CO2 occupancy model.

    Enter 0 while everyone is out to teach it how fast this space airs out.
    """

    _attr_name = "People home (calibrate)"
    _attr_icon = "mdi:account-check"
    _attr_native_min_value = 0
    _attr_native_max_value = 30
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "people_calibration")
        self._last: float | None = None

    @callback
    def _refresh(self) -> None:
        self._attr_native_value = self._last

    async def async_set_native_value(self, value: float) -> None:
        if not self.manager.calibrate_occupancy(int(value), None):
            raise HomeAssistantError("Need CO2 history before calibrating; try again in a few minutes")
        self._last = value


class FansCalibrationNumber(ApplianceEntity, NumberEntity):
    """Enter how many fans of the group are running right now (0 = none) to calibrate."""

    _attr_name = "Fans running (calibrate)"
    _attr_icon = "mdi:fan-alert"
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "fans_calibration")
        self._attr_native_max_value = appliance.count
        self._last: float | None = None

    @callback
    def _refresh(self) -> None:
        self._attr_native_value = self._last

    async def async_set_native_value(self, value: float) -> None:
        if not self.manager.calibrate_fans(self.appliance.id, None, int(value)):
            raise HomeAssistantError("Need CO2 history above outdoor levels before calibrating fans")
        self._last = value
