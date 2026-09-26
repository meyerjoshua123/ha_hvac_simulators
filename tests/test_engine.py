"""Scenario tests for the rule engine, learner and appliance simulator."""

from hvac_simulators.appliances import Appliance, available_labels, make_label
from hvac_simulators.engine import (
    CAUSE_AIR_FILTERING,
    CAUSE_COOLING,
    CAUSE_DEHUMIDIFY,
    CAUSE_HEAT_LEAK_IN,
    CAUSE_HEAT_LOSS,
    CAUSE_HEATING,
    CAUSE_IDLE,
    CAUSE_SOLAR_GAIN,
    CAUSE_VENTILATION,
    CAUSE_WINDOW_AIRING,
    CAUSE_WINDOW_COOLING,
    Sample,
    SampleBuffer,
    Tuning,
    compute_features,
    score_causes,
)
from hvac_simulators.learning import Learner
from hvac_simulators.simulator import (
    STATUS_ACTIVE,
    STATUS_IDLE,
    STATUS_OFF,
    ApplianceSimulator,
)

WINDOW = 15 * 60


def features_for(
    r_t,
    t_in=21.0,
    r_h=None,
    rh=50.0,
    r_aq=None,
    aq=800.0,
    t_out=None,
    is_open=False,
    sun_up=None,
    hour=12,
    tuning=None,
    co2=None,
    r_co2=0.0,
):
    """Build features from synthetic linear trends over one window."""
    tuning = tuning or Tuning()
    buf = SampleBuffer(WINDOW * 2)
    for i in range(16):
        ts = i * 60.0
        frac = (ts - WINDOW) / 3600.0  # hours relative to "now"
        buf.add(
            Sample(
                ts=ts,
                t_in=t_in + r_t * frac,
                rh_in=None if r_h is None else rh + r_h * frac,
                aq=None if r_aq is None else aq + r_aq * aq * frac,
                t_out=t_out,
                co2=None if co2 is None else co2 + r_co2 * frac,
                is_open=is_open,
            )
        )
    f = compute_features(buf, WINDOW, WINDOW, tuning, sun_up, hour)
    assert f is not None
    return f, tuning


def top(f, tuning):
    probs = score_causes(f, tuning).probs
    return max(probs, key=probs.get)


def test_open_window_cold_outside_is_window_cooling():
    f, t = features_for(r_t=-1.0, t_in=21, t_out=8, is_open=True)
    assert top(f, t) == CAUSE_WINDOW_COOLING


def test_closed_co2_clearing_fast_is_ventilation():
    # 1000 ppm falling 900 ppm/h -> ~1.5 air changes/h, 3x natural.
    f, t = features_for(r_t=0.0, t_out=15, r_h=0.0, co2=1000, r_co2=-900)
    assert top(f, t) == CAUSE_VENTILATION


def test_co2_slow_natural_decay_is_not_ventilation():
    f, t = features_for(r_t=0.0, t_out=15, r_h=0.0, co2=1000, r_co2=-150)
    assert top(f, t) == CAUSE_IDLE


def test_particulates_improving_with_co2_flat_is_filtering():
    f, t = features_for(r_t=0.0, t_out=15, r_h=0.0, r_aq=-0.3, co2=800, r_co2=0.0)
    assert top(f, t) == CAUSE_AIR_FILTERING


def test_open_window_co2_clearing_is_airing():
    f, t = features_for(r_t=0.0, t_in=20, t_out=20, r_h=0.0, co2=1200, r_co2=-2500, is_open=True)
    assert top(f, t) == CAUSE_WINDOW_AIRING


def test_warming_fast_in_winter_is_heating():
    f, t = features_for(r_t=1.5, t_in=19, t_out=5)
    assert top(f, t) == CAUSE_HEATING


def test_humidity_falling_fast_temp_falling_slowly_is_dehumidify():
    f, t = features_for(r_t=-0.2, r_h=-6.0, t_out=20)
    assert top(f, t) == CAUSE_DEHUMIDIFY


def test_temp_falling_humidity_lagging_is_cooling():
    f, t = features_for(r_t=-1.5, t_in=26, r_h=0.2, t_out=30)
    assert top(f, t) == CAUSE_COOLING


def test_outside_warmer_below_solar_threshold_is_leak_in():
    f, t = features_for(r_t=0.3, t_in=17, t_out=21, sun_up=False)
    assert top(f, t) == CAUSE_HEAT_LEAK_IN


def test_outside_hot_above_solar_threshold_is_solar_gain():
    f, t = features_for(r_t=0.5, t_in=22, t_out=28, sun_up=True)
    assert top(f, t) == CAUSE_SOLAR_GAIN


def test_closed_slow_cooling_cold_outside_is_heat_loss():
    f, t = features_for(r_t=-0.4, t_in=20, t_out=5)
    assert top(f, t) == CAUSE_HEAT_LOSS


def test_stable_is_idle():
    f, t = features_for(r_t=0.0, r_h=0.0, t_out=20)
    assert top(f, t) == CAUSE_IDLE


def test_teaching_overrides_rules():
    heater = Appliance.from_config({"id": "h1", "name": "Oil heater", "type": "heater"})
    hp = Appliance.from_config({"id": "a1", "name": "Heat pump", "type": "aircon"})
    appliances = [heater, hp]
    learner = Learner()
    f, t = features_for(r_t=1.5, t_in=19, t_out=5)
    rule = score_causes(f, t)
    first = learner.predict(f, rule, appliances, t)
    assert first.label in (make_label(CAUSE_HEATING, "h1"), make_label(CAUSE_HEATING, "a1"))
    for _ in range(10):
        learner.teach(f, make_label(CAUSE_HEATING, "a1"), was_suggested=first.label)
    after = learner.predict(f, rule, appliances, t)
    assert after.label == make_label(CAUSE_HEATING, "a1")
    assert after.confidence > first.confidence


def test_learner_roundtrip():
    learner = Learner()
    f, t = features_for(r_t=-0.4, t_in=20, t_out=5)
    learner.teach(f, CAUSE_HEAT_LOSS, was_suggested=CAUSE_HEAT_LOSS)
    clone = Learner()
    clone.load(learner.as_dict())
    assert len(clone.samples) == 1 and clone.confirmed == 1
    assert clone.k_closed is not None


def test_labels_are_per_appliance():
    fan = Appliance.from_config({"id": "f1", "name": "Bathroom fan", "type": "extractor_fan"})
    labels = available_labels([fan])
    assert make_label(CAUSE_VENTILATION, "f1") in labels
    assert CAUSE_HEATING in labels  # generic, no heater configured


def _upd(sim, ts, f, label, conf=0.9, other=False, max_idle=3600):
    sim.update(ts, f, label, conf, 0.45, 1.0, other, max_idle_s=max_idle, effectiveness_warmup_s=0)


def test_cooling_cycles_between_active_and_idle_keeping_mode():
    ac = Appliance.from_config(
        {
            "id": "a1",
            "name": "AC",
            "type": "aircon",
            "power_cool": 1000,
            "power_fan": 40,
            "inverter": False,
            "min_setpoint": 18,
            "max_setpoint": 30,
        }
    )
    sim = ApplianceSimulator(ac)
    cool = make_label(CAUSE_COOLING, "a1")
    ts = 0
    for _cycle in range(3):
        f, _ = features_for(r_t=-1.5, t_in=23.4, t_out=30)
        _upd(sim, ts, f, cool)
        ts += 60
        assert (sim.mode, sim.status) == ("cool", STATUS_ACTIVE)
        f, _ = features_for(r_t=-0.1, t_in=22.6, t_out=30)
        _upd(sim, ts, f, CAUSE_IDLE)
        ts += 60
        assert (sim.mode, sim.status) == ("cool", STATUS_IDLE)
        assert sim.power_w == 40  # fixed-speed: compressor off, fan on
        f, _ = features_for(r_t=0.4, t_in=23.4, t_out=30)
        _upd(sim, ts, f, CAUSE_HEAT_LEAK_IN)
        ts += 60
        assert (sim.mode, sim.status) == ("cool", STATUS_IDLE)
    assert sim.cycles == 2
    assert sim.detected_setpoint("cool") == 23.0  # midpoint of 22.6 lows and 23.4 highs
    assert sim.duty_cycle is not None and 0 < sim.duty_cycle < 1
    # Warms well past the setpoint band: the unit has been switched off.
    f, _ = features_for(r_t=0.5, t_in=24.5, t_out=30)
    _upd(sim, ts, f, CAUSE_HEAT_LEAK_IN)
    assert (sim.mode, sim.status) == ("off", STATUS_OFF)


def test_fixed_speed_idle_timeout_turns_off_but_inverter_holds():
    for inverter, expected in ((False, STATUS_OFF), (True, STATUS_IDLE)):
        ac = Appliance.from_config({"id": "a1", "type": "aircon", "inverter": inverter})
        sim = ApplianceSimulator(ac)
        f, _ = features_for(r_t=-1.5, t_in=23, t_out=30)
        _upd(sim, 0, f, make_label(CAUSE_COOLING, "a1"))
        f, _ = features_for(r_t=0.0, t_in=22.5, t_out=30)
        _upd(sim, 60, f, CAUSE_IDLE)
        _upd(sim, 60 + 7200, f, CAUSE_IDLE, max_idle=3600)
        assert sim.status == expected


def test_inverter_idle_power():
    ac = Appliance.from_config({"id": "a1", "type": "aircon", "power_cool": 1000})
    sim = ApplianceSimulator(ac)
    f, _ = features_for(r_t=-1.5, t_in=23, t_out=30)
    _upd(sim, 0, f, make_label(CAUSE_COOLING, "a1"))
    f, _ = features_for(r_t=0.0, t_in=22.5, t_out=30)
    _upd(sim, 60, f, CAUSE_IDLE)
    assert sim.status == STATUS_IDLE and sim.power_w == 400
    # 60 s at 1000 W so far
    _upd(sim, 120, f, CAUSE_IDLE)
    assert abs(sim.energy_kwh - (1000 * 60 + 400 * 60) / 3.6e6) < 1e-9


def test_setpoint_clamped_to_range():
    heater = Appliance.from_config({"id": "h1", "type": "heater", "min_setpoint": 16, "max_setpoint": 20})
    sim = ApplianceSimulator(heater)
    f_heat, _ = features_for(r_t=1.5, t_in=19, t_out=5)
    _upd(sim, 0, f_heat, make_label(CAUSE_HEATING, "h1"))
    f_flat, _ = features_for(r_t=0.0, t_in=23, t_out=5)
    _upd(sim, 60, f_flat, CAUSE_IDLE)
    assert sim.setpoint == 20


def test_calibrations_blend_with_detection():
    heater = Appliance.from_config({"id": "h1", "type": "heater"})
    sim = ApplianceSimulator(heater)
    sim.calibrate(0, 20.0, "heat", "active", 21.0, None)
    sim.calibrate(10, 20.5, "heat", "idle", 22.0, None)
    assert sim.mode == "heat" and sim.status == STATUS_IDLE
    # Newer calibration weighted double: (21*1 + 22*2) / 3
    assert sim.setpoint == round((21 + 44) / 3, 2)


def test_manual_mode_simulates_thermostat_cycling():
    heater = Appliance.from_config({"id": "h1", "type": "heater", "power_heat": 1500})
    sim = ApplianceSimulator(heater)
    sim.manual_mode = "heat"
    sim.manual_setpoint = 21.0
    for ts, t_in, expected in (
        (0, 18, STATUS_ACTIVE),
        (60, 20.9, STATUS_IDLE),
        (120, 20.6, STATUS_IDLE),
        (180, 20.3, STATUS_ACTIVE),
    ):
        f, _ = features_for(r_t=0.0, t_in=t_in, t_out=5)
        _upd(sim, ts, f, CAUSE_IDLE)
        assert sim.status == expected, (t_in, sim.status)
    assert sim.mode == "heat"


def test_learned_tuning_keeps_co2_settings():
    t = Tuning(co2_outdoor=450.0, vent_factor=3.0, solar_threshold=25.0)
    learner = Learner()
    learner.k_closed = 0.08
    out = learner.apply_to_tuning(t)
    assert (out.co2_outdoor, out.vent_factor, out.solar_threshold, out.k_closed) == (450.0, 3.0, 25.0, 0.08)
