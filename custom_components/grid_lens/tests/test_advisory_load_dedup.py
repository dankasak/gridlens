#!/usr/bin/env python3
"""Tests for the advisory base-load de-duplication (Defect 1, the phantom-demand bug).

The advisory feeds the whole-home consumption sensor as base load AND re-adds the EV +
Sigen smart-load as deferrable LP variables. Those devices are already metered inside the
whole-home sensor, so their energy is counted twice (~15 kWh/day of phantom demand →
phantom grid-charging). The fix subtracts each deferrable device's hour-of-day energy from
the base-load vector before it becomes the forecaster — mirroring the main engine's
plan_calculator._subtract_ev_from_load — so deferrable demand is represented exactly once.

These tests exercise the REAL AdvisoryCoordinator._subtract_deferrable_from_load logic.
Home Assistant / scipy are not importable here, so their modules (and the sibling advisory
modules coordinator.py imports) are stubbed and coordinator.py is loaded directly by path.

Run:  python3 test_advisory_load_dedup.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)  # .../custom_components/grid_lens


def _mod(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


def _install_stubs() -> None:
    """Minimal stand-ins so coordinator.py imports without HA / scipy / siblings."""
    ha = _mod("homeassistant")
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = type("ConfigEntry", (), {})
    core = _mod("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    ha.config_entries = ce
    ha.core = core

    helpers = _mod("homeassistant.helpers")
    event = _mod("homeassistant.helpers.event")
    event.async_call_later = lambda *a, **k: (lambda: None)
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = type("DataUpdateCoordinator", (), {})
    helpers.event = event
    helpers.update_coordinator = uc
    ha.helpers = helpers

    util = _mod("homeassistant.util")
    dt = _mod("homeassistant.util.dt")
    dt.utcnow = lambda: None
    dt.as_local = lambda x: x
    util.dt = dt
    ha.util = util

    # Synthetic package tree for the relative imports in coordinator.py.
    for pkg in ("gl", "gl.advisory"):
        m = types.ModuleType(pkg)
        m.__path__ = []
        sys.modules[pkg] = m

    opt = _mod("gl.battery_optimizer")
    opt.BatteryOptimizer = type("BatteryOptimizer", (), {})
    const = _mod("gl.const")
    const.DOMAIN = "grid_lens"
    const.parse_hours_spec = lambda spec: None
    forecast = _mod("gl.advisory.forecast")
    forecast.FlatLoadForecaster = type("FlatLoadForecaster", (), {})
    forecast.ForecastProvider = type("ForecastProvider", (), {})
    forecast.HourOfDayLoadForecaster = type("HourOfDayLoadForecaster", (), {})
    lh = _mod("gl.advisory.load_history")
    lh.build_hour_of_day_load = lambda *a, **k: None
    planner = _mod("gl.advisory.planner")
    planner.AdvisoryPlanner = type("AdvisoryPlanner", (), {})
    rates = _mod("gl.advisory.rates")
    rates.PlanRateForecaster = type("PlanRateForecaster", (), {})


def _load_coordinator():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        "gl.advisory.coordinator",
        os.path.join(_COMPONENT, "advisory", "coordinator.py"),
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "gl.advisory"
    sys.modules["gl.advisory.coordinator"] = module
    spec.loader.exec_module(module)
    return module.AdvisoryCoordinator


AdvisoryCoordinator = _load_coordinator()
# The dedup method is pure (only reads self._deferrable_load_hod), so bind it to a stub.
_subtract = AdvisoryCoordinator.__dict__["_subtract_deferrable_from_load"]


class _Stub:
    def __init__(self, defer_hod):
        self._deferrable_load_hod = defer_hod


# --------------------------------------------------------------------------- tests
def test_dedup_equals_load_minus_deferrable():
    """Result must equal element-wise (load - deferrable) for a normal case."""
    load = [1.0 + 0.1 * h for h in range(24)]           # 1.0 .. 3.3 kWh/hour
    defer = [0.5 if 0 <= h < 6 else 0.2 for h in range(24)]  # EV overnight + steady base
    out = _subtract(_Stub(defer), load)
    expected = [max(0.0, load[h] - defer[h]) for h in range(24)]
    assert out == expected, (out, expected)
    # Total removed energy equals the deferrable total (nothing clamped in this case).
    assert round(sum(load) - sum(out), 6) == round(sum(defer), 6)


def test_dedup_floors_at_zero():
    """When a deferrable device exceeds base load in an hour, the result is floored at 0
    (never negative), matching _subtract_ev_from_load's max(0, ...)."""
    load = [0.3, 0.3, 5.0]
    defer = [1.0, 0.1, 2.0]   # hour 0: 1.0 > 0.3 -> floored to 0
    out = _subtract(_Stub(defer + [0.0] * 21), load)
    assert out[0] == 0.0, out
    assert abs(out[1] - 0.2) < 1e-9, out
    assert abs(out[2] - 3.0) < 1e-9, out
    assert all(v >= 0.0 for v in out)


def test_dedup_no_deferrable_returns_load_unchanged():
    """Empty / all-zero deferrable vector leaves the base load untouched."""
    load = [1.0, 2.0, 3.0]
    assert _subtract(_Stub([]), load) == load
    assert _subtract(_Stub(None), load) == load


def test_dedup_shorter_deferrable_vector_treated_as_zero():
    """A deferrable vector shorter than the load is padded with zeros (no IndexError)."""
    load = [1.0, 1.0, 1.0, 1.0]
    defer = [0.4, 0.4]  # only two hours provided
    out = _subtract(_Stub(defer), load)
    assert out == [0.6, 0.6, 1.0, 1.0], out


# --------------------------------------------------------------------------- runner
def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {err}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
