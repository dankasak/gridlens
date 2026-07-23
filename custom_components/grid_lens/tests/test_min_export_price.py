#!/usr/bin/env python3
"""Tests for the minimum export price floor (Feature 1 of
DEFERRABLE_EXPORT_CONTROL_PLAN.md).

battery_optimizer.py imports numpy/scipy/highspy only INSIDE the solver methods
(_lp_highspy, _lp_scipy), not at module level, so BatteryOptimizer itself imports
and its call-chain wiring is testable here without scipy (unavailable in this
container — see GRIDLENS_CHECKLIST.md). What's covered:

  1. optimize_hourly_schedule/._lp_optimize forward min_export_price correctly
     down the call chain.
  2. The HiGHS-bypass gate: min_export_price=0.0 (default/disabled) leaves the
     HiGHS-first attempt untouched (byte-identical to before this feature);
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
    # Force past the HiGHS attempt by making it raise ImportError, same as a real
    # "highspy not installed" environment — _lp_optimize should fall through to scipy.
    def fake_lp_highspy(self, *args, **kwargs):
        raise ImportError("no highspy")

    opt._lp_highspy = fake_lp_highspy.__get__(opt, BatteryOptimizer)
    opt.optimize_hourly_schedule(
        solar_profile=[1.0], load_profile=[1.0],
        import_rates=[0.30], export_rates=[0.05],
        min_export_price=0.02,
    )
    check("min_export_price=0.02 reaches _lp_scipy",
          seen.get("min_export_price") == 0.02, f"got {seen.get('min_export_price')!r}")


def test_highs_gate_unaffected_when_disabled():
    """min_export_price=0.0 (the default) must not change which solver path is
    attempted first — every other 'extra' at its own default leaves HiGHS as the
    first attempt, and this feature must not regress that."""
    opt = _optimizer()
    calls = []

    def fake_lp_highspy(self, *args, **kwargs):
        calls.append("highspy")
        return {"schedule": []}

    opt._lp_highspy = fake_lp_highspy.__get__(opt, BatteryOptimizer)
    opt.optimize_hourly_schedule(
        solar_profile=[1.0], load_profile=[1.0],
        import_rates=[0.30], export_rates=[0.05],
    )
    check("HiGHS attempted first when min_export_price is disabled (0.0)",
          calls == ["highspy"], f"calls={calls}")


def test_nonzero_value_bypasses_highs_gate():
    """Setting a floor forces the scipy path, same pattern as demand_rate/caps/
    credits/soc_reward/export_penalty/no_grid_charge/terminal_soc_value — none of
    those extras are modelled by the HiGHS/PuLP paths, only scipy."""
    opt = _optimizer()
    calls = []

    def fake_lp_highspy(self, *args, **kwargs):
        calls.append("highspy")
        return {"schedule": []}

    def fake_lp_scipy(self, *args, **kwargs):
        calls.append("scipy")
        return {"schedule": []}

    opt._lp_highspy = fake_lp_highspy.__get__(opt, BatteryOptimizer)
    opt._lp_scipy = fake_lp_scipy.__get__(opt, BatteryOptimizer)
    opt.optimize_hourly_schedule(
        solar_profile=[1.0], load_profile=[1.0],
        import_rates=[0.30], export_rates=[0.05],
        min_export_price=0.02,
    )
    check("HiGHS is skipped and scipy is called directly when a floor is set",
          calls == ["scipy"], f"calls={calls}")


if __name__ == "__main__":
    test_default_is_disabled_and_forwarded_as_zero()
    test_nonzero_value_forwarded_through_lp_optimize_to_lp_scipy()
    test_highs_gate_unaffected_when_disabled()
    test_nonzero_value_bypasses_highs_gate()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all min-export-price wiring tests passed.")
