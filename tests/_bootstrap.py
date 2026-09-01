"""Test bootstrap for the Smart Planner core.

Makes `custom_components/energy_planner/planner/core` importable as a
plain top-level `core` package, WITHOUT importing
`custom_components.energy_planner` (which requires the `homeassistant`
package to be installed).

This is what makes the optimizer core testable with nothing but the Python
standard library -- no `homeassistant` install, no running HA instance.

Every test module should do:

    from tests._bootstrap import core  # noqa: F401 (side effect: sys.path)
    from core import models, battery_math, optimizer, ...
"""

from __future__ import annotations

import os
import sys

_PLANNER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "energy_planner", "planner"
)
_PLANNER_DIR = os.path.normpath(_PLANNER_DIR)

if _PLANNER_DIR not in sys.path:
    sys.path.insert(0, _PLANNER_DIR)

import core  # noqa: E402  (import after sys.path manipulation, by design)

__all__ = ["core"]
