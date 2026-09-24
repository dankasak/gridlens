#!/usr/bin/env python3
"""Pure-function coverage of battery_optimizer._day0_target_kwh — the ground-truth
substitution for today's per-device energy target (both the flat daily-total
equality and the EV/SOC day-0 floor's no-active-charge-target branch).

Bug this guards against (found 2026-09-24): a Wattpilot's Daily Target was
dialed down mid-afternoon to 20% (~2.1 kWh) after it had already drawn ~8.9 kWh
today via Greedy Consumption's live forecast-surplus charging. The day-0 target
was still computed as daily_kwh * (remaining slots / slots_per_day) — a pure
time-fraction proration with zero awareness of energy already metered today —
so the plan kept demanding a fresh fractional slice of the (already massively
exceeded) 2.1 kWh target for the rest of the evening, off grid power. See
GRIDLENS_CHECKLIST.md 2026-09-24.

No scipy needed — this is a plain function, no LP solve involved.

Run:  python3 test_day0_target_kwh.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_COMPONENT))
sys.path.insert(0, _COMPONENT)

from battery_optimizer import _day0_target_kwh  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def test_consumed_today_none_reproduces_legacy_proration():
    # No live data at all (plan_calculator's backtest path) — byte-identical to
    # the old daily_kwh * time_fraction formula, whatever the fraction.
    check(
        "None -> legacy proration, partial day",
        abs(_day0_target_kwh(10.0, 0.3, None) - 3.0) < 1e-9,
    )
    check(
        "None -> legacy proration, full day (fraction 1.0)",
        abs(_day0_target_kwh(10.0, 1.0, None) - 10.0) < 1e-9,
    )
    check(
        "None -> legacy proration, zero remaining",
        abs(_day0_target_kwh(10.0, 0.0, None) - 0.0) < 1e-9,
    )


def test_consumed_today_zero_ignores_time_fraction():
    # Nothing drawn yet today: the WHOLE (possibly Daily-Target-scaled) target
    # is still owed, regardless of how little of today remains — no more
    # discounting by time-of-day once real data is available.
    check(
        "0.0 consumed, late in the day -> full target still owed",
        abs(_day0_target_kwh(2.1, 0.05, 0.0) - 2.1) < 1e-9,
    )


def test_consumed_today_partial_leaves_remaining_balance():
    check(
        "1.5 of 2.1 kWh consumed -> 0.6 kWh remaining, not time-prorated",
        abs(_day0_target_kwh(2.1, 0.3, 1.5) - 0.6) < 1e-9,
    )


def test_consumed_today_exceeding_target_floors_at_zero_not_negative():
    # The exact regression: Wattpilot's target scaled to 2.1 kWh, but ~8.9 kWh
    # already drawn today via Greedy Consumption. Old formula still demanded
    # ~0.66 kWh more overnight off grid power (2.1 * (7.5h remaining/24h)).
    remaining = _day0_target_kwh(2.1, 7.5 / 24.0, 8.9)
    check(
        "already-exceeded target -> 0.0, no more overnight grid charging demanded",
        abs(remaining - 0.0) < 1e-9,
        f"got {remaining}",
    )


def test_consumed_today_exactly_meets_target():
    check(
        "consumed == daily_kwh exactly -> 0.0 remaining",
        abs(_day0_target_kwh(5.0, 0.5, 5.0) - 0.0) < 1e-9,
    )


def test_lp_scipy_actually_calls_the_helper_in_both_places():
    """Source-check regression guard: both day-0 target computations (the flat
    daily-total equality and the EV/SOC floor's no-active-target branch) must
    route through _day0_target_kwh, not a hand-inlined formula that could
    silently drift from it or lose the ground-truth substitution again."""
    src = open(os.path.join(_COMPONENT, "battery_optimizer.py")).read()
    check(
        "_day0_target_kwh is called at least twice in battery_optimizer.py",
        src.count("_day0_target_kwh(") >= 3,  # 1 def + 2 call sites
        f"found {src.count('_day0_target_kwh(')} occurrence(s)",
    )
    check(
        "no leftover inline 'dev[\\'daily_kwh\\'] - consumed_today_kwh' formula",
        "dev['daily_kwh'] - consumed_today_kwh" not in src,
    )


if __name__ == "__main__":
    tests = [
        test_consumed_today_none_reproduces_legacy_proration,
        test_consumed_today_zero_ignores_time_fraction,
        test_consumed_today_partial_leaves_remaining_balance,
        test_consumed_today_exceeding_target_floors_at_zero_not_negative,
        test_consumed_today_exactly_meets_target,
        test_lp_scipy_actually_calls_the_helper_in_both_places,
    ]
    for t in tests:
        t()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — _day0_target_kwh behaves.")
