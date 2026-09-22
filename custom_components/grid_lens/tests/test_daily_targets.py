#!/usr/bin/env python3
"""Tests for daily_target_rules.py (Daily Target per-device/master percent
targets).

No HA imports in daily_target_rules.py by design (see its docstring), so
its read_percent/write_percent/clear_percent/should_notify_carryover/
mark_notified rules are fully testable here without the `homeassistant`
package (unavailable in this container — see GRIDLENS_CHECKLIST.md). What
this does NOT cover: DailyTargetStore's Store I/O, persistent_notification
calls, and AdvisoryCoordinator._apply_daily_targets wiring, which need a
running HA core — those need live verification.

Run:  python3 test_daily_targets.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

from daily_target_rules import (  # noqa: E402
    MASTER_KEY,
    clear_percent,
    mark_notified,
    read_percent,
    should_notify_carryover,
    write_percent,
)

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def test_unset_device_reads_none():
    check("unset device reads None (not 0.0)", read_percent({}, "sensor.ev") is None)


def test_set_today_reads_back():
    data = write_percent({}, "sensor.ev", 40.0, "2026-09-21")
    check("set today reads back the value", read_percent(data, "sensor.ev") == 40.0)


def test_zero_is_a_legitimate_explicit_value():
    """Unlike Today Boost, 0% must NOT be treated as "clear" — it means "skip
    this device entirely tomorrow"."""
    data = write_percent({}, "sensor.hot_water", 0.0, "2026-09-21")
    check("writing 0 stores 0, does not remove the entry",
          "sensor.hot_water" in data and read_percent(data, "sensor.hot_water") == 0.0)


def test_negative_value_clamped_not_cleared():
    data = write_percent({}, "sensor.ev", -5.0, "2026-09-21")
    check("a negative write clamps to 0.0 rather than clearing",
          "sensor.ev" in data and read_percent(data, "sensor.ev") == 0.0)


def test_over_100_percent_allowed():
    data = write_percent({}, "sensor.ev", 250.0, "2026-09-21")
    check(">100% is stored as given", read_percent(data, "sensor.ev") == 250.0)


def test_set_yesterday_still_reads_back():
    """No midnight auto-expiry (mirrors Today Boost's own 2026-07-31 fix) — a
    target set on a prior date must still apply today until explicitly cleared."""
    data = write_percent({}, "sensor.ev", 40.0, "2026-09-20")
    check("set on a prior date still reads back today (no auto-expiry)",
          read_percent(data, "sensor.ev") == 40.0)


def test_clear_removes_entry():
    data = write_percent({}, "sensor.ev", 40.0, "2026-09-21")
    data = clear_percent(data, "sensor.ev")
    check("clear_percent removes the entry entirely",
          "sensor.ev" not in data and read_percent(data, "sensor.ev") is None)


def test_clear_is_the_only_way_to_unset_a_zero():
    """Writing 0 does not clear (see test_zero_is_a_legitimate_explicit_value) —
    only clear_percent does."""
    data = write_percent({}, "sensor.ev", 0.0, "2026-09-21")
    check("a 0% pin is still present before clearing", "sensor.ev" in data)
    data = clear_percent(data, "sensor.ev")
    check("clear_percent removes a 0% pin too", "sensor.ev" not in data)


def test_master_key_is_independent_of_devices():
    data = write_percent({}, MASTER_KEY, 60.0, "2026-09-21")
    data = write_percent(data, "sensor.ev", 40.0, "2026-09-21")
    check("master and a device target don't collide",
          read_percent(data, MASTER_KEY) == 60.0
          and read_percent(data, "sensor.ev") == 40.0)


def test_devices_are_independent():
    data = write_percent({}, "sensor.ev", 40.0, "2026-09-21")
    data = write_percent(data, "sensor.pool", 80.0, "2026-09-20")
    check("one device's target doesn't affect another's",
          read_percent(data, "sensor.ev") == 40.0
          and read_percent(data, "sensor.pool") == 80.0)


def test_malformed_stored_value_reads_none_not_raise():
    data = {"sensor.ev": {"percent": "not-a-number", "set_date": "2026-09-21"}}
    check("malformed stored percent reads None instead of raising",
          read_percent(data, "sensor.ev") is None)


def test_no_carryover_notice_same_day():
    data = write_percent({}, "sensor.ev", 40.0, "2026-09-21")
    check("no carry-over notice the same day it was set",
          should_notify_carryover(data, "sensor.ev", "2026-09-21") is False)


def test_carryover_notice_fires_for_a_zero_target_too():
    """The 0-is-meaningful fix must not also break carry-over detection for 0%."""
    data = write_percent({}, "sensor.ev", 0.0, "2026-09-20")
    check("carry-over notice fires for an explicit 0% target on a later day",
          should_notify_carryover(data, "sensor.ev", "2026-09-21") is True)


def test_carryover_notice_fires_once_then_suppressed():
    data = write_percent({}, "sensor.ev", 40.0, "2026-09-20")
    check("carry-over notice fires the first read on a later day",
          should_notify_carryover(data, "sensor.ev", "2026-09-21") is True)
    data = mark_notified(data, "sensor.ev", "2026-09-21")
    check("carry-over notice is suppressed after being marked for that day",
          should_notify_carryover(data, "sensor.ev", "2026-09-21") is False)
    check("value is still readable after being marked notified",
          read_percent(data, "sensor.ev") == 40.0)


def test_no_carryover_notice_when_unset():
    check("no carry-over notice for a device with no target",
          should_notify_carryover({}, "sensor.ev", "2026-09-21") is False)


def test_fresh_write_resets_notified_flag():
    data = write_percent({}, "sensor.ev", 40.0, "2026-09-20")
    data = mark_notified(data, "sensor.ev", "2026-09-21")
    data = write_percent(data, "sensor.ev", 60.0, "2026-09-22")
    check("re-setting on a later day drops the stale notified flag",
          should_notify_carryover(data, "sensor.ev", "2026-09-23") is True)


if __name__ == "__main__":
    test_unset_device_reads_none()
    test_set_today_reads_back()
    test_zero_is_a_legitimate_explicit_value()
    test_negative_value_clamped_not_cleared()
    test_over_100_percent_allowed()
    test_set_yesterday_still_reads_back()
    test_clear_removes_entry()
    test_clear_is_the_only_way_to_unset_a_zero()
    test_master_key_is_independent_of_devices()
    test_devices_are_independent()
    test_malformed_stored_value_reads_none_not_raise()
    test_no_carryover_notice_same_day()
    test_carryover_notice_fires_for_a_zero_target_too()
    test_carryover_notice_fires_once_then_suppressed()
    test_no_carryover_notice_when_unset()
    test_fresh_write_resets_notified_flag()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all Daily Target tests passed.")
