"""Dynamic (economically-optimized, not fixed) SOC reserve for Smart Planner.

Computes a per-slot "soft" reserve target in kWh, driven by forecast
uncertainty for load (and optionally PV) over an upcoming lookahead
window. This is deliberately NOT a hard SOC floor: `optimizer.plan()`
only ever treats it as a shadow-price penalty
(`BatteryConfig.reserve_cost_sek_per_kwh`), so a big enough price spread
can still make the optimizer sell into the reserve -- exactly the
"ekonomiskt optimerad, inte ett fast SOC-golv" behaviour that was asked
for. On a normal, low-uncertainty day the reserve is small; on a cold or
otherwise hard-to-forecast day (higher `uncertainty_kwh` on the load/PV
forecast points feeding this) the reserve grows automatically, with no
separate "is this a cold day" branch needed.

No Home Assistant imports.
"""

from __future__ import annotations

import datetime as dt

from .models import LoadForecastPoint, PvForecastPoint


def compute_dynamic_reserve(
    slots: list[tuple[dt.datetime, dt.datetime]],
    load_forecast: list[LoadForecastPoint],
    pv_forecast: list[PvForecastPoint] | None = None,
    lookahead_hours: float = 6.0,
    z: float = 1.0,
    pv_weight: float = 0.5,
) -> list[float]:
    """Return one reserve target (kWh) per slot in `slots`.

    `load_forecast` and `pv_forecast` (if given) must be the same length
    as `slots` and aligned index-for-index with it (exactly how
    `forecast_load()`/`forecast_pv()` already return their output).

    For each slot, sums the load forecast's `uncertainty_kwh` (plus
    `pv_weight` x the PV forecast's `uncertainty_kwh`, since low-than-
    expected PV also means needing more from the battery) over the
    following `lookahead_hours`, scaled by `z`. `z=1.0` means "keep about
    one typical forecast error's worth of margin"; a caller wanting more
    conservative behaviour (e.g. explicitly for a known-uncertain
    forecast) can pass a higher z -- but note the mechanism is already
    self-adjusting via the per-slot uncertainty values themselves, so in
    most cases z does not need to change day to day.
    """
    if len(load_forecast) != len(slots):
        raise ValueError(
            f"load_forecast length ({len(load_forecast)}) must match "
            f"slots length ({len(slots)})"
        )
    if pv_forecast is not None and len(pv_forecast) != len(slots):
        raise ValueError(
            f"pv_forecast length ({len(pv_forecast)}) must match "
            f"slots length ({len(slots)})"
        )

    reserve: list[float] = []
    for i, (start, _end) in enumerate(slots):
        window_end = start + dt.timedelta(hours=lookahead_hours)
        total_uncertainty = 0.0
        for j in range(i, len(slots)):
            slot_start = slots[j][0]
            if slot_start >= window_end:
                break
            total_uncertainty += load_forecast[j].uncertainty_kwh
            if pv_forecast is not None:
                total_uncertainty += pv_weight * pv_forecast[j].uncertainty_kwh
        reserve.append(max(0.0, z * total_uncertainty))
    return reserve
