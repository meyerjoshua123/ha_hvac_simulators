"""Extractor fan groups (pure Python, no Home Assistant imports).

Several extractor fans in one space (bathroom, kitchen, laundry...) each add
air changes. The extra air-change rate seen in CO2 decay, beyond natural
leakage, tells us roughly how many are running:

* Uncalibrated, each fan is assumed to move its rated airflow: ``airflow / volume``
  air changes per hour.
* Calibrate a single named fan to learn its own rate, or give a count of fans
  running to learn an average rate for the group.

With per-fan rates known, the combination whose total best matches the
observed excess is reported, so it can say not just "2 on" but which two.
"""

from __future__ import annotations

import itertools
import statistics
from typing import Any

_CALIBRATIONS_KEPT = 60
# Search every subset up to this many fans; larger groups fall back to a count.
_MAX_SUBSET_SEARCH = 10


class FanGroupEstimator:
    """Estimate which fans of a group are running."""

    def __init__(self, members: list[str], airflow_m3h: float, volume_m3: float) -> None:
        self.members = members
        self.default_ach = max(airflow_m3h, 1.0) / max(volume_m3, 1.0)
        self.calibrations: list[dict[str, Any]] = []

    def as_dict(self) -> dict[str, Any]:
        return {"calibrations": self.calibrations}

    def load(self, data: dict[str, Any] | None) -> None:
        if data:
            self.calibrations = list(data.get("calibrations", []))

    def reset(self) -> None:
        self.calibrations = []

    def calibrate(self, ts: float, excess_ach: float, fans_on: list[str] | None, count: int | None) -> bool:
        """Record the excess air changes seen with known fans (by name) or a known count running."""
        if fans_on:
            named = [m for m in fans_on if m in self.members]
            if not named:
                return False
            self.calibrations.append({"ts": ts, "excess": excess_ach, "fans": named})
        elif count is not None and count >= 0:
            self.calibrations.append({"ts": ts, "excess": excess_ach, "count": int(count)})
        else:
            return False
        del self.calibrations[:-_CALIBRATIONS_KEPT]
        return True

    def _group_average(self) -> float | None:
        per_fan = [
            c["excess"] / len(c["fans"]) if "fans" in c else c["excess"] / c["count"]
            for c in self.calibrations
            if (c.get("count") or c.get("fans")) and c["excess"] > 0
        ]
        return statistics.median(per_fan) if per_fan else None

    def _has_own(self, member: str) -> bool:
        return any(c.get("fans") == [member] and c["excess"] > 0 for c in self.calibrations)

    def member_ach(self, member: str) -> float:
        """Learned air changes per hour for one fan."""
        own = [c["excess"] for c in self.calibrations if c.get("fans") == [member] and c["excess"] > 0]
        if own:
            return statistics.median(own)
        return self._group_average() or self.default_ach

    def natural_offset(self) -> float:
        """Excess seen with zero fans on (calibration error in the natural rate)."""
        zero = [c["excess"] for c in self.calibrations if c.get("count") == 0]
        return statistics.median(zero) if zero else 0.0

    def estimate(self, excess_ach: float | None) -> dict[str, Any]:
        """How many (and which) fans best explain ``excess_ach``."""
        n = len(self.members)
        if excess_ach is None:
            return {"count": None, "members": [], "excess_ach": None}
        excess = excess_ach - self.natural_offset()
        rates = {m: self.member_ach(m) for m in self.members}
        smallest = min(rates.values())
        if excess < smallest * 0.5:
            return {"count": 0, "members": [], "excess_ach": excess}
        if n <= _MAX_SUBSET_SEARCH:
            best: tuple[float, int, tuple[str, ...]] | None = None
            for k in range(1, n + 1):
                for combo in itertools.combinations(self.members, k):
                    err = abs(sum(rates[m] for m in combo) - excess)
                    key = (err, k, combo)
                    if best is None or key[:2] < best[:2]:
                        best = key
            assert best is not None
            # Only name fans once each has its own calibration; otherwise any
            # same-sized combination would fit equally well.
            identified = all(self._has_own(m) for m in self.members)
            return {
                "count": best[1],
                "members": list(best[2]) if identified else [],
                "excess_ach": excess,
            }
        count = max(1, min(n, round(excess / statistics.fmean(rates.values()))))
        return {"count": count, "members": [], "excess_ach": excess}
