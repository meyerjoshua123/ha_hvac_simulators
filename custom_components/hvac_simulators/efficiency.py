"""Long-term building efficiency record (pure Python, no Home Assistant imports).

Every evaluation, the part of the temperature change the engine attributes to
the building (not to HVAC or plugged-in appliances) is added to a daily
record: heat lost to outside, heat leaking in, solar gain, appliance heat and
air changes. The most useful single number is the **heat-loss coefficient**:
how many °C per hour the room drifts per °C of indoor/outdoor difference with
everything off and closed. Lower is better insulated; compare it between rooms,
seasons, or your old and new home.
"""

from __future__ import annotations

import statistics
from typing import Any

from .engine import (
    CAUSE_HEAT_LEAK_IN,
    CAUSE_HEAT_LOSS,
    CAUSE_IDLE,
    CAUSE_SOLAR_GAIN,
    CAUSE_VENTILATION,
    CAUSE_WINDOW_AIRING,
    Features,
)

DAYS_KEPT = 400
SUMMARY_DAYS = 30
_MIN_CONFIDENCE = 0.6
_MIN_GAP_C = 2.0

FIELDS = (
    "heat_lost_ch",  # °C·h the room cooled passively
    "heat_leaked_in_ch",  # °C·h it warmed from a warmer outside
    "solar_gain_ch",  # °C·h it warmed from the sun
    "appliance_heat_ch",  # °C·h added by plugged-in appliances
    "air_changes",  # air changes from ventilation / airing
    "degree_hours",  # Σ |indoor − outdoor| · h, to normalise the above
    "hours",
)


def _new_day() -> dict[str, Any]:
    day: dict[str, Any] = {f: 0.0 for f in FIELDS}
    day["k_samples"] = []
    return day


class EfficiencyTracker:
    """Daily passive heat-flow record for one room."""

    def __init__(self) -> None:
        self.days: dict[str, dict[str, Any]] = {}

    def as_dict(self) -> dict[str, Any]:
        return {"days": self.days}

    def load(self, data: dict[str, Any] | None) -> None:
        if data:
            self.days = dict(data.get("days", {}))

    def reset(self) -> None:
        self.days = {}

    def record(
        self, date: str, dt_h: float, f: Features | None, cause: str | None, confidence: float
    ) -> None:
        """Add ``dt_h`` hours of observation on local ``date`` (YYYY-MM-DD)."""
        if f is None or dt_h <= 0 or dt_h > 0.5:
            return
        day = self.days.setdefault(date, _new_day())
        day["hours"] += dt_h
        if f.delta_out is not None:
            day["degree_hours"] += abs(f.delta_out) * dt_h
        if f.gain_t > 0:
            day["appliance_heat_ch"] += f.gain_t * dt_h
        if cause is None or confidence < _MIN_CONFIDENCE:
            return
        if cause == CAUSE_HEAT_LOSS and f.r_t < 0:
            day["heat_lost_ch"] += -f.r_t * dt_h
        elif cause == CAUSE_HEAT_LEAK_IN and f.r_t > 0:
            day["heat_leaked_in_ch"] += f.r_t * dt_h
        elif cause == CAUSE_SOLAR_GAIN and f.r_t > 0:
            day["solar_gain_ch"] += f.r_t * dt_h
        elif cause in (CAUSE_VENTILATION, CAUSE_WINDOW_AIRING) and f.ach_obs:
            day["air_changes"] += f.ach_obs * dt_h
        # Heat-loss coefficient: passive drift per °C of gap, everything closed.
        d_out = f.delta_out
        if (
            cause in (CAUSE_HEAT_LOSS, CAUSE_HEAT_LEAK_IN, CAUSE_IDLE)
            and not f.is_open
            and d_out is not None
            and abs(d_out) >= _MIN_GAP_C
        ):
            k = f.r_t / d_out
            if 0.0 <= k < 1.0:
                day["k_samples"].append(round(k, 4))
                del day["k_samples"][:-500]
        self._trim()

    def _trim(self) -> None:
        if len(self.days) > DAYS_KEPT:
            for key in sorted(self.days)[: len(self.days) - DAYS_KEPT]:
                del self.days[key]

    def today(self, date: str) -> dict[str, float]:
        day = self.days.get(date) or _new_day()
        return {f: round(day[f], 3) for f in FIELDS}

    def summary(self, days: int = SUMMARY_DAYS) -> dict[str, Any]:
        """Averages per day and the median heat-loss coefficient over the last ``days``."""
        recent = [self.days[k] for k in sorted(self.days)[-days:]]
        if not recent:
            return {"days": 0}
        k_all = [k for d in recent for k in d["k_samples"]]
        per_day = {f: round(statistics.fmean(d[f] for d in recent), 3) for f in FIELDS if f != "hours"}
        return {
            "days": len(recent),
            "heat_loss_coefficient": round(statistics.median(k_all), 4) if k_all else None,
            "k_samples": len(k_all),
            "per_day": per_day,
        }
