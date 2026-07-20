#!/usr/bin/env python3
"""Tests for the solar-vs-grid charge-source split (Issue 2, the 10 kW import-spike bug).

The optimizer plan distinguishes total battery ``charge_kwh`` from grid ``buy_kwh``. A
solar-charge slot charges the battery entirely from PV surplus (grid import only covers
house + deferrable load). Executing such a slot with a Sigenergy PV-first *force_charge*
setpoint makes the inverter import from the grid whenever instantaneous PV surplus falls
below the setpoint — real grid-import spikes where the plan predicted ~0 import.

The fix threads a ``grid_charge_w`` intent through ``DispatchInterval``:
  * solar charge (grid contribution immaterial) -> self-consumption (never imports; the
    charge-rate cap is reset to hardware max so the battery absorbs ALL surplus PV)
  * material grid charge (grid a real share of the slot AND above an absolute floor)
    -> force_charge at the *grid* watts

A grid contribution is "material" only when it exceeds ``_GRID_CHARGE_MIN_W`` (250 W) AND
is at least ``_GRID_CHARGE_MIN_FRACTION`` (50%) of the slot's total charge. A tiny LP grid
nibble (e.g. 135 W on a 3.2 kW solar charge) is NOT material: force_charging at it would
cap total battery charge power at 135 W and dump the surplus PV to a $0 export.

These tests exercise the REAL executor and planner logic. Home Assistant and scipy are
not importable in this container, so their modules are stubbed and the target source
files are loaded directly by path (pure control/plan logic — no HA runtime needed).

Run:  python3 test_charge_source_split.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- paths
_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)  # .../custom_components/grid_lens
_FIXED_NOW = datetime(2026, 7, 14, 11, 0, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------- HA / dep stubs
def _install_stubs() -> None:
    """Minimal stand-ins so executor.py / planner.py import without HA or scipy."""

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    # homeassistant.core.HomeAssistant
    ha = _mod("homeassistant")
    core = _mod("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})

    def _callback(fn):  # decorator no-op
        return fn

    core.callback = _callback
    ha.core = core

    # homeassistant.helpers.event.async_track_time_change / async_call_later
    helpers = _mod("homeassistant.helpers")
    event = _mod("homeassistant.helpers.event")
    event.async_track_time_change = lambda *a, **k: (lambda: None)
    event.async_call_later = lambda *a, **k: (lambda: None)
    helpers.event = event
    ha.helpers = helpers

    # homeassistant.util.dt.now()
    util = _mod("homeassistant.util")
    dt = _mod("homeassistant.util.dt")
    dt.now = lambda: _FIXED_NOW
    util.dt = dt
    ha.util = util


def _load(path: str, fqname: str, package: str | None = None) -> types.ModuleType:
    """Load a source file as ``fqname`` (with ``package`` for relative imports)."""
    spec = importlib.util.spec_from_file_location(fqname, path)
    module = importlib.util.module_from_spec(spec)
    if package is not None:
        module.__package__ = package
    sys.modules[fqname] = module
    spec.loader.exec_module(module)
    return module


def _bootstrap():
    """Load real base.py / executor.py / planner.py under a synthetic ``gl`` package."""
    _install_stubs()

    # Synthetic package tree so relative imports (``..inverters.base`` etc.) resolve.
    for pkg in ("gl", "gl.inverters", "gl.control", "gl.advisory"):
        m = types.ModuleType(pkg)
        m.__path__ = []  # mark as package
        sys.modules[pkg] = m

    # Real base.py (stdlib-only) — gives us the genuine BatteryAction enum.
    base = _load(os.path.join(_COMPONENT, "inverters", "base.py"), "gl.inverters.base",
                 package="gl.inverters")

    # Stub battery_controller module (executor only needs the *name* at import time;
    # the tests inject a fake controller instance).
    bc_stub = types.ModuleType("gl.control.battery_controller")
    bc_stub.BatteryController = type("BatteryController", (), {})
    sys.modules["gl.control.battery_controller"] = bc_stub

    executor = _load(os.path.join(_COMPONENT, "control", "executor.py"),
                     "gl.control.executor", package="gl.control")

    # Stub planner deps that pull in scipy / models we don't need for _classify.
    opt_stub = types.ModuleType("gl.battery_optimizer")
    opt_stub.BatteryOptimizer = type("BatteryOptimizer", (), {})
    sys.modules["gl.battery_optimizer"] = opt_stub
    models_stub = types.ModuleType("gl.advisory.models")
    models_stub.AdvisoryResult = type("AdvisoryResult", (), {})
    models_stub.ForecastBundle = type("ForecastBundle", (), {})
    sys.modules["gl.advisory.models"] = models_stub

    planner = _load(os.path.join(_COMPONENT, "advisory", "planner.py"),
                    "gl.advisory.planner", package="gl.advisory")

    return base, executor, planner


BASE, EXECUTOR, PLANNER = _bootstrap()
BatteryAction = BASE.BatteryAction
DispatchInterval = EXECUTOR.DispatchInterval
ScheduleExecutor = EXECUTOR.ScheduleExecutor
AdvisoryPlanner = PLANNER.AdvisoryPlanner


# ----------------------------------------------------------------- fake controller
class FakeBatteryController:
    """Records every command the executor issues so tests can assert on them."""

    supports_battery_control = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def force_charge(self, power_w, duration=None):
        self.calls.append(("force_charge", power_w, duration))
        return True

    async def force_discharge(self, power_w, duration=None):
        self.calls.append(("force_discharge", power_w, duration))
        return True

    async def set_idle(self, duration=None):
        self.calls.append(("set_idle", duration))
        return True

    async def set_self_consumption_mode(self):
        self.calls.append(("set_self_consumption_mode",))
        return True

    async def restore_normal(self):
        self.calls.append(("restore_normal",))
        return True

    # convenience
    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _make_executor() -> tuple[ScheduleExecutor, FakeBatteryController]:
    bc = FakeBatteryController()
    ex = ScheduleExecutor(hass=object(), battery_controller=bc, interval_minutes=5)
    return ex, bc


def _tick(ex: ScheduleExecutor, at: datetime) -> None:
    asyncio.run(ex._tick(at))


# --------------------------------------------------------------------------- tests
def test_solar_charge_slot_never_grid_force_charges():
    """A solar-source charge slot (grid_charge_w == 0) must NOT issue force_charge;
    it must be executed as self-consumption so the inverter never imports to charge."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.CHARGE,
        power_w=10_000.0, grid_charge_w=0.0,  # 10 kW charge, all from solar surplus
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))

    assert "force_charge" not in bc.names(), (
        f"solar charge slot issued a grid force_charge: {bc.calls}")
    assert bc.names() == ["set_self_consumption_mode"], bc.calls


def test_grid_charge_slot_force_charges_with_grid_watts():
    """A genuine grid-charge slot must issue force_charge with the GRID watts as the
    rate cap (not the full charge rate, and not self-consumption)."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.CHARGE,
        power_w=10_000.0, grid_charge_w=6_000.0,  # 10 kW charge, 6 kW of it from grid
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))

    assert bc.names() == ["force_charge"], bc.calls
    _name, power_w, _dur = bc.calls[0]
    assert power_w == 6_000.0, f"expected grid watts (6000), got {power_w}"
    assert "set_self_consumption_mode" not in bc.names()


def test_solar_charge_transition_economy_no_respam():
    """Consecutive solar-charge ticks (resolved to self-use) must not re-spam
    self-consumption every tick (transition economy preserved)."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.CHARGE,
        power_w=8_000.0, grid_charge_w=0.0,
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))
    _tick(ex, _FIXED_NOW + timedelta(minutes=5))  # same slot, next tick

    assert bc.names().count("set_self_consumption_mode") == 1, bc.calls


def test_grid_charge_below_eps_is_solar():
    """Float-noise grid contributions (<= 1 W) are treated as solar-only."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.CHARGE,
        power_w=5_000.0, grid_charge_w=0.5,
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))
    assert "force_charge" not in bc.names(), bc.calls
    assert bc.names() == ["set_self_consumption_mode"], bc.calls


def test_immaterial_grid_nibble_on_solar_charge_is_self_consumption():
    """Regression for the live free-export bug: a predominantly-solar charge slot with a
    tiny LP grid nibble (135 W grid on a 3.24 kW charge) must run as self-consumption, NOT
    force_charge at 135 W — otherwise the ESS charge cap is pinned at 135 W and the surplus
    PV is exported for $0 while the battery sits half-empty."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.CHARGE,
        power_w=3_242.6, grid_charge_w=134.9,  # mostly solar; grid is 4% of the slot
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))
    assert "force_charge" not in bc.names(), (
        f"immaterial grid nibble issued a grid force_charge: {bc.calls}")
    assert bc.names() == ["set_self_consumption_mode"], bc.calls


def test_material_grid_above_floor_but_minority_share_is_solar():
    """A grid contribution above the absolute floor but a minority of the slot (solar is
    the majority) stays solar-only — self-consumption soaks up the larger PV surplus rather
    than capping total charge at the smaller grid figure."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.CHARGE,
        power_w=5_000.0, grid_charge_w=1_000.0,  # 1 kW grid = 20% of a 5 kW charge
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))
    assert bc.names() == ["set_self_consumption_mode"], bc.calls


def test_load_covering_discharge_slot_never_force_discharges():
    """A load-covering discharge slot (export_w == 0) must NOT issue force_discharge; it
    must be executed as self-consumption so real-time load dipping below the plan's
    slot-average discharge never spills into a $0 export."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.DISCHARGE,
        power_w=7_000.0, export_w=0.0,
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))
    assert "force_discharge" not in bc.names(), (
        f"load-covering discharge slot issued a forced discharge: {bc.calls}")
    assert bc.names() == ["set_self_consumption_mode"], bc.calls


def test_export_discharge_slot_force_discharges_with_full_power():
    """A genuine export slot must issue force_discharge at the slot's full planned rate
    (battery-first), not self-consumption."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.DISCHARGE,
        power_w=10_000.0, export_w=8_780.0,  # 10 kW discharge, most of it exported
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))
    assert bc.names() == ["force_discharge"], bc.calls
    _name, power_w, _dur = bc.calls[0]
    assert power_w == 10_000.0, f"expected the full discharge rate, got {power_w}"


def test_immaterial_export_nibble_on_load_discharge_is_self_consumption():
    """Regression for the 0c/kWh-FiT bug: a predominantly load-covering discharge slot
    with a tiny LP export nibble must run as self-consumption, NOT force_discharge at the
    slot's rate — forcing that rate ("battery first") would spill any real-time load
    shortfall into a $0 export."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.DISCHARGE,
        power_w=398.6, export_w=0.0,  # early-morning slot, purely covering house load
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))
    assert "force_discharge" not in bc.names(), (
        f"immaterial export nibble issued a forced discharge: {bc.calls}")
    assert bc.names() == ["set_self_consumption_mode"], bc.calls


def test_material_export_above_floor_but_minority_share_is_self_consumption():
    """An export contribution above the absolute floor but a minority of the slot (load
    coverage is the majority) stays self-consumption — the battery isn't forced to a rate
    when most of the discharge is destined for the house, not the grid."""
    ex, bc = _make_executor()
    slot = DispatchInterval(
        start=_FIXED_NOW, action=BatteryAction.DISCHARGE,
        power_w=5_000.0, export_w=1_000.0,  # 1 kW export = 20% of a 5 kW discharge
    )
    ex.set_plan([slot], updated_at=_FIXED_NOW)
    _tick(ex, _FIXED_NOW + timedelta(seconds=1))
    assert bc.names() == ["set_self_consumption_mode"], bc.calls


def test_stale_plan_hands_back_to_native():
    """Watchdog: a stale plan reverts to native EMS exactly once (deadman preserved)."""
    ex, bc = _make_executor()
    slot = DispatchInterval(start=_FIXED_NOW, action=BatteryAction.CHARGE,
                            power_w=5_000.0, grid_charge_w=5_000.0)
    ex.set_plan([slot], updated_at=_FIXED_NOW - timedelta(hours=2))  # older than max age
    _tick(ex, _FIXED_NOW)
    assert bc.names() == ["restore_normal"], bc.calls


# --------------------------------------------- planner split-math (real _classify)
def _planner() -> AdvisoryPlanner:
    return AdvisoryPlanner(optimizer=None)


def test_planner_solar_slot_grid_charge_zero():
    """import only covers load+deferrable -> grid_charge_w == 0 (solar-only charge)."""
    p = _planner()
    step = {
        "charge_kwh": 5.0,      # 10 kW over a 0.5 h slot
        "discharge_kwh": 0.0,
        "import_kwh": 1.0,      # <= load+deferrable, so none feeds the battery
        "load_kwh": 0.6,
        "deferrable_kwh": 0.4,
    }
    action, power_w, grid_w, export_w = p._classify(step, dt_h=0.5)
    assert action == BatteryAction.CHARGE
    assert round(power_w, 1) == 10_000.0
    assert grid_w == 0.0, grid_w
    assert export_w == 0.0, export_w


def test_planner_grid_slot_computes_grid_watts():
    """import beyond load+deferrable feeds the battery -> grid_charge_w = that, in W."""
    p = _planner()
    step = {
        "charge_kwh": 5.0,      # 10 kW total charge over 0.5 h
        "discharge_kwh": 0.0,
        "import_kwh": 4.0,      # 4 kWh import; 1 kWh covers load+deferrable
        "load_kwh": 0.7,
        "deferrable_kwh": 0.3,  # -> grid-to-battery = 3 kWh over 0.5 h = 6 kW
    }
    action, power_w, grid_w, _export_w = p._classify(step, dt_h=0.5)
    assert action == BatteryAction.CHARGE
    assert round(grid_w, 1) == 6_000.0, grid_w
    assert grid_w < power_w  # grid portion never exceeds the full charge rate


def test_planner_grid_charge_capped_at_total_charge():
    """grid-to-battery can never exceed the slot's total charge energy."""
    p = _planner()
    step = {
        "charge_kwh": 2.0,      # only 2 kWh charged this slot
        "discharge_kwh": 0.0,
        "import_kwh": 10.0,     # large import (also feeding big loads)
        "load_kwh": 1.0,
        "deferrable_kwh": 1.0,  # grid-to-battery raw = 8 kWh, but capped at charge=2 kWh
    }
    _action, power_w, grid_w, _export_w = p._classify(step, dt_h=0.5)
    assert round(grid_w, 1) == round(power_w, 1) == 4_000.0, (grid_w, power_w)


def test_planner_discharge_has_zero_grid_charge():
    p = _planner()
    step = {"charge_kwh": 0.0, "discharge_kwh": 4.0, "import_kwh": 0.0,
            "load_kwh": 0.0, "deferrable_kwh": 0.0, "export_kwh": 0.0}
    action, _power, grid_w, _export_w = p._classify(step, dt_h=0.5)
    assert action == BatteryAction.DISCHARGE
    assert grid_w == 0.0


def test_planner_load_covering_discharge_export_zero():
    """export_kwh == 0 (all discharge covers house load) -> export_w == 0."""
    p = _planner()
    step = {"charge_kwh": 0.0, "discharge_kwh": 2.0,  # 4 kW over a 0.5 h slot
            "import_kwh": 0.0, "load_kwh": 4.0, "deferrable_kwh": 0.0, "export_kwh": 0.0}
    action, power_w, _grid_w, export_w = p._classify(step, dt_h=0.5)
    assert action == BatteryAction.DISCHARGE
    assert round(power_w, 1) == 4_000.0
    assert export_w == 0.0, export_w


def test_planner_export_slot_computes_export_watts():
    """export_kwh feeds the grid -> export_w = that, in W."""
    p = _planner()
    step = {"charge_kwh": 0.0, "discharge_kwh": 5.0,  # 10 kW total discharge over 0.5 h
             "import_kwh": 0.0, "load_kwh": 1.0, "deferrable_kwh": 0.0,
             "export_kwh": 4.0}  # 4 kWh sold -> 8 kW
    action, power_w, _grid_w, export_w = p._classify(step, dt_h=0.5)
    assert action == BatteryAction.DISCHARGE
    assert round(export_w, 1) == 8_000.0, export_w
    assert export_w < power_w  # export portion never exceeds the full discharge rate


def test_planner_export_capped_at_total_discharge():
    """export-to-grid can never exceed the slot's total discharge energy."""
    p = _planner()
    step = {"charge_kwh": 0.0, "discharge_kwh": 1.0,  # only 1 kWh discharged this slot
            "import_kwh": 0.0, "load_kwh": 0.0, "deferrable_kwh": 0.0,
            "export_kwh": 3.0}  # export figure larger than the discharge itself
    _action, power_w, _grid_w, export_w = p._classify(step, dt_h=0.5)
    assert round(export_w, 1) == round(power_w, 1) == 2_000.0, (export_w, power_w)


# ------------------------------------------ soft terminal-SOC valuation (Defect 2)
class _Bundle:
    def __init__(self, export_rate):
        self.export_rate = export_rate


def test_terminal_soc_value_is_mean_export_rate():
    """Terminal energy is valued at the horizon's mean export rate — a conservative
    'you could have sold it' price that softens the hard terminal-SOC floor."""
    # 0.45 in the 2h window, 0.05 otherwise, over a short horizon.
    exp = [0.05, 0.05, 0.45, 0.45, 0.05, 0.05]
    v = AdvisoryPlanner._terminal_soc_value(_Bundle(exp))
    assert abs(v - sum(exp) / len(exp)) < 1e-12
    # Must sit well below the grid-charge break-even (~import/eta ~ 0.35) so it can
    # never make buying grid to bank terminal energy worthwhile.
    assert v < 0.34


def test_terminal_soc_value_empty_is_zero():
    assert AdvisoryPlanner._terminal_soc_value(_Bundle([])) == 0.0


def test_terminal_soc_value_ignores_negative_rates():
    """Negative export rates (curtailment) are floored at 0 before averaging."""
    v = AdvisoryPlanner._terminal_soc_value(_Bundle([-0.1, 0.2, 0.2]))
    assert abs(v - (0.0 + 0.2 + 0.2) / 3) < 1e-12


# --------------------------------------------------------------------------- runner
def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {err}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
