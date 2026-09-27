# HVAC Simulators

**Know what your heating, cooling and ventilation are doing, without smart plugs on them.**

HVAC Simulators is a Home Assistant integration (installed through HACS). It watches how each room's temperature, humidity and CO2 change and subtracts what the weather, the building and your plugged-in appliances explain. What's left, it attributes to your aircon, heater, extractor fans or open windows. You get simulated appliances with a mode, an active/idle/off state, a setpoint, power and energy, and the integration learns from you whenever you confirm or correct it.

📖 **Full feature guide and help: [hacs.luukza.com](https://hacs.luukza.com)**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=meyerjoshua123&repository=ha_hvac_simulators&category=integration)

---

## Highlights

- **Works out what's happening**: heating, cooling, dehumidifying, humidifying, extractor fans, air filtering, open doors/windows, heat leaking in, solar gain, heat loss, and heat or moisture from appliances.
- **Rooms and zones**: each Home Assistant area can be a room with its own sensors. Rooms that flow into each other form a zone, so a bathroom fan can be seen in the living room's CO2.
- **Cycling you can't see in the temperature**: detects compressor on/off cycling from humidity, even when the building's thermal mass keeps the temperature flat.
- **HVAC units by function**: tick Heat, Ventilate and/or Air condition and get the right modes (heat, cool, dry, fresh-air ventilate, indoor filter).
- **Plugged-in appliances**: fridge, washer, dryer, oven/microwave, stove and general appliances get an active/idle/off status from their plug. Their heat and moisture is learned and removed before anything else is decided.
- **Virtual door/window sensor** and **shower detection**.
- **Efficiency record**: heat-loss coefficient, heat lost, solar gain, heat leaked in and air changes, day by day. Compare rooms, seasons, or your old and new home.
- **Effectiveness ratings** and filter reminders; **people from CO2**; **energy** for the Energy dashboard or PowerCalc.
- **Learns from you**: confirm or correct suggestions, calibrate setpoints, occupancy and fans.

## What it detects

| Situation | How it's recognised |
| :--- | :--- |
| **Heating** (heater or heat pump) | Warming faster than the building shell explains |
| **Cooling** (aircon) | Temperature dropping fast while humidity lags, or regular compressor cycling in humidity |
| **Dehumidifying** (dry mode or dehumidifier) | Humidity falling hard while the temperature barely moves |
| **Humidifying** | Humidity rising hard with doors and windows closed, and no shower in the zone |
| **Extractor fan / HVAC ventilate** | CO2 clearing faster than natural leakage with doors and windows known to be closed |
| **Air filtering** (aircon filter / purifier) | PM/VOC improving while CO2 is *not* clearing (filters don't remove CO2) |
| **Open door/window** | A door/window sensor, or two signs of outside air: temperature and absolute humidity heading to outdoor levels, CO2 flushing out |
| **Heat leaking in** | Outside warmer, and the room warming at the rate the shell explains |
| **Solar gain** | Warming with the sun up while it's warm outside, or while the room is already well above outdoor on a mild day |
| **Heat loss** | Outside colder, and the room cooling at the rate the shell explains |
| **Appliance heat/moisture** | A plugged-in fridge, dryer, oven etc. explains the change |
| **Idle** | Nothing much changing |

Without door or window information, a fast CO2 drop could be a fan or an open window. In that case the integration says so and splits the suggestion between the two, rather than guessing a fan.

## Rooms and zones

Your first setup is the **main room**, the space's own sensors, and you pick its Home Assistant area. Under **Configure → Add a room** you can add more areas, each with its own:

- temperature, humidity, CO2 and air-quality sensors, plus door/window sensors
- **air volume** (m³), used for CO2, occupancy and fan airflow
- **known air gaps** (none, small, large) with notes. Leakier rooms start with faster heat and air exchange until it's learned.
- room type. **Bathrooms get shower detection.**

**Room links (zones):** choose which rooms flow into each other: an open doorway, a hallway, a gap under a door. Linked rooms form a zone.
- An appliance in a room without sensors, like a bathroom fan, is observed by a sensored room in the same zone, like the living room whose CO2 it clears.
- A shower in the zone stops the other rooms from blaming a humidifier for the rising humidity.

Each room gets its own device in its area. Sensors that compare it with the main room (temperature, humidity, absolute humidity) show things like the laundry versus the living room. Link a dryer's plug to the laundry and its heat and moisture are learned in that room.

## HVAC units: pick H, V and/or AC

| Function | Modes |
| :--- | :--- |
| Heat | heat |
| Air condition | cool, dehumidify (dry) |
| Ventilate | ventilate (fresh air, lowers CO2), filter (indoor recirculation, cleans particulates only) |

### Mode vs state, and cycling

Every appliance has a **mode** (heat / cool / dry / ventilate / filter / on / off) and a **state**:
- `active`: running and driving the temperature
- `idle`: on, but resting
- `off`

When the room falls, rises a little and falls again, the aircon stays in cool mode while its state cycles between active and idle. The midpoint of those highs and lows becomes the **detected setpoint**, kept within the min/max you enter.

**Cycling you can't see in the temperature.** In an apartment or other heavy building, a cooling unit can run all night while the temperature hardly moves, because neighbouring rooms and the concrete keep feeding heat back in. The compressor still shows in **humidity**: the coil pulls moisture out while it runs, and moisture comes back while it rests, giving a regular swing every 15–25 minutes. The integration detects that pattern. The unit then follows the compressor between active and idle, and energy is charged for the real on-time. It also learns the **thermostat offset** between your room sensor and the unit's own thermostat.

The state sensor's `idle_reason` tells you which kind of idle it is: holding the setpoint, between compressor cycles, or setpoint already satisfied (compressor off, fan only).

### Manual mode, timeouts and "switched off"

Use the appliance's **Manual mode** and **Setpoint** entities to tell it what you set on the unit. Per appliance you choose:
- **Manual timeout**: revert to automatic detection after N minutes, or never.
- **Switched-off detection**: in manual heat or cool, if the room drifts the wrong way past the setpoint for N minutes (with no cycling seen), the unit is marked off and returns to auto.

Both fire an `hvac_simulators_appliance_event` for your automations.

## Extractor fan groups

Add several extractor fans as one group: set the count and name each fan. It works out **how many are on** from how fast CO2 clears beyond natural leakage.
- **Which fans:** calibrate each fan on its own (the `calibrate` service with `fans: [Bathroom]`) and it tells you which ones are likely on.
- **Count only:** use the *Fans running (calibrate)* number.

## Plugged-in appliances (heat sources)

Add a **fridge, washer, dryer, oven/microwave, stove** or **general** appliance (TV, iron, steamer, hair dryer, soldering iron) and pick its smart plug's power sensor. You get:

- a **Status** sensor (active / idle / off) with cycles, 6-hour duty cycle, last active spell and last run length, from the plug's power (thresholds adjustable)
- its learned **heat effect** (°C/h and %/h per kW). Dryers and stoves start out assumed to add moisture. The effect is **removed before HVAC detection**, so a tumble dryer isn't read as heating or a humidifier.
- `binary_sensor.<room>_appliance_heat_or_moisture`: use it as a condition so humidifier/dehumidifier automations don't react to the dryer

This is deliberately basic. For full washing-machine program detection, use a dedicated integration like [WashData](https://github.com/3dg1luk43/ha_washdata).

## Differentials and rates

- **Indoor − outdoor:** temperature, relative humidity and **absolute humidity** (g/m³), which tells you whether opening a window will dry or humidify, plus CO2 above outdoor.
- **Rates of change** for temperature, humidity, CO2 and air quality over the main trend window.
- **Extra rate sensors**, hidden by default so enable the ones you want: since the last reading, and over 5, 10 and 15 minutes.
- **Raw and compensated** trends when plugged-in appliances are configured.

**Configure → Tuning → Update** can evaluate every time a sensor updates, instead of on a fixed interval.

## Efficiency record

Each sensored room keeps a day-by-day record of what the building did on its own, after removing HVAC and appliances:

| Sensor | Meaning |
| :--- | :--- |
| **Heat loss coefficient** | °C per hour the room drifts per °C of indoor-outdoor difference with everything off and closed (30-day median). Lower is better insulated. |
| Heat lost today | °C·h the room cooled passively |
| Heat leaked in today | °C·h it warmed from a warmer outside |
| Solar gain today | °C·h it warmed from the sun |
| Appliance heat today | °C·h added by plugged-in appliances |
| Ventilation air changes today | air changes from fans and airing out |

Home Assistant keeps their history, so you can compare rooms, seasons, or your old and new home.

## Effectiveness and filter reminders

While heating or cooling, the integration measures how hard the unit pushes the temperature beyond the passive drift. It compares that with a baseline built after the last filter clean, **at the same indoor/outdoor temperature gap**, so a heatwave doesn't look like a dirty filter. The result is rated **Excellent → Great → Good → OK → Bad → Terrible**.
- When it drops to your chosen level, you get a persistent notification, optionally a push through any `notify` service, and an `hvac_simulators_effectiveness_changed` event.
- After cleaning the filter, press **Filter cleaned** to start a new baseline.

## People from CO2

`generation = dCO2/dt + air_changes × (CO2 − outdoor CO2)`, divided by what one person breathes out at their activity level: sleeping, resting, active or exercising. The activity defaults to *sleeping* during your sleep hours.

To calibrate, use *People home (calibrate)* or `calibrate_occupancy`:
- Enter **0** while nobody is home. This teaches the room's natural air-change rate, which also sharpens fan detection.
- Enter the real head count to learn one person's output in that room.

## Setup

1. Install through HACS (**Integrations → ⋮ → Custom repositories →** `https://github.com/meyerjoshua123/ha_hvac_simulators`), then restart Home Assistant.
2. **Settings → Devices & services → Add integration → HVAC Simulators.**
3. Pick the main room's sensors and area. Only indoor temperature is required. Outdoor temperature (a sensor or a `weather` entity) is strongly recommended.
4. Add appliances. Later, under **Configure**, add rooms, room links, more appliances, and tuning (thresholds, trend window, update mode, solar margin, sleep hours, notifications).

## Teaching it

| Entity / service | Use |
| :--- | :--- |
| `button.<room>_suggestion_is_correct` | The current suggestion is right |
| `select.<room>_actual_mode_teach` | Pick what's really happening |
| `binary_sensor.<room>_needs_feedback` | On when an active suggestion is uncertain: a good moment to teach |
| `select.<appliance>_manual_mode`, `number.<appliance>_setpoint` | What you set on the unit (also teaches every 15 min) |
| `hvac_simulators.calibrate` | Record mode / state / setpoint / filter (clean, ok, dirty), or which fans are running |
| `hvac_simulators.calibrate_occupancy` | Head count + activity for a room |
| `hvac_simulators.teach`, `confirm`, `reset_learning`, `set_manual`, `reset_energy` | Service equivalents (target a room or appliance device) |

Rules drive the suggestions at first. As you teach it, a nearest-neighbour model trained on your examples takes a growing share of the decision (up to 75% by default). It also learns each room's heat-exchange rates and which appliance usually does what.

## Electricity

Each appliance gets a **Power** sensor (W) based on its rated power per mode, idle or fan-only power, and the number of fans running. There is also an **Energy** sensor (kWh, `total_increasing`) for the Energy dashboard. Using **PowerCalc** instead? Turn off *Calculate energy*, then point PowerCalc at the Power sensor, or at the **On** binary sensor with a fixed wattage.

## Limitations

- Everything is inferred from how rooms respond. Two appliances doing the same thing in the same room at the same time can't be told apart without teaching.
- Temperatures are handled in °C internally; °F and K sensors are converted.
- CO2-based estimates assume a reasonably well-mixed zone.
- Default thresholds come from a limited amount of real data. Teaching and calibration make the biggest difference.

## Development

```bash
python3.13 -m venv .venv313 && .venv313/bin/pip install pytest-homeassistant-custom-component
.venv313/bin/python -m pytest
```

The detection engine, learning, rooms, detectors, plugs, efficiency and simulator modules are pure Python with their own tests. The integration tests run inside a real Home Assistant core, and one replays a real overnight recording.

See [CHANGELOG.md](CHANGELOG.md) for changes.

---

> **About this project:** HVAC Simulators was designed and built with the help of [Claude Code](https://claude.com/claude-code), Anthropic's AI coding assistant, under the direction of [@meyerjoshua123](https://github.com/meyerjoshua123). All code is tested, but please treat it as a hobby project: [report issues](https://github.com/meyerjoshua123/ha_hvac_simulators/issues) if something looks wrong.
