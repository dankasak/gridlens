"""Offline tests for deferrable-load switch control (Feature 3b).

Covers the safety-critical logic of DeferrableLoadController + LoadControlManager without
HA or scipy (neither importable in this container): the on/off threshold, min-on/min-off
debounce, transition economy, hardware-drift re-assert, fail-safe command writes, and the
manager's fail-closed entitlement + leave-as-is deadman.

Run: python3 tests/test_deferrable_load_control.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
_NOW = [_T0]  # mutable "current time" the dt.now() stub reads


# ----------------------------------------------------------------- HA / dep stubs
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
    # Stub entity_lookup (pulls in homeassistant.helpers.entity_registry which we don't stub);
    # the manager only uses it to name the control switch, irrelevant to these tests.
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


# ----------------------------------------------------------------- fakes
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
        self.fail = False

    async def async_call(self, domain, service, data, blocking=False):
        if self.fail:
            raise RuntimeError("service boom")
        eid = data.get("entity_id")
        self.calls.append((domain, service, eid, data))


class FakeBus:
    def __init__(self):
        self.listeners = []

    def async_listen_once(self, event, cb):
        self.listeners.append((event, cb))


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


# ----------------------------------------------------------------- controller tests
def test_threshold():
    hass = FakeHass()
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    assert c.on_threshold_w() == 1000.0
    assert c.desired_on(1000.0) is True
    assert c.desired_on(999.9) is False
    # Absolute floor dominates for a tiny device.
    c2 = DeferrableLoadController(hass, name="Y", switch_entity_id="switch.y", max_w=40.0)
    assert c2.on_threshold_w() == 50.0


async def _run_first_tick_establishes():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    await c.apply(1500.0, _T0)  # first tick, wants on
    assert len(_turn_ons(hass)) == 1
    assert c._commanded is True


async def _run_transition_economy():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    await c.apply(1500.0, _T0)                       # -> on
    hass.states.set("switch.x", "on")               # hardware now reflects it
    await c.apply(1500.0, _T0 + timedelta(minutes=5))  # same want, no drift
    assert len(_turn_ons(hass)) == 1                # no duplicate command


async def _run_debounce_min_on():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0,
                                min_on_seconds=900, min_off_seconds=900)
    await c.apply(1500.0, _T0)                       # on at t0
    hass.states.set("switch.x", "on")
    await c.apply(0.0, _T0 + timedelta(minutes=5))   # wants off, only 5<15min held -> hold
    assert len(_turn_offs(hass)) == 0
    await c.apply(0.0, _T0 + timedelta(minutes=15))  # 15min elapsed -> off
    assert len(_turn_offs(hass)) == 1


async def _run_debounce_min_off():
    hass = FakeHass()
    hass.states.set("switch.x", "on")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    await c.apply(0.0, _T0)                           # off at t0
    hass.states.set("switch.x", "off")
    await c.apply(1500.0, _T0 + timedelta(minutes=5)) # wants on, held off <15min -> hold
    assert len(_turn_ons(hass)) == 0
    await c.apply(1500.0, _T0 + timedelta(minutes=15))
    assert len(_turn_ons(hass)) == 1


async def _run_drift_reassert():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    await c.apply(1500.0, _T0)                        # on
    assert len(_turn_ons(hass)) == 1
    hass.states.set("switch.x", "off")               # something externally turned it off
    await c.apply(1500.0, _T0 + timedelta(minutes=5))  # want==commanded(on), drift -> re-issue
    assert len(_turn_ons(hass)) == 2                 # re-asserted despite same intent


async def _run_command_error_is_safe():
    hass = FakeHass()
    hass.services.fail = True
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    await c.apply(1500.0, _T0)   # must not raise
    assert c._commanded is None  # unchanged: a failed write doesn't claim success
    assert "command_error" in c._note


# ----------------------------------------------------------------- climate-domain tests
async def _run_climate_actual_state():
    hass = FakeHass()
    c = DeferrableLoadController(hass, name="AC", switch_entity_id="climate.ac", max_w=1500.0)
    hass.states.set("climate.ac", "off")
    assert c._actual_state() is False
    hass.states.set("climate.ac", "cool")
    assert c._actual_state() is True   # any non-off hvac_mode counts as on
    hass.states.set("climate.ac", "fan_only")
    assert c._actual_state() is True
    hass.states.set("climate.ac", "unavailable")
    assert c._actual_state() is None


async def _run_climate_turn_on_off():
    # supported_features 128|256 = TURN_OFF|TURN_ON — the ECHONET/SmartIR case.
    hass = FakeHass()
    hass.states.set("climate.ac", "off", {"supported_features": 384, "hvac_modes": ["off", "cool", "heat"]})
    c = DeferrableLoadController(hass, name="AC", switch_entity_id="climate.ac", max_w=1500.0,
                                 min_on_seconds=0, min_off_seconds=0)
    await c.apply(1000.0, _T0)  # wants on
    assert hass.services.calls[-1][:3] == ("climate", "turn_on", "climate.ac")
    hass.states.set("climate.ac", "cool", {"supported_features": 384, "hvac_modes": ["off", "cool", "heat"]})
    await c.apply(0.0, _T0 + timedelta(minutes=1))  # wants off
    assert hass.services.calls[-1][:3] == ("climate", "turn_off", "climate.ac")


async def _run_climate_fallback_hvac_mode():
    # No TURN_ON/TURN_OFF feature bits — must fall back to set_hvac_mode.
    hass = FakeHass()
    hass.states.set("climate.ac2", "off",
                    {"supported_features": 1, "hvac_modes": ["off", "heat_cool", "cool"]})
    c = DeferrableLoadController(hass, name="AC2", switch_entity_id="climate.ac2", max_w=1500.0,
                                 min_on_seconds=0, min_off_seconds=0)
    await c.apply(1000.0, _T0)  # wants on -> auto-picks first non-"off" hvac_modes entry
    call = hass.services.calls[-1]
    assert call[:3] == ("climate", "set_hvac_mode", "climate.ac2")
    assert call[3]["hvac_mode"] == "heat_cool"
    hass.states.set("climate.ac2", "heat_cool",
                    {"supported_features": 1, "hvac_modes": ["off", "heat_cool", "cool"]})
    await c.apply(0.0, _T0 + timedelta(minutes=1))  # wants off
    call = hass.services.calls[-1]
    assert call[:3] == ("climate", "set_hvac_mode", "climate.ac2")
    assert call[3]["hvac_mode"] == "off"


async def _run_climate_configured_on_mode_wins():
    hass = FakeHass()
    hass.states.set("climate.ac3", "off",
                    {"supported_features": 1, "hvac_modes": ["off", "heat_cool", "cool"]})
    c = DeferrableLoadController(hass, name="AC3", switch_entity_id="climate.ac3", max_w=1500.0,
                                 min_on_seconds=0, min_off_seconds=0, climate_on_mode="cool")
    await c.apply(1000.0, _T0)
    assert hass.services.calls[-1][3]["hvac_mode"] == "cool"  # configured mode beats auto-pick


# ----------------------------------------------------------------- manager tests
def _mgr(controllable=True):
    hass = FakeHass()
    hass.states.set("switch.pool", "off")
    data = {
        "deferrable_load_sensors": ["sensor.pool"],
        "deferrable_load_max_kw": [2.0],
        "deferrable_load_switches": ["switch.pool"] if controllable else [""],
    }
    m = LoadControlManager(hass, FakeEntry(data))
    return m, hass


def _plan_on(now):
    # One interval covering "now" that wants the (2 kW) device fully on.
    return [DispatchInterval(start=now - timedelta(minutes=1), action=BatteryAction.SELF_USE,
                             deferrable_w=[2000.0])]


async def _run_fail_closed_entitlement():
    m, hass = _mgr()
    ok = await m.enable(0)                # not entitled yet
    assert ok is False
    assert m.is_enabled(0) is False
    assert hass.services.calls == []      # no writes


async def _run_enable_ticks_device():
    m, hass = _mgr()
    _NOW[0] = _T0
    m.set_plan(_plan_on(_T0), updated_at=_T0)
    await m.set_entitled(True)
    ok = await m.enable(0)
    assert ok is True and m.is_enabled(0) is True
    assert len(_turn_ons(hass)) == 1      # first enable tick turned the pool on


async def _run_revoke_leaves_as_is_then_resumes():
    m, hass = _mgr()
    _NOW[0] = _T0
    m.set_plan(_plan_on(_T0), updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    n_off_before = len(_turn_offs(hass))
    await m.set_entitled(False)           # revoke
    assert m.is_enabled(0) is False
    assert len(_turn_offs(hass)) == n_off_before  # deadman = leave as-is, never forced off
    assert m._want_enabled[0] is True     # intent persists
    await m.set_entitled(True)            # re-grant auto-resumes
    assert m.is_enabled(0) is True


async def _run_stale_plan_leaves_as_is():
    m, hass = _mgr()
    _NOW[0] = _T0
    m.set_plan(_plan_on(_T0), updated_at=_T0 - timedelta(hours=2))  # already stale
    await m.set_entitled(True)
    await m.enable(0)
    # enable's immediate tick sees a stale plan -> no actuation (leave as-is).
    assert hass.services.calls == []
    # a periodic tick with the stale plan marks degraded and still doesn't actuate.
    await m._tick(_T0)
    assert hass.services.calls == []
    assert m._degraded is True


async def _run_hass_stop_leaves_as_is():
    m, hass = _mgr()
    _NOW[0] = _T0
    m.set_plan(_plan_on(_T0), updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    n_off = len(_turn_offs(hass))
    # Fire the registered HA-stop listener.
    for event, cb in hass.bus.listeners:
        await cb(None)
    assert len(_turn_offs(hass)) == n_off  # never forces the load off
    assert m._cancel_timer is None         # timer stopped


async def _run_forecast_only_device_uncontrolled():
    m, hass = _mgr(controllable=False)
    assert m.has_controllable() is False
    assert m.controllers == {}


def _run_async(coro):
    asyncio.new_event_loop().run_until_complete(coro())


if __name__ == "__main__":
    tests = [
        ("threshold", test_threshold),
        ("first_tick_establishes", lambda: _run_async(_run_first_tick_establishes)),
        ("transition_economy", lambda: _run_async(_run_transition_economy)),
        ("debounce_min_on", lambda: _run_async(_run_debounce_min_on)),
        ("debounce_min_off", lambda: _run_async(_run_debounce_min_off)),
        ("drift_reassert", lambda: _run_async(_run_drift_reassert)),
        ("command_error_is_safe", lambda: _run_async(_run_command_error_is_safe)),
        ("climate_actual_state", lambda: _run_async(_run_climate_actual_state)),
        ("climate_turn_on_off", lambda: _run_async(_run_climate_turn_on_off)),
        ("climate_fallback_hvac_mode", lambda: _run_async(_run_climate_fallback_hvac_mode)),
        ("climate_configured_on_mode_wins", lambda: _run_async(_run_climate_configured_on_mode_wins)),
        ("fail_closed_entitlement", lambda: _run_async(_run_fail_closed_entitlement)),
        ("enable_ticks_device", lambda: _run_async(_run_enable_ticks_device)),
        ("revoke_leaves_as_is_then_resumes", lambda: _run_async(_run_revoke_leaves_as_is_then_resumes)),
        ("stale_plan_leaves_as_is", lambda: _run_async(_run_stale_plan_leaves_as_is)),
        ("hass_stop_leaves_as_is", lambda: _run_async(_run_hass_stop_leaves_as_is)),
        ("forecast_only_uncontrolled", lambda: _run_async(_run_forecast_only_device_uncontrolled)),
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
