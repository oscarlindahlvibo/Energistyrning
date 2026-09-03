import datetime as dt
import unittest

from tests._bootstrap import core  # noqa: F401

from core.models import LoadForecastPoint, PvForecastPoint
from core.reserve import compute_dynamic_reserve

TZ = dt.timezone(dt.timedelta(hours=1))


def _slots(start, n, minutes=15):
    out = []
    cur = start
    for _ in range(n):
        nxt = cur + dt.timedelta(minutes=minutes)
        out.append((cur, nxt))
        cur = nxt
    return out


class TestComputeDynamicReserve(unittest.TestCase):
    def test_zero_uncertainty_gives_zero_reserve(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        slots = _slots(start, 8)
        load = [LoadForecastPoint(s, e, 1.0, uncertainty_kwh=0.0) for s, e in slots]
        result = compute_dynamic_reserve(slots, load, lookahead_hours=2.0)
        self.assertEqual(result, [0.0] * 8)

    def test_sums_uncertainty_within_lookahead_window(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        slots = _slots(start, 8)  # 15-min slots, 2h = 8 slots total
        load = [LoadForecastPoint(s, e, 1.0, uncertainty_kwh=1.0) for s, e in slots]
        # 1-hour lookahead = 4 slots
        result = compute_dynamic_reserve(slots, load, lookahead_hours=1.0, z=1.0)
        # First slot: itself + next 3 slots within the next hour = 4 kWh
        self.assertAlmostEqual(result[0], 4.0)
        # Last slot: only itself remains within its own lookahead window
        self.assertAlmostEqual(result[-1], 1.0)

    def test_z_scales_linearly(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        slots = _slots(start, 4)
        load = [LoadForecastPoint(s, e, 1.0, uncertainty_kwh=2.0) for s, e in slots]
        r1 = compute_dynamic_reserve(slots, load, lookahead_hours=1.0, z=1.0)
        r2 = compute_dynamic_reserve(slots, load, lookahead_hours=1.0, z=2.0)
        for a, b in zip(r1, r2, strict=True):
            self.assertAlmostEqual(b, a * 2)

    def test_pv_uncertainty_adds_with_configured_weight(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        slots = _slots(start, 4)
        load = [LoadForecastPoint(s, e, 1.0, uncertainty_kwh=1.0) for s, e in slots]
        pv = [PvForecastPoint(s, e, 1.0, uncertainty_kwh=2.0) for s, e in slots]
        result = compute_dynamic_reserve(
            slots, load, pv_forecast=pv, lookahead_hours=0.25, pv_weight=0.5
        )
        # Only this slot's own window (15 min = 1 slot): 1.0 (load) + 0.5*2.0 (pv) = 2.0
        self.assertAlmostEqual(result[0], 2.0)

    def test_mismatched_lengths_raise(self):
        start = dt.datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
        slots = _slots(start, 4)
        load = [LoadForecastPoint(s, e, 1.0) for s, e in slots[:3]]
        with self.assertRaises(ValueError):
            compute_dynamic_reserve(slots, load)


if __name__ == "__main__":
    unittest.main()
