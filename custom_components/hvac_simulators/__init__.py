"""HVAC Simulators: infer what your heating, cooling and ventilation are doing."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_ACTIVITY,
    ATTR_FANS,
    ATTR_FANS_ON,
    ATTR_FILTER,
    ATTR_LABEL,
    ATTR_MODE,
    ATTR_PEOPLE,
    ATTR_SETPOINT,
    ATTR_STATUS,
    DOMAIN,
    SERVICE_CALIBRATE,
    SERVICE_CALIBRATE_OCCUPANCY,
    SERVICE_CONFIRM,
    SERVICE_RESET_ENERGY,
    SERVICE_RESET_LEARNING,
    SERVICE_SET_MANUAL,
    SERVICE_TEACH,
)
from .manager import HvacSimulatorManager
from .occupancy import ACTIVITIES
from .simulator import STATUSES

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HvacSimConfigEntry = ConfigEntry[HvacSimulatorManager]


def appliance_identifier(entry_id: str, appliance_id: str) -> str:
    """Device identifier for an appliance under a space."""
    return f"{entry_id}_{appliance_id}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once for all entries."""

    def resolve(call: ServiceCall) -> tuple[HvacSimulatorManager, str | None]:
        device = dr.async_get(hass).async_get(call.data[ATTR_DEVICE_ID])
        if device is None:
            raise ServiceValidationError("Unknown device")
        for domain, ident in device.identifiers:
            if domain != DOMAIN:
                continue
            for entry in hass.config_entries.async_entries(DOMAIN):
                manager = getattr(entry, "runtime_data", None)
                if not isinstance(manager, HvacSimulatorManager):
                    continue
                if ident == entry.entry_id:
                    return manager, None
                for appliance in manager.appliances:
                    if ident == appliance_identifier(entry.entry_id, appliance.id):
                        return manager, appliance.id
        raise ServiceValidationError("Device is not an HVAC Simulators space or appliance")

    async def handle_teach(call: ServiceCall) -> None:
        manager, _ = resolve(call)
        label = manager.label_from_name(call.data[ATTR_LABEL])
        if label is None:
            raise ServiceValidationError(f"Unknown label. Valid: {', '.join(manager.labels)}")
        if not manager.teach(label):
            raise ServiceValidationError("Not enough sensor history yet to learn from")

    async def handle_confirm(call: ServiceCall) -> None:
        manager, _ = resolve(call)
        if not manager.confirm():
            raise ServiceValidationError("There is no suggestion to confirm yet")

    async def handle_reset_learning(call: ServiceCall) -> None:
        manager, _ = resolve(call)
        manager.reset_learning()

    def resolve_simulated(call: ServiceCall) -> tuple[HvacSimulatorManager, str]:
        manager, appliance_id = resolve(call)
        if appliance_id is None or appliance_id not in manager.sims:
            raise ServiceValidationError("Pick an appliance device (not the space or a heat source)")
        return manager, appliance_id

    async def handle_set_manual(call: ServiceCall) -> None:
        manager, appliance_id = resolve_simulated(call)
        mode = call.data.get(ATTR_MODE)
        sim = manager.sims[appliance_id]
        if mode is not None and mode not in ("auto", "off", *sim.appliance.modes):
            raise ServiceValidationError(f"Mode must be one of: auto, off, {', '.join(sim.appliance.modes)}")
        manager.set_manual(appliance_id, mode, call.data.get(ATTR_SETPOINT))
        if ATTR_FANS_ON in call.data:
            manager.set_manual_units(appliance_id, call.data[ATTR_FANS_ON])

    async def handle_reset_energy(call: ServiceCall) -> None:
        manager, appliance_id = resolve(call)
        targets = [appliance_id] if appliance_id else list(manager.sims)
        for target in targets:
            if target in manager.sims:
                manager.reset_energy(target)

    async def handle_calibrate(call: ServiceCall) -> None:
        manager, appliance_id = resolve_simulated(call)
        fans = call.data.get(ATTR_FANS)
        fans_on = call.data.get(ATTR_FANS_ON)
        if fans is not None or fans_on is not None:
            if appliance_id not in manager.fan_groups:
                raise ServiceValidationError("fans / fans_on only apply to extractor fan groups")
            if not manager.calibrate_fans(appliance_id, fans, fans_on):
                raise ServiceValidationError(
                    "Needs a CO2 sensor reading well above outdoor levels, and valid fan names"
                )
        mode = call.data.get(ATTR_MODE)
        sim = manager.sims[appliance_id]
        if mode is not None and mode not in ("off", *sim.appliance.modes):
            raise ServiceValidationError(f"Mode must be one of: off, {', '.join(sim.appliance.modes)}")
        if any(k in call.data for k in (ATTR_MODE, ATTR_STATUS, ATTR_SETPOINT, ATTR_FILTER)):
            manager.calibrate(
                appliance_id,
                mode,
                call.data.get(ATTR_STATUS),
                call.data.get(ATTR_SETPOINT),
                call.data.get(ATTR_FILTER),
            )

    async def handle_calibrate_occupancy(call: ServiceCall) -> None:
        manager, _ = resolve(call)
        if not manager.calibrate_occupancy(call.data[ATTR_PEOPLE], call.data.get(ATTR_ACTIVITY)):
            raise ServiceValidationError(
                "Needs a CO2 sensor and a few minutes of CO2 history before calibrating"
            )

    device_schema = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})
    hass.services.async_register(
        DOMAIN,
        SERVICE_TEACH,
        handle_teach,
        schema=device_schema.extend({vol.Required(ATTR_LABEL): cv.string}),
    )
    hass.services.async_register(DOMAIN, SERVICE_CONFIRM, handle_confirm, schema=device_schema)
    hass.services.async_register(DOMAIN, SERVICE_RESET_LEARNING, handle_reset_learning, schema=device_schema)
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MANUAL,
        handle_set_manual,
        schema=device_schema.extend(
            {
                vol.Optional(ATTR_MODE): cv.string,
                vol.Optional(ATTR_SETPOINT): vol.Coerce(float),
                vol.Optional(ATTR_FANS_ON): vol.All(vol.Coerce(int), vol.Range(min=0, max=50)),
            }
        ),
    )
    hass.services.async_register(DOMAIN, SERVICE_RESET_ENERGY, handle_reset_energy, schema=device_schema)
    hass.services.async_register(
        DOMAIN,
        SERVICE_CALIBRATE,
        handle_calibrate,
        schema=device_schema.extend(
            {
                vol.Optional(ATTR_MODE): cv.string,
                vol.Optional(ATTR_STATUS): vol.In(STATUSES),
                vol.Optional(ATTR_SETPOINT): vol.Coerce(float),
                vol.Optional(ATTR_FILTER): vol.In(("clean", "ok", "dirty")),
                vol.Optional(ATTR_FANS): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_FANS_ON): vol.All(vol.Coerce(int), vol.Range(min=0, max=50)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CALIBRATE_OCCUPANCY,
        handle_calibrate_occupancy,
        schema=device_schema.extend(
            {
                vol.Required(ATTR_PEOPLE): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                vol.Optional(ATTR_ACTIVITY): vol.In(ACTIVITIES),
            }
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HvacSimConfigEntry) -> bool:
    """Set up a monitored space."""
    manager = HvacSimulatorManager(hass, entry)
    entry.runtime_data = manager
    _remove_stale_appliance_devices(hass, entry, manager)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HvacSimConfigEntry) -> bool:
    """Unload a space."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: HvacSimConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _remove_stale_appliance_devices(
    hass: HomeAssistant, entry: ConfigEntry, manager: HvacSimulatorManager
) -> None:
    """Delete devices for appliances that were removed in the options flow."""
    registry = dr.async_get(hass)
    wanted = {entry.entry_id} | {appliance_identifier(entry.entry_id, a.id) for a in manager.appliances}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        idents = {i for d, i in device.identifiers if d == DOMAIN}
        if idents and not idents & wanted:
            registry.async_remove_device(device.id)
