#!/usr/bin/env python3
"""Pure maths for the ad-hoc "charge to X% by a datetime" target
(charge_target.py) — read/write/clear semantics, reach/expiry auto-clear, and
the horizon-slot rounding the LP's floor row binds against.

No scipy/numpy/HA needed — this is exactly the part of the feature that CAN be
exercised in this container (battery_optimizer._lp_scipy itself cannot: scipy
isn't importable here, see test_demand_charge.py's header for the established
split). The LP wiring itself (battery_optimizer.py's track_slots/floor_slot,
advisory/coordinator.py's horizon lookup) is unverified until run on the live
HA instance.

Run:  python3 test_charge_target.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import charge_target as ct

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_read_write_roundtrip():
    data: dict = {}
    data = ct.write_target(data, "sensor.ev", 100.0, "2026-09-15T07:00:00+10:00")
    got = ct.read_target(data, "sensor.ev")
    check("roundtrip percent", got["percent"] == 100.0, got)
    check("roundtrip target_iso", got["target_iso"] == "2026-09-15T07:00:00+10:00", got)
    check("unset device reads None", ct.read_target(data, "sensor.other") is None)


def test_write_clears_on_zero_or_blank():
    data = ct.write_target({}, "sensor.ev", 80.0, "2026-09-15T07:00:00+10:00")
    check("nonzero+datetime present", "sensor.ev" in data)
    cleared = ct.write_target(data, "sensor.ev", 0.0, "2026-09-15T07:00:00+10:00")
    check("percent<=0 clears the entry", "sensor.ev" not in cleared)
    check("percent<=0 reads back None", ct.read_target(cleared, "sensor.ev") is None)
    cleared2 = ct.write_target(data, "sensor.ev", 80.0, "")
    check("blank datetime clears the entry", "sensor.ev" not in cleared2)


def test_read_rejects_partial_entry():
    # A hand-edited or corrupted store entry missing one half must not read as active.
    data = {"sensor.ev": {"percent": 90.0}}  # no target_iso
    check("missing target_iso reads None", ct.read_target(data, "sensor.ev") is None)
    data2 = {"sensor.ev": {"target_iso": "2026-09-15T07:00:00+10:00"}}  # no percent
    check("missing percent reads None", ct.read_target(data2, "sensor.ev") is None)


def test_is_reached():
    check("below target not reached", not ct.is_reached(100.0, 87.0))
    check("at target reached", ct.is_reached(100.0, 100.0))
    check("above target reached", ct.is_reached(90.0, 95.0))
    check("unknown current SOC never reached", not ct.is_reached(100.0, None))


def test_is_expired():
    target = dt.datetime(2026, 9, 15, 7, 0, tzinfo=dt.timezone.utc)
    before = target - dt.timedelta(minutes=1)
    after = target + dt.timedelta(minutes=1)
    check("before deadline not expired", not ct.is_expired(target, before))
    check("at deadline expired", ct.is_expired(target, target))
    check("after deadline expired", ct.is_expired(target, after))


def test_slot_for_datetime_rounds_up_and_bounds():
    start = dt.datetime(2026, 9, 15, 0, 0, tzinfo=dt.timezone.utc)
    # 30-min slots, 96-slot (2-day) horizon.
    check(
        "exact boundary (6:00 -> slot 12)",
        ct.slot_for_datetime(start + dt.timedelta(hours=6), start, 30, 96) == 12,
    )
    check(
        "mid-slot deadline rounds UP, not to nearest",
        # 6:05 falls inside slot 12->13; must bind at 13 (the slot that ENDS at/after
        # the deadline), not 12 (which would let the LP stop 5 min early).
        ct.slot_for_datetime(start + dt.timedelta(hours=6, minutes=5), start, 30, 96) == 13,
    )
    check(
        "one minute before a boundary still rounds up to it",
        ct.slot_for_datetime(start + dt.timedelta(hours=5, minutes=59), start, 30, 96) == 12,
    )
    check(
        "already past (negative delta) -> None",
        ct.slot_for_datetime(start - dt.timedelta(minutes=5), start, 30, 96) is None,
    )
    check(
        "exactly now (delta 0) -> None (nothing left to schedule)",
        ct.slot_for_datetime(start, start, 30, 96) is None,
    )
    check(
        "beyond the horizon's last slot -> None (not yet in view)",
        ct.slot_for_datetime(start + dt.timedelta(hours=49), start, 30, 96) is None,
    )
    check(
        "exactly the horizon's last slot is in view",
        ct.slot_for_datetime(start + dt.timedelta(hours=48), start, 30, 96) == 96,
    )


def main():
    test_read_write_roundtrip()
    test_write_clears_on_zero_or_blank()
    test_read_rejects_partial_entry()
    test_is_reached()
    test_is_expired()
    test_slot_for_datetime_rounds_up_and_bounds()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("All charge_target tests passed.")


if __name__ == "__main__":
    main()
