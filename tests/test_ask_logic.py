"""When to ask "is it on?": only when unsure or overdue, and never in a burst."""

from hvac_simulators.appliances import Appliance
from hvac_simulators.simulator import ApplianceSimulator

HOUR = 3600.0


def _sim():
    return ApplianceSimulator(Appliance.from_config({"id": "ac", "type": "aircon", "functions": ["aircon"]}))


def _unsure_for(sim, since, now):
    sim.confidence = 0.3
    sim._low_conf_since = since


def test_overdue_checkin_when_never_asked():
    sim = _sim()
    assert sim.ask_due(1000.0)


def test_unsure_must_persist_before_asking():
    sim = _sim()
    sim.last_answer_ts = 0.0  # recently in touch, so only uncertainty can trigger
    _unsure_for(sim, since=5000.0, now=5300.0)
    assert not sim.ask_due(5300.0)  # unsure for only 5 minutes
    assert sim.ask_due(5000.0 + 11 * 60)


def test_not_asked_while_asleep_or_manual_or_for_fans():
    sim = _sim()
    assert not sim.ask_due(1000.0, asleep=True)
    sim.manual_mode = "cool"
    assert not sim.ask_due(1000.0)
    fans = ApplianceSimulator(Appliance.from_config({"id": "f", "type": "extractor_fan", "count": 2}))
    assert not fans.ask_due(1000.0)


def test_questions_are_spaced_out_and_capped():
    sim = _sim()
    _unsure_for(sim, since=0.0, now=0.0)
    now = 20 * 60.0
    assert sim.ask_due(now)
    sim.mark_asked(now)
    assert sim.pending_ask_ts == now
    sim.mark_answered(now + 60)
    # Still unsure straight after the answer: no second question inside six hours.
    _unsure_for(sim, since=now, now=now)
    assert not sim.ask_due(now + 2 * HOUR)
    assert sim.ask_due(now + 6.5 * HOUR)
    # At most three a day, however unsure.
    sim.asked = [now + 6.5 * HOUR - 20 * HOUR, now + 6.5 * HOUR - 13 * HOUR, now + 6.5 * HOUR - 7 * HOUR]
    assert not sim.ask_due(now + 6.5 * HOUR)


def test_unanswered_question_blocks_until_it_expires():
    sim = _sim()
    sim.mark_asked(0.0)
    _unsure_for(sim, since=0.0, now=0.0)
    assert not sim.ask_due(8 * HOUR)  # still waiting for an answer
    assert sim.ask_due(13 * HOUR)  # question expired, and 12+ hours have passed


def test_daily_checkin_not_repeated_once_answered():
    sim = _sim()
    sim.mark_asked(0.0)
    sim.mark_answered(60.0)
    sim.confidence, sim._low_conf_since = 0.9, None
    assert not sim.ask_due(10 * HOUR)  # confident and answered recently
    assert sim.ask_due(25 * HOUR)  # a day later, check in again


def test_asked_history_persists():
    sim = _sim()
    sim.mark_asked(100.0)
    sim.mark_answered(200.0)
    restored = _sim()
    restored.load(sim.as_dict())
    assert restored.asked == [100.0] and restored.last_answer_ts == 200.0


def test_confidence_drops_for_contradicted_manual_and_vetoed_flip():
    from hvac_simulators.engine import Sample, SampleBuffer, Tuning, compute_features

    sim = _sim()
    buf = SampleBuffer(3600)
    for i in range(16):
        buf.add(Sample(ts=i * 60.0, t_in=21.0, t_out=20.0))
    f = compute_features(buf, 900.0, 900.0, Tuning(), False, 12)
    sim.update(900.0, f, None, 0.8, 0.45, 1.0, False)
    assert sim.confidence == 0.8
    sim.set_manual(900.0, "cool", 18.0)
    sim.update(960.0, f, None, 0.8, 0.45, 1.0, False)
    assert sim.confidence == 0.85
