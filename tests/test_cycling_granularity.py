"""The cycling detector works at the pace sensors report (about every 30 s)."""

import math

from hvac_simulators.cycling import detect_cycling
from hvac_simulators.engine import Sample


def _stream(period_min, amplitude=2.0, step_s=31, hours=2.5, base=55.0):
    """Sawtooth-ish humidity (falls while the compressor runs, recovers while it rests)."""
    samples, t = [], 0.0
    while t <= hours * 3600:
        phase = (t / 60.0 / period_min) % 1.0
        wave = amplitude * (phase / 0.65 if phase < 0.65 else (1 - phase) / 0.35)  # slow rise, quick drop
        samples.append(Sample(ts=t, rh_in=base + wave - amplitude / 2, t_in=18.0))
        t += step_s
    return samples


def test_short_cycles_found_at_native_sensor_pace():
    samples = _stream(period_min=10)
    result = detect_cycling(samples, samples[-1].ts)
    assert result is not None and result["strength"] >= 0.6
    assert 9 <= result["period_min"] <= 11


def test_twenty_minute_cycle_still_found_with_jittery_timestamps():
    samples = _stream(period_min=21, step_s=33)
    for i, s in enumerate(samples):
        s.ts += 5 * math.sin(i)  # sensor timestamps are not evenly spaced
    result = detect_cycling(samples, samples[-1].ts)
    assert result is not None and result["strength"] >= 0.6
    assert 19 <= result["period_min"] <= 23
    assert result["cycles"] >= 5


def test_random_wander_is_not_cycling():
    import random

    rng = random.Random(4)
    samples, rh, t = [], 55.0, 0.0
    while t <= 2.5 * 3600:
        rh += rng.uniform(-0.15, 0.15)
        samples.append(Sample(ts=t, rh_in=rh, t_in=18.0))
        t += 31
    result = detect_cycling(samples, samples[-1].ts)
    assert result is None or result["strength"] < 0.6
