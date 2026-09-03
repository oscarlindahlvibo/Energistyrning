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

**Resolved**: the four *actual* production entity IDs are confirmed and
wired into `DEFAULT_PV_ACTUAL_ENTITIES` in `smart_planner.py`:
`sensor.solis_s6_solis_pv_energy_1..4`. Confirmed by exporting one full
year (2025-08-28 to 2026-09-01) of this installation's real HA recorder
long-term statistics and cross-checking the per-string totals against two
independent sources: the aggregate `sensor.solis_s6_solis_pv_total_energy_generation`
(10 024.0 kWh vs. the four strings summing to 10 023.2 kWh) and a wholly
separate integration's own daily-total sensors,
`sensor.daily_pv_1..4_energy` (summing to 10 023.1 kWh). All three agree
to within ~1 kWh over a year -- high confidence.

These are cumulative kWh meters, not power sensors, so
`_statistics_to_pv_samples` reads the recorder's **"sum"** long-term
statistic (an hour-over-hour delta) rather than "mean" -- using "mean"
here would have silently treated a cumulative energy reading as an
instantaneous power value and produced nonsense. The originally-named
`sensor.energy_production_tomorrow[_2/_3/_4]` forecast sensors (daily
totals for *tomorrow*) were NOT found in this installation's real entity
list at all -- `DEFAULT_PV_FORECAST_ENTITIES` still names them as the
documented intent, but they need to be re-confirmed against the live
instance (the actual forecast-sensor names may differ, or the integration
providing them may not be installed/enabled) before shadow mode can use
daily-total forecasting for real. Until then, and for *today's* remaining
production specifically (no "tomorrow"-style total exists for today
either), PV forecasting still falls back to the degraded even-daylight
spread and is marked accordingly.

**Also flagged while verifying this**: `sensor.solis_s6_solis_household_load_total_energy`
had 11 unexplained drops to inconsistent (not near-zero) values over the
year, e.g. 70 824 -> 18 750 kWh, then climbing and dropping again to a
different value (20 793, then 20 831, then 21 216...) -- not a normal
"counter reset to 0", more consistent with a Solis integration
reconnect momentarily re-baselining the sensor. `total_energy_consumption`
covers the same physical quantity and had zero such anomalies over the
same year, so Smart Planner does not use `household_load_total_energy` at
all for now. Worth investigating on the live instance independently of
Smart Planner (check Solis integration connectivity logs around those
dates), but out of scope for this repo.

## Load forecast (`core/forecast_consumption.py`)

Two levels, both statistics/median-based (no ML in Fas 1, per the approved
plan):

- `forecast_load()`: the original v1 approach -- median energy for the
  same time-of-day window across the last 28 days (optionally excluding
  weekday/weekend mismatches). Kept for backward compatibility; no longer
  called by `smart_planner.py`.
- `forecast_load_temperature_aware()` (used by `smart_planner.py` since
  the pre-Fas-2 temperature-forecasting work): buckets by time-of-day +
  weekday/weekend + outdoor temperature (widening the temperature
  tolerance once if too few matches, then falling back to the plain
  time-of-day median, then a flat rate). Bergvärme makes outdoor
  temperature the dominant driver of load beyond time-of-day, which the
  v1 model ignored entirely. Each bucket also reports a robust
  (MAD-based) `uncertainty_kwh`, consumed by `core/reserve.py`'s dynamic
  reserve (see below). A recent-24h-actual bias multiplier (clamped
  0.5x-2x) nudges the forecast up/down when the house has been using
  noticeably more/less than usual lately.

Both fall back to "degraded" (flagged) output rather than a fabricated
number when there isn't enough history (`min_samples=3` by default).
House-load history is read from
`sensor.solis_s6_solis_household_load_power` (a *power* sensor) via Home
Assistant's long-term **statistics** (hourly mean power -> energy), not
raw state history -- much cheaper to query over a multi-week lookback
window.

**Outdoor temperature source: not yet configured on the live instance.**
No confirmed outdoor-temperature sensor or weather-forecast entity name
came out of the earlier backup scan (unlike the PV entities, which were
independently cross-validated -- see "PV forecast" below), so
`smart_planner.py` does not hardcode a guess. Instead, two new `text.*`
entities let the real names be supplied from the HA UI:
`text.energy_planner_outdoor_temperature_entity_id` (a `sensor.*` reporting
current outdoor temperature, read via statistics for history) and
`text.energy_planner_weather_forecast_entity_id` (a `weather.*` entity,
queried via the `weather.get_forecasts` service, hourly type, for future
slots' forecasted temperature). Left empty, the load forecast transparently
degrades to the time-of-day-only tier -- never an error, never a guessed
sensor name. **Action for the user**: find the real entity IDs (Developer
Tools -> States, filter by "temp"/"weather") and set both text entities
before the temperature bucketing actually activates.

## Forecast uncertainty / dynamic reserve (`core/reserve.py`)

Per-slot reserve target (kWh) above `min_soc_kwh`, driven by forecast
uncertainty rather than a fixed year-round SOC floor -- the explicit
ask was "ekonomiskt optimerad, inte ett fast SOC-golv". For each slot,
`compute_dynamic_reserve()` sums the load forecast's `uncertainty_kwh`
(plus half the PV forecast's `uncertainty_kwh`, since PV coming in lower
than expected also draws down the battery) over a configurable lookahead
window (`reserve_lookahead_hours`, default 6h), scaled by a safety factor
`reserve_z` (default 1.0).

This is wired into `optimizer.plan()` as a **soft shadow price**
(`BatteryConfig.reserve_cost_sek_per_kwh`, a new `number.*` entity,
default **0.0 = disabled**), not a hard constraint: the DP optimizer only
pays the penalty for dipping below (`min_soc_kwh` + that slot's reserve
target), so a big enough price spread can still make it sell into the
reserve on purpose -- exactly "prefer keeping a few extra kWh over
selling cheap and having to rebuy expensive later", but never an absolute
floor. Each `PlanSlot` now reports `reserve_target_kwh` and
`reserve_shortfall_kwh` so the shadow output shows when/how far the plan
dipped into its own margin.

**Not yet activated on the live instance**: `reserve_cost_sek_per_kwh`
defaults to 0.0, so the reserve mechanism computes and is visible in the
plan JSON but has no effect on the chosen actions until a real cost is
set. Tuning it (and `reserve_lookahead_hours`/`reserve_z`) against real
backtests is part of the still-outstanding walk-forward backtest work
(see "Backtest" below).

**PV forecast uncertainty: not yet populated.** `PvForecastPoint.uncertainty_kwh`
exists in the model and `compute_dynamic_reserve()` already reads it, but
`forecast_pv()`/`build_daily_shape_profile()` don't compute it yet --
today the reserve is driven by load uncertainty only (`pv_weight` term is
always 0 in practice). Populating it is part of the still-outstanding
seasonal PV model improvement (see "What's next" below).

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

**Real-data validation (`core/walkforward.py` + `core/run_real_backtest.py`)**:
unlike `core.backtest`, this replays many days of real history with NO
look-ahead -- a decision at time t only ever sees price/load/PV
information that would genuinely have been available at t (day-ahead
price publication boundary, load/PV forecasts built from history
strictly before t, a leak-free 24h-persistence temperature proxy in
place of an unavailable archived weather forecast). It re-plans once a
day (receding horizon / MPC-style) and executes each day's plan against
ACTUAL prices/PV/load, reporting: baseline vs. simulated cost (SEK and
%), grid import/export, battery throughput, same-day sell-then-rebuy-
dearer incidents, load forecast MAE/RMSE (overall and by temperature
bucket), PV forecast MAE/RMSE, lowest simulated SOC, and reserve-
shortfall incidents -- the exact set requested for the pre-Fas-2
validation. Covered by 10 tests in `tests/test_walkforward.py`,
including one that perturbs a later day's actual data and asserts it
does not change earlier decisions (the core no-look-ahead property).

Run it against exported CSVs (see "Local extraction script" below):

```bash
PYTHONPATH=custom_components/energy_planner/planner \
    python3 -m core.run_real_backtest ha_backtest_export --days 30 --days 90
```

**Not yet run against real data**: the walk-forward harness and its CLI
are built and verified against synthetic CSVs matching the extraction
script's format, but no actual export from the live instance has been
fed through it yet -- that's the next step, and its output (in
particular the load/PV forecast error and the sell-then-rebuy count)
should drive how much further work points 4-5 below actually need.

### Local extraction script

`extract_ha_backtest_data.py` (handed to the user, not committed to this
repo -- it's a one-off local tool, stdlib only, run directly against
`home-assistant_v2.db` or a backup `.tar`) exports at least 90 days
(as far as history allows) of: Nord Pool price (from HA state history
where retained, filled in from the public elprisetjustnu.se API for the
rest of the window -- Nord Pool doesn't normally get long-term
statistics), `sensor.vp_ute_justerad` / `sensor.vp_ute` (outdoor
temperature, plus a plausibility sanity check comparing the two), house
load, total PV and PV1-4, battery SOC, battery charge/discharge energy,
and grid import/export (Tibber preferred, Solis as a cross-check). Every
row records its own source and resolution -- nothing is silently
resampled to a fixed grid. Output: one CSV per category under
`--outdir`, directly consumable by `core.run_real_backtest`.

## HA entities Smart Planner reads

- `sensor.solis_s6_solis_battery_soc` (current SOC)
- The configured Nordpool entity (`nordpool_entity_id`, same as legacy planners)
- `sensor.energy_production_tomorrow[_2/_3/_4]` (PV forecast, daily totals --
  documented intent only, NOT confirmed to exist on this installation; see
  "PV forecast" above)
- `sensor.solis_s6_solis_pv_energy_1..4` (actual PV production, confirmed)
- `sensor.solis_s6_solis_household_load_power` (via statistics, for load history)
- `text.energy_planner_outdoor_temperature_entity_id` -- points at a
  `sensor.*` for outdoor temperature history (unconfigured by default; see
  "Load forecast" above)
- `text.energy_planner_weather_forecast_entity_id` -- points at a
  `weather.*` entity, queried via `weather.get_forecasts` for future slots'
  temperature (unconfigured by default)
- Config: `battery_capacity`, `battery_shutdown_soc`, `battery_max_soc` (existing),
  plus new Fas-1 config: `battery_max_charge_power_kw`,
  `battery_max_discharge_power_kw`, `battery_charge_efficiency`,
  `battery_discharge_efficiency`, `battery_cycle_cost_sek_per_kwh`,
  `grid_max_import_power_kw`, `grid_max_export_power_kw`,
  `reserve_cost_sek_per_kwh`, `reserve_lookahead_hours`, `reserve_z`,
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

## Verified against a real year of backup data

Cross-checked the assumptions above against one full year (2025-08-29 to
2026-09-01) of this installation's actual HA recorder long-term
statistics, exported locally and analyzed offline (never uploaded as a
raw backup -- see the extraction scripts used during Fas 1 development).

- **Battery round-trip efficiency, measured: 92.0-92.3%** (two independent
  sensor pairs: `sensor.solis_s6_solis_total_battery_charge_energy` /
  `..._discharge_energy`, and `sensor.solis_total_energy_charged` /
  `..._discharged`). The Fas-1 defaults (`battery_charge_efficiency` /
  `battery_discharge_efficiency`, `number.py`) were 95%/95% (90.25% round
  trip); bumped to **96%/96% (92.16% round trip)** to match.
- **PV production entities remain solidly cross-validated**: the four
  `sensor.solis_s6_solis_pv_energy_1..4` sum to 10,023 kWh/year, matching
  both `pv_total_energy_generation` (10,023 kWh) and the independent
  `sensor.daily_pv_1..4_energy` tracker (10,023 kWh) to within a few kWh.
- **SOC-calibration drift** (a battery not cycled through its full 10-100%
  range periodically can under-report usable capacity) is a real concern
  the user raised, but could NOT be verified from this export:
  `sensor.solis_s6_solis_battery_soc`, `..._battery_soh`, and
  `sensor.solis_remaining_battery_capacity` all have zero rows in the
  long-term statistics table for the whole year, despite existing as
  entities. The same pattern (a metadata row with no/broken statistics)
  is how HA marks a sensor it has automatically excluded from statistics
  after detecting erratic behavior -- exactly what happened separately to
  `household_load_total_energy` (see below). **Action for the user**:
  check Settings -> System -> Statistics in HA for a "fix issue" prompt on
  the SOC/SOH sensors; if present, that independently confirms the
  calibration-drift suspicion and needs fixing on the Solis/HA side
  before Smart Planner (or anything else) can trust `battery_soc` at all.
- **`household_load_total_energy` is confirmed broken**: 11 unexplained
  drops to inconsistent (non-zero, non-matching) values over the year,
  e.g. 70,824 -> 18,750 kWh then climbing and dropping again to 20,793,
  20,831, 21,216... not a normal counter reset. `total_energy_consumption`
  (used by Smart Planner) had zero such anomalies over the same year.
- **Grid import/export**: the `tibber:`-prefixed daily statistics only
  cover Dec 2025-Aug 2026 (~8 months, not comparable to Solis's full-year
  totals). The one Tibber Pulse sensor that does cover the full year,
  `sensor.tibber_pulse_*_accumulated_production`, shows 3,964 kWh export
  vs. Solis's 5,269 kWh for the same period -- a real ~25% gap worth
  investigating on the live instance (meter placement, missing data
  windows), not resolved here.
- **Grid power limit -- found and fixed.** Peak hourly grid power over the
  year was **25,314 W** (Tibber Pulse), i.e. ~36.7 A/phase on a 3-phase
  230V service -- against a confirmed **20 A** service fuse
  (huvudsäkring), so the property has already been drawing roughly
  **1.8x its rated fuse current** at times. The optimizer previously had
  no concept of a grid power limit at all. Now implemented:
  `BatteryConfig.max_grid_import_power_kw` / `max_grid_export_power_kw`
  (`core/models.py`), enforced in `optimizer.py` by excluding any
  charge/discharge candidate action that would push that slot's grid
  import or export power over the configured cap -- charging is only
  filtered against the import cap (it can't affect export), discharging
  only against the export cap, and the idle action is never filtered (a
  house-load-driven overage the battery can't help isn't grounds to
  make the slot infeasible). Configured via two new number entities,
  `grid_max_import_power_kw` / `grid_max_export_power_kw`
  (`number.py`), both defaulting to **15.9 kW = 23 A x 230V x 3 phases**
  -- per the user, running at ~23 A briefly is fine, but not for
  extended stretches, so the default targets a sustained-safe level
  rather than the fuse's bare rated limit (which tolerates brief spikes
  above 20 A but not sustained ones). Covered by
  `tests/test_optimizer.py::TestGridPowerLimit` and
  `tests/test_battery_math.py`'s new `BatteryConfig.validate()` cases.
  `None` (unconfigured) means no limit is enforced -- backward compatible
  with every existing test and the shadow-mode default until the two new
  number entities are set on the live instance.

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

Before Fas 2 (bergvärme/Ohmigo/SG Ready), per explicit instruction. Status
of the pre-Fas-2 spec's 7 points:

1. **Temperature-sensitive load forecast -- done.** See "Load forecast"
   above (`forecast_load_temperature_aware()`, wired into
   `smart_planner.py`). Inactive until the two new `text.*` entities are
   set on the live instance (no confirmed temperature/weather entity name
   exists yet -- see point 3's lesson: don't guess a name from training
   data when it can be wrong).
2. **Forecast uncertainty / dynamic reserve -- done.** See "Forecast
   uncertainty / dynamic reserve" above. `reserve_cost_sek_per_kwh`
   defaults to 0.0 (no effect) until tuned against real backtests.
3. **Verify tomorrow's PV forecast entities -- investigated, unresolved.**
   The originally-described `sensor.energy_production_tomorrow[_2/_3/_4]`
   were NOT found in a real export of this installation's entities (see
   "PV forecast" above) -- confirming the user's explicit caution not to
   assume they exist. No alternative daily-total-forecast source has been
   identified yet either. Until a real source is confirmed (or its
   absence is confirmed final), PV forecasting stays on the even-daylight
   fallback, always marked `is_degraded`. **Needs**: another entity scan
   on the live instance specifically for `weather.*` forecast attributes
   or any solar-forecast integration (e.g. Forecast.Solar, Solcast) that
   might already be installed.
4. **Improve the seasonal PV model -- not started.** Blocked on point 3:
   without a confirmed forecast-total source, there's no "tomorrow's
   external weather/PV forecast" component to blend in. The
   recent-14-30-days + same-period-prior-year + sunrise/sunset pieces
   don't depend on point 3 and could be built independently, but haven't
   been.
5. **Optimize using forecast error, not just point forecasts -- partially
   done.** Load uncertainty flows end-to-end into the reserve (point 2).
   PV uncertainty does not yet (`PvForecastPoint.uncertainty_kwh` is
   always 0.0 today -- see "Forecast uncertainty / dynamic reserve"
   above), so low-confidence PV days don't yet make the plan more
   conservative the way low-confidence load days do.
6. **30+ day real-data backtest with statistics -- tooling built, awaiting
   a real export.** The walk-forward harness (`core/walkforward.py`),
   its CLI (`core/run_real_backtest.py`), and the local extraction
   script are all built, tested, and verified end-to-end against
   synthetic data (see "Backtest" above) -- computing every statistic
   requested, with no look-ahead. What's still missing is running it
   against an actual export from the live instance and analyzing the
   result.
7. **Shadow mode only -- holds.** No Solis/`slot_N_*` writes anywhere in
   this work. Physical control stays off the table until points 1, 3, and
   6 are in a good enough state, per explicit instruction.

Other, longer-standing items:

- Bergvärme integration (Fas 2) -- needs `ohmigo_planner.py` and
  `update_ohmigo_action.py` added to this repo first; not present here yet
  (see `docs/legacy-scripts.md`).
- Wallbox/Tesla true-solar-surplus charging (Fas 3).
- Investigate the `household_load_total_energy` re-baselining anomaly on
  the live instance (see "PV forecast" above) -- unrelated to Smart
  Planner directly, but worth a look.
- "What would Smart Planner have done vs. what the active planner did"
  comparison sensor.
- Confirm whether `battery_soc`/`battery_soh`/`solis_remaining_battery_capacity`
  show a "fix issue" prompt under Settings -> System -> Statistics in HA
  (see "Verified against a real year of backup data" above) -- would
  confirm the SOC-calibration-drift concern the user raised, and needs
  fixing before those entities can be trusted by Smart Planner or
  anything else.
