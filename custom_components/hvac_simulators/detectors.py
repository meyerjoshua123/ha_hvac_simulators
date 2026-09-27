"""Small event detectors (pure Python, no Home Assistant imports).

* ``virtual_opening``: a door or window is probably open because the room is
  exchanging air with outside far faster than the shell leaks. It needs at
  least two independent signs (temperature heading to outdoor at an
  open-window rate, absolute humidity heading to outdoor, CO2 flushing out) so
  one noisy signal, or an aircon cooling on a cold night, can't trigger it.
* ``shower``: a bathroom humidity surge.
* ``multi_rates``: rates of change since the last reading and over several
  windows, for the diagnostic sensors.
"""

from __future__ import annotations

from typing import Any

from .engine import Features, SampleBuffer, Tuning, ramp

RATE_WINDOWS_MIN = (5, 10, 15)


def virtual_opening(f: Features, tuning: Tuning) -> dict[str, Any]:
    """Estimate whether a door/window is open. ``f`` must be computed assuming closed."""
    signals: dict[str, float] = {}
    d_out = f.delta_out
    if d_out is not None and abs(d_out) >= 1.5:
        # Observed exchange rate: how fast indoor heads towards outdoor, per °C of gap.
        k_obs = f.r_t / d_out
        signals["temperature"] = ramp(k_obs, tuning.k_closed * 3, tuning.k_open * 0.7)
    if f.ah_in is not None and f.ah_out is not None and f.r_ah is not None:
        gap = f.ah_out - f.ah_in
        if abs(gap) >= 1.0:
            signals["humidity"] = ramp(f.r_ah / gap, tuning.k_closed * 3, tuning.k_open * 0.7)
    if f.ach_obs is not None:
        fast = tuning.ach_natural * tuning.vent_factor
        signals["co2"] = ramp(f.ach_obs, fast * 0.7, fast * 1.5)
    ordered = sorted(signals.values(), reverse=True)
    # Two agreeing signs of outside air, not one.
    score = (ordered[0] + ordered[1]) / 2 if len(ordered) >= 2 else 0.0
    return {"score": round(score, 3), "signals": {k: round(v, 2) for k, v in signals.items()}}


def shower(buffer: SampleBuffer, now: float, min_rise_rh: float = 8.0) -> dict[str, Any]:
    """Detect a shower from a steep bathroom humidity rise over the last ~10 minutes."""
    recent = [s for s in buffer.samples if s.ts >= now - 600 and s.rh_in is not None]
    if len(recent) < 2 or recent[-1].ts - recent[0].ts < 120:
        return {"showering": False, "score": 0.0, "rise_rh": None}
    low = min(s.rh_in for s in recent)  # type: ignore[type-var]
    rise = recent[-1].rh_in - low  # type: ignore[operator]
    latest = recent[-1].rh_in or 0.0
    score = ramp(rise, min_rise_rh * 0.5, min_rise_rh) * ramp(latest, 60.0, 75.0)
    return {"showering": score >= 0.6, "score": round(score, 2), "rise_rh": round(rise, 1)}


def last_rate(buffer: SampleBuffer, attr: str) -> float | None:
    """Rate per hour between the last two distinct readings of ``attr``."""
    points: list[tuple[float, float]] = []
    for sample in reversed(buffer.samples):
        value = getattr(sample, attr)
        if value is None:
            continue
        if not points or value != points[-1][1]:
            points.append((sample.ts, value))
        if len(points) == 2:
            break
    if len(points) < 2 or points[0][0] == points[1][0]:
        return None
    (t1, v1), (t0, v0) = points
    return (v1 - v0) / (t1 - t0) * 3600.0


def multi_rates(buffer: SampleBuffer, attr: str, now: float) -> dict[str, float | None]:
    """Rate of change since the last reading and over 5, 10 and 15 minutes (per hour)."""
    out: dict[str, float | None] = {"last": last_rate(buffer, attr)}
    for minutes in RATE_WINDOWS_MIN:
        out[f"{minutes}m"] = buffer.slope_per_hour(attr, minutes * 60, now)
    return out
