"""Offline tests for dispatch_realism.realize_schedule.

Verifies the plan-comparison LP's schedule gets the same materiality filter
control/executor.py applies live, so a battery-dispatch plan's projected import/
export can't include grid-charge/discharge slivers Grid Lens would never actually
command. See dispatch_realism.py's docstring and GRIDLENS_CHECKLIST.md, 2026-09-18.

Run: python3 tests/test_dispatch_realism.py
"""
from __future__ import annotations

import importlib.util
import os
import sys

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "dispatch_realism", os.path.join(_COMPONENT, "dispatch_realism.py")
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules["dispatch_realism"] = m
    spec.loader.exec_module(m)
    return m


dr = _load()


def _base_step(**overrides):
    step = {
        "hour": 0, "solar_kwh": 0.0, "load_kwh": 0.4, "deferrable_kwh": 0.0,
        "charge_kwh": 0.0, "discharge_kwh": 0.0,
        "import_kwh": 0.0, "export_kwh": 0.0,
        "import_rate": 0.2, "export_rate": 0.05,
        "import_cost": 0.0, "export_credit": 0.0,
    }
    step.update(overrides)
    return step


def test_marginal_grid_charge_top_up_is_dropped():
    """A charge slot where grid supplies a minority (<50%) share of the charge is
    exactly the "top the battery up before the export window" sliver found in the
    real AGL vs Origin comparison — the live executor would run this as pure
    self-consumption, so it should never reach the bill."""
    step = _base_step(
        solar_kwh=1.8787, load_kwh=0.4, charge_kwh=2.2608,
        import_kwh=1.1766,  # load(0.4) + grid-to-battery(0.7766) — 34% of the charge
    )
    out = dr.realize_schedule([step], dt_h=1.0)[0]
    assert out["charge_kwh"] < step["charge_kwh"], out
    assert abs(out["charge_kwh"] - 1.4842) < 1e-6, out["charge_kwh"]
    assert abs(out["import_kwh"] - 0.4) < 1e-6, out["import_kwh"]
    print("  ✓ sub-50%-share grid top-up dropped, only load import remains")


def test_material_grid_charge_is_kept():
    """A charge slot fully sourced from grid (no solar) is a real grid force-charge
    the executor would actually command — must pass through unchanged."""
    step = _base_step(
        solar_kwh=0.0, load_kwh=0.44, charge_kwh=1.2312,
        import_kwh=1.6745,
    )
    out = dr.realize_schedule([step], dt_h=1.0)[0]
    assert out["charge_kwh"] == step["charge_kwh"]
    assert out["import_kwh"] == step["import_kwh"]
    print("  ✓ fully grid-sourced (material) charge slot left untouched")


def test_below_absolute_floor_grid_charge_is_dropped():
    """Grid share is 100% of a tiny charge slot, but below the 250 W absolute
    floor — the classic 'LP rounding' nibble the executor guards against."""
    step = _base_step(solar_kwh=0.0, load_kwh=0.1, charge_kwh=0.1, import_kwh=0.2)
    out = dr.realize_schedule([step], dt_h=1.0)[0]
    assert out["charge_kwh"] == 0.0, out
    assert abs(out["import_kwh"] - 0.1) < 1e-6, out["import_kwh"]
    print("  ✓ sub-floor grid nibble dropped even at 100% share")


def test_material_export_is_kept():
    """A discharge slot that's mostly a paid export (the evening peak sale) is a
    real forced 'battery first' discharge — must pass through unchanged."""
    step = _base_step(
        solar_kwh=0.145, load_kwh=0.3, discharge_kwh=10.0, export_kwh=9.596,
        import_rate=0.539, export_rate=0.22,
    )
    out = dr.realize_schedule([step], dt_h=1.0)[0]
    assert out["discharge_kwh"] == step["discharge_kwh"]
    assert out["export_kwh"] == step["export_kwh"]
    print("  ✓ material export discharge left untouched")


def test_marginal_export_collapses_to_load_covering_only():
    """A discharge slot whose export share is small collapses to self-consumption:
    the battery still covers the load gap, but the forced sale is dropped."""
    step = _base_step(
        solar_kwh=0.0, load_kwh=1.0, discharge_kwh=1.0, export_kwh=0.2,
        import_rate=0.2, export_rate=0.05,
    )
    out = dr.realize_schedule([step], dt_h=1.0)[0]
    assert abs(out["discharge_kwh"] - 0.8) < 1e-6, out["discharge_kwh"]
    assert out["export_kwh"] == 0.0, out["export_kwh"]
    print("  ✓ marginal export dropped, load-covering discharge kept")


def test_free_rate_marginal_discharge_goes_idle():
    """Under a genuinely free import rate, a non-material discharge goes IDLE
    (not self-consumption): the load it covered falls through to free import."""
    step = _base_step(
        solar_kwh=0.0, load_kwh=1.0, discharge_kwh=1.0, export_kwh=0.2,
        import_kwh=0.0, import_rate=0.0, export_rate=0.05,
    )
    out = dr.realize_schedule([step], dt_h=1.0)[0]
    assert out["discharge_kwh"] == 0.0, out["discharge_kwh"]
    assert abs(out["import_kwh"] - 0.8) < 1e-6, out["import_kwh"]
    print("  ✓ free-rate marginal discharge goes idle, load shifts to import")


def test_capped_rate_step_passed_through_untouched():
    """A step carrying a capped-rate tranche is out of scope — must not be
    reinterpreted by this filter (see module docstring)."""
    step = _base_step(
        solar_kwh=0.0, load_kwh=0.4, charge_kwh=2.0, import_kwh=1.0,
        import_cap_free_kwh=0.6, import_cap_over_kwh=0.0,
    )
    out = dr.realize_schedule([step], dt_h=1.0)[0]
    assert out == step
    print("  ✓ capped-rate step passed through unchanged")


def test_amounts_stay_internally_consistent():
    """Whenever energy changes, cost/credit must still equal kwh x rate — no
    reconciliation plug (see plan_calculator.py's _compute_bill_items note)."""
    step = _base_step(
        solar_kwh=1.8787, load_kwh=0.4, charge_kwh=2.2608,
        import_kwh=1.1766, import_rate=0.187,
    )
    out = dr.realize_schedule([step], dt_h=1.0)[0]
    assert abs(out["import_cost"] - out["import_kwh"] * 0.187) < 1e-9, out
    print("  ✓ corrected import_cost still equals import_kwh x import_rate")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"Running {len(tests)} dispatch-realism tests\n")
    for t in tests:
        t()
    print(f"\n✅ all {len(tests)} passed")
