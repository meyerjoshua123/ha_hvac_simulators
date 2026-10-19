"""Asking "is it on?" end to end against Home Assistant (Python 3.13+ only)."""

import sys
import time
from unittest.mock import patch

import pytest

if sys.version_info < (3, 13):  # noqa: UP036
    pytest.skip("Home Assistant tests need Python 3.13+", allow_module_level=True)

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service
from test_integration import _create_entry, _entity, _run_minutes  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def test_confidence_sensor_and_attribute(hass: HomeAssistant, freezer):
    await _create_entry(hass)
    await _run_minutes(hass, freezer, 20, 25.0, -2.0)
    sensor = _entity(hass, "sensor", "lounge_ac_state_confidence")
    assert 0 <= float(sensor.state) <= 100
    assert _entity(hass, "sensor", "lounge_ac_state").attributes["confidence"] is not None


async def test_unsure_state_asks_once_and_answer_teaches(hass: HomeAssistant, freezer):
    entry = await _create_entry(hass)
    manager = entry.runtime_data
    ac_id = entry.options["appliances"][0]["id"]
    sim = manager.sims[ac_id]
    calls = async_mock_service(hass, "notify", "phone")
    manager.notify_service = "notify.phone"
    manager.activity_override = "resting"  # awake: no questions while asleep
    await _run_minutes(hass, freezer, 5, 25.0, -2.0)
    manager._started_at = 0.0  # past the first-hour quiet period

    now = time.time()
    sim.confidence, sim._low_conf_since = 0.3, now - 20 * 60
    sim.asked, sim.pending_ask_ts, sim.last_answer_ts = [], None, now - 60
    manager._maybe_ask(now)
    await hass.async_block_till_done()
    assert len(calls) == 1
    actions = {a["action"].rsplit("_", 1)[1] for a in calls[0].data["data"]["actions"]}
    assert actions == {"on", "off", "skip"}
    assert sim.pending_ask_ts is not None

    # No second question while this one is open, nor right after it is answered.
    manager._maybe_ask(now + 60)
    await hass.async_block_till_done()
    assert len(calls) == 1

    # Answer "off" from the phone: the unit is recorded off and passive drift is taught.
    before = len(manager.main.learner.samples)
    hass.bus.async_fire(
        "mobile_app_notification_action", {"action": f"HVACSIM_{entry.entry_id}_{ac_id}_off"}
    )
    await hass.async_block_till_done()
    assert sim.pending_ask_ts is None and sim.last_answer_ts is not None
    # The answer is recorded as ground truth and teaches the room's passive drift.
    assert sim.calibrations[-1]["mode"] == "off" and sim.calibrations[-1]["status"] == "off"
    assert len(manager.main.learner.samples) == before + 1
    manager._maybe_ask(now + 2 * 3600)
    await hass.async_block_till_done()
    assert len(calls) == 1  # six-hour spacing holds


async def test_not_asked_when_disabled_or_asleep(hass: HomeAssistant, freezer):
    entry = await _create_entry(hass)
    manager = entry.runtime_data
    sim = manager.sims[entry.options["appliances"][0]["id"]]
    manager._started_at = 0.0
    sim.confidence, sim._low_conf_since = 0.3, time.time() - 3600
    manager.ask_state = False
    with patch.object(manager, "_send_question") as send:
        manager._maybe_ask(time.time())
        assert not send.called
        manager.ask_state = True
        manager.activity_override = "sleeping"
        manager._maybe_ask(time.time())
        assert not send.called


async def test_answer_buttons(hass: HomeAssistant, freezer):
    entry = await _create_entry(hass)
    manager = entry.runtime_data
    ac_id = entry.options["appliances"][0]["id"]
    sim = manager.sims[ac_id]
    await _run_minutes(hass, freezer, 20, 25.0, -2.0)
    on = _entity(hass, "button", "lounge_ac_it_is_on")
    await hass.services.async_call("button", "press", {"entity_id": on.entity_id}, blocking=True)
    assert sim.status in ("active", "idle") and sim.mode == "cool"
    off = _entity(hass, "button", "lounge_ac_it_is_off")
    before = len(manager.main.learner.samples)
    await hass.services.async_call("button", "press", {"entity_id": off.entity_id}, blocking=True)
    assert sim.calibrations[-1]["status"] == "off"
    assert len(manager.main.learner.samples) == before + 1


async def test_first_hour_is_quiet(hass: HomeAssistant, freezer):
    entry = await _create_entry(hass)
    manager = entry.runtime_data
    calls = async_mock_service(hass, "notify", "phone")
    manager.notify_service = "notify.phone"
    manager.activity_override = "resting"
    sim = manager.sims[entry.options["appliances"][0]["id"]]
    sim.confidence, sim._low_conf_since = 0.3, time.time() - 3600
    manager._maybe_ask(time.time())
    await hass.async_block_till_done()
    assert not calls and sim.pending_ask_ts is None
