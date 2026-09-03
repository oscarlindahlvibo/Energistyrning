"""Pure-Python data model for the Smart Planner core.

This module has NO Home Assistant imports. Everything here must be
constructible and testable with plain `python -m unittest`, without a
running HA instance.

Units, fixed throughout the core:
- Energy: kWh
- Power: kW
- Money: SEK
- Time: timezone-aware `datetime.datetime`
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from enum import Enum


class SlotState(str, Enum):
    """Mirrors the seven states already understood by update_battery_action.py.

    discharge = self-use (battery covers house load only, no deliberate export)
    sell = deliberate battery export beyond house load
    sell-excess = self-use + export any PV surplus not needed by house/battery
    discard-excess = self-use + curtail (waste) PV surplus instead of exporting it
    """

    CHARGE = "charge"
    DISCHARGE = "discharge"
    SELL = "sell"
    SELL_EXCESS = "sell-excess"
    DISCARD_EXCESS = "discard-excess"
    PAUSE = "pause"
    OFF = "off"


@dataclasses.dataclass(frozen=True)
class PricePoint:
    """Price for one period.

    Import/export prices are already fully normalized to SEK/kWh (spot +
    network cost/compensation + any tax), see economics.py.
    """

    start: dt.datetime
    end: dt.datetime
    import_price_sek_per_kwh: float
    export_price_sek_per_kwh: float

    def __post_init__(self) -> None:
        """Reject a period with end <= start -- never a valid price window."""
        if self.end <= self.start:
            raise ValueError(
                f"PricePoint end ({self.end}) must be after start ({self.start})"
            )

    @property
    def duration_hours(self) -> float:
        """Actual period length, derived from start/end. Never assumed to be 0.25h."""
        return (self.end - self.start).total_seconds() / 3600.0


@dataclasses.dataclass(frozen=True)
class PvForecastPoint:
    """Forecast (or actual, in backtests) PV production during [start, end)."""

    start: dt.datetime
    end: dt.datetime
    energy_kwh: float
    is_degraded: bool = False
    """True if this value is a rough fallback (e.g. evenly split daily total)
    rather than a profile-shaped estimate."""
    uncertainty_kwh: float = 0.0
    """Estimated forecast error (e.g. historical MAE for this time-of-day/
    season bucket), in kWh. 0.0 means "unknown/not estimated", not
    "perfectly certain" -- callers that need a reserve should treat an
    unestimated uncertainty conservatively rather than as zero risk."""


@dataclasses.dataclass(frozen=True)
class LoadForecastPoint:
    """Forecast (or actual, in backtests) house consumption during [start, end)."""

    start: dt.datetime
    end: dt.datetime
    energy_kwh: float
    is_degraded: bool = False
    """True if this value is a rough fallback (e.g. flat average) rather than
    a time-of-day-aware median."""
    uncertainty_kwh: float = 0.0
    """Estimated forecast error (e.g. historical MAE for this time-of-day/
    temperature/season bucket), in kWh. See PvForecastPoint.uncertainty_kwh
    for the same "0.0 means unknown, not zero risk" caveat."""


@dataclasses.dataclass(frozen=True)
class BatteryConfig:
    """Battery configuration in physically meaningful units.

    charge_efficiency: fraction of energy drawn from PV/grid that ends up
        stored in the battery (0 < x <= 1).
    discharge_efficiency: fraction of stored battery energy that is usable
        at the inverter's AC output (0 < x <= 1).
    max_charge_power_kw / max_discharge_power_kw: AC-side power limits, NOT
        derived from amps here -- ampere-to-kW needs the battery/inverter
        voltage, which this module deliberately does not assume. Callers
        supply already-known kW values (config, or read from Solis).
    max_grid_import_power_kw / max_grid_export_power_kw: optional caps on
        total grid power (house load + any battery charge/discharge
        contribution), meant to keep the plan within the property's
        service fuse (huvudsäkring) rating. Like the battery power
        limits, these are explicit kW -- not derived from amps here, so
        the amps-to-kW voltage/phase-count assumption stays the caller's
        responsibility. None means "no limit enforced" (backward
        compatible default -- most callers, including all existing
        tests, don't set this).
    reserve_cost_sek_per_kwh: SOFT penalty (SEK per kWh of shortfall,
        applied every slot) for letting SOC drop below that slot's
        dynamic reserve target (see core/reserve.py). This is a shadow
        price, not a hard floor -- the optimizer will still dip into the
        reserve if the economic upside clears this cost, which is exactly
        what "reserven ska vara ekonomiskt optimerad, inte ett fast SOC-
        golv" means. 0.0 (default) disables the reserve mechanism
        entirely, matching every pre-existing test's expectations.
    """

    capacity_kwh: float
    min_soc_fraction: float
    max_soc_fraction: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    charge_efficiency: float
    discharge_efficiency: float
    cycle_cost_sek_per_kwh: float
    soc_resolution_kwh: float = 0.25
    max_grid_import_power_kw: float | None = None
    max_grid_export_power_kw: float | None = None
    reserve_cost_sek_per_kwh: float = 0.0

    @property
    def min_soc_kwh(self) -> float:
        """Minimum allowed SOC, in kWh."""
        return self.capacity_kwh * self.min_soc_fraction

    @property
    def max_soc_kwh(self) -> float:
        """Maximum allowed SOC, in kWh."""
        return self.capacity_kwh * self.max_soc_fraction

    def validate(self) -> str | None:
        """Return a human-readable error message, or None if valid."""
        if self.capacity_kwh <= 0:
            return f"battery capacity must be > 0, got {self.capacity_kwh}"
        if not (0 <= self.min_soc_fraction <= 1):
            return f"min_soc_fraction must be in [0,1], got {self.min_soc_fraction}"
        if not (0 <= self.max_soc_fraction <= 1):
            return f"max_soc_fraction must be in [0,1], got {self.max_soc_fraction}"
        if self.min_soc_fraction > self.max_soc_fraction:
            return (
                f"min_soc_fraction ({self.min_soc_fraction}) must be <= "
                f"max_soc_fraction ({self.max_soc_fraction})"
            )
        if self.max_charge_power_kw <= 0:
            return f"max_charge_power_kw must be > 0, got {self.max_charge_power_kw}"
        if self.max_discharge_power_kw <= 0:
            return (
                f"max_discharge_power_kw must be > 0, got {self.max_discharge_power_kw}"
            )
        if not (0 < self.charge_efficiency <= 1):
            return f"charge_efficiency must be in (0,1], got {self.charge_efficiency}"
        if not (0 < self.discharge_efficiency <= 1):
            return (
                f"discharge_efficiency must be in (0,1], "
                f"got {self.discharge_efficiency}"
            )
        if self.cycle_cost_sek_per_kwh < 0:
            return (
                f"cycle_cost_sek_per_kwh must be >= 0, "
                f"got {self.cycle_cost_sek_per_kwh}"
            )
        if self.soc_resolution_kwh <= 0:
            return f"soc_resolution_kwh must be > 0, got {self.soc_resolution_kwh}"
        if (
            self.max_grid_import_power_kw is not None
            and self.max_grid_import_power_kw <= 0
        ):
            return (
                f"max_grid_import_power_kw must be > 0 (or None), "
                f"got {self.max_grid_import_power_kw}"
            )
        if (
            self.max_grid_export_power_kw is not None
            and self.max_grid_export_power_kw <= 0
        ):
            return (
                f"max_grid_export_power_kw must be > 0 (or None), "
                f"got {self.max_grid_export_power_kw}"
            )
        if self.reserve_cost_sek_per_kwh < 0:
            return (
                f"reserve_cost_sek_per_kwh must be >= 0, "
                f"got {self.reserve_cost_sek_per_kwh}"
            )
        return None


@dataclasses.dataclass(frozen=True)
class PlanSlot:
    """One planned period in the resulting Smart Plan."""

    start: dt.datetime
    end: dt.datetime
    state: SlotState
    target_soc_kwh: float
    battery_charge_kwh: float
    """Energy stored into the battery this slot (post charge-efficiency), >= 0."""
    battery_discharge_kwh: float
    """Energy drawn out of the battery this slot (pre discharge-efficiency), >= 0."""
    pv_forecast_kwh: float
    load_forecast_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    cost_sek: float
    """Net cost for the slot; negative means net income."""
    reason: str
    is_degraded: bool = False
    reserve_target_kwh: float = 0.0
    """This slot's dynamic reserve target above min_soc_kwh (see
    core/reserve.py). 0.0 if no reserve was configured."""
    reserve_shortfall_kwh: float = 0.0
    """How far below (min_soc_kwh + reserve_target_kwh) this slot's SOC
    ended up -- > 0 means the optimizer decided the economics were worth
    dipping into the reserve. Always 0.0 if reserve_cost_sek_per_kwh is 0."""


@dataclasses.dataclass(frozen=True)
class PlanResult:
    """Successful planning outcome."""

    slots: list[PlanSlot]
    soc_start_kwh: float
    generated_at: dt.datetime

    @property
    def total_cost_sek(self) -> float:
        """Sum of all slots' cost_sek (negative = net income)."""
        return sum(s.cost_sek for s in self.slots)

    @property
    def total_grid_import_kwh(self) -> float:
        """Total energy imported from the grid across the whole plan."""
        return sum(s.grid_import_kwh for s in self.slots)

    @property
    def total_grid_export_kwh(self) -> float:
        """Total energy exported to the grid across the whole plan."""
        return sum(s.grid_export_kwh for s in self.slots)

    @property
    def total_charge_kwh(self) -> float:
        """Total energy stored into the battery across the whole plan."""
        return sum(s.battery_charge_kwh for s in self.slots)

    @property
    def total_discharge_kwh(self) -> float:
        """Total energy drawn out of the battery across the whole plan."""
        return sum(s.battery_discharge_kwh for s in self.slots)

    @property
    def total_pv_forecast_kwh(self) -> float:
        """Total forecasted PV production across the whole plan."""
        return sum(s.pv_forecast_kwh for s in self.slots)

    @property
    def total_load_forecast_kwh(self) -> float:
        """Total forecasted house consumption across the whole plan."""
        return sum(s.load_forecast_kwh for s in self.slots)


@dataclasses.dataclass(frozen=True)
class PlanningError:
    """Returned instead of a PlanResult when the optimizer cannot plan safely.

    Per the "failsafe" requirement: Smart Planner must never guess when
    critical inputs are missing or inconsistent -- it must say so instead.
    """

    code: str
    message: str


@dataclasses.dataclass(frozen=True)
class PlanOutcome:
    """Wrapper returned by optimizer.plan(): exactly one of result/error is set."""

    result: PlanResult | None = None
    error: PlanningError | None = None

    @property
    def ok(self) -> bool:
        """True if planning succeeded and `result` is populated."""
        return self.result is not None
