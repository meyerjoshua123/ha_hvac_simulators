"""Per-appliance state simulation (pure Python, no Home Assistant imports).

Turns the room-level prediction into a mode and a status for each appliance.

* **Mode** is what the unit is set to: heat, cool, dry, fan, on, or off.
* **Status** is what it is doing right now: ``active`` (compressor/element
  running), ``idle`` (on, but resting at its setpoint between cycles) or ``off``.

A cooling unit that lets the room fall, rests while it drifts back up a little,
then cools again stays in *cool* mode throughout; only its status cycles
between active and idle. The turning points of those cycles give the setpoint.
"""

from __future__ import annotations

import statistics
from typing import Any

from .appliances import MODE_COOL, MODE_DRY, MODE_HEAT, MODE_OFF, Appliance, split_label
from .effectiveness import EffectivenessTracker, rating_rank
from .engine import CYCLE_DETECTED, Features

STATUS_OFF = "off"
STATUS_ACTIVE = "active"
STATUS_IDLE = "idle"
STATUSES = (STATUS_ACTIVE, STATUS_IDLE, STATUS_OFF)

MANUAL_AUTO = "auto"

# Half-width of the band a manually-set unit cycles within around its setpoint.
_MANUAL_BAND = 0.3
# Past the setpoint by more than this, the unit has nothing to do: compressor
# or element off, fan only. Learned from a night where a cooling aircon set to
# 18 °C sat idle in a 17 °C room.
_SATISFIED_MARGIN = 0.5

IDLE_HOLDING = "holding setpoint"
IDLE_SATISFIED = "setpoint satisfied, compressor off (fan only)"
IDLE_CYCLING = "between compressor cycles (fan only)"
_OFFSET_EMA = 0.05
# Ignore power gaps longer than this when integrating energy (e.g. HA was down).
_MAX_INTEGRATION_GAP_S = 900.0
_TURN_POINTS_KEPT = 12
_DUTY_WINDOW_S = 2 * 3600
_CALIBRATIONS_KEPT = 20


class ApplianceSimulator:
    """Live state of one appliance."""

    def __init__(self, appliance: Appliance) -> None:
        self.appliance = appliance
        self.status = STATUS_OFF
        self.mode = MODE_OFF
        self.manual_mode = MANUAL_AUTO
        self.manual_setpoint: float | None = None
        self.energy_kwh = 0.0
        self.power_w = appliance.standby_w
        self.cycles = 0
        self.idle_reason: str | None = None
        # Room sensor minus the unit's own thermostat, learned while it cycles
        # (e.g. -1.0 when the room reads 17 °C with the unit holding 18 °C).
        self.thermostat_offset: float | None = None
        self.manual_since: float | None = None
        self.last_event: str | None = None
        self._wrong_side_since: float | None = None
        # Fan groups: how many fans are running (estimated, or set manually).
        self.units_on = 0
        self.units_on_members: list[str] = []
        self.manual_units: int | None = None
        self.active_since: float | None = None
        self.idle_since: float | None = None
        # Temperatures where the unit stopped/started driving the room, per mode.
        self.turn_points: dict[str, dict[str, list[float]]] = {}
        self.calibrations: list[dict[str, Any]] = []
        self.effectiveness: dict[str, EffectivenessTracker] = {}
        if appliance.has_setpoint:
            for mode in (MODE_HEAT, MODE_COOL):
                if mode in appliance.modes:
                    self.effectiveness[mode] = EffectivenessTracker()
        self._history: list[tuple[float, str]] = []
        self._last_ts: float | None = None

    # --- persistence -----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "manual_mode": self.manual_mode,
            "manual_setpoint": self.manual_setpoint,
            "manual_since": self.manual_since,
            "manual_units": self.manual_units,
            "thermostat_offset": self.thermostat_offset,
            "energy_kwh": self.energy_kwh,
            "cycles": self.cycles,
            "turn_points": self.turn_points,
            "calibrations": self.calibrations,
            "effectiveness": {m: t.as_dict() for m, t in self.effectiveness.items()},
        }

    def load(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        mode = data.get("manual_mode", MANUAL_AUTO)
        valid = (MANUAL_AUTO, MODE_OFF, *self.appliance.modes)
        self.manual_mode = mode if mode in valid else MANUAL_AUTO
        self.manual_setpoint = data.get("manual_setpoint")
        self.manual_since = data.get("manual_since")
        self.manual_units = data.get("manual_units")
        self.thermostat_offset = data.get("thermostat_offset")
        self.energy_kwh = float(data.get("energy_kwh", 0.0))
        self.cycles = int(data.get("cycles", 0))
        self.turn_points = {
            m: {k: [float(v) for v in vals] for k, vals in pts.items()}
            for m, pts in (data.get("turn_points") or {}).items()
        }
        self.calibrations = list(data.get("calibrations", []))
        for mode, tracker_data in (data.get("effectiveness") or {}).items():
            if mode in self.effectiveness:
                self.effectiveness[mode].load(tracker_data)

    def set_manual(self, now: float, mode: str | None = None, setpoint: float | None = None) -> None:
        """Apply a manual mode and/or setpoint; restarts the timeout clock."""
        if mode is not None:
            self.manual_mode = mode
        if setpoint is not None:
            self.manual_setpoint = self.appliance.clamp_setpoint(setpoint)
        self.manual_since = (
            now if self.manual_mode != MANUAL_AUTO or self.manual_setpoint is not None else None
        )
        self._wrong_side_since = None

    @property
    def manual_expires_at(self) -> float | None:
        if self.manual_since is None or self.appliance.manual_timeout_min <= 0:
            return None
        return self.manual_since + self.appliance.manual_timeout_min * 60

    def _revert_to_auto(self, event: str) -> None:
        self.manual_mode = MANUAL_AUTO
        self.manual_setpoint = None
        self.manual_units = None
        self.manual_since = None
        self._wrong_side_since = None
        self.last_event = event

    def reset_learning(self) -> None:
        self.turn_points = {}
        self.calibrations = []
        self.cycles = 0
        for mode in list(self.effectiveness):
            self.effectiveness[mode] = EffectivenessTracker()

    # --- setpoint --------------------------------------------------------

    def detected_setpoint(self, mode: str | None = None) -> float | None:
        """Setpoint inferred from cycling turn points (midpoint of recent highs and lows)."""
        mode = mode or (self.mode if self.mode in (MODE_HEAT, MODE_COOL) else None)
        modes = [mode] if mode else list(self.turn_points)
        highs: list[float] = []
        lows: list[float] = []
        for m in modes:
            highs += self.turn_points.get(m, {}).get("high", [])
            lows += self.turn_points.get(m, {}).get("low", [])
        if highs and lows:
            value = (statistics.median(highs) + statistics.median(lows)) / 2
        elif highs or lows:
            value = statistics.median(highs or lows)
        else:
            return None
        return round(self.appliance.clamp_setpoint(value), 2)

    def calibrated_setpoint(self, mode: str | None = None) -> float | None:
        """Recency-weighted mean of calibrated setpoints (optionally for one mode)."""
        points = [
            c
            for c in self.calibrations
            if c.get("setpoint") is not None and (mode is None or c.get("mode") in (None, mode))
        ]
        if not points:
            return None
        weights = [i + 1 for i in range(len(points))]  # newer calibrations count more
        value = sum(w * float(c["setpoint"]) for w, c in zip(weights, points)) / sum(weights)
        return round(self.appliance.clamp_setpoint(value), 2)

    @property
    def setpoint(self) -> float | None:
        """Best setpoint: manual, else calibrations blended with detection history."""
        if self.manual_setpoint is not None:
            return self.manual_setpoint
        mode = self.mode if self.mode in (MODE_HEAT, MODE_COOL) else None
        detected = self.detected_setpoint(mode)
        calibrated = self.calibrated_setpoint(mode)
        if detected is None or calibrated is None:
            return calibrated if detected is None else detected
        n_turns = sum(
            len(v)
            for m in ([mode] if mode else self.turn_points)
            for v in self.turn_points.get(m, {}).values()
        )
        n_cal = sum(1 for c in self.calibrations if c.get("setpoint") is not None)
        w_det = min(n_turns, 20) * 0.5
        w_cal = 2.0 * n_cal
        return round((w_det * detected + w_cal * calibrated) / (w_det + w_cal), 2)

    def _record_turn(self, kind: str, t_in: float) -> None:
        if self.mode not in (MODE_HEAT, MODE_COOL):
            return
        points = self.turn_points.setdefault(self.mode, {}).setdefault(kind, [])
        points.append(round(t_in, 2))
        del points[:-_TURN_POINTS_KEPT]

    # --- calibration -----------------------------------------------------

    def calibrate(
        self,
        ts: float,
        t_in: float | None,
        mode: str | None,
        status: str | None,
        setpoint: float | None,
        filter_status: str | None,
    ) -> None:
        """Store a user-supplied ground truth snapshot."""
        entry: dict[str, Any] = {"ts": ts, "t_in": t_in}
        if mode is not None:
            entry["mode"] = mode
        if status is not None:
            entry["status"] = status
        if setpoint is not None:
            entry["setpoint"] = self.appliance.clamp_setpoint(float(setpoint))
        if filter_status is not None:
            entry["filter"] = filter_status
            for tracker in self.effectiveness.values():
                tracker.mark_filter(ts, filter_status)
        self.calibrations.append(entry)
        del self.calibrations[:-_CALIBRATIONS_KEPT]
        # A calibrated mode+status is the truth right now.
        if mode is not None and mode in (MODE_OFF, *self.appliance.modes):
            self.mode = mode
            if mode == MODE_OFF:
                self.status = STATUS_OFF
            elif status in STATUSES:
                self.status = status
            elif self.status == STATUS_OFF:
                self.status = STATUS_ACTIVE

    # --- effectiveness ---------------------------------------------------

    @property
    def rating(self) -> str | None:
        """Worst current rating across modes."""
        ratings = [t.rating() for t in self.effectiveness.values()]
        ratings = [r for r in ratings if r is not None]
        if not ratings:
            return None
        return max(ratings, key=rating_rank)

    def _sample_effectiveness(self, now: float, f: Features | None, warmup_s: float) -> None:
        tracker = self.effectiveness.get(self.mode)
        if (
            tracker is None
            or f is None
            or f.t_out is None
            or f.is_open
            or self.status != STATUS_ACTIVE
            or self.active_since is None
            or now - self.active_since < warmup_s
        ):
            return
        if self.mode == MODE_HEAT:
            lift, perf = f.t_in - f.t_out, f.residual
        else:
            lift, perf = f.t_out - f.t_in, -f.residual
        if perf > 0:
            tracker.add(now, lift, perf)

    # --- update ----------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self.status != STATUS_OFF

    @property
    def duty_cycle(self) -> float | None:
        """Share of on-time spent active over the last two hours."""
        on = [s for _, s in self._history if s != STATUS_OFF]
        if not on:
            return None
        return sum(1 for s in on if s == STATUS_ACTIVE) / len(on)

    def update(
        self,
        now: float,
        features: Features | None,
        label: str | None,
        confidence: float,
        min_confidence: float,
        hold_band: float,
        other_active: bool,
        max_idle_s: float = 3600.0,
        effectiveness_warmup_s: float = 600.0,
        fan_estimate: dict[str, Any] | None = None,
    ) -> None:
        """Advance the state machine, integrate energy and sample effectiveness."""
        self._integrate(now)
        prev_status = self.status
        cycling = self._cycling(features)
        expires = self.manual_expires_at
        if expires is not None and now >= expires:
            self._revert_to_auto("manual_timeout")
        if self.manual_mode != MANUAL_AUTO:
            self._update_manual(features, hold_band)
        else:
            self._update_auto(
                now, features, label, confidence, min_confidence, hold_band, other_active, max_idle_s
            )
        if self.manual_mode in (MODE_HEAT, MODE_COOL) and not cycling:
            self._detect_switched_off(now, features)
        if cycling and self.mode in (MODE_COOL, MODE_DRY) and self.status != STATUS_OFF:
            # Evidence beats assumption: follow the compressor seen in humidity.
            self.status = STATUS_ACTIVE if features.compressor_on else STATUS_IDLE  # type: ignore[union-attr]
            self._learn_offset(features)  # type: ignore[arg-type]

        if self.status == STATUS_ACTIVE and prev_status != STATUS_ACTIVE:
            self.active_since = now
            if prev_status == STATUS_IDLE:
                self.cycles += 1
        if self.status == STATUS_IDLE and prev_status != STATUS_IDLE:
            self.idle_since = now
        if self.status == STATUS_OFF:
            self.active_since = self.idle_since = None

        self._sample_effectiveness(now, features, effectiveness_warmup_s)
        self._history.append((now, self.status))
        cutoff = now - _DUTY_WINDOW_S
        while self._history and self._history[0][0] < cutoff:
            self._history.pop(0)
        self._update_units(fan_estimate)
        if self.status == STATUS_IDLE:
            self.idle_reason = IDLE_CYCLING if cycling else self._idle_reason(features)
        else:
            self.idle_reason = None
        if self.status == STATUS_OFF:
            self.power_w = self.appliance.standby_w
        else:
            self.power_w = self.appliance.power_for(
                self.mode,
                idle=self.status == STATUS_IDLE,
                units_on=self.units_on,
                satisfied=self.idle_reason in (IDLE_SATISFIED, IDLE_CYCLING),
            )

    def _detect_switched_off(self, now: float, f: Features | None) -> None:
        """Manual heat/cool but the room drifts the wrong way past the setpoint: it was turned off."""
        sp = self.setpoint
        limit = self.appliance.off_detect_min * 60
        if f is None or sp is None or limit <= 0:
            return
        sp += self.thermostat_offset or 0.0
        band = 1.0
        wrong = (self.mode == MODE_COOL and f.t_in > sp + band and f.r_t >= 0) or (
            self.mode == MODE_HEAT and f.t_in < sp - band and f.r_t <= 0
        )
        if not wrong:
            self._wrong_side_since = None
            return
        if self._wrong_side_since is None:
            self._wrong_side_since = now
        elif now - self._wrong_side_since >= limit:
            self._revert_to_auto("switched_off_detected")
            self.status, self.mode = STATUS_OFF, MODE_OFF

    @staticmethod
    def _cycling(f: Features | None) -> bool:
        return f is not None and f.cycle_strength >= CYCLE_DETECTED and f.compressor_on is not None

    def _learn_offset(self, f: Features) -> None:
        sp = self.manual_setpoint if self.manual_setpoint is not None else self.calibrated_setpoint(self.mode)
        if sp is None:
            return
        diff = f.t_in - sp
        if self.thermostat_offset is None:
            self.thermostat_offset = round(diff, 2)
        else:
            self.thermostat_offset = round(
                self.thermostat_offset + _OFFSET_EMA * (diff - self.thermostat_offset), 3
            )

    def _idle_reason(self, f: Features | None) -> str:
        """Why an idle unit is idle: holding its setpoint, or already past it."""
        sp = self.setpoint
        if f is None or sp is None or self.mode not in (MODE_HEAT, MODE_COOL):
            return IDLE_HOLDING
        sp += self.thermostat_offset or 0.0  # judge in room-sensor terms
        # Positive = room is on the "done" side of the setpoint.
        past = (f.t_in - sp) if self.mode == MODE_HEAT else (sp - f.t_in)
        return IDLE_SATISFIED if past > _SATISFIED_MARGIN else IDLE_HOLDING

    def _update_units(self, fan_estimate: dict[str, Any] | None) -> None:
        """How many units are running: 1 for single appliances, estimated for fan groups."""
        if self.status == STATUS_OFF:
            self.units_on, self.units_on_members = 0, []
            return
        if not self.appliance.is_fan_group:
            self.units_on, self.units_on_members = 1, []
            return
        if self.manual_mode != MANUAL_AUTO and self.manual_units is not None:
            self.units_on = max(1, min(self.appliance.count, self.manual_units))
            self.units_on_members = []
            return
        count = (fan_estimate or {}).get("count")
        # The group was judged to be running, so at least one fan is on.
        self.units_on = max(1, min(self.appliance.count, count or 1))
        self.units_on_members = list((fan_estimate or {}).get("members") or [])

    def _update_manual(self, f: Features | None, hold_band: float) -> None:
        """User told us the mode: simulate thermostat cycling around the setpoint."""
        if self.manual_mode == MODE_OFF:
            self.status, self.mode = STATUS_OFF, MODE_OFF
            return
        self.mode = self.manual_mode
        sp = self.setpoint
        if self.mode not in (MODE_HEAT, MODE_COOL) or sp is None:
            self.status = STATUS_ACTIVE
            return
        if f is None:
            # No readings yet: on, but don't claim it's driving the room.
            if self.status == STATUS_OFF:
                self.status = STATUS_IDLE
            return
        band = min(_MANUAL_BAND, hold_band / 2)
        sign = 1 if self.mode == MODE_HEAT else -1
        # Distance still to go towards the setpoint (positive = not there yet).
        gap = sign * (sp - f.t_in)
        if self.status != STATUS_IDLE and gap <= band:
            self.status = STATUS_IDLE
        elif self.status == STATUS_IDLE and gap > band * 2:
            self.status = STATUS_ACTIVE
        elif self.status == STATUS_OFF:
            self.status = STATUS_ACTIVE if gap > band else STATUS_IDLE

    def _update_auto(
        self,
        now: float,
        f: Features | None,
        label: str | None,
        confidence: float,
        min_confidence: float,
        hold_band: float,
        other_active: bool,
        max_idle_s: float,
    ) -> None:
        claimed_mode: str | None = None
        if label is not None and confidence >= min_confidence:
            cause, appliance_id = split_label(label)
            if appliance_id == self.appliance.id:
                claimed_mode = self.appliance.mode_for_cause(cause)

        if claimed_mode is not None:
            if self.status == STATUS_IDLE and claimed_mode == self.mode and f is not None:
                # Resting -> driving again: the other turning point of the cycle.
                self._record_turn("high" if self.mode == MODE_COOL else "low", f.t_in)
            self.status, self.mode = STATUS_ACTIVE, claimed_mode
            return

        can_rest = (
            self.appliance.has_setpoint
            and self.mode in (MODE_HEAT, MODE_COOL)
            and f is not None
            and not f.is_open
            and not other_active
        )

        if self.status == STATUS_ACTIVE and can_rest:
            # Stopped driving the temperature: reached the setpoint for now.
            self._record_turn("low" if self.mode == MODE_COOL else "high", f.t_in)  # type: ignore[union-attr]
            self.status = STATUS_IDLE
            return

        if self.status == STATUS_IDLE and can_rest:
            reference = self.setpoint
            if reference is None:
                pts = self.turn_points.get(self.mode, {})
                last = (pts.get("low") if self.mode == MODE_COOL else pts.get("high")) or []
                reference = last[-1] if last else None
            drifted = reference is not None and (
                (self.mode == MODE_HEAT and f.t_in < reference - hold_band)  # type: ignore[union-attr]
                or (self.mode == MODE_COOL and f.t_in > reference + hold_band)  # type: ignore[union-attr]
            )
            # A fixed-speed unit that never restarts has been switched off; an
            # inverter can sit at its setpoint indefinitely.
            timed_out = (
                not self.appliance.inverter
                and self.idle_since is not None
                and now - self.idle_since > max_idle_s
            )
            if not drifted and not timed_out:
                return

        self.status, self.mode = STATUS_OFF, MODE_OFF

    def _integrate(self, now: float) -> None:
        if self._last_ts is not None and self.appliance.calculate_energy:
            gap = now - self._last_ts
            if 0 < gap <= _MAX_INTEGRATION_GAP_S:
                self.energy_kwh += self.power_w * gap / 3_600_000.0
        self._last_ts = now
