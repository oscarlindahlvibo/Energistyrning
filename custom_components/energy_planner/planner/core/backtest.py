r"""Backtesting for Smart Planner.

Replays a historical day's actual prices/PV/load through the optimizer
and reports what it would have decided.

This deliberately feeds *actual* historical PV/load as the "forecast" input
(perfect foresight) -- the goal in Fas 1 is to validate the optimizer's
decision logic and energy-balance math against real data, not to evaluate
forecast-provider accuracy (that is a separate, later concern once
forecast_consumption/forecast_pv have run for a while in shadow mode).

No Home Assistant imports. Runnable directly (from the repo root) without
installing the `homeassistant` package -- note the PYTHONPATH points at
the `planner` directory, not the repo root, so `core` resolves as a plain
top-level package instead of pulling in `custom_components.energy_planner`
(which does require `homeassistant`):

    PYTHONPATH=custom_components/energy_planner/planner \\
        python3 -m core.backtest <fixture.json>

where <fixture.json> has the shape documented in `load_fixture`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sys

from . import optimizer
from .models import (
    BatteryConfig,
    LoadForecastPoint,
    PlanOutcome,
    PricePoint,
    PvForecastPoint,
)


@dataclasses.dataclass(frozen=True)
class BacktestResult:
    """Outcome of one backtest run."""

    smart_planner_outcome: PlanOutcome
    no_battery_cost_sek: float
    """Cost if the battery had done nothing at all (grid buys the PV
    shortfall, sells nothing) -- a naive baseline to show the battery's
    (and the algorithm's) contribution."""


def _no_battery_baseline_cost(
    prices: list[PricePoint],
    pv_forecast: list[PvForecastPoint],
    load_forecast: list[LoadForecastPoint],
) -> float:
    """Cost of consuming PV directly with the battery completely idle."""

    def _kwh_for(points, start, end) -> float:
        total = 0.0
        for p in points:
            overlap_start = max(p.start, start)
            overlap_end = min(p.end, end)
            if overlap_end > overlap_start:
                span = (p.end - p.start).total_seconds()
                if span > 0:
                    overlap = (overlap_end - overlap_start).total_seconds()
                    total += p.energy_kwh * overlap / span
        return total

    cost = 0.0
    for price in prices:
        pv_kwh = _kwh_for(pv_forecast, price.start, price.end)
        load_kwh = _kwh_for(load_forecast, price.start, price.end)
        surplus = pv_kwh - load_kwh
        if surplus >= 0:
            if price.export_price_sek_per_kwh > 0:
                cost -= surplus * price.export_price_sek_per_kwh
        else:
            cost += (-surplus) * price.import_price_sek_per_kwh
    return cost


def run_backtest(
    prices: list[PricePoint],
    actual_pv: list[PvForecastPoint],
    actual_load: list[LoadForecastPoint],
    battery_config: BatteryConfig,
    initial_soc_kwh: float,
) -> BacktestResult:
    """Run the optimizer against historical actuals vs. a no-battery baseline."""
    outcome = optimizer.plan(
        prices=prices,
        pv_forecast=actual_pv,
        load_forecast=actual_load,
        battery_config=battery_config,
        current_soc_kwh=initial_soc_kwh,
        now=prices[0].start if prices else dt.datetime.now(dt.timezone.utc),
    )
    baseline_cost = _no_battery_baseline_cost(prices, actual_pv, actual_load)
    return BacktestResult(
        smart_planner_outcome=outcome, no_battery_cost_sek=baseline_cost
    )


def load_fixture(path: str) -> dict:
    """Load a JSON backtest fixture.

    Expected shape::

        {
          "battery": {
            "capacity_kwh": 51.2, "min_soc_fraction": 0.2, "max_soc_fraction": 0.9,
            "max_charge_power_kw": 7.0, "max_discharge_power_kw": 7.0,
            "charge_efficiency": 0.95, "discharge_efficiency": 0.95,
            "cycle_cost_sek_per_kwh": 0.05, "soc_resolution_kwh": 0.5
          },
          "initial_soc_kwh": 25.0,
          "prices": [
            {"start": "2026-01-10T00:00:00+01:00", "end": "2026-01-10T00:15:00+01:00",
             "import_price": 0.9, "export_price": 0.7},
            ...
          ],
          "pv_actual": [
            {"start": "...", "end": "...", "energy_kwh": 0.0}, ...
          ],
          "load_actual": [
            {"start": "...", "end": "...", "energy_kwh": 0.3}, ...
          ]
        }
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _parse_dt(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def build_inputs_from_fixture(
    fixture: dict,
) -> tuple[
    list[PricePoint],
    list[PvForecastPoint],
    list[LoadForecastPoint],
    BatteryConfig,
    float,
]:
    """Turn a raw fixture dict into typed core inputs."""
    prices = [
        PricePoint(
            _parse_dt(p["start"]),
            _parse_dt(p["end"]),
            p["import_price"],
            p["export_price"],
        )
        for p in fixture["prices"]
    ]
    pv_actual = [
        PvForecastPoint(_parse_dt(p["start"]), _parse_dt(p["end"]), p["energy_kwh"])
        for p in fixture["pv_actual"]
    ]
    load_actual = [
        LoadForecastPoint(_parse_dt(p["start"]), _parse_dt(p["end"]), p["energy_kwh"])
        for p in fixture["load_actual"]
    ]
    battery_config = BatteryConfig(**fixture["battery"])
    return prices, pv_actual, load_actual, battery_config, fixture["initial_soc_kwh"]


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} <fixture.json>", file=sys.stderr)
        return 2
    fixture = load_fixture(argv[1])
    prices, pv_actual, load_actual, battery_config, initial_soc = (
        build_inputs_from_fixture(fixture)
    )
    result = run_backtest(prices, pv_actual, load_actual, battery_config, initial_soc)

    if not result.smart_planner_outcome.ok:
        print("Smart Planner could not produce a plan:")
        print(
            f"  {result.smart_planner_outcome.error.code}: "
            f"{result.smart_planner_outcome.error.message}"
        )
        return 1

    plan = result.smart_planner_outcome.result
    print("=== Smart Planner backtest ===")
    print(f"Slots: {len(plan.slots)}")
    print(f"SOC start: {plan.soc_start_kwh:.2f} kWh")
    print(f"Smart Planner cost: {plan.total_cost_sek:.2f} SEK")
    print(f"No-battery baseline cost: {result.no_battery_cost_sek:.2f} SEK")
    print(f"Improvement: {result.no_battery_cost_sek - plan.total_cost_sek:.2f} SEK")
    print(f"Grid import: {plan.total_grid_import_kwh:.2f} kWh")
    print(f"Grid export: {plan.total_grid_export_kwh:.2f} kWh")
    print()
    print("Slot-by-slot:")
    for slot in plan.slots:
        print(
            f"  {slot.start.strftime('%Y-%m-%d %H:%M')} - {slot.end.strftime('%H:%M')} "
            f"{slot.state.value:15s} SOC->{slot.target_soc_kwh:6.2f} kWh  "
            f"cost={slot.cost_sek:7.3f}  {slot.reason}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
