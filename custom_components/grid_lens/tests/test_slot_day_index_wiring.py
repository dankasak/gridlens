#!/usr/bin/env python3
"""Source-check that slot_day_index (the calendar-day-boundary fix, 2026-09-23)
is actually threaded end to end, not just added to one signature and forgotten
elsewhere.

Bug this guards against: the LP's per-device daily-total constraint chunked
the horizon into slots_per_day-sized blocks counted from t=0 (horizon start),
not local midnight — "day 0" was a rolling 24h window, so a same-day-scoped
Daily Target (e.g. an EV charger scaled down because today is cloudy) could be
satisfied out of TOMORROW's cheaper solar instead, defeating the point. Fixed
by grouping by a real calendar-day key (retailer_plans.slot_calendar_day_index)
via the shared _day_groups helper. See GRIDLENS_CHECKLIST.md and
test_day_groups.py (the pure-function coverage of _day_groups itself).

battery_optimizer.py imports scipy inside its solver methods, which isn't
importable in this container, so the parameter-threading itself (as opposed
to _day_groups' own logic, tested directly in test_day_groups.py) is
source-checked here — same split as test_demand_charge.py/test_pooled_caps.py
use for other _lp_scipy-internal wiring.

Run:  python3 test_slot_day_index_wiring.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_COMPONENT))
sys.path.insert(0, _COMPONENT)

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def _read(path: str) -> str:
    return open(os.path.join(_COMPONENT, path)).read()


def test_optimizer_signature_chain_forwards_slot_day_index():
    src = _read("battery_optimizer.py")
    for fn in ("def optimize_hourly_schedule", "def _lp_optimize", "def _lp_scipy"):
        i = src.index(fn)
        sig = src[i:src.index(")", i) + 1] if fn != "def optimize_hourly_schedule" \
            else src[i:src.index("-> Dict", i)]
        check(f"{fn} takes slot_day_index", "slot_day_index" in sig)


def test_lp_optimize_forwards_slot_day_index_to_lp_scipy():
    src = _read("battery_optimizer.py")
    i = src.index("def _lp_optimize")
    j = src.index("def _lp_scipy", i)
    body = src[i:j]
    check("_lp_optimize's own call to _lp_scipy passes slot_day_index",
          "self._lp_scipy(" in body and "slot_day_index=slot_day_index" in body)


def test_optimize_hourly_schedule_forwards_slot_day_index_to_lp_optimize():
    src = _read("battery_optimizer.py")
    i = src.index("def optimize_hourly_schedule")
    j = src.index("def _diagnose_infeasible", i)
    body = src[i:j]
    check("the primary solve call passes slot_day_index through to _lp_optimize",
          "self._lp_optimize(" in body and "slot_day_index=slot_day_index" in body)


def test_day_chunk_computation_uses_day_groups_not_old_positional_math():
    src = _read("battery_optimizer.py")
    i = src.index("def _lp_scipy")
    body = src[i:src.index("def ", i + 10)] if "def " in src[i + 10:] else src[i:]
    check("_lp_scipy computes day_groups via the shared helper",
          "day_groups = _day_groups(T, slots_per_day, slot_day_index)" in body)
    check("day0_slots (the EV/SOC floor window — Wattpilot's own code path) "
          "derives from day_groups, not the old min(slots_per_day, T)",
          "day0_slots = len(day_groups[0][1])" in body)
    # The old literal must be gone from the deferrable per-device day loop —
    # its presence there would mean a half-finished refactor still grouping by
    # position instead of by day_groups.
    loop_start = body.index("for i, dev in enumerate(deferrable_loads):",
                             body.index("Per-device, per-day energy total constraints"))
    loop_end = body.index("SOC-tracked devices' floor", loop_start)
    loop_body = body[loop_start:loop_end]
    check("the deferrable daily-total loop iterates day_groups, not range(n_days)",
          "for day_key, slots in day_groups:" in loop_body)
    check("no leftover t0 = d * slots_per_day positional chunking in that loop",
          "t0 = d * slots_per_day" not in loop_body)


def test_consolidate_deferrable_schedule_accepts_and_forwards_slot_day_index():
    src = _read("battery_optimizer.py")
    i = src.index("def consolidate_deferrable_schedule")
    sig = src[i:src.index(") -> None:", i) + len(") -> None:")]
    check("consolidate_deferrable_schedule takes slot_day_index",
          "slot_day_index" in sig)
    call_site = src.index("consolidate_deferrable_schedule(\n", i + 10)
    call_body = src[call_site:call_site + 300]
    check("its call site (post-solve) forwards slot_day_index",
          "slot_day_index=slot_day_index" in call_body)


def test_callers_build_slot_day_index_from_retailer_plans():
    planner_src = _read(os.path.join("advisory", "planner.py"))
    check("advisory/planner.py imports slot_calendar_day_index",
          "slot_calendar_day_index" in planner_src)
    check("advisory/planner.py passes slot_day_index into optimize_hourly_schedule",
          "slot_day_index=slot_day_index" in planner_src)

    pc_src = _read("plan_calculator.py")
    check("plan_calculator.py imports slot_calendar_day_index",
          "slot_calendar_day_index" in pc_src)
    check("plan_calculator.py passes slot_day_index into the LP call",
          "slot_day_index=slot_day_index" in pc_src)


if __name__ == "__main__":
    test_optimizer_signature_chain_forwards_slot_day_index()
    test_lp_optimize_forwards_slot_day_index_to_lp_scipy()
    test_optimize_hourly_schedule_forwards_slot_day_index_to_lp_optimize()
    test_day_chunk_computation_uses_day_groups_not_old_positional_math()
    test_consolidate_deferrable_schedule_accepts_and_forwards_slot_day_index()
    test_callers_build_slot_day_index_from_retailer_plans()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — slot_day_index is wired end to end.")
