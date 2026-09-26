"""Tests for effectiveness ratings and CO2 occupancy."""

from hvac_simulators.effectiveness import EffectivenessTracker, rating_for
from hvac_simulators.occupancy import OccupancyModel


def test_rating_bands():
    assert rating_for(1.0) == "excellent"
    assert rating_for(0.9) == "great"
    assert rating_for(0.8) == "good"
    assert rating_for(0.65) == "ok"
    assert rating_for(0.5) == "bad"
    assert rating_for(0.2) == "terrible"
    assert rating_for(None) is None


def _perf(lift, health):
    # Clean unit: 2 °C/h at zero lift, losing 0.1 °C/h per degree of lift.
    return (2.0 - 0.1 * lift) * health


def test_effectiveness_accounts_for_outdoor_lift():
    t = EffectivenessTracker()
    ts = 0.0
    for i in range(40):  # reference period across a range of weather
        lift = 2 + (i % 10)
        t.add(ts, lift, _perf(lift, 1.0))
        ts += 600
    assert t.rating() == "excellent"
    # Much hotter days, same health: still excellent because lift is accounted for.
    for _ in range(40):
        t.add(ts, 14, _perf(14, 1.0))
        ts += 600
    assert t.rating() == "excellent"


def test_effectiveness_degrades_and_resets_on_filter_clean():
    t = EffectivenessTracker()
    ts = 0.0
    for i in range(40):
        lift = 2 + (i % 10)
        t.add(ts, lift, _perf(lift, 1.0))
        ts += 600
    ts = 20 * 86400  # after the reference period
    for i in range(40):
        lift = 2 + (i % 10)
        t.add(ts, lift, _perf(lift, 0.55))
        ts += 600
    assert t.rating() == "bad"
    t.mark_filter(ts, "clean")
    assert t.rating() is None  # needs fresh samples; previous baseline kept
    for i in range(12):
        lift = 2 + (i % 10)
        t.add(ts, lift, _perf(lift, 1.0))
        ts += 600
    assert t.rating() == "excellent"


def test_occupancy_default_from_volume():
    m = OccupancyModel(volume_m3=50, co2_outdoor=420)
    # 2 resting people in 50 m3: 2 * 0.018 / 50 * 1e6 = 720 ppm/h generation.
    # Steady state with 0.5 ach: C - C_out = 720 / 0.5 = 1440.
    est = m.estimate(420 + 1440, 0.0, "closed", "resting")
    assert round(est["people"], 2) == 2.0
    assert est["people_min"] < est["people"] < est["people_max"]


def test_occupancy_calibration_learns_ach_and_per_person():
    m = OccupancyModel(volume_m3=50, co2_outdoor=420)
    # Empty home: CO2 at 1020 decaying 240 ppm/h -> ach 0.4.
    m.calibrate(0, 0, "resting", "closed", 1020, -240)
    assert abs(m.ach("closed") - 0.4) < 1e-9
    # 3 people resting, steady at 1320 ppm -> G = 0.4 * 900 = 360 -> 120 ppm/h each.
    m.calibrate(1, 3, "resting", "closed", 1320, 0.0)
    assert abs(m.ppm_per_person("resting") - 120) < 1e-9
    assert abs(m.ppm_per_person("sleeping") - 90) < 1e-9  # scaled by activity
    est = m.estimate(420 + 600, 0.0, "closed", "resting")
    assert round(est["people"], 2) == 2.0
    # Same CO2 while exercising means fewer people.
    assert m.estimate(420 + 600, 0.0, "closed", "exercising")["people"] < 1
