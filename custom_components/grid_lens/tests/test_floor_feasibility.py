#!/usr/bin/env python3
"""An ad-hoc export/discharge floor must never make the LP INFEASIBLE.

The incident (2026-10-23, GRIDLENS_CHECKLIST.md). A 25%-until-09:00 floor was set
while the battery sat at 20.4%, i.e. already BELOW its own floor. Two things then
combined:

  1. ``AdvisoryPlanner._grid_charge_hedge_cap`` classified the CURRENT slot as
     "pre-solar" no matter the time of day — its loop tested the running P50 total
     before adding that slot's own solar, so slot 0 always got in. At 08:00 on a
     sunny day the hedge correctly decided no insurance charging was needed and
     pinned slot 0's grid-charge allowance to 0 kWh.
  2. With grid charge barred, slot 0 could only reach 5.731 kWh of the 6.000 kWh
     floor — short by 269 Wh. An unreachable ``lb`` on the SOC variable is a
     deterministic INFEASIBLE, so the entire solve was discarded every 2 minutes
     for a whole morning and the greedy fallback answered instead.

Greedy models no deferrable loads, no rate arbitrage and no SOC floor, so the
published plan skipped a 28c/kWh export window and bought grid power overnight to
hold 87% SOC — while the dashboard badge read a cheerful green "OK".

Covered here:
  * ``_floor_reach_ramp`` — the per-slot reachability clamp's arithmetic, on the real
    numbers from the incident.
  * ``_grid_charge_hedge_cap`` — a horizon that starts with solar already flowing has
    NO pre-solar window, while the genuine pre-dawn hedge still works.
  * ``optimize_hourly_schedule`` — the floor-relaxation retry prefers an LP without
    the floor over greedy with it, and ``solver_note`` is set on every degraded path
    (it is what the advisory card's status badge keys off).

No scipy/numpy/HA needed: battery_optimizer imports numpy/scipy only inside the
solver methods, and planner.py's HA deps are stubbed (same approach and the same
limitation as test_charge_source_split.py / test_grid_charge_cap.py — that the
clamped floor produces the intended *dispatch* still needs a real scipy solve).

Run:  python3 test_floor_feasibility.py
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys
import types
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_COMPONENT))
sys.path.insert(0, _COMPONENT)

from battery_optimizer import BatteryOptimizer, _floor_reach_ramp  # noqa: E402

# ⚠ Drop the component dir again before loading planner.py below. The integration ships
# its own ``datetime.py`` (the export-floor-until entity), which shadows the stdlib
# module for anything imported afterwards — planner.py's ``from datetime import
# timedelta`` then pulls in a HA platform module and dies. battery_optimizer is already
# imported above, which is all this path entry was for.
sys.path.remove(_COMPONENT)

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# --------------------------------------------------------------- planner bootstrap
def _load_planner():
    """Load the real advisory/planner.py with HA + heavy deps stubbed out."""

    def _mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    ha = _mod("homeassistant")
    util = _mod("homeassistant.util")
    dtmod = _mod("homeassistant.util.dt")
    dtmod.now = lambda: None
    util.dt = dtmod
    ha.util = util

    for pkg in ("glff", "glff.inverters", "glff.control", "glff.advisory"):
        m = types.ModuleType(pkg)
        m.__path__ = []
        sys.modules[pkg] = m

    base = types.ModuleType("glff.inverters.base")
    base.BatteryAction = SimpleNamespace(
        CHARGE="charge", DISCHARGE="discharge", SELF_USE="self_use", IDLE="idle"
    )
    sys.modules["glff.inverters.base"] = base

    ex = types.ModuleType("glff.control.executor")
    ex.DispatchInterval = type("DispatchInterval", (), {})
    sys.modules["glff.control.executor"] = ex

    opt = types.ModuleType("glff.battery_optimizer")
    opt.BatteryOptimizer = BatteryOptimizer
    sys.modules["glff.battery_optimizer"] = opt

    models = types.ModuleType("glff.advisory.models")
    models.AdvisoryResult = type("AdvisoryResult", (), {})
    models.ForecastBundle = type("ForecastBundle", (), {})
    sys.modules["glff.advisory.models"] = models

    rp = types.ModuleType("glff.retailer_plans")
    rp.slot_calendar_day_index = lambda *a, **k: []
    sys.modules["glff.retailer_plans"] = rp

    path = os.path.join(_COMPONENT, "advisory", "planner.py")
    spec = importlib.util.spec_from_file_location("glff.advisory.planner", path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "glff.advisory"
    sys.modules["glff.advisory.planner"] = module
    spec.loader.exec_module(module)
    return module


PLANNER = _load_planner()
AdvisoryPlanner = PLANNER.AdvisoryPlanner

# The incident's real inputs (sensor.roof_grid_lens_nsw_planned_dispatch, 08:00 solve).
ETA = math.sqrt(0.90)
E0_INCIDENT = 0.204 * 24.0          # 20.4% of a 24 kWh battery = 4.896 kWh
FLOOR_INCIDENT = 0.25 * 24.0        # the 25% floor               = 6.000 kWh
SOLAR_INCIDENT = [1.076, 1.283]
LOAD_INCIDENT = [0.196, 0.196]


def _optimizer(**kw):
    return BatteryOptimizer(
        capacity_kwh=kw.get("capacity_kwh", 24.0),
        max_charge_rate_kw=kw.get("max_charge_rate_kw", 10.0),
        max_discharge_rate_kw=10.0,
        efficiency_percent=90.0,
        min_soc_percent=10.0,
        max_soc_percent=kw.get("max_soc_percent", 100.0),
    )


# ------------------------------------------------------------- _floor_reach_ramp
def test_ramp_reproduces_the_incident_shortfall():
    """With slot 0's grid-charge allowance pinned to 0, the 6.0 kWh floor is
    unreachable — by the 269 Wh that took the whole morning's solves down."""
    ramp = _floor_reach_ramp(
        E0_INCIDENT, 2, SOLAR_INCIDENT, LOAD_INCIDENT, 0.5, ETA,
        max_charge_kw=10.0, ceiling=24.0,
        no_grid_charge=False, grid_charge_cap_kwh=[0.0, float("inf")],
    )
    check("slot 0 reachable SOC is ~5.731 kWh", abs(ramp[0] - 5.7308) < 1e-3, ramp)
    check("slot 0 falls short of the 6.0 kWh floor", ramp[0] < FLOOR_INCIDENT, ramp)
    check("shortfall is ~269 Wh",
          abs((FLOOR_INCIDENT - ramp[0]) - 0.2692) < 1e-3, ramp)
    # Slot 1 has no hedge cap, so the floor becomes attainable one slot later — which
    # is why the clamp is a per-slot ramp and not one scalar for the whole window.
    check("slot 1 clears the floor once the cap lifts", ramp[1] > FLOOR_INCIDENT, ramp)


def test_ramp_clears_the_floor_once_the_hedge_cap_is_gone():
    """The hedge-cap fix alone resolves the incident: with grid charge permitted,
    slot 0 reaches the floor comfortably (0.28 kWh of import is all it needed)."""
    ramp = _floor_reach_ramp(
        E0_INCIDENT, 2, SOLAR_INCIDENT, LOAD_INCIDENT, 0.5, ETA,
        max_charge_kw=10.0, ceiling=24.0,
        no_grid_charge=False, grid_charge_cap_kwh=None,
    )
    check("unrestricted slot 0 clears the floor", ramp[0] > FLOOR_INCIDENT, ramp)
    check("unrestricted slot 0 is charge-rate limited",
          abs(ramp[0] - (E0_INCIDENT + ETA * 10.0 * 0.5)) < 1e-6, ramp)


def test_ramp_respects_no_grid_charge_and_ceiling():
    solar_only = _floor_reach_ramp(
        E0_INCIDENT, 2, SOLAR_INCIDENT, LOAD_INCIDENT, 0.5, ETA,
        max_charge_kw=10.0, ceiling=24.0,
        no_grid_charge=True, grid_charge_cap_kwh=None,
    )
    check("no_grid_charge uses solar surplus only",
          abs(solar_only[0] - (E0_INCIDENT + ETA * (1.076 - 0.196))) < 1e-6, solar_only)
    check("no_grid_charge matches the 0-allowance case",
          abs(solar_only[0] - 5.7308) < 1e-3, solar_only)

    capped = _floor_reach_ramp(
        5.0, 3, [9.0, 9.0, 9.0], [0.0, 0.0, 0.0], 1.0, ETA,
        max_charge_kw=10.0, ceiling=8.0,
        no_grid_charge=False, grid_charge_cap_kwh=None,
    )
    check("ramp never exceeds the ceiling", max(capped) <= 8.0 + 1e-9, capped)
    check("ramp saturates at the ceiling", abs(capped[-1] - 8.0) < 1e-9, capped)


def test_ramp_is_monotonic_and_starts_from_e0():
    ramp = _floor_reach_ramp(
        4.0, 4, [0.0, 0.5, 2.0, 3.0], [0.2, 0.2, 0.2, 0.2], 0.5, ETA,
        max_charge_kw=10.0, ceiling=24.0,
        no_grid_charge=True, grid_charge_cap_kwh=None,
    )
    check("ramp is non-decreasing", all(b >= a for a, b in zip(ramp, ramp[1:])), ramp)
    check("a zero-surplus first slot cannot gain charge", abs(ramp[0] - 4.0) < 1e-9, ramp)
    check("empty window yields an empty ramp",
          _floor_reach_ramp(4.0, 0, [], [], 0.5, ETA, 10.0, 24.0, False, None) == [])


# --------------------------------------------------- _grid_charge_hedge_cap window
def _bundle(slots, solar, load, export_rate, p10=None, p90=None, dt_hours=0.5):
    return SimpleNamespace(
        slots=slots, solar_kwh=solar, load_kwh=load, export_rate=export_rate,
        solar_kwh_p10=p10 if p10 is not None else solar,
        solar_kwh_p90=p90 if p90 is not None else solar,
        dt_hours=dt_hours,
    )


def test_midday_horizon_has_no_pre_solar_window():
    """THE REGRESSION TEST. A solve whose first slot already has real solar is not a
    blind pre-dawn bet, so nothing may be restricted — this is what pinned slot 0's
    allowance to 0 and made the floor unreachable."""
    planner = AdvisoryPlanner(_optimizer(), min_export_price=0.05)
    T = 20
    solar = [1.076, 1.283] + [2.5] * (T - 2)
    bundle = _bundle(T, solar, [0.2] * T, [0.03] * T)
    caps = planner._grid_charge_hedge_cap(bundle, 20.4)
    check("every slot is unrestricted on a mid-morning solve",
          caps is not None and all(c == float("inf") for c in caps),
          caps[:4] if caps else caps)


def test_predawn_sunny_horizon_still_suppresses_the_hedge():
    """The feature itself must survive the fix: a genuine pre-dawn start on a day
    whose P90 easily refills the battery gets a 0 kWh allowance (no pointless
    insurance charging) — the 2026-10-15 behaviour, unchanged."""
    planner = AdvisoryPlanner(_optimizer(), min_export_price=0.05)
    T = 24
    dark, bright = 6, T - 6
    solar = [0.0] * dark + [3.0] * bright
    bundle = _bundle(T, solar, [0.2] * T, [0.03] * T)
    caps = planner._grid_charge_hedge_cap(bundle, 20.0)
    check("dark slots are restricted", all(c == 0.0 for c in caps[:dark]), caps[:dark])
    check("the first sunny slot is NOT in the window",
          caps[dark] == float("inf"), caps[dark])


def test_predawn_low_forecast_still_hedges():
    """A genuinely weak day (P90 also low) keeps its full-strength hedge."""
    planner = AdvisoryPlanner(_optimizer(), min_export_price=0.05)
    T = 24
    dark, bright = 6, T - 6
    solar = [0.0] * dark + [0.2] * bright
    bundle = _bundle(T, solar, [0.3] * T, [0.03] * T,
                     p10=[0.0] * dark + [0.05] * bright,
                     p90=[0.0] * dark + [0.4] * bright)
    caps = planner._grid_charge_hedge_cap(bundle, 20.0)
    check("a low-P90 day still gets a positive allowance",
          all(c > 0.0 for c in caps[:dark]), caps[:dark])
    check("the allowance stays within the charge rating",
          all(c <= 10.0 * 0.5 + 1e-9 for c in caps[:dark]), caps[:dark])


def test_missing_percentile_bands_disable_the_hedge_entirely():
    planner = AdvisoryPlanner(_optimizer(), min_export_price=0.05)
    bundle = _bundle(8, [0.0] * 8, [0.2] * 8, [0.03] * 8)
    bundle.solar_kwh_p10 = None
    check("no P10/P90 means no restriction at all",
          planner._grid_charge_hedge_cap(bundle, 20.0) is None)


# ------------------------------------------------- relaxation retry / solver_note
def _stub_solvers(opt, scipy_fn=None, pulp_fn=None):
    calls: list[str] = []

    def fake_scipy(self, *a, **kw):
        calls.append("scipy")
        if scipy_fn is None:
            raise RuntimeError("scipy down")
        return scipy_fn(kw)

    def fake_pulp(self, *a, **kw):
        calls.append("pulp")
        if pulp_fn is None:
            raise RuntimeError("pulp down")
        return pulp_fn(kw)

    opt._lp_scipy = fake_scipy.__get__(opt, BatteryOptimizer)
    opt._lp_pulp = fake_pulp.__get__(opt, BatteryOptimizer)
    return calls


def _lp_result(_kw):
    return {"schedule": [], "solver": "lp/scipy", "net_cost": 1.0,
            "final_soc_percent": 50.0}


def _solve(opt, **kw):
    return opt.optimize_hourly_schedule(
        solar_profile=kw.pop("solar", SOLAR_INCIDENT),
        load_profile=kw.pop("load", LOAD_INCIDENT),
        import_rates=[0.216, 0.216], export_rates=[0.03, 0.03],
        initial_soc_percent=20.4, timestep_hours=0.5, **kw,
    )


def test_unsatisfiable_floor_retries_without_it_instead_of_going_greedy():
    """An LP that drops ONE impossible user constraint beats greedy, which would
    ignore the floor too AND misprice every slot."""
    opt = _optimizer()
    calls = _stub_solvers(
        opt, scipy_fn=lambda kw: (_ for _ in ()).throw(RuntimeError("infeasible"))
        if kw.get("export_floor_kwh") is not None else _lp_result(kw)
    )
    out = _solve(opt, export_floor_kwh=FLOOR_INCIDENT, export_floor_slot=2)
    check("an LP answered, not greedy", out.get("solver") == "lp/scipy", out.get("solver"))
    check("solver_note names the dropped floor",
          "floor" in (out.get("solver_note") or "").lower(), out.get("solver_note"))
    check("the retry actually re-solved", calls.count("scipy") >= 2, calls)


def test_total_failure_falls_back_to_greedy_with_a_note():
    opt = _optimizer()
    _stub_solvers(opt)  # scipy and PuLP both fail
    out = _solve(opt, export_floor_kwh=FLOOR_INCIDENT, export_floor_slot=2)
    check("greedy answered", out.get("solver") == "greedy", out.get("solver"))
    check("greedy carries a solver_note", bool(out.get("solver_note")),
          out.get("solver_note"))
    check("the note says the plan is not optimised",
          "greedy" in out["solver_note"].lower(), out["solver_note"])


def test_clean_solve_sets_no_note():
    opt = _optimizer()
    _stub_solvers(opt, scipy_fn=_lp_result)
    out = _solve(opt)
    check("no floor, no note", out.get("solver_note") is None, out.get("solver_note"))
    # A floor the battery can actually reach is honoured silently.
    reachable = _solve(opt, export_floor_kwh=5.0, export_floor_slot=2)
    check("a satisfiable floor sets no note",
          reachable.get("solver_note") is None, reachable.get("solver_note"))


def test_clamped_floor_sets_a_note_even_though_the_solve_succeeded():
    """The clamp keeps the LP solving, but the user is not getting the reserve they
    asked for — the badge must not read a bare OK."""
    opt = _optimizer()
    _stub_solvers(opt, scipy_fn=_lp_result)
    out = _solve(opt, export_floor_kwh=FLOOR_INCIDENT, export_floor_slot=2,
                 no_grid_charge=True)
    check("solve succeeded", out.get("solver") == "lp/scipy", out.get("solver"))
    check("clamped floor is reported", bool(out.get("solver_note")),
          out.get("solver_note"))
    check("the note quotes the floor it could not hold",
          "6.0" in (out.get("solver_note") or ""), out.get("solver_note"))


if __name__ == "__main__":
    for fn in [
        test_ramp_reproduces_the_incident_shortfall,
        test_ramp_clears_the_floor_once_the_hedge_cap_is_gone,
        test_ramp_respects_no_grid_charge_and_ceiling,
        test_ramp_is_monotonic_and_starts_from_e0,
        test_midday_horizon_has_no_pre_solar_window,
        test_predawn_sunny_horizon_still_suppresses_the_hedge,
        test_predawn_low_forecast_still_hedges,
        test_missing_percentile_bands_disable_the_hedge_entirely,
        test_unsatisfiable_floor_retries_without_it_instead_of_going_greedy,
        test_total_failure_falls_back_to_greedy_with_a_note,
        test_clean_solve_sets_no_note,
        test_clamped_floor_sets_a_note_even_though_the_solve_succeeded,
    ]:
        print(f"\n{fn.__name__}")
        fn()
    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)} failure(s): {FAILURES}")
        sys.exit(1)
    print("\nOK — export-floor feasibility tests passed.")
