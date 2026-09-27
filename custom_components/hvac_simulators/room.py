"""One monitored room (pure Python, no Home Assistant imports).

A space (usually your home) has a main room plus optional extra rooms, each
tied to a Home Assistant area with its own sensors. Rooms that flow into each
other (open doorways, hallways, a gap under the door) form a zone: an
appliance in a room without sensors, like a bathroom fan, is observed by a
sensored room in the same zone, like the living room whose CO2 it clears.

Each room keeps its own trend buffer, learned model, heat-source effects,
occupancy model, fan-group estimators and efficiency record.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .appliances import Appliance, available_labels, label_name, make_label, split_label
from .cycling import detect_cycling
from .detectors import multi_rates, shower, virtual_opening
from .efficiency import EfficiencyTracker
from .engine import (
    ACTIVE_CAUSES,
    CAUSE_HUMIDIFY,
    Features,
    Sample,
    SampleBuffer,
    Tuning,
    absolute_humidity,
    compute_features,
    normalize,
    score_causes,
)
from .fans import FanGroupEstimator
from .gains import GainsModel
from .learning import Learner, Prediction
from .occupancy import DEFAULT_ACH, VENT_CLOSED, OccupancyModel
from .plugs import MOISTURE_PRIOR_PER_KW

MAIN_ROOM = "main"
CYCLE_HISTORY_S = 3 * 3600
FEEDBACK_CONFIDENCE = 0.6
_OCCUPANCY_EMA = 0.3
# Hysteresis for the estimated door/window.
_OPEN_ON, _OPEN_OFF = 0.6, 0.4
# A shower keeps affecting the zone's humidity for a while after it stops.
SHOWER_AFTERGLOW_S = 30 * 60

ROOM_TYPES = ("living", "bedroom", "bathroom", "laundry", "kitchen", "hallway", "office", "other")
AIR_GAPS = ("none", "small", "large")
# How much leakier than a tight room: scales the passive exchange rate and
# natural air changes before they are learned.
_AIR_GAP_FACTOR = {"none": 1.0, "small": 1.5, "large": 2.5}


@dataclass
class RoomConfig:
    """What the user configured for a room."""

    id: str
    name: str
    area_id: str | None = None
    type: str = "other"
    temperature: str | None = None
    humidity: str | None = None
    co2: str | None = None
    air_quality: str | None = None
    openings: list[str] = field(default_factory=list)
    volume_m3: float = 50.0
    air_gaps: str = "none"
    air_gap_notes: str = ""
    flows_into: list[str] = field(default_factory=list)
    use_virtual_openings: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoomConfig:
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            area_id=data.get("area_id") or None,
            type=data.get("type") or "other",
            temperature=data.get("temperature") or None,
            humidity=data.get("humidity") or None,
            co2=data.get("co2") or None,
            air_quality=data.get("air_quality") or None,
            openings=list(data.get("openings") or []),
            volume_m3=float(data.get("volume_m3") or 50.0),
            air_gaps=data.get("air_gaps") if data.get("air_gaps") in AIR_GAPS else "none",
            air_gap_notes=str(data.get("air_gap_notes") or ""),
            flows_into=list(data.get("flows_into") or []),
            use_virtual_openings=bool(data.get("use_virtual_openings", True)),
        )

    @property
    def is_main(self) -> bool:
        return self.id == MAIN_ROOM

    @property
    def sensed(self) -> bool:
        """Has a temperature sensor, so the full engine can run."""
        return self.temperature is not None

    @property
    def entities(self) -> list[str]:
        ids = [self.temperature, self.humidity, self.co2, self.air_quality, *self.openings]
        return [e for e in ids if e]


@dataclass
class RoomContext:
    """What the room needs to know from the rest of the space for one evaluation."""

    sun_up: bool | None
    hour: int
    date: str
    activity: str
    vent_state: str
    appliance_active: bool  # an HVAC appliance observed here is actively running
    shower_in_zone: bool = False


def zones(rooms: list[RoomConfig]) -> list[set[str]]:
    """Groups of rooms connected by 'flows into' links (links work both ways)."""
    ids = {r.id for r in rooms}
    links: dict[str, set[str]] = {r.id: set() for r in rooms}
    for room in rooms:
        for other in room.flows_into:
            if other in ids and other != room.id:
                links[room.id].add(other)
                links[other].add(room.id)
    seen: set[str] = set()
    groups: list[set[str]] = []
    for start in ids:
        if start in seen:
            continue
        group, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node in group:
                continue
            group.add(node)
            stack.extend(links[node] - group)
        seen |= group
        groups.append(group)
    return groups


def observing_room(appliance: Appliance, rooms: list[RoomConfig]) -> str:
    """The sensored room that sees an appliance's effect.

    Its own room if that room has sensors; otherwise a sensored room in the same
    zone (the main room first); otherwise the main room.
    """
    by_id = {r.id: r for r in rooms}
    by_area = {r.area_id: r for r in rooms if r.area_id}
    home = by_id.get(appliance.room or "") or by_area.get(appliance.room or "")
    if home is None:
        return MAIN_ROOM
    if home.sensed:
        return home.id
    for group in zones(rooms):
        if home.id in group:
            sensed = [rid for rid in group if by_id[rid].sensed]
            if MAIN_ROOM in sensed:
                return MAIN_ROOM
            if sensed:
                return sorted(sensed)[0]
    return MAIN_ROOM


class Room:
    """Runtime state and learning for one room."""

    def __init__(
        self,
        config: RoomConfig,
        base_tuning: Tuning,
        window_s: float,
        max_learned_weight: float,
        appliances: list[Appliance],
        heat_sources: list[Appliance],
    ) -> None:
        self.config = config
        factor = _AIR_GAP_FACTOR.get(config.air_gaps, 1.0)
        self.base_tuning = replace(
            base_tuning, k_closed=base_tuning.k_closed * factor, ach_natural=base_tuning.ach_natural * factor
        )
        self.window_s = window_s
        self.appliances = appliances  # simulated appliances observed in this room
        self.heat_sources = heat_sources
        self.learner = Learner(max_learned_weight)
        self.gains = GainsModel()
        self.gains.load(None, [s.id for s in heat_sources], self._moisture_priors())
        self.occupancy = OccupancyModel(config.volume_m3, base_tuning.co2_outdoor)
        self.occupancy.default_closed_ach = DEFAULT_ACH[VENT_CLOSED] * factor
        self.fan_groups: dict[str, FanGroupEstimator] = {
            a.id: FanGroupEstimator(a.members, a.airflow_m3h, config.volume_m3)
            for a in appliances
            if a.is_fan_group
        }
        self.efficiency = EfficiencyTracker()
        self.buffer = SampleBuffer(max(window_s * 2, CYCLE_HISTORY_S))
        self.features: Features | None = None
        self.prediction: Prediction | None = None
        self.cycle: dict[str, Any] | None = None
        self.opening_estimate: dict[str, Any] = {"score": 0.0, "signals": {}}
        self.estimated_open = False
        self.shower_state: dict[str, Any] = {"showering": False, "score": 0.0, "rise_rh": None}
        self.last_shower_ts: float | None = None
        self.fan_estimates: dict[str, dict[str, Any]] = {}
        self.occupancy_estimate: dict[str, Any] | None = None
        self.last_taught: str | None = None
        self._people_smoothed: float | None = None
        self._last_eval_ts: float | None = None

    # --- identity --------------------------------------------------------

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def name(self) -> str:
        return self.config.name

    def _moisture_priors(self) -> dict[str, float]:
        return {s.id: MOISTURE_PRIOR_PER_KW.get(s.kind, 0.0) for s in self.heat_sources}

    # --- persistence -----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "learner": self.learner.as_dict(),
            "gains": self.gains.as_dict(),
            "occupancy": self.occupancy.as_dict(),
            "fan_groups": {k: g.as_dict() for k, g in self.fan_groups.items()},
            "efficiency": self.efficiency.as_dict(),
            "last_taught": self.last_taught,
        }

    def load(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        self.learner.load(data.get("learner"))
        known = {a.id for a in self.appliances}
        for appliance_id in {split_label(s.label)[1] for s in self.learner.samples} - {None} - known:
            self.learner.forget_appliance(appliance_id)  # type: ignore[arg-type]
        self.gains.load(data.get("gains"), [s.id for s in self.heat_sources], self._moisture_priors())
        self.occupancy.load(data.get("occupancy"))
        for group_id, group_data in (data.get("fan_groups") or {}).items():
            if group_id in self.fan_groups:
                self.fan_groups[group_id].load(group_data)
        self.efficiency.load(data.get("efficiency"))
        self.last_taught = data.get("last_taught")

    def reset_learning(self) -> None:
        self.learner.reset()
        self.occupancy.reset()
        self.gains.reset()
        self.efficiency.reset()
        for group in self.fan_groups.values():
            group.reset()
        self.last_taught = None

    # --- evaluation --------------------------------------------------------

    @property
    def tuning(self) -> Tuning:
        tuning = self.learner.apply_to_tuning(self.base_tuning)
        tuning.ach_natural = self.occupancy.ach(VENT_CLOSED)
        return tuning

    @property
    def has_opening_sensors(self) -> bool:
        return bool(self.config.openings)

    def add_sample(self, sample: Sample) -> None:
        self.buffer.add(sample)

    def evaluate(self, now: float, ctx: RoomContext) -> None:
        """Recompute features, prediction and every per-room estimate."""
        tuning = self.tuning
        mean_power = self.buffer.mean_gains(self.window_s, now)
        gains = self.gains.predict(mean_power)
        self.cycle = detect_cycling(self.buffer.samples, now, CYCLE_HISTORY_S)
        self.shower_state = shower(self.buffer, now)
        if self.shower_state["showering"]:
            self.last_shower_ts = now

        def features(is_open: bool | None, known: bool) -> Features | None:
            return compute_features(
                self.buffer,
                now,
                self.window_s,
                tuning,
                ctx.sun_up,
                ctx.hour,
                gains=gains,
                cycle=self.cycle,
                openings_known=known,
                is_open=is_open,
            )

        if self.has_opening_sensors:
            f = features(None, True)
        else:
            closed = features(False, False)
            f = closed
            if closed is not None and self.config.use_virtual_openings:
                self.opening_estimate = virtual_opening(closed, tuning)
                score = self.opening_estimate["score"]
                if self.estimated_open:
                    self.estimated_open = score >= _OPEN_OFF
                else:
                    self.estimated_open = score >= _OPEN_ON
                # Door/window state counts as known once two independent signals exist.
                known = len(self.opening_estimate["signals"]) >= 2
                f = features(self.estimated_open, known) if (self.estimated_open or known) else closed
        self.features = f

        if f is not None:
            rule = score_causes(f, tuning)
            if ctx.shower_in_zone:
                # A shower in this zone explains rising humidity; don't blame a humidifier.
                rule.probs[CAUSE_HUMIDIFY] *= 0.2
                rule.probs = normalize(rule.probs)
                rule.reasons.setdefault(CAUSE_HUMIDIFY, []).append(
                    "A shower in this zone explains the humidity"
                )
            self.prediction = self.learner.predict(f, rule, self.appliances, tuning)
        else:
            self.prediction = None

        self._learn_gains(mean_power, ctx.appliance_active)
        excess = None if f is None or f.ach_obs is None else f.ach_obs - tuning.ach_natural
        self.fan_estimates = {gid: g.estimate(excess) for gid, g in self.fan_groups.items()}
        self._update_occupancy(ctx)
        if self._last_eval_ts is not None and self.prediction is not None:
            cause, _ = split_label(self.prediction.label)
            self.efficiency.record(
                ctx.date, (now - self._last_eval_ts) / 3600.0, f, cause, self.prediction.confidence
            )
        self._last_eval_ts = now

    def _learn_gains(self, mean_power: dict[str, float], appliance_active: bool) -> None:
        f = self.features
        if f is None or f.is_open or appliance_active or not any(w > 0 for w in mean_power.values()):
            return
        if self.prediction is not None and split_label(self.prediction.label)[0] in ACTIVE_CAUSES:
            return
        self.gains.learn(mean_power, f.r_t_raw - f.expected_passive, f.r_h_raw)

    def _update_occupancy(self, ctx: RoomContext) -> None:
        f = self.features
        estimate = self.occupancy.estimate(
            f.co2 if f else None, f.r_co2 if f else None, ctx.vent_state, ctx.activity
        )
        if estimate is None:
            self.occupancy_estimate = None
            return
        raw = estimate["people"]
        self._people_smoothed = (
            raw
            if self._people_smoothed is None
            else (self._people_smoothed + _OCCUPANCY_EMA * (raw - self._people_smoothed))
        )
        estimate["people_smoothed"] = self._people_smoothed
        estimate["calibrations"] = self.occupancy.calibration_count
        self.occupancy_estimate = estimate

    # --- queries -------------------------------------------------------------

    @property
    def labels(self) -> list[str]:
        return available_labels(self.appliances)

    def label_name(self, label: str | None) -> str | None:
        return None if label is None else label_name(label, self.appliances)

    def label_from_name(self, name: str) -> str | None:
        for label in self.labels:
            if name in (label, self.label_name(label)):
                return label
        return None

    @property
    def needs_feedback(self) -> bool:
        p = self.prediction
        if p is None:
            return False
        return split_label(p.label)[0] in ACTIVE_CAUSES and p.confidence < FEEDBACK_CONFIDENCE

    def rates(self, attr: str, now: float) -> dict[str, float | None]:
        return multi_rates(self.buffer, attr, now)

    def source_effects(self, now: float) -> dict[str, tuple[float, float]]:
        return self.gains.per_source(self.buffer.mean_gains(self.window_s, now))

    def differentials(self, other: Room | None = None) -> dict[str, float | None]:
        """Indoor minus outdoor (or minus ``other`` room) for each paired reading."""
        latest = self.buffer.latest
        t_in, rh_in, co2 = latest("t_in"), latest("rh_in"), latest("co2")
        if other is None:
            t_ref, rh_ref = latest("t_out"), latest("rh_out")
        else:
            t_ref, rh_ref = other.buffer.latest("t_in"), other.buffer.latest("rh_in")
        ah_in, ah_ref = absolute_humidity(t_in, rh_in), absolute_humidity(t_ref, rh_ref)

        def diff(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else a - b

        f = self.features
        out: dict[str, float | None] = {
            "temperature": diff(t_in, t_ref),
            "humidity": diff(rh_in, rh_ref),
            "absolute_humidity_in": ah_in,
            "absolute_humidity_out": ah_ref,
            "absolute_humidity": diff(ah_in, ah_ref),
            "temperature_rate_raw": None if f is None else f.r_t_raw,
            "humidity_rate_raw": None if f is None else f.r_h_raw,
        }
        if other is None:
            out["co2_excess"] = None if co2 is None else co2 - self.base_tuning.co2_outdoor
        return out

    # --- teaching --------------------------------------------------------------

    def teach(self, label: str, now: float, source: str = "feedback") -> bool:
        if label not in self.labels or self.features is None:
            return False
        suggested = self.prediction.label if self.prediction and source == "feedback" else None
        self.learner.teach(self.features, label, source=source, was_suggested=suggested, now=now)
        if source == "feedback":
            self.last_taught = label
        return True

    def teach_mode(self, appliance: Appliance, mode: str, now: float, source: str) -> None:
        cause = appliance.cause_for_mode(mode)
        if cause is not None and self.features is not None:
            self.learner.teach(self.features, make_label(cause, appliance.id), source=source, now=now)

    def calibrate_occupancy(self, now: float, people: int, activity: str, vent_state: str) -> bool:
        f = self.features
        ok = self.occupancy.calibrate(
            now, people, activity, vent_state, f.co2 if f else None, f.r_co2 if f else None
        )
        if ok:
            self._people_smoothed = float(people)
        return ok

    def calibrate_fans(
        self, now: float, appliance_id: str, fans_on: list[str] | None, count: int | None
    ) -> bool:
        group = self.fan_groups.get(appliance_id)
        f = self.features
        if group is None or f is None or f.ach_obs is None:
            return False
        return group.calibrate(now, f.ach_obs - self.tuning.ach_natural, fans_on, count)
