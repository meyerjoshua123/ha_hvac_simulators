"""Rule-based inference engine for HVAC Simulators.

Pure Python with no Home Assistant imports, so it can be unit tested standalone.

The engine looks at how the indoor climate is *changing* (temperature, humidity
and air-quality trends), compares that with what physics alone would explain
(heat flowing in or out through the building shell, open doors/windows, sun),
and scores every possible cause. The learning layer (``learning.py``) then
blends these rule scores with what the user has taught it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# --- Causes ------------------------------------------------------------------

CAUSE_IDLE = "idle"
CAUSE_HEATING = "heating"
CAUSE_COOLING = "cooling"
CAUSE_DEHUMIDIFY = "dehumidify"
CAUSE_VENTILATION = "ventilation"
CAUSE_WINDOW_COOLING = "window_cooling"
CAUSE_WINDOW_WARMING = "window_warming"
CAUSE_HEAT_LEAK_IN = "heat_leak_in"
CAUSE_SOLAR_GAIN = "solar_gain"
CAUSE_HEAT_LOSS = "heat_loss"
CAUSE_AIR_FILTERING = "air_filtering"
CAUSE_WINDOW_AIRING = "window_airing"
CAUSE_HUMIDIFY = "humidify"
CAUSE_INTERNAL_GAINS = "internal_gains"

# Cycling strength (cycling.py) at which the pattern counts as detected.
CYCLE_DETECTED = 0.6

# Causes that need an appliance to be running.
ACTIVE_CAUSES = (
    CAUSE_HEATING,
    CAUSE_COOLING,
    CAUSE_DEHUMIDIFY,
    CAUSE_VENTILATION,
    CAUSE_AIR_FILTERING,
    CAUSE_HUMIDIFY,
)
# Causes explained by the building and the weather.
PASSIVE_CAUSES = (
    CAUSE_IDLE,
    CAUSE_WINDOW_COOLING,
    CAUSE_WINDOW_WARMING,
    CAUSE_WINDOW_AIRING,
    CAUSE_HEAT_LEAK_IN,
    CAUSE_SOLAR_GAIN,
    CAUSE_HEAT_LOSS,
    CAUSE_INTERNAL_GAINS,
)
ALL_CAUSES = ACTIVE_CAUSES + PASSIVE_CAUSES

CAUSE_NAMES = {
    CAUSE_IDLE: "Idle / stable",
    CAUSE_HEATING: "Heating",
    CAUSE_COOLING: "Cooling",
    CAUSE_DEHUMIDIFY: "Dehumidifying",
    CAUSE_VENTILATION: "Extractor fan / ventilation",
    CAUSE_WINDOW_COOLING: "Cooling from open door/window",
    CAUSE_WINDOW_WARMING: "Warming from open door/window",
    CAUSE_HEAT_LEAK_IN: "Heat leaking in from outside",
    CAUSE_SOLAR_GAIN: "Solar gain (sun)",
    CAUSE_HEAT_LOSS: "Heat loss to outside",
    CAUSE_AIR_FILTERING: "Air filtering (aircon/purifier filter)",
    CAUSE_WINDOW_AIRING: "Airing out (door/window open)",
    CAUSE_HUMIDIFY: "Humidifying",
    CAUSE_INTERNAL_GAINS: "Heat/moisture from appliances (fridge, dryer...)",
}


# --- Data --------------------------------------------------------------------


@dataclass
class Sample:
    """One reading of every source at a point in time (``ts`` in epoch seconds)."""

    ts: float
    t_in: float | None = None
    rh_in: float | None = None
    aq: float | None = None
    t_out: float | None = None
    rh_out: float | None = None
    co2: float | None = None
    is_open: bool = False
    # Measured power (W) of each heat source (fridge, dryer...) by id.
    gains: dict[str, float] = field(default_factory=dict)

    @property
    def ah_in(self) -> float | None:
        """Indoor absolute humidity (g/m³)."""
        return absolute_humidity(self.t_in, self.rh_in)


@dataclass
class Tuning:
    """User-adjustable thresholds. Rates are per hour, temperatures in °C."""

    active_rate: float = 0.4  # °C/h beyond passive that implies an appliance
    stable_rate: float = 0.15  # °C/h below which temperature counts as stable
    dehumid_rh_rate: float = -3.0  # %RH/h drop that implies dehumidifying
    aq_improve_rel: float = -0.05  # relative AQ change per hour meaning "improving"
    solar_threshold: float = 22.0  # outdoor °C above which warming is from the sun
    # Indoor this many °C above outdoor while warming with the sun up points to
    # solar gain: the warming can't be heat leaking in from a cooler outside.
    solar_margin: float = 2.0
    k_closed: float = 0.05  # passive exchange rate (1/h) with openings closed
    k_open: float = 0.3  # passive exchange rate (1/h) with an opening open
    aq_lower_is_better: bool = True
    co2_outdoor: float = 420.0  # ppm of outdoor air
    ach_natural: float = 0.5  # natural air changes per hour with everything closed
    vent_factor: float = 2.0  # CO2 decay this many times faster than natural = ventilation


@dataclass
class Features:
    """Derived trends and context used for scoring and learning."""

    r_t: float  # indoor temperature rate, °C/h
    r_h: float | None  # indoor humidity rate, %RH/h
    r_aq: float | None  # relative particulate/VOC change per hour; negative = improving
    t_in: float
    t_out: float | None
    rh_in: float | None
    is_open: bool
    sun_up: bool | None
    hour: int
    expected_passive: float  # °C/h the building shell alone would explain
    residual: float  # r_t - expected_passive
    co2: float | None = None  # ppm
    r_co2: float | None = None  # ppm/h
    ach_obs: float | None = None  # apparent air changes per hour from CO2 decay
    # r_t / r_h above are *compensated*: the learned effect of heat sources is removed.
    gain_t: float = 0.0  # °C/h attributed to heat sources
    gain_h: float = 0.0  # %RH/h attributed to heat sources
    # Absolute humidity (g/m³): indoor, outdoor, and indoor rate of change.
    ah_in: float | None = None
    ah_out: float | None = None
    r_ah: float | None = None
    # True when door/window state is known (real sensors, or a confident estimate).
    openings_known: bool = True
    # Compressor cycling seen in humidity (see cycling.py); 0 when none.
    cycle_strength: float = 0.0
    cycle_period_min: float | None = None
    compressor_on: bool | None = None

    @property
    def delta_out(self) -> float | None:
        """Outdoor minus indoor temperature."""
        if self.t_out is None:
            return None
        return self.t_out - self.t_in

    def as_dict(self) -> dict[str, object]:
        """Serializable view."""
        return {
            "r_t": round(self.r_t, 3),
            "r_h": None if self.r_h is None else round(self.r_h, 3),
            "r_aq": None if self.r_aq is None else round(self.r_aq, 4),
            "t_in": round(self.t_in, 2),
            "t_out": None if self.t_out is None else round(self.t_out, 2),
            "rh_in": None if self.rh_in is None else round(self.rh_in, 1),
            "is_open": self.is_open,
            "sun_up": self.sun_up,
            "hour": self.hour,
            "expected_passive": round(self.expected_passive, 3),
            "residual": round(self.residual, 3),
            "co2": None if self.co2 is None else round(self.co2, 1),
            "r_co2": None if self.r_co2 is None else round(self.r_co2, 2),
            "ach_obs": None if self.ach_obs is None else round(self.ach_obs, 3),
            "gain_t": round(self.gain_t, 3),
            "gain_h": round(self.gain_h, 3),
            "ah_in": None if self.ah_in is None else round(self.ah_in, 2),
            "ah_out": None if self.ah_out is None else round(self.ah_out, 2),
            "r_ah": None if self.r_ah is None else round(self.r_ah, 3),
            "openings_known": self.openings_known,
            "cycle_strength": self.cycle_strength,
            "cycle_period_min": self.cycle_period_min,
            "compressor_on": self.compressor_on,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Features:
        """Rebuild from ``as_dict`` output."""
        return cls(
            r_t=float(data["r_t"]),
            r_h=None if data.get("r_h") is None else float(data["r_h"]),
            r_aq=None if data.get("r_aq") is None else float(data["r_aq"]),
            t_in=float(data["t_in"]),
            t_out=None if data.get("t_out") is None else float(data["t_out"]),
            rh_in=None if data.get("rh_in") is None else float(data["rh_in"]),
            is_open=bool(data.get("is_open", False)),
            sun_up=data.get("sun_up"),  # type: ignore[arg-type]
            hour=int(data.get("hour", 12)),
            expected_passive=float(data.get("expected_passive", 0.0)),
            residual=float(data.get("residual", data["r_t"])),
            co2=_opt(data.get("co2")),
            r_co2=_opt(data.get("r_co2")),
            ach_obs=_opt(data.get("ach_obs")),
            gain_t=float(data.get("gain_t", 0.0)),
            gain_h=float(data.get("gain_h", 0.0)),
        )

    @property
    def r_t_raw(self) -> float:
        """Indoor temperature rate before removing heat-source effects."""
        return self.r_t + self.gain_t

    @property
    def r_h_raw(self) -> float | None:
        return None if self.r_h is None else self.r_h + self.gain_h


@dataclass
class RuleResult:
    """Normalized cause probabilities plus human-readable reasons."""

    probs: dict[str, float]
    reasons: dict[str, list[str]] = field(default_factory=dict)


def _opt(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


# --- Trend buffer ------------------------------------------------------------


class SampleBuffer:
    """Rolling window of samples with least-squares slopes."""

    def __init__(self, max_age_s: float) -> None:
        self.max_age_s = max_age_s
        self.samples: list[Sample] = []

    def add(self, sample: Sample) -> None:
        """Append a sample and drop anything older than the window."""
        self.samples.append(sample)
        cutoff = sample.ts - self.max_age_s
        while self.samples and self.samples[0].ts < cutoff:
            self.samples.pop(0)

    def latest(self, attr: str) -> float | None:
        """Most recent non-empty value of ``attr``."""
        for sample in reversed(self.samples):
            value = getattr(sample, attr)
            if value is not None:
                return value
        return None

    def mean(self, attr: str, window_s: float, now: float) -> float | None:
        """Mean of ``attr`` over the window."""
        values = [
            getattr(s, attr) for s in self.samples if s.ts >= now - window_s and getattr(s, attr) is not None
        ]
        if not values:
            return None
        return sum(values) / len(values)

    def mean_gains(self, window_s: float, now: float) -> dict[str, float]:
        """Average measured power (W) of each heat source over the window."""
        recent = [s for s in self.samples if s.ts >= now - window_s]
        totals: dict[str, float] = {}
        for sample in recent:
            for key, watts in sample.gains.items():
                totals[key] = totals.get(key, 0.0) + watts
        return {k: v / len(recent) for k, v in totals.items()} if recent else {}

    def slope_per_hour(self, attr: str, window_s: float, now: float) -> float | None:
        """Least-squares slope of ``attr`` in units per hour, or None if too little data."""
        points = [
            (s.ts, getattr(s, attr))
            for s in self.samples
            if s.ts >= now - window_s and getattr(s, attr) is not None
        ]
        if len(points) < 3:
            return None
        span = points[-1][0] - points[0][0]
        if span < min(window_s * 0.3, 300):
            return None
        n = len(points)
        mean_x = sum(p[0] for p in points) / n
        mean_y = sum(p[1] for p in points) / n
        sxx = sum((p[0] - mean_x) ** 2 for p in points)
        if sxx == 0:
            return None
        sxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
        return sxy / sxx * 3600.0


# --- Helpers -----------------------------------------------------------------


def ramp(x: float, lo: float, hi: float) -> float:
    """0 at ``lo``, 1 at ``hi``, linear in between and clamped. ``lo`` may exceed ``hi``."""
    if lo == hi:
        return 1.0 if x >= hi else 0.0
    t = (x - lo) / (hi - lo)
    return max(0.0, min(1.0, t))


def absolute_humidity(t_c: float | None, rh: float | None) -> float | None:
    """Water vapour content in g/m³ (Magnus formula)."""
    if t_c is None or rh is None:
        return None
    return 6.112 * math.exp(17.67 * t_c / (t_c + 243.5)) * rh * 2.1674 / (273.15 + t_c)


def expected_passive_rate(t_in: float, t_out: float | None, is_open: bool, tuning: Tuning) -> float:
    """Newton's-law estimate of the temperature change the shell alone explains."""
    if t_out is None:
        return 0.0
    k = tuning.k_open if is_open else tuning.k_closed
    return k * (t_out - t_in)


def compute_features(
    buffer: SampleBuffer,
    now: float,
    window_s: float,
    tuning: Tuning,
    sun_up: bool | None,
    hour: int,
    gains: tuple[float, float] = (0.0, 0.0),
    cycle: dict | None = None,
    openings_known: bool = True,
    is_open: bool | None = None,
) -> Features | None:
    """Turn the sample buffer into a feature vector. None when there is not enough data.

    ``gains`` is the (°C/h, %RH/h) that heat sources are predicted to cause;
    it is subtracted so the rules only see what's left to explain.
    """
    gain_t, gain_h = gains
    r_t = buffer.slope_per_hour("t_in", window_s, now)
    t_in = buffer.latest("t_in")
    if r_t is None or t_in is None:
        return None
    r_t -= gain_t
    r_h = buffer.slope_per_hour("rh_in", window_s, now)
    if r_h is not None:
        r_h -= gain_h
    r_aq: float | None = None
    aq_slope = buffer.slope_per_hour("aq", window_s, now)
    aq_mean = buffer.mean("aq", window_s, now)
    if aq_slope is not None and aq_mean is not None:
        r_aq = aq_slope / max(abs(aq_mean), 1.0)
        if not tuning.aq_lower_is_better:
            r_aq = -r_aq
    # Average outdoor temperature over the window smooths weather-station jitter.
    t_out = buffer.mean("t_out", window_s, now)
    co2 = buffer.latest("co2")
    r_co2 = buffer.slope_per_hour("co2", window_s, now)
    ach_obs: float | None = None
    if co2 is not None and r_co2 is not None and co2 - tuning.co2_outdoor > 80:
        # Exponential decay towards outdoor air: dC/dt = -ach * (C - C_out).
        ach_obs = max(0.0, -r_co2 / (co2 - tuning.co2_outdoor))
    latest = buffer.samples[-1] if buffer.samples else None
    if is_open is None:
        is_open = bool(latest.is_open) if latest else False
    rh_out = buffer.mean("rh_out", window_s, now)
    ah_in = absolute_humidity(t_in, buffer.latest("rh_in"))
    ah_out = absolute_humidity(t_out, rh_out)
    r_ah = buffer.slope_per_hour("ah_in", window_s, now)
    expected = expected_passive_rate(t_in, t_out, is_open, tuning)
    return Features(
        r_t=r_t,
        r_h=r_h,
        r_aq=r_aq,
        t_in=t_in,
        t_out=t_out,
        rh_in=buffer.latest("rh_in"),
        is_open=is_open,
        sun_up=sun_up,
        hour=hour,
        expected_passive=expected,
        residual=r_t - expected,
        co2=co2,
        r_co2=r_co2,
        ach_obs=ach_obs,
        gain_t=gain_t,
        gain_h=gain_h if r_h is not None else 0.0,
        ah_in=ah_in,
        ah_out=ah_out,
        r_ah=r_ah,
        openings_known=openings_known,
        cycle_strength=float((cycle or {}).get("strength") or 0.0),
        cycle_period_min=(cycle or {}).get("period_min"),
        compressor_on=(cycle or {}).get("compressor_on"),
    )


# --- Rules -------------------------------------------------------------------


def score_causes(f: Features, tuning: Tuning) -> RuleResult:
    """Score every cause from the current features and normalize to probabilities."""
    at = tuning.active_rate
    st = tuning.stable_rate
    rt = f.r_t
    rh = f.r_h
    resid = f.residual
    closed = not f.is_open
    d_out = f.delta_out
    scores: dict[str, float] = {c: 0.0 for c in ALL_CAUSES}
    reasons: dict[str, list[str]] = {c: [] for c in ALL_CAUSES}

    trend = f"Indoor temp {rt:+.2f} °C/h (passive would explain {f.expected_passive:+.2f})"
    # How likely warming is from the sun: sun not known to be down, and either
    # warm outside (at/above the solar threshold) or indoor already well above
    # outdoor so the warming can't be leaking in from outside.
    solar_likelihood = 0.0
    # Sun state unknown (no sun entity): treat night hours as sun down.
    sun_up = f.sun_up if f.sun_up is not None else (None if 7 <= f.hour < 19 else False)
    if f.t_out is not None and sun_up is not False:
        by_outdoor = ramp(f.t_out, tuning.solar_threshold - 3.0, tuning.solar_threshold)
        # Indoor well above outdoor counts on mild days; on cold days a heater
        # is the likelier reason for warming, so this evidence fades out.
        mild = ramp(f.t_out, tuning.solar_threshold - 10.0, tuning.solar_threshold - 4.0)
        by_margin = ramp(f.t_in - f.t_out, 0.0, tuning.solar_margin) * mild
        solar_likelihood = max(by_outdoor, by_margin) * (1.0 if sun_up else 0.7)
    aq_improving = 0.0
    if f.r_aq is not None:
        aq_improving = ramp(f.r_aq, tuning.aq_improve_rel * 0.5, tuning.aq_improve_rel * 1.5)
    # CO2 only falls faster than natural infiltration through real air exchange
    # (open door/window or extractor fan); aircon filters do not remove CO2.
    co2_flushing: float | None = None
    if f.ach_obs is not None:
        fast = tuning.ach_natural * tuning.vent_factor
        co2_flushing = ramp(f.ach_obs, fast * 0.7, fast * 1.3)

    # Humidity dropping (or rising) hard.
    rh_drop = 0.0
    rh_rise = 0.0
    if rh is not None:
        rh_drop = ramp(rh, tuning.dehumid_rh_rate * 0.5, tuning.dehumid_rh_rate * 1.5)
        rh_rise = ramp(rh, -tuning.dehumid_rh_rate * 0.5, -tuning.dehumid_rh_rate * 1.5)

    # Idle: nothing much is changing.
    idle = ramp(abs(rt), st * 2, st * 0.5)
    if rh is not None:
        idle *= ramp(abs(rh), 3.0, 1.0)
    idle *= 1.0 - rh_rise
    if f.r_aq is not None:
        idle *= 1.0 - aq_improving
    if co2_flushing is not None:
        idle *= 1.0 - co2_flushing
    scores[CAUSE_IDLE] = idle
    if idle > 0.3:
        reasons[CAUSE_IDLE].append("Temperature and humidity are stable")

    # Heating: warming faster than the building shell explains.
    if rt > 0:
        heat = ramp(resid, at * 0.5, at * 1.5)
        if d_out is not None and d_out > 0:
            heat *= 0.7
            if heat > 0:
                reasons[CAUSE_HEATING].append("Outside is warmer, so some warming is passive")
        if closed:
            heat *= 1.0 - 0.5 * solar_likelihood
        if f.is_open and d_out is not None and d_out > 0:
            heat *= 0.5
        scores[CAUSE_HEATING] = heat
        if heat > 0.2:
            reasons[CAUSE_HEATING].insert(0, trend)

    # Cooling (aircon): falling fast, humidity lagging behind.
    if rt < 0:
        cool = ramp(-resid, at * 0.5, at * 1.5)
        if rh is None:
            lag = 0.6
        else:
            lag = ramp(rh, tuning.dehumid_rh_rate, tuning.dehumid_rh_rate * 0.3)
        cool *= 0.3 + 0.7 * lag
        if f.is_open and d_out is not None and d_out < 0:
            cool *= 0.4
        scores[CAUSE_COOLING] = cool
        if cool > 0.2:
            reasons[CAUSE_COOLING].append(trend)
            if rh is not None and lag > 0.5:
                reasons[CAUSE_COOLING].append(f"Humidity lagging ({rh:+.1f} %/h) while temperature drops")

    # Dehumidifying: humidity falling hard, temperature only drifting a little.
    if rh is not None:
        temp_fit = 1.0 if -1.5 * at <= rt <= 0.5 else 0.3
        dehum = rh_drop * temp_fit
        if f.is_open:
            dehum *= 0.5
        if aq_improving > 0.5:
            dehum *= 0.7
        scores[CAUSE_DEHUMIDIFY] = dehum
        if dehum > 0.2:
            reasons[CAUSE_DEHUMIDIFY].append(
                f"Humidity falling {rh:+.1f} %/h with temperature {rt:+.2f} °C/h"
            )

    # Humidifying: humidity rising hard with openings closed and not explained by appliances.
    if rh is not None and closed:
        humid = rh_rise * (1.0 if abs(rt) <= at * 1.5 else 0.4)
        scores[CAUSE_HUMIDIFY] = humid
        if humid > 0.2:
            reasons[CAUSE_HUMIDIFY].append(f"Humidity rising {rh:+.1f} %/h with doors and windows closed")

    # Heat sources (fridge, dryer...) explain most of what changed.
    gain_size = max(abs(f.gain_t) / at, abs(f.gain_h) / abs(tuning.dehumid_rh_rate))
    if gain_size > 0:
        leftover = max(abs(rt) / at, abs(rh or 0.0) / abs(tuning.dehumid_rh_rate))
        internal = ramp(gain_size, 0.3, 1.0) * ramp(leftover, 1.0, 0.3)
        scores[CAUSE_INTERNAL_GAINS] = internal
        # Stable only because we removed the appliances' effect: that's not "idle".
        scores[CAUSE_IDLE] *= 1.0 - internal
        if internal > 0.2:
            reasons[CAUSE_INTERNAL_GAINS].append(
                f"Appliances with plugs explain {f.gain_t:+.2f} °C/h and {f.gain_h:+.1f} %/h"
            )
        if f.gain_t or f.gain_h:
            for cause in (CAUSE_HEATING, CAUSE_HUMIDIFY, CAUSE_DEHUMIDIFY, CAUSE_COOLING):
                if scores[cause] > 0.2:
                    reasons[cause].append(
                        f"After removing appliance heat/moisture ({f.gain_t:+.2f} °C/h, {f.gain_h:+.1f} %/h)"
                    )

    # Ventilation vs filtering vs airing out.
    temp_explained = ramp(abs(resid), at * 1.5, at * 0.5)
    if co2_flushing is not None:
        if closed and not f.openings_known:
            # No door/window information: a fan and an open window look the same.
            both = co2_flushing * temp_explained
            scores[CAUSE_VENTILATION] = 0.5 * both
            scores[CAUSE_WINDOW_AIRING] = 0.5 * both
            if both > 0.2:
                note = (
                    f"CO2 clearing at {f.ach_obs:.1f} air changes/h, but without door/window "
                    "sensors a fan can't be told from an open window"
                )
                reasons[CAUSE_VENTILATION].append(note)
                reasons[CAUSE_WINDOW_AIRING].append(note)
        elif closed:
            vent = co2_flushing * temp_explained
            scores[CAUSE_VENTILATION] = vent
            if vent > 0.2:
                reasons[CAUSE_VENTILATION].append(
                    f"CO2 clearing at {f.ach_obs:.1f} air changes/h with doors and windows closed "
                    f"(natural is about {tuning.ach_natural:.1f})"
                )
        else:
            airing = co2_flushing * temp_explained
            scores[CAUSE_WINDOW_AIRING] = airing
            if airing > 0.2:
                reasons[CAUSE_WINDOW_AIRING].append(
                    f"CO2 clearing at {f.ach_obs:.1f} air changes/h through an open door/window"
                )
        if f.r_aq is not None:
            # Particulates/VOC improving while CO2 is not being flushed = a filter.
            filt = aq_improving * (1.0 - co2_flushing)
            scores[CAUSE_AIR_FILTERING] = filt
            if filt > 0.2:
                reasons[CAUSE_AIR_FILTERING].append(
                    f"Air quality improving {-f.r_aq:.0%}/h but CO2 is not falling faster than normal"
                )
    elif f.r_aq is not None:
        # No CO2 sensor: an improving AQ reading could be either a fan or a filter.
        scores[CAUSE_VENTILATION] = 0.6 * aq_improving * temp_explained * (1.0 if closed else 0.3)
        scores[CAUSE_AIR_FILTERING] = 0.5 * aq_improving
        if aq_improving > 0.3:
            reasons[CAUSE_VENTILATION].append(
                f"Air quality improving {-f.r_aq:.0%}/h (add a CO2 sensor to tell fans from filters)"
            )
            reasons[CAUSE_AIR_FILTERING].extend(reasons[CAUSE_VENTILATION])
    elif closed and rh is not None:
        # No AQ sensor: a bathroom-style extractor mostly shows as falling humidity.
        scores[CAUSE_VENTILATION] = 0.35 * rh_drop * temp_explained

    # Open door/window.
    if f.is_open:
        if d_out is not None and d_out < -1 and rt < 0:
            scores[CAUSE_WINDOW_COOLING] = ramp(-rt, st * 0.5, st * 2)
            reasons[CAUSE_WINDOW_COOLING].append(f"Opening is open and outside is {-d_out:.1f} °C colder")
        elif d_out is not None and d_out > 1 and rt > 0:
            scores[CAUSE_WINDOW_WARMING] = ramp(rt, st * 0.5, st * 2)
            reasons[CAUSE_WINDOW_WARMING].append(f"Opening is open and outside is {d_out:.1f} °C warmer")
        elif d_out is None and rt < 0:
            scores[CAUSE_WINDOW_COOLING] = 0.4 * ramp(-rt, st * 0.5, st * 2)

    # Passive drift with everything closed.
    explained = ramp(abs(resid), at, at * 0.3)
    if closed and d_out is not None:
        if rt > st * 0.5:
            warming = ramp(rt, st * 0.5, st * 1.5)
            if solar_likelihood > 0:
                # Sun through glass is moderate; very fast warming is more likely a heater.
                plausible = ramp(resid, at * 4.0, at * 1.5)
                scores[CAUSE_SOLAR_GAIN] = warming * solar_likelihood * max(plausible, 0.3)
                if scores[CAUSE_SOLAR_GAIN] > 0.2:
                    reasons[CAUSE_SOLAR_GAIN].append(
                        f"Warming with the sun up; outside {f.t_out:.1f} °C "
                        f"(solar threshold {tuning.solar_threshold:.1f} °C), indoor {-d_out:+.1f} °C vs outside"
                    )
            if d_out > 0.5:
                scores[CAUSE_HEAT_LEAK_IN] = warming * explained * (1.0 - solar_likelihood)
                reasons[CAUSE_HEAT_LEAK_IN].append(
                    f"Outside is {d_out:.1f} °C warmer; warming matches passive leakage"
                )
        elif rt < -st * 0.5 and d_out < -0.5:
            scores[CAUSE_HEAT_LOSS] = ramp(-rt, st * 0.5, st * 1.5) * explained
            reasons[CAUSE_HEAT_LOSS].append(
                f"Outside is {-d_out:.1f} °C colder; cooling matches passive heat loss"
            )

    # Regular compressor cycling in humidity: a cooling or dry-mode unit holding
    # the room, even when the building's thermal mass keeps temperature flat.
    if f.cycle_strength >= CYCLE_DETECTED:
        cyc = f.cycle_strength
        # The short swings inside each cycle are the compressor itself, not
        # separate heating/humidifying events or a stable room.
        for cause in (CAUSE_HEATING, CAUSE_HUMIDIFY, CAUSE_IDLE):
            scores[cause] *= 1.0 - cyc
        scores[CAUSE_COOLING] = max(scores[CAUSE_COOLING], cyc)
        scores[CAUSE_DEHUMIDIFY] = max(scores[CAUSE_DEHUMIDIFY], 0.4 * cyc)
        note = (
            f"Humidity cycling every ~{f.cycle_period_min} min (compressor on/off) while "
            "temperature is held; the building's stored heat can balance what the unit removes"
        )
        reasons[CAUSE_COOLING].append(note)
        reasons[CAUSE_DEHUMIDIFY].append(note)

    return RuleResult(probs=normalize(scores), reasons=reasons)


def normalize(scores: dict[str, float]) -> dict[str, float]:
    """Scale scores to sum to 1, falling back to idle when nothing scored."""
    total = sum(max(0.0, v) for v in scores.values())
    if total < 0.05:
        out = {k: 0.0 for k in scores}
        out[CAUSE_IDLE] = 1.0
        return out
    return {k: max(0.0, v) / total for k, v in scores.items()}


def entropy_confidence(probs: dict[str, float]) -> float:
    """Top probability, lightly penalized when the runner-up is close."""
    ordered = sorted(probs.values(), reverse=True)
    if not ordered:
        return 0.0
    top = ordered[0]
    second = ordered[1] if len(ordered) > 1 else 0.0
    margin = top - second
    return max(0.0, min(1.0, top * (0.6 + 0.4 * math.sqrt(max(margin, 0.0)))))
