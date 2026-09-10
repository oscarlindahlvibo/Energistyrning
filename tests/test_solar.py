import datetime as dt
import unittest

from tests._bootstrap import core  # noqa: F401

from core.solar import sunrise_sunset

TZ = dt.timezone(dt.timedelta(hours=1))
STOCKHOLM_LAT = 59.33
STOCKHOLM_LON = 18.06


class TestSunriseSunset(unittest.TestCase):
    def test_summer_solstice_matches_known_stockholm_values(self):
        window = sunrise_sunset(dt.date(2026, 6, 21), STOCKHOLM_LAT, STOCKHOLM_LON, TZ)
        # Real values: sunrise ~03:31, sunset ~22:07 (CEST, UTC+2 in summer,
        # but TZ here is a fixed UTC+1 offset -- shift expectations by 1h).
        self.assertEqual(window.sunrise.hour, 2)
        self.assertTrue(21 <= window.sunset.hour <= 22)
        self.assertTrue(window.has_daylight)

    def test_winter_solstice_matches_known_stockholm_values(self):
        window = sunrise_sunset(dt.date(2026, 12, 21), STOCKHOLM_LAT, STOCKHOLM_LON, TZ)
        # Real values: sunrise ~08:44, sunset ~14:48 (CET = UTC+1, matches TZ).
        self.assertTrue(8 <= window.sunrise.hour <= 9)
        self.assertTrue(14 <= window.sunset.hour <= 15)
        self.assertTrue(window.has_daylight)

    def test_summer_day_longer_than_winter_day(self):
        summer = sunrise_sunset(dt.date(2026, 6, 21), STOCKHOLM_LAT, STOCKHOLM_LON, TZ)
        winter = sunrise_sunset(dt.date(2026, 12, 21), STOCKHOLM_LAT, STOCKHOLM_LON, TZ)
        summer_hours = (summer.sunset - summer.sunrise).total_seconds() / 3600
        winter_hours = (winter.sunset - winter.sunrise).total_seconds() / 3600
        self.assertGreater(summer_hours, 15)
        self.assertLess(winter_hours, 8)

    def test_further_north_has_more_extreme_seasonal_swing(self):
        # Kiruna (67.85N) should have a much bigger summer/winter day-length
        # difference than Stockholm (59.33N).
        kiruna_summer = sunrise_sunset(dt.date(2026, 6, 21), 67.85, 20.22, TZ)
        kiruna_winter = sunrise_sunset(dt.date(2026, 12, 21), 67.85, 20.22, TZ)
        # Kiruna is inside the Arctic Circle -- midnight sun / polar night.
        summer_hours = (
            kiruna_summer.sunset - kiruna_summer.sunrise
        ).total_seconds() / 3600
        winter_hours = (
            kiruna_winter.sunset - kiruna_winter.sunrise
        ).total_seconds() / 3600
        self.assertGreaterEqual(summer_hours, 23.9)
        self.assertLessEqual(winter_hours, 0.1)
        self.assertFalse(kiruna_winter.has_daylight)

    def test_equator_day_length_roughly_constant(self):
        d1 = sunrise_sunset(dt.date(2026, 3, 20), 0.0, 0.0, dt.timezone.utc)
        d2 = sunrise_sunset(dt.date(2026, 9, 23), 0.0, 0.0, dt.timezone.utc)
        h1 = (d1.sunset - d1.sunrise).total_seconds() / 3600
        h2 = (d2.sunset - d2.sunrise).total_seconds() / 3600
        self.assertAlmostEqual(h1, 12.0, delta=0.3)
        self.assertAlmostEqual(h2, 12.0, delta=0.3)


if __name__ == "__main__":
    unittest.main()
