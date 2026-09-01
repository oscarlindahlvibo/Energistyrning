import unittest

from tests._bootstrap import core  # noqa: F401

from core.economics import PriceConfig, compute_import_export_prices


class TestEconomics(unittest.TestCase):
    def test_no_extras_passthrough(self):
        cfg = PriceConfig()
        imp, exp = compute_import_export_prices(1.0, cfg)
        self.assertAlmostEqual(imp, 1.0)
        self.assertAlmostEqual(exp, 1.0)

    def test_vat_and_network_cost_applied_to_import(self):
        cfg = PriceConfig(network_cost_sek_per_kwh=0.3, vat_multiplier=1.25)
        imp, _ = compute_import_export_prices(1.0, cfg)
        self.assertAlmostEqual(imp, 1.0 * 1.25 + 0.3)

    def test_network_compensation_applied_to_export(self):
        cfg = PriceConfig(network_compensation_sek_per_kwh=0.1)
        _, exp = compute_import_export_prices(1.0, cfg)
        self.assertAlmostEqual(exp, 1.0 + 0.1)

    def test_export_vat_defaults_to_no_op(self):
        cfg = PriceConfig(vat_multiplier=1.25)
        _, exp = compute_import_export_prices(1.0, cfg)
        self.assertAlmostEqual(exp, 1.0)

    def test_negative_spot_price_can_make_export_negative(self):
        cfg = PriceConfig(network_compensation_sek_per_kwh=0.05)
        _, exp = compute_import_export_prices(-1.0, cfg)
        self.assertAlmostEqual(exp, -0.95)


if __name__ == "__main__":
    unittest.main()
