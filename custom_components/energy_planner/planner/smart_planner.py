"""Smart Planner: Home Assistant adapter for the pure-Python optimizer core.

FAS 1 -- SHADOW MODE ONLY.

This module reads live/historical state from Home Assistant, feeds it to
`core.optimizer`, and publishes the result as new sensor entities
(`sensor.energy_planner_smart_*`). It NEVER writes to `slot_N_*` entities
and NEVER touches any Solis entity, directly or indirectly. That is a hard
rule for Fas 1: only `update_battery_action.py` (unchanged) may ever
control the battery.

Runs independently of `planner_state`: shadow mode is controlled by its own
switch, `switch.energy_planner_smart_shadow_enabled`, so it can compute in
parallel with whatever planner (basic/cheapest hours/price peak/off) is
actually driving the house.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from ..const import DOMAIN
from .core import optimizer
from .core.economics import PriceConfig, compute_import_export_prices
from .core.forecast_consumption import HistoricalSample, forecast_load
from .core.forecast_pv import (
    HistoricalPvSample,
    PvSourceForecast,
    build_daily_shape_profile,
    forecast_pv,
)
from .core.models import BatteryConfig, PlanOutcome, PricePoint
from .nordpool_utils import fetch_nordpool_data, tzs
from .utils import parse_datetime

_LOGGER = logging.getLogger(__name__)

SHADOW_SWITCH_ID = "smart_shadow_enabled"
"""Config-store key for switch.energy_planner_smart_shadow_enabled."""

HORIZON_HOURS = 36
LOOKBACK_DAYS_LOAD = 28
LOOKBACK_DAYS_PV_PROFILE = 21

# Battery telemetry. Mirrors the entity IDs already hardcoded in
# examples/update_battery_action.py -- kept identical on purpose so both
# the real executor and the shadow planner read the same source of truth.
BATTERY_SOC_ENTITY = "sensor.solis_s6_solis_battery_soc"
PV_POWER_ENTITY = "sensor.solis_s6_solis_total_pv_power"
HOUSE_LOAD_POWER_ENTITY = "sensor.solis_s6_solis_household_load_power"

# The four PV *forecast* sensors originally described by the user (daily
# totals for tomorrow). GAP: these exact names were not found among this
# installation's actual entities (verified against a real export of the
# HA recorder statistics, see docs/smart-planner.md) -- kept here as the
# documented intent, but they need to be confirmed/corrected against the
# live instance before shadow mode can use them. Until then, PV daily-total
# forecasting has no live source and falls back to the degraded even
# daylight spread the same way a missing actual-production entity does.
DEFAULT_PV_FORECAST_ENTITIES = [
    "sensor.energy_production_tomorrow",
    "sensor.energy_production_tomorrow_2",
    "sensor.energy_production_tomorrow_3",
    "sensor.energy_production_tomorrow_4",
]
# The four PV *actual* production entities, confirmed against a real
# export of this installation's HA recorder statistics (one year, cross-
# validated against two independent sources -- see docs/smart-planner.md).
# These feed build_daily_shape_profile() so the PV forecast can use a real
# historical shape per string/orientation instead of an even daylight
# spread. Each is a cumulative kWh meter; _statistics_to_pv_samples reads
# the recorder's "sum" statistic (not "mean") accordingly.
DEFAULT_PV_ACTUAL_ENTITIES: list[str] = [
    "sensor.solis_s6_solis_pv_energy_1",
    "sensor.solis_s6_solis_pv_energy_2",
    "sensor.solis_s6_solis_pv_energy_3",
    "sensor.solis_s6_solis_pv_energy_4",
]


def _config(hass: HomeAssistant, key: str, default):
    return hass.data[DOMAIN]["config"].get(key, default)


def _battery_config_from_ha(hass: HomeAssistant) -> BatteryConfig:
    capacity_wh = float(_config(hass, "battery_capacity", 25600))
    min_soc_pct = float(_config(hass, "battery_shutdown_soc", 20))
    max_soc_pct = float(_config(hass, "battery_max_soc", 90))
    # New Fas-1 config keys (see number.py). Fall back to conservative
    # defaults if not yet configured, and log loudly so it's obvious in
    # the shadow-mode logs that a real value is still needed.
    max_charge_kw = _config(hass, "battery_max_charge_power_kw", None)
    max_discharge_kw = _config(hass, "battery_max_discharge_power_kw", None)
    if max_charge_kw is None or max_discharge_kw is None:
        _LOGGER.warning(
            "Smart Planner: battery_max_charge_power_kw/"
            "battery_max_discharge_power_kw not configured yet -- "
            "using a conservative 3 kW placeholder. Set the real inverter "
            "limit (in kW, not amps) for an accurate plan."
        )
    charge_eff = float(_config(hass, "battery_charge_efficiency", 95)) / 100
    discharge_eff = float(_config(hass, "battery_discharge_efficiency", 95)) / 100
    cycle_cost = float(_config(hass, "battery_cycle_cost_sek_per_kwh", 0.0))

    return BatteryConfig(
        capacity_kwh=capacity_wh / 1000.0,
        min_soc_fraction=min_soc_pct / 100.0,
        max_soc_fraction=max_soc_pct / 100.0,
        max_charge_power_kw=float(max_charge_kw) if max_charge_kw else 3.0,
        max_discharge_power_kw=float(max_discharge_kw) if max_discharge_kw else 3.0,
        charge_efficiency=charge_eff,
        discharge_efficiency=discharge_eff,
        cycle_cost_sek_per_kwh=cycle_cost,
        soc_resolution_kwh=0.25,
    )


def _price_config_from_ha(hass: HomeAssistant) -> PriceConfig:
    # network_cost/network_compensation are already stored in öre/kWh in
    # the existing config entities (see number.py) -- convert to SEK/kWh
    # here, once, per the "normalize economics at ingestion" rule.
    network_cost_ore = float(_config(hass, "network_cost", 0.0))
    network_compensation_ore = float(_config(hass, "network_compensation", 0.0))
    return PriceConfig(
        network_cost_sek_per_kwh=network_cost_ore / 100.0,
        network_compensation_sek_per_kwh=network_compensation_ore / 100.0,
        # The legacy planners consume nordpool "value" directly as SEK/kWh
        # with no extra VAT multiplication (get_nordpool_price_per_kwh_in_cent
        # exists in utils.py but is not actually called anywhere) -- so the
        # Nordpool integration is assumed to already deliver VAT-inclusive
        # SEK/kWh. Kept as a no-op multiplier here for the same reason; if
        # that assumption turns out wrong for this installation, this is
        # the one place to fix it.
        vat_multiplier=1.0,
        export_vat_multiplier=1.0,
    )


async def _fetch_price_points(hass: HomeAssistant) -> list[PricePoint] | None:
    """Build the price horizon from the same Nordpool source the legacy planners use.

    Converted to normalized SEK/kWh import/export prices. Returns None if
    Nordpool data isn't available at all (failsafe).
    """
    nordpool_entity_id = _config(hass, "nordpool_entity_id", None)
    if nordpool_entity_id is None:
        _LOGGER.warning("Smart Planner: nordpool_entity_id not configured")
        return None
    nordpool_state = hass.states.get(nordpool_entity_id)
    if nordpool_state is None:
        _LOGGER.warning(
            "Smart Planner: Nordpool entity %s not found", nordpool_entity_id
        )
        return None

    nordpool_currency = str(nordpool_entity_id.split("_")[3]).upper()
    nordpool_area = str(nordpool_entity_id.split("_")[2]).upper()
    tomorrow_valid = nordpool_state.attributes.get("tomorrow_valid")

    try:
        yesterday, today, tomorrow = await fetch_nordpool_data(
            hass, nordpool_currency, nordpool_area, tomorrow_valid
        )
    except Exception:
        _LOGGER.exception("Smart Planner: failed to fetch Nordpool data")
        return None
    if yesterday is None or today is None:
        _LOGGER.warning("Smart Planner: Nordpool data not available")
        return None

    zone_name = tzs.get(nordpool_area)
    if zone_name is None:
        _LOGGER.warning("Smart Planner: unknown Nordpool area %s", nordpool_area)
        return None
    zone = await dt_utils.async_get_time_zone(zone_name)

    now = dt_utils.now()
    horizon_end = now + dt.timedelta(hours=HORIZON_HOURS)
    raw_values = [*today]
    if tomorrow is not None:
        raw_values.extend(tomorrow)

    price_config = _price_config_from_ha(hass)
    price_points: list[PricePoint] = []
    resolutions_seen: set[float] = set()
    for entry in sorted(raw_values, key=lambda x: x["start"]):
        start = parse_datetime(entry["start"], zone)
        end = parse_datetime(entry["end"], zone)
        if end <= now or start >= horizon_end:
            continue
        resolutions_seen.add((end - start).total_seconds() / 60.0)
        import_price, export_price = compute_import_export_prices(
            entry["value"], price_config
        )
        price_points.append(PricePoint(start, end, import_price, export_price))

    if resolutions_seen and len(resolutions_seen) > 1:
        _LOGGER.info(
            "Smart Planner: Nordpool price periods are NOT a single fixed "
            "resolution this horizon (minutes seen: %s) -- this is fine, "
            "the optimizer reads each period's own start/end.",
            sorted(resolutions_seen),
        )
    elif resolutions_seen:
        _LOGGER.info(
            "Smart Planner: Nordpool price resolution this run: %s minutes",
            sorted(resolutions_seen)[0],
        )

    if not price_points:
        return None
    return price_points


def _current_soc_kwh(
    hass: HomeAssistant, battery_config: BatteryConfig
) -> float | None:
    state = hass.states.get(BATTERY_SOC_ENTITY)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        soc_percent = float(state.state)
    except ValueError:
        return None
    return battery_config.capacity_kwh * soc_percent / 100.0


async def _statistics_to_energy_samples(
    hass: HomeAssistant,
    entity_id: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[HistoricalSample]:
    """Turn a power sensor's hourly mean statistics into energy buckets.

    mean_power_kw * 1h = kWh for that hour. Uses HA's long-term statistics
    (recorder.statistics), NOT raw state
    history -- statistics are pre-aggregated per hour and far cheaper to
    query over multi-week lookback windows. Returns [] (not an exception)
    on any failure, consistent with the "fail safe, don't guess" rule --
    callers treat an empty list exactly like "no history available yet".
    """
    try:
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {entity_id},
            "hour",
            None,
            {"mean"},
        )
    except Exception:
        _LOGGER.exception("Smart Planner: failed to read statistics for %s", entity_id)
        return []

    rows = stats.get(entity_id, [])
    samples = []
    for row in rows:
        mean_power_w = row.get("mean")
        if mean_power_w is None:
            continue
        bucket_start = dt_utils.utc_from_timestamp(row["start"])
        bucket_end = bucket_start + dt.timedelta(hours=1)
        energy_kwh = (mean_power_w / 1000.0) * 1.0
        samples.append(HistoricalSample(bucket_start, bucket_end, energy_kwh))
    return samples


async def _statistics_to_pv_samples(
    hass: HomeAssistant,
    entity_id: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[HistoricalPvSample]:
    """Turn a cumulative PV-energy sensor's statistics into hourly production.

    Unlike `_statistics_to_energy_samples` (for *power* sensors, where
    energy = mean power x time), the confirmed PV production entities
    (`sensor.solis_s6_solis_pv_energy_1..4`) are cumulative *energy*
    meters (kWh), so this reads the "sum" statistic instead of "mean" and
    takes the hour-over-hour delta -- HA's own reset-compensated running
    total, which cross-checked correctly against these specific entities'
    raw state history (verified against an actual year of exported data;
    see docs/smart-planner.md for the one entity where this reset
    handling was found to be unreliable and should not be trusted as-is).
    A negative hour-over-hour delta is treated as a data anomaly and
    skipped rather than guessed at.
    """
    try:
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {entity_id},
            "hour",
            None,
            {"sum"},
        )
    except Exception:
        _LOGGER.exception("Smart Planner: failed to read statistics for %s", entity_id)
        return []

    rows = sorted(stats.get(entity_id, []), key=lambda r: r["start"])
    samples = []
    prev_sum = None
    for row in rows:
        current_sum = row.get("sum")
        if current_sum is None:
            continue
        bucket_start = dt_utils.utc_from_timestamp(row["start"])
        bucket_end = bucket_start + dt.timedelta(hours=1)
        if prev_sum is not None:
            delta = current_sum - prev_sum
            if delta >= 0:
                samples.append(HistoricalPvSample(bucket_start, bucket_end, delta))
            # else: negative delta -- an anomaly for these entities (none
            # observed in the verified year of data); skip rather than
            # invent a value.
        prev_sum = current_sum
    return samples


async def _build_load_forecast(
    hass: HomeAssistant, slots: list[tuple[dt.datetime, dt.datetime]]
):
    now = dt_utils.now()
    history_start = now - dt.timedelta(days=LOOKBACK_DAYS_LOAD + 1)
    samples = await _statistics_to_energy_samples(
        hass, HOUSE_LOAD_POWER_ENTITY, history_start, now
    )
    return forecast_load(
        samples,
        slots,
        lookback_days=LOOKBACK_DAYS_LOAD,
        split_weekday_weekend=True,
        min_samples=3,
        fallback_kwh_per_hour=None,
    )


async def _build_pv_forecast(
    hass: HomeAssistant, slots: list[tuple[dt.datetime, dt.datetime]]
):
    now = dt_utils.now()
    tomorrow = (now + dt.timedelta(days=1)).date()
    today = now.date()

    sources: list[PvSourceForecast] = []
    profiles: dict[str, dict] = {}

    profile_history_start = now - dt.timedelta(days=LOOKBACK_DAYS_PV_PROFILE)
    actual_entities = DEFAULT_PV_ACTUAL_ENTITIES

    for i, forecast_entity in enumerate(DEFAULT_PV_FORECAST_ENTITIES):
        state = hass.states.get(forecast_entity)
        daily_total: dict = {}
        if state is not None and state.state not in ("unknown", "unavailable"):
            with contextlib.suppress(ValueError):
                daily_total[tomorrow] = float(state.state)
        sources.append(
            PvSourceForecast(name=forecast_entity, daily_total_kwh=daily_total)
        )

        if i < len(actual_entities):
            actual_samples = await _statistics_to_pv_samples(
                hass, actual_entities[i], profile_history_start, now
            )
            profile = build_daily_shape_profile(
                actual_samples, bucket_minutes=15, min_days=5
            )
            if profile is not None:
                profiles[forecast_entity] = profile

    if not profiles:
        _LOGGER.info(
            "Smart Planner: no PV production history profile available yet "
            "(DEFAULT_PV_ACTUAL_ENTITIES is empty or has too little history) "
            "-- falling back to an even daylight spread for today/tomorrow's "
            "PV forecast. This is a known Fas-1 gap, see docs/smart-planner.md."
        )

    del today  # not currently used for a "today" forecast source -- see gap note above
    return forecast_pv(sources, slots, profiles=profiles, bucket_minutes=15)


def _publish_unavailable(hass: HomeAssistant, reason_code: str, message: str) -> None:
    hass.data[DOMAIN]["smart_shadow_last_outcome"] = {
        "available": False,
        "error_code": reason_code,
        "error_message": message,
        "generated_at": dt_utils.now().isoformat(),
    }
    _LOGGER.warning("Smart Planner shadow: unavailable (%s): %s", reason_code, message)


def _publish_plan(hass: HomeAssistant, outcome: PlanOutcome) -> None:
    plan = outcome.result
    slots_payload = [
        {
            "start": s.start.isoformat(),
            "end": s.end.isoformat(),
            "state": s.state.value,
            "target_soc_kwh": round(s.target_soc_kwh, 3),
            "battery_charge_kwh": round(s.battery_charge_kwh, 3),
            "battery_discharge_kwh": round(s.battery_discharge_kwh, 3),
            "pv_forecast_kwh": round(s.pv_forecast_kwh, 3),
            "load_forecast_kwh": round(s.load_forecast_kwh, 3),
            "grid_import_kwh": round(s.grid_import_kwh, 3),
            "grid_export_kwh": round(s.grid_export_kwh, 3),
            "cost_sek": round(s.cost_sek, 3),
            "reason": s.reason,
            "is_degraded": s.is_degraded,
        }
        for s in plan.slots
    ]
    hass.data[DOMAIN]["smart_shadow_last_outcome"] = {
        "available": True,
        "generated_at": plan.generated_at.isoformat(),
        "soc_start_kwh": round(plan.soc_start_kwh, 3),
        "total_cost_sek": round(plan.total_cost_sek, 3),
        "total_grid_import_kwh": round(plan.total_grid_import_kwh, 3),
        "total_grid_export_kwh": round(plan.total_grid_export_kwh, 3),
        "total_charge_kwh": round(plan.total_charge_kwh, 3),
        "total_discharge_kwh": round(plan.total_discharge_kwh, 3),
        "total_pv_forecast_kwh": round(plan.total_pv_forecast_kwh, 3),
        "total_load_forecast_kwh": round(plan.total_load_forecast_kwh, 3),
        "any_degraded": any(s.is_degraded for s in plan.slots),
        "slots": slots_payload,
    }


async def async_run_shadow_planner(hass: HomeAssistant) -> None:
    """Compute the Smart Planner shadow plan and store it for the sensors.

    Never touches slot_N_* or Solis. On any missing/invalid input, records
    an "unavailable" outcome (with a reason) instead of guessing.
    """
    if not hass.data[DOMAIN]["values"].get(SHADOW_SWITCH_ID, False):
        return

    battery_config = _battery_config_from_ha(hass)
    battery_error = battery_config.validate()
    if battery_error is not None:
        _publish_unavailable(hass, "invalid_battery_config", battery_error)
        return

    current_soc_kwh = _current_soc_kwh(hass, battery_config)
    if current_soc_kwh is None:
        _publish_unavailable(
            hass, "missing_soc", f"{BATTERY_SOC_ENTITY} unavailable or not numeric"
        )
        return

    price_points = await _fetch_price_points(hass)
    if not price_points:
        _publish_unavailable(hass, "missing_prices", "Nordpool price data unavailable")
        return

    slots = [(p.start, p.end) for p in price_points]
    load_forecast = await _build_load_forecast(hass, slots)
    pv_forecast = await _build_pv_forecast(hass, slots)

    outcome = optimizer.plan(
        prices=price_points,
        pv_forecast=pv_forecast,
        load_forecast=load_forecast,
        battery_config=battery_config,
        current_soc_kwh=current_soc_kwh,
        now=dt_utils.now(),
    )

    if not outcome.ok:
        _publish_unavailable(hass, outcome.error.code, outcome.error.message)
        return

    _publish_plan(hass, outcome)


async def async_setup_smart_shadow(hass: HomeAssistant) -> None:
    """Initialize the smart-shadow result slot in hass.data. Idempotent."""
    hass.data[DOMAIN].setdefault(
        "smart_shadow_last_outcome",
        {"available": False, "error_code": "not_run_yet", "error_message": ""},
    )
