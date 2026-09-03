r"""CLI driver: run the walk-forward backtest against real exported HA data.

Reads the CSVs produced by the local `extract_ha_backtest_data.py` script
(prices.csv, temperature.csv, load.csv, pv_total.csv, pv_strings.csv,
battery_soc.csv, battery_energy.csv, grid.csv) and prints the full report
requested for the pre-Fas-2 real-data validation: baseline vs. simulated
cost, grid import/export, battery throughput, sell-then-rebuy incidents,
load/PV forecast error (overall and by temperature bucket), and the
lowest simulated SOC / reserve shortfall incidents.

No Home Assistant imports. Runnable directly (from the repo root):

    PYTHONPATH=custom_components/energy_planner/planner \\
        python3 -m core.run_real_backtest <data_dir> --days 30
    PYTHONPATH=custom_components/energy_planner/planner \\
        python3 -m core.run_real_backtest <data_dir> --days 90

Battery/price config defaults below match this installation's values
already established during Fas 1 (see docs/smart-planner.md) -- override
with the CLI flags if they've since changed on the live instance.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

from .economics import PriceConfig, compute_import_export_prices
from .forecast_consumption import HistoricalSample
from .forecast_pv import HistoricalPvSample
from .models import BatteryConfig, PricePoint
from .walkforward import WalkforwardConfig, baseline_actual_cost_sek, run_walkforward


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        print(f"  WARNING: {path.name} not found -- skipping", file=sys.stderr)
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _parse_dt(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def load_prices(data_dir: Path, price_config: PriceConfig) -> list[PricePoint]:
    rows = _read_csv(data_dir / "prices.csv")
    points = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["start"], row["end"])
        if key in seen:
            continue
        seen.add(key)
        try:
            start = _parse_dt(row["start"])
            end = _parse_dt(row["end"])
            spot = float(row["value"])
        except (ValueError, KeyError):
            continue
        if end <= start:
            continue
        import_price, export_price = compute_import_export_prices(spot, price_config)
        points.append(PricePoint(start, end, import_price, export_price))
    points.sort(key=lambda p: p.start)
    return points


def load_temperature(data_dir: Path) -> dict[dt.datetime, float]:
    rows = _read_csv(data_dir / "temperature.csv")
    result: dict[dt.datetime, float] = {}
    for row in rows:
        if row.get("label") != "outdoor_adjusted":
            continue
        try:
            start = _parse_dt(row["start"])
            value = float(row["value"])
        except (ValueError, KeyError):
            continue
        result[start.replace(minute=0, second=0, microsecond=0)] = value
    return result


def load_house_load(
    data_dir: Path, temperature: dict[dt.datetime, float]
) -> list[HistoricalSample]:
    rows = _read_csv(data_dir / "load.csv")
    samples = []
    for row in rows:
        try:
            start = _parse_dt(row["start"])
            end = _parse_dt(row["end"])
            value = float(row["value"])
        except (ValueError, KeyError):
            continue
        temp_c = temperature.get(start.replace(minute=0, second=0, microsecond=0))
        samples.append(HistoricalSample(start, end, value, temperature_c=temp_c))
    samples.sort(key=lambda s: s.start)
    return samples


def load_pv_total(data_dir: Path) -> list[HistoricalPvSample]:
    rows = _read_csv(data_dir / "pv_total.csv")
    samples = []
    for row in rows:
        try:
            start = _parse_dt(row["start"])
            end = _parse_dt(row["end"])
            value = float(row["value"])
        except (ValueError, KeyError):
            continue
        samples.append(HistoricalPvSample(start, end, value))
    samples.sort(key=lambda s: s.start)
    return samples


def load_pv_strings(data_dir: Path) -> dict[str, list[HistoricalPvSample]]:
    rows = _read_csv(data_dir / "pv_strings.csv")
    by_label: dict[str, list[HistoricalPvSample]] = {}
    for row in rows:
        try:
            start = _parse_dt(row["start"])
            end = _parse_dt(row["end"])
            value = float(row["value"])
        except (ValueError, KeyError):
            continue
        by_label.setdefault(row["label"], []).append(
            HistoricalPvSample(start, end, value)
        )
    for samples in by_label.values():
        samples.sort(key=lambda s: s.start)
    return by_label


def load_grid(data_dir: Path) -> tuple[list[HistoricalSample], list[HistoricalSample]]:
    """Return (grid_import_actual, grid_export_actual) samples.

    Prefers Tibber labels (per the user: most trusted) over Solis if
    both are present.
    """
    rows = _read_csv(data_dir / "grid.csv")
    by_label: dict[str, list[HistoricalSample]] = {}
    for row in rows:
        try:
            start = _parse_dt(row["start"])
            end = _parse_dt(row["end"])
            value = float(row["value"])
        except (ValueError, KeyError):
            continue
        by_label.setdefault(row["label"], []).append(
            HistoricalSample(start, end, value)
        )

    import_samples = by_label.get("grid_import_tibber") or by_label.get(
        "grid_import_solis", []
    )
    export_samples = by_label.get("grid_export_tibber") or by_label.get(
        "grid_export_solis", []
    )
    return import_samples, export_samples


def load_soc_initial(
    data_dir: Path, capacity_kwh: float, at_or_after: dt.datetime
) -> float | None:
    rows = _read_csv(data_dir / "battery_soc.csv")
    candidates = []
    for row in rows:
        try:
            start = _parse_dt(row["start"])
            value = float(row["value"])
        except (ValueError, KeyError):
            continue
        if start >= at_or_after:
            candidates.append((start, value))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return capacity_kwh * candidates[0][1] / 100.0


def print_report(label: str, result, baseline_cost: float) -> None:
    plan_cost = result.total_cost_sek
    improvement_sek = baseline_cost - plan_cost
    improvement_pct = (improvement_sek / baseline_cost * 100) if baseline_cost else 0.0

    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    print(f"Executed slots: {len(result.executed_slots)}")
    print(f"Unavailable decision points: {len(result.unavailable_days)}")
    print()
    print(f"1. Baseline (actual, real metered) cost:  {baseline_cost:10.2f} SEK")
    print(f"2. Smart Planner simulated cost:          {plan_cost:10.2f} SEK")
    print(
        f"3. Improvement:                            {improvement_sek:10.2f} SEK ({improvement_pct:+.1f}%)"
    )
    print(
        f"4. Grid import (simulated):                {result.total_grid_import_kwh:10.2f} kWh"
    )
    print(
        f"5. Grid export (simulated):                {result.total_grid_export_kwh:10.2f} kWh"
    )
    print(
        f"6. Battery throughput (charge+discharge):  {result.total_battery_throughput_kwh:10.2f} kWh"
    )
    print(
        f"7. Sell-then-rebuy-dearer incidents:       {result.sell_then_rebuy_incidents():10d}"
    )
    print(
        f"8. Load forecast MAE / RMSE:                {result.load_mae():8.3f} / {result.load_rmse():.3f} kWh"
    )
    print("9. Load forecast error by temperature bucket:")
    for bucket, stats in sorted(
        result.load_forecast_error_by_temperature_bucket().items()
    ):
        print(
            f"     {bucket:>8s}: n={stats['n']:5d}  "
            f"MAE={stats['mae_kwh']:.3f} kWh  RMSE={stats['rmse_kwh']:.3f} kWh"
        )
    print(
        f"10. PV forecast MAE / RMSE:                 {result.pv_mae():8.3f} / {result.pv_rmse():.3f} kWh"
    )
    print(f"11. Lowest simulated SOC:                   {result.min_soc_kwh:8.2f} kWh")
    shortfalls = result.reserve_shortfall_incidents
    print(f"    Reserve-shortfall slots:                {len(shortfalls):8d}")
    if result.unavailable_days:
        print("\nUnavailable decision points (no plan could be computed):")
        for d in result.unavailable_days[:10]:
            print(
                f"     {d.decision_time.isoformat()}  {d.error_code}: {d.error_message}"
            )
        if len(result.unavailable_days) > 10:
            print(f"     ... and {len(result.unavailable_days) - 10} more")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("data_dir", type=Path)
    parser.add_argument(
        "--days",
        type=int,
        action="append",
        default=None,
        help="Backtest window(s) in days, e.g. --days 30 --days 90 (default: 30 and 90)",
    )
    parser.add_argument("--capacity-kwh", type=float, default=25.6)
    parser.add_argument("--min-soc-pct", type=float, default=20.0)
    parser.add_argument("--max-soc-pct", type=float, default=90.0)
    parser.add_argument("--max-charge-kw", type=float, default=7.0)
    parser.add_argument("--max-discharge-kw", type=float, default=7.0)
    parser.add_argument("--charge-efficiency-pct", type=float, default=96.0)
    parser.add_argument("--discharge-efficiency-pct", type=float, default=96.0)
    parser.add_argument("--cycle-cost-sek-per-kwh", type=float, default=0.0)
    parser.add_argument("--max-grid-import-kw", type=float, default=15.9)
    parser.add_argument("--max-grid-export-kw", type=float, default=15.9)
    parser.add_argument(
        "--reserve-cost-sek-per-kwh",
        type=float,
        default=0.0,
        help="0.0 = reserve mechanism computed but not enforced (Fas-1 default)",
    )
    parser.add_argument("--network-cost-sek-per-kwh", type=float, default=0.0)
    parser.add_argument("--network-compensation-sek-per-kwh", type=float, default=0.0)
    args = parser.parse_args(argv[1:])

    price_config = PriceConfig(
        network_cost_sek_per_kwh=args.network_cost_sek_per_kwh,
        network_compensation_sek_per_kwh=args.network_compensation_sek_per_kwh,
    )
    battery_config = BatteryConfig(
        capacity_kwh=args.capacity_kwh,
        min_soc_fraction=args.min_soc_pct / 100.0,
        max_soc_fraction=args.max_soc_pct / 100.0,
        max_charge_power_kw=args.max_charge_kw,
        max_discharge_power_kw=args.max_discharge_kw,
        charge_efficiency=args.charge_efficiency_pct / 100.0,
        discharge_efficiency=args.discharge_efficiency_pct / 100.0,
        cycle_cost_sek_per_kwh=args.cycle_cost_sek_per_kwh,
        soc_resolution_kwh=0.25,
        max_grid_import_power_kw=args.max_grid_import_kw,
        max_grid_export_power_kw=args.max_grid_export_kw,
        reserve_cost_sek_per_kwh=args.reserve_cost_sek_per_kwh,
    )
    battery_error = battery_config.validate()
    if battery_error is not None:
        print(f"Invalid battery config: {battery_error}", file=sys.stderr)
        return 2

    print(f"Loading exported data from {args.data_dir}/ ...")
    prices = load_prices(args.data_dir, price_config)
    temperature_actual = load_temperature(args.data_dir)
    load_samples = load_house_load(args.data_dir, temperature_actual)
    pv_actual_total = load_pv_total(args.data_dir)
    pv_sources_for_profile = load_pv_strings(args.data_dir)
    grid_import_actual, grid_export_actual = load_grid(args.data_dir)

    print(f"  prices: {len(prices)} slots")
    print(f"  temperature: {len(temperature_actual)} hourly readings")
    print(f"  load: {len(load_samples)} hourly samples")
    print(f"  pv_total: {len(pv_actual_total)} hourly samples")
    print(
        f"  pv_strings: {sum(len(v) for v in pv_sources_for_profile.values())} rows across {len(pv_sources_for_profile)} sources"
    )
    print(
        f"  grid import/export: {len(grid_import_actual)} / {len(grid_export_actual)} hourly samples"
    )

    if not prices or not load_samples:
        print(
            "\nNot enough data to run a backtest (need at least prices and load).",
            file=sys.stderr,
        )
        return 1

    overall_end = max(prices[-1].end, load_samples[-1].end)
    windows = args.days or [30, 90]

    for days in sorted(windows):
        start = overall_end - dt.timedelta(days=days)
        window_prices = [p for p in prices if p.start >= start]
        if not window_prices:
            print(f"\n{days}-day window: no price data covers this window, skipping.")
            continue
        actual_start = window_prices[0].start
        soc_initial = load_soc_initial(args.data_dir, args.capacity_kwh, actual_start)
        if soc_initial is None:
            soc_initial = battery_config.capacity_kwh * 0.5
            print(
                f"\n{days}-day window: no battery_soc.csv reading at/after "
                f"{actual_start.isoformat()} -- using 50% capacity as a "
                f"placeholder initial SOC. Real results need this fixed."
            )

        result = run_walkforward(
            prices=window_prices,
            load_samples=load_samples,
            pv_actual_total=pv_actual_total,
            pv_sources_for_profile=pv_sources_for_profile,
            temperature_actual=temperature_actual,
            soc_initial_kwh=soc_initial,
            battery_config=battery_config,
            start=actual_start,
            end=overall_end,
            config=WalkforwardConfig(),
        )
        baseline_cost = baseline_actual_cost_sek(
            window_prices, grid_import_actual, grid_export_actual
        )
        print_report(
            f"{days}-day backtest ({actual_start.date()} .. {overall_end.date()})",
            result,
            baseline_cost,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
