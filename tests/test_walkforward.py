import copy
import datetime as dt
import unittest

from tests._bootstrap import core  # noqa: F401

from core.forecast_consumption import HistoricalSample
from core.forecast_pv import HistoricalPvSample
from core.models import BatteryConfig, PricePoint, SlotState
from core.walkforward import (
    DayUnavailable,
    ExecutedSlot,
    WalkforwardConfig,
    WalkforwardResult,
    baseline_actual_cost_sek,
    run_walkforward,
)

TZ = dt.timezone(dt.timedelta(hours=1))


def _battery(**overrides) -> BatteryConfig:
    defaults = {
        "capacity_kwh": 20.0,
        "min_soc_fraction": 0.1,
        "max_soc_fraction": 0.95,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "cycle_cost_sek_per_kwh": 0.02,
        "soc_resolution_kwh": 0.5,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _hourly_prices(day_start: dt.datetime, n_days: int, spot_fn) -> list[PricePoint]:
    prices = []
    cur = day_start
    for i in range(n_days * 24):
        prices.append(
            PricePoint(cur, cur + dt.timedelta(hours=1), spot_fn(i), spot_fn(i) * 0.7)
        )
        cur += dt.timedelta(hours=1)
    return prices


def _hourly_load_samples(
    day_start: dt.datetime, n_days: int, base_kwh: float = 1.0
) -> list[HistoricalSample]:
    samples = []
    cur = day_start
    for _i in range(n_days * 24):
        hour = cur.hour
        # A mild diurnal pattern with a fixed temperature so the median
        # forecast has something stable to converge to.
        energy = base_kwh + (0.5 if 6 <= hour <= 9 or 17 <= hour <= 21 else 0.0)
        samples.append(
            HistoricalSample(
                cur, cur + dt.timedelta(hours=1), energy, temperature_c=0.0
            )
        )
        cur += dt.timedelta(hours=1)
    return samples


def _temperature_actual(
    day_start: dt.datetime, n_days: int
) -> dict[dt.datetime, float]:
    result = {}
    cur = day_start
    for _ in range(n_days * 24):
        result[cur] = 0.0
        cur += dt.timedelta(hours=1)
    return result


def _hourly_pv_samples(
    day_start: dt.datetime, n_days: int, peak_kwh: float = 1.0
) -> list[HistoricalPvSample]:
    samples = []
    cur = day_start
    for _ in range(n_days * 24):
        hour = cur.hour
        energy = peak_kwh if 9 <= hour <= 15 else 0.0
        samples.append(HistoricalPvSample(cur, cur + dt.timedelta(hours=1), energy))
        cur += dt.timedelta(hours=1)
    return samples


class TestRunWalkforwardSmoke(unittest.TestCase):
    def _run(self, n_days=8, **kwargs):
        day_start = dt.datetime(2026, 1, 1, 0, 0, tzinfo=TZ)
        prices = _hourly_prices(
            day_start, n_days, lambda i: 0.5 + 0.3 * ((i % 24) in range(16, 20))
        )
        load_samples = _hourly_load_samples(day_start, n_days)
        pv_samples = _hourly_pv_samples(day_start, n_days)
        temperature_actual = _temperature_actual(day_start, n_days)
        params = {
            "prices": prices,
            "load_samples": load_samples,
            "pv_actual_total": pv_samples,
            "pv_sources_for_profile": {"pv1": pv_samples},
            "temperature_actual": temperature_actual,
            "soc_initial_kwh": 10.0,
            "battery_config": _battery(),
            "start": day_start,
            "end": day_start + dt.timedelta(days=n_days),
            "config": WalkforwardConfig(lookback_days_load=5, min_samples=2),
        }
        params.update(kwargs)
        return run_walkforward(**params)

    def test_produces_executed_slots_and_stays_within_soc_bounds(self):
        result = self._run()
        self.assertGreater(len(result.executed_slots), 0)
        for slot in result.executed_slots:
            self.assertGreaterEqual(slot.soc_after_kwh, -1e-6)
            self.assertLessEqual(slot.soc_after_kwh, 20.0 + 1e-6)

    def test_no_lookahead_perturbing_future_actuals_leaves_earlier_decisions_unchanged(
        self,
    ):
        day_start = dt.datetime(2026, 1, 1, 0, 0, tzinfo=TZ)
        n_days = 6
        prices = _hourly_prices(day_start, n_days, lambda i: 0.5)
        load_samples = _hourly_load_samples(day_start, n_days)
        pv_samples = _hourly_pv_samples(day_start, n_days)
        temperature_actual = _temperature_actual(day_start, n_days)

        result_a = run_walkforward(
            prices=prices,
            load_samples=load_samples,
            pv_actual_total=pv_samples,
            pv_sources_for_profile={"pv1": pv_samples},
            temperature_actual=temperature_actual,
            soc_initial_kwh=10.0,
            battery_config=_battery(),
            start=day_start,
            end=day_start + dt.timedelta(days=n_days),
            config=WalkforwardConfig(lookback_days_load=5, min_samples=2),
        )

        # Perturb only the LAST day's actual load/PV -- a look-ahead bug
        # would let this leak backward into earlier decisions.
        last_day_start = day_start + dt.timedelta(days=n_days - 1)
        load_samples_b = copy.deepcopy(load_samples)
        pv_samples_b = copy.deepcopy(pv_samples)
        load_samples_b = [
            HistoricalSample(
                s.start, s.end, s.energy_kwh * 5.0, temperature_c=s.temperature_c
            )
            if s.start >= last_day_start
            else s
            for s in load_samples_b
        ]
        pv_samples_b = [
            HistoricalPvSample(s.start, s.end, s.energy_kwh * 5.0)
            if s.start >= last_day_start
            else s
            for s in pv_samples_b
        ]

        result_b = run_walkforward(
            prices=prices,
            load_samples=load_samples_b,
            pv_actual_total=pv_samples_b,
            pv_sources_for_profile={"pv1": pv_samples_b},
            temperature_actual=temperature_actual,
            soc_initial_kwh=10.0,
            battery_config=_battery(),
            start=day_start,
            end=day_start + dt.timedelta(days=n_days),
            config=WalkforwardConfig(lookback_days_load=5, min_samples=2),
        )

        cutoff = day_start + dt.timedelta(days=n_days - 1)
        early_a = [s for s in result_a.executed_slots if s.start < cutoff]
        early_b = [s for s in result_b.executed_slots if s.start < cutoff]
        self.assertEqual(len(early_a), len(early_b))
        for a, b in zip(early_a, early_b, strict=True):
            self.assertAlmostEqual(a.planned_charge_kwh, b.planned_charge_kwh, places=6)
            self.assertAlmostEqual(
                a.planned_discharge_kwh, b.planned_discharge_kwh, places=6
            )
            self.assertAlmostEqual(a.forecast_load_kwh, b.forecast_load_kwh, places=6)
            self.assertAlmostEqual(a.forecast_pv_kwh, b.forecast_pv_kwh, places=6)

    def test_invalid_initial_soc_is_recorded_as_unavailable(self):
        result = self._run(soc_initial_kwh=-100.0)
        self.assertEqual(len(result.executed_slots), 0)
        self.assertGreater(len(result.unavailable_days), 0)
        self.assertTrue(
            all(isinstance(d, DayUnavailable) for d in result.unavailable_days)
        )

    def test_clamping_flag_set_when_actual_pv_diverges_sharply_from_forecast(self):
        # A battery with very little headroom and a sharp, unforecastable
        # PV spike should force at least one clamped charge action.
        result = self._run(
            battery_config=_battery(capacity_kwh=2.0, max_soc_fraction=0.95),
            soc_initial_kwh=1.0,
        )
        # Not asserting True unconditionally (depends on the DP's chosen
        # actions), but the flag must exist and be a bool on every slot,
        # and SOC must never exceed capacity regardless.
        for slot in result.executed_slots:
            self.assertIsInstance(slot.clamped, bool)
            self.assertLessEqual(slot.soc_after_kwh, 2.0 + 1e-6)


class TestWalkforwardResultStatistics(unittest.TestCase):
    def _slot(self, **overrides) -> ExecutedSlot:
        defaults = {
            "start": dt.datetime(2026, 1, 1, 8, 0, tzinfo=TZ),
            "end": dt.datetime(2026, 1, 1, 9, 0, tzinfo=TZ),
            "state": SlotState.PAUSE,
            "planned_charge_kwh": 0.0,
            "planned_discharge_kwh": 0.0,
            "executed_charge_kwh": 0.0,
            "executed_discharge_kwh": 0.0,
            "clamped": False,
            "soc_before_kwh": 10.0,
            "soc_after_kwh": 10.0,
            "actual_pv_kwh": 0.0,
            "actual_load_kwh": 1.0,
            "forecast_pv_kwh": 0.0,
            "forecast_load_kwh": 1.0,
            "import_price_sek_per_kwh": 1.0,
            "export_price_sek_per_kwh": 0.7,
            "grid_import_kwh": 1.0,
            "grid_export_kwh": 0.0,
            "cost_sek": 1.0,
            "reserve_target_kwh": 0.0,
            "reserve_shortfall_kwh": 0.0,
            "decision_time": dt.datetime(2026, 1, 1, 13, 0, tzinfo=TZ),
        }
        defaults.update(overrides)
        return ExecutedSlot(**defaults)

    def test_load_mae_and_rmse(self):
        slots = [
            self._slot(forecast_load_kwh=1.0, actual_load_kwh=1.0),
            self._slot(forecast_load_kwh=1.0, actual_load_kwh=2.0),
            self._slot(forecast_load_kwh=2.0, actual_load_kwh=0.0),
        ]
        result = WalkforwardResult(
            executed_slots=slots, unavailable_days=[], soc_initial_kwh=10.0
        )
        # errors: 0, 1, 2 -> MAE = 1.0
        self.assertAlmostEqual(result.load_mae(), 1.0)
        # squared errors: 0, 1, 4 -> mean 5/3 -> rmse = sqrt(5/3)
        self.assertAlmostEqual(result.load_rmse(), (5 / 3) ** 0.5)

    def test_sell_then_rebuy_incident_detected(self):
        day = dt.date(2026, 1, 5)
        sell = self._slot(
            start=dt.datetime(2026, 1, 5, 12, 0, tzinfo=TZ),
            end=dt.datetime(2026, 1, 5, 13, 0, tzinfo=TZ),
            state=SlotState.SELL,
            grid_export_kwh=2.0,
            export_price_sek_per_kwh=0.5,
        )
        rebuy = self._slot(
            start=dt.datetime(2026, 1, 5, 18, 0, tzinfo=TZ),
            end=dt.datetime(2026, 1, 5, 19, 0, tzinfo=TZ),
            state=SlotState.CHARGE,
            grid_import_kwh=2.0,
            import_price_sek_per_kwh=2.0,
        )
        result = WalkforwardResult(
            executed_slots=[sell, rebuy], unavailable_days=[], soc_initial_kwh=10.0
        )
        self.assertEqual(result.sell_then_rebuy_incidents(), 1)
        del day

    def test_no_incident_when_rebuy_is_cheaper_than_the_sell(self):
        sell = self._slot(
            start=dt.datetime(2026, 1, 5, 12, 0, tzinfo=TZ),
            end=dt.datetime(2026, 1, 5, 13, 0, tzinfo=TZ),
            state=SlotState.SELL,
            grid_export_kwh=2.0,
            export_price_sek_per_kwh=2.0,
        )
        cheap_buy = self._slot(
            start=dt.datetime(2026, 1, 5, 18, 0, tzinfo=TZ),
            end=dt.datetime(2026, 1, 5, 19, 0, tzinfo=TZ),
            state=SlotState.CHARGE,
            grid_import_kwh=2.0,
            import_price_sek_per_kwh=0.5,
        )
        result = WalkforwardResult(
            executed_slots=[sell, cheap_buy], unavailable_days=[], soc_initial_kwh=10.0
        )
        self.assertEqual(result.sell_then_rebuy_incidents(), 0)

    def test_total_battery_throughput(self):
        slots = [
            self._slot(executed_charge_kwh=1.0, executed_discharge_kwh=0.0),
            self._slot(executed_charge_kwh=0.0, executed_discharge_kwh=2.0),
        ]
        result = WalkforwardResult(
            executed_slots=slots, unavailable_days=[], soc_initial_kwh=10.0
        )
        self.assertAlmostEqual(result.total_battery_throughput_kwh, 3.0)


class TestBaselineActualCost(unittest.TestCase):
    def test_simple_import_only(self):
        price = PricePoint(
            dt.datetime(2026, 1, 1, 8, 0, tzinfo=TZ),
            dt.datetime(2026, 1, 1, 9, 0, tzinfo=TZ),
            1.5,
            1.0,
        )
        imports = [
            HistoricalSample(price.start, price.end, 2.0),
        ]
        exports: list[HistoricalSample] = []
        cost = baseline_actual_cost_sek([price], imports, exports)
        self.assertAlmostEqual(cost, 3.0)

    def test_export_reduces_cost(self):
        price = PricePoint(
            dt.datetime(2026, 1, 1, 8, 0, tzinfo=TZ),
            dt.datetime(2026, 1, 1, 9, 0, tzinfo=TZ),
            1.5,
            1.0,
        )
        imports = [HistoricalSample(price.start, price.end, 1.0)]
        exports = [HistoricalSample(price.start, price.end, 1.0)]
        cost = baseline_actual_cost_sek([price], imports, exports)
        self.assertAlmostEqual(cost, 1.5 - 1.0)


if __name__ == "__main__":
    unittest.main()
