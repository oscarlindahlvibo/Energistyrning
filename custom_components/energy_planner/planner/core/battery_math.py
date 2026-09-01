"""Battery power/energy math shared by the optimizer and its tests.

All functions here are pure and side-effect free.
"""

from __future__ import annotations

from .models import BatteryConfig


def soc_kwh_to_ticks(soc_kwh: float, resolution_kwh: float) -> int:
    """Snap a SOC value (kWh) to the nearest discretization tick."""
    return round(soc_kwh / resolution_kwh)


def ticks_to_soc_kwh(ticks: int, resolution_kwh: float) -> float:
    """Convert a discretization tick count back to a SOC value in kWh."""
    return ticks * resolution_kwh


def max_charge_in_kwh(
    soc_kwh: float, duration_hours: float, config: BatteryConfig
) -> float:
    """Max energy that can be stored into the battery this slot.

    Post charge-efficiency, bounded by remaining headroom to max SOC and by
    the AC-side charge power limit.
    """
    headroom_kwh = max(0.0, config.max_soc_kwh - soc_kwh)
    power_limit_kwh = (
        config.max_charge_power_kw * duration_hours * config.charge_efficiency
    )
    return max(0.0, min(headroom_kwh, power_limit_kwh))


def max_discharge_out_kwh(
    soc_kwh: float, duration_hours: float, config: BatteryConfig
) -> float:
    """Max energy that can be drawn out of the battery's storage this slot.

    Pre discharge-efficiency, bounded by available energy above min SOC and
    by the AC-side discharge power limit.
    """
    available_kwh = max(0.0, soc_kwh - config.min_soc_kwh)
    if config.discharge_efficiency <= 0:
        return 0.0
    power_limit_kwh = (
        config.max_discharge_power_kw * duration_hours / config.discharge_efficiency
    )
    return max(0.0, min(available_kwh, power_limit_kwh))


def usable_discharge_kwh(discharge_out_kwh: float, config: BatteryConfig) -> float:
    """Energy available at the inverter's AC output for energy drawn out of storage."""
    return discharge_out_kwh * config.discharge_efficiency


def source_energy_for_charge(charge_in_kwh: float, config: BatteryConfig) -> float:
    """Energy that must be drawn from PV/grid to store `charge_in_kwh`."""
    if config.charge_efficiency <= 0:
        return float("inf")
    return charge_in_kwh / config.charge_efficiency
