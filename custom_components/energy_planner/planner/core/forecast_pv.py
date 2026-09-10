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

Two levels are provided, same pattern as `forecast_consumption.py`:

- `forecast_pv()` / `build_daily_shape_profile()`: the original approach
  -- a single recent-history shape profile per source, an externally
  supplied daily total (e.g. a forecast-total sensor), or the degraded
  even-daylight-hours spread. Kept unchanged for backward compatibility.
- `forecast_pv_seasonal()`: the improved seasonal model (per the pre-
  Fas-2 spec's requirement 4) -- blends a recent shape (this specific
  installation's current panel/shading condition) with the same
  calendar period a year ago (seasonal shape, if that much history
  exists), clips the result to the day's real sunrise/sunset window
  (`core.solar`, a physical constraint no amount of historical data can
  override), and estimates the daily total itself the same way (recent
  average blended with same-period-last-year) with a robust uncertainty
  figure -- unless a real external forecast total is supplied, which
  always wins where given.

No Home Assistant imports.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import statistics

from .models import PvForecastPoint
from .solar import sunrise_sunset

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


def _shape_profile_for_window(
    samples: list[HistoricalPvSample],
    window_start: dt.datetime,
    window_end: dt.datetime,
    bucket_minutes: int,
    min_days: int,
) -> DailyShapeProfile | None:
    windowed = [s for s in samples if window_start <= s.start < window_end]
    return build_daily_shape_profile(
        windowed, bucket_minutes=bucket_minutes, min_days=min_days
    )


def build_seasonal_shape_profile(
    samples: list[HistoricalPvSample],
    target_date: dt.date,
    tz: dt.tzinfo,
    bucket_minutes: int = 15,
    recent_days: int = 21,
    prior_year_window_days: int = 21,
    recent_weight: float = 0.6,
    min_days: int = 5,
) -> DailyShapeProfile | None:
    """Blend a recent shape with the same calendar period a year ago.

    Recent history reflects this specific installation's *current*
    condition (panel soiling, new shading from a grown tree, a changed
    inverter setting); the same period last year reflects the seasonal
    *shape* (sun angle, day length) that recent history alone can't show
    yet if, say, it's currently only early autumn and last winter's low-
    sun-angle shape hasn't been seen this cycle. `recent_weight` is how
    much of the blend is recent vs. prior-year (default 60/40).

    Falls back to whichever single window has enough data if only one
    does, and to None (caller must use the degraded even-spread) if
    neither does.
    """
    recent_end = dt.datetime.combine(target_date, dt.time.min, tzinfo=tz)
    recent_start = recent_end - dt.timedelta(days=recent_days)
    recent_profile = _shape_profile_for_window(
        samples, recent_start, recent_end, bucket_minutes, min_days
    )

    prior_year_center = target_date.replace(year=target_date.year - 1)
    prior_year_start = dt.datetime.combine(
        prior_year_center - dt.timedelta(days=prior_year_window_days // 2),
        dt.time.min,
        tzinfo=tz,
    )
    prior_year_end = prior_year_start + dt.timedelta(days=prior_year_window_days)
    prior_year_profile = _shape_profile_for_window(
        samples, prior_year_start, prior_year_end, bucket_minutes, min_days
    )

    if recent_profile is not None and prior_year_profile is not None:
        buckets = set(recent_profile) | set(prior_year_profile)
        return {
            b: recent_weight * recent_profile.get(b, 0.0)
            + (1 - recent_weight) * prior_year_profile.get(b, 0.0)
            for b in buckets
        }
    return recent_profile if recent_profile is not None else prior_year_profile


def apply_daylight_constraint(
    profile: DailyShapeProfile,
    date: dt.date,
    latitude_deg: float,
    longitude_deg: float,
    tz: dt.tzinfo,
    bucket_minutes: int = 15,
) -> DailyShapeProfile:
    """Zero out any bucket outside real sunrise-sunset, then renormalize.

    No amount of historical data can make PV production happen before
    sunrise or after sunset; a profile built from noisy/sparse history
    can still assign a small nonzero share to an impossible bucket
    (measurement noise, a bucket-boundary artifact), which this clips.
    Renormalizes the remaining buckets back to summing to ~1.0 so a
    caller multiplying by a daily total still conserves that total.
    Falls back to an even spread across the *real* daylight window
    (not the hardcoded default) if clipping would zero out everything.
    """
    window = sunrise_sunset(date, latitude_deg, longitude_deg, tz)
    if not window.has_daylight:
        return dict.fromkeys(profile, 0.0)

    clipped = {}
    for bucket, fraction in profile.items():
        bucket_start = dt.datetime.combine(date, dt.time.min, tzinfo=tz) + dt.timedelta(
            minutes=bucket
        )
        bucket_end = bucket_start + dt.timedelta(minutes=bucket_minutes)
        clipped[bucket] = (
            fraction
            if (bucket_end > window.sunrise and bucket_start < window.sunset)
            else 0.0
        )

    total = sum(clipped.values())
    if total <= 0:
        # A profile whose given buckets happen to fall entirely outside
        # daylight (sparse/noisy history) still needs a fallback across
        # the *real* daylight window -- generate the full set of buckets
        # for the day rather than only the ones the profile happened to
        # define, otherwise there would be nothing left to spread across.
        buckets = list(range(0, 24 * 60, bucket_minutes))
        daylight_buckets = [
            b
            for b in buckets
            if (
                dt.datetime.combine(date, dt.time.min, tzinfo=tz)
                + dt.timedelta(minutes=b + bucket_minutes)
                > window.sunrise
            )
            and (
                dt.datetime.combine(date, dt.time.min, tzinfo=tz)
                + dt.timedelta(minutes=b)
                < window.sunset
            )
        ]
        if not daylight_buckets:
            return dict.fromkeys(profile, 0.0)
        fraction = 1.0 / len(daylight_buckets)
        return {b: (fraction if b in daylight_buckets else 0.0) for b in buckets}

    return {bucket: value / total for bucket, value in clipped.items()}


def _robust_uncertainty(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return mad * 1.4826


def estimate_daily_total_with_uncertainty(
    samples: list[HistoricalPvSample],
    target_date: dt.date,
    tz: dt.tzinfo,
    recent_days: int = 14,
    prior_year_window_days: int = 14,
    min_days: int = 3,
) -> tuple[float | None, float]:
    """Estimate a source's daily total (kWh) with a robust uncertainty figure.

    Pools recent daily totals with the same calendar period a year ago
    (whichever are available) and returns (median, MAD-based
    uncertainty). Returns (None, 0.0) if neither window has enough data
    -- callers must treat that as "no estimate", not zero production.
    """
    recent_end = dt.datetime.combine(target_date, dt.time.min, tzinfo=tz)
    recent_start = recent_end - dt.timedelta(days=recent_days)

    prior_year_center = target_date.replace(year=target_date.year - 1)
    prior_year_start = dt.datetime.combine(
        prior_year_center - dt.timedelta(days=prior_year_window_days // 2),
        dt.time.min,
        tzinfo=tz,
    )
    prior_year_end = prior_year_start + dt.timedelta(days=prior_year_window_days)

    by_day: dict[dt.date, float] = {}
    for sample in samples:
        in_recent = recent_start <= sample.start < recent_end
        in_prior_year = prior_year_start <= sample.start < prior_year_end
        if not (in_recent or in_prior_year):
            continue
        day = sample.start.date()
        by_day[day] = by_day.get(day, 0.0) + sample.energy_kwh

    values = list(by_day.values())
    if len(values) < min_days:
        return None, 0.0
    return statistics.median(values), _robust_uncertainty(values)


def forecast_pv_seasonal(
    sources_actual: dict[str, list[HistoricalPvSample]],
    slots: list[tuple[dt.datetime, dt.datetime]],
    latitude_deg: float,
    longitude_deg: float,
    tz: dt.tzinfo,
    weather_daily_total_kwh: dict[str, dict[dt.date, float]] | None = None,
    bucket_minutes: int = 15,
    recent_days: int = 21,
    prior_year_window_days: int = 21,
    recent_weight: float = 0.6,
    min_days: int = 5,
) -> list[PvForecastPoint]:
    """Seasonal PV forecast: blended shape, daylight-clipped, with uncertainty.

    `sources_actual` maps source name -> that source's historical actual
    production (same role as the `profiles` history feeding
    `build_daily_shape_profile` for `forecast_pv()`, but raw samples so
    this function can build the per-target-date seasonal blend itself).
    `weather_daily_total_kwh`, if given, overrides the statistical daily-
    total estimate for a (source, date) pair that's present in it -- the
    place a real external forecast-total source (once one is confirmed
    to exist, see docs/smart-planner.md) would plug in; still degrades
    gracefully to the statistical estimate for anything not covered.
    """
    weather_daily_total_kwh = weather_daily_total_kwh or {}
    target_dates = sorted({s.date() for s, _e in slots})

    totals: dict[tuple[dt.datetime, dt.datetime], float] = dict.fromkeys(slots, 0.0)
    uncertainty: dict[tuple[dt.datetime, dt.datetime], float] = dict.fromkeys(
        slots, 0.0
    )
    degraded: dict[tuple[dt.datetime, dt.datetime], bool] = dict.fromkeys(slots, False)

    for name, samples in sources_actual.items():
        source_weather = weather_daily_total_kwh.get(name, {})
        for date in target_dates:
            day_slots = [(s, e) for s, e in slots if s.date() == date]
            if not day_slots:
                continue

            profile = build_seasonal_shape_profile(
                samples,
                date,
                tz,
                bucket_minutes=bucket_minutes,
                recent_days=recent_days,
                prior_year_window_days=prior_year_window_days,
                recent_weight=recent_weight,
                min_days=min_days,
            )
            is_degraded = profile is None
            if profile is None:
                profile = _even_daylight_fraction(bucket_minutes)
            profile = apply_daylight_constraint(
                profile, date, latitude_deg, longitude_deg, tz, bucket_minutes
            )

            if date in source_weather:
                daily_total = source_weather[date]
                daily_uncertainty = 0.0
            else:
                daily_total, daily_uncertainty = estimate_daily_total_with_uncertainty(
                    samples,
                    date,
                    tz,
                    recent_days=recent_days,
                    prior_year_window_days=prior_year_window_days,
                )
                if daily_total is None:
                    daily_total = 0.0
                    is_degraded = True

            distributed = _distribute_daily_total(
                daily_total, date, day_slots, profile, bucket_minutes
            )
            for start, end, energy in distributed:
                totals[(start, end)] += energy
                fraction = profile.get(
                    (start.hour * 60 + start.minute) // bucket_minutes * bucket_minutes,
                    0.0,
                )
                uncertainty[(start, end)] += daily_uncertainty * fraction
                if is_degraded:
                    degraded[(start, end)] = True

    return [
        PvForecastPoint(
            start,
            end,
            max(0.0, totals[(start, end)]),
            degraded[(start, end)],
            uncertainty_kwh=uncertainty[(start, end)],
        )
        for start, end in slots
    ]
