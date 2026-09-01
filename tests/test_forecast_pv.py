import datetime as dt
import unittest

from tests._bootstrap import core  # noqa: F401

from core.forecast_pv import (
    HistoricalPvSample,
    PvSourceForecast,
    build_daily_shape_profile,
    forecast_pv,
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


if __name__ == "__main__":
    unittest.main()
