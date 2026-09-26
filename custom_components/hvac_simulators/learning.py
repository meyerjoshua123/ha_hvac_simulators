"""Learning layer for HVAC Simulators (pure Python, no Home Assistant imports).

Every time the user confirms or corrects a suggestion, the current feature
vector is stored with the right label. Predictions blend the rule engine with a
distance-weighted nearest-neighbour vote over those examples; the more the user
teaches, the more weight the learned vote gets. Feedback also tunes the passive
heat-exchange rates of the building and which appliance usually produces a
cause (e.g. "heating is usually the heat pump, not the oil heater").
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .appliances import Appliance, available_labels, make_label, split_label
from .engine import (
    ACTIVE_CAUSES,
    CAUSE_HEAT_LEAK_IN,
    CAUSE_HEAT_LOSS,
    CAUSE_IDLE,
    CAUSE_WINDOW_COOLING,
    CAUSE_WINDOW_WARMING,
    Features,
    RuleResult,
    Tuning,
    entropy_confidence,
)

MAX_SAMPLES = 1000
KNN_K = 7
# Learned vote weight approaches this as examples accumulate.
DEFAULT_MAX_LEARNED_WEIGHT = 0.75
_WEIGHT_HALF_POINT = 15  # examples at which the learned vote gets half its max weight
_PASSIVE_EMA = 0.2


@dataclass
class TrainingSample:
    """A labeled feature vector."""

    features: Features
    label: str
    ts: float
    source: str = "feedback"  # feedback | confirm | manual

    def as_dict(self) -> dict[str, Any]:
        return {
            "features": self.features.as_dict(),
            "label": self.label,
            "ts": self.ts,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingSample:
        return cls(
            features=Features.from_dict(data["features"]),
            label=str(data["label"]),
            ts=float(data.get("ts", 0.0)),
            source=str(data.get("source", "feedback")),
        )


@dataclass
class Prediction:
    """Final blended suggestion."""

    label: str
    confidence: float
    probs: dict[str, float]
    rule_probs: dict[str, float]
    learned_weight: float
    reasons: list[str] = field(default_factory=list)


def feature_vector(f: Features, tuning: Tuning) -> list[float]:
    """Scale features so each dimension matters roughly equally."""
    d_out = f.delta_out
    return [
        f.r_t / max(tuning.active_rate, 0.05),
        (f.r_h or 0.0) / 3.0,
        (f.r_aq or 0.0) / 0.1,
        0.0 if d_out is None else d_out / 5.0,
        1.0 if f.is_open else 0.0,
        f.residual / max(tuning.active_rate, 0.05),
        0.0 if f.t_out is None else (f.t_out - tuning.solar_threshold) / 8.0,
        # Time of day as a point on a circle so 23:00 is close to 01:00.
        0.5 * math.sin(2 * math.pi * f.hour / 24.0),
        0.5 * math.cos(2 * math.pi * f.hour / 24.0),
    ]


class Learner:
    """Stores training samples and learned parameters, and makes predictions."""

    def __init__(self, max_learned_weight: float = DEFAULT_MAX_LEARNED_WEIGHT) -> None:
        self.samples: list[TrainingSample] = []
        self.max_learned_weight = max_learned_weight
        self.k_closed: float | None = None
        self.k_open: float | None = None
        self.appliance_counts: dict[str, int] = {}
        self.confirmed = 0
        self.corrected = 0

    # --- persistence -----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": [s.as_dict() for s in self.samples],
            "k_closed": self.k_closed,
            "k_open": self.k_open,
            "appliance_counts": dict(self.appliance_counts),
            "confirmed": self.confirmed,
            "corrected": self.corrected,
        }

    def load(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        self.samples = []
        for raw in data.get("samples", []):
            try:
                self.samples.append(TrainingSample.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        self.k_closed = data.get("k_closed")
        self.k_open = data.get("k_open")
        self.appliance_counts = {str(k): int(v) for k, v in data.get("appliance_counts", {}).items()}
        self.confirmed = int(data.get("confirmed", 0))
        self.corrected = int(data.get("corrected", 0))

    def reset(self) -> None:
        self.__init__(self.max_learned_weight)  # type: ignore[misc]

    # --- stats -----------------------------------------------------------

    @property
    def accuracy(self) -> float | None:
        """Share of feedback where the suggestion was right."""
        total = self.confirmed + self.corrected
        if total == 0:
            return None
        return self.confirmed / total

    def learned_weight(self) -> float:
        n = len(self.samples)
        return self.max_learned_weight * n / (n + _WEIGHT_HALF_POINT)

    def apply_to_tuning(self, tuning: Tuning) -> Tuning:
        """Tuning with learned passive rates substituted in."""
        return replace(
            tuning,
            k_closed=self.k_closed if self.k_closed is not None else tuning.k_closed,
            k_open=self.k_open if self.k_open is not None else tuning.k_open,
        )

    # --- teaching --------------------------------------------------------

    def teach(
        self,
        features: Features,
        label: str,
        source: str = "feedback",
        was_suggested: str | None = None,
        now: float | None = None,
    ) -> None:
        """Record a labeled example and update learned parameters."""
        ts = now if now is not None else time.time()
        self.samples.append(TrainingSample(features, label, ts, source))
        if len(self.samples) > MAX_SAMPLES:
            self.samples = self.samples[-MAX_SAMPLES:]
        if was_suggested is not None:
            if was_suggested == label:
                self.confirmed += 1
            else:
                self.corrected += 1
        cause, appliance_id = split_label(label)
        if appliance_id is not None:
            self.appliance_counts[label] = self.appliance_counts.get(label, 0) + 1
        self._update_passive(features, cause)

    def _update_passive(self, f: Features, cause: str) -> None:
        """Learn how fast the building exchanges heat with outside."""
        d_out = f.delta_out
        if d_out is None or abs(d_out) < 2.0:
            return
        observed_k = f.r_t / d_out
        if not 0.0 < observed_k < 2.0:
            return
        if not f.is_open and cause in (CAUSE_HEAT_LEAK_IN, CAUSE_HEAT_LOSS, CAUSE_IDLE):
            base = self.k_closed if self.k_closed is not None else observed_k
            self.k_closed = base + _PASSIVE_EMA * (observed_k - base)
        elif f.is_open and cause in (CAUSE_WINDOW_COOLING, CAUSE_WINDOW_WARMING):
            base = self.k_open if self.k_open is not None else observed_k
            self.k_open = base + _PASSIVE_EMA * (observed_k - base)

    def forget_appliance(self, appliance_id: str) -> None:
        """Drop everything learned about a removed appliance."""
        self.samples = [s for s in self.samples if split_label(s.label)[1] != appliance_id]
        self.appliance_counts = {
            k: v for k, v in self.appliance_counts.items() if split_label(k)[1] != appliance_id
        }

    # --- prediction ------------------------------------------------------

    def expand_rule_probs(self, rule: RuleResult, appliances: list[Appliance]) -> dict[str, float]:
        """Spread each active cause across the appliances able to produce it."""
        out: dict[str, float] = {}
        for cause, p in rule.probs.items():
            if cause not in ACTIVE_CAUSES:
                out[cause] = out.get(cause, 0.0) + p
                continue
            capable = [a for a in appliances if cause in a.capabilities]
            if not capable:
                out[cause] = out.get(cause, 0.0) + p
                continue
            weights = [1 + self.appliance_counts.get(make_label(cause, a.id), 0) for a in capable]
            total = float(sum(weights))
            for appliance, weight in zip(capable, weights):
                label = make_label(cause, appliance.id)
                out[label] = out.get(label, 0.0) + p * weight / total
        return out

    def knn_probs(self, f: Features, tuning: Tuning, valid: list[str]) -> dict[str, float] | None:
        """Distance-weighted vote of the nearest taught examples."""
        pool = [s for s in self.samples if s.label in valid]
        if not pool:
            return None
        query = feature_vector(f, tuning)
        scored: list[tuple[float, str]] = []
        for sample in pool:
            vec = feature_vector(sample.features, tuning)
            dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(query, vec)))
            scored.append((dist, sample.label))
        scored.sort(key=lambda item: item[0])
        votes: dict[str, float] = {}
        for dist, label in scored[:KNN_K]:
            votes[label] = votes.get(label, 0.0) + 1.0 / (dist + 0.25)
        total = sum(votes.values())
        return {k: v / total for k, v in votes.items()}

    def predict(
        self,
        f: Features,
        rule: RuleResult,
        appliances: list[Appliance],
        tuning: Tuning,
    ) -> Prediction:
        """Blend rule scores with learned examples."""
        valid = available_labels(appliances)
        rule_probs = {k: v for k, v in self.expand_rule_probs(rule, appliances).items() if k in valid}
        knn = self.knn_probs(f, tuning, valid)
        weight = self.learned_weight() if knn else 0.0
        probs: dict[str, float] = {}
        for label in valid:
            probs[label] = (1 - weight) * rule_probs.get(label, 0.0) + weight * (
                knn.get(label, 0.0) if knn else 0.0
            )
        total = sum(probs.values()) or 1.0
        probs = {k: v / total for k, v in probs.items()}
        best = max(probs, key=lambda k: probs[k])
        cause, _ = split_label(best)
        reasons = list(rule.reasons.get(cause, []))
        if knn and knn.get(best, 0.0) > 0.5 and weight > 0.2:
            reasons.append("Matches situations you taught before")
        return Prediction(
            label=best,
            confidence=entropy_confidence(probs),
            probs=probs,
            rule_probs=rule_probs,
            learned_weight=weight,
            reasons=reasons,
        )
