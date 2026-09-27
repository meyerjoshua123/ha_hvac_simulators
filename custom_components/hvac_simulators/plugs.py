"""Status of plugged-in appliances from their smart-plug power (pure Python).

HVAC Simulators only needs these appliances because they heat or humidify the
air, but their plug power also gives a simple status for free: ``active``
(running: fridge compressor on, dryer tumbling), ``idle`` (plugged in, drawing
a little between cycles) or ``off``. This deliberately stays basic; for full
washing-machine program detection use a dedicated integration such as WashData.
"""

from __future__ import annotations

from typing import Any

KIND_FRIDGE = "fridge"
KIND_WASHER = "washer"
KIND_DRYER = "dryer"
KIND_OVEN = "oven"
KIND_STOVE = "stove"
KIND_GENERAL = "general"
KINDS = (KIND_FRIDGE, KIND_WASHER, KIND_DRYER, KIND_OVEN, KIND_STOVE, KIND_GENERAL)
KIND_NAMES = {
    KIND_FRIDGE: "Fridge / freezer",
    KIND_WASHER: "Washing machine",
    KIND_DRYER: "Clothes dryer",
    KIND_OVEN: "Oven / microwave",
    KIND_STOVE: "Stove / cooktop",
    KIND_GENERAL: "General (TV, iron, steamer, hair dryer, soldering iron...)",
}
# (off below W, active above W)
DEFAULT_THRESHOLDS = {
    KIND_FRIDGE: (1.0, 30.0),
    KIND_WASHER: (1.0, 10.0),
    KIND_DRYER: (1.0, 50.0),
    KIND_OVEN: (2.0, 100.0),
    KIND_STOVE: (2.0, 100.0),
    KIND_GENERAL: (1.0, 10.0),
}
# Moisture each kind typically adds per kW (%RH/h in a typical room), used as the
# starting point before its real effect is learned.
MOISTURE_PRIOR_PER_KW = {
    KIND_DRYER: 3.0,
    KIND_STOVE: 2.0,
    KIND_WASHER: 1.0,
    KIND_GENERAL: 0.0,
    KIND_FRIDGE: 0.0,
    KIND_OVEN: 0.5,
}

STATUS_OFF = "off"
STATUS_IDLE = "idle"
STATUS_ACTIVE = "active"
PLUG_STATUSES = (STATUS_ACTIVE, STATUS_IDLE, STATUS_OFF)

# Gaps shorter than this between active spells belong to the same run (a
# washer pausing between wash and spin).
_RUN_GAP_S = 10 * 60
_DUTY_WINDOW_S = 6 * 3600


class PlugTracker:
    """Active/idle/off status, cycles and duty cycle from plug power."""

    def __init__(
        self, kind: str, off_below_w: float | None = None, active_above_w: float | None = None
    ) -> None:
        default_off, default_active = DEFAULT_THRESHOLDS.get(kind, DEFAULT_THRESHOLDS[KIND_GENERAL])
        self.kind = kind
        self.off_below_w = default_off if off_below_w is None else off_below_w
        self.active_above_w = default_active if active_above_w is None else active_above_w
        self.status = STATUS_OFF
        self.cycles = 0
        self.power_w: float | None = None
        self.active_since: float | None = None
        self.last_active_s: float | None = None
        self.run_started: float | None = None
        self.last_run_s: float | None = None
        self._last_active_end: float | None = None
        self._history: list[tuple[float, str]] = []

    def update(self, now: float, watts: float | None) -> None:
        if watts is None:
            return
        self.power_w = watts
        prev = self.status
        if watts >= self.active_above_w:
            status = STATUS_ACTIVE
        elif watts < self.off_below_w:
            status = STATUS_OFF
        else:
            status = STATUS_IDLE
        self.status = status
        if status == STATUS_ACTIVE and prev != STATUS_ACTIVE:
            self.cycles += 1
            self.active_since = now
            if self._last_active_end is None or now - self._last_active_end > _RUN_GAP_S:
                self.run_started = now
        elif status != STATUS_ACTIVE and prev == STATUS_ACTIVE:
            if self.active_since is not None:
                self.last_active_s = now - self.active_since
            self._last_active_end = now
            self.active_since = None
        if (
            status != STATUS_ACTIVE
            and self.run_started is not None
            and self._last_active_end is not None
            and now - self._last_active_end > _RUN_GAP_S
        ):
            self.last_run_s = self._last_active_end - self.run_started
            self.run_started = None
        self._history.append((now, status))
        cutoff = now - _DUTY_WINDOW_S
        while self._history and self._history[0][0] < cutoff:
            self._history.pop(0)

    @property
    def duty_cycle(self) -> float | None:
        """Share of the last 6 h spent active, weighted by time."""
        if len(self._history) < 2:
            return None
        active = total = 0.0
        for (t0, status), (t1, _) in zip(self._history, self._history[1:]):
            total += t1 - t0
            if status == STATUS_ACTIVE:
                active += t1 - t0
        return active / total if total > 0 else None

    @property
    def running(self) -> bool:
        """In a run (active, or paused briefly within one)."""
        return self.run_started is not None

    def as_dict(self) -> dict[str, Any]:
        return {"cycles": self.cycles, "last_active_s": self.last_active_s, "last_run_s": self.last_run_s}

    def load(self, data: dict[str, Any] | None) -> None:
        if data:
            self.cycles = int(data.get("cycles", 0))
            self.last_active_s = data.get("last_active_s")
            self.last_run_s = data.get("last_run_s")
