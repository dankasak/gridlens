#!/usr/bin/env python3
"""Tests for load_estimate_math.py — the pure sample-accept/EMA/integration logic behind
LoadEstimator (see load_estimation.py). Zero HA imports by design (mirrors
override_expiry.py's split from deferrable_overrides.py), so fully testable here without
the `homeassistant` package. What this does NOT cover: LoadEstimator's state-change
listeners, timers, and Store I/O — those need a running HA core, covered by live
verification.

Run: python3 tests/test_load_estimate_math.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

from load_estimate_math import (  # noqa: E402
    EMA_ALPHA,
    MIN_ENERGY_SAMPLE_HOURS,
    SAMPLE_FLOOR_W,
    ema_update,
    energy_sample_avg_w,
    integrate_kwh,
    sample_ceiling_w,
    sample_delta_w,
    sample_is_plausible,
)

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def test_delta_is_settled_minus_baseline():
    check("delta = settled - baseline", sample_delta_w(500.0, 1700.0) == 1200.0)
    check("delta can be negative (device use dropped, not our concern here)",
          sample_delta_w(1700.0, 500.0) == -1200.0)


def test_floor_rejects_noise():
    check("a tiny delta is implausible (noise floor)",
          sample_is_plausible(10.0, seed_kw=1.2) is False)
    check("exactly the floor is plausible", sample_is_plausible(SAMPLE_FLOOR_W, seed_kw=1.2) is True)


def test_ceiling_scales_with_seed():
    # seed 3.0kW -> ceiling = 3000*4 = 12000W (well above the 6000W floor, so this
    # actually exercises the "scales with seed" branch rather than the floor).
    check("within 4x the seed is plausible", sample_is_plausible(11000.0, seed_kw=3.0) is True)
    check("beyond 4x the seed is implausible (unrelated household noise)",
          sample_is_plausible(13000.0, seed_kw=3.0) is False)


def test_ceiling_has_a_floor_for_tiny_or_zero_seed():
    # A near-zero seed must not make the ceiling collapse to ~0 and reject every real
    # sample before any data has refined the estimate.
    check("zero seed still allows a real-looking sample",
          sample_is_plausible(1200.0, seed_kw=0.0) is True)
    check("sample_ceiling_w floors at 6000W for a tiny seed",
          sample_ceiling_w(0.05) == 6000.0)


def test_ema_moves_toward_the_sample_by_alpha():
    updated = ema_update(1200.0, 2000.0)
    expected = (1 - EMA_ALPHA) * 1200.0 + EMA_ALPHA * 2000.0
    check("ema_update matches the weighted-average formula", updated == expected)
    check("ema_update moves toward (not to) the new sample",
          1200.0 < updated < 2000.0)


def test_ema_converges_over_repeated_identical_samples():
    w = 1200.0
    for _ in range(30):
        w = ema_update(w, 1800.0)
    check("EMA converges close to a consistently repeated sample value",
          abs(w - 1800.0) < 1.0, detail=f"got {w}")


def test_integrate_adds_energy_while_on():
    kwh = integrate_kwh(0.0, power_w=1500.0, elapsed_hours=0.5)
    check("1.5kW for 30min adds 0.75kWh", kwh == 0.75)
    kwh2 = integrate_kwh(kwh, power_w=1500.0, elapsed_hours=1.0)
    check("integration accumulates across ticks", kwh2 == 2.25)


def test_integrate_never_goes_backwards():
    check("negative elapsed hours is a no-op, not negative energy",
          integrate_kwh(5.0, power_w=1000.0, elapsed_hours=-1.0) == 5.0)
    check("negative/zero power is a no-op",
          integrate_kwh(5.0, power_w=0.0, elapsed_hours=1.0) == 5.0)


def test_energy_sample_avg_w_computes_rate():
    check("1.0kWh over 1h -> 1000W",
          energy_sample_avg_w(1.0, 1.0) == 1000.0)
    check("0.6kWh over 30min -> 1200W",
          energy_sample_avg_w(0.6, 0.5) == 1200.0)


def test_energy_sample_avg_w_rejects_too_short_a_window():
    check("just under the minimum window is rejected",
          energy_sample_avg_w(1.0, MIN_ENERGY_SAMPLE_HOURS * 0.9) is None)
    check("exactly the minimum window is accepted",
          energy_sample_avg_w(1.0, MIN_ENERGY_SAMPLE_HOURS) is not None)


def test_energy_sample_avg_w_rejects_negative_delta():
    check("a counter that went backwards (reset/rollover) is rejected, not treated as 0",
          energy_sample_avg_w(-0.1, 1.0) is None)


if __name__ == "__main__":
    for fn in [
        test_delta_is_settled_minus_baseline,
        test_floor_rejects_noise,
        test_ceiling_scales_with_seed,
        test_ceiling_has_a_floor_for_tiny_or_zero_seed,
        test_ema_moves_toward_the_sample_by_alpha,
        test_ema_converges_over_repeated_identical_samples,
        test_integrate_adds_energy_while_on,
        test_integrate_never_goes_backwards,
        test_energy_sample_avg_w_computes_rate,
        test_energy_sample_avg_w_rejects_too_short_a_window,
        test_energy_sample_avg_w_rejects_negative_delta,
    ]:
        fn()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all load-estimate-math tests passed.")
