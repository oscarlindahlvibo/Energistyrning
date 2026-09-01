"""LoadForecastProvider for Smart Planner.

A simple, deterministic house-consumption forecast built from Home
Assistant Recorder history, with NO Home Assistant imports. v1 approach
(per the approved Fas 1 plan): median energy consumption for the same
time-of-day window, optionally split weekday/weekend, computed over the
last `lookback_days` days. No ML, no external weather dependency.

The HA adapter (smart_planner.py) is responsible for querying the Recorder
and turning raw state history into `HistoricalSample` objects; this module
only does the statistics.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import statistics

from .models import LoadForecastPoint


@dataclasses.dataclass(frozen=True)
class HistoricalSample:
    """One bucket of historical energy consumption.

    `start`/`end` define the bucket's own time window (buckets do not need
    to be a fixed size, but a fixed size, e.g. 15 or 60 minutes, gives the
    most stable statistics).
    """

    start: dt.datetime
    end: dt.datetime
    energy_kwh: float

    @property
    def duration_seconds(self) -> float:
        """Sample's own bucket length, in seconds."""
        return (self.end - self.start).total_seconds()


def _is_weekend(moment: dt.datetime) -> bool:
    return moment.weekday() >= 5  # Saturday=5, Sunday=6


def _energy_in_range(
    samples: list[HistoricalSample], start: dt.datetime, end: dt.datetime
) -> float | None:
    """Sum sample energy overlapping [start, end), pro-rated by overlap fraction.

    Returns None if less than half the window is covered by data (i.e. we
    don't trust a mostly-missing window).
    """
    span = (end - start).total_seconds()
    if span <= 0:
        return None
    total = 0.0
    covered = 0.0
    for sample in samples:
        overlap_start = max(sample.start, start)
        overlap_end = min(sample.end, end)
        if overlap_end <= overlap_start:
            continue
        sample_span = sample.duration_seconds
        if sample_span <= 0:
            continue
        overlap_seconds = (overlap_end - overlap_start).total_seconds()
        fraction = overlap_seconds / sample_span
        total += sample.energy_kwh * fraction
        covered += overlap_seconds
    if covered < span * 0.5:
        return None
    return total * (span / covered)


def median_energy_for_time_of_day(
    samples: list[HistoricalSample],
    target_start: dt.datetime,
    target_end: dt.datetime,
    lookback_days: int = 28,
    split_weekday_weekend: bool = True,
    min_samples: int = 3,
) -> tuple[float | None, int]:
    """Return (median_kwh, n_samples_used) for the matching time-of-day window.

    Matches [target_start, target_end) across up to `lookback_days` prior
    days. Returns (None, 0) if fewer than `min_samples` valid historical windows
    were found -- callers must treat this as "no forecast available", not
    silently fall back to 0.
    """
    duration = target_end - target_start
    values: list[float] = []
    for days_back in range(1, lookback_days + 1):
        candidate_start = target_start - dt.timedelta(days=days_back)
        candidate_end = candidate_start + duration
        if split_weekday_weekend and _is_weekend(candidate_start) != _is_weekend(
            target_start
        ):
            continue
        value = _energy_in_range(samples, candidate_start, candidate_end)
        if value is not None:
            values.append(value)
    if len(values) < min_samples:
        return None, len(values)
    return statistics.median(values), len(values)


def forecast_load(
    samples: list[HistoricalSample],
    slots: list[tuple[dt.datetime, dt.datetime]],
    lookback_days: int = 28,
    split_weekday_weekend: bool = True,
    min_samples: int = 3,
    fallback_kwh_per_hour: float | None = None,
) -> list[LoadForecastPoint]:
    """Build a LoadForecastPoint per requested slot.

    `slots` gives the exact [start, end) windows to forecast -- normally
    taken directly from the price series so the load forecast lines up with
    whatever period length Nord Pool is actually delivering (15 min, 60
    min, or otherwise), never a hardcoded assumption.

    If a slot's time-of-day has insufficient history and
    `fallback_kwh_per_hour` is given, that flat rate is used and the point
    is marked degraded. If no fallback is given either, the point is 0 kWh
    and marked degraded -- callers/UI should surface that clearly rather
    than presenting it as a real forecast.
    """
    points: list[LoadForecastPoint] = []
    for start, end in slots:
        median, _ = median_energy_for_time_of_day(
            samples,
            start,
            end,
            lookback_days=lookback_days,
            split_weekday_weekend=split_weekday_weekend,
            min_samples=min_samples,
        )
        if median is not None:
            points.append(LoadForecastPoint(start, end, median, is_degraded=False))
            continue
        duration_hours = (end - start).total_seconds() / 3600.0
        if fallback_kwh_per_hour is not None:
            points.append(
                LoadForecastPoint(
                    start,
                    end,
                    fallback_kwh_per_hour * duration_hours,
                    is_degraded=True,
                )
            )
        else:
            points.append(LoadForecastPoint(start, end, 0.0, is_degraded=True))
    return points
