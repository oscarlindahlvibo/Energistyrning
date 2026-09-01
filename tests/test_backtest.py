import os
import unittest

from tests._bootstrap import core  # noqa: F401

from core import backtest

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "example_day.json")


class TestBacktest(unittest.TestCase):
    def test_example_day_fixture_produces_a_plan_cheaper_than_no_battery(self):
        fixture = backtest.load_fixture(_FIXTURE_PATH)
        prices, pv_actual, load_actual, battery_config, initial_soc = (
            backtest.build_inputs_from_fixture(fixture)
        )
        result = backtest.run_backtest(
            prices, pv_actual, load_actual, battery_config, initial_soc
        )
        outcome = result.smart_planner_outcome
        self.assertTrue(outcome.ok, outcome.error)
        plan = result.smart_planner_outcome.result
        self.assertEqual(len(plan.slots), len(prices))
        # The whole point of the battery: Smart Planner's plan should never
        # cost more than doing nothing with it.
        self.assertLessEqual(plan.total_cost_sek, result.no_battery_cost_sek + 1e-6)

    def test_example_day_fixture_respects_soc_band(self):
        fixture = backtest.load_fixture(_FIXTURE_PATH)
        prices, pv_actual, load_actual, battery_config, initial_soc = (
            backtest.build_inputs_from_fixture(fixture)
        )
        result = backtest.run_backtest(
            prices, pv_actual, load_actual, battery_config, initial_soc
        )
        plan = result.smart_planner_outcome.result
        for slot in plan.slots:
            self.assertGreaterEqual(
                slot.target_soc_kwh, battery_config.min_soc_kwh - 1e-6
            )
            self.assertLessEqual(slot.target_soc_kwh, battery_config.max_soc_kwh + 1e-6)


if __name__ == "__main__":
    unittest.main()
