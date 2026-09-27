"""Runtime manager: reads sensors, runs each room, drives appliances, persists learning."""

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

from .appliances import Appliance
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
    CONF_MAIN_AIR_GAP_NOTES,
    CONF_MAIN_AIR_GAPS,
    CONF_MAIN_AREA,
    CONF_MAIN_FLOWS_INTO,
    CONF_MAIN_TYPE,
    CONF_MAX_IDLE_MINUTES,
    CONF_MAX_LEARNED_WEIGHT,
    CONF_MIN_CONFIDENCE,
    CONF_NOTIFY_RATING,
    CONF_NOTIFY_SERVICE,
    CONF_OPENINGS,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMP,
    CONF_ROOMS,
    CONF_SLEEP_END,
    CONF_SLEEP_START,
    CONF_SOLAR_MARGIN,
    CONF_SOLAR_THRESHOLD,
    CONF_STABLE_RATE,
    CONF_SUN_ENTITY,
    CONF_UPDATE_INTERVAL,
    CONF_UPDATE_MODE,
    CONF_USE_VIRTUAL_OPENINGS,
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
    DEFAULT_SOLAR_MARGIN,
    DEFAULT_SOLAR_THRESHOLD,
    DEFAULT_STABLE_RATE,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_VOLUME,
    DEFAULT_WINDOW_MINUTES,
    DOMAIN,
    EFFECTIVENESS_WARMUP_FRACTION,
    EVENT_APPLIANCE,
    EVENT_EFFECTIVENESS,
    MANUAL_TEACH_INTERVAL_S,
    MIN_EVAL_GAP_ON_CHANGE_S,
    SIGNAL_UPDATE,
    STORAGE_VERSION,
    UPDATE_MODE_ON_CHANGE,
)
from .effectiveness import RATING_NAMES, rating_rank
from .engine import CAUSE_VENTILATION, Sample, Tuning
from .occupancy import (
    ACTIVITY_RESTING,
    ACTIVITY_SLEEPING,
    VENT_CLOSED,
    VENT_FAN,
    VENT_OPEN,
)
from .plugs import PlugTracker
from .room import (
    MAIN_ROOM,
    SHOWER_AFTERGLOW_S,
    Room,
    RoomConfig,
    RoomContext,
    observing_room,
    zones,
)
from .simulator import MANUAL_AUTO, STATUS_ACTIVE, STATUS_OFF, ApplianceSimulator

_LOGGER = logging.getLogger(__name__)

_SAVE_DELAY_S = 30
_MIN_EVAL_GAP_S = 10
ACTIVITY_AUTO = "auto"


class HvacSimulatorManager:
    """One monitored space (usually a home): its rooms and appliances."""

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
        self.plugs: dict[str, PlugTracker] = {
            a.id: PlugTracker(a.kind, a.on_threshold_w, a.active_above_w) for a in self.heat_sources
        }
        self.source_power: dict[str, float] = {}
        self.window_s = float(self._opt(CONF_WINDOW_MINUTES, DEFAULT_WINDOW_MINUTES)) * 60
        self.min_confidence = float(self._opt(CONF_MIN_CONFIDENCE, DEFAULT_MIN_CONFIDENCE))
        self.hold_band = float(self._opt(CONF_HOLD_BAND, DEFAULT_HOLD_BAND))
        self.learn_from_manual = bool(self._opt(CONF_LEARN_FROM_MANUAL, DEFAULT_LEARN_FROM_MANUAL))
        self.update_on_change = self.config.get(CONF_UPDATE_MODE) == UPDATE_MODE_ON_CHANGE
        self.update_interval_s = max(5.0, float(self._opt(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)))
        self.base_tuning = Tuning(
            active_rate=float(self._opt(CONF_ACTIVE_RATE, DEFAULT_ACTIVE_RATE)),
            stable_rate=float(self._opt(CONF_STABLE_RATE, DEFAULT_STABLE_RATE)),
            dehumid_rh_rate=float(self._opt(CONF_DEHUMID_RATE, DEFAULT_DEHUMID_RATE)),
            aq_improve_rel=-abs(float(self._opt(CONF_AQ_IMPROVE_PCT, DEFAULT_AQ_IMPROVE_PCT))) / 100.0,
            solar_threshold=float(self._opt(CONF_SOLAR_THRESHOLD, DEFAULT_SOLAR_THRESHOLD)),
            solar_margin=float(self._opt(CONF_SOLAR_MARGIN, DEFAULT_SOLAR_MARGIN)),
            aq_lower_is_better=bool(self._opt(CONF_AQ_LOWER_IS_BETTER, True)),
            co2_outdoor=float(self._opt(CONF_CO2_OUTDOOR, DEFAULT_CO2_OUTDOOR)),
        )
        self.max_idle_s = float(self._opt(CONF_MAX_IDLE_MINUTES, DEFAULT_MAX_IDLE_MINUTES)) * 60
        self.sleep_start = int(self._opt(CONF_SLEEP_START, DEFAULT_SLEEP_START))
        self.sleep_end = int(self._opt(CONF_SLEEP_END, DEFAULT_SLEEP_END))
        self.notify_service: str | None = self.config.get(CONF_NOTIFY_SERVICE) or None
        self.notify_rating = str(self._opt(CONF_NOTIFY_RATING, DEFAULT_NOTIFY_RATING))
        self.activity_override = ACTIVITY_AUTO
        self.last_ratings: dict[str, str | None] = {}

        self.room_configs: list[RoomConfig] = [self._main_room_config()] + [
            RoomConfig.from_dict(r) for r in self.config.get(CONF_ROOMS, []) if r.get("id") != MAIN_ROOM
        ]
        self.appliance_room: dict[str, str] = {
            a.id: observing_room(a, self.room_configs) for a in self.appliances
        }
        max_weight = float(self._opt(CONF_MAX_LEARNED_WEIGHT, DEFAULT_MAX_LEARNED_WEIGHT))
        self.rooms: dict[str, Room] = {}
        for rc in self.room_configs:
            self.rooms[rc.id] = Room(
                rc,
                self.base_tuning,
                self.window_s,
                max_weight,
                [s.appliance for s in self.sims.values() if self.appliance_room[s.appliance.id] == rc.id],
                [h for h in self.heat_sources if self.appliance_room[h.id] == rc.id],
            )
        self.zones = zones(self.room_configs)
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsubs: list[Callable[[], None]] = []
        self._last_eval = 0.0
        self._last_manual_teach: dict[str, float] = {}

    def _opt(self, key: str, default: Any) -> Any:
        value = self.config.get(key)
        return default if value in (None, "") else value

    def _main_room_config(self) -> RoomConfig:
        c = self.config
        return RoomConfig(
            id=MAIN_ROOM,
            name=self.entry.title,
            area_id=c.get(CONF_MAIN_AREA) or None,
            type=c.get(CONF_MAIN_TYPE) or "living",
            temperature=c.get(CONF_INDOOR_TEMP) or None,
            humidity=c.get(CONF_INDOOR_HUMIDITY) or None,
            co2=c.get(CONF_CO2) or None,
            air_quality=c.get(CONF_AIR_QUALITY) or None,
            openings=list(c.get(CONF_OPENINGS) or []),
            volume_m3=float(self._opt(CONF_VOLUME, DEFAULT_VOLUME)),
            air_gaps=c.get(CONF_MAIN_AIR_GAPS) or "none",
            air_gap_notes=c.get(CONF_MAIN_AIR_GAP_NOTES) or "",
            flows_into=list(c.get(CONF_MAIN_FLOWS_INTO) or []),
            use_virtual_openings=bool(c.get(CONF_USE_VIRTUAL_OPENINGS, True)),
        )

    # --- rooms -------------------------------------------------------------

    @property
    def main(self) -> Room:
        return self.rooms[MAIN_ROOM]

    def room_of(self, appliance_id: str) -> Room:
        """The room whose sensors observe this appliance."""
        return self.rooms[self.appliance_room.get(appliance_id, MAIN_ROOM)]

    def zone_of(self, room_id: str) -> set[str]:
        for group in self.zones:
            if room_id in group:
                return group
        return {room_id}

    @property
    def simulated(self) -> list[Appliance]:
        return [sim.appliance for sim in self.sims.values()]

    # --- lifecycle -----------------------------------------------------------

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        rooms_data = dict(data.get("rooms") or {})
        if "learner" in data and MAIN_ROOM not in rooms_data:
            # Stored before rooms existed: everything belonged to the one space.
            rooms_data[MAIN_ROOM] = {
                k: data.get(k) for k in ("learner", "gains", "occupancy", "fan_groups", "last_taught")
            }
        for room_id, room in self.rooms.items():
            room.load(rooms_data.get(room_id))
        for appliance_id, sim_data in data.get("appliances", {}).items():
            if appliance_id in self.sims:
                self.sims[appliance_id].load(sim_data)
        for plug_id, plug_data in (data.get("plugs") or {}).items():
            if plug_id in self.plugs:
                self.plugs[plug_id].load(plug_data)
        self.activity_override = data.get("activity_override", ACTIVITY_AUTO)
        self.last_ratings = dict(data.get("last_ratings", {}))

        watched = self._source_entities()
        if watched:
            self._unsubs.append(async_track_state_change_event(self.hass, watched, self._handle_state_change))
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._handle_interval, timedelta(seconds=self.update_interval_s)
            )
        )
        self._evaluate()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "rooms": {rid: room.as_dict() for rid, room in self.rooms.items()},
            "appliances": {k: s.as_dict() for k, s in self.sims.items()},
            "plugs": {k: p.as_dict() for k, p in self.plugs.items()},
            "activity_override": self.activity_override,
            "last_ratings": self.last_ratings,
        }

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY_S)

    def _source_entities(self) -> list[str]:
        entities = [self.config.get(CONF_OUTDOOR_TEMP), self.config.get(CONF_OUTDOOR_HUMIDITY)]
        for rc in self.room_configs:
            entities.extend(rc.entities)
        entities.extend(a.power_entity for a in self.heat_sources)
        return sorted({e for e in entities if e})

    # --- reading sensors -------------------------------------------------------

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

    def openings_open(self, room_id: str = MAIN_ROOM) -> list[str]:
        """Entity ids of a room's doors/windows that are open."""
        return [
            e
            for e in self.rooms[room_id].config.openings
            if (s := self.hass.states.get(e)) is not None and s.state == STATE_ON
        ]

    def _sun_up(self) -> bool | None:
        state = self.hass.states.get(self.config.get(CONF_SUN_ENTITY) or "sun.sun")
        if state is None:
            return None
        return state.state == "above_horizon"

    def _read_source_power(self) -> dict[str, float]:
        """Measured power (W) of each heat source, zero below its off threshold."""
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

    def _take_samples(self, now: float) -> None:
        self.source_power = self._read_source_power()
        t_out = self._read_temperature(self.config.get(CONF_OUTDOOR_TEMP))
        rh_out = self._read_number(self.config.get(CONF_OUTDOOR_HUMIDITY), "humidity")
        for room in self.rooms.values():
            rc = room.config
            room.add_sample(
                Sample(
                    ts=now,
                    t_in=self._read_temperature(rc.temperature),
                    rh_in=self._read_number(rc.humidity, "humidity"),
                    aq=self._read_number(rc.air_quality),
                    t_out=t_out,
                    rh_out=rh_out,
                    co2=self._read_number(rc.co2),
                    is_open=bool(self.openings_open(rc.id)),
                    gains={s.id: self.source_power.get(s.id, 0.0) for s in room.heat_sources},
                )
            )

    # --- evaluation -------------------------------------------------------------

    @callback
    def _handle_state_change(self, event: Event) -> None:
        now = time.time()
        was_running = {k for k, w in self.source_power.items() if w}
        self._take_samples(now)
        is_running = {k for k, w in self.source_power.items() if w}
        entity_id = event.data.get("entity_id", "")
        openings = {e for rc in self.room_configs for e in rc.openings}
        min_gap = MIN_EVAL_GAP_ON_CHANGE_S if self.update_on_change else _MIN_EVAL_GAP_S
        # Doors/windows and heat sources switching on/off change the picture
        # immediately; other sensors wait for the throttle.
        if entity_id in openings or was_running != is_running or now - self._last_eval >= min_gap:
            self._evaluate(samples_taken=True)

    @callback
    def _handle_interval(self, _now: Any) -> None:
        self._evaluate()

    def _vent_state(self, room: Room) -> str:
        if self.openings_open(room.id) or room.estimated_open:
            return VENT_OPEN
        if any(
            s.status != STATUS_OFF
            and s.appliance.cause_for_mode(s.mode) == CAUSE_VENTILATION
            and self.appliance_room[s.appliance.id] in self.zone_of(room.id)
            for s in self.sims.values()
        ):
            return VENT_FAN
        return VENT_CLOSED

    def _shower_in_zone(self, room: Room, now: float) -> bool:
        return any(
            self.rooms[rid].last_shower_ts is not None
            and now - self.rooms[rid].last_shower_ts <= SHOWER_AFTERGLOW_S
            for rid in self.zone_of(room.id)
        )

    @callback
    def _evaluate(self, samples_taken: bool = False) -> None:
        now = time.time()
        self._last_eval = now
        if not samples_taken:
            self._take_samples(now)
        local = dt_util.now()
        sun_up = self._sun_up()
        for room in self.rooms.values():
            observed = [s for s in self.sims.values() if self.appliance_room[s.appliance.id] == room.id]
            room.evaluate(
                now,
                RoomContext(
                    sun_up=sun_up,
                    hour=local.hour,
                    date=local.date().isoformat(),
                    activity=self.activity,
                    vent_state=self._vent_state(room),
                    appliance_active=any(s.status == STATUS_ACTIVE for s in observed),
                    shower_in_zone=self._shower_in_zone(room, now),
                ),
            )

        warmup = self.window_s * EFFECTIVENESS_WARMUP_FRACTION
        for sim in self.sims.values():
            room = self.room_of(sim.appliance.id)
            prediction = room.prediction
            others_active = any(
                o.status == STATUS_ACTIVE
                and o.appliance.id != sim.appliance.id
                and self.appliance_room[o.appliance.id] == room.id
                for o in self.sims.values()
            )
            sim.update(
                now,
                room.features,
                prediction.label if prediction else None,
                prediction.confidence if prediction else 0.0,
                self.min_confidence,
                self.hold_band,
                others_active,
                max_idle_s=self.max_idle_s,
                effectiveness_warmup_s=warmup,
                fan_estimate=room.fan_estimates.get(sim.appliance.id),
            )
            if sim.last_event:
                self.hass.bus.async_fire(
                    EVENT_APPLIANCE,
                    {"entry_id": self.entry_id, "appliance_id": sim.appliance.id, "event": sim.last_event},
                )
                sim.last_event = None
        for plug_id, plug in self.plugs.items():
            plug.update(now, self.source_power.get(plug_id))
        self._check_effectiveness()
        self._teach_from_manual(now)
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry_id))

    def _teach_from_manual(self, now: float) -> None:
        """A manually-set mode is ground truth: learn from it periodically."""
        if not self.learn_from_manual:
            return
        for sim in self.sims.values():
            if sim.manual_mode in (MANUAL_AUTO, "off") or sim.status != STATUS_ACTIVE:
                continue
            if now - self._last_manual_teach.get(sim.appliance.id, 0.0) < MANUAL_TEACH_INTERVAL_S:
                continue
            self._last_manual_teach[sim.appliance.id] = now
            self.room_of(sim.appliance.id).teach_mode(sim.appliance, sim.manual_mode, now, "manual")

    # --- occupancy / activity -----------------------------------------------------

    @property
    def activity(self) -> str:
        """Override if set, otherwise sleeping during the configured sleep hours."""
        if self.activity_override != ACTIVITY_AUTO:
            return self.activity_override
        hour = dt_util.now().hour
        start, end = self.sleep_start, self.sleep_end
        asleep = start <= hour < end if start < end else hour >= start or hour < end
        return ACTIVITY_SLEEPING if asleep else ACTIVITY_RESTING

    def set_activity(self, activity: str) -> None:
        self.activity_override = activity
        self._evaluate()

    def calibrate_occupancy(self, room_id: str, people: int, activity: str | None) -> bool:
        room = self.rooms[room_id]
        ok = room.calibrate_occupancy(time.time(), people, activity or self.activity, self._vent_state(room))
        if ok:
            self._evaluate()
        return ok

    # --- effectiveness ---------------------------------------------------------------

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

    # --- actions used by entities and services -----------------------------------------

    def teach(self, room_id: str, label: str) -> bool:
        ok = self.rooms[room_id].teach(label, time.time())
        if ok:
            self._evaluate()
        return ok

    def confirm(self, room_id: str) -> bool:
        room = self.rooms[room_id]
        return room.prediction is not None and self.teach(room_id, room.prediction.label)

    def calibrate(
        self,
        appliance_id: str,
        mode: str | None,
        status: str | None,
        setpoint: float | None,
        filter_status: str | None,
    ) -> None:
        """Record ground truth for an appliance and teach its room's classifier from it."""
        sim = self.sims[appliance_id]
        room = self.room_of(appliance_id)
        f = room.features
        now = time.time()
        sim.calibrate(now, f.t_in if f else None, mode, status, setpoint, filter_status)
        if filter_status == "clean":
            self.last_ratings.pop(appliance_id, None)
        if mode is not None and status in (None, STATUS_ACTIVE):
            room.teach_mode(sim.appliance, mode, now, "calibration")
        self._evaluate()

    def calibrate_fans(self, appliance_id: str, fans_on: list[str] | None, count: int | None) -> bool:
        ok = self.room_of(appliance_id).calibrate_fans(time.time(), appliance_id, fans_on, count)
        if ok:
            self._evaluate()
        return ok

    def reset_learning(self, room_id: str | None = None) -> None:
        targets = [self.rooms[room_id]] if room_id else list(self.rooms.values())
        for room in targets:
            room.reset_learning()
        if room_id in (None, MAIN_ROOM):
            self.last_ratings = {}
            for sim in self.sims.values():
                sim.reset_learning()
        self._evaluate()

    def set_manual(self, appliance_id: str, mode: str | None, setpoint: float | None) -> None:
        sim = self.sims[appliance_id]
        sim.set_manual(time.time(), mode, setpoint)
        if mode is not None:
            self._last_manual_teach.pop(appliance_id, None)
        self._evaluate()

    def set_manual_units(self, appliance_id: str, units: int | None) -> None:
        self.sims[appliance_id].manual_units = units
        self._evaluate()

    def clear_manual_setpoint(self, appliance_id: str) -> None:
        self.sims[appliance_id].manual_setpoint = None
        self._evaluate()

    def reset_energy(self, appliance_id: str) -> None:
        self.sims[appliance_id].energy_kwh = 0.0
        self._evaluate()
