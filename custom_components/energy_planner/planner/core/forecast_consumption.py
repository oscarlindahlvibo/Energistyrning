"""LoadForecastProvider for Smart Planner.

A simple, deterministic house-consumption forecast built from Home
Assistant Recorder history, with NO Home Assistant imports.

Two levels are provided:

- `forecast_load()`: the original v1 approach -- median energy for the
  same time-of-day window (+ weekday/weekend split), no temperature.
  Kept as-is for simplicity/backward compatibility.
- `forecast_load_temperature_aware()`: bucket model of
  time-of-day + temperature + weekday/weekend, with a widening-tolerance
  fallback chain down to the plain time-of-day median and finally a flat
  rate, plus a "how's the house behaving today" bias correction from the
  last 24h of actual consumption. Bergvärme makes temperature the
  dominant driver of load beyond time-of-day, which the v1 model ignored
  entirely.

Still no ML: bucket/median statistics only, per the approved plan.

The HA adapter (smart_planner.py) is responsible for querying the Recorder
(and a temperature sensor/forecast) and turning raw data into
`HistoricalSample` objects; this module only does the statistics.
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
    most stable statistics). `temperature_c` is this bucket's average
    outdoor temperature, if known -- None is fine, it just means this
    sample can't participate in temperature-bucketed matching.
    """

    start: dt.datetime
    end: dt.datetime
    energy_kwh: float
    temperature_c: float | None = None

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


def _avg_temperature_in_range(
    samples: list[HistoricalSample], start: dt.datetime, end: dt.datetime
) -> float | None:
    """Energy-weighted-by-time average temperature overlapping [start, end).

    Returns None if no sample with a known temperature overlaps the window.
    """
    span = (end - start).total_seconds()
    if span <= 0:
        return None
    weighted_sum = 0.0
    covered = 0.0
    for sample in samples:
        if sample.temperature_c is None:
            continue
        overlap_start = max(sample.start, start)
        overlap_end = min(sample.end, end)
        if overlap_end <= overlap_start:
            continue
        overlap_seconds = (overlap_end - overlap_start).total_seconds()
        weighted_sum += sample.temperature_c * overlap_seconds
        covered += overlap_seconds
    if covered < span * 0.5:
        return None
    return weighted_sum / covered


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
    values = _collect_time_of_day_values(
        samples, target_start, target_end, lookback_days, split_weekday_weekend
    )
    if len(values) < min_samples:
        return None, len(values)
    return statistics.median(values), len(values)


def _collect_time_of_day_values(
    samples: list[HistoricalSample],
    target_start: dt.datetime,
    target_end: dt.datetime,
    lookback_days: int,
    split_weekday_weekend: bool,
    target_temperature_c: float | None = None,
    temp_tolerance_c: float | None = None,
) -> list[float]:
    """Shared candidate-window collection for the plain and temperature-aware medians.

    If `target_temperature_c` and `temp_tolerance_c` are given, only
    windows whose own historical average temperature falls within that
    tolerance are kept.
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
        if target_temperature_c is not None:
            candidate_temp = _avg_temperature_in_range(
                samples, candidate_start, candidate_end
            )
            if candidate_temp is None:
                continue
            if abs(candidate_temp - target_temperature_c) > temp_tolerance_c:
                continue
        value = _energy_in_range(samples, candidate_start, candidate_end)
        if value is not None:
            values.append(value)
    return values


def _robust_uncertainty(values: list[float]) -> float:
    """Median absolute deviation, scaled to be comparable to a std dev.

    (x1.4826 for normally-distributed data) -- more outlier-resistant
    than a plain std dev for a small, noisy historical sample.
    """
    if len(values) < 2:
        return 0.0
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return mad * 1.4826


def median_energy_for_time_of_day_and_temperature(
    samples: list[HistoricalSample],
    target_start: dt.datetime,
    target_end: dt.datetime,
    target_temperature_c: float,
    lookback_days: int = 60,
    split_weekday_weekend: bool = True,
    temp_tolerance_c: float = 3.0,
    min_samples: int = 3,
) -> tuple[float | None, float, int]:
    """Return (median_kwh, uncertainty_kwh, n_samples_used).

    Matches the same time-of-day window as `median_energy_for_time_of_day`,
    additionally restricted to historical days whose own average
    temperature during that window was within `temp_tolerance_c` of
    `target_temperature_c`. If too few matches are found, the tolerance is
    doubled once before giving up (returns (None, 0.0, n)).

    `uncertainty_kwh` is a robust (MAD-based) spread of the matched
    historical values -- how much these bucket-mates actually varied,
    which is exactly the "how good is this forecast" signal `core.reserve`
    needs. A longer default lookback (60 vs. the original 28 days) is used
    since restricting by temperature already narrows the candidate pool a
    lot; a full heating season eventually gives much better coverage.
    """
    values = _collect_time_of_day_values(
        samples,
        target_start,
        target_end,
        lookback_days,
        split_weekday_weekend,
        target_temperature_c=target_temperature_c,
        temp_tolerance_c=temp_tolerance_c,
    )
    if len(values) < min_samples:
        values = _collect_time_of_day_values(
            samples,
            target_start,
            target_end,
            lookback_days,
            split_weekday_weekend,
            target_temperature_c=target_temperature_c,
            temp_tolerance_c=temp_tolerance_c * 2,
        )
    if len(values) < min_samples:
        return None, 0.0, len(values)
    return statistics.median(values), _robust_uncertainty(values), len(values)


def _historical_average_daily_kwh(
    samples: list[HistoricalSample], as_of: dt.datetime, lookback_days: int
) -> float | None:
    """Average of full-day (00:00-24:00) energy totals over recent days.

    Uses the last `lookback_days` calendar days before `as_of`'s date.
    Used only for the recent-actual bias correction -- returns None if
    fewer than 5 complete days of data are available.
    """
    day_start = dt.datetime.combine(as_of.date(), dt.time.min, tzinfo=as_of.tzinfo)
    totals = []
    for days_back in range(1, lookback_days + 1):
        start = day_start - dt.timedelta(days=days_back)
        end = start + dt.timedelta(days=1)
        value = _energy_in_range(samples, start, end)
        if value is not None:
            totals.append(value)
    if len(totals) < 5:
        return None
    return statistics.mean(totals)


def forecast_load(
    samples: list[HistoricalSample],
    slots: list[tuple[dt.datetime, dt.datetime]],
    lookback_days: int = 28,
    split_weekday_weekend: bool = True,
    min_samples: int = 3,
    fallback_kwh_per_hour: float | None = None,
) -> list[LoadForecastPoint]:
    """Build a LoadForecastPoint per requested slot (no temperature).

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


def forecast_load_temperature_aware(
    samples: list[HistoricalSample],
    slots: list[tuple[dt.datetime, dt.datetime]],
    slot_temperatures_c: list[float | None],
    lookback_days: int = 60,
    split_weekday_weekend: bool = True,
    temp_tolerance_c: float = 3.0,
    min_samples: int = 3,
    recent_actual_kwh_24h: float | None = None,
    recent_bias_lookback_days: int = 21,
    fallback_kwh_per_hour: float | None = None,
) -> list[LoadForecastPoint]:
    """Build a LoadForecastPoint per requested slot, temperature-aware.

    `slot_temperatures_c` must be the same length as `slots`, aligned
    index-for-index: the forecasted (or None if unknown) outdoor
    temperature for that slot.

    Per-slot fallback chain:
    1. time-of-day + weekday/weekend + temperature bucket (widening
       tolerance once if too few matches)
    2. time-of-day + weekday/weekend only (temperature ignored) --
       `median_energy_for_time_of_day`, uncertainty estimated the same
       (robust MAD) way from its own matched values
    3. flat `fallback_kwh_per_hour` (if given) or 0.0, both marked
       degraded, with an uncertainty equal to half the fallback value
       itself (a flat guess should never be reported as low-risk)

    `recent_actual_kwh_24h`, if given, is compared against this
    installation's own recent historical daily average (over
    `recent_bias_lookback_days`) to produce a single bias multiplier
    applied to every slot's forecast -- a simple way to reflect "the
    house has been using noticeably more/less than usual lately" (e.g. a
    cold snap already under way, extra people home) without a full
    walk-forward re-forecast. Clamped to [0.5, 2.0] so a single noisy
    24h reading can't blow up the whole forecast.
    """
    if len(slot_temperatures_c) != len(slots):
        raise ValueError(
            f"slot_temperatures_c length ({len(slot_temperatures_c)}) must "
            f"match slots length ({len(slots)})"
        )

    bias = 1.0
    if recent_actual_kwh_24h is not None and slots:
        historical_avg = _historical_average_daily_kwh(
            samples, slots[0][0], recent_bias_lookback_days
        )
        if historical_avg is not None and historical_avg > 0:
            bias = recent_actual_kwh_24h / historical_avg
            bias = max(0.5, min(2.0, bias))

    points: list[LoadForecastPoint] = []
    for (start, end), temp_c in zip(slots, slot_temperatures_c, strict=True):
        duration_hours = (end - start).total_seconds() / 3600.0

        if temp_c is not None:
            median, uncertainty, _n = median_energy_for_time_of_day_and_temperature(
                samples,
                start,
                end,
                temp_c,
                lookback_days=lookback_days,
                split_weekday_weekend=split_weekday_weekend,
                temp_tolerance_c=temp_tolerance_c,
                min_samples=min_samples,
            )
            if median is not None:
                points.append(
                    LoadForecastPoint(
                        start,
                        end,
                        median * bias,
                        is_degraded=False,
                        uncertainty_kwh=uncertainty * bias,
                    )
                )
                continue

        # Fall back to time-of-day only (temperature ignored/unavailable).
        values = _collect_time_of_day_values(
            samples, start, end, lookback_days, split_weekday_weekend
        )
        if len(values) >= min_samples:
            points.append(
                LoadForecastPoint(
                    start,
                    end,
                    statistics.median(values) * bias,
                    is_degraded=False,
                    uncertainty_kwh=_robust_uncertainty(values) * bias,
                )
            )
            continue

        # Final fallback: flat rate (or zero), always marked degraded.
        if fallback_kwh_per_hour is not None:
            value = fallback_kwh_per_hour * duration_hours * bias
            points.append(
                LoadForecastPoint(
                    start, end, value, is_degraded=True, uncertainty_kwh=value * 0.5
                )
            )
        else:
            points.append(LoadForecastPoint(start, end, 0.0, is_degraded=True))
    return points
