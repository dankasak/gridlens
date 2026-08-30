#!/usr/bin/env python3
"""Tests for greedy_energy_math.py — the pure accumulation logic behind
GreedyEnergyTracker (see greedy_energy.py). Zero HA imports by design (mirrors
load_estimate_math.py's split from load_estimation.py), so fully testable here without the
`homeassistant` package. What this does NOT cover: GreedyEnergyTracker's state-change
listener and Store I/O — those need a running HA core, covered by live verification.

Run: python3 tests/test_greedy_energy_math.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

from greedy_energy_math import accumulate  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def test_first_reading_establishes_baseline_only():
    added, reset = accumulate(None, 12.5, was_greedy=True)
    check("first observation contributes nothing", added == 0.0)
    check("first observation is not a counter reset", reset is False)


def test_delta_counts_while_greedy():
    added, reset = accumulate(10.0, 11.5, was_greedy=True)
    check("delta added in full while greedy", added == 1.5)
    check("not a counter reset", reset is False)


def test_delta_discarded_while_not_greedy():
    added, reset = accumulate(10.0, 11.5, was_greedy=False)
    check("delta contributes nothing while not greedy", added == 0.0)
    check("not a counter reset", reset is False)


def test_zero_delta_is_a_no_op_either_way():
    added_g, _ = accumulate(10.0, 10.0, was_greedy=True)
    added_ng, _ = accumulate(10.0, 10.0, was_greedy=False)
    check("no reading change while greedy adds nothing", added_g == 0.0)
    check("no reading change while not greedy adds nothing", added_ng == 0.0)


def test_counter_reset_is_discarded_not_subtracted():
    added, reset = accumulate(10.0, 2.0, was_greedy=True)
    check("a backwards reading contributes nothing, even while greedy", added == 0.0)
    check("a backwards reading is flagged as a counter reset", reset is True)

    added2, reset2 = accumulate(10.0, 2.0, was_greedy=False)
    check("a backwards reading while not greedy also contributes nothing", added2 == 0.0)
    check("still flagged as a counter reset regardless of greedy state", reset2 is True)


if __name__ == "__main__":
    for fn in [
        test_first_reading_establishes_baseline_only,
        test_delta_counts_while_greedy,
        test_delta_discarded_while_not_greedy,
        test_zero_delta_is_a_no_op_either_way,
        test_counter_reset_is_discarded_not_subtracted,
    ]:
        fn()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all greedy-energy-math tests passed.")
