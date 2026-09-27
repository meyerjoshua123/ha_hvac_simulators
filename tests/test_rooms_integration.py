"""End-to-end: rooms from areas, zones, room-to-room differences, shower, plug status (Python 3.13+)."""

import sys
from datetime import timedelta

import pytest

if sys.version_info < (3, 13):  # noqa: UP036 - lets the pure tests run on older Python
    pytest.skip("Home Assistant tests need Python 3.13+", allow_module_level=True)

from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hvac_simulators.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _states(hass, living_t=21.0, bath_rh=55.0, laundry_t=23.0, laundry_rh=60.0, dryer_w=0.0):
    hass.states.async_set("sensor.living_temp", str(living_t), {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.living_rh", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.living_co2", "900", {"unit_of_measurement": "ppm"})
    hass.states.async_set("sensor.outside_temp", "15", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.bath_rh", str(bath_rh), {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.laundry_temp", str(laundry_t), {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.laundry_rh", str(laundry_rh), {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.dryer_power", str(dryer_w), {"unit_of_measurement": "W"})


def _entity(hass, domain, suffix):
    matches = [s for s in hass.states.async_all(domain) if s.entity_id.endswith(suffix)]
    assert matches, (
        f"no {domain} ending {suffix}: {sorted(s.entity_id for s in hass.states.async_all(domain))}"
    )
    return matches[0]


async def test_rooms_zones_shower_and_plugs(hass: HomeAssistant, freezer: FrozenDateTimeFactory):
    areas = ar.async_get(hass)
    living = areas.async_create("Living room")
    bathroom = areas.async_create("Bathroom")
    laundry = areas.async_create("Laundry")
    _states(hass)

    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "indoor_temperature": "sensor.living_temp",
            "indoor_humidity": "sensor.living_rh",
            "co2": "sensor.living_co2",
            "outdoor_temperature": "sensor.outside_temp",
            "aq_lower_is_better": True,
            "openings": [],
            "solar_threshold": 22,
            "main_area": living.id,
            "main_type": "living",
            "main_air_gaps": "small",
            "use_virtual_openings": True,
        },
    )
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    entry = result["result"]
    await hass.async_block_till_done()

    options = hass.config_entries.options

    async def menu(step):
        r = await options.async_init(entry.entry_id)
        return await options.async_configure(r["flow_id"], {"next_step_id": step})

    # Bathroom: humidity only. Laundry: temperature + humidity.
    r = await menu("add_room")
    r = await options.async_configure(
        r["flow_id"],
        {
            "area_id": bathroom.id,
            "type": "bathroom",
            "humidity": "sensor.bath_rh",
            "volume_m3": 12,
            "air_gaps": "none",
            "use_virtual_openings": True,
            "openings": [],
        },
    )
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    r = await menu("add_room")
    r = await options.async_configure(
        r["flow_id"],
        {
            "area_id": laundry.id,
            "type": "laundry",
            "temperature": "sensor.laundry_temp",
            "humidity": "sensor.laundry_rh",
            "volume_m3": 15,
            "air_gaps": "large",
            "use_virtual_openings": True,
            "openings": [],
        },
    )
    await hass.async_block_till_done()
    # Adding the same area twice is refused.
    r = await menu("add_room")
    dup = await options.async_configure(
        r["flow_id"],
        {
            "area_id": laundry.id,
            "type": "laundry",
            "volume_m3": 15,
            "air_gaps": "none",
            "use_virtual_openings": True,
            "openings": [],
        },
    )
    assert dup["errors"] == {"area_id": "room_exists"}

    # Bathroom flows into the living room (one zone).
    r = await menu("room_links")
    r = await options.async_configure(r["flow_id"], {"links_main": [bathroom.id]})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # Bathroom fan in the bathroom area; dryer (heat source) in the laundry.
    r = await menu("add_appliance")
    r = await options.async_configure(
        r["flow_id"], {"name": "Bath fan", "type": "extractor_fan", "room": bathroom.id}
    )
    r = await options.async_configure(
        r["flow_id"],
        {"count": 1, "airflow_m3h": 90, "power_on": 20, "standby_w": 0, "calculate_energy": True},
    )
    await hass.async_block_till_done()
    r = await menu("add_appliance")
    r = await options.async_configure(
        r["flow_id"], {"name": "Dryer", "type": "heat_source", "room": laundry.id}
    )
    r = await options.async_configure(
        r["flow_id"], {"kind": "dryer", "power_entity": "sensor.dryer_power", "on_threshold_w": 1}
    )
    await hass.async_block_till_done()

    manager = entry.runtime_data
    assert set(manager.rooms) == {"main", bathroom.id, laundry.id}
    fan_id = next(a.id for a in manager.appliances if a.name == "Bath fan")
    dryer_id = next(a.id for a in manager.appliances if a.name == "Dryer")
    # The unsensored bathroom's fan is observed through the living room it flows into.
    assert manager.room_of(fan_id).id == "main"
    assert manager.room_of(dryer_id).id == laundry.id

    # Devices land in their areas.
    devices = dr.async_get(hass)
    laundry_dev = devices.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_room_{laundry.id}")})
    assert laundry_dev is not None and laundry_dev.area_id == laundry.id
    assert devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)}).area_id == living.id

    # Laundry vs living room difference.
    for _ in range(3):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert float(_entity(hass, "sensor", "temperature_difference_to_main_room").state) == 2.0
    assert _entity(hass, "sensor", "fans_on").attributes["of"] == 1
    assert _entity(hass, "binary_sensor", "door_or_window_open_estimated").state == "off"

    # Shower: bathroom humidity surges.
    for step in range(10):
        freezer.tick(timedelta(minutes=1))
        _states(hass, bath_rh=55.0 + 3.0 * step)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert _entity(hass, "binary_sensor", "shower_running").state == "on"

    # Dryer plug: active status.
    _states(hass, bath_rh=82.0, dryer_w=2200)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    status = _entity(hass, "sensor", "dryer_status")
    assert status.state == "active" and status.attributes["kind"] == "Clothes dryer"

    # Efficiency sensors exist per sensored room.
    assert _entity(hass, "sensor", "home_heat_loss_coefficient")

    assert await hass.config_entries.async_unload(entry.entry_id)
