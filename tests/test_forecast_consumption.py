import datetime as dt
import unittest

from tests._bootstrap import core  # noqa: F401

from core.forecast_consumption import (
    HistoricalSample,
    forecast_load,
    forecast_load_temperature_aware,
    median_energy_for_time_of_day,
    median_energy_for_time_of_day_and_temperature,
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


def _daily_samples(target_day, hour, energy_by_temp):
    """Build one HistoricalSample per prior calendar day.

    No weekday filtering -- tests call with split_weekday_weekend=False.
    energy_by_temp is a dict of days_back -> (energy_kwh, temp_c).
    """
    samples = []
    for d, (energy, temp) in energy_by_temp.items():
        day = target_day - dt.timedelta(days=d)
        start = dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
        samples.append(
            HistoricalSample(
                start, start + dt.timedelta(hours=1), energy, temperature_c=temp
            )
        )
    return samples


class TestMedianEnergyForTimeOfDayAndTemperature(unittest.TestCase):
    def test_matches_only_within_temperature_tolerance(self):
        target_day = dt.date(2026, 2, 9)  # a Monday
        # Cold days (near 0C) use more energy than mild days (near 10C).
        energy_by_temp = {}
        for d in range(1, 22):
            energy_by_temp[d] = (3.0, 0.0) if d % 2 == 0 else (1.0, 10.0)
        samples = _daily_samples(target_day, 8, energy_by_temp)
        target_start = dt.datetime(2026, 2, 9, 8, 0, tzinfo=TZ)
        target_end = target_start + dt.timedelta(hours=1)
        median, uncertainty, n = median_energy_for_time_of_day_and_temperature(
            samples,
            target_start,
            target_end,
            target_temperature_c=0.0,
            lookback_days=21,
            split_weekday_weekend=False,
            temp_tolerance_c=2.0,
            min_samples=3,
        )
        self.assertIsNotNone(median)
        self.assertAlmostEqual(median, 3.0)
        self.assertGreaterEqual(n, 3)
        self.assertGreaterEqual(uncertainty, 0.0)

    def test_insufficient_matches_returns_none(self):
        target_start = dt.datetime(2026, 2, 9, 8, 0, tzinfo=TZ)
        target_end = target_start + dt.timedelta(hours=1)
        median, uncertainty, n = median_energy_for_time_of_day_and_temperature(
            [],
            target_start,
            target_end,
            target_temperature_c=0.0,
            lookback_days=21,
            min_samples=3,
        )
        self.assertIsNone(median)
        self.assertEqual(uncertainty, 0.0)
        self.assertEqual(n, 0)

    def test_widens_tolerance_when_too_few_close_matches(self):
        target_day = dt.date(2026, 2, 9)  # a Monday
        # Only 2 samples within 2C, but 5 within 4C -- should widen once
        # and succeed rather than giving up.
        energy_by_temp = {
            1: (2.0, 0.5),
            2: (2.2, -0.5),
            3: (2.1, 3.5),
            4: (2.3, -3.5),
            5: (2.0, 3.9),
        }
        samples = _daily_samples(target_day, 8, energy_by_temp)
        target_start = dt.datetime(2026, 2, 9, 8, 0, tzinfo=TZ)
        target_end = target_start + dt.timedelta(hours=1)
        median, _uncertainty, n = median_energy_for_time_of_day_and_temperature(
            samples,
            target_start,
            target_end,
            target_temperature_c=0.0,
            lookback_days=21,
            split_weekday_weekend=False,
            temp_tolerance_c=2.0,
            min_samples=3,
        )
        self.assertIsNotNone(median)
        self.assertEqual(n, 5)


class TestForecastLoadTemperatureAware(unittest.TestCase):
    def test_mismatched_temperature_lengths_raise(self):
        start = dt.datetime(2026, 2, 1, 8, 0, tzinfo=TZ)
        slots = [(start, start + dt.timedelta(hours=1))]
        with self.assertRaises(ValueError):
            forecast_load_temperature_aware([], slots, slot_temperatures_c=[])

    def test_uses_temperature_bucket_when_available(self):
        target_day = dt.date(2026, 2, 9)  # a Monday
        energy_by_temp = {}
        for d in range(1, 22):
            energy_by_temp[d] = (3.0, 0.0) if d % 2 == 0 else (1.0, 10.0)
        samples = _daily_samples(target_day, 8, energy_by_temp)
        target_start = dt.datetime(2026, 2, 9, 8, 0, tzinfo=TZ)
        slots = [(target_start, target_start + dt.timedelta(hours=1))]
        points = forecast_load_temperature_aware(
            samples,
            slots,
            slot_temperatures_c=[0.0],
            lookback_days=21,
            split_weekday_weekend=False,
        )
        self.assertEqual(len(points), 1)
        self.assertFalse(points[0].is_degraded)
        self.assertAlmostEqual(points[0].energy_kwh, 3.0)

    def test_falls_back_to_time_of_day_when_temperature_unknown(self):
        target_day = dt.date(2026, 2, 9)  # a Monday
        samples = []
        for d in range(1, 22):
            day = target_day - dt.timedelta(days=d)
            if day.weekday() != target_day.weekday():
                continue
            start = dt.datetime(day.year, day.month, day.day, 8, 0, tzinfo=TZ)
            samples.append(HistoricalSample(start, start + dt.timedelta(hours=1), 1.5))
        target_start = dt.datetime(2026, 2, 9, 8, 0, tzinfo=TZ)
        slots = [(target_start, target_start + dt.timedelta(hours=1))]
        points = forecast_load_temperature_aware(
            samples, slots, slot_temperatures_c=[None], lookback_days=21
        )
        self.assertFalse(points[0].is_degraded)
        self.assertAlmostEqual(points[0].energy_kwh, 1.5)

    def test_final_fallback_when_no_history_at_all(self):
        start = dt.datetime(2026, 2, 1, 8, 0, tzinfo=TZ)
        slots = [(start, start + dt.timedelta(hours=1))]
        points = forecast_load_temperature_aware(
            [], slots, slot_temperatures_c=[5.0], fallback_kwh_per_hour=0.8
        )
        self.assertTrue(points[0].is_degraded)
        self.assertAlmostEqual(points[0].energy_kwh, 0.8)
        self.assertAlmostEqual(points[0].uncertainty_kwh, 0.4)

    def test_recent_actual_bias_scales_forecast(self):
        target_day = dt.date(2026, 2, 9)  # a Monday
        # Build 21 days of full-day history at a flat 24 kWh/day (1 kWh/h
        # for all 24 slots) so _historical_average_daily_kwh has enough
        # complete days, plus the specific 8h time-of-day bucket used below.
        samples = []
        for d in range(1, 22):
            day = target_day - dt.timedelta(days=d)
            day_start = dt.datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
            samples.append(
                HistoricalSample(day_start, day_start + dt.timedelta(days=1), 24.0)
            )
            if day.weekday() == target_day.weekday():
                start = dt.datetime(day.year, day.month, day.day, 8, 0, tzinfo=TZ)
                samples.append(
                    HistoricalSample(start, start + dt.timedelta(hours=1), 1.0)
                )
        target_start = dt.datetime(2026, 2, 9, 8, 0, tzinfo=TZ)
        slots = [(target_start, target_start + dt.timedelta(hours=1))]
        # Recent actual daily usage (48 kWh) is double the historical
        # average (24 kWh) -> bias should scale the forecast up towards 2x,
        # clamped to [0.5, 2.0].
        points = forecast_load_temperature_aware(
            samples,
            slots,
            slot_temperatures_c=[None],
            lookback_days=21,
            recent_actual_kwh_24h=48.0,
            recent_bias_lookback_days=21,
        )
        self.assertAlmostEqual(points[0].energy_kwh, 2.0)


class TestRobustUncertaintyViaTemperatureMedian(unittest.TestCase):
    def test_uncertainty_is_zero_for_identical_values(self):
        target_day = dt.date(2026, 2, 9)  # a Monday
        energy_by_temp = dict.fromkeys(range(1, 8), (1.0, 0.0))
        samples = _daily_samples(target_day, 8, energy_by_temp)
        target_start = dt.datetime(2026, 2, 9, 8, 0, tzinfo=TZ)
        target_end = target_start + dt.timedelta(hours=1)
        _median, uncertainty, _n = median_energy_for_time_of_day_and_temperature(
            samples,
            target_start,
            target_end,
            target_temperature_c=0.0,
            lookback_days=21,
            split_weekday_weekend=False,
            temp_tolerance_c=2.0,
            min_samples=3,
        )
        self.assertAlmostEqual(uncertainty, 0.0)

    def test_uncertainty_is_positive_for_varying_values(self):
        target_day = dt.date(2026, 2, 9)  # a Monday
        energy_by_temp = {
            1: (1.0, 0.0),
            2: (2.0, 0.5),
            3: (3.0, -0.5),
            4: (1.5, 0.2),
            5: (2.5, -0.2),
        }
        samples = _daily_samples(target_day, 8, energy_by_temp)
        target_start = dt.datetime(2026, 2, 9, 8, 0, tzinfo=TZ)
        target_end = target_start + dt.timedelta(hours=1)
        _median, uncertainty, _n = median_energy_for_time_of_day_and_temperature(
            samples,
            target_start,
            target_end,
            target_temperature_c=0.0,
            lookback_days=21,
            split_weekday_weekend=False,
            temp_tolerance_c=2.0,
            min_samples=3,
        )
        self.assertGreater(uncertainty, 0.0)


if __name__ == "__main__":
    unittest.main()
