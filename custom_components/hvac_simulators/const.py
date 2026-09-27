"""Constants for HVAC Simulators."""

from __future__ import annotations

DOMAIN = "hvac_simulators"
MANUFACTURER = "HVAC Simulators"
STORAGE_VERSION = 1

SIGNAL_UPDATE = DOMAIN + "_update_{}"

# Sensors (config entry data/options)
CONF_INDOOR_TEMP = "indoor_temperature"
CONF_INDOOR_HUMIDITY = "indoor_humidity"
CONF_OUTDOOR_TEMP = "outdoor_temperature"
CONF_OUTDOOR_HUMIDITY = "outdoor_humidity"
CONF_AIR_QUALITY = "air_quality"
CONF_CO2 = "co2"
CONF_AQ_LOWER_IS_BETTER = "aq_lower_is_better"
CONF_OPENINGS = "openings"
CONF_SUN_ENTITY = "sun_entity"

SENSOR_KEYS = (
    CONF_INDOOR_TEMP,
    CONF_INDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMP,
    CONF_OUTDOOR_HUMIDITY,
    CONF_AIR_QUALITY,
    CONF_CO2,
    CONF_AQ_LOWER_IS_BETTER,
    CONF_OPENINGS,
    CONF_SUN_ENTITY,
    "main_area",
    "main_type",
    "main_air_gaps",
    "main_air_gap_notes",
    "use_virtual_openings",
)

# Tuning (options)
CONF_WINDOW_MINUTES = "trend_window_minutes"
CONF_SOLAR_THRESHOLD = "solar_threshold"
CONF_ACTIVE_RATE = "active_rate"
CONF_STABLE_RATE = "stable_rate"
CONF_DEHUMID_RATE = "dehumidify_rate"
CONF_AQ_IMPROVE_PCT = "aq_improve_percent"
CONF_MIN_CONFIDENCE = "min_confidence"
CONF_MAX_LEARNED_WEIGHT = "max_learned_weight"
CONF_HOLD_BAND = "hold_band"
CONF_LEARN_FROM_MANUAL = "learn_from_manual"
CONF_MAX_IDLE_MINUTES = "max_idle_minutes"
CONF_CO2_OUTDOOR = "co2_outdoor"
CONF_VOLUME = "volume_m3"
CONF_SLEEP_START = "sleep_start_hour"
CONF_SLEEP_END = "sleep_end_hour"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_NOTIFY_RATING = "notify_rating"
CONF_SOLAR_MARGIN = "solar_margin"
CONF_UPDATE_MODE = "update_mode"
CONF_UPDATE_INTERVAL = "update_interval_s"
# Main room (the space's own sensors) and extra rooms
CONF_MAIN_AREA = "main_area"
CONF_MAIN_TYPE = "main_type"
CONF_MAIN_AIR_GAPS = "main_air_gaps"
CONF_MAIN_AIR_GAP_NOTES = "main_air_gap_notes"
CONF_MAIN_FLOWS_INTO = "main_flows_into"
CONF_USE_VIRTUAL_OPENINGS = "use_virtual_openings"
CONF_ROOMS = "rooms"

DEFAULT_WINDOW_MINUTES = 15
DEFAULT_SOLAR_THRESHOLD = 22.0
DEFAULT_ACTIVE_RATE = 0.4
DEFAULT_STABLE_RATE = 0.15
DEFAULT_DEHUMID_RATE = -3.0
DEFAULT_AQ_IMPROVE_PCT = 5.0
DEFAULT_MIN_CONFIDENCE = 0.45
DEFAULT_MAX_LEARNED_WEIGHT = 0.75
DEFAULT_HOLD_BAND = 1.0
DEFAULT_LEARN_FROM_MANUAL = True
DEFAULT_MAX_IDLE_MINUTES = 60
DEFAULT_CO2_OUTDOOR = 420.0
DEFAULT_VOLUME = 100.0
DEFAULT_SLEEP_START = 23
DEFAULT_SLEEP_END = 7
DEFAULT_NOTIFY_RATING = "ok"
DEFAULT_SOLAR_MARGIN = 2.0
UPDATE_MODE_INTERVAL = "interval"
UPDATE_MODE_ON_CHANGE = "on_change"
DEFAULT_UPDATE_INTERVAL = 60
# Never evaluate more often than this, even on every sensor change.
MIN_EVAL_GAP_ON_CHANGE_S = 2
# Don't sample effectiveness until the trend window reflects active running.
EFFECTIVENESS_WARMUP_FRACTION = 0.6

# Appliances (list of dicts stored in options)
CONF_APPLIANCES = "appliances"
CONF_APPLIANCE_ID = "id"
CONF_APPLIANCE_NAME = "name"
CONF_APPLIANCE_TYPE = "type"
CONF_MODES = "modes"
CONF_FUNCTIONS = "functions"
CONF_COUNT = "count"
CONF_MEMBERS = "members"
CONF_AIRFLOW = "airflow_m3h"
CONF_POWER_ENTITY = "power_entity"
CONF_ON_THRESHOLD_W = "on_threshold_w"
CONF_STANDBY_W = "standby_w"
CONF_MIN_SETPOINT = "min_setpoint"
CONF_MAX_SETPOINT = "max_setpoint"
CONF_INVERTER = "inverter"
CONF_HOLD_FACTOR = "hold_factor"
CONF_IDLE_FAN_W = "idle_fan_w"
CONF_CALCULATE_ENERGY = "calculate_energy"

# Evaluation cadence
UPDATE_INTERVAL_S = 60
# While the user keeps a manual mode set, record a training example this often.
MANUAL_TEACH_INTERVAL_S = 15 * 60
# Below this confidence an active suggestion asks for feedback.
FEEDBACK_CONFIDENCE = 0.6

SERVICE_TEACH = "teach"
SERVICE_CONFIRM = "confirm"
SERVICE_RESET_LEARNING = "reset_learning"
SERVICE_SET_MANUAL = "set_manual"
SERVICE_RESET_ENERGY = "reset_energy"
SERVICE_CALIBRATE = "calibrate"
SERVICE_CALIBRATE_OCCUPANCY = "calibrate_occupancy"

EVENT_EFFECTIVENESS = DOMAIN + "_effectiveness_changed"
EVENT_APPLIANCE = DOMAIN + "_appliance_event"

ATTR_LABEL = "label"
ATTR_MODE = "mode"
ATTR_SETPOINT = "setpoint"
ATTR_STATUS = "status"
ATTR_FILTER = "filter"
ATTR_PEOPLE = "people"
ATTR_ACTIVITY = "activity"
ATTR_FANS = "fans"
ATTR_FANS_ON = "fans_on"
CONF_KIND = "kind"
CONF_ACTIVE_ABOVE_W = "active_above_w"
CONF_ROOM = "room"
CONF_MANUAL_TIMEOUT = "manual_timeout_min"
CONF_OFF_DETECT = "off_detect_min"
