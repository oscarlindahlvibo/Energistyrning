import datetime as dt
import itertools
import unittest

from tests._bootstrap import core  # noqa: F401

from core import optimizer
from core.models import (
    BatteryConfig,
    LoadForecastPoint,
    PricePoint,
    PvForecastPoint,
    SlotState,
)

TZ = dt.timezone(dt.timedelta(hours=1))


def _battery(**overrides) -> BatteryConfig:
    defaults = {
        "capacity_kwh": 51.2,
        "min_soc_fraction": 0.2,
        "max_soc_fraction": 0.9,
        "max_charge_power_kw": 7.0,
        "max_discharge_power_kw": 7.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "cycle_cost_sek_per_kwh": 0.05,
        "soc_resolution_kwh": 0.5,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _quarter_hour_prices(start, spot_values, export_ratio=0.8):
    prices = []
    cur = start
    for spot in spot_values:
        prices.append(
            PricePoint(cur, cur + dt.timedelta(minutes=15), spot, spot * export_ratio)
        )
        cur += dt.timedelta(minutes=15)
    return prices


def _flat_forecast(cls, prices, kwh_per_slot):
    return [cls(p.start, p.end, kwh_per_slot) for p in prices]


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)

    def test_no_prices_is_a_failsafe_error(self):
        outcome = optimizer.plan(
            prices=[],
            pv_forecast=[],
            load_forecast=[],
            battery_config=_battery(),
            current_soc_kwh=25.0,
            now=self.start,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.code, "missing_prices")

    def test_missing_soc_is_a_failsafe_error(self):
        prices = _quarter_hour_prices(self.start, [1.0, 1.0])
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=_battery(),
            current_soc_kwh=None,
            now=self.start,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.code, "missing_soc")

    def test_soc_wildly_out_of_range_is_a_failsafe_error(self):
        prices = _quarter_hour_prices(self.start, [1.0, 1.0])
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=_battery(capacity_kwh=51.2),
            current_soc_kwh=999.0,
            now=self.start,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.code, "invalid_soc")

    def test_invalid_battery_config_is_a_failsafe_error(self):
        prices = _quarter_hour_prices(self.start, [1.0, 1.0])
        bad_battery = _battery(min_soc_fraction=0.9, max_soc_fraction=0.1)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=bad_battery,
            current_soc_kwh=25.0,
            now=self.start,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.code, "invalid_battery_config")

    def test_unordered_prices_is_a_failsafe_error(self):
        prices = _quarter_hour_prices(self.start, [1.0, 1.0, 1.0])
        prices[0], prices[1] = prices[1], prices[0]
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=_battery(),
            current_soc_kwh=25.0,
            now=self.start,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.code, "unordered_prices")

    def test_overlapping_prices_is_a_failsafe_error(self):
        prices = [
            PricePoint(self.start, self.start + dt.timedelta(minutes=30), 1.0, 0.8),
            PricePoint(
                self.start + dt.timedelta(minutes=15),
                self.start + dt.timedelta(minutes=45),
                1.0,
                0.8,
            ),
        ]
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=_battery(),
            current_soc_kwh=25.0,
            now=self.start,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.code, "overlapping_prices")

    def test_soc_slightly_outside_band_is_still_plannable(self):
        # A battery can legitimately sit a fraction below min_soc right
        # after an unplanned discharge -- this must not hard-fail.
        prices = _quarter_hour_prices(self.start, [1.0, 1.0])
        battery = _battery(min_soc_fraction=0.2, max_soc_fraction=0.9)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=battery.min_soc_kwh - 0.1,
            now=self.start,
        )
        self.assertTrue(outcome.ok, outcome.error)


class TestSocRespected(unittest.TestCase):
    def test_soc_never_leaves_configured_band(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        # Wild price swings to encourage aggressive charge/discharge.
        spot_values = [0.1, 5.0] * 48
        prices = _quarter_hour_prices(start, spot_values)
        battery = _battery(
            min_soc_fraction=0.2, max_soc_fraction=0.9, soc_resolution_kwh=0.5
        )
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=25.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        for slot in outcome.result.slots:
            self.assertGreaterEqual(slot.target_soc_kwh, battery.min_soc_kwh - 1e-6)
            self.assertLessEqual(slot.target_soc_kwh, battery.max_soc_kwh + 1e-6)


class TestArbitrageBehaviour(unittest.TestCase):
    def test_charges_when_cheap_and_sells_when_expensive(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        spot_values = [0.2] * 8 + [3.0] * 8  # 2h cheap, 2h expensive
        prices = _quarter_hour_prices(start, spot_values, export_ratio=0.9)
        battery = _battery(
            min_soc_fraction=0.1, max_soc_fraction=0.95, soc_resolution_kwh=0.5
        )
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=20.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        slots = outcome.result.slots
        cheap_slots = slots[:8]
        expensive_slots = slots[8:]
        self.assertTrue(any(s.state == SlotState.CHARGE for s in cheap_slots))
        self.assertTrue(any(s.state == SlotState.SELL for s in expensive_slots))
        # SOC should be higher at the end of the cheap window than at the start.
        self.assertGreater(cheap_slots[-1].target_soc_kwh, 20.0)

    def test_does_not_arbitrage_when_spread_too_small_to_cover_losses(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        # Charge at 1.0, could only sell at 1.02 -- efficiency + cycle cost
        # should make this round trip unprofitable, so the battery should
        # never charge at all. Start at min SOC so there is no pre-existing
        # (sunk-cost) energy it could sell off regardless of the charge
        # side -- that would be rational but isn't what this test checks.
        spot_values = [1.0] * 8 + [1.02] * 8
        prices = _quarter_hour_prices(start, spot_values, export_ratio=1.0)
        battery = _battery(
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
            cycle_cost_sek_per_kwh=0.1,
            soc_resolution_kwh=0.5,
        )
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=battery.min_soc_kwh,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        total_cycled = (
            outcome.result.total_charge_kwh + outcome.result.total_discharge_kwh
        )
        self.assertAlmostEqual(total_cycled, 0.0, places=6)

    def test_discards_instead_of_selling_at_negative_export_price(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = [PricePoint(start, start + dt.timedelta(minutes=15), 0.5, -0.2)]
        pv = [PvForecastPoint(start, start + dt.timedelta(minutes=15), 5.0)]
        load = [LoadForecastPoint(start, start + dt.timedelta(minutes=15), 0.5)]
        battery = _battery(soc_resolution_kwh=0.5)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=pv,
            load_forecast=load,
            battery_config=battery,
            current_soc_kwh=battery.max_soc_kwh,  # no room to charge -> forces a choice
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        slot = outcome.result.slots[0]
        self.assertEqual(slot.grid_export_kwh, 0.0)
        self.assertIn(slot.state, (SlotState.DISCARD_EXCESS, SlotState.PAUSE))


class TestVariableSlotDuration(unittest.TestCase):
    def test_works_with_hourly_prices_not_just_15_minutes(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = []
        cur = start
        for spot in [0.2, 0.2, 3.0, 3.0]:
            end = cur + dt.timedelta(hours=1)
            prices.append(PricePoint(cur, end, spot, spot * 0.9))
            cur = end
        battery = _battery(soc_resolution_kwh=0.5)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=20.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(len(outcome.result.slots), 4)
        # A full hour at 7 kW charge power (95% efficiency) can move up to
        # ~7*0.95 = 6.65 kWh into the battery in a single slot -- far more
        # than a 15-minute slot could (~1.66 kWh) with the same config. The
        # two cheap hours are priced identically, so the optimizer is free
        # to place the charging in either one; what matters is that a
        # single hourly slot moves a multi-kWh amount, proving the power
        # limit was converted using the slot's real (60 min) duration and
        # not a hardcoded 15-minute assumption.
        cheap_slots = outcome.result.slots[:2]
        self.assertTrue(
            any(s.battery_charge_kwh > 3.0 for s in cheap_slots),
            [s.battery_charge_kwh for s in cheap_slots],
        )

    def test_works_with_mixed_duration_slots(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = [
            PricePoint(start, start + dt.timedelta(minutes=15), 0.2, 0.15),
            PricePoint(
                start + dt.timedelta(minutes=15),
                start + dt.timedelta(minutes=75),
                3.0,
                2.7,
            ),
        ]
        battery = _battery(soc_resolution_kwh=0.5)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=20.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(len(outcome.result.slots), 2)


class TestEnergyBalance(unittest.TestCase):
    def test_grid_and_battery_flows_balance_the_house(self):
        start = dt.datetime(2026, 1, 10, 12, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [0.3, 1.0, 1.0, 2.5])
        pv = [
            PvForecastPoint(p.start, p.end, kwh)
            for p, kwh in zip(prices, [3.0, 1.0, 0.0, 0.0], strict=True)
        ]
        load = _flat_forecast(LoadForecastPoint, prices, 0.5)
        battery = _battery(soc_resolution_kwh=0.5)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=pv,
            load_forecast=load,
            battery_config=battery,
            current_soc_kwh=25.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        for slot in outcome.result.slots:
            usable_discharge = slot.battery_discharge_kwh * battery.discharge_efficiency
            source_for_charge = (
                slot.battery_charge_kwh / battery.charge_efficiency
                if slot.battery_charge_kwh
                else 0.0
            )
            supply = slot.pv_forecast_kwh + usable_discharge + slot.grid_import_kwh
            demand = slot.load_forecast_kwh + source_for_charge + slot.grid_export_kwh
            # Allow for the PV actually consumed vs curtailed distinction:
            # supply must at least cover demand (curtailed PV just doesn't
            # appear as export), never fall short of it.
            self.assertGreaterEqual(supply + 1e-6, demand - 1e-6)


class TestOutputShape(unittest.TestCase):
    def test_slots_cover_the_full_horizon_contiguously(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [1.0] * 10)
        battery = _battery(soc_resolution_kwh=0.5)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=25.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        slots = outcome.result.slots
        self.assertEqual(slots[0].start, start)
        for a, b in itertools.pairwise(slots):
            self.assertEqual(a.end, b.start)

    def test_every_slot_has_a_reason(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [0.2, 3.0, 1.0])
        battery = _battery(soc_resolution_kwh=0.5)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=25.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        for slot in outcome.result.slots:
            self.assertTrue(slot.reason)


class TestGridPowerLimit(unittest.TestCase):
    def test_charging_is_capped_by_grid_import_limit(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [0.2] * 4)
        # 7 kW battery charge power would need ~7.4 kW from the grid (95%
        # efficiency) with no PV -- cap the grid import well below that.
        battery = _battery(
            soc_resolution_kwh=0.5,
            max_grid_import_power_kw=2.0,
        )
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=20.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        for slot in outcome.result.slots:
            if slot.grid_import_kwh > 0:
                import_power_kw = slot.grid_import_kwh / (
                    (slot.end - slot.start).total_seconds() / 3600.0
                )
                self.assertLessEqual(import_power_kw, 2.0 + 1e-6)

    def test_selling_is_capped_by_grid_export_limit(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [3.0] * 4, export_ratio=0.9)
        battery = _battery(
            soc_resolution_kwh=0.5,
            max_grid_export_power_kw=1.0,
        )
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=battery.max_soc_kwh,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        for slot in outcome.result.slots:
            if slot.grid_export_kwh > 0:
                export_power_kw = slot.grid_export_kwh / (
                    (slot.end - slot.start).total_seconds() / 3600.0
                )
                self.assertLessEqual(export_power_kw, 1.0 + 1e-6)

    def test_idle_action_stays_available_even_if_load_alone_exceeds_limit(self):
        # House load by itself (5 kWh in 15 minutes = 20 kW) already blows
        # past a 2 kW import cap -- the battery cannot fix that by doing
        # nothing, and the optimizer must still return a plan rather than
        # treating every slot as infeasible.
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [1.0])
        load = [LoadForecastPoint(prices[0].start, prices[0].end, 5.0)]
        battery = _battery(
            soc_resolution_kwh=0.5,
            max_grid_import_power_kw=2.0,
            max_charge_power_kw=0.0001,
            max_discharge_power_kw=7.0,
        )
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=load,
            battery_config=battery,
            current_soc_kwh=25.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)

    def test_no_limit_configured_behaves_as_before(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [0.2, 0.2, 3.0, 3.0])
        battery = _battery(soc_resolution_kwh=0.5)
        self.assertIsNone(battery.max_grid_import_power_kw)
        self.assertIsNone(battery.max_grid_export_power_kw)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=20.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)


class TestReserve(unittest.TestCase):
    def test_no_reserve_kwh_behaves_as_before(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [3.0] * 4)
        battery = _battery(soc_resolution_kwh=0.5, reserve_cost_sek_per_kwh=5.0)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=20.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        for slot in outcome.result.slots:
            self.assertEqual(slot.reserve_target_kwh, 0.0)
            self.assertEqual(slot.reserve_shortfall_kwh, 0.0)

    def test_reserve_kwh_length_mismatch_is_a_failsafe_error(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [1.0, 1.0, 1.0])
        battery = _battery()
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=20.0,
            now=start,
            reserve_kwh=[1.0, 2.0],
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.code, "invalid_reserve")

    def test_high_reserve_penalty_prevents_selling_below_target(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        # Flat, mildly attractive price throughout -- selling is only
        # marginally profitable, so a real reserve penalty should win.
        prices = _quarter_hour_prices(start, [1.0] * 8, export_ratio=1.0)
        battery = _battery(
            soc_resolution_kwh=0.5,
            min_soc_fraction=0.1,
            reserve_cost_sek_per_kwh=50.0,
            cycle_cost_sek_per_kwh=0.0,
        )
        reserve_kwh = [15.0] * 8
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=20.0,
            now=start,
            reserve_kwh=reserve_kwh,
        )
        self.assertTrue(outcome.ok, outcome.error)
        min_soc_seen = min(s.target_soc_kwh for s in outcome.result.slots)
        # min_soc_kwh (5.12) + reserve (15.0) = 20.12 -- with a steep
        # enough penalty the optimizer should stay at or above roughly
        # that line rather than selling down toward the hard floor.
        self.assertGreaterEqual(min_soc_seen, battery.min_soc_kwh + 10.0)

    def test_reserve_shortfall_reported_when_breached(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        # Huge price spike makes selling through the reserve worth it
        # even with a real (but not absurd) penalty.
        prices = _quarter_hour_prices(start, [0.5, 0.5, 10.0, 10.0], export_ratio=1.0)
        battery = _battery(
            soc_resolution_kwh=0.5,
            min_soc_fraction=0.1,
            reserve_cost_sek_per_kwh=0.5,
        )
        reserve_kwh = [10.0] * 4
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=15.0,
            now=start,
            reserve_kwh=reserve_kwh,
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(any(s.reserve_shortfall_kwh > 0 for s in outcome.result.slots))

    def test_reserve_penalty_excluded_from_reported_cost_sek(self):
        # cost_sek must reflect real projected SEK, not the internal
        # shadow-priced planning objective -- verified via the optimizer's
        # own internal consistency assertion (would raise if wrong).
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        prices = _quarter_hour_prices(start, [1.0] * 4)
        battery = _battery(soc_resolution_kwh=0.5, reserve_cost_sek_per_kwh=3.0)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=battery,
            current_soc_kwh=20.0,
            now=start,
            reserve_kwh=[5.0] * 4,
        )
        self.assertTrue(outcome.ok, outcome.error)


if __name__ == "__main__":
    unittest.main()
