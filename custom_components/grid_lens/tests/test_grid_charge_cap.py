#!/usr/bin/env python3
"""Tests for grid_charge_cap_kwh — the P10/P90-sized pre-dawn grid-charge hedge cap.

See GRIDLENS_CHECKLIST.md 2026-10-15 ("pre-dawn grid-charge on a big-solar day") for
the incident this fixes: a plain P50 point forecast made the LP's grid-charge hedge
decision a cliff (0 kWh at 100% forecast accuracy, ~9-18kWh at 50-70%), with no
proportionality to how uncertain the forecast actually was. ``grid_charge_cap_kwh``
restricts how much grid import may fund battery charge (load/deferrables untouched),
sized by AdvisoryPlanner._grid_charge_hedge_cap from Solcast's P10/P90 bands.

battery_optimizer.py imports numpy/scipy only INSIDE the solver methods (_lp_scipy,
_lp_pulp), not at module level, so BatteryOptimizer itself imports and its call-chain
wiring is testable here without scipy (unavailable in this container). What's covered:

  1. optimize_hourly_schedule/._lp_optimize forward grid_charge_cap_kwh correctly down
     the call chain.
  2. Solver routing: an active (not all-inf) grid_charge_cap_kwh reaches _lp_scipy and
     is refused by the PuLP fallback, matching every other scipy-only feature
     (deferrables, export_floor_kwh, min_export_price, ...).
  3. An all-inf (or absent) grid_charge_cap_kwh is a no-op: same routing as not
     passing it at all.

What this does NOT cover (needs a real scipy solve — run on the LXC, see
GRIDLENS_CHECKLIST.md 2026-10-15): that the cap actually suppresses the unwanted
pre-dawn hedge while leaving a genuinely-needed one (low P90) untouched. That
end-to-end behaviour was verified against the real solver with today's actual
rates/deferrables via docker on the LXC, not offline-testable here — same
limitation test_min_export_price.py documents for its own floor.

Run:  python3 test_grid_charge_cap.py
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
        capacity_kwh=24.0, max_charge_rate_kw=10.0, max_discharge_rate_kw=10.0,
        efficiency_percent=95.0, min_soc_percent=10.0, max_soc_percent=100.0,
    )


def test_default_is_none_and_forwarded():
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
    check("grid_charge_cap_kwh defaults to None and is forwarded",
          seen.get("grid_charge_cap_kwh") is None, f"got {seen.get('grid_charge_cap_kwh')!r}")


def test_nonzero_value_forwarded_through_lp_optimize_to_lp_scipy():
    opt = _optimizer()
    seen = {}

    def fake_lp_scipy(self, *args, **kwargs):
        seen.update(kwargs)
        return {"schedule": []}

    opt._lp_scipy = fake_lp_scipy.__get__(opt, BatteryOptimizer)
    cap = [0.0, float("inf"), 2.5]
    opt.optimize_hourly_schedule(
        solar_profile=[1.0, 1.0, 1.0], load_profile=[1.0, 1.0, 1.0],
        import_rates=[0.30, 0.30, 0.30], export_rates=[0.05, 0.05, 0.05],
        grid_charge_cap_kwh=cap,
    )
    check("grid_charge_cap_kwh reaches _lp_scipy unchanged",
          seen.get("grid_charge_cap_kwh") == cap, f"got {seen.get('grid_charge_cap_kwh')!r}")


def test_scipy_is_the_first_and_only_lp_path():
    for cap in (None, [0.0, float("inf")]):
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
            solar_profile=[1.0, 1.0], load_profile=[1.0, 1.0],
            import_rates=[0.30, 0.30], export_rates=[0.05, 0.05],
            grid_charge_cap_kwh=cap,
        )
        check(f"scipy handles the solve directly (grid_charge_cap_kwh={cap!r})",
              calls == ["scipy"], f"calls={calls}")


def test_active_cap_never_silently_answered_by_a_solver_that_ignores_it():
    """If scipy fails with an ACTIVE (not all-inf) cap, the top-level call must
    never silently return a PuLP-sourced answer — PuLP models no grid-charge
    restriction and would let the solver over-hedge exactly as the cap exists
    to prevent. It falls back to greedy instead (tagged 'solver': 'greedy'),
    same contract as deferrable_loads/export_floor_kwh.

    NOTE: _diagnose_infeasible's own internal probing legitimately calls PuLP
    while relaxing features one at a time to name the infeasibility cause (see
    its docstring) — that's diagnostic-only and never reaches the caller, so
    this asserts the real contract (what optimize_hourly_schedule returns),
    not "PuLP never ran anywhere". A naive "pulp not in calls" assertion is
    already broken this way for the pre-existing deferrable+export-floor case
    in test_min_export_price.py — see GRIDLENS_CHECKLIST.md 2026-10-15.
    """
    opt = _optimizer()

    def fake_lp_scipy(self, *args, **kwargs):
        raise RuntimeError("scipy down")

    opt._lp_scipy = fake_lp_scipy.__get__(opt, BatteryOptimizer)
    result = opt.optimize_hourly_schedule(
        solar_profile=[1.0, 1.0], load_profile=[1.0, 1.0],
        import_rates=[0.30, 0.30], export_rates=[0.05, 0.05],
        grid_charge_cap_kwh=[0.0, 1.5],
    )
    check("falls back to greedy (not a silent PuLP answer) when scipy fails with an active cap",
          result.get("solver") == "greedy", f"got solver={result.get('solver')!r}")


def test_all_inf_cap_is_a_no_op_for_routing():
    """An all-inf cap (nothing actually restricted) must behave exactly like
    passing None — it's a no-op, so PuLP is fine as a fallback."""
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
        solar_profile=[1.0, 1.0], load_profile=[1.0, 1.0],
        import_rates=[0.30, 0.30], export_rates=[0.05, 0.05],
        grid_charge_cap_kwh=[float("inf"), float("inf")],
    )
    check("all-inf cap falls back to PuLP like no cap at all",
          "pulp" in calls, f"calls={calls}")


if __name__ == "__main__":
    test_default_is_none_and_forwarded()
    test_nonzero_value_forwarded_through_lp_optimize_to_lp_scipy()
    test_scipy_is_the_first_and_only_lp_path()
    test_active_cap_never_silently_answered_by_a_solver_that_ignores_it()
    test_all_inf_cap_is_a_no_op_for_routing()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all grid-charge-cap wiring tests passed.")
