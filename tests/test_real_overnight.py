"""Replay a real night from an apartment: aircon in cool mode at 18 °C all night.

What happened: the room sensor sat at ~17 °C and temperature barely moved,
because the building's thermal mass (neighbouring apartments, concrete)
donated back the heat the aircon removed. The aircon was still working: its
compressor cycled every ~20 minutes, which shows up as a regular ~2 %RH swing
in humidity (coil condenses moisture while on, re-evaporates while off).

The model should see a cool/dry unit cycling between active and idle, charge
energy for the compressor's on-time, and learn that the room sensor reads about
1 °C below the unit's own thermostat.
"""

import csv
from pathlib import Path

from hvac_simulators.appliances import Appliance, make_label
from hvac_simulators.cycling import detect_cycling
from hvac_simulators.engine import (
    CAUSE_COOLING,
    CAUSE_DEHUMIDIFY,
    Sample,
    SampleBuffer,
    Tuning,
    compute_features,
    score_causes,
)
from hvac_simulators.learning import Learner
from hvac_simulators.simulator import (
    IDLE_CYCLING,
    STATUS_ACTIVE,
    STATUS_IDLE,
    ApplianceSimulator,
)

FIXTURE = Path(__file__).parent / "fixtures" / "overnight_cool_cycling.csv"
WINDOW = 15 * 60
HISTORY = 3 * 3600
CYCLING_FROM_MIN = 360  # the pattern is well established from here on


def _rows():
    with FIXTURE.open() as fh:
        for row in csv.DictReader(fh):
            yield {k: float(v) for k, v in row.items()}


def _replay(manual: bool):
    tuning = Tuning()
    learner = Learner()
    ac = Appliance.from_config(
        {"id": "ac", "name": "Aircon", "type": "aircon", "functions": ["heat", "aircon"], "power_cool": 1000}
    )
    sim = ApplianceSimulator(ac)
    if manual:
        sim.manual_mode, sim.manual_setpoint = "cool", 18.0
    buf = SampleBuffer(HISTORY)
    log = []
    energy_at_start = None
    for row in _rows():
        minute = row["minute"]
        ts = minute * 60
        buf.add(
            Sample(
                ts=ts,
                t_in=row["t_in"],
                rh_in=row["rh_in"],
                co2=row["co2"],
                t_out=row["t_out"],
                rh_out=row["rh_out"],
            )
        )
        cycle = detect_cycling(buf.samples, ts, HISTORY)
        f = compute_features(buf, ts, WINDOW, tuning, None, 3, cycle=cycle)
        label, conf = None, 0.0
        if f is not None:
            pred = learner.predict(f, score_causes(f, tuning), [ac], tuning)
            label, conf = pred.label, pred.confidence
        sim.update(ts, f, label, conf, 0.45, 1.0, False)
        if minute == CYCLING_FROM_MIN:
            energy_at_start = sim.energy_kwh
        log.append(
            (minute, label if conf >= 0.45 else None, sim.mode, sim.status, sim.idle_reason, sim.power_w)
        )
    return sim, log, energy_at_start


def _late(log):
    return [entry for entry in log if entry[0] >= CYCLING_FROM_MIN]


def test_manual_cool_follows_compressor_cycles():
    sim, log, e0 = _replay(manual=True)
    late = _late(log)
    assert all(mode == "cool" for _, _, mode, _, _, _ in late)
    statuses = [status for _, _, _, status, _, _ in late]
    assert statuses.count(STATUS_ACTIVE) > 20 and statuses.count(STATUS_IDLE) > 20
    assert sim.cycles >= 5
    idle_reasons = [reason for _, _, _, status, reason, _ in late if status == STATUS_IDLE]
    # Mostly "between compressor cycles"; brief dips in the signal fall back to holding.
    assert idle_reasons.count(IDLE_CYCLING) > 0.6 * len(idle_reasons)
    # Room sensor ~17 °C while the unit held its 18 °C setpoint.
    assert -1.4 < sim.thermostat_offset < -0.6
    # Energy follows on-time: between fan-only (30 W) and running flat out (1 kW).
    hours = (log[-1][0] - CYCLING_FROM_MIN) / 60
    avg_w = (sim.energy_kwh - e0) * 1000 / hours
    assert 150 < avg_w < 850


def test_auto_mode_recognises_cooling_or_drying_once_cycling():
    _, log, _ = _replay(manual=False)
    late = _late(log)
    working = {make_label(CAUSE_COOLING, "ac"), make_label(CAUSE_DEHUMIDIFY, "ac")}
    share = sum(1 for _, label, *_ in late if label in working) / len(late)
    assert share > 0.6


def test_detector_finds_the_twenty_minute_cycle():
    samples = [Sample(ts=r["minute"] * 60, rh_in=r["rh_in"]) for r in _rows()]
    result = detect_cycling(samples, samples[-1].ts, HISTORY)
    assert result["strength"] >= 0.6
    assert 16 <= result["period_min"] <= 24
    assert 0.2 < result["duty"] < 0.8
