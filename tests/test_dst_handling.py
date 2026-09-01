"""DST edge cases for the optimizer.

The optimizer must key everything off each PricePoint's own start/end
(which already carry correct tz-aware offsets once the HA adapter has
parsed them), never off an assumed slot count. A "spring forward" day has
23 hours (92 quarter-hours), a "fall back" day has 25 hours (100
quarter-hours) -- both must simply produce that many slots without any
special-casing in the optimizer itself.
"""

import datetime as dt
import unittest

from tests._bootstrap import core  # noqa: F401

from core import optimizer
from core.models import BatteryConfig, PricePoint

try:
    from zoneinfo import ZoneInfo

    STOCKHOLM = ZoneInfo("Europe/Stockholm")
except Exception:  # pragma: no cover - zoneinfo always available on 3.9+
    STOCKHOLM = None


def _battery() -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=51.2,
        min_soc_fraction=0.2,
        max_soc_fraction=0.9,
        max_charge_power_kw=7.0,
        max_discharge_power_kw=7.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        cycle_cost_sek_per_kwh=0.05,
        soc_resolution_kwh=0.5,
    )


def _quarter_hour_prices(start: dt.datetime, n: int) -> list[PricePoint]:
    prices = []
    cur = start
    for i in range(n):
        spot = 0.3 if i % 8 < 4 else 1.5
        prices.append(PricePoint(cur, cur + dt.timedelta(minutes=15), spot, spot * 0.8))
        cur += dt.timedelta(minutes=15)
    return prices


@unittest.skipIf(STOCKHOLM is None, "zoneinfo not available")
class TestDstHandling(unittest.TestCase):
    def test_spring_forward_short_day_23_hours(self):
        # 2026-03-29 is a Sunday; Sweden's clocks spring forward at 02:00 -> 03:00.
        start = dt.datetime(2026, 3, 29, 0, 0, tzinfo=STOCKHOLM)
        n_slots = 23 * 4  # 92 quarter-hours, not the usual 96
        prices = _quarter_hour_prices(start, n_slots)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=_battery(),
            current_soc_kwh=25.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(len(outcome.result.slots), n_slots)
        total_hours = sum(s.duration_hours for s in prices)
        self.assertAlmostEqual(total_hours, 23.0)

    def test_fall_back_long_day_25_hours(self):
        # 2026-10-25 is a Sunday; Sweden's clocks fall back at 03:00 -> 02:00.
        start = dt.datetime(2026, 10, 25, 0, 0, tzinfo=STOCKHOLM)
        n_slots = 25 * 4  # 100 quarter-hours
        prices = _quarter_hour_prices(start, n_slots)
        outcome = optimizer.plan(
            prices=prices,
            pv_forecast=[],
            load_forecast=[],
            battery_config=_battery(),
            current_soc_kwh=25.0,
            now=start,
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(len(outcome.result.slots), n_slots)
        total_hours = sum(s.duration_hours for s in prices)
        self.assertAlmostEqual(total_hours, 25.0)

    def test_price_point_duration_uses_actual_utc_span_across_dst_boundary(self):
        # The slot that actually spans the spring-forward jump is only
        # nominally 15 minutes of wall-clock label but must still report a
        # sane positive duration in hours (never zero, never negative).
        before_jump = dt.datetime(2026, 3, 29, 1, 45, tzinfo=STOCKHOLM)
        after_jump = dt.datetime(2026, 3, 29, 3, 0, tzinfo=STOCKHOLM)
        point = PricePoint(before_jump, after_jump, 1.0, 0.8)
        self.assertGreater(point.duration_hours, 0)


if __name__ == "__main__":
    unittest.main()
