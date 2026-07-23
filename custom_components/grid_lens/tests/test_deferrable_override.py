#!/usr/bin/env python3
"""Tests for the per-day deferrable load override (Feature 2 of
DEFERRABLE_EXPORT_CONTROL_PLAN.md).

override_expiry.py has zero HA imports by design (see its docstring), so its
read_value/write_value expiry rule is fully testable here without the
`homeassistant` package (unavailable in this container — see
GRIDLENS_CHECKLIST.md). What this does NOT cover: DeferrableOverrideStore's Store
I/O and AdvisoryCoordinator._apply_overrides wiring, which need a running HA core
— those are covered by live verification per the plan doc.

Run:  python3 test_deferrable_override.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

from override_expiry import read_value, write_value  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def test_unset_sensor_reads_zero():
    check("unset sensor reads 0.0", read_value({}, "sensor.ev", "2026-07-23") == 0.0)


def test_set_today_reads_back():
    data = write_value({}, "sensor.ev", 20.0, "2026-07-23")
    check("set today reads back the value",
          read_value(data, "sensor.ev", "2026-07-23") == 20.0)


def test_set_yesterday_is_expired():
    data = write_value({}, "sensor.ev", 20.0, "2026-07-22")
    check("set on a prior date reads 0.0 today (expired)",
          read_value(data, "sensor.ev", "2026-07-23") == 0.0)


def test_zero_clears_existing_override():
    data = write_value({}, "sensor.ev", 20.0, "2026-07-23")
    data = write_value(data, "sensor.ev", 0.0, "2026-07-23")
    check("writing 0 removes the entry entirely",
          "sensor.ev" not in data and read_value(data, "sensor.ev", "2026-07-23") == 0.0)


def test_negative_value_also_clears():
    data = write_value({}, "sensor.ev", 20.0, "2026-07-23")
    data = write_value(data, "sensor.ev", -5.0, "2026-07-23")
    check("writing a negative value also clears the entry",
          "sensor.ev" not in data)


def test_devices_are_independent():
    data = write_value({}, "sensor.ev", 20.0, "2026-07-23")
    data = write_value(data, "sensor.pool", 5.0, "2026-07-22")  # stale
    check("one device's override doesn't affect another's",
          read_value(data, "sensor.ev", "2026-07-23") == 20.0
          and read_value(data, "sensor.pool", "2026-07-23") == 0.0)


def test_malformed_stored_value_reads_zero_not_raise():
    data = {"sensor.ev": {"value_kwh": "not-a-number", "set_date": "2026-07-23"}}
    check("malformed stored value_kwh reads 0.0 instead of raising",
          read_value(data, "sensor.ev", "2026-07-23") == 0.0)


if __name__ == "__main__":
    test_unset_sensor_reads_zero()
    test_set_today_reads_back()
    test_set_yesterday_is_expired()
    test_zero_clears_existing_override()
    test_negative_value_also_clears()
    test_devices_are_independent()
    test_malformed_stored_value_reads_zero_not_raise()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all deferrable-override expiry tests passed.")
