#!/usr/bin/env python3
"""Tests for the minimum export price floor (Feature 1 of
DEFERRABLE_EXPORT_CONTROL_PLAN.md).

battery_optimizer.py imports numpy/scipy only INSIDE the solver methods
(_lp_scipy, _lp_pulp), not at module level, so BatteryOptimizer itself imports
and its call-chain wiring is testable here without scipy (unavailable in this
container — see GRIDLENS_CHECKLIST.md). What's covered:

  1. optimize_hourly_schedule/._lp_optimize forward min_export_price correctly
     down the call chain.
  2. Solver routing: a min_export_price horizon reaches _lp_scipy, the only path
     that models the floor, and is never handed to a solver that ignores it;
     min_export_price>0 skips straight to the scipy path, matching every other
     "extra" (demand, caps, credits, soc_reward, ...) already gated the same way.

What this does NOT cover (needs a real scipy solve — see the plan doc's
Verification section, run on the LXC): that the floor actually changes the LP's
chosen schedule (routes surplus to a deferrable load instead of exporting below
the floor). That's a live/LXC check, not offline-testable here.

Run:  python3 test_min_export_price.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_COMPONENT))
sys.path.insert(0, _COMPONENT)

from battery_optimizer import BatteryOptimizer  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def _optimizer():
    return BatteryOptimizer(
        capacity_kwh=13.5, max_charge_rate_kw=5.0, max_discharge_rate_kw=5.0,
        efficiency_percent=95.0, min_soc_percent=10.0, max_soc_percent=90.0,
    )


def test_default_is_disabled_and_forwarded_as_zero():
    opt = _optimizer()
    seen = {}

    def fake_lp_optimize(self, *args, **kwargs):
        seen.update(kwargs)
        return {"schedule": []}

    opt._lp_optimize = fake_lp_optimize.__get__(opt, BatteryOptimizer)
    opt.optimize_hourly_schedule(
        solar_profile=[1.0], load_profile=[1.0],
        import_rates=[0.30], export_rates=[0.05],
    )
    check("min_export_price defaults to 0.0 and is forwarded",
          seen.get("min_export_price") == 0.0, f"got {seen.get('min_export_price')!r}")


def test_nonzero_value_forwarded_through_lp_optimize_to_lp_scipy():
    opt = _optimizer()
    seen = {}

    def fake_lp_scipy(self, *args, **kwargs):
        seen.update(kwargs)
        return {"schedule": []}

    opt._lp_scipy = fake_lp_scipy.__get__(opt, BatteryOptimizer)
    opt.optimize_hourly_schedule(
        solar_profile=[1.0], load_profile=[1.0],
        import_rates=[0.30], export_rates=[0.05],
        min_export_price=0.02,
    )
    check("min_export_price=0.02 reaches _lp_scipy",
          seen.get("min_export_price") == 0.02, f"got {seen.get('min_export_price')!r}")


def test_scipy_is_the_first_and_only_lp_path():
    """min_export_price is modelled by _lp_scipy alone.

    This used to assert HiGHS was attempted first when the floor was disabled, and
    skipped when it was set. The parallel hand-rolled highspy model was removed on
    2026-08-22 (scipy's linprog/milp already are HiGHS), so there is no gate left to
    bypass — scipy is now reached directly whatever the floor is. The property that
    still matters is that no deferrable-blind solver gets the horizon instead.
    """
    for floor in (0.0, 0.02):
        opt = _optimizer()
        calls = []

        def fake_lp_scipy(self, *args, **kwargs):
            calls.append("scipy")
            return {"schedule": []}

        def fake_lp_pulp(self, *args, **kwargs):
            calls.append("pulp")
            return {"schedule": []}

        opt._lp_scipy = fake_lp_scipy.__get__(opt, BatteryOptimizer)
        opt._lp_pulp = fake_lp_pulp.__get__(opt, BatteryOptimizer)
        opt.optimize_hourly_schedule(
            solar_profile=[1.0], load_profile=[1.0],
            import_rates=[0.30], export_rates=[0.05],
            min_export_price=floor,
        )
        check(f"scipy handles the solve directly (min_export_price={floor})",
              calls == ["scipy"], f"calls={calls}")


def test_floor_never_reaches_a_solver_that_ignores_it():
    """If scipy fails, PuLP must not silently answer a min_export_price horizon.

    PuLP models no export floor, so a fallback that accepted this horizon would
    return a confident number for a different question.
    """
    opt = _optimizer()
    calls = []

    def fake_lp_scipy(self, *args, **kwargs):
        calls.append("scipy")
        raise RuntimeError("scipy down")

    def fake_lp_pulp(self, *args, **kwargs):
        calls.append("pulp")
        return {"schedule": []}

    opt._lp_scipy = fake_lp_scipy.__get__(opt, BatteryOptimizer)
    opt._lp_pulp = fake_lp_pulp.__get__(opt, BatteryOptimizer)
    opt.optimize_hourly_schedule(
        solar_profile=[1.0], load_profile=[1.0],
        import_rates=[0.30], export_rates=[0.05],
        min_export_price=0.02,
        deferrable_loads=[{"max_kw": 3.5, "daily_kwh": 8.0}],
    )
    check("PuLP refused a deferrable+floor horizon after scipy failed",
          "pulp" not in calls, f"calls={calls}")


if __name__ == "__main__":
    test_default_is_disabled_and_forwarded_as_zero()
    test_nonzero_value_forwarded_through_lp_optimize_to_lp_scipy()
    test_scipy_is_the_first_and_only_lp_path()
    test_floor_never_reaches_a_solver_that_ignores_it()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all min-export-price wiring tests passed.")
