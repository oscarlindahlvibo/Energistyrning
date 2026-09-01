# Smart Planner (Fas 1: shadow mode)

Status: **shadow mode only**. Nothing in this feature writes to `slot_N_*`
entities or any Solis entity. The only thing that can ever control the
battery is the existing `update_battery_action.py`, unchanged.

## Architecture

```
custom_components/energy_planner/planner/
  core/                        <- pure Python, NO Home Assistant imports
    models.py                  <- PricePoint, PvForecastPoint, LoadForecastPoint,
                                   BatteryConfig, PlanSlot, PlanResult, PlanningError
    economics.py                <- SEK/kWh price normalization (spot -> import/export price)
    battery_math.py             <- kWh<->SOC, power-to-energy-per-slot conversions
    optimizer.py                 <- the actual planning algorithm (see below)
    forecast_consumption.py      <- LoadForecastProvider (historical median)
    forecast_pv.py                <- PvForecastProvider (daily total -> shaped curve)
    backtest.py                   <- replay historical data through the optimizer
  smart_planner.py               <- Home Assistant adapter (reads state, calls core,
                                     writes shadow sensors). The ONLY file here that
                                     imports Home Assistant.
```

`core/` is deliberately isolated so it can be unit tested with nothing but
the Python standard library -- no `homeassistant` package install, no
running HA instance. Because `custom_components/energy_planner/__init__.py`
imports `homeassistant.config_entries` at module level, importing
`core.*` the normal dotted way (`custom_components.energy_planner.planner.core...`)
would still require `homeassistant` to be installed. Tests avoid this by
adding `custom_components/energy_planner/planner` directly to `sys.path`
and importing `core` as a plain top-level package (see `tests/_bootstrap.py`).
Run them with:

```bash
python3 -m unittest discover -s tests -v
```

## How the optimizer works

Forward dynamic programming over a discretized battery SOC grid (default
step: 0.25 kWh -- more than enough resolution for a 51.2 kWh battery), one
step per `PricePoint`. For each price period, the optimizer explores every
feasible battery action for that period:

- Charge some amount (from 0 up to whatever the SOC headroom and the
  configured `max_charge_power_kw` allow *for that period's actual
  duration*).
- Discharge some amount (bounded the same way by `max_discharge_power_kw`
  and available energy above the minimum SOC).
- Do neither (idle).

For each candidate action it computes a full energy balance for the
period: PV first covers house load, then any remaining PV charges the
battery, then the grid fills whatever is still needed for load and/or
charging. Any battery discharge covers remaining load first; anything left
over (either from the battery or from surplus PV) is either sold (revenue
= amount x export price) or curtailed, whichever is worth more -- this is
the same choice the legacy planner's `sell-excess` vs `discard-excess`
states represent, but decided by the optimizer rather than picked in
advance.

Total cost for a period = `grid_import_kwh x import_price - export_revenue
+ (charge_kwh + discharge_kwh) x cycle_cost_sek_per_kwh`. The DP keeps,
for every reachable SOC value at every period, only the cheapest way to
have gotten there, and backtracks from the globally cheapest final state
to produce the plan. **No externally imported solver dependency** -- this
is plain Python.

### Explicitly NOT assumed

- **Period length.** Every `PricePoint` carries its own `start`/`end`;
  `duration_hours` is computed from that, never hardcoded as 0.25h. The
  optimizer is exercised in tests with 15-minute, 60-minute, and mixed
  15/60-minute price series, plus DST days with 92 and 100 quarter-hour
  periods (`tests/test_dst_handling.py`). `smart_planner.py` logs the
  actual period length(s) seen on every run specifically so this can be
  verified against real Nordpool data once deployed.
- **Ampere-to-kW.** `BatteryConfig.max_charge_power_kw` /
  `max_discharge_power_kw` are explicit kW config values (new
  `number.energy_planner_battery_max_charge_power_kw` /
  `..._battery_max_discharge_power_kw`), never derived from the existing
  amp-based `max_charge_current`/`max_discharge_current` (those stay
  exactly as they are, still used by the legacy planners and by
  `update_battery_action.py`).
- **A full sub-daily PV curve.** See "PV forecast" below.

### Failsafe behavior

`optimizer.plan(...)` returns a `PlanOutcome` with either `.result` or
`.error` set -- never raises for "normal" bad input, never guesses. It
refuses to plan (with a specific `error.code`) when: there is no price
data, prices are out of order or overlapping, SOC is missing or wildly out
of range, or the battery config itself is invalid (e.g. min SOC > max
SOC). A SOC value slightly outside the configured min/max band (e.g. just
after an unplanned discharge) is still planned for -- that is normal
operation, not an error.

## Price normalization (`core/economics.py`)

The optimizer core only ever sees two numbers per period, both already in
SEK/kWh: `import_price_sek_per_kwh` and `export_price_sek_per_kwh`. All
VAT/network-cost/network-compensation math happens exactly once, in
`smart_planner.py`, before building `PricePoint`s. No `* 1.25` / `* 10`
style magic constants inside the optimizer itself -- that was the bug
class found in the legacy `price_peak_planner.py` (see below).

**Assumption carried over from the legacy planners**: the Nordpool
integration's `value` is consumed directly as SEK/kWh, VAT-inclusive
(`get_nordpool_price_per_kwh_in_cent` exists in `planner/utils.py` but is
not actually called by any planner, legacy or new). `network_cost` /
`network_compensation` (existing config entities, öre/kWh) are converted
to SEK/kWh and added/subtracted once. If this VAT assumption turns out
wrong for this installation, `PriceConfig.vat_multiplier` /
`export_vat_multiplier` in `_price_config_from_ha()`
(`smart_planner.py`) is the one place to fix it.

## PV forecast (`core/forecast_pv.py`)

Confirmed today: four daily-total forecast sensors,
`sensor.energy_production_tomorrow[_2/_3/_4]` (kWh, one per PV
string/orientation). These are **daily totals for tomorrow**, not a
sub-daily curve.

`PvForecastProvider` (`forecast_pv()`) distributes a daily total across
the requested periods using a normalized historical *shape* profile built
separately per source (`build_daily_shape_profile`, from that source's own
historical actual-production data). If no profile is available yet for a
source (too little history, or -- see gap below -- the actual-production
entity isn't wired up), it falls back to an even spread across daylight
hours (06:00-20:00) and marks every affected `PlanSlot.is_degraded = True`.
The shadow sensors and plan JSON both surface `is_degraded` so this is
never silently presented as a precise forecast.

**Known Fas-1 gap**: the four *actual* production entity IDs (one per PV
source, needed to build each source's historical shape) are not yet
confirmed/wired into `smart_planner.py`
(`DEFAULT_PV_ACTUAL_ENTITIES` is currently empty). Until they are set,
PV forecasting for the whole horizon runs in the degraded even-spread
mode. **Action needed**: confirm the four actual-production entity IDs
(likely `sensor.energy_production_today[_2/_3/_4]` or similar -- verify
against the live HA instance) and fill in `DEFAULT_PV_ACTUAL_ENTITIES` in
`smart_planner.py`.

**Also note**: there is currently no forecast source at all for *today's*
remaining PV production (only tomorrow's daily total is confirmed to
exist as a sensor) -- today's PV forecast is built purely from the
historical shape profile applied against... no daily total for today,
which currently means today's PV forecast is 0 unless a profile-based
"today" total is added. This needs the same entity-ID confirmation as
above; flagged rather than guessed.

## Load forecast (`core/forecast_consumption.py`)

Simple median-per-time-of-day approach: for each requested period, look at
the same time-of-day window on each of the last 28 days (optionally
excluding weekday/weekend mismatches), and take the median energy
consumed in that window. Falls back to "degraded" (flagged) output rather
than a fabricated number when there isn't enough history (`min_samples=3`
by default). History is read from `sensor.solis_s6_solis_household_load_power`
(a *power* sensor) via Home Assistant's long-term **statistics** (hourly
mean power -> energy), not raw state history -- much cheaper to query over
a multi-week lookback window. No ML in Fas 1, per the approved plan.

## Backtest (`core/backtest.py`)

Replays a historical day's actual prices/PV/load through the same
optimizer (perfect foresight -- this validates the optimizer's decision
logic and energy-balance math, not forecast-provider accuracy). Run it
directly, no `homeassistant` install needed:

```bash
PYTHONPATH=custom_components/energy_planner/planner \
    python3 -m core.backtest tests/fixtures/example_day.json
```

`tests/fixtures/example_day.json` is a synthetic example day (winter PV
bell curve, morning/evening load bumps, a price spike 16:00-20:00). On
this fixture the backtest currently reports roughly a 50 SEK/day
improvement over a "do nothing with the battery" baseline -- see the tool's
own output for exact numbers, and treat the fixture as illustrative, not a
real historical day.

**Next step for real validation** (not yet built): a small script that
pulls a real day's actual Nordpool prices + actual PV/load from the
Recorder/statistics and feeds them through `run_backtest()`, so specific
"the old planner did X, here's what it should have done" days can be
checked. The building blocks (`_statistics_to_energy_samples`,
`_statistics_to_pv_samples` in `smart_planner.py`) already exist for this.

## HA entities Smart Planner reads

- `sensor.solis_s6_solis_battery_soc` (current SOC)
- The configured Nordpool entity (`nordpool_entity_id`, same as legacy planners)
- `sensor.energy_production_tomorrow[_2/_3/_4]` (PV forecast, daily totals)
- `sensor.solis_s6_solis_household_load_power` (via statistics, for load history)
- Config: `battery_capacity`, `battery_shutdown_soc`, `battery_max_soc` (existing),
  plus new Fas-1 config: `battery_max_charge_power_kw`,
  `battery_max_discharge_power_kw`, `battery_charge_efficiency`,
  `battery_discharge_efficiency`, `battery_cycle_cost_sek_per_kwh`,
  `network_cost`, `network_compensation` (existing, reused)

## HA entities Smart Planner writes (shadow only)

- `switch.energy_planner_smart_shadow_enabled` -- the on/off gate. Runs
  independently of `select.energy_planner_planner_state`: whichever real
  planner (basic/cheapest hours/price peak/off) is actually driving the
  house keeps doing so untouched; Smart Planner just also computes, in
  parallel, what it itself would have done.
- `sensor.energy_planner_smart_status` -- "available"/"unavailable" + error detail
- `sensor.energy_planner_smart_projected_cost` -- total SEK for the horizon
- `sensor.energy_planner_smart_planned_charge` / `_planned_discharge` (kWh)
- `sensor.energy_planner_smart_pv_forecast` / `_load_forecast` (kWh)
- `sensor.energy_planner_smart_grid_import` / `_grid_export` (kWh)
- `sensor.energy_planner_smart_next_action` -- state + reason for the next slot
- `sensor.energy_planner_smart_plan` -- full plan as a JSON attribute (`slots`),
  for building a Lovelace card

**Known caveat**: Home Assistant caps state attribute size (historically
around 16 KiB, logged as a recorder warning if exceeded, not a hard
crash). A 36-hour horizon at 15-minute resolution is 144 slots; the full
`slots` JSON attribute on `sensor.energy_planner_smart_plan` may approach
or exceed that limit depending on field verbosity. Not yet an issue on the
first real run this needs verifying against actual recorder logs once
deployed -- if it is, the fix is trimming fields or storing the full plan
via `Store` instead of as a state attribute, exposing only a short preview
on the sensor itself.

**Not yet built**: a "what did the currently active planner actually do,
in the same cost terms" comparison sensor. This needs re-scoring the
active planner's committed `slot_N_*` schedule through the same cost
function Smart Planner uses, which is a reasonably self-contained
follow-up once shadow mode has run for a few days.

## Update cadence

- Every 15 minutes (`async_track_time_interval`), unconditionally --
  `async_run_shadow_planner` itself checks the shadow-enabled switch and
  no-ops if it's off.
- Immediately on a SOC change or a PV forecast sensor change
  (`async_track_state_change_event`).
- Immediately on a Nordpool price update (registered once
  `nordpool_entity_id` is known, in `async_setup_entry`).
- Continuous power sensors (actual PV production, actual house load) are
  deliberately left to the 15-minute cadence rather than triggering a
  re-run on every reading -- they change too often for a per-event re-run
  to be useful.

## Known bugs in the legacy `price_peak_planner.py` (documented, NOT fixed in Fas 1)

Per explicit instruction: `price_peak_planner.py` is left completely
untouched in Fas 1 so the planner actually driving the house today is not
put at risk while Smart Planner is being built and verified. These are
still worth recording:

1. **Efficiency direction bug.** The arbitrage check multiplies the charge
   price by `price_peak_efficiency_factor` (e.g. 0.85) instead of dividing
   by it. Since efficiency is < 1, multiplying *lowers* the effective
   charge cost used in the comparison, making worse efficiency look like
   it makes arbitrage *easier* to justify -- backwards. See
   `tests/test_battery_math.py::test_source_energy_for_charge_applies_efficiency`
   for the correct (divide) behavior as implemented in Smart Planner's
   core.
2. **Hardcoded SOC targets.** Charge/inbetween periods target SOC 100,
   discharge periods target SOC 0, ignoring the configured
   `battery_max_soc`/`battery_shutdown_soc` entirely (unlike
   `basic_planner.py` and `cheapest_hours_planner.py`, which do read them
   correctly).
3. **No kWh-based sizing.** `battery_capacity` is configured but never
   used by this planner to decide how many kWh to move -- it only ever
   picks a fixed number of *hours* to charge/discharge, and lets
   `update_battery_action.py` ramp toward the (wrong) SOC target at max
   current for however long the window lasts.

## What's next (not in Fas 1)

- Bergvärme integration (Fas 2) -- needs `ohmigo_planner.py` and
  `update_ohmigo_action.py` added to this repo first; not present here yet
  (see `docs/legacy-scripts.md`).
- Wallbox/Tesla true-solar-surplus charging (Fas 3).
- Confirm the four PV actual-production entity IDs and today's PV forecast
  source (see gap above).
- Build the real-data backtest script (vs. the synthetic fixture).
- "What would Smart Planner have done vs. what the active planner did"
  comparison sensor.
