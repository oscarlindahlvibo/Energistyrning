"""Walk-forward (receding-horizon) backtest for Smart Planner.

Unlike `core.backtest` (which replays a single horizon with perfect
foresight, to validate the optimizer's own energy-balance math), this
module simulates how Smart Planner would actually have behaved over many
days of REAL history: it re-plans periodically using only information
that would genuinely have been available at each decision point, then
scores the resulting decisions against what actually happened.

No Home Assistant imports. No look-ahead by construction:

- Prices for a slot are only usable once "published" -- a decision made at
  time t only ever sees price slots up to the end of the day *after* t's
  own calendar day (mirrors real day-ahead Nord Pool publication; the
  exact cutoff time doesn't matter for this bound, since even the most
  generous real-world case never reveals day-after-tomorrow's prices).
- The load forecast is built from load history strictly ending before the
  decision time (`forecast_load_temperature_aware`, same function
  `smart_planner.py` uses live).
- Each future slot's temperature "forecast" is a simple persistence proxy
  (this slot's time 24h ago, repeated back in 24h steps until it lands on
  or before the decision time) -- NOT the real future temperature. No
  archived historical weather-forecast snapshots exist to backtest
  against yet (see docs/smart-planner.md), so this is the closest
  leak-free stand-in; it is intentionally naive and the resulting load
  forecast error partly reflects that, not just the bucket model itself.
- The PV forecast's daily total is a simple recent-average persistence
  estimate per source, computed only from production strictly before the
  decision time. Production (`smart_planner.py`) does not have even this
  today (no confirmed forecast-total entity exists -- see the PV-forecast
  gap in docs/smart-planner.md, where it would fall back to 0), so this
  backtest is deliberately a bit more capable than production currently
  is, in order to produce a meaningful PV forecast error measurement
  (requirement 10) rather than a trivial "always predicts zero" result.

Execution model (receding horizon / MPC-style): a plan computed at a
decision point is executed slot-by-slot against ACTUAL prices/PV/load
until the next decision point, using the same physical energy-balance
function the optimizer itself uses (`optimizer._evaluate_action`) so
replaying a plan against reality is scored identically to how the
optimizer scored it against its own forecast. If the plan's chosen
charge/discharge for a slot turns out infeasible against the simulated
SOC actually reached (because reality diverged from the forecast), it is
clamped to what's physically possible, exactly as a real BMS would.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import statistics

from . import battery_math, optimizer
from .forecast_consumption import HistoricalSample, forecast_load_temperature_aware
from .forecast_pv import (
    HistoricalPvSample,
    PvSourceForecast,
    build_daily_shape_profile,
    forecast_pv,
)
from .models import BatteryConfig, PricePoint, SlotState
from .reserve import compute_dynamic_reserve


@dataclasses.dataclass(frozen=True)
class WalkforwardConfig:
    """Tunables for the walk-forward simulation itself (not the optimizer)."""

    replan_hour_local: int = 13
    """Local hour of day at which a new plan is computed (matches Nord
    Pool's real day-ahead publication time, roughly early afternoon)."""
    lookback_days_load: int = 60
    lookback_days_pv_profile: int = 21
    lookback_days_pv_total: int = 14
    """Window for the naive recent-average PV daily-total persistence
    estimate (see module docstring)."""
    temp_tolerance_c: float = 3.0
    min_samples: int = 3
    reserve_lookahead_hours: float = 6.0
    reserve_z: float = 1.0
    pv_bucket_minutes: int = 15


@dataclasses.dataclass
class ExecutedSlot:
    """One slot as actually simulated (planned action x actual PV/load)."""

    start: dt.datetime
    end: dt.datetime
    state: SlotState
    planned_charge_kwh: float
    planned_discharge_kwh: float
    executed_charge_kwh: float
    executed_discharge_kwh: float
    clamped: bool
    """True if the planned action had to be reduced because the simulated
    SOC (having diverged from the forecast SOC path) made it infeasible."""
    soc_before_kwh: float
    soc_after_kwh: float
    actual_pv_kwh: float
    actual_load_kwh: float
    forecast_pv_kwh: float
    forecast_load_kwh: float
    import_price_sek_per_kwh: float
    export_price_sek_per_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    cost_sek: float
    reserve_target_kwh: float
    reserve_shortfall_kwh: float
    decision_time: dt.datetime
    """Which day's plan this slot was executed from -- groups slots for
    the same-day sell-then-rebuy check."""
    actual_temperature_c: float | None = None
    """This slot's real outdoor temperature (not the persistence proxy
    used at decision time) -- for bucketing load forecast error by
    temperature range after the fact, never for making the decision."""


@dataclasses.dataclass
class DayUnavailable:
    """One day's decision point where the optimizer could not produce a plan."""

    decision_time: dt.datetime
    error_code: str
    error_message: str


@dataclasses.dataclass
class WalkforwardResult:
    """Outcome of a full `run_walkforward` run: executed slots plus stats."""

    executed_slots: list[ExecutedSlot]
    unavailable_days: list[DayUnavailable]
    soc_initial_kwh: float

    @property
    def total_cost_sek(self) -> float:
        """Simulated Smart Planner cost across the whole run."""
        return sum(s.cost_sek for s in self.executed_slots)

    @property
    def total_grid_import_kwh(self) -> float:
        """Simulated grid import across the whole run."""
        return sum(s.grid_import_kwh for s in self.executed_slots)

    @property
    def total_grid_export_kwh(self) -> float:
        """Simulated grid export across the whole run."""
        return sum(s.grid_export_kwh for s in self.executed_slots)

    @property
    def total_battery_throughput_kwh(self) -> float:
        """Charge + discharge kWh actually moved through the battery.

        The "cycled kWh" figure requirement 6 asks for.
        """
        return sum(
            s.executed_charge_kwh + s.executed_discharge_kwh
            for s in self.executed_slots
        )

    @property
    def min_soc_kwh(self) -> float:
        """Lowest simulated SOC reached anywhere in the run."""
        if not self.executed_slots:
            return self.soc_initial_kwh
        return min(s.soc_after_kwh for s in self.executed_slots)

    @property
    def reserve_shortfall_incidents(self) -> list[ExecutedSlot]:
        """Slots where the plan dipped below its own dynamic reserve target."""
        return [s for s in self.executed_slots if s.reserve_shortfall_kwh > 1e-9]

    def load_forecast_errors(self) -> list[tuple[float, float, float | None]]:
        """(forecast_kwh, actual_kwh, actual_temperature_c) per executed slot."""
        return [
            (s.forecast_load_kwh, s.actual_load_kwh, s.actual_temperature_c)
            for s in self.executed_slots
        ]

    def load_mae(self) -> float:
        """Mean absolute error of the load forecast, in kWh."""
        errors = [abs(f - a) for f, a, _ in self.load_forecast_errors()]
        return statistics.mean(errors) if errors else 0.0

    def load_rmse(self) -> float:
        """Root-mean-square error of the load forecast, in kWh."""
        errors = [(f - a) ** 2 for f, a, _ in self.load_forecast_errors()]
        return statistics.mean(errors) ** 0.5 if errors else 0.0

    def load_forecast_error_by_temperature_bucket(
        self,
    ) -> dict[str, dict[str, float | int]]:
        """MAE/RMSE of the load forecast, split by actual temperature range.

        Buckets: <-5, -5..0, 0..5, 5..10, >10 (deg C). Slots with no known
        actual temperature are grouped under "unknown" rather than
        silently dropped.
        """

        def _bucket(temp_c: float | None) -> str:
            if temp_c is None:
                return "unknown"
            if temp_c < -5:
                return "<-5"
            if temp_c < 0:
                return "-5..0"
            if temp_c < 5:
                return "0..5"
            if temp_c < 10:
                return "5..10"
            return ">10"

        by_bucket: dict[str, list[tuple[float, float]]] = {}
        for forecast_kwh, actual_kwh, temp_c in self.load_forecast_errors():
            by_bucket.setdefault(_bucket(temp_c), []).append((forecast_kwh, actual_kwh))

        result: dict[str, dict[str, float | int]] = {}
        for bucket, pairs in by_bucket.items():
            errors = [abs(f - a) for f, a in pairs]
            sq_errors = [(f - a) ** 2 for f, a in pairs]
            result[bucket] = {
                "n": len(pairs),
                "mae_kwh": statistics.mean(errors),
                "rmse_kwh": statistics.mean(sq_errors) ** 0.5,
            }
        return result

    def pv_mae(self) -> float:
        """Mean absolute error of the PV forecast, in kWh."""
        errors = [abs(s.forecast_pv_kwh - s.actual_pv_kwh) for s in self.executed_slots]
        return statistics.mean(errors) if errors else 0.0

    def pv_rmse(self) -> float:
        """Root-mean-square error of the PV forecast, in kWh."""
        errors = [
            (s.forecast_pv_kwh - s.actual_pv_kwh) ** 2 for s in self.executed_slots
        ]
        return statistics.mean(errors) ** 0.5 if errors else 0.0

    def sell_then_rebuy_incidents(self) -> int:
        """Count "sold cheap, had to buy back dearer" incidents.

        A sell event later followed, same calendar day (in the slot's own
        local time), by a grid import at a strictly higher price than
        that sell -- the pattern requirement 7 asks about.
        """
        by_day: dict[dt.date, list[ExecutedSlot]] = {}
        for s in self.executed_slots:
            by_day.setdefault(s.start.date(), []).append(s)

        incidents = 0
        for day_slots in by_day.values():
            day_slots = sorted(day_slots, key=lambda s: s.start)
            sells = [
                s
                for s in day_slots
                if s.grid_export_kwh > 1e-9
                and s.state in (SlotState.SELL, SlotState.SELL_EXCESS)
            ]
            for sell in sells:
                later_imports = [
                    s
                    for s in day_slots
                    if s.start > sell.start
                    and s.grid_import_kwh > 1e-9
                    and s.import_price_sek_per_kwh > sell.export_price_sek_per_kwh
                ]
                if later_imports:
                    incidents += 1
        return incidents


def _actual_kwh_for_slot(
    samples: list[HistoricalSample] | list[HistoricalPvSample],
    start: dt.datetime,
    end: dt.datetime,
) -> float:
    """Sum actual energy overlapping [start, end), pro-rated by overlap."""
    total = 0.0
    for sample in samples:
        overlap_start = max(sample.start, start)
        overlap_end = min(sample.end, end)
        if overlap_end <= overlap_start:
            continue
        span = (sample.end - sample.start).total_seconds()
        if span <= 0:
            continue
        fraction = (overlap_end - overlap_start).total_seconds() / span
        total += sample.energy_kwh * fraction
    return total


def _persistence_temperature_for_slot(
    slot_start: dt.datetime,
    decision_time: dt.datetime,
    temperature_actual: dict[dt.datetime, float],
) -> float | None:
    """Build a naive leak-free temperature 'forecast' via 24h persistence.

    Uses the same hour 24h ago, stepping back another 24h at a time until
    landing on or before the decision time (so a slot 30h into the
    horizon uses the actual temperature from 6h *before* the decision
    time, not a future value).
    """
    candidate = slot_start
    for _ in range(10):  # generous cap; 10 days back is always enough
        candidate = candidate - dt.timedelta(hours=24)
        if candidate > decision_time:
            continue
        hour_start = candidate.replace(minute=0, second=0, microsecond=0)
        if hour_start in temperature_actual:
            return temperature_actual[hour_start]
        if candidate <= decision_time:
            # No exact hour match this far back either -- give up rather
            # than walk back indefinitely; the caller's fallback chain
            # (time-of-day only, then flat rate) handles a None here.
            return None
    return None


def _recent_daily_pv_total(
    samples: list[HistoricalPvSample], as_of: dt.datetime, lookback_days: int
) -> float | None:
    """Mean of full calendar-day PV totals over recent days before `as_of`.

    Covers the last `lookback_days` days strictly before `as_of` -- the
    backtest's naive persistence stand-in for a real forecast-total
    source (see module docstring).
    """
    by_day: dict[dt.date, float] = {}
    cutoff = as_of - dt.timedelta(days=lookback_days)
    for sample in samples:
        if not (cutoff <= sample.start < as_of):
            continue
        day_key = sample.start.date()
        by_day[day_key] = by_day.get(day_key, 0.0) + sample.energy_kwh
    if len(by_day) < 3:
        return None
    return statistics.mean(by_day.values())


def _known_price_horizon(
    prices: list[PricePoint], decision_time: dt.datetime
) -> list[PricePoint]:
    """Return prices usable at `decision_time`, respecting the publication boundary.

    From now through the end of the day *after* decision_time's own
    calendar day -- the day-ahead Nord Pool publication boundary. Never
    includes anything from the day after that, regardless of
    decision_time's hour within its day.
    """
    horizon_end = dt.datetime.combine(
        decision_time.date() + dt.timedelta(days=2),
        dt.time.min,
        tzinfo=decision_time.tzinfo,
    )
    return [p for p in prices if p.start >= decision_time and p.end <= horizon_end]


def run_walkforward(
    *,
    prices: list[PricePoint],
    load_samples: list[HistoricalSample],
    pv_actual_total: list[HistoricalPvSample],
    pv_sources_for_profile: dict[str, list[HistoricalPvSample]],
    temperature_actual: dict[dt.datetime, float],
    soc_initial_kwh: float,
    battery_config: BatteryConfig,
    start: dt.datetime,
    end: dt.datetime,
    config: WalkforwardConfig | None = None,
) -> WalkforwardResult:
    """Run the walk-forward simulation over [start, end).

    `temperature_actual` must be keyed by hour-start `datetime` (matching
    how `_statistics_to_temperature_samples` in `smart_planner.py` returns
    it). `pv_sources_for_profile` is per-string actual production, used
    only to build each source's historical shape/persistence total;
    `pv_actual_total` is the combined actual PV production used to score
    executed slots against reality -- kept separate so a caller can use a
    different (coarser or finer) source for each without them needing to
    reconcile exactly.
    """
    config = config or WalkforwardConfig()
    prices = sorted(prices, key=lambda p: p.start)

    executed: list[ExecutedSlot] = []
    unavailable: list[DayUnavailable] = []
    sim_soc_kwh = soc_initial_kwh

    day = start.date()
    while True:
        decision_time = dt.datetime.combine(
            day, dt.time(hour=config.replan_hour_local), tzinfo=start.tzinfo
        )
        if decision_time >= end:
            break
        if decision_time < start:
            day += dt.timedelta(days=1)
            continue

        next_decision_time = dt.datetime.combine(
            day + dt.timedelta(days=1),
            dt.time(hour=config.replan_hour_local),
            tzinfo=start.tzinfo,
        )
        execute_until = min(next_decision_time, end)

        horizon_prices = _known_price_horizon(prices, decision_time)
        if not horizon_prices:
            day += dt.timedelta(days=1)
            continue

        slots = [(p.start, p.end) for p in horizon_prices]

        history_load_samples = [s for s in load_samples if s.end <= decision_time]
        slot_temperatures_c = [
            _persistence_temperature_for_slot(s, decision_time, temperature_actual)
            for s, _ in slots
        ]
        recent_start = decision_time - dt.timedelta(hours=24)
        recent_kwh = sum(
            s.energy_kwh
            for s in history_load_samples
            if s.start >= recent_start and s.end <= decision_time
        )
        load_forecast = forecast_load_temperature_aware(
            history_load_samples,
            slots,
            slot_temperatures_c,
            lookback_days=config.lookback_days_load,
            split_weekday_weekend=True,
            temp_tolerance_c=config.temp_tolerance_c,
            min_samples=config.min_samples,
            recent_actual_kwh_24h=recent_kwh if recent_kwh > 0 else None,
            fallback_kwh_per_hour=None,
        )

        profile_cutoff = decision_time - dt.timedelta(
            days=config.lookback_days_pv_profile
        )
        pv_sources: list[PvSourceForecast] = []
        profiles = {}
        for name, samples in pv_sources_for_profile.items():
            history_pv_samples = [
                s for s in samples if profile_cutoff <= s.start < decision_time
            ]
            profile = build_daily_shape_profile(
                history_pv_samples, bucket_minutes=config.pv_bucket_minutes, min_days=5
            )
            if profile is not None:
                profiles[name] = profile
            all_history_pv = [s for s in samples if s.start < decision_time]
            daily_total = _recent_daily_pv_total(
                all_history_pv, decision_time, config.lookback_days_pv_total
            )
            daily_total_kwh = {}
            if daily_total is not None:
                horizon_days = {p.start.date() for p in horizon_prices}
                daily_total_kwh = dict.fromkeys(horizon_days, daily_total)
            pv_sources.append(
                PvSourceForecast(name=name, daily_total_kwh=daily_total_kwh)
            )
        pv_forecast = forecast_pv(
            pv_sources,
            slots,
            profiles=profiles,
            bucket_minutes=config.pv_bucket_minutes,
        )

        reserve_kwh = compute_dynamic_reserve(
            slots,
            load_forecast,
            pv_forecast=pv_forecast,
            lookahead_hours=config.reserve_lookahead_hours,
            z=config.reserve_z,
        )

        outcome = optimizer.plan(
            prices=horizon_prices,
            pv_forecast=pv_forecast,
            load_forecast=load_forecast,
            battery_config=battery_config,
            current_soc_kwh=sim_soc_kwh,
            now=decision_time,
            reserve_kwh=reserve_kwh,
        )

        if not outcome.ok:
            unavailable.append(
                DayUnavailable(decision_time, outcome.error.code, outcome.error.message)
            )
            day += dt.timedelta(days=1)
            continue

        for plan_slot, forecast_pv_point, forecast_load_point in zip(
            outcome.result.slots, pv_forecast, load_forecast, strict=True
        ):
            if plan_slot.start < decision_time or plan_slot.start >= execute_until:
                continue
            duration_hours = (plan_slot.end - plan_slot.start).total_seconds() / 3600.0
            actual_pv_kwh = _actual_kwh_for_slot(
                pv_actual_total, plan_slot.start, plan_slot.end
            )
            actual_load_kwh = _actual_kwh_for_slot(
                load_samples, plan_slot.start, plan_slot.end
            )
            actual_temperature_c = temperature_actual.get(
                plan_slot.start.replace(minute=0, second=0, microsecond=0)
            )

            max_charge = battery_math.max_charge_in_kwh(
                sim_soc_kwh, duration_hours, battery_config
            )
            max_discharge = battery_math.max_discharge_out_kwh(
                sim_soc_kwh, duration_hours, battery_config
            )
            executed_charge = min(plan_slot.battery_charge_kwh, max_charge)
            executed_discharge = min(plan_slot.battery_discharge_kwh, max_discharge)
            clamped = (
                executed_charge < plan_slot.battery_charge_kwh - 1e-9
                or executed_discharge < plan_slot.battery_discharge_kwh - 1e-9
            )

            price_point = next(p for p in horizon_prices if p.start == plan_slot.start)
            evaluation = optimizer._evaluate_action(  # noqa: SLF001 -- same core package
                pv_kwh=actual_pv_kwh,
                load_kwh=actual_load_kwh,
                charge_in_kwh=executed_charge,
                discharge_out_kwh=executed_discharge,
                import_price=price_point.import_price_sek_per_kwh,
                export_price=price_point.export_price_sek_per_kwh,
                config=battery_config,
            )

            soc_before = sim_soc_kwh
            sim_soc_kwh = max(
                0.0,
                min(
                    battery_config.capacity_kwh,
                    sim_soc_kwh + executed_charge - executed_discharge,
                ),
            )

            executed.append(
                ExecutedSlot(
                    start=plan_slot.start,
                    end=plan_slot.end,
                    state=plan_slot.state,
                    planned_charge_kwh=plan_slot.battery_charge_kwh,
                    planned_discharge_kwh=plan_slot.battery_discharge_kwh,
                    executed_charge_kwh=executed_charge,
                    executed_discharge_kwh=executed_discharge,
                    clamped=clamped,
                    soc_before_kwh=soc_before,
                    soc_after_kwh=sim_soc_kwh,
                    actual_pv_kwh=actual_pv_kwh,
                    actual_load_kwh=actual_load_kwh,
                    forecast_pv_kwh=forecast_pv_point.energy_kwh,
                    forecast_load_kwh=forecast_load_point.energy_kwh,
                    import_price_sek_per_kwh=price_point.import_price_sek_per_kwh,
                    export_price_sek_per_kwh=price_point.export_price_sek_per_kwh,
                    grid_import_kwh=evaluation["grid_import_kwh"],
                    grid_export_kwh=evaluation["grid_export_kwh"],
                    cost_sek=evaluation["cost"],
                    reserve_target_kwh=plan_slot.reserve_target_kwh,
                    reserve_shortfall_kwh=plan_slot.reserve_shortfall_kwh,
                    decision_time=decision_time,
                    actual_temperature_c=actual_temperature_c,
                )
            )

        day += dt.timedelta(days=1)

    return WalkforwardResult(
        executed_slots=executed,
        unavailable_days=unavailable,
        soc_initial_kwh=soc_initial_kwh,
    )


def baseline_actual_cost_sek(
    prices: list[PricePoint],
    grid_import_actual: list[HistoricalSample],
    grid_export_actual: list[HistoricalSample],
) -> float:
    """Compute what actually happened, in SEK, from real metered grid import/export.

    Independent of any battery-control assumption -- the "faktisk/
    baseline kostnad" (requirement 1) to compare Smart Planner's
    simulated cost against.
    """
    cost = 0.0
    for price in prices:
        import_kwh = _actual_kwh_for_slot(grid_import_actual, price.start, price.end)
        export_kwh = _actual_kwh_for_slot(grid_export_actual, price.start, price.end)
        cost += import_kwh * price.import_price_sek_per_kwh
        cost -= export_kwh * price.export_price_sek_per_kwh
    return cost
