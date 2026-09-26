"""End-to-end tests against a real Home Assistant core (Python 3.13+ only)."""

import sys
from datetime import timedelta

import pytest

if sys.version_info < (3, 13):  # noqa: UP036
    pytest.skip("Home Assistant tests need Python 3.13+", allow_module_level=True)

from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hvac_simulators.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _set_climate(hass, t_in, rh=50.0, co2=800.0, pm=10.0, t_out=30.0, door="off", dryer_w=0.0):
    hass.states.async_set(
        "sensor.lounge_temp", str(t_in), {"unit_of_measurement": "°C", "device_class": "temperature"}
    )
    hass.states.async_set("sensor.lounge_rh", str(rh), {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.lounge_co2", str(co2), {"unit_of_measurement": "ppm"})
    hass.states.async_set("sensor.lounge_pm25", str(pm), {"unit_of_measurement": "µg/m³"})
    hass.states.async_set("sensor.outside_temp", str(t_out), {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outside_rh", "70", {"unit_of_measurement": "%"})
    hass.states.async_set("binary_sensor.patio_door", door)
    hass.states.async_set(
        "sensor.dryer_power", str(dryer_w), {"unit_of_measurement": "W", "device_class": "power"}
    )


async def _create_entry(hass: HomeAssistant):
    _set_climate(hass, 25.0)
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await flow.async_configure(
        result["flow_id"],
        {
            "name": "Lounge",
            "indoor_temperature": "sensor.lounge_temp",
            "indoor_humidity": "sensor.lounge_rh",
            "outdoor_temperature": "sensor.outside_temp",
            "outdoor_humidity": "sensor.outside_rh",
            "co2": "sensor.lounge_co2",
            "air_quality": "sensor.lounge_pm25",
            "aq_lower_is_better": True,
            "openings": ["binary_sensor.patio_door"],
            "solar_threshold": 22,
        },
    )
    assert result["type"] is FlowResultType.MENU

    # HVAC unit: pick functions, then only the relevant power fields are asked.
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add_appliance"})
    result = await flow.async_configure(result["flow_id"], {"name": "Lounge AC", "type": "aircon"})
    assert result["step_id"] == "hvac_functions"
    empty = await flow.async_configure(result["flow_id"], {"functions": []})
    assert empty["errors"] == {"functions": "no_functions"}
    result = await flow.async_configure(result["flow_id"], {"functions": ["heat", "ventilate", "aircon"]})
    assert result["step_id"] == "appliance_details"
    details = {
        "power_heat": 1200,
        "power_cool": 1000,
        "power_dry": 600,
        "power_ventilate": 60,
        "power_filter": 40,
        "standby_w": 2,
        "inverter": False,
        "hold_factor": 0.4,
        "calculate_energy": True,
    }
    bad = await flow.async_configure(result["flow_id"], {**details, "min_setpoint": 30, "max_setpoint": 18})
    assert bad["errors"] == {"max_setpoint": "setpoint_range"}
    result = await flow.async_configure(
        result["flow_id"], {**details, "min_setpoint": 18, "max_setpoint": 30}
    )
    assert result["type"] is FlowResultType.MENU

    # A group of two extractor fans.
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add_appliance"})
    result = await flow.async_configure(result["flow_id"], {"name": "Extractors", "type": "extractor_fan"})
    result = await flow.async_configure(
        result["flow_id"],
        {
            "count": 2,
            "members": "Bathroom, Kitchen",
            "airflow_m3h": 90,
            "power_on": 25,
            "standby_w": 0,
            "calculate_energy": True,
        },
    )

    # A dryer measured through its smart plug.
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add_appliance"})
    result = await flow.async_configure(result["flow_id"], {"name": "Dryer", "type": "heat_source"})
    result = await flow.async_configure(
        result["flow_id"], {"power_entity": "sensor.dryer_power", "on_threshold_w": 20}
    )

    result = await flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


def _entity(hass, domain, suffix):
    matches = [s for s in hass.states.async_all(domain) if s.entity_id.endswith(suffix)]
    assert matches, (
        f"no {domain} entity ending in {suffix}: {[s.entity_id for s in hass.states.async_all(domain)]}"
    )
    return matches[0]


async def _run_minutes(hass, freezer, minutes, t_start, rate_per_h, **kw):
    t = t_start
    for _ in range(minutes):
        freezer.tick(timedelta(minutes=1))
        t += rate_per_h / 60
        _set_climate(hass, round(t, 3), **kw)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    return t


async def test_full_flow(hass: HomeAssistant, freezer: FrozenDateTimeFactory):
    entry = await _create_entry(hass)
    assert entry.title == "Lounge"
    assert len(entry.options["appliances"]) == 3
    assert entry.options["appliances"][0]["functions"] == ["heat", "ventilate", "aircon"]
    assert _entity(hass, "select", "lounge_ac_manual_mode").attributes["options"] == [
        "auto",
        "off",
        "heat",
        "cool",
        "dry",
        "ventilate",
        "filter",
    ]

    # Differentials: indoor minus outdoor, CO2 above outdoor air.
    assert float(_entity(hass, "sensor", "indoor_outdoor_temperature_difference").state) == -5.0
    assert float(_entity(hass, "sensor", "co2_above_outdoor").state) == 380.0
    assert float(_entity(hass, "sensor", "indoor_outdoor_absolute_humidity_difference").state) < 0

    # Fan group and heat source entities.
    assert _entity(hass, "sensor", "extractors_fans_on").attributes["of"] == 2
    assert _entity(hass, "binary_sensor", "dryer_running").state == "off"
    assert float(_entity(hass, "sensor", "dryer_heat_effect").state) == 0.0

    assert _entity(hass, "sensor", "lounge_suggested_mode").state == "Collecting data"
    assert _entity(hass, "sensor", "lounge_ac_state").state == "off"

    # Aircon cooling hard on a hot day with humidity holding: cooling.
    t = await _run_minutes(hass, freezer, 20, 25.0, -2.0)
    suggested = _entity(hass, "sensor", "lounge_suggested_mode")
    assert suggested.state == "Cooling - Lounge AC", suggested.attributes
    assert _entity(hass, "sensor", "lounge_ac_state").state == "active"
    assert _entity(hass, "sensor", "lounge_ac_mode").state == "cool"
    assert float(_entity(hass, "sensor", "lounge_ac_power").state) == 1000.0
    assert _entity(hass, "binary_sensor", "lounge_ac_on").state == "on"

    # Confirm via button and teach via select.
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity(hass, "button", "suggestion_is_correct").entity_id},
        blocking=True,
    )
    teach = _entity(hass, "select", "actual_mode_teach")
    assert "Cooling - Lounge AC" in teach.attributes["options"]
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": teach.entity_id, "option": "Cooling - Lounge AC"},
        blocking=True,
    )
    learned = _entity(hass, "sensor", "learned_examples")
    assert int(learned.state) == 2
    assert learned.attributes["confirmed"] == 2

    # Temperature flattens: unit rests at setpoint but stays in cool mode (cycling).
    t = await _run_minutes(hass, freezer, 25, t, 0.0)
    assert _entity(hass, "sensor", "lounge_ac_mode").state == "cool"
    assert _entity(hass, "sensor", "lounge_ac_state").state == "idle"
    assert float(_entity(hass, "sensor", "lounge_ac_power").state) == 40.0  # fan only
    assert _entity(hass, "sensor", "lounge_ac_detected_setpoint").state not in ("unknown", "unavailable")

    # Energy accumulates.
    assert float(_entity(hass, "sensor", "lounge_ac_energy").state) > 0

    # Calibrate the appliance and occupancy through services.
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}_{entry.options['appliances'][0]['id']}")}
    )
    await hass.services.async_call(
        DOMAIN,
        "calibrate",
        {"device_id": device.id, "mode": "cool", "status": "idle", "setpoint": 22, "filter": "clean"},
        blocking=True,
    )
    space = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    await hass.services.async_call(
        DOMAIN,
        "calibrate_occupancy",
        {"device_id": space.id, "people": 2, "activity": "resting"},
        blocking=True,
    )
    people = _entity(hass, "sensor", "estimated_people")
    assert people.state == "2", people.attributes

    # Manual mode select and setpoint number.
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": _entity(hass, "select", "lounge_ac_manual_mode").entity_id, "option": "heat"},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _entity(hass, "number", "lounge_ac_setpoint").entity_id, "value": 26},
        blocking=True,
    )
    assert _entity(hass, "sensor", "lounge_ac_mode").state == "heat"
    assert _entity(hass, "sensor", "lounge_ac_state").state == "active"

    # Fan group calibration (CO2 is above outdoor, so the excess air-change rate is known).
    fans = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}_{entry.options['appliances'][1]['id']}")}
    )
    await hass.services.async_call(
        DOMAIN, "calibrate", {"device_id": fans.id, "fans": ["Bathroom"]}, blocking=True
    )
    assert _entity(hass, "sensor", "extractors_fans_on").attributes["calibrations"] == 1

    # Dryer plug power shows up.
    _set_climate(hass, 22.0, dryer_w=2000)
    await hass.async_block_till_done()
    assert _entity(hass, "binary_sensor", "dryer_running").state == "on"
    assert _entity(hass, "binary_sensor", "appliance_heat_or_moisture").state in ("on", "off")

    # Options flow: remove the fan group; its device goes away after reload.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_appliance"}
    )
    fan_id = entry.options["appliances"][1]["id"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"id": fan_id})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_{fan_id}")}) is None

    # Learning survived the reload.
    assert int(_entity(hass, "sensor", "learned_examples").state) >= 2

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_open_door_cooling(hass: HomeAssistant, freezer: FrozenDateTimeFactory):
    await _create_entry(hass)
    await _run_minutes(hass, freezer, 20, 21.0, -1.5, t_out=8.0, door="on")
    assert _entity(hass, "sensor", "lounge_suggested_mode").state == "Cooling from open door/window"
    assert _entity(hass, "binary_sensor", "door_or_window_open").state == "on"
