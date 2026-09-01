import datetime as dt
import unittest

from tests._bootstrap import core  # noqa: F401

from core.forecast_consumption import (
    HistoricalSample,
    forecast_load,
    median_energy_for_time_of_day,
)

TZ = dt.timezone(dt.timedelta(hours=1))


class TestMedianEnergyForTimeOfDay(unittest.TestCase):
    def test_insufficient_history_returns_none(self):
        target_start = dt.datetime(2026, 2, 1, 8, 0, tzinfo=TZ)
        target_end = target_start + dt.timedelta(hours=1)
        value, n = median_energy_for_time_of_day(
            [], target_start, target_end, lookback_days=10, min_samples=3
        )
        self.assertIsNone(value)
        self.assertEqual(n, 0)

    def test_returns_median_of_matching_time_of_day(self):
        target_day = dt.date(2026, 2, 8)  # a Sunday
        # Build weekend-only history so all lookback days match target's weekend-ness.
        samples = []
        for d in range(1, 22):
            day = target_day - dt.timedelta(days=d)
            if day.weekday() < 5:
                continue
            start = dt.datetime(day.year, day.month, day.day, 8, 0, tzinfo=TZ)
            samples.append(
                HistoricalSample(
                    start, start + dt.timedelta(hours=1), 1.0 + (d % 3) * 0.1
                )
            )
        target_start = dt.datetime(
            target_day.year, target_day.month, target_day.day, 8, 0, tzinfo=TZ
        )
        target_end = target_start + dt.timedelta(hours=1)
        value, n = median_energy_for_time_of_day(
            samples, target_start, target_end, lookback_days=28, min_samples=3
        )
        self.assertIsNotNone(value)
        self.assertGreater(n, 0)

    def test_weekday_weekend_split_excludes_mismatched_days(self):
        # Target is a Saturday; history only has weekday samples -> should
        # find nothing when split_weekday_weekend=True.
        target_start = dt.datetime(2026, 2, 7, 8, 0, tzinfo=TZ)  # Saturday
        target_end = target_start + dt.timedelta(hours=1)
        samples = []
        cur = dt.date(2026, 1, 5)  # a Monday
        while cur < dt.date(2026, 2, 7):
            if cur.weekday() < 5:
                start = dt.datetime(cur.year, cur.month, cur.day, 8, 0, tzinfo=TZ)
                samples.append(
                    HistoricalSample(start, start + dt.timedelta(hours=1), 1.0)
                )
            cur += dt.timedelta(days=1)
        value, _n = median_energy_for_time_of_day(
            samples,
            target_start,
            target_end,
            lookback_days=40,
            split_weekday_weekend=True,
            min_samples=3,
        )
        self.assertIsNone(value)


class TestForecastLoad(unittest.TestCase):
    def test_falls_back_when_no_history(self):
        start = dt.datetime(2026, 2, 1, 8, 0, tzinfo=TZ)
        slots = [(start, start + dt.timedelta(hours=1))]
        points = forecast_load([], slots, fallback_kwh_per_hour=0.5)
        self.assertEqual(len(points), 1)
        self.assertTrue(points[0].is_degraded)
        self.assertAlmostEqual(points[0].energy_kwh, 0.5)

    def test_zero_fallback_when_nothing_available_at_all(self):
        start = dt.datetime(2026, 2, 1, 8, 0, tzinfo=TZ)
        slots = [(start, start + dt.timedelta(hours=1))]
        points = forecast_load([], slots, fallback_kwh_per_hour=None)
        self.assertTrue(points[0].is_degraded)
        self.assertEqual(points[0].energy_kwh, 0.0)

    def test_uses_history_when_available(self):
        target_day = dt.date(2026, 2, 9)  # a Monday
        samples = []
        cur = dt.date(2026, 1, 5)
        while cur < target_day:
            if cur.weekday() < 5:
                start = dt.datetime(cur.year, cur.month, cur.day, 8, 0, tzinfo=TZ)
                samples.append(
                    HistoricalSample(start, start + dt.timedelta(hours=1), 1.2)
                )
            cur += dt.timedelta(days=1)
        target_start = dt.datetime(
            target_day.year, target_day.month, target_day.day, 8, 0, tzinfo=TZ
        )
        slots = [(target_start, target_start + dt.timedelta(hours=1))]
        points = forecast_load(samples, slots, lookback_days=40, min_samples=3)
        self.assertFalse(points[0].is_degraded)
        self.assertAlmostEqual(points[0].energy_kwh, 1.2)


if __name__ == "__main__":
    unittest.main()
