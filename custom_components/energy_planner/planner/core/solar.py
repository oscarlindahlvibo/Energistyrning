"""Sunrise/sunset (physical daylight window) for Smart Planner.

Standard NOAA-style approximate solar position formulas -- accurate to
within a few minutes, which is plenty for "is this bucket physically
inside daylight" clipping. No external dependency (ephem/astral/etc):
this is pure math over `datetime`/`math`, matching the rest of `core/`
having zero third-party dependencies.

No Home Assistant imports.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math


@dataclasses.dataclass(frozen=True)
class DaylightWindow:
    """One day's sunrise/sunset, in the same tzinfo as the date was given."""

    sunrise: dt.datetime
    sunset: dt.datetime

    @property
    def has_daylight(self) -> bool:
        """False for a polar-night day where the sun never rises."""
        return self.sunset > self.sunrise


def _julian_day(date: dt.date) -> float:
    a = (14 - date.month) // 12
    y = date.year + 4800 - a
    m = date.month + 12 * a - 3
    return (
        date.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    )


def sunrise_sunset(
    date: dt.date, latitude_deg: float, longitude_deg: float, tz: dt.tzinfo
) -> DaylightWindow:
    """Compute sunrise/sunset for `date` at the given location.

    Uses the standard solar-elevation-based approximation (solar noon +
    hour angle from the sun's declination and the observer's latitude).
    Returns a window with sunrise == sunset (has_daylight False) for a
    genuine polar night, and a full 00:00-24:00 window for a genuine
    midnight sun day -- both real cases for northern Sweden in winter/
    summer, not error conditions.
    """
    jd = _julian_day(date)
    n = jd - 2451545.0 + 0.0008

    # Mean solar noon (fractional Julian day offset for this longitude).
    j_star = n - longitude_deg / 360.0

    # Solar mean anomaly (degrees).
    m = (357.5291 + 0.98560028 * j_star) % 360.0
    m_rad = math.radians(m)

    # Equation of the center.
    c = (
        1.9148 * math.sin(m_rad)
        + 0.02 * math.sin(2 * m_rad)
        + 0.0003 * math.sin(3 * m_rad)
    )

    # Ecliptic longitude (degrees).
    lam = (m + 102.9372 + c + 180.0) % 360.0
    lam_rad = math.radians(lam)

    # Solar transit (Julian day).
    j_transit = (
        2451545.0 + j_star + 0.0053 * math.sin(m_rad) - 0.0069 * math.sin(2 * lam_rad)
    )

    # Declination of the sun (radians).
    sin_delta = math.sin(lam_rad) * math.sin(math.radians(23.4397))
    delta = math.asin(sin_delta)

    lat_rad = math.radians(latitude_deg)
    # -0.83 deg standard altitude accounts for atmospheric refraction and
    # the sun's apparent radius.
    cos_omega = (
        math.sin(math.radians(-0.83)) - math.sin(lat_rad) * math.sin(delta)
    ) / (math.cos(lat_rad) * math.cos(delta))

    day_start = dt.datetime.combine(date, dt.time.min, tzinfo=tz)
    if cos_omega > 1:
        # Sun never rises above the horizon this day (polar night).
        return DaylightWindow(sunrise=day_start, sunset=day_start)
    if cos_omega < -1:
        # Sun never sets this day (midnight sun).
        return DaylightWindow(
            sunrise=day_start, sunset=day_start + dt.timedelta(days=1)
        )

    omega = math.degrees(math.acos(cos_omega))
    j_rise = j_transit - omega / 360.0
    j_set = j_transit + omega / 360.0

    sunrise = _julian_day_to_datetime(j_rise, tz)
    sunset = _julian_day_to_datetime(j_set, tz)
    return DaylightWindow(sunrise=sunrise, sunset=sunset)


def _julian_day_to_datetime(jd: float, tz: dt.tzinfo) -> dt.datetime:
    unix_epoch_jd = 2440587.5
    unix_seconds = (jd - unix_epoch_jd) * 86400.0
    return dt.datetime.fromtimestamp(unix_seconds, tz=dt.timezone.utc).astimezone(tz)
