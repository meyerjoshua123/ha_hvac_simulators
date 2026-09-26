"""Occupancy estimation from CO2 (pure Python, no Home Assistant imports).

People breathe out CO2 at a rate that depends on what they are doing. With a
single well-mixed space the mass balance is::

    dC/dt = G - ach * (C - C_out)

where ``C`` is indoor CO2 (ppm), ``ach`` the air changes per hour and ``G`` the
generation rate in ppm/h. Solving for ``G`` and dividing by the ppm/h one
person adds at the current activity level gives the number of people.

Two things are unknown up front and are learned from calibrations:

* ``ach`` for each ventilation state (closed / fan / open), from calibrations
  where nobody is home (CO2 then simply decays towards outdoor levels).
* ppm/h per person, from calibrations with a known number of people and
  activity. With no calibration it falls back to room volume and textbook
  breathing rates.
"""

from __future__ import annotations

import statistics
from typing import Any

ACTIVITY_SLEEPING = "sleeping"
ACTIVITY_RESTING = "resting"
ACTIVITY_ACTIVE = "active"
ACTIVITY_EXERCISING = "exercising"
ACTIVITIES = (ACTIVITY_SLEEPING, ACTIVITY_RESTING, ACTIVITY_ACTIVE, ACTIVITY_EXERCISING)
# CO2 output relative to a seated adult at rest.
ACTIVITY_MULTIPLIER = {
    ACTIVITY_SLEEPING: 0.75,
    ACTIVITY_RESTING: 1.0,
    ACTIVITY_ACTIVE: 1.8,
    ACTIVITY_EXERCISING: 4.0,
}

VENT_CLOSED = "closed"
VENT_FAN = "fan"
VENT_OPEN = "open"
DEFAULT_ACH = {VENT_CLOSED: 0.5, VENT_FAN: 2.5, VENT_OPEN: 4.0}

# m³ of CO2 exhaled per hour by a resting adult (~0.3 L/min).
_RESTING_CO2_M3_PER_H = 0.018
_MIN_EXCESS_PPM = 100.0
_CALIBRATIONS_KEPT = 60


class OccupancyModel:
    """Learns ventilation rates and per-person CO2 output from calibrations."""

    def __init__(self, volume_m3: float = 100.0, co2_outdoor: float = 420.0) -> None:
        self.volume_m3 = max(volume_m3, 5.0)
        self.co2_outdoor = co2_outdoor
        self.calibrations: list[dict[str, Any]] = []

    # --- persistence -----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {"calibrations": self.calibrations}

    def load(self, data: dict[str, Any] | None) -> None:
        if data:
            self.calibrations = list(data.get("calibrations", []))

    def reset(self) -> None:
        self.calibrations = []

    # --- calibration -----------------------------------------------------

    def calibrate(
        self,
        ts: float,
        people: int,
        activity: str,
        vent_state: str,
        co2: float | None,
        r_co2: float | None,
    ) -> bool:
        """Record ground truth. Returns False when CO2 data is missing."""
        if co2 is None or r_co2 is None:
            return False
        self.calibrations.append(
            {
                "ts": ts,
                "people": int(people),
                "activity": activity if activity in ACTIVITIES else ACTIVITY_RESTING,
                "vent": vent_state,
                "co2": co2,
                "r_co2": r_co2,
            }
        )
        del self.calibrations[:-_CALIBRATIONS_KEPT]
        return True

    # --- learned parameters ----------------------------------------------

    def _observed_ach(self, vent_state: str) -> float | None:
        """Median decay rate from empty-home calibrations in ``vent_state``."""
        observed = [
            -c["r_co2"] / (c["co2"] - self.co2_outdoor)
            for c in self.calibrations
            if c["people"] == 0
            and c["vent"] == vent_state
            and c["co2"] - self.co2_outdoor > _MIN_EXCESS_PPM
            and c["r_co2"] < 0
        ]
        return statistics.median(observed) if observed else None

    def ach(self, vent_state: str) -> float:
        """Air changes per hour, learned from empty-home calibrations."""
        observed = self._observed_ach(vent_state)
        if observed is not None:
            return observed
        return DEFAULT_ACH.get(vent_state, DEFAULT_ACH[VENT_CLOSED])

    def generation(self, co2: float, r_co2: float, vent_state: str) -> float:
        """CO2 generation rate G (ppm/h) from the mass balance."""
        return r_co2 + self.ach(vent_state) * (co2 - self.co2_outdoor)

    def ppm_per_person(self, activity: str) -> float:
        """ppm/h one person adds at ``activity``."""
        mult = ACTIVITY_MULTIPLIER.get(activity, 1.0)
        own: list[float] = []
        scaled: list[float] = []
        for c in self.calibrations:
            if c["people"] <= 0:
                continue
            per_person = self.generation(c["co2"], c["r_co2"], c["vent"]) / c["people"]
            if per_person <= 0:
                continue
            if c["activity"] == activity:
                own.append(per_person)
            scaled.append(per_person / ACTIVITY_MULTIPLIER.get(c["activity"], 1.0) * mult)
        if len(own) >= 2:
            return statistics.median(own)
        if scaled:
            return statistics.median(scaled)
        return _RESTING_CO2_M3_PER_H * mult / self.volume_m3 * 1e6

    @property
    def calibration_count(self) -> int:
        return len(self.calibrations)

    # --- estimate --------------------------------------------------------

    def estimate(
        self, co2: float | None, r_co2: float | None, vent_state: str, activity: str
    ) -> dict[str, Any] | None:
        """Estimated people, with the range across activity levels."""
        if co2 is None or r_co2 is None:
            return None
        gen = max(0.0, self.generation(co2, r_co2, vent_state))
        people = gen / self.ppm_per_person(activity)
        return {
            "people": people,
            "people_min": gen / self.ppm_per_person(ACTIVITY_EXERCISING),
            "people_max": gen / self.ppm_per_person(ACTIVITY_SLEEPING),
            "generation_ppm_h": gen,
            "ach": self.ach(vent_state),
            "activity": activity,
            "vent_state": vent_state,
            # Airflow through an open door/window is guesswork unless calibrated.
            "reliable": vent_state == VENT_CLOSED
            or any(c["people"] == 0 and c["vent"] == vent_state for c in self.calibrations),
        }
