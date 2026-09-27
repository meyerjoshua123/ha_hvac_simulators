"""Compressor cycling detection (pure Python, no Home Assistant imports).

A cooling or dry-mode unit that cycles leaves a clock-regular fingerprint in
indoor humidity: while the compressor runs the coil condenses moisture and
humidity falls; while it rests the coil re-evaporates and humidity recovers.
Temperature may barely move when the building's thermal mass (neighbouring
apartments, concrete) donates back the heat the unit removes, so humidity is
the reliable signal.

Learned from a real night: an aircon in cool mode held a room at ~17 °C with
humidity swinging ~1.8 %RH on a steady ~20 minute cycle while temperature moved
only ~0.2 °C. Random room wander is not periodic, so the detector asks for a
clear autocorrelation peak AND regularly spaced falling phases before it says
anything.
"""

from __future__ import annotations

import statistics
from typing import Any

from .engine import Sample

GRID_S = 60
MIN_SPAN_S = 75 * 60  # need a few cycles before judging
MIN_LAG_MIN, MAX_LAG_MIN = 8, 60
_SMOOTH_MIN = 31  # detrending window
_PHASE_DIFF_MIN = 3
_MIN_CYCLES = 3
MIN_AMPLITUDE_RH = 0.8


def _ramp(x: float, lo: float, hi: float) -> float:
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _grid(samples: list[Sample], attr: str, start: float, end: float) -> list[float]:
    """Forward-filled 1-minute series of ``attr`` between ``start`` and ``end``."""
    out: list[float] = []
    value: float | None = None
    i = 0
    t = start
    while t <= end:
        while i < len(samples) and samples[i].ts <= t:
            v = getattr(samples[i], attr)
            if v is not None:
                value = v
            i += 1
        if value is not None:
            out.append(value)
        t += GRID_S
    return out


def _detrend(x: list[float], width: int) -> list[float]:
    half = width // 2
    return [x[i] - statistics.fmean(x[max(0, i - half) : i + half + 1]) for i in range(len(x))]


def _autocorr(x: list[float], lag: int) -> float:
    n = len(x) - lag
    mean = statistics.fmean(x)
    var = sum((a - mean) ** 2 for a in x)
    if var == 0 or n <= 0:
        return 0.0
    return sum((x[i] - mean) * (x[i + lag] - mean) for i in range(n)) / var


def detect_cycling(samples: list[Sample], now: float, window_s: float = 3 * 3600) -> dict[str, Any] | None:
    """Look for regular compressor cycling in indoor humidity.

    Returns ``None`` when there isn't enough history, otherwise a dict with
    ``strength`` (0-1), ``period_min``, ``duty`` (share of time humidity was
    falling, i.e. compressor on), ``cycles``, ``amplitude_rh`` and
    ``compressor_on`` (humidity falling right now).
    """
    recent = [s for s in samples if s.ts >= now - window_s and s.rh_in is not None]
    if len(recent) < 2 or recent[-1].ts - recent[0].ts < MIN_SPAN_S:
        return None
    rh = _grid(recent, "rh_in", recent[0].ts, now)
    if len(rh) < MIN_SPAN_S // GRID_S:
        return None
    dh = _detrend(rh, _SMOOTH_MIN)

    lags = range(MIN_LAG_MIN, min(MAX_LAG_MIN, len(dh) // 3) + 1)
    if not lags:
        return None
    best_lag = max(lags, key=lambda lag: _autocorr(dh, lag))
    peak = _autocorr(dh, best_lag)
    # A true oscillation is anti-correlated at half its period and repeats at
    # twice it; slow random wander does neither reliably.
    trough = _autocorr(dh, max(1, best_lag // 2))
    repeat = _autocorr(dh, min(2 * best_lag, len(dh) // 2))

    # Falling phases (compressor on): smoothed humidity heading down.
    slope = [rh[i] - rh[i - _PHASE_DIFF_MIN] for i in range(_PHASE_DIFF_MIN, len(rh))]
    amplitude = statistics.pstdev(dh) * 2.83  # peak-to-peak of a sine
    threshold = max(0.15, amplitude * 0.15)
    state: str | None = None
    down_starts: list[int] = []
    down_minutes = 0
    for i, s in enumerate(slope):
        new = "down" if s < -threshold else ("up" if s > threshold else state)
        if new == "down":
            down_minutes += 1
            if state != "down":
                down_starts.append(i)
        state = new
    spacing = [b - a for a, b in zip(down_starts, down_starts[1:])]

    regularity = 0.0
    if len(spacing) >= _MIN_CYCLES - 1:
        spacing.sort()
        median = statistics.median(spacing)
        iqr = spacing[3 * len(spacing) // 4] - spacing[len(spacing) // 4]
        regularity = max(0.0, 1.0 - iqr / max(median, 1.0))
    periodic = _ramp(peak, 0.15, 0.35) * _ramp(-trough, 0.0, 0.2)
    repeats = _ramp(repeat, 0.1, 0.3)
    # Real cycling swung 1.7-2.4 %RH; random wander in tests stayed under ~0.9.
    big_enough = _ramp(amplitude, MIN_AMPLITUDE_RH, MIN_AMPLITUDE_RH + 0.6)
    strength = periodic * regularity * big_enough * (0.5 + 0.5 * repeats)
    if len(down_starts) < _MIN_CYCLES:
        strength = 0.0

    return {
        "strength": round(strength, 3),
        "period_min": best_lag,
        "duty": round(down_minutes / max(len(slope), 1), 3),
        "cycles": len(down_starts),
        "amplitude_rh": round(amplitude, 2),
        "compressor_on": state == "down",
    }
