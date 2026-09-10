import datetime as dt
import unittest

from tests._bootstrap import core  # noqa: F401

from core.forecast_pv import (
    HistoricalPvSample,
    PvSourceForecast,
    apply_daylight_constraint,
    build_daily_shape_profile,
    build_seasonal_shape_profile,
    estimate_daily_total_with_uncertainty,
    forecast_pv,
    forecast_pv_seasonal,
)

TZ = dt.timezone(dt.timedelta(hours=1))


def _slots_for_day(
    day: dt.date, bucket_minutes: int = 60
) -> list[tuple[dt.datetime, dt.datetime]]:
    slots = []
    cur = dt.datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
    end_of_day = cur + dt.timedelta(days=1)
    while cur < end_of_day:
        nxt = cur + dt.timedelta(minutes=bucket_minutes)
        slots.append((cur, nxt))
        cur = nxt
    return slots


class TestDailyShapeProfile(unittest.TestCase):
    def test_insufficient_days_returns_none(self):
        samples = [
            HistoricalPvSample(
                dt.datetime(2026, 6, 1, 12, 0, tzinfo=TZ),
                dt.datetime(2026, 6, 1, 13, 0, tzinfo=TZ),
                2.0,
            )
        ]
        self.assertIsNone(build_daily_shape_profile(samples, min_days=5))

    def test_profile_sums_to_roughly_one(self):
        samples = []
        for d in range(1, 10):
            day = dt.date(2026, 6, 1) + dt.timedelta(days=d)
            for hour, kwh in [(10, 1.0), (12, 2.0), (14, 1.0)]:
                start = dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
                samples.append(
                    HistoricalPvSample(start, start + dt.timedelta(hours=1), kwh)
                )
        profile = build_daily_shape_profile(samples, bucket_minutes=60, min_days=5)
        self.assertIsNotNone(profile)
        self.assertAlmostEqual(sum(profile.values()), 1.0, places=6)


class TestForecastPv(unittest.TestCase):
    def test_daily_total_distributed_by_profile_sums_back_to_total(self):
        day = dt.date(2026, 7, 1)
        slots = _slots_for_day(day, bucket_minutes=60)
        profile = {10 * 60: 0.25, 12 * 60: 0.5, 14 * 60: 0.25}
        source = PvSourceForecast(name="south", daily_total_kwh={day: 10.0})
        points = forecast_pv(
            [source], slots, profiles={"south": profile}, bucket_minutes=60
        )
        total = sum(p.energy_kwh for p in points)
        self.assertAlmostEqual(total, 10.0, places=6)
        degraded_flags = {p.is_degraded for p in points if p.energy_kwh > 0}
        self.assertEqual(degraded_flags, {False})

    def test_missing_profile_falls_back_to_even_daylight_and_is_marked_degraded(self):
        day = dt.date(2026, 7, 1)
        slots = _slots_for_day(day, bucket_minutes=60)
        source = PvSourceForecast(name="west", daily_total_kwh={day: 8.0})
        points = forecast_pv([source], slots, profiles=None, bucket_minutes=60)
        total = sum(p.energy_kwh for p in points)
        self.assertAlmostEqual(total, 8.0, places=6)
        self.assertTrue(any(p.is_degraded and p.energy_kwh > 0 for p in points))

    def test_multiple_sources_are_summed(self):
        day = dt.date(2026, 7, 1)
        slots = _slots_for_day(day, bucket_minutes=60)
        profile = {h * 60: 1 / 24 for h in range(24)}
        sources = [
            PvSourceForecast(name="a", daily_total_kwh={day: 4.0}),
            PvSourceForecast(name="b", daily_total_kwh={day: 6.0}),
        ]
        points = forecast_pv(
            sources, slots, profiles={"a": profile, "b": profile}, bucket_minutes=60
        )
        total = sum(p.energy_kwh for p in points)
        self.assertAlmostEqual(total, 10.0, places=6)

    def test_curve_source_used_directly(self):
        day = dt.date(2026, 7, 1)
        slots = _slots_for_day(day, bucket_minutes=60)
        noon = dt.datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
        source = PvSourceForecast(
            name="curve-source",
            curve=[(noon, noon + dt.timedelta(hours=1), 3.5)],
        )
        points = forecast_pv([source], slots)
        matching = [p for p in points if p.start == noon]
        self.assertEqual(len(matching), 1)
        self.assertAlmostEqual(matching[0].energy_kwh, 3.5)
        self.assertFalse(matching[0].is_degraded)

    def test_source_with_no_data_at_all_marks_everything_degraded(self):
        day = dt.date(2026, 7, 1)
        slots = _slots_for_day(day, bucket_minutes=60)
        source = PvSourceForecast(name="offline", daily_total_kwh={})
        points = forecast_pv([source], slots)
        self.assertTrue(all(p.is_degraded for p in points))
        self.assertTrue(all(p.energy_kwh == 0.0 for p in points))


STOCKHOLM_LAT = 59.33
STOCKHOLM_LON = 18.06


def _samples_for_days(
    days: list[dt.date], hour_kwh: dict[int, float]
) -> list[HistoricalPvSample]:
    samples = []
    for day in days:
        for hour, kwh in hour_kwh.items():
            start = dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
            samples.append(
                HistoricalPvSample(start, start + dt.timedelta(hours=1), kwh)
            )
    return samples


class TestBuildSeasonalShapeProfile(unittest.TestCase):
    def test_blends_recent_and_prior_year_when_both_available(self):
        target_date = dt.date(2026, 7, 15)
        recent_days = [target_date - dt.timedelta(days=d) for d in range(1, 8)]
        prior_year_days = [
            target_date.replace(year=target_date.year - 1) - dt.timedelta(days=d)
            for d in range(-3, 4)
        ]
        # Recent history: all production at hour 12. Prior-year: all at hour 14.
        recent_samples = _samples_for_days(recent_days, {12: 2.0})
        prior_year_samples = _samples_for_days(prior_year_days, {14: 2.0})
        profile = build_seasonal_shape_profile(
            recent_samples + prior_year_samples,
            target_date,
            TZ,
            bucket_minutes=60,
            recent_days=10,
            prior_year_window_days=10,
            recent_weight=0.6,
            min_days=5,
        )
        self.assertIsNotNone(profile)
        self.assertAlmostEqual(profile.get(12 * 60, 0.0), 0.6, places=2)
        self.assertAlmostEqual(profile.get(14 * 60, 0.0), 0.4, places=2)

    def test_falls_back_to_recent_only_when_no_prior_year_data(self):
        target_date = dt.date(2026, 7, 15)
        recent_days = [target_date - dt.timedelta(days=d) for d in range(1, 8)]
        recent_samples = _samples_for_days(recent_days, {12: 2.0})
        profile = build_seasonal_shape_profile(
            recent_samples, target_date, TZ, bucket_minutes=60, min_days=5
        )
        self.assertIsNotNone(profile)
        self.assertAlmostEqual(profile.get(12 * 60, 0.0), 1.0, places=2)

    def test_returns_none_when_neither_window_has_enough_data(self):
        target_date = dt.date(2026, 7, 15)
        samples = _samples_for_days([target_date - dt.timedelta(days=1)], {12: 2.0})
        profile = build_seasonal_shape_profile(
            samples, target_date, TZ, bucket_minutes=60, min_days=5
        )
        self.assertIsNone(profile)


class TestApplyDaylightConstraint(unittest.TestCase):
    def test_zeroes_buckets_outside_daylight_and_renormalizes(self):
        # Summer in Stockholm: sunrise ~03:32, sunset ~22:09 local (TZ=+1).
        date = dt.date(2026, 6, 21)
        profile = {1 * 60: 0.5, 12 * 60: 0.5}  # 01:00 is before sunrise
        result = apply_daylight_constraint(
            profile, date, STOCKHOLM_LAT, STOCKHOLM_LON, TZ, bucket_minutes=60
        )
        self.assertAlmostEqual(result.get(1 * 60, 0.0), 0.0)
        self.assertAlmostEqual(result.get(12 * 60, 0.0), 1.0, places=6)

    def test_winter_night_bucket_zeroed(self):
        # Winter: sunrise ~08:44, sunset ~14:49 local.
        date = dt.date(2026, 12, 21)
        profile = {6 * 60: 0.3, 12 * 60: 0.7}
        result = apply_daylight_constraint(
            profile, date, STOCKHOLM_LAT, STOCKHOLM_LON, TZ, bucket_minutes=60
        )
        self.assertAlmostEqual(result.get(6 * 60, 0.0), 0.0)
        self.assertAlmostEqual(result.get(12 * 60, 0.0), 1.0, places=6)

    def test_falls_back_to_even_spread_when_all_mass_outside_daylight(self):
        date = dt.date(2026, 12, 21)
        profile = {1 * 60: 1.0}  # entirely at 01:00, well before winter sunrise
        result = apply_daylight_constraint(
            profile, date, STOCKHOLM_LAT, STOCKHOLM_LON, TZ, bucket_minutes=60
        )
        self.assertAlmostEqual(sum(result.values()), 1.0, places=2)
        self.assertAlmostEqual(result.get(1 * 60, 0.0), 0.0)


class TestEstimateDailyTotalWithUncertainty(unittest.TestCase):
    def test_returns_none_with_insufficient_data(self):
        target_date = dt.date(2026, 7, 15)
        samples = _samples_for_days([target_date - dt.timedelta(days=1)], {12: 5.0})
        total, uncertainty = estimate_daily_total_with_uncertainty(
            samples, target_date, TZ, min_days=3
        )
        self.assertIsNone(total)
        self.assertEqual(uncertainty, 0.0)

    def test_median_and_uncertainty_from_recent_days(self):
        target_date = dt.date(2026, 7, 15)
        days = [target_date - dt.timedelta(days=d) for d in range(1, 8)]
        # A clear spread with no majority value at the median (unlike a
        # simple 2-value alternation, which can give a MAD of exactly 0
        # depending on which value is more common).
        kwh_values = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0]
        samples = []
        for day, kwh in zip(days, kwh_values, strict=True):
            start = dt.datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
            samples.append(
                HistoricalPvSample(start, start + dt.timedelta(hours=1), kwh)
            )
        total, uncertainty = estimate_daily_total_with_uncertainty(
            samples, target_date, TZ, min_days=3
        )
        self.assertIsNotNone(total)
        self.assertGreater(uncertainty, 0.0)


class TestForecastPvSeasonal(unittest.TestCase):
    def test_conserves_energy_and_clips_to_daylight(self):
        target_date = dt.date(2026, 6, 21)  # summer, sunrise ~02:32 local (TZ=+1)
        history_days = [target_date - dt.timedelta(days=d) for d in range(1, 15)]
        # Consistent 10 kWh/day, all at noon, plus a small noise reading at
        # 01:00 (fully before sunrise) that a real sensor glitch might
        # produce.
        samples = _samples_for_days(history_days, {1: 0.1, 12: 9.9})
        slots = _slots_for_day(target_date, bucket_minutes=60)

        points = forecast_pv_seasonal(
            {"south": samples},
            slots,
            STOCKHOLM_LAT,
            STOCKHOLM_LON,
            TZ,
            bucket_minutes=60,
            recent_days=20,
            min_days=5,
        )
        total = sum(p.energy_kwh for p in points)
        self.assertAlmostEqual(total, 10.0, places=1)

        pre_sunrise = [p for p in points if p.start.hour == 1]
        self.assertEqual(len(pre_sunrise), 1)
        self.assertAlmostEqual(pre_sunrise[0].energy_kwh, 0.0, places=6)

    def test_no_history_marks_degraded_and_zero(self):
        target_date = dt.date(2026, 6, 21)
        slots = _slots_for_day(target_date, bucket_minutes=60)
        points = forecast_pv_seasonal(
            {"offline": []}, slots, STOCKHOLM_LAT, STOCKHOLM_LON, TZ, bucket_minutes=60
        )
        self.assertTrue(all(p.is_degraded for p in points))
        self.assertTrue(all(p.energy_kwh == 0.0 for p in points))

    def test_weather_override_wins_and_has_zero_uncertainty(self):
        target_date = dt.date(2026, 6, 21)
        history_days = [target_date - dt.timedelta(days=d) for d in range(1, 15)]
        samples = _samples_for_days(history_days, {12: 10.0})
        slots = _slots_for_day(target_date, bucket_minutes=60)

        points_stat = forecast_pv_seasonal(
            {"south": samples},
            slots,
            STOCKHOLM_LAT,
            STOCKHOLM_LON,
            TZ,
            bucket_minutes=60,
            min_days=5,
        )
        total_stat = sum(p.energy_kwh for p in points_stat)

        points_weather = forecast_pv_seasonal(
            {"south": samples},
            slots,
            STOCKHOLM_LAT,
            STOCKHOLM_LON,
            TZ,
            weather_daily_total_kwh={"south": {target_date: 25.0}},
            bucket_minutes=60,
            min_days=5,
        )
        total_weather = sum(p.energy_kwh for p in points_weather)

        self.assertAlmostEqual(total_stat, 10.0, places=1)
        self.assertAlmostEqual(total_weather, 25.0, places=1)
        self.assertTrue(all(p.uncertainty_kwh == 0.0 for p in points_weather))


if __name__ == "__main__":
    unittest.main()
