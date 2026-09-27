"""Tests for v0.3.0 detection logic: solar rule, openings, shower, rates, plugs, efficiency, manual timeouts."""

from hvac_simulators.appliances import Appliance
from hvac_simulators.detectors import multi_rates, shower, virtual_opening
from hvac_simulators.efficiency import EfficiencyTracker
from hvac_simulators.engine import (
    CAUSE_HEAT_LEAK_IN,
    CAUSE_HEAT_LOSS,
    CAUSE_HEATING,
    CAUSE_SOLAR_GAIN,
    CAUSE_VENTILATION,
    CAUSE_WINDOW_AIRING,
    Sample,
    SampleBuffer,
    Tuning,
    compute_features,
    score_causes,
)
from hvac_simulators.plugs import STATUS_ACTIVE, STATUS_IDLE, STATUS_OFF, PlugTracker
from hvac_simulators.simulator import MANUAL_AUTO, ApplianceSimulator
from hvac_simulators.simulator import STATUS_OFF as SIM_OFF

WINDOW = 15 * 60


def _buf(
    r_t=0.0,
    t_in=21.0,
    r_h=0.0,
    rh=50.0,
    t_out=None,
    rh_out=None,
    co2=None,
    r_co2=0.0,
    is_open=False,
    minutes=16,
):
    buf = SampleBuffer(WINDOW * 4)
    for i in range(minutes):
        ts = i * 60.0
        frac = (ts - (minutes - 1) * 60) / 3600.0
        buf.add(
            Sample(
                ts=ts,
                t_in=t_in + r_t * frac,
                rh_in=rh + r_h * frac,
                t_out=t_out,
                rh_out=rh_out,
                co2=None if co2 is None else co2 + r_co2 * frac,
                is_open=is_open,
            )
        )
    return buf, (minutes - 1) * 60.0


def _top(buf, now, sun_up=None, openings_known=True, tuning=None):
    tuning = tuning or Tuning()
    f = compute_features(buf, now, WINDOW, tuning, sun_up, 12, openings_known=openings_known)
    probs = score_causes(f, tuning).probs
    return max(probs, key=probs.get), probs, f


# --- solar gain -------------------------------------------------------------


def test_solar_gain_from_indoor_above_outdoor_with_sun():
    # Mild 19 °C outside (below the 22 °C threshold) but the room is warmer and still warming gently.
    buf, now = _buf(r_t=0.5, t_in=23.0, t_out=19.0)
    top, _, _ = _top(buf, now, sun_up=True)
    assert top == CAUSE_SOLAR_GAIN


def test_same_warming_at_night_is_not_solar():
    buf, now = _buf(r_t=0.5, t_in=23.0, t_out=19.0)
    top, probs, _ = _top(buf, now, sun_up=False)
    assert probs[CAUSE_SOLAR_GAIN] == 0.0
    assert top == CAUSE_HEATING


def test_fast_warming_on_cold_sunny_morning_stays_heating():
    buf, now = _buf(r_t=2.0, t_in=19.0, t_out=5.0)
    top, _, _ = _top(buf, now, sun_up=True)
    assert top == CAUSE_HEATING


def test_warmer_outside_cool_day_is_leak_not_solar():
    buf, now = _buf(r_t=0.3, t_in=17.0, t_out=19.0)
    top, _, _ = _top(buf, now, sun_up=True)
    assert top == CAUSE_HEAT_LEAK_IN


# --- openings ---------------------------------------------------------------


def test_without_opening_sensors_fan_and_window_are_split():
    buf, now = _buf(t_out=15.0, co2=1000, r_co2=-900)
    _, probs, _ = _top(buf, now, openings_known=False)
    assert abs(probs[CAUSE_VENTILATION] - probs[CAUSE_WINDOW_AIRING]) < 1e-9
    _, probs_known, _ = _top(buf, now, openings_known=True)
    assert probs_known[CAUSE_VENTILATION] > probs_known[CAUSE_WINDOW_AIRING]


def test_virtual_opening_needs_two_signs_of_outside_air():
    tuning = Tuning()
    # Room at 21 °C heading fast towards 10 °C outside, dry outside air, CO2 flushing.
    buf, now = _buf(r_t=-2.5, t_in=21.0, r_h=-6.0, rh=55.0, t_out=10.0, rh_out=60.0, co2=900, r_co2=-900)
    f = compute_features(buf, now, WINDOW, tuning, None, 12, is_open=False)
    result = virtual_opening(f, tuning)
    assert result["score"] > 0.6, result
    # Temperature alone (e.g. an aircon cooling on a cold night) is not enough.
    buf2, now2 = _buf(r_t=-2.5, t_in=21.0, t_out=10.0)
    f2 = compute_features(buf2, now2, WINDOW, tuning, None, 12, is_open=False)
    assert virtual_opening(f2, tuning)["score"] < 0.6


def test_heat_loss_still_detected():
    buf, now = _buf(r_t=-0.4, t_in=20.0, t_out=5.0)
    top, _, _ = _top(buf, now, sun_up=False)
    assert top == CAUSE_HEAT_LOSS


# --- shower -----------------------------------------------------------------


def test_shower_detected_from_bathroom_humidity_surge():
    buf = SampleBuffer(3600)
    for i in range(11):
        buf.add(Sample(ts=i * 60.0, t_in=22.0 + 0.05 * i, rh_in=58.0 + 2.5 * i))
    assert shower(buf, 600.0)["showering"]
    calm = SampleBuffer(3600)
    for i in range(11):
        calm.add(Sample(ts=i * 60.0, t_in=22.0, rh_in=60.0 + 0.2 * i))
    assert not shower(calm, 600.0)["showering"]


# --- multi-window rates ------------------------------------------------------


def test_multi_rates_last_and_windows():
    buf = SampleBuffer(3600)
    for i in range(16):
        buf.add(Sample(ts=i * 60.0, t_in=20.0 + i * 0.02))  # 1.2 °C/h
    rates = multi_rates(buf, "t_in", 15 * 60.0)
    assert abs(rates["last"] - 1.2) < 1e-6
    for key in ("5m", "10m", "15m"):
        assert abs(rates[key] - 1.2) < 1e-6


# --- plugs ------------------------------------------------------------------


def test_fridge_cycles_and_duty():
    fridge = PlugTracker("fridge")
    t = 0.0
    for _ in range(4):
        for watts, minutes in ((90.0, 10), (3.0, 20)):
            for _m in range(minutes):
                fridge.update(t, watts)
                t += 60
    assert fridge.cycles == 4
    assert abs(fridge.duty_cycle - 1 / 3) < 0.05
    assert fridge.status == STATUS_IDLE
    fridge.update(t, 0.0)
    assert fridge.status == STATUS_OFF


def test_washer_run_spans_short_pauses():
    washer = PlugTracker("washer")
    t = 0.0
    for watts, minutes in ((500, 20), (3, 5), (400, 15), (2, 30)):
        for _ in range(minutes):
            washer.update(t, watts)
            t += 60
    assert washer.status == STATUS_IDLE
    assert washer.last_run_s == (20 + 5 + 15) * 60
    washer.update(t, 600)
    assert washer.status == STATUS_ACTIVE and washer.running


# --- efficiency ---------------------------------------------------------------


def test_efficiency_heat_loss_coefficient_and_totals():
    tracker = EfficiencyTracker()
    buf, now = _buf(r_t=-0.6, t_in=20.0, t_out=8.0)
    f = compute_features(buf, now, WINDOW, Tuning(), False, 2)
    for _ in range(60):
        tracker.record("2026-09-27", 1 / 60, f, CAUSE_HEAT_LOSS, 0.9)
    today = tracker.today("2026-09-27")
    assert abs(today["heat_lost_ch"] - 0.6) < 1e-6
    summary = tracker.summary()
    assert abs(summary["heat_loss_coefficient"] - 0.05) < 0.005  # 0.6 °C/h over a 12 °C gap


# --- manual timeout / switched off ----------------------------------------------


def _features(t_in, r_t=0.0, t_out=30.0):
    buf, now = _buf(r_t=r_t, t_in=t_in, t_out=t_out)
    return compute_features(buf, now, WINDOW, Tuning(), None, 12)


def test_manual_mode_times_out_back_to_auto():
    ac = Appliance.from_config(
        {"id": "a", "type": "aircon", "functions": ["aircon"], "manual_timeout_min": 60}
    )
    sim = ApplianceSimulator(ac)
    sim.set_manual(0.0, "cool", 22.0)
    sim.update(1800.0, _features(22.0), None, 0.0, 0.45, 1.0, False)
    assert sim.manual_mode == "cool"
    sim.update(3700.0, _features(22.0), None, 0.0, 0.45, 1.0, False)
    assert sim.manual_mode == MANUAL_AUTO and sim.manual_setpoint is None
    assert sim.last_event == "manual_timeout"


def test_manual_mode_without_timeout_holds():
    ac = Appliance.from_config({"id": "a", "type": "aircon", "functions": ["aircon"]})
    sim = ApplianceSimulator(ac)
    sim.set_manual(0.0, "cool", 22.0)
    sim.update(10 * 3600.0, _features(22.0), None, 0.0, 0.45, 1.0, False)
    assert sim.manual_mode == "cool"


def test_switched_off_detected_in_manual_cool():
    ac = Appliance.from_config({"id": "a", "type": "aircon", "functions": ["aircon"], "off_detect_min": 30})
    sim = ApplianceSimulator(ac)
    sim.set_manual(0.0, "cool", 22.0)
    t = 0.0
    for _ in range(40):  # room climbing well above the setpoint for 40 min
        t += 60
        sim.update(t, _features(24.5, r_t=0.6), None, 0.0, 0.45, 1.0, False)
    assert sim.status == SIM_OFF and sim.mode == "off"
    assert sim.manual_mode == MANUAL_AUTO and sim.last_event == "switched_off_detected"
