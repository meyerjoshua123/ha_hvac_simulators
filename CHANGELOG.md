# Changelog

All notable changes to HVAC Simulators are tracked here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **HVAC functions**: an HVAC unit is set up by ticking what it can do (Heat, Ventilate, Air condition) and its modes follow: heat; cool and dehumidify (dry); ventilate (fresh air, lowers CO2) and filter (indoor recirculation, particulates only). Power is asked only for the modes it has.
- **Extractor fan groups**: one appliance with N named fans and airflow per fan. Estimates how many are on from the excess CO2 air-change rate; calibrate a named fan alone (or give a count) and it identifies which fans are running. `Fans on` sensor, `Fans running (calibrate)` number, `fans`/`fans_on` on the `calibrate` and `set_manual` services; power scales with fans on.
- **Heat sources** (`gains.py`): fridge, dryer, oven etc. linked to their smart-plug power sensor. Their effect on temperature and humidity per kW is learned (ridge regression with backfitting when several run at once, only while no HVAC is active and doors/windows are shut) and subtracted before detection, so a dryer isn't read as heating or a humidifier. New cause "Heat/moisture from appliances", `Appliance heat or moisture` binary sensor for automations, per-source `Running` and `Heat effect`, raw vs compensated trends.
- **Humidifier** appliance type and humidifying cause.
- **Differentials**: indoor-outdoor temperature, relative humidity and absolute humidity (g/m³) differences, indoor/outdoor absolute humidity, CO2 above outdoor.

### Changed
- Air conditioner type is now "HVAC unit"; old configs listing modes are mapped to functions.
- A heat source switching on/off triggers an immediate re-evaluation.

## [0.1.0-dev] - initial build

### Added
- Integration scaffold (`custom_components/hvac_simulators`), HACS metadata.
- **Inference engine** (`engine.py`): trend analysis (least-squares slopes) of indoor temperature, humidity, CO2 and air quality; physics-based passive model (Newton's law of cooling with separate closed/open exchange rates); scored causes:
  - Active: heating, cooling, dehumidifying, extractor fan/ventilation, air filtering.
  - Passive: idle, cooling/warming from an open door or window, airing out, heat leaking in from outside, solar gain (outdoor temp above a configurable threshold), heat loss to outside.
  - Cooling vs dehumidify told apart by whether humidity lags the temperature drop.
- **CO2 vs air quality split**: only real air exchange (open door/window or extractor fan) can clear CO2 faster than natural infiltration; particulates/VOC improving with CO2 flat is attributed to a filter (aircon or purifier).
- **Appliances** (`appliances.py`): heater, air conditioner/heat pump (heat/cool/dry/fan), extractor fan, dehumidifier, air purifier. Rated power per mode, standby power, min/max setpoint, inverter flag, idle power factor.
- **Learning** (`learning.py`): confirm/correct feedback stored as labeled examples; distance-weighted nearest-neighbour vote blended with the rules (weight grows with examples); learns the building's passive exchange rates and which appliance usually produces each cause.
- **Cycling detection** (`simulator.py`): each appliance keeps its *mode* (heat/cool/…) while its *status* cycles between `active` and `idle`; goes `off` when temperature drifts past the setpoint band or a fixed-speed unit stays idle too long. Cycle count and duty cycle.
- **Setpoint detection**: midpoint of cycling turn points, clamped to the user's min/max, blended with calibrations; manual setpoint override.
- **Effectiveness rating** (`effectiveness.py`): heating/cooling performance vs a baseline fitted after the last filter clean, normalized for outdoor lift; rated Excellent → Great → Good → OK → Bad → Terrible.
- **Calibration**: record setpoint, mode, status and filter state at any time; multiple calibrations are combined with detection history.
- **Occupancy from CO2** (`occupancy.py`): mass-balance estimate of people home, adjusted for activity (sleeping, resting, active, exercising) and ventilation state (closed, extractor fan, open). Calibrate with a head count; 0 people teaches the natural air-change rate. Multiple calibrations combined by median. Sleep hours set the default activity.
- **Notifications**: persistent notification, optional `notify` service, and an `hvac_simulators_effectiveness_changed` event when effectiveness drops to a chosen rating.
- **Energy**: estimated power sensor per appliance (usable by PowerCalc / Energy dashboard) and an integrated energy sensor. Idle power: inverter at a reduced share, fixed-speed aircon on fan only.
- **Config flow**: set up a space (sensors, solar threshold) and add appliances during setup; options flow for sensors, tuning (incl. volume, outdoor CO2, sleep hours, max idle time, notifications) and adding/editing/removing appliances. Removed appliances' devices are cleaned up.
- **Entities**: suggested mode (with reasons/candidates), confidence, trends (temperature, humidity, air quality, CO2, air changes), learned examples, estimated people, needs feedback, door/window open, teach select, activity select, people calibration; per appliance: state, mode, on, power, energy, detected setpoint, effectiveness, filter needs attention, manual mode, setpoint, filter cleaned, use detected setpoint.
- **Services**: `teach`, `confirm`, `reset_learning`, `set_manual`, `calibrate`, `calibrate_occupancy`, `reset_energy`.
- Tests: pure-Python scenario tests for engine, learner, simulator, effectiveness and occupancy, plus end-to-end tests inside a real Home Assistant core. CI workflow runs hassfest, HACS validation, ruff and pytest.

### Fixed
- Learned passive rates no longer reset the CO2 settings (outdoor CO2, ventilation factor) back to defaults.
