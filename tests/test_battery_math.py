import unittest

from tests._bootstrap import core  # noqa: F401

from core import battery_math
from core.models import BatteryConfig


def _config(**overrides) -> BatteryConfig:
    defaults = {
        "capacity_kwh": 51.2,
        "min_soc_fraction": 0.2,
        "max_soc_fraction": 0.9,
        "max_charge_power_kw": 7.0,
        "max_discharge_power_kw": 7.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "cycle_cost_sek_per_kwh": 0.05,
        "soc_resolution_kwh": 0.25,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


class TestBatteryConfig(unittest.TestCase):
    def test_min_max_soc_kwh(self):
        cfg = _config()
        self.assertAlmostEqual(cfg.min_soc_kwh, 51.2 * 0.2)
        self.assertAlmostEqual(cfg.max_soc_kwh, 51.2 * 0.9)

    def test_validate_ok(self):
        self.assertIsNone(_config().validate())

    def test_validate_rejects_negative_capacity(self):
        self.assertIsNotNone(_config(capacity_kwh=-1).validate())

    def test_validate_rejects_min_above_max(self):
        self.assertIsNotNone(
            _config(min_soc_fraction=0.9, max_soc_fraction=0.2).validate()
        )

    def test_validate_rejects_zero_power(self):
        self.assertIsNotNone(_config(max_charge_power_kw=0).validate())
        self.assertIsNotNone(_config(max_discharge_power_kw=0).validate())

    def test_validate_rejects_efficiency_out_of_range(self):
        self.assertIsNotNone(_config(charge_efficiency=0).validate())
        self.assertIsNotNone(_config(charge_efficiency=1.1).validate())


class TestSocTicks(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(battery_math.soc_kwh_to_ticks(10.0, 0.25), 40)
        self.assertAlmostEqual(battery_math.ticks_to_soc_kwh(40, 0.25), 10.0)

    def test_snapping(self):
        # 10.1 kWh should snap to the nearest 0.25 kWh tick (40 -> 10.0)
        self.assertEqual(battery_math.soc_kwh_to_ticks(10.1, 0.25), 40)
        self.assertEqual(battery_math.soc_kwh_to_ticks(10.2, 0.25), 41)


class TestChargeDischargeLimits(unittest.TestCase):
    def test_max_charge_limited_by_headroom(self):
        cfg = _config(max_soc_fraction=0.5, max_charge_power_kw=100.0)
        # SOC already at max -> zero headroom
        result = battery_math.max_charge_in_kwh(cfg.max_soc_kwh, 0.25, cfg)
        self.assertAlmostEqual(result, 0.0)

    def test_max_charge_limited_by_power(self):
        cfg = _config(max_charge_power_kw=4.0, charge_efficiency=0.9)
        # 4 kW * 0.25 h * 0.9 eff = 0.9 kWh stored, well under headroom
        result = battery_math.max_charge_in_kwh(10.0, 0.25, cfg)
        self.assertAlmostEqual(result, 0.9)

    def test_max_discharge_limited_by_available_energy(self):
        cfg = _config(min_soc_fraction=0.2, max_discharge_power_kw=100.0)
        # SOC just above min -> tiny headroom
        soc = cfg.min_soc_kwh + 0.1
        result = battery_math.max_discharge_out_kwh(soc, 0.25, cfg)
        self.assertAlmostEqual(result, 0.1)

    def test_max_discharge_limited_by_power(self):
        cfg = _config(max_discharge_power_kw=4.0, discharge_efficiency=0.9)
        # 4 kW * 0.25 h / 0.9 eff = 1.111 kWh drawn from storage
        result = battery_math.max_discharge_out_kwh(30.0, 0.25, cfg)
        self.assertAlmostEqual(result, 4.0 * 0.25 / 0.9)

    def test_usable_discharge_applies_efficiency(self):
        cfg = _config(discharge_efficiency=0.9)
        self.assertAlmostEqual(battery_math.usable_discharge_kwh(10.0, cfg), 9.0)

    def test_source_energy_for_charge_applies_efficiency(self):
        cfg = _config(charge_efficiency=0.8)
        # Regression guard for the legacy bug: worse efficiency must mean
        # MORE source energy required, i.e. divide, never multiply.
        self.assertAlmostEqual(battery_math.source_energy_for_charge(8.0, cfg), 10.0)
        worse_cfg = _config(charge_efficiency=0.5)
        self.assertGreater(
            battery_math.source_energy_for_charge(8.0, worse_cfg),
            battery_math.source_energy_for_charge(8.0, cfg),
        )


if __name__ == "__main__":
    unittest.main()
