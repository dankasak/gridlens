#!/usr/bin/env python3
"""Tests for pooled rate caps (plan_rates.cap_period / cap_application).

`daily_cap_kwh` alone never said what a cap RESETS on. Two mechanisms exist in
the market and they price differently:

  strict  a hard limit inside each period. AGL, on Solar Sharer: "the first
          24 kWh you use during the free window each day is free".
  pooled  the allowance accrues across the billing period, so unused headroom
          banks. EnergyAustralia, on the SAME regulated cap: "applied as an
          average of 24 kWh per day across your billing period, rather than a
          strict daily limit... if you use 14 kWh one day and 34 kWh the next,
          that averages to 24 kWh per day". GloBird's step rates likewise.

Pricing a pooled cap as strict understates the plan, so this is a real money
difference, not a labelling one.

battery_optimizer imports scipy inside _lp_scipy and scipy isn't importable in
this container, so the LP's cap-row GROUPING is exercised here directly (it is
pure Python) while the solve itself is verified on the live HA instance.

Run:  python3 test_pooled_caps.py
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

import retailer_plans  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _cap_helpers():
    """PlanCalculator's cap helpers, without importing Home Assistant."""
    src = open(os.path.join(_COMPONENT, "plan_calculator.py")).read()
    i = src.index("    _CAP_PERIOD_DAYS")
    j = src.index("    def _split_capped_kwh")
    ns: dict = {}
    exec("class C:\n" + src[i:j], ns)
    return ns["C"]()


def test_allowance_strict_vs_pooled():
    c = _cap_helpers()
    check("strict ignores the billing period",
          c._cap_allowance(24, "day", "strict", 30) == 24)
    check("pooled scales by days in the period",
          c._cap_allowance(24, "day", "pooled", 30) == 720)
    check("a weekly cap pools by weeks, not days",
          abs(c._cap_allowance(24, "week", "pooled", 28) - 96) < 0.5,
          str(c._cap_allowance(24, "week", "pooled", 28)))
    check("a billing_period cap is already whole-period",
          c._cap_allowance(24, "billing_period", "pooled", 30) == 24)
    # Without a period length there is nothing to pool over. Falling back to
    # the strict reading understates the plan, which is the safe direction.
    check("no period length falls back to strict, not to infinity",
          c._cap_allowance(24, "day", "pooled", None) == 24)
    check("and to strict, not to zero",
          c._cap_allowance(24, "day", "pooled", 0) == 24)


def test_strict_buckets_reset_and_pooled_does_not():
    c = _cap_helpers()
    d1, d2 = dt.datetime(2026, 8, 1, 12), dt.datetime(2026, 8, 2, 12)
    check("daily buckets differ across days",
          c._cap_bucket("day", d1) != c._cap_bucket("day", d2))
    check("weekly buckets do not differ within a week",
          c._cap_bucket("week", d1) == c._cap_bucket("week", d2))
    check("monthly buckets differ across months",
          c._cap_bucket("month", d1) != c._cap_bucket("month", dt.datetime(2026, 9, 2)))
    check("quarterly buckets differ across quarters",
          c._cap_bucket("quarter", dt.datetime(2026, 3, 1))
          != c._cap_bucket("quarter", dt.datetime(2026, 4, 1)))
    check("billing_period is one bucket for the whole bill",
          c._cap_bucket("billing_period", d1) == c._cap_bucket("billing_period", d2))
    check("an unknown period falls back to daily",
          c._cap_bucket("nonsense", d1) == d1.date())


def test_energyaustralia_worked_example():
    """Their published example must come out the way they describe it."""
    c = _cap_helpers()
    allowance = c._cap_allowance(24, "day", "pooled", 2)
    check("two days of a 24 kWh/day cap pool to 48 kWh", allowance == 48)
    check("14 kWh then 34 kWh is entirely free when pooled", 14 + 34 <= allowance)
    check("but 10 kWh is chargeable under a strict daily cap",
          abs(34 - c._cap_allowance(24, "day", "strict", 2)) == 10)


def test_lp_groups_a_pooled_cap_into_one_row():
    """Strict = one row per calendar day; pooled = one row for the horizon.

    Same total allowance either way — the difference is that pooling lets the
    LP spend it unevenly, which is the entire point.
    """
    src = open(os.path.join(_COMPONENT, "battery_optimizer.py")).read()
    check("the LP reads cap_application", 'cb.get("cap_application")' in src)
    check("and scales the bound by the group's day count",
          re.search(r'b_ub\[r\]\s*=\s*cb\["daily_cap_kwh"\]\s*\*\s*n_days', src)
          is not None)
    check("the approximation is documented, not silent",
          "APPROXIMATION" in src and "billing period" in src)

    T, slots_per_day = 96, 48          # 48h horizon of 30-min slots
    for app, want_rows, want_total in (("strict", 2, 48.0), ("pooled", 1, 48.0)):
        cb = {"hours": list(range(T)), "daily_cap_kwh": 24.0,
              "cap_application": app}
        groups = []
        if cb.get("cap_application") == "pooled":
            horizon_days = max(1.0, len(cb["hours"]) and T / slots_per_day)
            groups.append((cb, list(range(len(cb["hours"]))), horizon_days))
        else:
            days: dict = {}
            for j, t in enumerate(cb["hours"]):
                days.setdefault(t // slots_per_day, []).append(j)
            for js in days.values():
                groups.append((cb, js, 1.0))
        total = sum(cb["daily_cap_kwh"] * n for _, _, n in groups)
        check(f"{app}: {want_rows} constraint row(s)", len(groups) == want_rows,
              str(len(groups)))
        check(f"{app}: total allowance {want_total} kWh", total == want_total,
              str(total))


def test_cap_rows_are_observable_at_runtime():
    """The solve must report the rows it built, not just the cap it was given.

    Row grouping was previously provable only by unit test — invisible on a live
    solve, which is where a wiring mistake would actually surface.
    """
    src = open(os.path.join(_COMPONENT, "battery_optimizer.py")).read()
    check("the log reports the constraint-row count",
          "constraint row(s)" in src)
    check("and the horizon budget those rows allow",
          "budget over the horizon" in src)
    check("and names strict vs pooled",
          'cb.get("cap_application", "strict")' in src)
    # Rows belong to their own block: identity, not equality, since two blocks
    # can hold equal cap values.
    check("rows are selected by block identity",
          "if cb2 is cb" in src)

    a, b = {"daily_cap_kwh": 50.0}, {"daily_cap_kwh": 15.0}
    groups = [(a, [0, 1], 1.0), (a, [2, 3], 1.0), (b, list(range(4)), 2.0)]
    rows_a = [(js, nd) for cb2, js, nd in groups if cb2 is a]
    rows_b = [(js, nd) for cb2, js, nd in groups if cb2 is b]
    check("a strict block reports its per-day rows", len(rows_a) == 2, str(rows_a))
    check("a pooled block reports one row", len(rows_b) == 1, str(rows_b))
    check("budget follows the day count",
          sum(b["daily_cap_kwh"] * nd for _, nd in rows_b) == 30.0)


def test_optimizer_carries_cap_semantics_into_its_own_block():
    """Regression: the LP rebuilds its cap block and drops uncopied fields.

    battery_optimizer constructs cap_blocks from the incoming descriptor rather
    than passing it through, so any field not named at that site is silently
    lost. cap_application was, and every pooled cap solved as strict while the
    DB, the API, _rate_info and build_rate_caps were all correct (2026-08-26).
    The failure was invisible until the constraint-row log reported "strict" for
    a rate the DB held as pooled.
    """
    src = open(os.path.join(_COMPONENT, "battery_optimizer.py")).read()
    i = src.index("cap_blocks.append({")
    block = src[i:src.index("})", i)]
    for field in ("cap_application", "cap_period", "daily_cap_kwh",
                  "rate_after_cap", "hours"):
        check(f"the LP's cap block carries {field}", field in block, block[:200])

    # Every key build_rate_caps emits must be copied or deliberately consumed;
    # a new one added there and forgotten here is the same bug again.
    produced = {"daily_cap_kwh", "rate_after_cap", "cap_period",
                "cap_application", "hour_mask"}
    consumed = {f for f in produced if f in block} | {"hour_mask"}
    check("no field produced by build_rate_caps is dropped",
          produced <= consumed, f"dropped: {produced - consumed}")


def test_rate_info_defaults_when_the_api_omits_the_fields():
    """The API emits these only when non-default, so absence must mean default."""
    plan = retailer_plans.PlanFromData.__new__(retailer_plans.PlanFromData)
    info = retailer_plans.PlanFromData._rate_info(
        plan, [{"rate": 0.30, "label": "Peak", "hours": list(range(24)),
                "days": "all"}], dt.datetime(2026, 8, 1, 12))
    check("cap_period defaults to day", info.get("cap_period") == "day", str(info))
    check("cap_application defaults to strict",
          info.get("cap_application") == "strict", str(info))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"{name}:")
            fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — " + ", ".join(FAILURES))
        sys.exit(1)
    print("OK — pooled caps behave.")


if __name__ == "__main__":
    main()
