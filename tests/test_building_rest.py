"""Building rest temperature: the closed room relaxes to stored building heat, not to outdoor air."""

import math

from hvac_simulators.engine import (
    CAUSE_HEATING,
    Sample,
    SampleBuffer,
    Tuning,
    compute_features,
    expected_passive_rate,
    score_causes,
)
from hvac_simulators.learning import Learner, fit_rest

K, T_REST = 0.3, 24.0


def _drift_points(n=40):
    """(indoor °C, °C/h) points from an exponential warm-up from 17 °C towards 24 °C."""
    pts, t = [], 17.0
    for _ in range(n):
        rate = K * (T_REST - t)
        pts.append([round(t, 2), round(rate, 3)])
        t += rate * 0.1
    return pts


def test_fit_rest_recovers_building_temperature():
    k, t_rest = fit_rest(_drift_points())
    assert abs(k - K) < 0.02 and abs(t_rest - T_REST) < 0.3


def test_fit_rest_needs_range_and_a_restoring_slope():
    assert fit_rest([[20.0, 0.1]] * 30) is None  # no spread
    assert fit_rest([[17.0 + 0.2 * i, 0.5] for i in range(30)]) is None  # flat rate: no slope
    assert fit_rest([[17.0 + 0.2 * i, 0.1 * i] for i in range(30)]) is None  # rate grows with temperature


def test_learner_learns_rest_from_passive_examples_and_exports_it():
    from hvac_simulators.engine import CAUSE_IDLE

    learner = Learner()
    for t_in, rate in _drift_points():
        buf = SampleBuffer(3600)
        for i in range(16):
            ts = i * 60.0
            buf.add(Sample(ts=ts, t_in=t_in + rate * (ts - 900) / 3600.0, t_out=14.0))
        f = compute_features(buf, 900.0, 900.0, Tuning(), False, 12)
        learner.teach(f, CAUSE_IDLE, source="manual", now=900.0)
    assert learner.t_rest is not None and abs(learner.t_rest - T_REST) < 0.5
    tuning = learner.apply_to_tuning(Tuning())
    assert tuning.t_rest == learner.t_rest
    restored = Learner()
    restored.load(learner.as_dict())
    assert restored.t_rest == learner.t_rest and restored.rest_points == learner.rest_points


def test_passive_rate_uses_rest_when_closed_and_outdoor_when_open():
    t = Tuning(k_closed=K, k_open=0.5, t_rest=T_REST)
    assert math.isclose(expected_passive_rate(18.0, 10.0, False, t), K * 6.0)
    assert math.isclose(expected_passive_rate(18.0, 10.0, True, t), 0.5 * (10.0 - 18.0))
    assert math.isclose(expected_passive_rate(18.0, None, False, t), K * 6.0)
    assert expected_passive_rate(18.0, 10.0, False, Tuning(k_closed=K)) == K * -8.0


def _features(tuning, t_in, r_t, t_out=14.0):
    buf = SampleBuffer(3600)
    for i in range(16):
        ts = i * 60.0
        buf.add(Sample(ts=ts, t_in=t_in + r_t * (ts - 900) / 3600.0, t_out=t_out))
    return compute_features(buf, 900.0, 900.0, tuning, False, 12)


def test_warming_towards_building_is_not_heating_once_rest_is_known():
    # 20 °C room warming 1.2 °C/h with 14 °C outside: with only the outdoor
    # baseline that is heating; knowing the building holds 24 °C explains it.
    f_before = _features(Tuning(), 20.0, 1.2)
    assert f_before.residual > 1.0
    assert max(score_causes(f_before, Tuning()).probs.items(), key=lambda kv: kv[1])[0] == CAUSE_HEATING
    tuned = Tuning(k_closed=K, t_rest=T_REST)
    f_after = _features(tuned, 20.0, 1.2)
    assert abs(f_after.residual) < 0.2
    probs = score_causes(f_after, tuned).probs
    assert probs[CAUSE_HEATING] < 0.2


# --- learned vote must fade when the taught examples look nothing like now --------


def test_single_label_teaching_does_not_claim_every_situation():
    from hvac_simulators.appliances import Appliance, make_label
    from hvac_simulators.engine import CAUSE_COOLING, CAUSE_IDLE

    ac = Appliance.from_config({"id": "ac", "type": "aircon", "functions": ["aircon"]})
    learner = Learner()
    tuning = Tuning()
    # Taught only "idle", in a slowly drifting room.
    for _ in range(40):
        learner.teach(_features(tuning, 21.0, 0.2), CAUSE_IDLE, source="manual", now=0.0)
    # Now the room is crashing 5 °C/h with 30 °C outside: nothing like what was taught.
    f = _features(tuning, 24.0, -5.0, t_out=30.0)
    prediction = learner.predict(f, score_causes(f, tuning), [ac], tuning)
    assert prediction.learned_weight < 0.25
    assert prediction.label == make_label(CAUSE_COOLING, "ac")


def test_similar_examples_keep_their_weight():
    from hvac_simulators.appliances import Appliance
    from hvac_simulators.engine import CAUSE_IDLE

    ac = Appliance.from_config({"id": "ac", "type": "aircon", "functions": ["aircon"]})
    learner = Learner()
    tuning = Tuning()
    f = _features(tuning, 21.0, 0.2)
    for _ in range(40):
        learner.teach(f, CAUSE_IDLE, source="manual", now=0.0)
    prediction = learner.predict(f, score_causes(f, tuning), [ac], tuning)
    assert prediction.learned_weight > 0.5 * learner.learned_weight()


# --- an unlabelled opposite-mode flip right after the unit stops is a drift, not a mode change ----


def _flip_sim():
    from hvac_simulators.appliances import Appliance
    from hvac_simulators.simulator import ApplianceSimulator

    ac = Appliance.from_config({"id": "ac", "type": "aircon", "functions": ["heat", "aircon"]})
    return ac, ApplianceSimulator(ac)


def test_cool_then_heat_claim_is_vetoed_while_room_is_below_rest_temperature():
    from hvac_simulators.appliances import make_label
    from hvac_simulators.engine import CAUSE_COOLING
    from hvac_simulators.simulator import STATUS_OFF

    ac, sim = _flip_sim()
    tuning = Tuning(k_closed=K, t_rest=T_REST)
    f_cool = _features(tuning, 19.0, -2.0, t_out=25.0)
    sim.update(0.0, f_cool, make_label(CAUSE_COOLING, "ac"), 0.9, 0.45, 1.0, False)
    assert sim.mode == "cool"
    # Unit stops; the room warms 3 °C/h towards the 24 °C the building holds.
    f_warm = _features(tuning, 19.0, 3.0, t_out=14.0)
    for minute in range(1, 30):
        sim.update(minute * 60.0, f_warm, make_label(CAUSE_HEATING, "ac"), 0.9, 0.45, 1.0, False)
    assert sim.mode != "heat"
    assert sim.status in (STATUS_OFF, "idle")


def test_heat_claim_accepted_once_room_is_above_rest_temperature():
    from hvac_simulators.appliances import make_label
    from hvac_simulators.engine import CAUSE_COOLING

    ac, sim = _flip_sim()
    tuning = Tuning(k_closed=K, t_rest=T_REST)
    sim.update(0.0, _features(tuning, 19.0, -2.0, t_out=25.0), make_label(CAUSE_COOLING, "ac"), 0.9, 0.45, 1.0, False)
    hot = _features(tuning, 26.0, 3.0, t_out=14.0)
    sim.update(60.0, hot, make_label(CAUSE_HEATING, "ac"), 0.9, 0.45, 1.0, False)
    assert sim.mode == "heat" and sim.status == "active"


def test_without_rest_temperature_only_very_confident_flips_pass():
    from hvac_simulators.appliances import make_label
    from hvac_simulators.engine import CAUSE_COOLING

    ac, sim = _flip_sim()
    tuning = Tuning()
    sim.update(0.0, _features(tuning, 19.0, -2.0, t_out=25.0), make_label(CAUSE_COOLING, "ac"), 0.9, 0.45, 1.0, False)
    f = _features(tuning, 19.0, 3.0, t_out=14.0)
    sim.update(60.0, f, make_label(CAUSE_HEATING, "ac"), 0.8, 0.45, 1.0, False)
    assert sim.mode != "heat"
    sim.update(120.0, f, make_label(CAUSE_HEATING, "ac"), 0.97, 0.45, 1.0, False)
    assert sim.mode == "heat"


def test_off_since_tracks_how_long_the_unit_has_been_off():
    ac, sim = _flip_sim()
    f = _features(Tuning(), 21.0, 0.0)
    sim.update(100.0, f, None, 0.0, 0.45, 1.0, False)
    assert sim.status == "off" and sim.off_since == 100.0
    sim.update(400.0, f, None, 0.0, 0.45, 1.0, False)
    assert sim.off_since == 100.0
