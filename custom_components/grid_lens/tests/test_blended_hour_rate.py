#!/usr/bin/env python3
"""Tests for retailer_plans.blended_hour_rate — the LP's per-hour rate assignment
at a mid-hour TOU/FiT window boundary.

Before this existed, the LP's hourly rate arrays (plan_calculator.py's
`_calculate_plan_cost_with_battery_optimization`) sampled a plan's rate at a
single instant (the hour's start) to represent the whole simulated hour. A plan
whose window boundary falls at :30 (e.g. Flow Power's 17:30-19:30 FiT window)
would then have its LP-simulated 17:00 hour priced entirely off-peak, even
though half of it is really inside the window — the same bug class
`subhourly_energy_archive.py` fixes for real historical data, just at the LP's
rate-assignment stage instead of the bill-reporting stage. There is no real
data to recover there (the LP's hour is a simulated dispatch decision, not
something that happened), so the fix is to average the two half-hour rates
rather than sample one.

Run:  python3 test_blended_hour_rate.py
"""
from __future__ import annotations

import datetime as dt
import os
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


_TZ = dt.timezone(dt.timedelta(hours=10))


def _flow_power_export_rate(local_dt: dt.datetime) -> float:
    """Flow Power's real shape: 45c 17:30-19:30, 0c everywhere else."""
    minutes = local_dt.hour * 60 + local_dt.minute
    return 0.45 if 17 * 60 + 30 <= minutes < 19 * 60 + 30 else 0.0


def test_hour_wholly_outside_the_window_is_unblended():
    rate = retailer_plans.blended_hour_rate(
        _flow_power_export_rate, dt.datetime(2026, 7, 1, 16, 0, tzinfo=_TZ))
    check("16:00 hour (wholly off-peak) stays 0.0", rate == 0.0, str(rate))


def test_hour_wholly_inside_the_window_is_unblended():
    rate = retailer_plans.blended_hour_rate(
        _flow_power_export_rate, dt.datetime(2026, 7, 1, 18, 0, tzinfo=_TZ))
    check("18:00 hour (wholly peak) stays 0.45", rate == 0.45, str(rate))


def test_hour_the_window_opens_inside_is_blended():
    rate = retailer_plans.blended_hour_rate(
        _flow_power_export_rate, dt.datetime(2026, 7, 1, 17, 0, tzinfo=_TZ))
    check("17:00 hour (window opens at :30) blends to 0.225", rate == 0.225, str(rate))


def test_hour_the_window_closes_inside_is_blended():
    rate = retailer_plans.blended_hour_rate(
        _flow_power_export_rate, dt.datetime(2026, 7, 1, 19, 0, tzinfo=_TZ))
    check("19:00 hour (window closes at :30) blends to 0.225", rate == 0.225, str(rate))


def test_flat_rate_plan_is_never_blended():
    rate = retailer_plans.blended_hour_rate(
        lambda d: 0.33998, dt.datetime(2026, 7, 1, 11, 0, tzinfo=_TZ))
    check("flat rate passes through unchanged", rate == 0.33998, str(rate))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"{name}:")
            fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — " + ", ".join(FAILURES))
        sys.exit(1)
    print("OK — blended_hour_rate behaves.")


if __name__ == "__main__":
    main()
