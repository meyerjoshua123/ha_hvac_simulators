# HVAC Simulators

A Home Assistant custom integration that works out what your heating, cooling and ventilation are doing **without smart plugs on them**. It watches how your indoor climate changes, subtracts what the building and weather explain, and creates simulated appliances with modes, on/idle/off state, setpoints, power and energy. It learns from you: confirm or correct its suggestions and it gets better.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=meyerjoshua123&repository=ha_hvac_simulators&category=integration)

## What it detects

| Situation | How it's recognised |
| :--- | :--- |
| **Heating** (heater or heat pump) | Warming faster than the building shell explains |
| **Cooling** (aircon) | Temperature dropping fast while humidity lags behind |
| **Dehumidifying** (dry mode or dehumidifier) | Humidity falling hard, temperature only drifting slightly |
| **Extractor fan / HVAC ventilate** | CO2 clearing faster than natural air leakage, doors and windows closed |
| **Air filtering** (aircon filter / purifier) | PM/VOC improving while CO2 is *not* clearing (filters don't remove CO2) |
| **Open door/window** | Opening open, indoor moving towards outdoor temperature, or CO2 flushing out |
| **Heat leaking in** | Outside warmer, warming at the rate the shell explains |
| **Solar gain** | Warming while outdoor temp is at/above your *solar threshold* |
| **Heat loss** | Outside colder, cooling at the rate the shell explains |
| **Idle** | Nothing much changing |
| **Humidifying** | Humidity rising hard with doors and windows closed |
| **Appliance heat/moisture** | A plugged-in fridge, dryer or oven explains the change |

### HVAC units: pick H, V and/or AC

When adding an HVAC unit, tick what it can do. The modes follow from that:

| Function | Modes |
| :--- | :--- |
| Heat | heat |
| Air condition | cool, dehumidify (dry) |
| Ventilate | ventilate (fresh air, lowers CO2), filter (indoor recirculation, cleans particulates only) |

### Extractor fan groups

Add several extractor fans as one group: set the count, give each a name and airflow. It works out **how many are on** from how fast CO2 clears beyond natural leakage. To teach it:
- Run one named fan on its own and calibrate it (the `calibrate` service with `fans: [Bathroom]`). Once each fan is calibrated, it tells you **which** fans are likely on.
- Or just enter how many are running with the *Fans running (calibrate)* number.

### Heat sources (fridge, dryer, oven...)

Add appliances that heat or humidify the space and pick the **power sensor of their smart plug**. HVAC Simulators learns how many °C/h and %/h each kW adds. It only learns while no HVAC is running and doors and windows are closed, and splits the credit when several sources run at once. That effect is **removed before anything else is decided**, so a tumble dryer won't be read as heating or a humidifier. Use `binary_sensor.<space>_appliance_heat_or_moisture` as a condition to stop humidifier/dehumidifier automations reacting to it.

### Differentials and rates

Virtual sensors for indoor − outdoor temperature, relative humidity and **absolute humidity** (g/m³; tells you whether opening a window will dry or humidify), CO2 above outdoor, and rates of change for temperature, humidity, CO2 and air quality. With heat sources configured there are raw and compensated versions of the trends.

### Cycling: mode vs status

Every appliance has a **mode** (heat / cool / dry / ventilate / filter / on / off) and a **state**:

- `active`: compressor or element running and driving the temperature
- `idle`: on, resting at its setpoint between cycles
- `off`

If the temperature falls, rises a little, falls again and so on, the aircon stays in **cool** mode while its state cycles between `active` and `idle`. The midpoint of those highs and lows becomes the **detected setpoint**, kept within the min/max range you enter. The unit only counts as `off` once the temperature drifts beyond the setpoint band. A fixed-speed unit also counts as `off` if it stays idle longer than the max idle time; an inverter unit can hold its setpoint indefinitely.

### Effectiveness and filter reminders

While heating or cooling, the integration measures how hard the unit pushes the temperature beyond the passive drift. It compares that with a baseline built after the last filter clean, **at the same indoor/outdoor temperature gap**, so a heatwave doesn't look like a dirty filter. The result is rated **Excellent → Great → Good → OK → Bad → Terrible**.

When the rating drops to your chosen level you get a persistent notification, and optionally a push through any `notify` service. An `hvac_simulators_effectiveness_changed` event is also fired for your own automations. After you clean the filter, press **Filter cleaned** to start a new baseline.

### People from CO2

With a CO2 sensor it estimates how many people are home using a CO2 mass balance:
`generation = dCO2/dt + air_changes × (CO2 − outdoor CO2)`.
People breathe out different amounts depending on activity (sleeping < resting < active < exercising). The activity defaults to *sleeping* during your sleep hours, you can override it, and the sensor attributes show the range from "everyone exercising" to "everyone asleep".

**Calibrate it** with the *People home (calibrate)* number or the `calibrate_occupancy` service:
- Enter **0** while nobody is home. It learns how fast your space airs out naturally, which also sharpens the extractor-fan detection.
- Enter the real head count with an activity level. It learns how much CO2 one person adds in your space.
Calibrations are combined (median), so more is better.

## Setup

1. Install through HACS (custom repository → Integration), restart Home Assistant.
2. **Settings → Devices & services → Add integration → HVAC Simulators.**
3. Choose sensors. Only indoor temperature is required. The other inputs each add something: humidity, outdoor temperature (a sensor or a `weather` entity), CO2, air quality (PM2.5/VOC), door/window sensors, and the solar threshold.
4. Add your appliances: HVAC units (pick heat / ventilate / air condition), heaters, extractor fan groups, dehumidifiers, humidifiers, purifiers, and heat sources with smart plugs. Enter power per mode, setpoint range and inverter flag. You can add, edit or remove them later under **Configure**.

**Configure → Tuning** has the thresholds, trend window, space volume, outdoor CO2, sleep hours, max idle time and notification settings.

## Teaching it

| Entity / service | Use |
| :--- | :--- |
| `button.<space>_suggestion_is_correct` | The current suggestion is right |
| `select.<space>_actual_mode_teach` | Pick what's really happening (e.g. *Cooling - Lounge AC*) |
| `binary_sensor.<space>_needs_feedback` | On when an active suggestion is uncertain: a good moment to teach |
| `select.<appliance>_manual_mode` | Tell it the mode you set on the unit (`auto` = infer). Also teaches every 15 min |
| `number.<appliance>_setpoint` | The setpoint on the unit |
| `hvac_simulators.calibrate` | Record mode / state / setpoint / filter (clean, ok, dirty) for an appliance at any time |
| `hvac_simulators.calibrate_occupancy` | Head count + activity |
| `hvac_simulators.teach`, `confirm`, `reset_learning`, `set_manual`, `reset_energy` | Service equivalents |

Rules drive the suggestions at first. As you teach it, a nearest-neighbour model trained on your examples takes a growing share of the decision (up to 75% by default). It also learns your building's heat-exchange rates and which appliance usually does what.

## Electricity

Each appliance gets a **Power** sensor (W). It uses the rated power for the current mode; when idle, an inverter draws a share of that and a fixed-speed aircon draws its fan power. There is also an **Energy** sensor (kWh, `total_increasing`) you can add straight to the Energy dashboard.

Using **PowerCalc** instead? Turn off *Calculate energy* for the appliance, then point PowerCalc at the Power sensor, or at the appliance's **On** binary sensor with a fixed wattage.

## Entities per space

Suggested mode (with reasons and candidate scores), suggestion confidence, temperature/humidity/CO2/air-quality trends, air changes per hour, estimated people, activity level, learned examples (with accuracy), needs feedback, door or window open.

## Entities per appliance

State (active/idle/off), mode, on, power, energy, detected setpoint, effectiveness, filter needs attention, manual mode, setpoint, filter cleaned, use detected setpoint. Fan groups add *Fans on* and *Fans running (calibrate)*. Heat sources have *Running* and *Heat effect*.

## Limitations

- Everything is inferred from how the room responds. Two appliances doing the same thing at the same time in one space can't be told apart without teaching.
- Temperatures are handled in °C internally; °F and K sensors are converted.
- CO2 occupancy assumes the space is reasonably well mixed; open doors to other rooms add error until calibrated.

## Development

```bash
python3.13 -m venv .venv313 && .venv313/bin/pip install pytest-homeassistant-custom-component
.venv313/bin/python -m pytest
```

The engine, learner, simulator, effectiveness and occupancy modules are pure Python and have their own unit tests. `tests/test_integration.py` runs the integration inside a real Home Assistant core.

See [CHANGELOG.md](CHANGELOG.md) for changes.
