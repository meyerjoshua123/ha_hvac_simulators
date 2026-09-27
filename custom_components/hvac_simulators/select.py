"""Selects for HVAC Simulators: teach the actual mode, and set appliance modes manually."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HvacSimConfigEntry
from .appliances import Appliance
from .entity import ApplianceEntity, HvacSimEntity
from .manager import ACTIVITY_AUTO, HvacSimulatorManager
from .occupancy import ACTIVITIES
from .room import Room
from .simulator import MANUAL_AUTO

OPTION_NONE = "Not taught"


async def async_setup_entry(
    hass: HomeAssistant, entry: HvacSimConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    entities: list[SelectEntity] = [
        TeachSelect(manager, r) for r in manager.rooms.values() if r.config.sensed
    ]
    if any(r.config.co2 for r in manager.rooms.values()):
        entities.append(ActivitySelect(manager))
    entities.extend(ManualModeSelect(manager, a) for a in manager.simulated)
    async_add_entities(entities)


class TeachSelect(HvacSimEntity, SelectEntity):
    """Pick what is actually happening right now; the integration learns from it."""

    _attr_name = "Actual mode (teach)"
    _attr_icon = "mdi:school-outline"

    def __init__(self, manager: HvacSimulatorManager, room: Room) -> None:
        super().__init__(manager, "teach", room)
        self._refresh()

    @callback
    def _refresh(self) -> None:
        names = [self.room.label_name(label) for label in self.room.labels]
        self._attr_options = [OPTION_NONE, *names]
        last = self.room.label_name(self.room.last_taught)
        self._attr_current_option = last if last in names else OPTION_NONE

    async def async_select_option(self, option: str) -> None:
        if option == OPTION_NONE:
            return
        label = self.room.label_from_name(option)
        if label is None or not self.manager.teach(self.room.id, label):
            raise HomeAssistantError("Not enough sensor history yet to learn from")


class ManualModeSelect(ApplianceEntity, SelectEntity):
    """Override what the appliance is doing (auto = inferred)."""

    _attr_name = "Manual mode"
    _attr_icon = "mdi:hand-back-right"

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance) -> None:
        super().__init__(manager, appliance, "manual_mode")
        self._attr_options = [MANUAL_AUTO, "off", *appliance.modes]

    @callback
    def _refresh(self) -> None:
        self._attr_current_option = self.sim.manual_mode

    async def async_select_option(self, option: str) -> None:
        self.manager.set_manual(self.appliance.id, option, None)


class ActivitySelect(HvacSimEntity, SelectEntity):
    """What people are doing; auto = sleeping during sleep hours, otherwise resting."""

    _attr_name = "Activity level"
    _attr_icon = "mdi:run"
    _attr_options = [ACTIVITY_AUTO, *ACTIVITIES]

    def __init__(self, manager: HvacSimulatorManager) -> None:
        super().__init__(manager, "activity")

    @callback
    def _refresh(self) -> None:
        self._attr_current_option = self.manager.activity_override
        self._attr_extra_state_attributes = {"effective_activity": self.manager.activity}

    async def async_select_option(self, option: str) -> None:
        self.manager.set_activity(option)
