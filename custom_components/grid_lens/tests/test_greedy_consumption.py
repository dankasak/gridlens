"""Offline tests for deferrable-load Greedy Consumption.

Covers the real-time override on top of plan-driven load control: turn a device on when
import price is free, or when export is being wasted (export price free + household
exporting at least as much as the device draws) — subject to the device's own Greedy
Consumption / Greedy Respects Schedule toggles, always suppressed under a manual
override, and folded into the same debounce as a normal plan-driven transition. No HA or
scipy needed (neither importable in this container).

Run: python3 tests/test_greedy_consumption.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_T0 = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)  # Friday
_NOW = [_T0]  # mutable "current time" the dt.now()/as_local() stubs read


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
    dt.as_local = lambda d: d  # tests use naive local-equivalent datetimes directly
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
    _load(os.path.join(_COMPONENT, "schedule_grid.py"), "gl.schedule_grid", package="gl")
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
    const = sys.modules["gl.const"]
    return (lc.DeferrableLoadController, lcm.LoadControlManager, ex.DispatchInterval,
            const.DOMAIN)


DeferrableLoadController, LoadControlManager, DispatchInterval, DOMAIN = _bootstrap()
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
        self.calls.append((domain, service, eid))


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
        self.data = {}


class FakeEntry:
    def __init__(self, data):
        self.data = data
        self.entry_id = "e1"


class FakeScheduleStore:
    """Stand-in for DeferrableScheduleStore: async_get(sensor_id) -> stored week or None."""

    def __init__(self, weeks: dict | None = None):
        self._weeks = weeks or {}

    async def async_get(self, sensor_id: str):
        return self._weeks.get(sensor_id)


def _turn_ons(hass):
    return [c for c in hass.services.calls if c[1] == "turn_on"]


def _turn_offs(hass):
    return [c for c in hass.services.calls if c[1] == "turn_off"]


def _run_async(coro):
    asyncio.new_event_loop().run_until_complete(coro())


# ----------------------------------------------------------------- controller-level tests
async def _run_greedy_import_free_turns_on():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    # Plan wants it OFF (planned_w=0), but import is free -> greedy turns it on anyway.
    await c.apply(0.0, _T0, import_rate=0.0)
    assert len(_turn_ons(hass)) == 1
    assert c._note.endswith("_greedy")


async def _run_greedy_export_surplus_turns_on():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    # Exporting 3000W (>= 2000W device draw) at a $0 export price -> free to run.
    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.0, grid_power_w=-3000.0)
    assert len(_turn_ons(hass)) == 1


async def _run_greedy_export_insufficient_no_effect():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    # Only exporting 1000W (< 2000W device draw) -> would create new import, no greedy.
    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.0, grid_power_w=-1000.0)
    assert len(_turn_ons(hass)) == 0


async def _run_greedy_disabled_no_effect():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    # Greedy never enabled (default False) -> conditions are true but no effect.
    await c.apply(0.0, _T0, import_rate=0.0, export_rate=0.0, grid_power_w=-5000.0)
    assert len(_turn_ons(hass)) == 0


async def _run_greedy_none_inputs_fail_closed():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    await c.apply(0.0, _T0)  # all rate/power inputs default None
    assert len(_turn_ons(hass)) == 0


async def _run_greedy_respects_schedule_suppresses():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_respects_schedule(True)
    # Free import, but the schedule says this device isn't allowed to run right now.
    await c.apply(0.0, _T0, import_rate=0.0, schedule_allows=False)
    assert len(_turn_ons(hass)) == 0


async def _run_greedy_ignores_schedule_when_flag_off():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    # greedy_respects_schedule left False (default) -> schedule_allows=False is ignored.
    await c.apply(0.0, _T0, import_rate=0.0, schedule_allows=False)
    assert len(_turn_ons(hass)) == 1


async def _run_override_suppresses_greedy():
    hass = FakeHass()
    hass.states.set("switch.x", "on")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    await c.set_override(False, _T0)  # Force Off
    n_before = len(hass.services.calls)
    await c.apply(0.0, _T0 + timedelta(minutes=1), import_rate=0.0)
    assert len(hass.services.calls) == n_before  # greedy never evaluated under override


async def _run_greedy_honours_debounce():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0,
                                  min_on_seconds=900, min_off_seconds=900)
    await c.apply(0.0, _T0)  # establish off at t0 (first tick)
    c.set_greedy(True)
    # Free import shows up 5 min later -> wants on, but min_off (15min) not yet elapsed.
    await c.apply(0.0, _T0 + timedelta(minutes=5), import_rate=0.0)
    assert len(_turn_ons(hass)) == 0
    assert "hold" in c._note
    await c.apply(0.0, _T0 + timedelta(minutes=15), import_rate=0.0)
    assert len(_turn_ons(hass)) == 1


def test_status_reports_greedy_state():
    hass = FakeHass()
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    assert c.status()["greedy"] is False
    assert c.status()["greedy_respects_schedule"] is False
    c.set_greedy(True)
    c.set_greedy_respects_schedule(True)
    assert c.status()["greedy"] is True
    assert c.status()["greedy_respects_schedule"] is True


# ----------------------------------------------------------------- manager-level tests
def _mgr(extra_data=None, grid_power_sensor=""):
    hass = FakeHass()
    hass.states.set("switch.pool", "off")
    data = {
        "deferrable_load_sensors": ["sensor.pool"],
        "deferrable_load_max_kw": [2.0],
        "deferrable_load_switches": ["switch.pool"],
        "deferrable_load_hours": ["9-17"],
    }
    if grid_power_sensor:
        data["grid_power_sensor"] = grid_power_sensor
    if extra_data:
        data.update(extra_data)
    m = LoadControlManager(hass, FakeEntry(data))
    return m, hass


def test_manager_set_greedy_roundtrip():
    m, _hass = _mgr()

    async def go():
        assert m.is_greedy(0) is False
        ok = await m.set_greedy(0, True)
        assert ok is True
        assert m.is_greedy(0) is True
        assert m.is_greedy_respects_schedule(0) is False
        ok2 = await m.set_greedy_respects_schedule(0, True)
        assert ok2 is True
        assert m.is_greedy_respects_schedule(0) is True
        # Unknown index fails harmlessly.
        assert await m.set_greedy(99, True) is False
        assert m.is_greedy(99) is False

    _run_async(go)


def test_manager_reads_grid_power_sensor():
    m, hass = _mgr(grid_power_sensor="sensor.grid_power")
    assert m._read_grid_power_w() is None  # not set yet
    hass.states.set("sensor.grid_power", "-3200.5")
    assert m._read_grid_power_w() == -3200.5
    hass.states.set("sensor.grid_power", "unavailable")
    assert m._read_grid_power_w() is None
    hass.states.set("sensor.grid_power", "not_a_number")
    assert m._read_grid_power_w() is None


def test_manager_no_grid_power_sensor_configured():
    m, _hass = _mgr()  # grid_power_sensor left unset
    assert m._read_grid_power_w() is None


def test_manager_schedule_static_fallback():
    # "9-17" -> allowed hours 9..16 inclusive (end-exclusive per parse_hours_spec).
    m, _hass = _mgr()

    async def go():
        # _T0 is Friday 12:00 UTC-as-local -> inside 9-17.
        assert await m._schedule_allows_now(0, _T0) is True
        # 20:00 same day -> outside 9-17.
        assert await m._schedule_allows_now(0, _T0.replace(hour=20)) is False

    _run_async(go)


def test_manager_schedule_store_overrides_static_fallback():
    # Static fallback would disallow 20:00, but a stored weekly grid (all-hours-on)
    # takes priority over it.
    week_all_on = [[1] * 48 for _ in range(7)]
    m, hass = _mgr()
    hass.data[DOMAIN] = {f"{m.entry.entry_id}_deferrable_schedules":
                         FakeScheduleStore({"sensor.pool": week_all_on})}

    async def go():
        assert await m._schedule_allows_now(0, _T0.replace(hour=20)) is True

    _run_async(go)


async def _run_manager_end_to_end_greedy_tick():
    """Full integration: a device the LP wants OFF still gets switched on by a live
    free-import-price tick, once entitled/enabled/greedy are all set."""
    m, hass = _mgr()
    _NOW[0] = _T0
    plan = [DispatchInterval(start=_T0 - timedelta(minutes=1), action=BatteryAction.SELF_USE,
                              deferrable_w=[0.0], import_rate=0.0, export_rate=0.6)]
    m.set_plan(plan, updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)  # first enable tick: plan wants off, no greedy yet -> no turn_on
    assert len(_turn_ons(hass)) == 0
    await m.set_greedy(0, True)
    # Past the default 15-min min-off debounce, so the greedy-triggered "on" isn't held.
    later = _T0 + timedelta(minutes=16)
    await m._tick_device(0, later)
    assert len(_turn_ons(hass)) == 1


if __name__ == "__main__":
    tests = [
        ("greedy_import_free_turns_on", lambda: _run_async(_run_greedy_import_free_turns_on)),
        ("greedy_export_surplus_turns_on", lambda: _run_async(_run_greedy_export_surplus_turns_on)),
        ("greedy_export_insufficient_no_effect", lambda: _run_async(_run_greedy_export_insufficient_no_effect)),
        ("greedy_disabled_no_effect", lambda: _run_async(_run_greedy_disabled_no_effect)),
        ("greedy_none_inputs_fail_closed", lambda: _run_async(_run_greedy_none_inputs_fail_closed)),
        ("greedy_respects_schedule_suppresses", lambda: _run_async(_run_greedy_respects_schedule_suppresses)),
        ("greedy_ignores_schedule_when_flag_off", lambda: _run_async(_run_greedy_ignores_schedule_when_flag_off)),
        ("override_suppresses_greedy", lambda: _run_async(_run_override_suppresses_greedy)),
        ("greedy_honours_debounce", lambda: _run_async(_run_greedy_honours_debounce)),
        ("status_reports_greedy_state", test_status_reports_greedy_state),
        ("manager_set_greedy_roundtrip", test_manager_set_greedy_roundtrip),
        ("manager_reads_grid_power_sensor", test_manager_reads_grid_power_sensor),
        ("manager_no_grid_power_sensor_configured", test_manager_no_grid_power_sensor_configured),
        ("manager_schedule_static_fallback", test_manager_schedule_static_fallback),
        ("manager_schedule_store_overrides_static_fallback", test_manager_schedule_store_overrides_static_fallback),
        ("manager_end_to_end_greedy_tick", lambda: _run_async(_run_manager_end_to_end_greedy_tick)),
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
