#!/usr/bin/env python3
"""Pure maths for the ad-hoc "don't discharge/export below X% until a
datetime" reserve on the main battery (export_floor.py) — read/write/clear
semantics and expiry auto-clear. slot_for_datetime itself is re-exported from
charge_target.py (same math either direction) and already covered by
test_charge_target.py, so it isn't re-tested here.

No scipy/numpy/HA needed — same reasoning/split as test_charge_target.py's
header. The LP wiring itself (battery_optimizer.py's export_floor_kwh/
export_floor_slot, advisory/coordinator.py's _export_floor) is unverified
until run on the live HA instance.

Run:  python3 test_export_floor.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_floor as ef

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_read_write_roundtrip():
    data = ef.write_target({}, 50.0, "2026-10-07T07:00:00+11:00")
    got = ef.read_target(data)
    check("roundtrip percent", got["percent"] == 50.0, got)
    check("roundtrip until_iso", got["until_iso"] == "2026-10-07T07:00:00+11:00", got)
    check("empty dict reads None", ef.read_target({}) is None)


def test_write_clears_only_when_both_halves_blank():
    data = ef.write_target({}, 50.0, "2026-10-07T07:00:00+11:00")
    check("nonzero+datetime present", bool(data))
    cleared = ef.write_target(data, 0.0, "")
    check("both blank clears the entry", cleared == {})
    check("cleared entry reads back None", ef.read_target(cleared) is None)


def test_write_retains_partial_entry_instead_of_clearing():
    # Same "setting one field must not blank the other" requirement as
    # charge_target.write_target — see that module's test for the full
    # rationale (this is the plan-level mirror, single entry not keyed by device).
    percent_only = ef.write_target({}, 50.0, "")
    check("percent-only write is retained", bool(percent_only))
    check("percent-only reads as inactive (not both halves set)", ef.read_target(percent_only) is None)
    check("percent-only round-trips via read_raw", ef.read_raw(percent_only) == {"percent": 50.0, "until_iso": ""})

    time_only = ef.write_target({}, 0.0, "2026-10-07T07:00:00+11:00")
    check("time-only write is retained", bool(time_only))
    check("time-only reads as inactive (not both halves set)", ef.read_target(time_only) is None)
    check(
        "time-only round-trips via read_raw",
        ef.read_raw(time_only) == {"percent": 0.0, "until_iso": "2026-10-07T07:00:00+11:00"},
    )

    completed = ef.write_target(percent_only, 50.0, "2026-10-07T07:00:00+11:00")
    got = ef.read_target(completed)
    check("completing the pair makes it active", got is not None, got)
    check("completed percent preserved", got["percent"] == 50.0, got)
    check("completed until_iso preserved", got["until_iso"] == "2026-10-07T07:00:00+11:00", got)


def test_read_rejects_partial_entry():
    data = {"percent": 50.0}  # no until_iso
    check("missing until_iso reads None via read_target", ef.read_target(data) is None)
    check("missing until_iso still visible via read_raw", ef.read_raw(data) == {"percent": 50.0, "until_iso": ""})
    data2 = {"until_iso": "2026-10-07T07:00:00+11:00"}  # no percent
    check("missing percent reads None via read_target", ef.read_target(data2) is None)
    check(
        "missing percent still visible via read_raw",
        ef.read_raw(data2) == {"percent": 0.0, "until_iso": "2026-10-07T07:00:00+11:00"},
    )


def test_is_expired():
    until = dt.datetime(2026, 10, 7, 7, 0, tzinfo=dt.timezone.utc)
    before = until - dt.timedelta(minutes=1)
    after = until + dt.timedelta(minutes=1)
    check("before deadline not expired", not ef.is_expired(until, before))
    check("at deadline expired", ef.is_expired(until, until))
    check("after deadline expired", ef.is_expired(until, after))


def main():
    test_read_write_roundtrip()
    test_write_clears_only_when_both_halves_blank()
    test_write_retains_partial_entry_instead_of_clearing()
    test_read_rejects_partial_entry()
    test_is_expired()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("All export_floor tests passed.")


if __name__ == "__main__":
    main()
