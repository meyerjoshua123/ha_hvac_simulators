"""Buttons for HVAC Simulators."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HvacSimConfigEntry
from .appliances import Appliance
from .entity import ApplianceEntity, HvacSimEntity
from .manager import HvacSimulatorManager


async def async_setup_entry(
    hass: HomeAssistant, entry: HvacSimConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    entities: list[ButtonEntity] = [ConfirmButton(manager), ResetLearningButton(manager)]
    for appliance in manager.simulated:
        if appliance.has_setpoint:
            entities.append(ClearSetpointButton(manager, appliance))
        if manager.sims[appliance.id].effectiveness:
            entities.append(FilterCleanedButton(manager, appliance))
    async_add_entities(entities)


class ConfirmButton(HvacSimEntity, ButtonEntity):
    """Tell the integration its current suggestion is right."""

    _attr_name = "Suggestion is correct"
    _attr_icon = "mdi:thumb-up"

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "confirm")

    async def async_press(self) -> None:
        if not self.manager.confirm():
            raise HomeAssistantError("No suggestion yet; wait for sensor history to build up")


class ResetLearningButton(HvacSimEntity, ButtonEntity):
    _attr_name = "Reset learning"
    _attr_icon = "mdi:restore"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "reset_learning")

    async def async_press(self) -> None:
        self.manager.reset_learning()


class ClearSetpointButton(ApplianceEntity, ButtonEntity):
    """Forget the manual setpoint and go back to the detected one."""

    _attr_name = "Use detected setpoint"
    _attr_icon = "mdi:thermostat-auto"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "clear_setpoint")

    async def async_press(self) -> None:
        self.manager.clear_manual_setpoint(self.appliance.id)


class FilterCleanedButton(ApplianceEntity, ButtonEntity):
    """Calibration: filter just cleaned/replaced, start a fresh effectiveness baseline."""

    _attr_name = "Filter cleaned"
    _attr_icon = "mdi:air-filter"

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "filter_cleaned")

    async def async_press(self) -> None:
        self.manager.calibrate(self.appliance.id, None, None, None, "clean")
