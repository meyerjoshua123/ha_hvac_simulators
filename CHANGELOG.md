# Changelog

All notable changes to HVAC Simulators are tracked here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-09-27

### Added
- **Rooms from Home Assistant areas.** The space's own sensors are the main room (now with an area, room type, air gaps and notes); add more rooms under Configure, each with its own temperature/humidity/CO2/air-quality/door-window sensors, air volume and known air gaps. Every room runs its own detection, learning, occupancy, fan groups, appliance-heat model and efficiency record, and gets its own device in its area.
- **Zones from room links.** Pick which rooms flow into each other; linked rooms form a zone. An appliance in a room without sensors (e.g. a bathroom fan) is observed by a sensored room in its zone (e.g. the living room whose CO2 it clears). Appliances can be assigned to an area.
- **Room-to-room differences**: temperature, humidity and absolute humidity versus the main room (e.g. laundry vs living room). A dryer plug linked to the laundry is learned in the laundry.
- **Shower detection** for bathroom rooms (steep humidity surge); a shower in the zone stops other rooms blaming a humidifier.
- **Virtual door/window sensor** for rooms without door/window sensors: needs two independent signs of outside air (temperature and absolute humidity heading to outdoor levels at an open-window rate, CO2 flushing out).
- **Fan vs open window**: without door/window information, a fast CO2 drop is split between "extractor fan" and "airing out" instead of being pinned on a fan.
- **Solar gain** now combines outdoor temperature with how far indoor sits above outdoor (new *solar margin* option); the margin counts fully on mild days and fades on cold ones, and very fast warming stays with heating. Night hours count as sun down when there is no sun entity.
- **Plugged-in appliance kinds**: fridge, washer, dryer, oven/microwave, stove, general (TV, iron, steamer, hair tools, soldering iron). Each gets an active/idle/off **Status** sensor from its plug with cycles, 6-hour duty cycle, last active spell and last run length; dryers, stoves and washers start with a moisture prior.
- **Manual timeouts**: a manual mode/setpoint can revert to auto detection after a set time.
- **Switched-off detection** in manual heat/cool: drifting the wrong way past the setpoint for a set time (with no cycling) marks the unit off and returns it to auto. Both fire `hvac_simulators_appliance_event`.
- **Update on every sensor change** (or a configurable interval), and **rate sensors** for temperature, humidity and CO2: since the last reading and over 5/10/15 minutes (hidden by default).
- **Efficiency record** per room: heat-loss coefficient (30-day median), heat lost, heat leaked in, solar gain, appliance heat and ventilation air changes per day.
- Absolute humidity features (indoor, outdoor, rate) in the engine.
- README link to [hacs.luukza.com](https://hacs.luukza.com) and a note that the project was built with Claude Code.

### Changed
- Services accept room devices as well as the space and appliance devices.
- Existing installs keep their entities: the old space becomes the main room with the same entity and device IDs, and stored learning moves into it automatically.

## [0.2.0] - 2026-09-27

Learned from a real night in an apartment: an aircon in cool mode at 18 °C
while the room sensor read ~17 °C. Temperature barely moved because the
building's thermal mass donated back the heat the aircon removed, but humidity
swung ~2 %RH on a steady ~20 minute cycle: the compressor was cycling all night.

### Added
- **Compressor cycling detection** (`cycling.py`): finds regular on/off cycling in indoor humidity (periodic, anti-correlated at half the period, repeats at twice it, big enough swing). Rejects random humidity wander. Exposed as `compressor_cycling` on the suggested-mode sensor.
- While cycling is detected in cool or dry mode, the appliance state follows the compressor: `active` while humidity falls, `idle` between cycles. Mode stays the same; cycles are counted; energy follows the real on-time.
- Cycling counts as evidence for cooling (or dry mode) even when temperature is flat, and the in-cycle swings are no longer read as separate heating/humidifying events.
- **Thermostat offset**: learned while cycling. The difference between the room sensor and the unit's own thermostat (about −1 °C on the recorded night) is shown on the State sensor and used when judging whether the room is past the setpoint.
- **Fan-only power** per HVAC unit/heater, and an `idle_reason` on the State sensor: holding setpoint, between compressor cycles, or setpoint satisfied (compressor off).
- Regression test replaying the recorded night (1-minute data, relative times only).

### Fixed
- Idle power: a unit that is idle because the room is already past its setpoint, or between compressor cycles, is charged fan-only power instead of inverter holding power. The recorded night had been charged ~2.2 kWh at a flat 217 W.
- Manual heat/cool mode with no sensor history yet starts `idle` instead of charging full power.
- The space device is created before appliance devices that link to it (Home Assistant 2025.12+ rejects links to devices that don't exist yet).

## [0.1.0] - 2026-09-27

### Added
- **HVAC functions**: an HVAC unit is set up by ticking what it can do (Heat, Ventilate, Air condition) and its modes follow: heat; cool and dehumidify (dry); ventilate (fresh air, lowers CO2) and filter (indoor recirculation, particulates only). Power is asked only for the modes it has.
- **Extractor fan groups**: one appliance with N named fans and airflow per fan. Estimates how many are on from the excess CO2 air-change rate; calibrate a named fan alone (or give a count) and it identifies which fans are running. `Fans on` sensor, `Fans running (calibrate)` number, `fans`/`fans_on` on the `calibrate` and `set_manual` services; power scales with fans on.
- **Heat sources** (`gains.py`): fridge, dryer, oven etc. linked to their smart-plug power sensor. Their effect on temperature and humidity per kW is learned (ridge regression with backfitting when several run at once, only while no HVAC is active and doors/windows are shut) and subtracted before detection, so a dryer isn't read as heating or a humidifier. New cause "Heat/moisture from appliances", `Appliance heat or moisture` binary sensor for automations, per-source `Running` and `Heat effect`, raw vs compensated trends.
- **Humidifier** appliance type and humidifying cause.
- **Differentials**: indoor-outdoor temperature, relative humidity and absolute humidity (g/m³) differences, indoor/outdoor absolute humidity, CO2 above outdoor.

### Changed
- Air conditioner type is now "HVAC unit"; old configs listing modes are mapped to functions.
- A heat source switching on/off triggers an immediate re-evaluation.


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
