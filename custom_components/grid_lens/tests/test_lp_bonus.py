#!/usr/bin/env python3
"""Tests for the LP's opportunistic "bonus" energy (battery_optimizer ``bonus_value``),
the LP-native form of Greedy Consumption's forecast-surplus condition, currently run
in SHADOW mode only (advisory/planner.py ``_shadow_bonus``).

These need a REAL scipy solve. scipy isn't installable in the Claude add-on container,
so every solve test skips there; run on the LXC (GRIDLENS_CHECKLIST.md):

    docker run --rm -v "$PWD":/w -w /w python:3.12-slim \
        sh -c "pip -q install numpy scipy && python3 tests/test_lp_bonus.py"

What's covered:
  1. No bonus_value -> no bonus anywhere, and the solve is unchanged.
  2. Spill priced at $0 is soaked up as bonus.
  3. Paid export (above the bonus value) is NOT diverted to bonus.
  4. Battery energy needed later for expensive import is NOT spent on bonus.
  5. Front-loading: when the device can't absorb the afternoon spill in the
     afternoon alone, the LP draws bonus earlier, refilling the battery from spill.
  6. An SOC-tracked device's bonus respects its SOC ceiling.
  7. def + bonus never exceeds the device's rating in a slot.
  8. Bonus is scoped to day 0 (no bonus on later calendar days).
  9. Minimum Export Price: below-floor export counts as spill.

Run:  python3 test_lp_bonus.py
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
BONUS = 0.01


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def _optimizer(capacity=10.0, rate=5.0):
    return BatteryOptimizer(
        capacity_kwh=capacity, max_charge_rate_kw=rate, max_discharge_rate_kw=rate,
        efficiency_percent=95.0, min_soc_percent=10.0, max_soc_percent=100.0,
    )


# 24 hourly slots, all calendar day 0 unless a test says otherwise.
def _solar(peak, start=7, end=18):
    out = []
    for h in range(24):
        if start <= h < end:
            mid = (start + end - 1) / 2
            out.append(max(0.0, peak * (1 - abs(h - mid) / ((end - start) / 2))))
        else:
            out.append(0.0)
    return out


def _solve(opt, solar, load, imp, exp, devs, soc=50.0, **kw):
    kw.setdefault("slot_day_index", [0] * len(solar))
    kw.setdefault("soc_reward", 0.0003)
    kw.setdefault("terminal_soc_value", sum(exp) / len(exp))
    return opt.optimize_hourly_schedule(
        solar, load, imp, exp, initial_soc_percent=soc, deferrable_loads=devs,
        timestep_hours=1.0, **kw,
    )


def _bonus(res, i=0):
    return [row.get("bonus_per_device", [0.0] * (i + 1))[i] for row in res["schedule"]]


def _dev(bonus=True, max_kw=2.0, daily=2.0, **extra):
    d = {"daily_kwh": daily, "max_kw": max_kw, "hour_mask": None, "name": "dev"}
    if bonus:
        d["bonus_value"] = BONUS
    d.update(extra)
    return d


LOAD = [0.5] * 24
IMP = [0.30] * 7 + [0.20] * 10 + [0.45] * 4 + [0.30] * 3


def test_no_bonus_is_unchanged():
    opt = _optimizer()
    solar = _solar(8.0)
    base = _solve(opt, solar, LOAD, IMP, [0.0] * 24, [_dev(bonus=False)])
    check("1. no bonus_value -> zero bonus", sum(_bonus(base)) < 1e-6)
    check("1. solver is lp", str(base["solver"]).startswith("lp/"), base["solver"])


def test_spill_soaked_up():
    opt = _optimizer()
    res = _solve(opt, _solar(8.0), LOAD, IMP, [0.0] * 24, [_dev()])
    base = _solve(opt, _solar(8.0), LOAD, IMP, [0.0] * 24, [_dev(bonus=False)])
    b = sum(_bonus(res))
    check("2. $0 spill -> bonus taken", b > 5.0, f"bonus={b:.2f}")
    # Bonus may also use battery energy the horizon-end valuation prices at $0 here
    # (mean export rate = 0), but must never be funded by paid import.
    check("2. bonus never adds paid import",
          res["total_import_kwh"] <= base["total_import_kwh"] + 1e-3,
          f"import {base['total_import_kwh']:.2f} -> {res['total_import_kwh']:.2f}")


def test_paid_export_kept():
    opt = _optimizer()
    res = _solve(opt, _solar(8.0), LOAD, IMP, [0.08] * 24, [_dev()])
    b = sum(_bonus(res))
    check("3. 8c export -> no bonus", b < 1e-3, f"bonus={b:.3f}")


def test_battery_not_drained_for_bonus_when_needed_later():
    opt = _optimizer(capacity=20.0)
    # Modest solar the battery can absorb entirely, and a house load big enough that
    # every stored kWh displaces 30-45c import later — none of it is spare.
    res = _solve(opt, _solar(3.0), [1.5] * 24, IMP, [0.0] * 24, [_dev()], soc=20.0)
    b = sum(_bonus(res))
    check("4. no spill, costly evening -> no bonus", b < 0.05, f"bonus={b:.3f}")


def test_front_loading():
    opt = _optimizer(capacity=10.0, rate=5.0)
    # Morning sun the battery could take, then a large afternoon spill.
    solar = [0.0] * 7 + [1.5, 2.0, 2.5, 3.0] + [8.0] * 5 + [2.0, 1.0] + [0.0] * 6
    dev = _dev(max_kw=1.0, daily=0.0)
    res = _solve(opt, solar, LOAD, IMP, [0.0] * 24, [dev], soc=40.0)
    b = _bonus(res)
    morning = sum(b[7:11])
    total = sum(b)
    check("5. bonus also drawn before the spill (front-load)", morning > 1.0,
          f"morning={morning:.2f} per-slot={[round(x, 2) for x in b]}")
    check("5. total bonus exceeds what the spill hours alone allow (7 h x 1 kW)",
          total > 7.0 + 1e-3, f"total={total:.2f}")


def test_soc_ceiling_caps_bonus():
    opt = _optimizer()
    dev = _dev(max_kw=7.0, daily=0.0, soc_capacity_kwh=10.0, soc_initial_percent=70.0,
               soc_max_percent=80.0, soc_charge_efficiency_percent=90.0)
    res = _solve(opt, _solar(10.0), LOAD, IMP, [0.0] * 24, [dev])
    # def_i[0] is excluded: the pre-existing SOC model leaves slot 0's planned draw out
    # of the device's SOC (OPEN_ITEMS.md). The bonus, slot 0 included, must fit.
    total = sum(_bonus(res)) + sum(r["deferrable_per_device"][0] for r in res["schedule"][1:])
    headroom = (8.0 - 7.0) / 0.9
    check("6. SOC ceiling caps def + bonus", total <= headroom + 1e-3,
          f"total={total:.3f} headroom={headroom:.3f}")


def test_rating_shared():
    opt = _optimizer()
    dev = _dev(max_kw=2.0, daily=6.0)
    res = _solve(opt, _solar(10.0), LOAD, IMP, [0.0] * 24, [dev])
    worst = max(
        r["deferrable_per_device"][0] + r["bonus_per_device"][0] for r in res["schedule"]
    )
    check("7. def + bonus <= max_kw*dt", worst <= 2.0 + 1e-6, f"worst={worst:.3f}")


def test_bonus_day0_only():
    opt = _optimizer()
    solar = _solar(8.0) * 2
    imp = IMP * 2
    sdi = [0] * 12 + [1] * 24 + [2] * 12   # horizon starts at noon
    res = _solve(opt, solar, LOAD * 2, imp, [0.0] * 48, [_dev()], slot_day_index=sdi)
    b = _bonus(res)
    check("8. no bonus after day 0", sum(b[12:]) < 1e-6, f"later={sum(b[12:]):.3f}")


def test_min_export_price_floor():
    opt = _optimizer()
    exp = [0.03] * 24
    with_floor = _solve(opt, _solar(8.0), LOAD, IMP, exp, [_dev()], min_export_price=0.05)
    no_floor = _solve(opt, _solar(8.0), LOAD, IMP, exp, [_dev()])
    check("9. 3c export under a 5c floor -> bonus", sum(_bonus(with_floor)) > 5.0,
          f"bonus={sum(_bonus(with_floor)):.2f}")
    check("9. 3c export with no floor -> no bonus", sum(_bonus(no_floor)) < 1e-3,
          f"bonus={sum(_bonus(no_floor)):.3f}")


if __name__ == "__main__":
    try:
        import scipy  # noqa: F401
    except ImportError:
        print("SKIP: scipy not available (run on the LXC — see module docstring)")
        sys.exit(0)
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{len(_FAILURES)} failure(s)" if _FAILURES else "\nall passed")
    sys.exit(1 if _FAILURES else 0)
