#!/usr/bin/env python3
"""Tests for charge_timing_preference.py (Prefer early / No preference / Prefer
just-in-time per SOC-tracked deferrable device).

No HA imports in charge_timing_preference.py by design (see its docstring), so
its read/write/rank rules are fully testable here without the `homeassistant`
package (unavailable in this container). What this does NOT cover:
ChargeTimingPreferenceStore's Store I/O and the battery_optimizer.py objective
wiring itself (scipy isn't importable in this container either — see
test_charge_target.py's header for the established split) — those need live
verification on the actual HA instance.

Run:  python3 test_charge_timing_preference.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

from charge_timing_preference import (  # noqa: E402
    DEFAULT,
    LABELS,
    NO_PREFERENCE,
    OPTIONS,
    PREFER_EARLY,
    PREFER_JUST_IN_TIME,
    VALUES_BY_LABEL,
    rank,
    read,
    write,
)

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def test_unset_device_reads_default():
    check("unset device reads DEFAULT", read({}, "sensor.ev") == DEFAULT)


def test_default_is_just_in_time():
    check("the product default is Prefer just-in-time", DEFAULT == PREFER_JUST_IN_TIME)


def test_set_reads_back():
    data = write({}, "sensor.ev", PREFER_EARLY)
    check("set reads back the value", read(data, "sensor.ev") == PREFER_EARLY)


def test_no_preference_reads_back():
    data = write({}, "sensor.ev", NO_PREFERENCE)
    check("No preference is stored and read back, not treated as unset",
          read(data, "sensor.ev") == NO_PREFERENCE)


def test_unrecognised_value_coerced_to_default_on_write():
    data = write({}, "sensor.ev", "some_future_value")
    check("an unrecognised write is coerced to DEFAULT rather than stored raw",
          read(data, "sensor.ev") == DEFAULT)


def test_malformed_stored_value_reads_default_not_raise():
    data = {"sensor.ev": {"preference": "garbage"}}
    check("malformed stored preference reads DEFAULT instead of raising",
          read(data, "sensor.ev") == DEFAULT)


def test_devices_are_independent():
    data = write({}, "sensor.ev", PREFER_EARLY)
    data = write(data, "sensor.pool", NO_PREFERENCE)
    check("one device's preference doesn't affect another's",
          read(data, "sensor.ev") == PREFER_EARLY
          and read(data, "sensor.pool") == NO_PREFERENCE)


def test_every_option_has_a_label_and_round_trips():
    check("every OPTIONS value has a LABELS entry",
          all(o in LABELS for o in OPTIONS))
    check("every label round-trips back to its value via VALUES_BY_LABEL",
          all(VALUES_BY_LABEL[LABELS[o]] == o for o in OPTIONS))


def test_rank_prefer_early_cheapest_at_slot_zero():
    floor_slot = 6
    ranks = [rank(PREFER_EARLY, t, floor_slot) for t in range(floor_slot)]
    check("Prefer early ranks slot 0 cheapest and the last pre-floor slot priciest",
          ranks[0] == 0 and ranks[-1] == floor_slot - 1 and ranks == sorted(ranks))


def test_rank_prefer_just_in_time_cheapest_at_last_slot():
    floor_slot = 6
    ranks = [rank(PREFER_JUST_IN_TIME, t, floor_slot) for t in range(floor_slot)]
    check("Prefer just-in-time ranks the last pre-floor slot cheapest, slot 0 priciest",
          ranks[-1] == 0 and ranks[0] == floor_slot - 1 and ranks == sorted(ranks, reverse=True))


def test_rank_unrecognised_preference_matches_just_in_time():
    floor_slot = 4
    for t in range(floor_slot):
        check(f"unrecognised preference ranks slot {t} same as Prefer just-in-time",
              rank("garbage", t, floor_slot) == rank(PREFER_JUST_IN_TIME, t, floor_slot))


if __name__ == "__main__":
    test_unset_device_reads_default()
    test_default_is_just_in_time()
    test_set_reads_back()
    test_no_preference_reads_back()
    test_unrecognised_value_coerced_to_default_on_write()
    test_malformed_stored_value_reads_default_not_raise()
    test_devices_are_independent()
    test_every_option_has_a_label_and_round_trips()
    test_rank_prefer_early_cheapest_at_slot_zero()
    test_rank_prefer_just_in_time_cheapest_at_last_slot()
    test_rank_unrecognised_preference_matches_just_in_time()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all Charge Timing Preference tests passed.")
