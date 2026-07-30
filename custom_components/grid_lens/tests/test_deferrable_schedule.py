"""Offline tests for the weekly deferrable-load availability grid (schedule_grid.py).

Pure Python by design — schedule_grid has zero HA imports (same convention as
override_expiry.py), so the grid semantics the LP mask builders and the service layer
rely on are testable without HA or scipy. Canonical resolution is 48 half-hour slots
per day; 24-hourly input is accepted and expanded.

Run: python3 tests/test_deferrable_schedule.py
"""
from __future__ import annotations

import importlib.util
import os
import sys

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "schedule_grid", os.path.join(_COMPONENT, "schedule_grid.py")
)
sg = importlib.util.module_from_spec(spec)
sys.modules["schedule_grid"] = sg
spec.loader.exec_module(sg)


def _full_week(v=1, slots=48):
    return [[v] * slots for _ in range(7)]


def test_normalize_valid():
    week = sg.normalize_week(_full_week())
    assert len(week) == 7 and all(len(r) == 48 for r in week)
    # Truthy coercion → strict 0/1
    raw = _full_week(0)
    raw[2][10] = 5
    raw[2][11] = True
    assert sg.normalize_week(raw)[2][10] == 1
    assert sg.normalize_week(raw)[2][11] == 1


def test_normalize_hourly_input_expands():
    # A 24-hour row expands each hour into its two half-hours.
    raw = _full_week(0, slots=24)
    raw[1][14] = 1  # Tuesday 14:00-15:00
    week = sg.normalize_week(raw)
    assert all(len(r) == 48 for r in week)
    assert week[1][28] == 1 and week[1][29] == 1     # 14:00-14:30, 14:30-15:00
    assert week[1][27] == 0 and week[1][30] == 0
    assert sum(sum(r) for r in week) == 2


def test_normalize_rejects_bad_shapes():
    for bad in (None, [], _full_week()[:6], "all", 7):
        try:
            sg.normalize_week(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except ValueError:
            pass
    short_day = _full_week()
    short_day[3] = [1] * 47
    try:
        sg.normalize_week(short_day)
        raise AssertionError("should have rejected a 47-slot day")
    except ValueError:
        pass
    non_numeric = _full_week()
    non_numeric[0][0] = "x"
    try:
        sg.normalize_week(non_numeric)
        raise AssertionError("should have rejected a non-numeric cell")
    except ValueError:
        pass


def test_week_from_hours():
    # None = "all hours" seeds a fully-allowed grid.
    assert sg.week_from_hours(None) == _full_week(1)
    # A static hours set repeats identically on every weekday (the config-spec default),
    # with both half-hours of each allowed hour set.
    week = sg.week_from_hours({18, 19, 20})
    for d in range(7):
        assert sum(week[d]) == 6
        assert week[d][36] == 1 and week[d][41] == 1  # 18:00 and 20:30 slots
        assert week[d][35] == 0 and week[d][42] == 0  # 17:30 and 21:00 slots


def test_slot_allowed_and_fail_open():
    week = _full_week(0)
    week[1][28] = 1  # Tuesday 14:00-14:30 only
    assert sg.slot_allowed(week, 1, 14, 0) is True
    assert sg.slot_allowed(week, 1, 14, 29) is True
    assert sg.slot_allowed(week, 1, 14, 30) is False  # second half blocked
    assert sg.slot_allowed(week, 1, 15) is False
    assert sg.slot_allowed(week, 2, 14) is False
    # Malformed grids fail OPEN (allowed) — a broken store must never pin a device off.
    assert sg.slot_allowed([[1]], 6, 23) is True
    assert sg.slot_allowed(None, 0, 0) is True


def test_hour_fraction():
    week = _full_week(0)
    week[3][14] = 1                 # Thu 7:00-7:30 only → half the hour
    week[3][16] = 1
    week[3][17] = 1                 # Thu 8:00-9:00 fully
    assert sg.hour_fraction(week, 3, 7) == 0.5
    assert sg.hour_fraction(week, 3, 8) == 1.0
    assert sg.hour_fraction(week, 3, 9) == 0.0
    # Fails open at full availability on a malformed grid, matching slot_allowed.
    assert sg.hour_fraction([[1]], 6, 23) == 1.0


def test_max_daily_hours():
    week = _full_week(0)
    week[0] = [1] * 12 + [0] * 36  # Mon: 12 half-hours = 6h
    week[5] = [1] * 48             # Sat: 24h
    assert sg.max_daily_hours(week) == 24.0
    week[5] = [1] * 13 + [0] * 35  # Sat: 6.5h — halves count as 0.5
    assert sg.max_daily_hours(week) == 6.5
    assert sg.max_daily_hours(_full_week(0)) == 0.0
    assert sg.max_daily_hours([]) == 0.0


def test_store_roundtrip_and_clear():
    week = _full_week(0)
    week[3][7] = 1
    data = sg.write_week({}, "sensor.ev", week, "2026-07-30")
    assert sg.read_week(data, "sensor.ev")[3][7] == 1
    assert sg.read_week(data, "sensor.other") is None
    # Clearing removes the key entirely (device reverts to the config spec).
    cleared = sg.write_week(data, "sensor.ev", None, "2026-07-31")
    assert sg.read_week(cleared, "sensor.ev") is None
    assert "sensor.ev" not in cleared
    # write_week is non-mutating on the input dict.
    assert sg.read_week(data, "sensor.ev") is not None


def test_store_malformed_is_none_and_write_rejects():
    # Malformed persisted entries read as "no schedule" rather than crashing.
    assert sg.read_week({"sensor.ev": {"week": [[1]]}}, "sensor.ev") is None
    assert sg.read_week({"sensor.ev": "junk"}, "sensor.ev") is None
    assert sg.read_week(None, "sensor.ev") is None
    # And a malformed write raises without persisting anything.
    try:
        sg.write_week({}, "sensor.ev", [[1]], "2026-07-30")
        raise AssertionError("should have rejected a malformed grid")
    except ValueError:
        pass


if __name__ == "__main__":
    tests = [
        ("normalize_valid", test_normalize_valid),
        ("normalize_hourly_input_expands", test_normalize_hourly_input_expands),
        ("normalize_rejects_bad_shapes", test_normalize_rejects_bad_shapes),
        ("week_from_hours", test_week_from_hours),
        ("slot_allowed_and_fail_open", test_slot_allowed_and_fail_open),
        ("hour_fraction", test_hour_fraction),
        ("max_daily_hours", test_max_daily_hours),
        ("store_roundtrip_and_clear", test_store_roundtrip_and_clear),
        ("store_malformed_is_none_and_write_rejects", test_store_malformed_is_none_and_write_rejects),
    ]
    passed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as err:  # noqa: BLE001
            print(f"FAIL {name}: {err}")
            raise
        print(f"ok   {name}")
        passed += 1
    print(f"\n{passed}/{len(tests)} passed")
