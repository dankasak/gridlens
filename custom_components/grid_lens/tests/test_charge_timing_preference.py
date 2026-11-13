#!/usr/bin/env python3
"""Tests for charge_timing_preference.py (Prefer early / No preference / Prefer
just-in-time per SOC-tracked deferrable device).

No HA imports in charge_timing_preference.py by design (see its docstring), so
its read/write/rank rules are fully testable here without the `homeassistant`
package (unavailable in this container). The solar-coverage ordering the live
path actually uses lives in battery_optimizer._solar_first_slot_order and is
covered here too — it is pure Python (battery_optimizer only imports
numpy/scipy inside its solver methods), so it imports fine in this container.
What this does NOT cover: ChargeTimingPreferenceStore's Store I/O and the
battery_optimizer.py objective wiring itself (scipy isn't importable in this
container — see test_charge_target.py's header for the established split) —
those need live verification on the actual HA instance.

Run:  python3 test_charge_timing_preference.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

from battery_optimizer import _solar_first_slot_order  # noqa: E402
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


def test_solar_order_prefers_earliest_covered_slot_over_earliest_overall():
    """The 2026-11-12 fix: "Prefer early" means the earliest slot the sun can
    actually run the device in. Slots 0-1 have some sun but not enough; 2-4 do."""
    surplus = {0: 1.1, 1: 1.6, 2: 4.8, 3: 5.0, 4: 4.9}
    order = _solar_first_slot_order(True, [0, 1, 2, 3, 4], surplus, demand_kwh=3.84)
    check("covered slots come first, earliest-covered (2) most preferred",
          order[:3] == [2, 3, 4], f"order={order}")
    check("the uncovered dawn slots rank last, sunniest of them first",
          order[3:] == [1, 0], f"order={order}")


def test_solar_order_prefer_just_in_time_takes_the_latest_covered_slot():
    surplus = {0: 1.1, 1: 4.8, 2: 5.0, 3: 4.9}
    order = _solar_first_slot_order(False, [0, 1, 2, 3], surplus, demand_kwh=3.84)
    check("latest covered slot (3) is most preferred", order[0] == 3, f"order={order}")
    check("the uncovered slot still ranks last", order[-1] == 0, f"order={order}")


def test_solar_order_is_a_permutation_and_keeps_every_slot():
    surplus = {0: 0.0, 1: 2.0, 2: 2.0, 3: 9.0}
    for prefer_early in (True, False):
        order = _solar_first_slot_order(prefer_early, [0, 1, 2, 3], surplus, demand_kwh=3.0)
        check(f"prefer_early={prefer_early}: every slot appears exactly once",
              sorted(order) == [0, 1, 2, 3], f"order={order}")


def test_solar_order_equal_surplus_falls_back_to_the_clock():
    """A fixture with no solar differentiation (every slot identical, including
    the all-zero case) must reproduce the original pure clock ordering exactly —
    this is what keeps the change a no-op wherever solar can't discriminate."""
    surplus = {t: 0.0 for t in range(5)}
    check("prefer_early with no solar signal is still earliest-first",
          _solar_first_slot_order(True, list(range(5)), surplus, 1.0) == [0, 1, 2, 3, 4])
    check("prefer_just_in_time with no solar signal is still latest-first",
          _solar_first_slot_order(False, list(range(5)), surplus, 1.0) == [4, 3, 2, 1, 0])


def test_solar_order_zero_demand_device_treats_nothing_as_covered():
    """A device with no max_kw (demand_kwh == 0) must not be reported as covered
    by every slot — that would make the "covered" group meaningless and silently
    restore pure clock ordering."""
    surplus = {0: 0.0, 1: 5.0}
    order = _solar_first_slot_order(True, [0, 1], surplus, demand_kwh=0.0)
    check("zero-demand device orders by surplus, not by coverage",
          order == [1, 0], f"order={order}")


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
    test_solar_order_prefers_earliest_covered_slot_over_earliest_overall()
    test_solar_order_prefer_just_in_time_takes_the_latest_covered_slot()
    test_solar_order_is_a_permutation_and_keeps_every_slot()
    test_solar_order_equal_surplus_falls_back_to_the_clock()
    test_solar_order_zero_demand_device_treats_nothing_as_covered()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all Charge Timing Preference tests passed.")
