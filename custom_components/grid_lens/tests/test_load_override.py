"""Offline tests for the manual deferrable-load override (Force On / Force Off / Auto).

Covers the override state machine in DeferrableLoadController + LoadControlManager
without HA (stubbed, same pattern as test_deferrable_load_control.py): a force commands
the hardware immediately (no debounce — it's a direct user action), fully suspends
plan-driven control INCLUDING drift re-asserts (a human at the physical switch wins),
works regardless of the device's enable switch or entitlement, restores across restart
without touching hardware (actuate=False), and clearing back to Auto re-establishes the
planned state immediately.

Run: python3 tests/test_load_override.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_T0 = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
_NOW = [_T0]


def _install_stubs() -> None:
    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    ha = _mod("homeassistant")
    core = _mod("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda fn: fn
    ha.core = core

    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = type("ConfigEntry", (), {})
    ha.config_entries = ce

    const = _mod("homeassistant.const")
    const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    ha.const = const

    helpers = _mod("homeassistant.helpers")
    event = _mod("homeassistant.helpers.event")
    event.async_track_time_change = lambda *a, **k: (lambda: None)
    helpers.event = event
    ha.helpers = helpers

    util = _mod("homeassistant.util")
    dt = _mod("homeassistant.util.dt")
    dt.now = lambda: _NOW[0]
    util.dt = dt
    ha.util = util


def _load(path: str, fqname: str, package: str | None = None) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(fqname, path)
    module = importlib.util.module_from_spec(spec)
    if package is not None:
        module.__package__ = package
    sys.modules[fqname] = module
    spec.loader.exec_module(module)
    return module


def _bootstrap():
    _install_stubs()
    for pkg in ("gl", "gl.inverters", "gl.control"):
        m = types.ModuleType(pkg)
        m.__path__ = []
        sys.modules[pkg] = m
    _load(os.path.join(_COMPONENT, "inverters", "base.py"), "gl.inverters.base",
          package="gl.inverters")
    bc_stub = types.ModuleType("gl.control.battery_controller")
    bc_stub.BatteryController = type("BatteryController", (), {})
    sys.modules["gl.control.battery_controller"] = bc_stub
    _load(os.path.join(_COMPONENT, "control", "executor.py"),
          "gl.control.executor", package="gl.control")
    _load(os.path.join(_COMPONENT, "const.py"), "gl.const", package="gl")
    el_stub = types.ModuleType("gl.entity_lookup")
    el_stub.resolve_device_name = lambda hass, *anchors: next((a for a in anchors if a), None)
    el_stub.resolve_power_sensor = lambda hass, *anchors: None
    sys.modules["gl.entity_lookup"] = el_stub
    lc = _load(os.path.join(_COMPONENT, "control", "load_controller.py"),
               "gl.control.load_controller", package="gl.control")
    lcm = _load(os.path.join(_COMPONENT, "control", "load_control_manager.py"),
                "gl.control.load_control_manager", package="gl.control")
    ex = sys.modules["gl.control.executor"]
    return lc.DeferrableLoadController, lcm.LoadControlManager, ex.DispatchInterval


DeferrableLoadController, LoadControlManager, DispatchInterval = _bootstrap()
from gl.inverters.base import BatteryAction  # noqa: E402  (loaded above)


class FakeState:
    def __init__(self, state, attrs=None):
        self.state = state
        self.attributes = attrs or {}


class FakeStates:
    def __init__(self):
        self._d = {}

    def get(self, eid):
        return self._d.get(eid)

    def set(self, eid, state, attrs=None):
        self._d[eid] = FakeState(state, attrs)


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data.get("entity_id")))


class FakeBus:
    def async_listen_once(self, event, cb):
        pass


class FakeHass:
    def __init__(self):
        self.states = FakeStates()
        self.services = FakeServices()
        self.bus = FakeBus()


class FakeEntry:
    def __init__(self, data):
        self.data = data
        self.entry_id = "e1"


def _turn_ons(hass):
    return [c for c in hass.services.calls if c[1] == "turn_on"]


def _turn_offs(hass):
    return [c for c in hass.services.calls if c[1] == "turn_off"]


def _mgr():
    hass = FakeHass()
    hass.states.set("switch.pool", "off")
    data = {
        "deferrable_load_sensors": ["sensor.pool"],
        "deferrable_load_max_kw": [2.0],
        "deferrable_load_switches": ["switch.pool"],
    }
    return LoadControlManager(hass, FakeEntry(data)), hass


def _plan_on(now):
    return [DispatchInterval(start=now - timedelta(minutes=1), action=BatteryAction.SELF_USE,
                             deferrable_w=[2000.0])]


# ------------------------------------------------------------ controller-level
async def _run_force_off_commands_once_and_suspends():
    hass = FakeHass()
    hass.states.set("switch.x", "on")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    await c.apply(1500.0, _T0)                        # plan establishes ON
    assert c._commanded is True
    await c.set_override(False, _T0 + timedelta(minutes=1))   # instant off
    assert len(_turn_offs(hass)) == 1                 # commanded immediately, no debounce
    # Plan still says full power — apply must NOT flip it back on.
    await c.apply(2000.0, _T0 + timedelta(minutes=6))
    await c.apply(2000.0, _T0 + timedelta(minutes=30))
    assert len(_turn_ons(hass)) == 1                  # only the original establish
    assert c.status()["override"] == "off"
    assert c.status()["note"] == "override_off"


async def _run_force_on_ignores_debounce():
    hass = FakeHass()
    hass.states.set("switch.x", "on")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0,
                                 min_on_seconds=900, min_off_seconds=900)
    await c.apply(0.0, _T0)                           # establishes OFF
    assert len(_turn_offs(hass)) == 1
    # 1 minute later — a plan-driven ON would be debounced (min_off 15m); a force isn't.
    await c.set_override(True, _T0 + timedelta(minutes=1))
    assert len(_turn_ons(hass)) == 1


async def _run_no_drift_reassert_during_override():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    await c.set_override(True, _T0)                   # forced on
    assert len(_turn_ons(hass)) == 1
    hass.states.set("switch.x", "off")                # human turns it off at the wall
    await c.apply(2000.0, _T0 + timedelta(minutes=5))
    assert len(_turn_ons(hass)) == 1                  # NOT re-asserted — human wins


async def _run_restore_reestablishes_immediately():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0,
                                 min_on_seconds=900, min_off_seconds=900)
    await c.apply(2000.0, _T0)                        # establishes ON
    await c.set_override(False, _T0 + timedelta(minutes=1))   # instant off
    await c.set_override(None, _T0 + timedelta(minutes=2))    # restore control
    assert c.status()["override"] == "auto"
    # Next apply re-establishes from the plan via the first-tick path — no debounce wait.
    await c.apply(2000.0, _T0 + timedelta(minutes=2))
    assert len(_turn_ons(hass)) == 2


async def _run_restore_persisted_without_actuation():
    hass = FakeHass()
    hass.states.set("switch.x", "on")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    await c.set_override(False, _T0, actuate=False)   # restart path: re-arm only
    assert hass.services.calls == []                  # leave-as-is: zero writes
    assert c.status()["override"] == "off"
    await c.apply(2000.0, _T0 + timedelta(minutes=5))
    assert hass.services.calls == []                  # still suspended


# ------------------------------------------------------------ manager-level
async def _run_manager_force_works_when_disabled_and_unentitled():
    m, hass = _mgr()
    assert m.is_enabled(0) is False                   # enable switch off, no entitlement
    ok = await m.set_override(0, "off")
    assert ok is True
    assert len(_turn_offs(hass)) == 1                 # a manual command needs neither
    assert m.get_override(0) == "off"
    assert (await m.set_override(0, "on")) is True
    assert len(_turn_ons(hass)) == 1


async def _run_manager_restore_ticks_enabled_device():
    m, hass = _mgr()
    await m.set_entitled(True)
    await m.enable(0)
    m.set_plan(_plan_on(_NOW[0]))
    await m._tick(_NOW[0])                            # plan establishes ON
    n_on = len(_turn_ons(hass))
    await m.set_override(0, "off")                    # user: instant off
    assert len(_turn_offs(hass)) >= 1
    await m.set_override(0, None)                     # restore control
    assert m.get_override(0) is None
    # The restore itself re-ticked the device: plan wants ON, so it's back on already.
    assert len(_turn_ons(hass)) == n_on + 1


async def _run_manager_unknown_index():
    m, _hass = _mgr()
    assert (await m.set_override(5, "off")) is False
    assert m.get_override(5) is None


def _run_async(coro):
    asyncio.new_event_loop().run_until_complete(coro())


if __name__ == "__main__":
    tests = [
        ("force_off_commands_once_and_suspends", lambda: _run_async(_run_force_off_commands_once_and_suspends)),
        ("force_on_ignores_debounce", lambda: _run_async(_run_force_on_ignores_debounce)),
        ("no_drift_reassert_during_override", lambda: _run_async(_run_no_drift_reassert_during_override)),
        ("restore_reestablishes_immediately", lambda: _run_async(_run_restore_reestablishes_immediately)),
        ("restore_persisted_without_actuation", lambda: _run_async(_run_restore_persisted_without_actuation)),
        ("manager_force_works_when_disabled_and_unentitled", lambda: _run_async(_run_manager_force_works_when_disabled_and_unentitled)),
        ("manager_restore_ticks_enabled_device", lambda: _run_async(_run_manager_restore_ticks_enabled_device)),
        ("manager_unknown_index", lambda: _run_async(_run_manager_unknown_index)),
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
