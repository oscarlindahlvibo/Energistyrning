# Legacy scripts and automations

What actually controls Solis today, what's dead code, and what can
eventually be retired once Smart Planner (see `docs/smart-planner.md`)
takes over. Written as part of the Fas 1 analysis so this is versioned
alongside the code it describes, instead of living only in chat history.

## Currently live, controls the battery

- **`update_battery_action.py`** (deployed as a `python_script` in the live
  HA config; a copy lives here at `examples/update_battery_action.py` for
  reference). Reads only `slot_1_*` (state/active/soc) and `slot_2`'s start
  as the current slot's end, and writes directly to the Solis TOU
  charge/discharge entities plus the grid feed-in limit switch. Triggered
  by `examples/update_battery_action_automation.yml`: every 10 minutes,
  plus on slot-1 state/active/datetime changes and on grid/battery power
  crossing certain thresholds. **This is the only thing that may ever
  write to Solis, in Fas 1 and beyond until explicitly changed.**
- **One of `basic_planner` / `cheapest_hours_planner` / `price_peak_planner`**,
  whichever `select.energy_planner_planner_state` is currently set to.
  These are the planners that fill `slot_N_*`, which
  `update_battery_action.py` then acts on. All three are functionally
  active code paths (unlike `dynamic_planner`, see below).

## Present but effectively dead

- **`dynamic_planner.py`**. Confirmed WIP: it only runs a diagnostic SQL
  query against the Recorder database (looking at `sensor.sun_next_dawn`
  history) and logs the result. It never writes to any `slot_N_*` entity.
  If `planner_state` is ever set to `"dynamic"`, the schedule simply stops
  updating -- whatever slots were last written (by a previous planner, or
  manually) stay in place.

## Referenced by the user, not present in this repository

These are described in the task but not part of this fork -- they live
(if they exist) as separate `python_scripts`/automations in the live HA
config, outside version control:

- **`ohmigo_planner.py`** -- bergvärme (heat pump) planning: looks at the
  first 12 hours of the next day's prices, finds four cheap hours, applies
  a fixed temperature offset.
- **`update_ohmigo_action.py`** -- executes the offset via Ohmigo/Shelly
  SG Ready relay.
- **`save_prod_and_estimates.py`** -- reads/stores the four PV production
  forecasts and their actual outcomes.

**Action needed before Fas 2 (bergvärme integration) can start**: add
these three files to this repository (e.g. under a new `legacy/` folder,
matching the pattern already used for `examples/`), so their actual
behavior can be read and modeled precisely instead of worked from
description alone.

## Entities Smart Planner shadow mode does NOT touch

For clarity, since this is easy to get nervous about: none of the Fas 1
Smart Planner code reads or writes any of the following, at any point:

- Any `time.solis_*` / `number.solis_*eh3p*` entity (Solis TOU config)
- `switch.grid_feed_in_power_limit_switch`
- Any `select.energy_planner_slot_N_state` / `number.energy_planner_slot_N_soc`
  / `datetime.energy_planner_slot_N_date_time_start` /
  `switch.energy_planner_slot_N_active`

It only ever reads telemetry (SOC, PV forecast, Nordpool price, load
history) and writes to its own new `sensor.energy_planner_smart_*` /
`switch.energy_planner_smart_shadow_enabled` entities.
