"""Runtime manager: reads sensors, runs the engine, drives appliances, persists learning."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .appliances import (
    Appliance,
    available_labels,
    label_name,
    make_label,
    split_label,
)
from .const import (
    CONF_ACTIVE_RATE,
    CONF_AIR_QUALITY,
    CONF_APPLIANCES,
    CONF_AQ_IMPROVE_PCT,
    CONF_AQ_LOWER_IS_BETTER,
    CONF_CO2,
    CONF_CO2_OUTDOOR,
    CONF_DEHUMID_RATE,
    CONF_HOLD_BAND,
    CONF_INDOOR_HUMIDITY,
    CONF_INDOOR_TEMP,
    CONF_LEARN_FROM_MANUAL,
    CONF_MAX_IDLE_MINUTES,
    CONF_MAX_LEARNED_WEIGHT,
    CONF_MIN_CONFIDENCE,
    CONF_NOTIFY_RATING,
    CONF_NOTIFY_SERVICE,
    CONF_OPENINGS,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMP,
    CONF_SLEEP_END,
    CONF_SLEEP_START,
    CONF_SOLAR_THRESHOLD,
    CONF_STABLE_RATE,
    CONF_SUN_ENTITY,
    CONF_VOLUME,
    CONF_WINDOW_MINUTES,
    DEFAULT_ACTIVE_RATE,
    DEFAULT_AQ_IMPROVE_PCT,
    DEFAULT_CO2_OUTDOOR,
    DEFAULT_DEHUMID_RATE,
    DEFAULT_HOLD_BAND,
    DEFAULT_LEARN_FROM_MANUAL,
    DEFAULT_MAX_IDLE_MINUTES,
    DEFAULT_MAX_LEARNED_WEIGHT,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_NOTIFY_RATING,
    DEFAULT_SLEEP_END,
    DEFAULT_SLEEP_START,
    DEFAULT_SOLAR_THRESHOLD,
    DEFAULT_STABLE_RATE,
    DEFAULT_VOLUME,
    DEFAULT_WINDOW_MINUTES,
    DOMAIN,
    EFFECTIVENESS_WARMUP_FRACTION,
    EVENT_EFFECTIVENESS,
    FEEDBACK_CONFIDENCE,
    MANUAL_TEACH_INTERVAL_S,
    SIGNAL_UPDATE,
    STORAGE_VERSION,
    UPDATE_INTERVAL_S,
)
from .cycling import detect_cycling
from .effectiveness import RATING_NAMES, rating_rank
from .engine import (
    ACTIVE_CAUSES,
    CAUSE_VENTILATION,
    Features,
    Sample,
    SampleBuffer,
    Tuning,
    absolute_humidity,
    compute_features,
    score_causes,
)
from .fans import FanGroupEstimator
from .gains import GainsModel
from .learning import Learner, Prediction
from .occupancy import (
    ACTIVITY_RESTING,
    ACTIVITY_SLEEPING,
    VENT_CLOSED,
    VENT_FAN,
    VENT_OPEN,
    OccupancyModel,
)
from .simulator import MANUAL_AUTO, STATUS_ACTIVE, STATUS_OFF, ApplianceSimulator

_LOGGER = logging.getLogger(__name__)

_SAVE_DELAY_S = 30
_MIN_EVAL_GAP_S = 10
_OCCUPANCY_EMA = 0.3
CYCLE_HISTORY_S = 3 * 3600
ACTIVITY_AUTO = "auto"


class HvacSimulatorManager:
    """One monitored space (a room or the whole home) and its appliances."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        self.config: dict[str, Any] = {**entry.data, **entry.options}
        self.appliances: list[Appliance] = [
            Appliance.from_config(a) for a in self.config.get(CONF_APPLIANCES, [])
        ]
        # Heat sources are measured from their plugs; everything else is simulated.
        self.heat_sources: list[Appliance] = [a for a in self.appliances if a.is_heat_source]
        self.sims: dict[str, ApplianceSimulator] = {
            a.id: ApplianceSimulator(a) for a in self.appliances if not a.is_heat_source
        }
        self.gains = GainsModel()
        self.gains.load(None, [a.id for a in self.heat_sources])
        self.source_power: dict[str, float] = {}
        self.window_s = float(self._opt(CONF_WINDOW_MINUTES, DEFAULT_WINDOW_MINUTES)) * 60
        self.min_confidence = float(self._opt(CONF_MIN_CONFIDENCE, DEFAULT_MIN_CONFIDENCE))
        self.hold_band = float(self._opt(CONF_HOLD_BAND, DEFAULT_HOLD_BAND))
        self.learn_from_manual = bool(self._opt(CONF_LEARN_FROM_MANUAL, DEFAULT_LEARN_FROM_MANUAL))
        self.base_tuning = Tuning(
            active_rate=float(self._opt(CONF_ACTIVE_RATE, DEFAULT_ACTIVE_RATE)),
            stable_rate=float(self._opt(CONF_STABLE_RATE, DEFAULT_STABLE_RATE)),
            dehumid_rh_rate=float(self._opt(CONF_DEHUMID_RATE, DEFAULT_DEHUMID_RATE)),
            aq_improve_rel=-abs(float(self._opt(CONF_AQ_IMPROVE_PCT, DEFAULT_AQ_IMPROVE_PCT))) / 100.0,
            solar_threshold=float(self._opt(CONF_SOLAR_THRESHOLD, DEFAULT_SOLAR_THRESHOLD)),
            aq_lower_is_better=bool(self._opt(CONF_AQ_LOWER_IS_BETTER, True)),
            co2_outdoor=float(self._opt(CONF_CO2_OUTDOOR, DEFAULT_CO2_OUTDOOR)),
        )
        self.max_idle_s = float(self._opt(CONF_MAX_IDLE_MINUTES, DEFAULT_MAX_IDLE_MINUTES)) * 60
        self.sleep_start = int(self._opt(CONF_SLEEP_START, DEFAULT_SLEEP_START))
        self.sleep_end = int(self._opt(CONF_SLEEP_END, DEFAULT_SLEEP_END))
        self.notify_service: str | None = self.config.get(CONF_NOTIFY_SERVICE) or None
        self.notify_rating = str(self._opt(CONF_NOTIFY_RATING, DEFAULT_NOTIFY_RATING))
        volume = float(self._opt(CONF_VOLUME, DEFAULT_VOLUME))
        self.occupancy = OccupancyModel(volume, self.base_tuning.co2_outdoor)
        self.fan_groups: dict[str, FanGroupEstimator] = {
            a.id: FanGroupEstimator(a.members, a.airflow_m3h, volume)
            for a in self.appliances
            if a.is_fan_group
        }
        self.fan_estimates: dict[str, dict[str, Any]] = {}
        self.activity_override = ACTIVITY_AUTO
        self.occupancy_estimate: dict[str, Any] | None = None
        self._people_smoothed: float | None = None
        self.last_ratings: dict[str, str | None] = {}
        self.learner = Learner(float(self._opt(CONF_MAX_LEARNED_WEIGHT, DEFAULT_MAX_LEARNED_WEIGHT)))
        # Keep enough history for the compressor-cycling detector (3 h).
        self.buffer = SampleBuffer(max(self.window_s * 2, CYCLE_HISTORY_S))
        self.cycle: dict[str, Any] | None = None
        self.features: Features | None = None
        self.prediction: Prediction | None = None
        self.last_taught: str | None = None
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsubs: list[Callable[[], None]] = []
        self._last_eval = 0.0
        self._last_manual_teach: dict[str, float] = {}

    def _opt(self, key: str, default: Any) -> Any:
        value = self.config.get(key)
        return default if value in (None, "") else value

    # --- lifecycle -------------------------------------------------------

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self.learner.load(data.get("learner"))
        known = {a.id for a in self.appliances}
        for appliance_id in {split_label(s.label)[1] for s in self.learner.samples} - {None} - known:
            self.learner.forget_appliance(appliance_id)  # type: ignore[arg-type]
        for appliance_id, sim_data in data.get("appliances", {}).items():
            if appliance_id in self.sims:
                self.sims[appliance_id].load(sim_data)
        self.last_taught = data.get("last_taught")
        self.occupancy.load(data.get("occupancy"))
        self.activity_override = data.get("activity_override", ACTIVITY_AUTO)
        self.last_ratings = dict(data.get("last_ratings", {}))
        self.gains.load(data.get("gains"), [a.id for a in self.heat_sources])
        for group_id, group_data in (data.get("fan_groups") or {}).items():
            if group_id in self.fan_groups:
                self.fan_groups[group_id].load(group_data)

        watched = [e for e in self._source_entities() if e]
        if watched:
            self._unsubs.append(async_track_state_change_event(self.hass, watched, self._handle_state_change))
        self._unsubs.append(
            async_track_time_interval(self.hass, self._handle_interval, timedelta(seconds=UPDATE_INTERVAL_S))
        )
        self._evaluate()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "learner": self.learner.as_dict(),
            "appliances": {k: s.as_dict() for k, s in self.sims.items()},
            "last_taught": self.last_taught,
            "occupancy": self.occupancy.as_dict(),
            "activity_override": self.activity_override,
            "last_ratings": self.last_ratings,
            "gains": self.gains.as_dict(),
            "fan_groups": {k: g.as_dict() for k, g in self.fan_groups.items()},
        }

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY_S)

    def _source_entities(self) -> list[str]:
        entities = [
            self.config.get(CONF_INDOOR_TEMP),
            self.config.get(CONF_INDOOR_HUMIDITY),
            self.config.get(CONF_OUTDOOR_TEMP),
            self.config.get(CONF_OUTDOOR_HUMIDITY),
            self.config.get(CONF_AIR_QUALITY),
            self.config.get(CONF_CO2),
        ]
        entities.extend(self.config.get(CONF_OPENINGS) or [])
        entities.extend(a.power_entity for a in self.heat_sources)
        return [e for e in entities if e]

    # --- reading sensors -------------------------------------------------

    def _read_number(self, entity_id: str | None, attr: str | None = None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        if entity_id.startswith("weather."):
            raw = state.attributes.get(attr or "temperature")
        else:
            if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                return None
            raw = state.state
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _read_temperature(self, entity_id: str | None) -> float | None:
        value = self._read_number(entity_id, "temperature")
        if value is None or not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        unit = None
        if state is not None:
            unit = state.attributes.get("temperature_unit") or state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit == UnitOfTemperature.FAHRENHEIT:
            return TemperatureConverter.convert(
                value, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
            )
        if unit == UnitOfTemperature.KELVIN:
            return TemperatureConverter.convert(value, UnitOfTemperature.KELVIN, UnitOfTemperature.CELSIUS)
        return value

    def openings_open(self) -> list[str]:
        """Entity ids of doors/windows currently open."""
        return [
            e
            for e in self.config.get(CONF_OPENINGS) or []
            if (s := self.hass.states.get(e)) is not None and s.state == STATE_ON
        ]

    def _sun_up(self) -> bool | None:
        state = self.hass.states.get(self.config.get(CONF_SUN_ENTITY) or "sun.sun")
        if state is None:
            return None
        return state.state == "above_horizon"

    def _read_source_power(self) -> dict[str, float]:
        """Measured power (W) of each heat source, zero below its on-threshold."""
        powers: dict[str, float] = {}
        for source in self.heat_sources:
            watts = self._read_number(source.power_entity)
            if watts is None:
                continue
            state = self.hass.states.get(source.power_entity or "")
            if state is not None and state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == "kW":
                watts *= 1000
            powers[source.id] = watts if watts >= source.on_threshold_w else 0.0
        return powers

    def _take_sample(self, now: float) -> Sample:
        self.source_power = self._read_source_power()
        return Sample(
            ts=now,
            t_in=self._read_temperature(self.config.get(CONF_INDOOR_TEMP)),
            rh_in=self._read_number(self.config.get(CONF_INDOOR_HUMIDITY), "humidity"),
            aq=self._read_number(self.config.get(CONF_AIR_QUALITY)),
            t_out=self._read_temperature(self.config.get(CONF_OUTDOOR_TEMP)),
            rh_out=self._read_number(self.config.get(CONF_OUTDOOR_HUMIDITY), "humidity"),
            co2=self._read_number(self.config.get(CONF_CO2)),
            is_open=bool(self.openings_open()),
            gains=dict(self.source_power),
        )

    # --- evaluation ------------------------------------------------------

    @callback
    def _handle_state_change(self, event: Event) -> None:
        now = time.time()
        was_running = {k for k, w in self.source_power.items() if w}
        self.buffer.add(self._take_sample(now))
        is_running = {k for k, w in self.source_power.items() if w}
        entity_id = event.data.get("entity_id", "")
        # Doors/windows and heat sources switching on/off change the picture
        # immediately; other sensors wait for the throttle.
        if (
            entity_id in (self.config.get(CONF_OPENINGS) or [])
            or was_running != is_running
            or now - self._last_eval >= _MIN_EVAL_GAP_S
        ):
            self._evaluate(sample_taken=True)

    @callback
    def _handle_interval(self, _now: Any) -> None:
        self._evaluate()

    @property
    def tuning(self) -> Tuning:
        """Base tuning with learned passive and ventilation rates applied."""
        tuning = self.learner.apply_to_tuning(self.base_tuning)
        tuning.ach_natural = self.occupancy.ach(VENT_CLOSED)
        return tuning

    @callback
    def _evaluate(self, sample_taken: bool = False) -> None:
        now = time.time()
        self._last_eval = now
        if not sample_taken:
            self.buffer.add(self._take_sample(now))
        tuning = self.tuning
        mean_power = self.buffer.mean_gains(self.window_s, now)
        self.cycle = detect_cycling(self.buffer.samples, now, CYCLE_HISTORY_S)
        self.features = compute_features(
            self.buffer,
            now,
            self.window_s,
            tuning,
            self._sun_up(),
            dt_util.now().hour,
            gains=self.gains.predict(mean_power),
            cycle=self.cycle,
        )
        if self.features is not None:
            rule = score_causes(self.features, tuning)
            self.prediction = self.learner.predict(self.features, rule, self.appliances, tuning)
        else:
            self.prediction = None

        label = self.prediction.label if self.prediction else None
        confidence = self.prediction.confidence if self.prediction else 0.0
        self._learn_gains(mean_power)
        self._estimate_fans(tuning)
        warmup = self.window_s * EFFECTIVENESS_WARMUP_FRACTION
        for sim in self.sims.values():
            # Another unit actively driving the room means this one can't be resting.
            others_active = any(
                o.status == STATUS_ACTIVE and o.appliance.id != sim.appliance.id for o in self.sims.values()
            )
            sim.update(
                now,
                self.features,
                label,
                confidence,
                self.min_confidence,
                self.hold_band,
                others_active,
                max_idle_s=self.max_idle_s,
                effectiveness_warmup_s=warmup,
                fan_estimate=self.fan_estimates.get(sim.appliance.id),
            )
        self._update_occupancy()
        self._check_effectiveness()
        self._teach_from_manual(now)
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry_id))

    def _teach_from_manual(self, now: float) -> None:
        """A manually-set mode is ground truth: learn from it periodically."""
        if not self.learn_from_manual or self.features is None:
            return
        for sim in self.sims.values():
            if sim.manual_mode in (MANUAL_AUTO, "off") or sim.status != STATUS_ACTIVE:
                continue
            cause = sim.appliance.cause_for_mode(sim.manual_mode)
            if cause is None:
                continue
            if now - self._last_manual_teach.get(sim.appliance.id, 0.0) < MANUAL_TEACH_INTERVAL_S:
                continue
            self._last_manual_teach[sim.appliance.id] = now
            self.learner.teach(self.features, make_label(cause, sim.appliance.id), source="manual", now=now)

    @property
    def simulated(self) -> list[Appliance]:
        """Appliances whose state is inferred (everything except heat sources)."""
        return [sim.appliance for sim in self.sims.values()]

    # --- heat sources ----------------------------------------------------

    def _learn_gains(self, mean_power: dict[str, float]) -> None:
        """Learn heat-source effects only when nothing else could be causing the change."""
        f = self.features
        if (
            f is None
            or f.is_open
            or not any(w > 0 for w in mean_power.values())
            or any(sim.status == STATUS_ACTIVE for sim in self.sims.values())
        ):
            return
        if self.prediction is not None and split_label(self.prediction.label)[0] in ACTIVE_CAUSES:
            return
        self.gains.learn(mean_power, f.r_t_raw - f.expected_passive, f.r_h_raw)

    def source_effects(self) -> dict[str, tuple[float, float]]:
        """Current (°C/h, %RH/h) of each heat source."""
        return self.gains.per_source(self.buffer.mean_gains(self.window_s, time.time()))

    # --- fan groups ------------------------------------------------------

    def _excess_ach(self, tuning: Tuning) -> float | None:
        f = self.features
        if f is None or f.ach_obs is None:
            return None
        return f.ach_obs - tuning.ach_natural

    def _estimate_fans(self, tuning: Tuning) -> None:
        excess = self._excess_ach(tuning)
        self.fan_estimates = {gid: g.estimate(excess) for gid, g in self.fan_groups.items()}

    def calibrate_fans(self, appliance_id: str, fans_on: list[str] | None, count: int | None) -> bool:
        """Record which fans (or how many) are running right now."""
        group = self.fan_groups.get(appliance_id)
        excess = self._excess_ach(self.tuning)
        if group is None or excess is None:
            return False
        ok = group.calibrate(time.time(), excess, fans_on, count)
        if ok:
            self._evaluate()
        return ok

    def set_manual_units(self, appliance_id: str, units: int | None) -> None:
        self.sims[appliance_id].manual_units = units
        self._evaluate()

    # --- differentials ---------------------------------------------------

    @property
    def differentials(self) -> dict[str, float | None]:
        """Indoor minus outdoor for each paired reading, plus CO2 above outdoor air."""
        f = self.features
        latest = self.buffer.latest
        t_in, t_out = latest("t_in"), latest("t_out")
        rh_in, rh_out = latest("rh_in"), latest("rh_out")
        ah_in, ah_out = absolute_humidity(t_in, rh_in), absolute_humidity(t_out, rh_out)
        co2 = latest("co2")

        def diff(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else a - b

        return {
            "temperature": diff(t_in, t_out),
            "humidity": diff(rh_in, rh_out),
            "absolute_humidity_in": ah_in,
            "absolute_humidity_out": ah_out,
            "absolute_humidity": diff(ah_in, ah_out),
            "co2_excess": None if co2 is None else co2 - self.base_tuning.co2_outdoor,
            "temperature_rate_raw": None if f is None else f.r_t_raw,
            "humidity_rate_raw": None if f is None else f.r_h_raw,
        }

    # --- occupancy -------------------------------------------------------

    @property
    def vent_state(self) -> str:
        if self.openings_open():
            return VENT_OPEN
        if any(
            s.status != STATUS_OFF and s.appliance.cause_for_mode(s.mode) == CAUSE_VENTILATION
            for s in self.sims.values()
        ):
            return VENT_FAN
        return VENT_CLOSED

    @property
    def activity(self) -> str:
        """Override if set, otherwise sleeping during the configured sleep hours."""
        if self.activity_override != ACTIVITY_AUTO:
            return self.activity_override
        hour = dt_util.now().hour
        start, end = self.sleep_start, self.sleep_end
        asleep = start <= hour < end if start < end else hour >= start or hour < end
        return ACTIVITY_SLEEPING if asleep else ACTIVITY_RESTING

    def _update_occupancy(self) -> None:
        f = self.features
        estimate = self.occupancy.estimate(
            f.co2 if f else None, f.r_co2 if f else None, self.vent_state, self.activity
        )
        if estimate is None:
            self.occupancy_estimate = None
            return
        raw = estimate["people"]
        if self._people_smoothed is None:
            self._people_smoothed = raw
        else:
            self._people_smoothed += _OCCUPANCY_EMA * (raw - self._people_smoothed)
        estimate["people_smoothed"] = self._people_smoothed
        estimate["calibrations"] = self.occupancy.calibration_count
        self.occupancy_estimate = estimate

    def calibrate_occupancy(self, people: int, activity: str | None) -> bool:
        f = self.features
        ok = self.occupancy.calibrate(
            time.time(),
            people,
            activity or self.activity,
            self.vent_state,
            f.co2 if f else None,
            f.r_co2 if f else None,
        )
        if ok:
            self._people_smoothed = float(people)
            self._evaluate()
        return ok

    def set_activity(self, activity: str) -> None:
        self.activity_override = activity
        self._evaluate()

    # --- effectiveness ---------------------------------------------------

    def _check_effectiveness(self) -> None:
        for appliance_id, sim in self.sims.items():
            rating = sim.rating
            previous = self.last_ratings.get(appliance_id)
            if rating is None or rating == previous:
                continue
            self.last_ratings[appliance_id] = rating
            self.hass.bus.async_fire(
                EVENT_EFFECTIVENESS,
                {
                    "entry_id": self.entry_id,
                    "appliance_id": appliance_id,
                    "appliance": sim.appliance.name,
                    "rating": rating,
                    "previous": previous,
                },
            )
            worse = previous is None or rating_rank(rating) > rating_rank(previous)
            if worse and rating_rank(rating) >= rating_rank(self.notify_rating):
                self._notify_effectiveness(sim, rating)

    def _notify_effectiveness(self, sim: ApplianceSimulator, rating: str) -> None:
        title = f"{sim.appliance.name}: effectiveness {RATING_NAMES[rating]}"
        message = (
            f"{sim.appliance.name} is heating/cooling less effectively than after its last "
            "filter clean, even allowing for outdoor conditions. Check or clean the filter, "
            'then press "Filter cleaned" so the baseline resets.'
        )
        persistent_notification.async_create(
            self.hass,
            message,
            title=title,
            notification_id=f"{DOMAIN}_{self.entry_id}_{sim.appliance.id}_filter",
        )
        if self.notify_service:
            domain, _, service = self.notify_service.partition(".")
            if not service:
                domain, service = "notify", domain
            self.hass.async_create_task(
                self.hass.services.async_call(domain, service, {"title": title, "message": message})
            )

    # --- calibration -----------------------------------------------------

    def calibrate(
        self,
        appliance_id: str,
        mode: str | None,
        status: str | None,
        setpoint: float | None,
        filter_status: str | None,
    ) -> None:
        """Record ground truth for an appliance and teach the classifier from it."""
        sim = self.sims[appliance_id]
        f = self.features
        sim.calibrate(time.time(), f.t_in if f else None, mode, status, setpoint, filter_status)
        if filter_status == "clean":
            self.last_ratings.pop(appliance_id, None)
        if f is not None and mode is not None and status in (None, STATUS_ACTIVE):
            cause = sim.appliance.cause_for_mode(mode)
            if cause is not None:
                self.learner.teach(f, make_label(cause, appliance_id), source="calibration")
        self._evaluate()

    # --- public API used by entities and services ------------------------

    @property
    def labels(self) -> list[str]:
        return available_labels(self.appliances)

    def label_name(self, label: str | None) -> str | None:
        if label is None:
            return None
        return label_name(label, self.appliances)

    def label_from_name(self, name: str) -> str | None:
        """Accept either a label key or its display name."""
        for label in self.labels:
            if name in (label, self.label_name(label)):
                return label
        return None

    @property
    def needs_feedback(self) -> bool:
        """An active suggestion that the engine is unsure about."""
        p = self.prediction
        if p is None:
            return False
        cause, _ = split_label(p.label)
        return cause in ACTIVE_CAUSES and p.confidence < FEEDBACK_CONFIDENCE

    def teach(self, label: str) -> bool:
        """User says ``label`` is what is actually happening now."""
        if label not in self.labels or self.features is None:
            return False
        suggested = self.prediction.label if self.prediction else None
        self.learner.teach(self.features, label, source="feedback", was_suggested=suggested)
        self.last_taught = label
        _LOGGER.debug("Taught %s (suggested %s)", label, suggested)
        self._evaluate()
        return True

    def confirm(self) -> bool:
        """User says the current suggestion is right."""
        if self.prediction is None:
            return False
        return self.teach(self.prediction.label)

    def reset_learning(self) -> None:
        self.learner.reset()
        self.occupancy.reset()
        self.gains.reset()
        for group in self.fan_groups.values():
            group.reset()
        self.last_taught = None
        self.last_ratings = {}
        for sim in self.sims.values():
            sim.reset_learning()
        self._evaluate()

    def set_manual(self, appliance_id: str, mode: str | None, setpoint: float | None) -> None:
        sim = self.sims[appliance_id]
        if mode is not None:
            sim.manual_mode = mode
            self._last_manual_teach.pop(appliance_id, None)
        if setpoint is not None:
            sim.manual_setpoint = sim.appliance.clamp_setpoint(setpoint)
        self._evaluate()

    def clear_manual_setpoint(self, appliance_id: str) -> None:
        self.sims[appliance_id].manual_setpoint = None
        self._evaluate()

    def reset_energy(self, appliance_id: str) -> None:
        self.sims[appliance_id].energy_kwh = 0.0
        self._evaluate()
