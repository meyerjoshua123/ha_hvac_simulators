"""Heating/cooling effectiveness tracking (pure Python, no Home Assistant imports).

While an appliance is actively heating or cooling, the engine knows how fast
it is moving the temperature *beyond* what the building would do on its own
(the residual rate). That performance naturally drops as the gap to outdoor
temperature grows (more "lift"), so every sample is stored as
``(lift, performance)``.

A baseline is fitted from the reference period that starts when the filter was
last cleaned (or when tracking began). Recent performance is compared with what
the baseline predicts at the same lift, so a hot day doesn't look like a
clogged filter. The ratio maps to a rating from Excellent to Terrible.
"""

from __future__ import annotations

import statistics
from typing import Any

RATINGS = ("excellent", "great", "good", "ok", "bad", "terrible")
RATING_NAMES = {
    "excellent": "Excellent",
    "great": "Great",
    "good": "Good",
    "ok": "OK",
    "bad": "Bad",
    "terrible": "Terrible",
}
# Lower bound of the performance ratio for each rating.
_RATING_FLOORS = (
    ("excellent", 0.95),
    ("great", 0.85),
    ("good", 0.75),
    ("ok", 0.62),
    ("bad", 0.48),
)

REFERENCE_DAYS = 14
MIN_BASELINE_SAMPLES = 20
MIN_RECENT_SAMPLES = 10
RECENT_SAMPLES = 40
MAX_SAMPLES = 3000
_MIN_PREDICTED = 0.05


def rating_for(ratio: float | None) -> str | None:
    """Map a performance ratio to a rating key."""
    if ratio is None:
        return None
    for rating, floor in _RATING_FLOORS:
        if ratio >= floor:
            return rating
    return "terrible"


def rating_rank(rating: str | None) -> int:
    """0 = excellent ... 5 = terrible, -1 = unknown."""
    return RATINGS.index(rating) if rating in RATINGS else -1


class Baseline:
    """``performance = a + b * lift`` with b <= 0."""

    def __init__(self, a: float, b: float) -> None:
        self.a = a
        self.b = b

    def predict(self, lift: float) -> float:
        return max(_MIN_PREDICTED, self.a + self.b * lift)

    def as_dict(self) -> dict[str, float]:
        return {"a": self.a, "b": self.b}

    @classmethod
    def fit(cls, points: list[tuple[float, float]]) -> Baseline | None:
        if len(points) < MIN_BASELINE_SAMPLES:
            return None
        lifts = [p[0] for p in points]
        perfs = [p[1] for p in points]
        mean_l = statistics.fmean(lifts)
        mean_p = statistics.fmean(perfs)
        sxx = sum((x - mean_l) ** 2 for x in lifts)
        b = 0.0
        if sxx > len(points):  # lift varies by more than ~1 °C: enough to fit a slope
            b = sum((x - mean_l) * (y - mean_p) for x, y in zip(lifts, perfs)) / sxx
            b = min(0.0, b)
        a = mean_p - b * mean_l
        if a <= _MIN_PREDICTED:
            return None
        return cls(a, b)


class EffectivenessTracker:
    """Effectiveness of one appliance in one mode (heat or cool)."""

    def __init__(self) -> None:
        self.samples: list[tuple[float, float, float]] = []  # (ts, lift, performance)
        self.reference_since: float | None = None
        self.previous_baseline: Baseline | None = None
        self.filter_events: list[dict[str, Any]] = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": [[round(t, 1), round(l, 3), round(p, 4)] for t, l, p in self.samples],
            "reference_since": self.reference_since,
            "previous_baseline": self.previous_baseline.as_dict() if self.previous_baseline else None,
            "filter_events": self.filter_events[-50:],
        }

    def load(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        self.samples = [(float(t), float(l), float(p)) for t, l, p in data.get("samples", [])]
        self.reference_since = data.get("reference_since")
        prev = data.get("previous_baseline")
        self.previous_baseline = Baseline(prev["a"], prev["b"]) if prev else None
        self.filter_events = list(data.get("filter_events", []))

    def add(self, ts: float, lift: float, performance: float) -> None:
        """Record a sample taken while actively heating/cooling."""
        if self.reference_since is None:
            self.reference_since = ts
        self.samples.append((ts, lift, performance))
        if len(self.samples) > MAX_SAMPLES:
            self.samples = self.samples[-MAX_SAMPLES:]

    def mark_filter(self, ts: float, status: str) -> None:
        """Calibration: ``clean`` starts a fresh reference period."""
        self.filter_events.append({"ts": ts, "status": status})
        if status == "clean":
            current = self.baseline()
            if current is not None:
                self.previous_baseline = current
            self.reference_since = ts

    def _reference_points(self) -> list[tuple[float, float]]:
        if self.reference_since is None:
            return []
        end = self.reference_since + REFERENCE_DAYS * 86400
        return [(l, p) for t, l, p in self.samples if self.reference_since <= t <= end]

    def baseline(self) -> Baseline | None:
        """Baseline from the current reference period, or the previous one while it fills."""
        return Baseline.fit(self._reference_points()) or self.previous_baseline

    def ratio(self) -> float | None:
        """Recent performance relative to baseline (1.0 = as good as when clean)."""
        base = self.baseline()
        if base is None:
            return None
        since = self.reference_since or 0.0
        recent = [s for s in self.samples if s[0] >= since][-RECENT_SAMPLES:]
        if len(recent) < MIN_RECENT_SAMPLES:
            return None
        return statistics.median(p / base.predict(l) for _, l, p in recent)

    def rating(self) -> str | None:
        return rating_for(self.ratio())

    @property
    def learning(self) -> bool:
        return self.baseline() is None
