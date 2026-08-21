"""Offline tests for the LP's grid-import bound and the deferrable-solver gate.

The bound in question (`_import_bound`) is the reason every plan on a real install
came back INFEASIBLE from scipy on 2026-08-21 and silently fell through to the greedy
fallback. It is pure arithmetic, so it is testable here without scipy/numpy/highspy —
none of which are importable in this container.

Run: python3 tests/test_import_bound.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    # battery_optimizer imports numpy/scipy lazily inside the solve methods, so the
    # module itself imports cleanly with nothing installed.
    spec = importlib.util.spec_from_file_location("bo", os.path.join(_COMPONENT, "battery_optimizer.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["bo"] = m
    spec.loader.exec_module(m)
    return m


bo = _load()


def test_bound_covers_peak_net_load():
    """The old battery-derived bound is the whole bug: it ignored the house."""
    load = [2.0] * 24
    load[18] = 30.0          # one heavy evening hour
    solar = [0.0] * 24

    old = (5.0 + 5.0) * 2.0 * 1.0          # what the code used to compute = 20 kWh
    new = bo._import_bound(load, solar, [], 1.0, 5.0, 5.0)

    # The 30 kWh hour needs more import than the battery-derived bound allows, even
    # after the battery discharges flat out (20 + 5 = 25 < 30) — that is exactly the
    # equality-constraint violation scipy reported as "infeasible".
    assert old + 5.0 < 30.0, "precondition: old bound could not serve the peak"
    assert new > 30.0, new
    print(f"  ✓ peak 30 kWh hour: old bound {old:.0f} kWh (infeasible) → new {new:.0f} kWh")


def test_solar_offsets_the_peak():
    """Import only has to cover NET load, so on-site solar lowers the bound."""
    load = [10.0] * 24
    with_solar = bo._import_bound(load, [8.0] * 24, [], 1.0, 5.0, 5.0)
    without = bo._import_bound(load, [0.0] * 24, [], 1.0, 5.0, 5.0)
    assert with_solar < without, (with_solar, without)
    print("  ✓ net load, not gross: solar reduces the required bound")


def test_deferrable_headroom_included():
    """A deferrable device the LP may switch on must fit under the bound too."""
    load = [1.0] * 24
    with_ev = bo._import_bound(
        load, [0.0] * 24, [{"max_kw": 22.0}], 1.0, 5.0, 5.0
    )
    # The invariant that matters: base load and the charger running together must sit
    # comfortably inside the bound, or that hour's energy balance is unsatisfiable.
    assert with_ev > 1.0 + 22.0, with_ev
    print("  ✓ a 22 kW EV charger's draw is inside the bound, not outside it")


def test_battery_floor_retained_for_tiny_loads():
    """A near-zero horizon still gets the old battery-derived value as a floor."""
    b = bo._import_bound([0.0] * 24, [0.0] * 24, [], 1.0, 5.0, 5.0)
    assert b == (5.0 + 5.0) * 2.0 * 1.0, b
    print("  ✓ battery-derived floor kept when there is no load to size against")


def test_bound_survives_a_broken_sensor():
    """The reading that triggered this: 733 kW average from a miscalibrated meter."""
    load = [733.0] * 24
    b = bo._import_bound(load, [0.0] * 24, [], 1.0, 5.0, 5.0)
    assert b > 733.0, b
    # Still finite — it doubles as the conditional-credit big-M, and an infinite
    # import lets an FiT>import plan farm unbounded arbitrage.
    assert b != float("inf")
    print("  ✓ absurd meter reading yields a large but finite bound, not infeasibility")


def test_half_hour_slots_scale():
    """Bounds are per-slot ENERGY, so dt must scale the power terms."""
    load = [5.0] * 48
    hourly = bo._import_bound(load, [0.0] * 48, [{"max_kw": 7.0}], 1.0, 5.0, 5.0)
    half = bo._import_bound(load, [0.0] * 48, [{"max_kw": 7.0}], 0.5, 5.0, 5.0)
    assert half < hourly, (half, hourly)
    print("  ✓ half-hour slots produce a smaller per-slot energy bound")


def test_malformed_deferrable_entry_tolerated():
    b = bo._import_bound([1.0], [0.0], [{"max_kw": None}, {}], 1.0, 5.0, 5.0)
    assert b > 0
    print("  ✓ a deferrable entry with a missing/None max_kw doesn't crash the bound")


# ----------------------------------------------------------------- solver gate
class _FakeOpt:
    """Just enough of BatteryOptimizer to exercise _lp_optimize's routing."""

    max_charge_rate_kw = 5.0
    max_discharge_rate_kw = 5.0

    def __init__(self):
        self.called = []

    def _lp_scipy(self, *a, **k):
        self.called.append("scipy")
        raise RuntimeError("scipy unavailable in this test")

    def _lp_pulp(self, *a, **k):
        self.called.append("pulp")
        return "pulp-result"

    _lp_optimize = bo.BatteryOptimizer._lp_optimize


def _run(deferrable):
    opt = _FakeOpt()
    try:
        opt._lp_optimize([0.0] * 24, [1.0] * 24, [0.3] * 24, [0.05] * 24,
                         5.0, 24, deferrable)
    except Exception:
        pass
    return opt.called


def test_deferrable_horizon_never_reaches_a_blind_solver():
    """PuLP models no deferrable loads — it must not be handed a horizon with any."""
    called = _run([{"max_kw": 7.0, "daily_kwh": 12.0}])
    assert "pulp" not in called, called
    assert called == ["scipy"], called
    print("  ✓ with deferrable loads: scipy only, PuLP refused")


def test_battery_only_horizon_may_fall_back_to_pulp():
    """Without deferrable loads PuLP is still a legitimate last resort."""
    called = _run([])
    assert called == ["scipy", "pulp"], called
    print("  ✓ without deferrable loads: scipy first, then PuLP")


def test_highspy_path_is_gone():
    """The parallel hand-rolled HiGHS model was removed on 2026-08-22 — scipy's
    linprog/milp already are HiGHS. Guard against it being reintroduced."""
    assert not hasattr(bo.BatteryOptimizer, "_lp_highspy"), \
        "the highspy path is back; scipy already uses HiGHS"
    assert "highspy" not in open(
        os.path.join(_COMPONENT, "battery_optimizer.py")).read().replace(
            "highspy` lived here until", ""), "stray highspy reference"
    print("  ✓ no second hand-rolled HiGHS model")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"Running {len(tests)} import-bound / solver-gate tests\n")
    for t in tests:
        t()
    print(f"\n✅ all {len(tests)} passed")
