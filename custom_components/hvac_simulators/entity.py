"""Base entities for HVAC Simulators."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .appliances import APPLIANCE_TYPE_NAMES, Appliance
from .const import DOMAIN, MANUFACTURER, SIGNAL_UPDATE
from .manager import HvacSimulatorManager
from .simulator import ApplianceSimulator


class HvacSimEntity(Entity):
    """Entity on the space device, refreshed on every evaluation."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: HvacSimulatorManager, key: str) -> None:
        self.manager = manager
        self._attr_unique_id = f"{manager.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.entry_id)},
            name=manager.entry.title,
            manufacturer=MANUFACTURER,
            model="Space",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE.format(self.manager.entry_id), self._handle_update
            )
        )
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        self._refresh()
        self.async_write_ha_state()

    @callback
    def _refresh(self) -> None:
        """Pull the latest values from the manager."""


class ApplianceEntity(HvacSimEntity):
    """Entity on an appliance device (linked to its space)."""

    def __init__(self, manager: HvacSimulatorManager, appliance: Appliance, key: str) -> None:
        super().__init__(manager, f"{appliance.id}_{key}")
        self.appliance = appliance
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{manager.entry_id}_{appliance.id}")},
            name=appliance.name,
            manufacturer=MANUFACTURER,
            model=APPLIANCE_TYPE_NAMES.get(appliance.type, appliance.type),
            via_device=(DOMAIN, manager.entry_id),
        )

    @property
    def sim(self) -> ApplianceSimulator:
        return self.manager.sims[self.appliance.id]
