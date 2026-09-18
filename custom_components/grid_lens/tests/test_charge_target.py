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


def test_write_clears_only_when_both_halves_blank():
    data = ct.write_target({}, "sensor.ev", 80.0, "2026-09-15T07:00:00+10:00")
    check("nonzero+datetime present", "sensor.ev" in data)
    cleared = ct.write_target(data, "sensor.ev", 0.0, "")
    check("both blank clears the entry", "sensor.ev" not in cleared)
    check("cleared entry reads back None", ct.read_target(cleared, "sensor.ev") is None)


def test_write_retains_partial_entry_instead_of_clearing():
    # Regression: setting only the percent entity (before the datetime entity has
    # ever been set) must not be discarded just because target_iso is still blank —
    # this was the "setting one field resets the other" UI bug. A single missing
    # half is a genuine in-progress state, not an explicit clear (that's percent<=0
    # AND target_iso blank together — see test_write_clears_only_when_both_halves_blank).
    percent_only = ct.write_target({}, "sensor.ev", 50.0, "")
    check("percent-only write is retained", "sensor.ev" in percent_only)
    check("percent-only reads as inactive (not both halves set)", ct.read_target(percent_only, "sensor.ev") is None)
    check("percent-only round-trips via read_raw", ct.read_raw(percent_only, "sensor.ev") == {"percent": 50.0, "target_iso": ""})

    time_only = ct.write_target({}, "sensor.ev", 0.0, "2026-09-15T07:00:00+10:00")
    check("time-only write is retained", "sensor.ev" in time_only)
    check("time-only reads as inactive (not both halves set)", ct.read_target(time_only, "sensor.ev") is None)
    check(
        "time-only round-trips via read_raw",
        ct.read_raw(time_only, "sensor.ev") == {"percent": 0.0, "target_iso": "2026-09-15T07:00:00+10:00"},
    )

    # Setting the second half afterwards (as number.py/datetime.py do: read_raw the
    # existing entry, then write_target with the other half carried forward) must
    # complete the pair rather than clobbering the first half that was already there.
    completed = ct.write_target(percent_only, "sensor.ev", 50.0, "2026-09-15T07:00:00+10:00")
    got = ct.read_target(completed, "sensor.ev")
    check("completing the pair makes it active", got is not None, got)
    check("completed percent preserved", got["percent"] == 50.0, got)
    check("completed target_iso preserved", got["target_iso"] == "2026-09-15T07:00:00+10:00", got)


def test_read_rejects_partial_entry():
    # A hand-edited or corrupted store entry missing one half must not read as active
    # via read_target (the gate for the optimizer) — but read_raw must still surface it
    # as-is for the paired entities to display/merge.
    data = {"sensor.ev": {"percent": 90.0}}  # no target_iso
    check("missing target_iso reads None via read_target", ct.read_target(data, "sensor.ev") is None)
    check("missing target_iso still visible via read_raw", ct.read_raw(data, "sensor.ev") == {"percent": 90.0, "target_iso": ""})
    data2 = {"sensor.ev": {"target_iso": "2026-09-15T07:00:00+10:00"}}  # no percent
    check("missing percent reads None via read_target", ct.read_target(data2, "sensor.ev") is None)
    check(
        "missing percent still visible via read_raw",
        ct.read_raw(data2, "sensor.ev") == {"percent": 0.0, "target_iso": "2026-09-15T07:00:00+10:00"},
    )


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
    test_write_clears_only_when_both_halves_blank()
    test_write_retains_partial_entry_instead_of_clearing()
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
