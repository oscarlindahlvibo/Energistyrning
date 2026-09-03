"""Deterministic energy-balance optimizer for Smart Planner.

Given price, PV forecast and load forecast for a rolling horizon, plus a
battery configuration and the current SOC, this computes a plan that
minimizes total cost (equivalently: maximizes economic outcome), taking
into account:

- energy balance per slot (PV + battery discharge + grid import
  = load + battery charge + grid export)
- SOC limits (min/max)
- charge/discharge power limits (translated to per-slot kWh via the
  slot's own duration -- never assumes 15 minutes)
- charge/discharge efficiency losses
- battery cycle cost (SEK/kWh throughput)
- a choice, when there is exportable surplus, between selling it and
  discarding it (curtailing), whichever is more valuable -- this replaces
  the legacy hard split between "sell-excess" and "discard-excess" being
  chosen up front.

Implementation: forward dynamic programming over a discretized SOC grid
(`BatteryConfig.soc_resolution_kwh`, default 0.25 kWh -- plenty for a
50+ kWh battery). No external solver dependency.

This module has NO Home Assistant imports and is fully unit-testable.
"""

from __future__ import annotations

import datetime as dt
import itertools
import math

from . import battery_math
from .models import (
    BatteryConfig,
    LoadForecastPoint,
    PlanningError,
    PlanOutcome,
    PlanResult,
    PlanSlot,
    PricePoint,
    PvForecastPoint,
    SlotState,
)

# Cost added (SEK) to any state combination the optimizer should never
# realistically choose, used only as a numerical guard -- not surfaced to
# users. Keeps the DP well-defined even in pathological inputs.
_INFEASIBLE_COST = float("inf")


def _validate_inputs(
    prices: list[PricePoint],
    battery_config: BatteryConfig,
    current_soc_kwh: float,
) -> PlanningError | None:
    if not prices:
        return PlanningError(
            "missing_prices", "No price data available for the planning horizon."
        )

    battery_error = battery_config.validate()
    if battery_error is not None:
        return PlanningError("invalid_battery_config", battery_error)

    sorted_prices = sorted(prices, key=lambda p: p.start)
    if sorted_prices != list(prices):
        return PlanningError(
            "unordered_prices", "Price periods are not in chronological order."
        )
    for a, b in itertools.pairwise(sorted_prices):
        if b.start < a.end:
            return PlanningError(
                "overlapping_prices",
                f"Price periods overlap: {a.start}-{a.end} and {b.start}-{b.end}.",
            )

    if current_soc_kwh is None:
        return PlanningError("missing_soc", "Current battery SOC is not available.")
    # Allow a little slack outside [min,max] -- a battery can legitimately be
    # a fraction below min_soc right after an unplanned discharge -- but
    # reject values that make no physical sense at all.
    if not (-0.5 <= current_soc_kwh <= battery_config.capacity_kwh + 0.5):
        return PlanningError(
            "invalid_soc",
            f"Current SOC ({current_soc_kwh} kWh) is outside the battery's "
            f"physical capacity (0-{battery_config.capacity_kwh} kWh).",
        )

    return None


def _forecast_for_slot(
    forecasts: list[PvForecastPoint] | list[LoadForecastPoint],
    start: dt.datetime,
    end: dt.datetime,
) -> tuple[float, bool]:
    """Sum forecast energy overlapping [start, end). Returns (kwh, is_degraded).

    If nothing overlaps, returns (0.0, True) -- a degraded zero-fill, not a
    silent guess.
    """
    total = 0.0
    covered_seconds = 0.0
    degraded = False
    span = (end - start).total_seconds()
    for point in forecasts:
        overlap_start = max(point.start, start)
        overlap_end = min(point.end, end)
        if overlap_end <= overlap_start:
            continue
        point_span = (point.end - point.start).total_seconds()
        if point_span <= 0:
            continue
        overlap_seconds = (overlap_end - overlap_start).total_seconds()
        fraction = overlap_seconds / point_span
        total += point.energy_kwh * fraction
        covered_seconds += overlap_seconds
        degraded = degraded or point.is_degraded
    if span > 0 and covered_seconds < span * 0.999:
        # Partial or no coverage -- scale what we have, but flag it.
        degraded = True
        if covered_seconds > 0:
            total *= span / covered_seconds
    return max(0.0, total), degraded


def _evaluate_action(
    *,
    pv_kwh: float,
    load_kwh: float,
    charge_in_kwh: float,
    discharge_out_kwh: float,
    import_price: float,
    export_price: float,
    config: BatteryConfig,
) -> dict:
    """Compute the physical energy split and cost for one slot.

    Given a chosen charge_in_kwh (>=0) XOR discharge_out_kwh (>=0).
    """
    pv_to_load = min(pv_kwh, load_kwh)
    remaining_load = load_kwh - pv_to_load
    pv_remaining = pv_kwh - pv_to_load

    usable_discharge = battery_math.usable_discharge_kwh(discharge_out_kwh, config)
    discharge_to_load = min(usable_discharge, remaining_load)
    remaining_load_after_discharge = remaining_load - discharge_to_load
    discharge_export_available = usable_discharge - discharge_to_load

    source_needed_for_charge = battery_math.source_energy_for_charge(
        charge_in_kwh, config
    )
    pv_to_charge = min(pv_remaining, source_needed_for_charge)
    grid_to_charge = source_needed_for_charge - pv_to_charge
    pv_export_available = pv_remaining - pv_to_charge

    grid_to_load = remaining_load_after_discharge
    grid_import_kwh = grid_to_load + grid_to_charge

    exportable_kwh = pv_export_available + discharge_export_available
    # Sell only if it is worth it; otherwise curtail (discard-excess).
    if export_price > 0:
        grid_export_kwh = exportable_kwh
        export_revenue = exportable_kwh * export_price
    else:
        grid_export_kwh = 0.0
        export_revenue = 0.0

    cycle_kwh = charge_in_kwh + discharge_out_kwh
    cost = (
        grid_import_kwh * import_price
        - export_revenue
        + cycle_kwh * config.cycle_cost_sek_per_kwh
    )

    pv_used_kwh = pv_to_load + pv_to_charge

    return {
        "grid_import_kwh": grid_import_kwh,
        "grid_to_charge_kwh": grid_to_charge,
        "grid_export_kwh": grid_export_kwh,
        "pv_used_kwh": pv_used_kwh,
        "pv_export_available": pv_export_available,
        "discharge_export_available": discharge_export_available,
        "cost": cost,
    }


def _violates_grid_power_limit(
    charge_in_kwh: float,
    discharge_out_kwh: float,
    evaluation: dict,
    duration_hours: float,
    config: BatteryConfig,
) -> bool:
    """Reject a battery action that would push grid power past the fuse limit.

    Only battery actions that *add to* the risk are filtered: charging
    can only increase grid import (never export), so it's checked against
    `max_grid_import_power_kw`; discharging can only increase grid export
    (never import), so it's checked against `max_grid_export_power_kw`.
    The idle action (no charge, no discharge) is never filtered here --
    house load alone exceeding the limit is not something the battery
    made worse, and it stays available as a fallback so the DP never runs
    out of feasible actions for a slot.
    """
    epsilon = 1e-6
    if (
        config.max_grid_import_power_kw is not None
        and charge_in_kwh > epsilon
        and evaluation["grid_import_kwh"] / duration_hours
        > config.max_grid_import_power_kw + epsilon
    ):
        return True
    return bool(
        config.max_grid_export_power_kw is not None
        and discharge_out_kwh > epsilon
        and evaluation["grid_export_kwh"] / duration_hours
        > config.max_grid_export_power_kw + epsilon
    )


def _classify_state(
    charge_in_kwh: float,
    discharge_out_kwh: float,
    pv_export_available: float,
    discharge_export_available: float,
    grid_export_kwh: float,
) -> SlotState:
    epsilon = 1e-6
    if charge_in_kwh > epsilon:
        return SlotState.CHARGE
    if discharge_out_kwh > epsilon:
        if discharge_export_available > epsilon:
            return SlotState.SELL
        return SlotState.DISCHARGE
    if grid_export_kwh > epsilon:
        return SlotState.SELL_EXCESS
    if pv_export_available > epsilon:
        return SlotState.DISCARD_EXCESS
    return SlotState.PAUSE


def _reason_for_slot(
    state: SlotState,
    *,
    import_price: float,
    export_price: float,
    grid_to_charge_kwh: float,
    is_degraded: bool,
) -> str:
    prefix = "[degraded data] " if is_degraded else ""
    if state == SlotState.CHARGE:
        if grid_to_charge_kwh > 1e-6:
            return (
                f"{prefix}charge: importing at {import_price:.2f} SEK/kWh, "
                f"cheaper than expected future price"
            )
        return f"{prefix}charge: storing surplus solar production"
    if state == SlotState.SELL:
        return (
            f"{prefix}sell: profitable after efficiency and cycle cost, "
            f"export price {export_price:.2f} SEK/kWh"
        )
    if state == SlotState.DISCHARGE:
        return f"{prefix}discharge: covering house load from battery (self-use)"
    if state == SlotState.SELL_EXCESS:
        return f"{prefix}sell-excess: exporting solar surplus not needed by the house"
    if state == SlotState.DISCARD_EXCESS:
        return f"{prefix}discard-excess: export price too low to be worth selling"
    if state == SlotState.OFF:
        return f"{prefix}off: planner disabled"
    return f"{prefix}pause: no economically favorable action this period"


def plan(
    *,
    prices: list[PricePoint],
    pv_forecast: list[PvForecastPoint],
    load_forecast: list[LoadForecastPoint],
    battery_config: BatteryConfig,
    current_soc_kwh: float,
    now: dt.datetime,
    reserve_kwh: list[float] | None = None,
) -> PlanOutcome:
    """Compute an optimal plan over the horizon covered by `prices`.

    `reserve_kwh`, if given, must be the same length as `prices` and
    aligned index-for-index with it (as returned by
    `core.reserve.compute_dynamic_reserve`) -- each entry is that price
    period's soft reserve target above `min_soc_kwh`, enforced via
    `battery_config.reserve_cost_sek_per_kwh` as a shadow-price penalty,
    never a hard constraint. Omit (or pass all zeros) for the pre-reserve
    behaviour.

    Returns a PlanOutcome wrapping either a PlanResult or a PlanningError.
    Never raises for "normal" bad input -- see PlanningError codes. Genuine
    programming errors (e.g. wrong types) still raise as usual.
    """
    error = _validate_inputs(prices, battery_config, current_soc_kwh)
    if error is not None:
        return PlanOutcome(error=error)
    if reserve_kwh is not None and len(reserve_kwh) != len(prices):
        return PlanOutcome(
            error=PlanningError(
                "invalid_reserve",
                f"reserve_kwh length ({len(reserve_kwh)}) must match "
                f"prices length ({len(prices)}).",
            )
        )

    resolution = battery_config.soc_resolution_kwh
    # Round min UP and max DOWN so every grid point the DP explores is
    # strictly inside [min_soc_kwh, max_soc_kwh] -- rounding to the
    # *nearest* tick here could place the grid's floor below the
    # configured minimum (e.g. min=10.24 kWh at a 0.5 kWh resolution would
    # round to 10.0, silently violating the configured floor).
    min_ticks = math.ceil(battery_config.min_soc_kwh / resolution - 1e-9)
    max_ticks = math.floor(battery_config.max_soc_kwh / resolution + 1e-9)
    start_ticks = battery_math.soc_kwh_to_ticks(current_soc_kwh, resolution)
    # Clamp the starting point into the feasible band -- if the battery is
    # currently outside [min,max] (e.g. just above min after a manual
    # discharge) we still want a plan, we just don't let the DP explore
    # further outside the band.
    start_ticks = max(min_ticks, min(max_ticks, start_ticks))

    if reserve_kwh is not None:
        _paired = sorted(
            zip(prices, reserve_kwh, strict=True), key=lambda pr: pr[0].start
        )
        ordered_prices = [p for p, _ in _paired]
        ordered_reserve = [r for _, r in _paired]
    else:
        ordered_prices = sorted(prices, key=lambda p: p.start)
        ordered_reserve = [0.0] * len(ordered_prices)

    # dp[ticks] = (cost_so_far, prev_ticks, action_detail) for the current step
    dp: dict[int, tuple[float, int | None, dict | None]] = {
        start_ticks: (0.0, None, None)
    }
    # history[t] = dp snapshot *before* processing slot t, to allow backtracking
    history: list[dict[int, tuple[float, int | None, dict | None]]] = []

    for slot_index, price_point in enumerate(ordered_prices):
        history.append(dp)
        duration_hours = price_point.duration_hours
        reserve_target_kwh = ordered_reserve[slot_index]
        pv_kwh, pv_degraded = _forecast_for_slot(
            pv_forecast, price_point.start, price_point.end
        )
        load_kwh, load_degraded = _forecast_for_slot(
            load_forecast, price_point.start, price_point.end
        )
        degraded = pv_degraded or load_degraded

        next_dp: dict[int, tuple[float, int | None, dict | None]] = {}

        for ticks, (cost_so_far, _, _) in dp.items():
            soc_kwh = battery_math.ticks_to_soc_kwh(ticks, resolution)

            max_charge = battery_math.max_charge_in_kwh(
                soc_kwh, duration_hours, battery_config
            )
            max_discharge = battery_math.max_discharge_out_kwh(
                soc_kwh, duration_hours, battery_config
            )

            charge_steps = max(0, round(max_charge / resolution))
            discharge_steps = max(0, round(max_discharge / resolution))

            candidate_actions: list[tuple[float, float, int]] = []
            # charge_in_kwh, discharge_out_kwh, delta_ticks
            candidate_actions.append((0.0, 0.0, 0))
            for step in range(1, charge_steps + 1):
                charge_in = min(max_charge, step * resolution)
                charge_ticks = round(charge_in / resolution)
                candidate_actions.append((charge_in, 0.0, charge_ticks))
            for step in range(1, discharge_steps + 1):
                discharge_out = min(max_discharge, step * resolution)
                candidate_actions.append(
                    (0.0, discharge_out, -round(discharge_out / resolution))
                )

            for charge_in_kwh, discharge_out_kwh, delta_ticks in candidate_actions:
                next_ticks = ticks + delta_ticks
                if next_ticks < min_ticks or next_ticks > max_ticks:
                    continue
                evaluation = _evaluate_action(
                    pv_kwh=pv_kwh,
                    load_kwh=load_kwh,
                    charge_in_kwh=charge_in_kwh,
                    discharge_out_kwh=discharge_out_kwh,
                    import_price=price_point.import_price_sek_per_kwh,
                    export_price=price_point.export_price_sek_per_kwh,
                    config=battery_config,
                )
                if _violates_grid_power_limit(
                    charge_in_kwh,
                    discharge_out_kwh,
                    evaluation,
                    duration_hours,
                    battery_config,
                ):
                    continue
                next_soc_kwh = battery_math.ticks_to_soc_kwh(next_ticks, resolution)
                reserve_shortfall_kwh = max(
                    0.0,
                    (battery_config.min_soc_kwh + reserve_target_kwh) - next_soc_kwh,
                )
                reserve_penalty = (
                    reserve_shortfall_kwh * battery_config.reserve_cost_sek_per_kwh
                )
                total_cost = cost_so_far + evaluation["cost"] + reserve_penalty
                detail = {
                    "charge_in_kwh": charge_in_kwh,
                    "discharge_out_kwh": discharge_out_kwh,
                    "pv_kwh": pv_kwh,
                    "load_kwh": load_kwh,
                    "degraded": degraded,
                    "reserve_target_kwh": reserve_target_kwh,
                    "reserve_shortfall_kwh": reserve_shortfall_kwh,
                    **evaluation,
                }
                existing = next_dp.get(next_ticks)
                if existing is None or total_cost < existing[0]:
                    next_dp[next_ticks] = (total_cost, ticks, detail)

        if not next_dp:
            # Should not happen (charge_in=0/discharge_out=0 is always
            # feasible), but guard against a fully-infeasible horizon.
            return PlanOutcome(
                error=PlanningError(
                    "no_feasible_plan",
                    "No feasible battery schedule found for the given horizon "
                    "and configuration.",
                )
            )
        dp = next_dp

    # Pick the cheapest final state.
    best_ticks = min(dp, key=lambda t: dp[t][0])
    best_cost, _, _ = dp[best_ticks]

    # Backtrack from the cheapest final SOC to slot 0, using the per-step
    # dp snapshots captured in `history` (history[i] = dp *before* slot i
    # was applied, i.e. dp *after* slot i-1).
    slots: list[PlanSlot] = []
    total_reserve_penalty = 0.0
    cur_ticks = best_ticks
    for i in range(len(ordered_prices) - 1, -1, -1):
        step_dp = dp if i == len(ordered_prices) - 1 else history[i + 1]
        _, prev_ticks, detail = step_dp[cur_ticks]
        price_point = ordered_prices[i]
        total_reserve_penalty += (
            detail["reserve_shortfall_kwh"] * battery_config.reserve_cost_sek_per_kwh
        )
        target_soc_kwh = battery_math.ticks_to_soc_kwh(cur_ticks, resolution)
        state = _classify_state(
            detail["charge_in_kwh"],
            detail["discharge_out_kwh"],
            detail["pv_export_available"],
            detail["discharge_export_available"],
            detail["grid_export_kwh"],
        )
        reason = _reason_for_slot(
            state,
            import_price=price_point.import_price_sek_per_kwh,
            export_price=price_point.export_price_sek_per_kwh,
            grid_to_charge_kwh=detail["grid_to_charge_kwh"],
            is_degraded=detail["degraded"],
        )
        slots.append(
            PlanSlot(
                start=price_point.start,
                end=price_point.end,
                state=state,
                target_soc_kwh=target_soc_kwh,
                battery_charge_kwh=detail["charge_in_kwh"],
                battery_discharge_kwh=detail["discharge_out_kwh"],
                pv_forecast_kwh=detail["pv_kwh"],
                load_forecast_kwh=detail["load_kwh"],
                grid_import_kwh=detail["grid_import_kwh"],
                grid_export_kwh=detail["grid_export_kwh"],
                cost_sek=detail["cost"],
                reason=reason,
                is_degraded=detail["degraded"],
                reserve_target_kwh=detail["reserve_target_kwh"],
                reserve_shortfall_kwh=detail["reserve_shortfall_kwh"],
            )
        )
        cur_ticks = prev_ticks

    slots.reverse()

    result = PlanResult(
        slots=slots,
        soc_start_kwh=current_soc_kwh,
        generated_at=now,
    )
    # Sanity: recomputed cost should match backtracked cost. best_cost is
    # the DP's internal planning objective, which includes the reserve
    # shadow-price penalty; result.total_cost_sek is the real projected
    # SEK cost (what PlanSlot.cost_sek actually reflects, deliberately
    # excluding the soft reserve penalty -- that's a planning nudge, not
    # real money). The two must differ by exactly the total reserve
    # penalty paid along the chosen path. A mismatch beyond that would
    # mean a bug in the DP/backtracking, not a bad user input -- worth
    # failing loudly rather than silently returning a wrong plan.
    if abs(result.total_cost_sek + total_reserve_penalty - best_cost) >= 1e-6:
        raise AssertionError(
            f"Smart Planner internal inconsistency: recomputed total cost "
            f"{result.total_cost_sek} + reserve penalty {total_reserve_penalty} "
            f"does not match DP cost {best_cost}"
        )
    return PlanOutcome(result=result)
