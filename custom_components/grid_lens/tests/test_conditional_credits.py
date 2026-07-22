#!/usr/bin/env python3
"""Tests for conditional day-credits (GloBird ZEROHERO's "$1/day when imports
are 0.03 kWh/hour or less, 6pm-9pm" and the generic mechanism behind it).

Covers retailer_plans.build_conditional_credits: window matching, and — the
important correctness fix — grouping masked hours by REAL calendar date
(day_index) rather than a fixed slots-since-horizon-start chunk. The LP
horizon starts at "now", not local midnight, so a naive t // slots_per_day
grouping can split one calendar day's window across two chunks whenever "now"
happens to fall inside a future occurrence of the window — which would double
the $1/day credit in the objective (see battery_optimizer.py's credit_blocks).

battery_optimizer.py itself imports scipy inside _lp_scipy, and scipy isn't
importable in this container, so the actual MILP solve is exercised on the
live HA instance instead (see GRIDLENS_CHECKLIST.md). retailer_plans.py has
no HA/scipy dependency, so it's tested directly here, no stubbing needed.

Run:  python3 test_conditional_credits.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_COMPONENT))  # so "custom_components.grid_lens" resolves if needed
sys.path.insert(0, _COMPONENT)

from retailer_plans import PlanFromData, build_conditional_credits  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


ZEROHERO_PLAN = {
    "id": "globird_zerohero",
    "name": "ZEROHERO",
    "retailer": "GloBird Energy",
    "state": "NSW",
    "charges": {"daily_supply_charge": 1.815},
    "import_rates": [],
    "export_rates": [],
    "conditional_credits": [
        {
            "label": "ZEROHERO Credit",
            "condition": "max_import_kwh",
            "threshold_kwh": 0.03,
            "amount_per_day": 1.00,
            "window": {"days": "all", "hours": [18, 19, 20]},
            "note": "Up to $365/year.",
        }
    ],
}


def test_plan_from_data_parses_credit():
    plan = PlanFromData(ZEROHERO_PLAN)
    credits = plan.get_conditional_credits()
    check("PlanFromData exposes one conditional credit", len(credits) == 1)
    check("label round-trips", credits[0]["label"] == "ZEROHERO Credit")
    check("threshold_kwh round-trips", credits[0]["threshold_kwh"] == 0.03)
    check("amount_per_day round-trips", credits[0]["amount_per_day"] == 1.00)


def test_plan_from_data_no_credits_by_default():
    plan = PlanFromData({"id": "flat", "import_rates": [], "export_rates": [], "charges": {"daily_supply_charge": 1.0}})
    check("plan without conditional_credits key returns []", plan.get_conditional_credits() == [])


def test_build_conditional_credits_masks_window_hours():
    plan = PlanFromData(ZEROHERO_PLAN)
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Sydney")
    # Horizon starts well before the window (6am today, 30-min slots, 36h — the
    # real advisory coordinator's shape) so today's full 6-9pm window is visible.
    start = datetime(2026, 7, 22, 6, 0, tzinfo=tz).astimezone(timezone.utc)
    n_slots = int(36 * 60 / 30)
    out = build_conditional_credits(plan, start, n_slots, slot_minutes=30)
    check("one credit block returned", len(out) == 1)
    cc = out[0]
    masked_count = sum(cc["hour_mask"])
    # 3-hour window at 30-min resolution = 6 masked slots per day the window
    # is fully visible; horizon spans ~1.5 calendar-day occurrences of it.
    check("some hours are masked", masked_count > 0, f"masked_count={masked_count}")
    check("masked count is a multiple of 6 (3h @ 30min slots per occurrence)",
          masked_count % 6 == 0, f"masked_count={masked_count}")


def test_day_index_survives_midwindow_horizon_start():
    """The bug this exists to prevent: starting the horizon AT 7pm (mid-window)
    used to risk a slots-since-start chunk boundary landing inside a LATER
    occurrence of the window (at now+24h = 7pm the next day, i.e. inside
    6-9pm), splitting one calendar day's credit into two LP "days" and
    double-counting the $1. day_index must instead put every slot of a given
    real calendar day's window under the same key.
    """
    plan = PlanFromData(ZEROHERO_PLAN)
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Sydney")
    start_local = datetime(2026, 7, 22, 19, 0, tzinfo=tz)  # 7pm today, mid-window
    start = start_local.astimezone(timezone.utc)
    n_slots = int(36 * 60 / 30)  # 36h horizon, matches HORIZON_HOURS
    out = build_conditional_credits(plan, start, n_slots, slot_minutes=30)
    check("credit visible when horizon starts mid-window", len(out) == 1)
    cc = out[0]
    day_index = cc["day_index"]
    mask = cc["hour_mask"]

    masked_days = sorted({day_index[t] for t in range(n_slots) if mask[t]})
    # Tomorrow's full 6-9pm occurrence (27-30h from a 7pm start) must NOT be
    # split: every masked slot within it shares one day_index distinct from
    # today's partial (already-mid-window) occurrence.
    check("exactly two distinct calendar days masked (today's remainder + tomorrow's full window)",
          len(masked_days) == 2, f"masked_days={masked_days}")

    tomorrow_slots = [t for t in range(n_slots) if mask[t] and day_index[t] == masked_days[1]]
    check("tomorrow's occurrence has all 6 half-hour slots under one day_index",
          len(tomorrow_slots) == 6, f"tomorrow_slots={len(tomorrow_slots)}")


if __name__ == "__main__":
    test_plan_from_data_parses_credit()
    test_plan_from_data_no_credits_by_default()
    test_build_conditional_credits_masks_window_hours()
    test_day_index_survives_midwindow_horizon_start()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all conditional-credit tests passed.")
