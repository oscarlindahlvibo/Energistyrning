"""Price normalization for Smart Planner.

Turns a raw spot price into the two numbers the optimizer core is allowed
to know about -- grid_import_price and grid_export_revenue, both in
SEK/kWh. This is the ONLY place where network cost/compensation and VAT
get applied. The optimizer itself (optimizer.py) must never re-derive
these from spot price, and must never contain magic constants like
`* 1.25` or `* 10` -- that was the bug class found in the legacy
price_peak_planner.py.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class PriceConfig:
    """Configured cost/compensation added on top of the spot price.

    network_cost_sek_per_kwh: grid operator's transfer fee etc, added when
        importing (buying) from the grid.
    network_compensation_sek_per_kwh: grid operator's compensation, added
        when exporting (selling) to the grid.
    vat_multiplier: applied once to the spot price before adding network
        cost, e.g. 1.25 for 25% Swedish VAT on purchased electricity. Set to
        1.0 if the spot price handed in is already VAT-inclusive.
    export_vat_multiplier: applied to the spot price on the export side.
        Private micro-producers in Sweden typically do not charge VAT on
        surplus sold to the grid, so this defaults to 1.0 -- verify against
        your own agreement before changing it.
    """

    network_cost_sek_per_kwh: float = 0.0
    network_compensation_sek_per_kwh: float = 0.0
    vat_multiplier: float = 1.0
    export_vat_multiplier: float = 1.0


def compute_import_export_prices(
    spot_price_sek_per_kwh: float, config: PriceConfig
) -> tuple[float, float]:
    """Return (grid_import_price, grid_export_revenue) in SEK/kWh.

    grid_import_price = spot * vat_multiplier + network_cost
    grid_export_revenue = spot * export_vat_multiplier + network_compensation
    """
    import_price = (
        spot_price_sek_per_kwh * config.vat_multiplier + config.network_cost_sek_per_kwh
    )
    export_price = (
        spot_price_sek_per_kwh * config.export_vat_multiplier
        + config.network_compensation_sek_per_kwh
    )
    return import_price, export_price
