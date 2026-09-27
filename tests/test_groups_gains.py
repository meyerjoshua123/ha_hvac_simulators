"""Tests for HVAC functions, fan groups, heat sources and humidity helpers."""

from hvac_simulators.appliances import Appliance, available_labels, make_label
from hvac_simulators.engine import (
    CAUSE_AIR_FILTERING,
    CAUSE_HEATING,
    CAUSE_HUMIDIFY,
    CAUSE_INTERNAL_GAINS,
    CAUSE_VENTILATION,
    Sample,
    SampleBuffer,
    Tuning,
    absolute_humidity,
    compute_features,
    score_causes,
)
from hvac_simulators.fans import FanGroupEstimator
from hvac_simulators.gains import GainsModel
from hvac_simulators.simulator import ApplianceSimulator

WINDOW = 15 * 60


def test_hvac_functions_define_modes_and_capabilities():
    hv = Appliance.from_config({"id": "u", "type": "aircon", "functions": ["ventilate"]})
    assert hv.modes == ["ventilate", "filter"]
    assert set(hv.capabilities) == {CAUSE_VENTILATION, CAUSE_AIR_FILTERING}
    assert not hv.has_setpoint
    full = Appliance.from_config({"id": "u", "type": "aircon", "functions": ["heat", "aircon", "ventilate"]})
    assert full.modes == ["heat", "cool", "dry", "ventilate", "filter"]
    assert full.has_setpoint
    legacy = Appliance.from_config({"id": "u", "type": "aircon", "modes": ["cool", "dry"]})
    assert legacy.modes == ["cool", "dry"]


def test_heat_sources_are_not_labels():
    dryer = Appliance.from_config({"id": "d", "type": "heat_source", "power_entity": "sensor.dryer_power"})
    assert dryer.is_heat_source and dryer.capabilities == []
    assert not any(label.endswith(":d") for label in available_labels([dryer]))


def test_fan_group_count_from_default_airflow():
    group = FanGroupEstimator(["Bath", "Kitchen", "Laundry"], airflow_m3h=100, volume_m3=100)  # 1 ach each
    assert group.estimate(0.1)["count"] == 0
    assert group.estimate(1.1)["count"] == 1
    assert group.estimate(2.2)["count"] == 2
    # Not calibrated individually: count only, no names.
    assert group.estimate(2.2)["members"] == []


def test_fan_group_identifies_members_after_individual_calibration():
    group = FanGroupEstimator(["Bath", "Kitchen", "Laundry"], airflow_m3h=100, volume_m3=100)
    group.calibrate(0, 0.8, ["Bath"], None)
    group.calibrate(1, 3.0, ["Kitchen"], None)
    group.calibrate(2, 1.5, ["Laundry"], None)
    est = group.estimate(3.8)
    assert est["count"] == 2 and sorted(est["members"]) == ["Bath", "Kitchen"]
    est = group.estimate(2.3)
    assert est["count"] == 2 and sorted(est["members"]) == ["Bath", "Laundry"]


def test_fan_group_power_scales_with_fans_on():
    fans = Appliance.from_config({"id": "f", "type": "extractor_fan", "count": 3, "power_on": 25})
    assert fans.members == ["Fan 1", "Fan 2", "Fan 3"]
    sim = ApplianceSimulator(fans)
    sim.update(
        0,
        None,
        make_label(CAUSE_VENTILATION, "f"),
        0.9,
        0.45,
        1.0,
        False,
        fan_estimate={"count": 2, "members": []},
    )
    assert sim.units_on == 2 and sim.power_w == 50


def _buffer(r_t, r_h, dryer_w, t_in=20.0, rh=50.0, t_out=20.0):
    buf = SampleBuffer(WINDOW * 2)
    for i in range(16):
        ts = i * 60.0
        frac = (ts - WINDOW) / 3600.0
        buf.add(
            Sample(
                ts=ts, t_in=t_in + r_t * frac, rh_in=rh + r_h * frac, t_out=t_out, gains={"dryer": dryer_w}
            )
        )
    return buf


def test_dryer_effect_is_learned_and_removed():
    tuning = Tuning()
    gains = GainsModel()
    gains.load(None, ["dryer"])
    # Dryer at 2 kW warms 1.0 °C/h and adds 8 %/h humidity, nothing else running.
    for _ in range(60):
        gains.learn({"dryer": 2000}, 1.0, 8.0)
    effect_t, effect_h = gains.predict({"dryer": 2000})
    assert abs(effect_t - 1.0) < 0.1 and abs(effect_h - 8.0) < 0.8

    buf = _buffer(1.0, 8.0, 2000)
    raw = compute_features(buf, WINDOW, WINDOW, tuning, None, 12)
    raw_probs = score_causes(raw, tuning).probs
    assert max(raw_probs, key=raw_probs.get) in (CAUSE_HEATING, CAUSE_HUMIDIFY)

    comp = compute_features(
        buf, WINDOW, WINDOW, tuning, None, 12, gains=gains.predict(buf.mean_gains(WINDOW, WINDOW))
    )
    probs = score_causes(comp, tuning).probs
    assert max(probs, key=probs.get) == CAUSE_INTERNAL_GAINS
    assert abs(comp.r_t) < 0.15 and abs(comp.r_t_raw - 1.0) < 0.01


def test_two_sources_share_credit():
    gains = GainsModel()
    gains.load(None, ["fridge", "oven"])
    for _ in range(80):
        gains.learn({"fridge": 150, "oven": 0}, 0.06, 0.0)
        gains.learn({"fridge": 150, "oven": 2000}, 0.06 + 1.2, 0.0)
    fridge_t, _ = gains.sources["fridge"].effect(150)
    oven_t, _ = gains.sources["oven"].effect(2000)
    assert abs(oven_t - 1.2) < 0.15
    assert fridge_t < 0.2


def test_humidifier_rule():
    buf = _buffer(0.0, 6.0, 0)
    f = compute_features(buf, WINDOW, WINDOW, Tuning(), None, 12)
    probs = score_causes(f, Tuning()).probs
    assert max(probs, key=probs.get) == CAUSE_HUMIDIFY


def test_absolute_humidity():
    # 20 °C at 50 % RH is about 8.6 g/m³.
    assert abs(absolute_humidity(20.0, 50.0) - 8.6) < 0.1
    assert absolute_humidity(None, 50.0) is None


def test_cycle_detector_ignores_random_humidity_wander():
    import random

    from hvac_simulators.cycling import detect_cycling

    rng = random.Random(7)
    for _ in range(40):
        value, samples = 60.0, []
        for minute in range(240):
            value += rng.gauss(0, 0.15)
            samples.append(Sample(ts=minute * 60, t_in=17.0, rh_in=round(value, 1)))
        assert detect_cycling(samples, 239 * 60)["strength"] < 0.6
