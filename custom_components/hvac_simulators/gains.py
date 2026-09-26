"""Learned effect of plugged-in heat sources (pure Python, no Home Assistant imports).

A fridge, dryer or oven turns electricity into heat (and a vented or condenser
dryer into moisture) inside the space. Each source's smart plug tells us its
power; this module learns how many °C/h and %RH/h one kW of it adds, so that
effect can be subtracted before the engine decides what the HVAC is doing.

Learning is a ridge regression through the origin per source, fitted on the
part of the temperature/humidity change the building shell doesn't explain,
taken only while no HVAC appliance is running and doors/windows are shut.
Other sources' current predictions are removed first (backfitting), so two
sources running together don't get each other's credit.
"""

from __future__ import annotations

from typing import Any

# Prior: 1 kW of heat into a typical room warms it ~0.4 °C/h; no moisture.
PRIOR_T_PER_KW = 0.4
PRIOR_H_PER_KW = 0.0
# Prior strength in kW² (≈ this many samples at 1 kW).
PRIOR_WEIGHT = 20.0
MAX_SAMPLES = 800
MIN_KW = 0.02


class HeatSourceModel:
    """Effect of one heat source per kW of measured power."""

    def __init__(self) -> None:
        self.samples: list[tuple[float, float, float | None]] = []  # (kW, °C/h, %/h)

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": [
                [round(p, 4), round(t, 4), None if h is None else round(h, 3)] for p, t, h in self.samples
            ]
        }

    def load(self, data: dict[str, Any] | None) -> None:
        if data:
            self.samples = [
                (float(p), float(t), None if h is None else float(h)) for p, t, h in data.get("samples", [])
            ]

    def add(self, kw: float, target_t: float, target_h: float | None) -> None:
        self.samples.append((kw, target_t, target_h))
        del self.samples[:-MAX_SAMPLES]

    @property
    def per_kw_t(self) -> float:
        num = sum(p * t for p, t, _ in self.samples) + PRIOR_WEIGHT * PRIOR_T_PER_KW
        den = sum(p * p for p, _, _ in self.samples) + PRIOR_WEIGHT
        return num / den

    @property
    def per_kw_h(self) -> float:
        pts = [(p, h) for p, _, h in self.samples if h is not None]
        num = sum(p * h for p, h in pts) + PRIOR_WEIGHT * PRIOR_H_PER_KW
        den = sum(p * p for p, _ in pts) + PRIOR_WEIGHT
        return num / den

    def effect(self, watts: float) -> tuple[float, float]:
        kw = max(0.0, watts) / 1000.0
        return kw * self.per_kw_t, kw * self.per_kw_h


class GainsModel:
    """All heat sources in a space."""

    def __init__(self) -> None:
        self.sources: dict[str, HeatSourceModel] = {}

    def as_dict(self) -> dict[str, Any]:
        return {k: m.as_dict() for k, m in self.sources.items()}

    def load(self, data: dict[str, Any] | None, source_ids: list[str]) -> None:
        self.sources = {sid: HeatSourceModel() for sid in source_ids}
        for sid, raw in (data or {}).items():
            if sid in self.sources:
                self.sources[sid].load(raw)

    def reset(self) -> None:
        self.sources = {sid: HeatSourceModel() for sid in self.sources}

    def per_source(self, powers: dict[str, float]) -> dict[str, tuple[float, float]]:
        return {sid: self.sources[sid].effect(w) for sid, w in powers.items() if sid in self.sources}

    def predict(self, powers: dict[str, float]) -> tuple[float, float]:
        """Total (°C/h, %RH/h) caused by the sources at these powers (W)."""
        effects = self.per_source(powers).values()
        return sum(e[0] for e in effects), sum(e[1] for e in effects)

    def learn(self, powers: dict[str, float], residual_t_raw: float, r_h_raw: float | None) -> None:
        """Fit each running source against what's left after the others' predictions."""
        effects = self.per_source(powers)
        total_t = sum(e[0] for e in effects.values())
        total_h = sum(e[1] for e in effects.values())
        for sid, watts in powers.items():
            kw = watts / 1000.0
            if sid not in self.sources or kw < MIN_KW:
                continue
            own_t, own_h = effects[sid]
            target_t = residual_t_raw - (total_t - own_t)
            target_h = None if r_h_raw is None else r_h_raw - (total_h - own_h)
            self.sources[sid].add(kw, target_t, target_h)
