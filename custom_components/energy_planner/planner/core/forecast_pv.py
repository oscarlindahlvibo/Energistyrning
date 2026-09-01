"""PvForecastProvider for Smart Planner.

Abstraction over the four separate PV forecasts (one per string/
orientation) that already exist as HA sensors
(`sensor.energy_production_tomorrow[_2/_3/_4]`).

Those sensors currently expose a single *daily total* in kWh for tomorrow,
not a sub-daily curve. This module deliberately does NOT pretend to have a
15-minute PV curve when only a daily total is available: it distributes the
daily total across the day using a normalized historical production
*shape* (fraction of the day's energy produced in each time-of-day bucket),
built separately per PV source so differing orientations (e.g. east vs
west-facing strings) are each shaped correctly.

If a source's own historical shape is not available yet, an even spread
across daylight hours is used as an explicitly degraded fallback -- this
must never be silently treated as equal quality to a real profile or a
real sub-daily curve.

No Home Assistant imports.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from .models import PvForecastPoint

_DEFAULT_DAYLIGHT_START_HOUR = 6
_DEFAULT_DAYLIGHT_END_HOUR = 20


@dataclasses.dataclass(frozen=True)
class HistoricalPvSample:
    """One bucket of historical actual PV production for a single source."""

    start: dt.datetime
    end: dt.datetime
    energy_kwh: float


@dataclasses.dataclass(frozen=True)
class PvSourceForecast:
    """What we know about one PV source (string/orientation) for the horizon.

    - If `curve` is provided (list of (start, end, energy_kwh)), it is used
      directly -- this is the path to take if/when the underlying
      integration starts exposing a real sub-daily forecast curve via
      attributes.
    - Else if `daily_total_kwh` is known, the total is distributed across
      the requested slots using `profile` (see `build_daily_shape_profile`)
      or, lacking a profile, an even daylight spread (degraded).
    - Else (nothing known for this source) it contributes 0 for the
      requested slots, marked degraded.
    """

    name: str
    daily_total_kwh: dict[dt.date, float] = dataclasses.field(default_factory=dict)
    curve: list[tuple[dt.datetime, dt.datetime, float]] | None = None


DailyShapeProfile = dict[int, float]
"""Maps minute-of-day (0..1439, at the historical sampling resolution's
granularity) to a fraction of that day's total production. Values for a
full day should sum to ~1.0."""


def build_daily_shape_profile(
    samples: list[HistoricalPvSample],
    bucket_minutes: int = 15,
    min_days: int = 5,
) -> DailyShapeProfile | None:
    """Build a normalized production shape from historical actual PV samples.

    One source's average share of the day's total energy produced in each
    minute-of-day bucket. Returns None if fewer than `min_days` distinct
    calendar days of data are available -- callers must fall back to the
    degraded even-spread path rather than trust a profile built from too
    little history.
    """
    by_day: dict[dt.date, dict[int, float]] = {}
    for sample in samples:
        day = sample.start.date()
        minute_of_day = sample.start.hour * 60 + sample.start.minute
        bucket = minute_of_day // bucket_minutes * bucket_minutes
        by_day.setdefault(day, {})
        by_day[day][bucket] = by_day[day].get(bucket, 0.0) + sample.energy_kwh

    valid_days = {
        day: buckets for day, buckets in by_day.items() if sum(buckets.values()) > 0
    }
    if len(valid_days) < min_days:
        return None

    accumulated: dict[int, float] = {}
    for buckets in valid_days.values():
        day_total = sum(buckets.values())
        for bucket, energy in buckets.items():
            accumulated[bucket] = accumulated.get(bucket, 0.0) + energy / day_total

    n_days = len(valid_days)
    return {bucket: value / n_days for bucket, value in accumulated.items()}


def _even_daylight_fraction(
    bucket_minutes: int,
    daylight_start_hour: int = _DEFAULT_DAYLIGHT_START_HOUR,
    daylight_end_hour: int = _DEFAULT_DAYLIGHT_END_HOUR,
) -> DailyShapeProfile:
    buckets = range(0, 24 * 60, bucket_minutes)
    daylight_buckets = [
        b for b in buckets if daylight_start_hour * 60 <= b < daylight_end_hour * 60
    ]
    if not daylight_buckets:
        # Degenerate config -- spread across the whole day rather than crash.
        daylight_buckets = list(buckets)
    fraction = 1.0 / len(daylight_buckets)
    return {b: (fraction if b in daylight_buckets else 0.0) for b in buckets}


def _distribute_daily_total(
    daily_total_kwh: float,
    day: dt.date,
    slots: list[tuple[dt.datetime, dt.datetime]],
    profile: DailyShapeProfile,
    bucket_minutes: int,
) -> list[tuple[dt.datetime, dt.datetime, float]]:
    points = []
    for start, end in slots:
        if start.date() != day:
            points.append((start, end, 0.0))
            continue
        bucket = (start.hour * 60 + start.minute) // bucket_minutes * bucket_minutes
        fraction = profile.get(bucket, 0.0)
        # Scale the fraction (defined per `bucket_minutes` bucket) to this
        # slot's actual duration.
        slot_minutes = (end - start).total_seconds() / 60.0
        scaled_fraction = fraction * (slot_minutes / bucket_minutes)
        points.append((start, end, daily_total_kwh * scaled_fraction))
    return points


def forecast_pv(
    sources: list[PvSourceForecast],
    slots: list[tuple[dt.datetime, dt.datetime]],
    profiles: dict[str, DailyShapeProfile] | None = None,
    bucket_minutes: int = 15,
) -> list[PvForecastPoint]:
    """Combine all PV sources into a single per-slot forecast.

    `profiles` maps source name -> DailyShapeProfile (from
    `build_daily_shape_profile`, using that source's own history). Missing
    a profile for a source falls back to an even daylight spread and marks
    those points degraded.
    """
    profiles = profiles or {}
    totals: dict[tuple[dt.datetime, dt.datetime], float] = dict.fromkeys(slots, 0.0)
    degraded: dict[tuple[dt.datetime, dt.datetime], bool] = dict.fromkeys(slots, False)

    for source in sources:
        if source.curve is not None:
            for c_start, c_end, c_energy in source.curve:
                for start, end in slots:
                    overlap_start = max(start, c_start)
                    overlap_end = min(end, c_end)
                    if overlap_end <= overlap_start:
                        continue
                    c_span = (c_end - c_start).total_seconds()
                    if c_span <= 0:
                        continue
                    fraction = (overlap_end - overlap_start).total_seconds() / c_span
                    totals[(start, end)] += c_energy * fraction
            continue

        if not source.daily_total_kwh:
            for slot in slots:
                degraded[slot] = True
            continue

        profile = profiles.get(source.name)
        is_degraded_source = profile is None
        if profile is None:
            profile = _even_daylight_fraction(bucket_minutes)

        for day, total_kwh in source.daily_total_kwh.items():
            distributed = _distribute_daily_total(
                total_kwh, day, slots, profile, bucket_minutes
            )
            for start, end, energy in distributed:
                totals[(start, end)] += energy
                if is_degraded_source and energy != 0.0:
                    degraded[(start, end)] = True

        covered_days = set(source.daily_total_kwh.keys())
        for start, end in slots:
            if start.date() not in covered_days:
                degraded[(start, end)] = True

    return [
        PvForecastPoint(
            start, end, max(0.0, totals[(start, end)]), degraded[(start, end)]
        )
        for start, end in slots
    ]
