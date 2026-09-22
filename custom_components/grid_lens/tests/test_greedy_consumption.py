"""Offline tests for deferrable-load Greedy Consumption.

Covers the real-time override on top of plan-driven load control: turn a device on when
import price is free, or when export is being wasted (export price at or below the user's
Minimum Export Price — $0 when that setting is disabled — plus the household exporting at
least as much as the device draws) — subject to the device's own Greedy Consumption /
Greedy Respects Schedule toggles, always suppressed under a manual override, and folded
into the same debounce as a normal plan-driven transition. No HA or scipy needed (neither
importable in this container).

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
    # Real dispatch_realism.py — executor.py imports its threshold constants.
    _load(os.path.join(_COMPONENT, "dispatch_realism.py"), "gl.dispatch_realism")
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
    # Stub runtime_settings (pulls in homeassistant.helpers.entity_registry, unstubbed).
    # _min_export_price() imports get_live_number from it lazily; returning the passed
    # default reproduces "no number entity registered yet" — the value then comes from
    # entry.data[min_export_price] (0.0 unless a test sets it).
    rs_stub = types.ModuleType("gl.runtime_settings")
    rs_stub.get_live_number = lambda hass, entry_id, suffix, default: default
    sys.modules["gl.runtime_settings"] = rs_stub
    lc = _load(os.path.join(_COMPONENT, "control", "load_controller.py"),
               "gl.control.load_controller", package="gl.control")
    lcm = _load(os.path.join(_COMPONENT, "control", "load_control_manager.py"),
                "gl.control.load_control_manager", package="gl.control")
    ex = sys.modules["gl.control.executor"]
    const = sys.modules["gl.const"]
    return (lc.DeferrableLoadController, lcm.LoadControlManager, ex.DispatchInterval,
            const.DOMAIN, lcm.GREEDY_SURPLUS_LOOKAHEAD_HOURS)


DeferrableLoadController, LoadControlManager, DispatchInterval, DOMAIN, LOOKAHEAD_H = _bootstrap()
_LOOKAHEAD_SLOTS = int(round(LOOKAHEAD_H * 2))  # 30-min slots that exactly fill the window
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


async def _run_greedy_export_below_floor_turns_on():
    """Condition #2's price bar is the user's Minimum Export Price, not a hard $0
    (2026-09-11). Exporting 3 kW (>= 2 kW draw) at 3c while the floor is 5c -> that
    export isn't worth selling to this user, so greedy soaks it."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.03, grid_power_w=-3000.0,
                  min_export_price=0.05)
    assert len(_turn_ons(hass)) == 1
    assert c.status()["greedy_reason"] == "export_surplus"


async def _run_greedy_export_below_floor_disabled_by_default():
    """Floor at its 0 default -> the condition is exactly the old `export_rate <= $0`,
    so a priced 3c export does nothing even with plenty of spill."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.03, grid_power_w=-9000.0)
    assert len(_turn_ons(hass)) == 0


async def _run_greedy_export_above_floor_no_effect():
    """Export ABOVE the floor (8c > 5c) is worth selling -> no greedy soak."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.08, grid_power_w=-9000.0,
                  min_export_price=0.05)
    assert len(_turn_ons(hass)) == 0


async def _run_greedy_below_floor_still_needs_power_cover():
    """The price bar widened; the POWER safety check did not. Only 1 kW of export vs a
    2 kW draw would still create real import, so the below-floor rate can't fire it."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.03, grid_power_w=-1000.0,
                  min_export_price=0.05)
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
    assert c.status()["greedy_forecast_surplus"] is False
    c.set_greedy(True)
    c.set_greedy_respects_schedule(True)
    c.set_greedy_forecast_surplus(True)
    assert c.status()["greedy"] is True
    assert c.status()["greedy_respects_schedule"] is True
    assert c.status()["greedy_forecast_surplus"] is True


# --------------------------------------------------- forecast-surplus (controller level)
async def _run_surplus_turns_on_when_rate_covers_draw():
    """Proportional forecast-surplus (2026-09-11). Nothing is free right now (import
    priced, export priced, importing), but the plan wastes 10 kWh over the 4 h budget
    window = a 2.5 kW average rate, which clears an on/off 2 kW device's all-or-nothing
    bar. Battery can supply the rate now (2 kW free) and absorb the whole 10 kWh budget
    (12 kWh to min SOC), so it runs fully on."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=10.0, forecast_hours=4.0,
                  battery_headroom_w=2000.0, battery_headroom_kwh=12.0)
    assert len(_turn_ons(hass)) == 1
    assert c._note.endswith("_greedy")
    assert c.status()["forecast_target_w"] == 2000.0


async def _run_surplus_needs_battery_headroom():
    """The spill clearing the rate bar is necessary but not sufficient — without a battery
    behind it, firing is just unbuffered grid import. Three ways the battery gate fails:
    no headroom known at all; the discharge *rate* can't reach the device's draw; the
    energy to min SOC can't cover the whole (possibly back-loaded) budget."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    # (a) no battery headroom known at all (no battery / unreadable sensors).
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=10.0, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0
    assert c.status()["greedy_blocked"] == "no_battery_headroom"
    # (b) plenty of energy to min SOC, but the live discharge rate can't reach 2 kW.
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=10.0, forecast_hours=4.0,
                  battery_headroom_w=500.0, battery_headroom_kwh=12.0)
    assert len(_turn_ons(hass)) == 0
    assert c.status()["greedy_blocked"] == "no_battery_headroom"
    # (c) rate is fine, but only 5 kWh to min SOC vs a 10 kWh budget that could all land
    # at the far end of the window -> the transient dip would breach min SOC.
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=10.0, forecast_hours=4.0,
                  battery_headroom_w=3000.0, battery_headroom_kwh=5.0)
    assert len(_turn_ons(hass)) == 0
    assert c.status()["greedy_blocked"] == "no_battery_headroom"


async def _run_surplus_ac_output_headroom_clamps_and_blocks():
    """CONF_MAX_AC_OUTPUT_KW's live headroom (LoadControlManager._ac_output_headroom_w)
    is a third, OPTIONAL gate alongside the two battery ones — None (no ceiling
    configured, the default) is a pure no-op, unlike a missing battery headroom which
    always blocks. A fresh FakeHass per case: _turn_ons() counts cumulative calls, and
    each case's outcome must be judged in isolation."""
    # ac_output_headroom_w=None (unconfigured) behaves exactly like before this feature
    # existed — battery headroom alone decides.
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=10.0, forecast_hours=4.0,
                  battery_headroom_w=2000.0, battery_headroom_kwh=12.0,
                  ac_output_headroom_w=None)
    assert len(_turn_ons(hass)) == 1
    assert c.status()["forecast_target_w"] == 2000.0

    # Battery headroom would allow the full 2 kW, but the plant's own AC output ceiling
    # (found 2026-09-12: PV alone was already at the inverter's rating) leaves nothing ->
    # blocked, distinctly from a battery-headroom block.
    hass2 = FakeHass()
    hass2.states.set("switch.x", "off")
    c2 = DeferrableLoadController(hass2, name="X2", switch_entity_id="switch.x", max_w=2000.0)
    c2.set_greedy(True)
    c2.set_greedy_forecast_surplus(True)
    await c2.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                   forecast_spill_kwh=10.0, forecast_hours=4.0,
                   battery_headroom_w=2000.0, battery_headroom_kwh=12.0,
                   ac_output_headroom_w=0.0)
    assert len(_turn_ons(hass2)) == 0
    assert c2.status()["greedy_blocked"] == "no_ac_output_headroom"

    # AC headroom exactly clearing this on/off device's all-or-nothing bar -> still fires
    # (an on/off load has no partial state to clamp INTO — see
    # test_modulating_load_control.py's _run_forecast_surplus_pins_to_battery_safe_rate
    # for the proportional case a modulating load gets instead).
    hass3 = FakeHass()
    hass3.states.set("switch.x", "off")
    c3 = DeferrableLoadController(hass3, name="X3", switch_entity_id="switch.x", max_w=2000.0)
    c3.set_greedy(True)
    c3.set_greedy_forecast_surplus(True)
    await c3.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                   forecast_spill_kwh=10.0, forecast_hours=4.0,
                   battery_headroom_w=2000.0, battery_headroom_kwh=12.0,
                   ac_output_headroom_w=2000.0)
    assert len(_turn_ons(hass3)) == 1
    assert c3.status()["greedy_blocked"] is None
    assert c3.status()["forecast_ac_output_headroom_w"] == 2000.0


async def _run_surplus_insufficient_no_effect():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    # 7 kWh over the 4 h window = 1.75 kW average, below the on/off device's 2 kW bar ->
    # the spill itself isn't enough; no fire and (unlike a battery limit) no block reason.
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=7.0, forecast_hours=4.0,
                  battery_headroom_w=5000.0, battery_headroom_kwh=20.0)
    assert len(_turn_ons(hass)) == 0
    assert c.status()["greedy_blocked"] is None


async def _run_surplus_needs_its_own_toggle():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)  # master greedy on, forecast-surplus left OFF (default)
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=100.0, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0


async def _run_surplus_needs_master_greedy():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy_forecast_surplus(True)  # but master greedy stays OFF
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=100.0, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0


async def _run_surplus_missing_forecast_fails_closed():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    await c.apply(0.0, _T0, import_rate=0.35, forecast_spill_kwh=None, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0
    # A covered span of 0 h can't justify anything either (bar would be 0 kWh).
    await c.apply(0.0, _T0, import_rate=0.35, forecast_spill_kwh=5.0, forecast_hours=0.0)
    assert len(_turn_ons(hass)) == 0


async def _run_surplus_respects_schedule():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    c.set_greedy_respects_schedule(True)
    await c.apply(0.0, _T0, import_rate=0.35, schedule_allows=False,
                  forecast_spill_kwh=100.0, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0


async def _run_status_reports_greedy_reason():
    """status() names WHICH condition fired — the whole point of the observability
    layer, since a greedy "on" is otherwise indistinguishable from a plan-driven one."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0,
                                 min_on_seconds=0, min_off_seconds=0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    assert c.status()["greedy_reason"] is None

    await c.apply(0.0, _T0, import_rate=0.0)
    assert c.status()["greedy_reason"] == "import_free"

    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.0, grid_power_w=-3000.0)
    assert c.status()["greedy_reason"] == "export_surplus"

    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=10.0, forecast_hours=4.0,
                  battery_headroom_w=2000.0, battery_headroom_kwh=12.0)
    st = c.status()
    assert st["greedy_reason"] == "forecast_surplus"
    assert st["forecast_free_kwh"] == 10.0
    assert st["forecast_needed_kwh"] == 8.0
    assert st["forecast_target_w"] == 2000.0
    assert st["forecast_battery_headroom_w"] == 2000.0
    assert st["forecast_battery_headroom_kwh"] == 12.0

    # Nothing free and nothing forecast -> reason clears, but the figures still publish
    # so the UI can show progress toward the bar.
    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.05, grid_power_w=500.0,
                  forecast_spill_kwh=3.0, forecast_hours=4.0)
    st = c.status()
    assert st["greedy_reason"] is None
    assert st["greedy_blocked"] is None
    assert st["forecast_free_kwh"] == 3.0


async def _run_greedy_reason_cleared_when_plan_already_wants_on():
    """Regression (2026-08-31): a device the plan alone would have run this slot must not
    get its consumption tagged greedy just because a greedy condition also happened to
    match — e.g. an EV charger already inside its scheduled plan window, that would have
    charged at this rate with or without any spare solar. Before the fix, `greedy_reason`
    (and so GreedyEnergyTracker's attribution, and the "_greedy" note tag) was set purely
    from whether a condition matched, with no check that the plan was going to run the
    device regardless."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0,
                                 min_on_seconds=0, min_off_seconds=0)
    c.set_greedy(True)

    # Plan alone already wants this device on (planned_w clears the on-threshold) AND
    # import is free -> greedy condition #1 matches too, but isn't why it's running.
    await c.apply(2000.0, _T0, import_rate=0.0)
    st = c.status()
    assert st["commanded"] == "on"
    assert st["greedy_reason"] is None
    assert "_greedy" not in st["note"]

    # Same free-import condition, but now the plan does NOT want it on -> greedy really is
    # the reason, and must still be reported (the fix must not over-suppress).
    c2 = DeferrableLoadController(hass, name="Y", switch_entity_id="switch.x", max_w=2000.0,
                                  min_on_seconds=0, min_off_seconds=0)
    c2.set_greedy(True)
    await c2.apply(0.0, _T0, import_rate=0.0)
    st2 = c2.status()
    assert st2["commanded"] == "on"
    assert st2["greedy_reason"] == "import_free"
    assert "_greedy" in st2["note"]


async def _run_status_reports_greedy_blocked():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0,
                                 min_on_seconds=0, min_off_seconds=0)
    c.set_greedy(True)
    c.set_greedy_respects_schedule(True)
    await c.apply(0.0, _T0, import_rate=0.0, schedule_allows=False)
    assert c.status()["greedy_blocked"] == "schedule"
    assert c.status()["greedy_reason"] is None
    # Back inside the window -> blocked clears and the reason appears.
    await c.apply(0.0, _T0, import_rate=0.0, schedule_allows=True)
    assert c.status()["greedy_blocked"] is None
    assert c.status()["greedy_reason"] == "import_free"
    # A manual override stops greedy being evaluated at all — the published reason must
    # not go stale and keep claiming greedy is why the device is on.
    await c.set_override(True, _T0)
    await c.apply(0.0, _T0, import_rate=0.0)
    assert c.status()["greedy_reason"] is None
    assert c.status()["greedy_blocked"] == "override"


async def _run_status_reports_no_grid_power_block():
    """The regression this exists for (2026-08-28): the house was spilling ~5 kW at a $0
    export price with a 1.9 kW EV charger sitting off, because no grid power sensor was
    configured — so greedy's export-surplus condition could never be judged. Nothing
    recorded that, and the card reported "armed, waiting for free energy", which reads as
    "no surplus yet" rather than "cannot see one". The block reason is the only signal
    distinguishing the two."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0,
                                 min_on_seconds=0, min_off_seconds=0)
    c.set_greedy(True)
    # Export is free, but there is no grid reading at all.
    await c.apply(0.0, _T0, export_rate=0.0, grid_power_w=None)
    assert len(_turn_ons(hass)) == 0
    assert c.status()["greedy_blocked"] == "no_grid_power"
    assert c.status()["greedy_reason"] is None
    # Same slot, grid reading now available and spilling more than the device draws ->
    # the block clears and the condition fires.
    await c.apply(0.0, _T0, export_rate=0.0, grid_power_w=-5000.0)
    assert c.status()["greedy_blocked"] is None
    assert c.status()["greedy_reason"] == "export_surplus"
    assert len(_turn_ons(hass)) == 1


async def _run_no_grid_power_block_yields_to_forecast_surplus():
    """A missing grid reading must not leave a stale "blocked" alongside a greedy that
    did fire on the forward-looking condition — status() would then report the device as
    both running-on-greedy and greedy-blocked."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0,
                                 min_on_seconds=0, min_off_seconds=0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    # No grid reading (would record no_grid_power), but the forecast condition fires:
    # 10 kWh wasted over 4 h = a 2.5 kW rate that clears the 2 kW device, and the battery
    # can both supply the rate and absorb the whole budget.
    await c.apply(0.0, _T0, export_rate=0.0, grid_power_w=None,
                  forecast_spill_kwh=10.0, forecast_hours=4.0,
                  battery_headroom_w=2000.0, battery_headroom_kwh=12.0)
    assert c.status()["greedy_reason"] == "forecast_surplus"
    assert c.status()["greedy_blocked"] is None
    assert len(_turn_ons(hass)) == 1


def test_surplus_bar_scales_with_covered_span():
    hass = FakeHass()
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    assert c.forecast_surplus_needed_kwh(4.0) == 8.0
    assert c.forecast_surplus_needed_kwh(2.5) == 5.0
    assert c.forecast_surplus_needed_kwh(0.0) == 0.0


# ----------------------------------------------------------------- manager-level tests
def _mgr(extra_data=None, grid_power_sensor=""):
    hass = FakeHass()
    hass.states.set("switch.pool", "off")
    data = {
        "deferrable_load_sensors": ["sensor.pool"],
        "deferrable_load_max_kw": [2.0],
        "deferrable_load_switches": ["switch.pool"],
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


def test_manager_min_export_price():
    # Unset -> 0.0 (disabled), so greedy's export bar stays at "<= $0".
    m, _hass = _mgr()
    assert m._min_export_price() == 0.0
    # entry.data holds the pre-entity fallback, in c/kWh; _min_export_price returns $/kWh.
    # (The stubbed get_live_number returns this default, standing in for "no number entity
    # registered yet".)
    m2, _hass2 = _mgr(extra_data={"min_export_price": 5.0})
    assert abs(m2._min_export_price() - 0.05) < 1e-9


def test_manager_battery_headroom_no_battery_configured():
    # No battery_soc_sensor / battery_charge_power_sensor at all -> the forecast-surplus
    # gate must fail closed, not silently read as "unlimited headroom".
    m, _hass = _mgr()
    assert m._battery_headroom_w() is None


def test_manager_battery_headroom_reads_soc_and_charge_sensors():
    m, hass = _mgr(extra_data={
        "battery_soc_sensor": "sensor.battery_soc",
        "battery_charge_power_sensor": "sensor.battery_power",
        "battery_min_soc": 10.0,
        "battery_max_discharge_rate": 5.0,  # kW
    })
    # Unreadable SOC -> unknown, not a guess.
    assert m._battery_headroom_w() is None
    # SOC healthy, battery idle (0 W) -> full 5 kW rated discharge is free.
    hass.states.set("sensor.battery_soc", "60")
    hass.states.set("sensor.battery_power", "0")
    assert m._battery_headroom_w() == 5000.0
    # SOC healthy, battery already discharging 2000 W (house load) -> only the remainder
    # is free (signed sensor: positive = charging, negative = discharging).
    hass.states.set("sensor.battery_power", "-2000")
    assert m._battery_headroom_w() == 3000.0
    # SOC healthy, battery charging -> nothing is being drawn from it, full rate is free.
    hass.states.set("sensor.battery_power", "1500")
    assert m._battery_headroom_w() == 5000.0
    # SOC at/below the configured minimum -> a real, measured zero, not "unknown".
    hass.states.set("sensor.battery_soc", "10")
    assert m._battery_headroom_w() == 0.0
    hass.states.set("sensor.battery_soc", "5")
    assert m._battery_headroom_w() == 0.0
    # SOC healthy again but the charge-power sensor is unavailable -> unknown, fails closed.
    hass.states.set("sensor.battery_soc", "60")
    hass.states.set("sensor.battery_power", "unavailable")
    assert m._battery_headroom_w() is None


def test_manager_ac_output_headroom_unconfigured():
    # No max_ac_output_kw at all (the common case) -> None, a pure no-op — never mistaken
    # for "0 W of headroom", which would incorrectly block every forecast-surplus device.
    m, _hass = _mgr()
    assert m._ac_output_headroom_w() is None
    # Explicit 0 (the config-flow default) means the same thing: unset.
    m2, _hass2 = _mgr(extra_data={"max_ac_output_kw": 0.0})
    assert m2._ac_output_headroom_w() is None


def test_manager_ac_output_headroom_reads_load_and_grid_sensors():
    # A configured ceiling (10 kW, matching the household's own Sigenergy plant) needs
    # both load and grid power to compute live plant output.
    m, hass = _mgr(extra_data={
        "max_ac_output_kw": 10.0,
        "load_power_sensor": "sensor.load_power",
        "grid_power_sensor": "sensor.grid_power",
    })
    # Configured but nothing readable yet -> fails CLOSED (0.0, not None): a real ceiling
    # the household told GridLens about must not be silently ignored on a sensor blip.
    assert m._ac_output_headroom_w() == 0.0
    # Load already 3.5kW over the ~10 kW cap found on this household's own Sigenergy
    # install (GRIDLENS_CHECKLIST.md, 2026-09-12), backfilled by grid import -> NEGATIVE
    # headroom (fixed 2026-09-13: netting the live import out of load_w before comparing
    # to the cap hid this overshoot as a false-safe 0.0, which is exactly what let a
    # device's setpoint freeze mid-overshoot instead of being pulled back down).
    hass.states.set("sensor.load_power", "13500")
    hass.states.set("sensor.grid_power", "3500")  # importing
    assert m._ac_output_headroom_w() == -3500.0  # 10000 - 13500 = -3500
    # Plant comfortably under the cap -> full remaining headroom.
    hass.states.set("sensor.load_power", "4000")
    hass.states.set("sensor.grid_power", "0")
    assert m._ac_output_headroom_w() == 6000.0
    # Load over the cap with grid_w exactly 0 (not importing) is physically
    # inconsistent, but exercises the non-importing branch's own floor at zero — the
    # negative-headroom fix above only applies once grid_w > 0.
    hass.states.set("sensor.load_power", "15000")
    hass.states.set("sensor.grid_power", "0")
    assert m._ac_output_headroom_w() == 0.0
    # A sensor going unavailable again -> back to failing closed at 0.0.
    hass.states.set("sensor.grid_power", "unavailable")
    assert m._ac_output_headroom_w() == 0.0
    # EXPORTING right at the plant's own production limit (found live 2026-09-12, hours
    # after the ceiling fix above shipped: household exporting ~3.4kW, PV essentially at
    # the plant's cap, and this clamp throttled a legitimately-surplus-soaking Wattpilot
    # DOWN because "plant output" alone looked maxed). Redirecting the exported power to
    # a load costs the plant nothing extra to produce -> it must be credited back, not
    # treated as already spoken for.
    hass.states.set("sensor.load_power", "6540")
    hass.states.set("sensor.grid_power", "-3385")  # exporting
    # (cap - plant_output) + export = (10000 - (6540 - -3385)) + 3385 = 75 + 3385 = 3460
    assert abs(m._ac_output_headroom_w() - 3460.0) < 1e-6
    # Comfortably exporting well below the cap -> the full export is redirectable, on top
    # of genuine spare capacity.
    hass.states.set("sensor.load_power", "2000")
    hass.states.set("sensor.grid_power", "-5000")  # exporting 5kW
    # (10000 - (2000 - -5000)) + 5000 = 3000 + 5000 = 8000
    assert m._ac_output_headroom_w() == 8000.0


def test_manager_ac_output_headroom_no_sensors_configured():
    # Ceiling configured, but neither load nor grid power sensor set (an install that
    # knows its inverter's rating but hasn't wired the general sensors) -> fails closed,
    # same discipline as an unreadable sensor.
    m, _hass = _mgr(extra_data={"max_ac_output_kw": 10.0})
    assert m._ac_output_headroom_w() == 0.0


def test_manager_schedule_default_unrestricted():
    # No stored weekly grid painted yet — the device is unrestricted (any hour) rather
    # than falling back to a static config spec (that field was removed 2026-08-02; the
    # dashboard schedule card is the only place an availability window is set now).
    m, _hass = _mgr()

    async def go():
        assert await m._schedule_allows_now(0, _T0) is True
        assert await m._schedule_allows_now(0, _T0.replace(hour=20)) is True

    _run_async(go)


def test_manager_schedule_store_restricts_hours():
    # A stored weekly grid (painted on the dashboard schedule card) is honored even
    # though the no-schedule default is unrestricted — hours left off actually block.
    week_9_to_17 = [[1 if 18 <= s < 34 else 0 for s in range(48)] for _ in range(7)]
    m, hass = _mgr()
    hass.data[DOMAIN] = {f"{m.entry.entry_id}_deferrable_schedules":
                         FakeScheduleStore({"sensor.pool": week_9_to_17})}

    async def go():
        # _T0 is Friday 12:00 UTC-as-local -> inside 9-17.
        assert await m._schedule_allows_now(0, _T0) is True
        # 20:00 same day -> outside 9-17.
        assert await m._schedule_allows_now(0, _T0.replace(hour=20)) is False

    _run_async(go)


def _slots(specs, start=None, minutes=30):
    """Build a plan of consecutive `minutes`-long slots from
    (import_rate, export_rate, total_export_w[, deferrable_w[, action, power_w]]) tuples.
    action defaults to SELF_USE (never a reservation) and power_w to 0.0."""
    t = start or _T0
    out = []
    for j, spec in enumerate(specs):
        imp, exp, tot_exp = spec[0], spec[1], spec[2]
        dev = spec[3] if len(spec) > 3 else 0.0
        action = spec[4] if len(spec) > 4 else BatteryAction.SELF_USE
        power_w = spec[5] if len(spec) > 5 else 0.0
        out.append(DispatchInterval(
            start=t + timedelta(minutes=j * minutes), action=action, power_w=power_w,
            import_rate=imp, export_rate=exp, total_export_w=tot_exp, deferrable_w=[dev],
        ))
    return out


def test_manager_forecast_surplus_budget_counts_spilled_export():
    """A plan that fills the whole look-ahead window exporting 5 kW at a $0 export price
    -> 5 kW * LOOKAHEAD_H kWh wasted."""
    m, _hass = _mgr()
    m.set_plan(_slots([(0.3, 0.0, 5000.0)] * _LOOKAHEAD_SLOTS), updated_at=_T0)
    kwh, hours = m._forecast_surplus_budget(0, _T0)
    assert abs(hours - LOOKAHEAD_H) < 1e-6
    assert abs(kwh - 5.0 * LOOKAHEAD_H) < 1e-6


def test_manager_forecast_surplus_budget_ignores_paid_export():
    """Same spill, but the export actually earns money -> nothing is being wasted."""
    m, _hass = _mgr()
    m.set_plan(_slots([(0.3, 0.08, 5000.0)] * _LOOKAHEAD_SLOTS), updated_at=_T0)
    kwh, hours = m._forecast_surplus_budget(0, _T0)
    assert abs(hours - LOOKAHEAD_H) < 1e-6
    assert kwh == 0.0


def test_manager_forecast_surplus_budget_counts_below_floor_export():
    """With a Minimum Export Price set, forecast export priced at or below it is "wasted"
    too (2026-09-11) — condition #3's numerator matches condition #2's live bar. A full
    window exporting 5 kW at 3c with a 5c floor -> 5 kW * LOOKAHEAD_H kWh; the same spill
    at 8c (above the floor) still earns money and counts for nothing."""
    m, _hass = _mgr(extra_data={"min_export_price": 5.0})  # c/kWh -> $0.05/kWh
    m.set_plan(_slots([(0.3, 0.03, 5000.0)] * _LOOKAHEAD_SLOTS), updated_at=_T0)
    kwh, hours = m._forecast_surplus_budget(0, _T0)
    assert abs(hours - LOOKAHEAD_H) < 1e-6
    assert abs(kwh - 5.0 * LOOKAHEAD_H) < 1e-6

    m2, _h2 = _mgr(extra_data={"min_export_price": 5.0})
    m2.set_plan(_slots([(0.3, 0.08, 5000.0)] * _LOOKAHEAD_SLOTS), updated_at=_T0)
    kwh2, _ = m2._forecast_surplus_budget(0, _T0)
    assert kwh2 == 0.0


def test_manager_forecast_surplus_budget_counts_unused_free_import():
    """A free-import window the plan doesn't already use for this device counts as free
    energy on the table; the half-hour it DOES schedule the device (2 kW = full draw)
    contributes nothing."""
    m, _hass = _mgr()  # device max_kw = 2.0
    plan = _slots([(0.0, 0.4, 0.0)] * (_LOOKAHEAD_SLOTS - 1) + [(0.0, 0.4, 0.0, 2000.0)])
    m.set_plan(plan, updated_at=_T0)
    kwh, hours = m._forecast_surplus_budget(0, _T0)
    assert abs(hours - LOOKAHEAD_H) < 1e-6
    assert abs(kwh - 2.0 * 0.5 * (_LOOKAHEAD_SLOTS - 1)) < 1e-6  # n-1 slots * 0.5 h * 2 kW


def test_manager_forecast_surplus_budget_clips_to_lookahead_and_now():
    """Only the part of the plan inside [now, now+LOOKAHEAD_H) counts — earlier slots and
    slots past the window are excluded, and the current slot counts only its remainder."""
    m, _hass = _mgr()
    # (LOOKAHEAD_SLOTS + 4) slots starting 1 h before "now": (LOOKAHEAD_H + 1) h remain,
    # the window clips it back to LOOKAHEAD_H.
    m.set_plan(_slots([(0.3, 0.0, 4000.0)] * (_LOOKAHEAD_SLOTS + 4),
                      start=_T0 - timedelta(hours=1)), updated_at=_T0)
    kwh, hours = m._forecast_surplus_budget(0, _T0)
    assert abs(hours - LOOKAHEAD_H) < 1e-6
    assert abs(kwh - 4.0 * LOOKAHEAD_H) < 1e-6


def test_manager_forecast_surplus_budget_short_plan_fails_closed():
    """Less than _MIN_BUDGET_WINDOW_H of plan left at all -> no judgement (None), rather
    than average a sliver into a rate that looks more trustworthy than it is."""
    m, _hass = _mgr()
    m.set_plan(_slots([(0.3, 0.0, 9000.0)] * 1), updated_at=_T0)  # one 30-min slot
    # Query 15 min into it -> only 15 min (< _MIN_BUDGET_WINDOW_H) of plan remains.
    assert m._forecast_surplus_budget(0, _T0 + timedelta(minutes=15)) == (None, 0.0)


def test_manager_forecast_surplus_budget_reservation_clips_the_window():
    """The budget window ends at the plan's first *material* planned discharge — past
    there the plan is spending the battery on something it values (2026-09-11's
    reservation clip), and the forecast-surplus condition must not borrow across it.
    4 slots (2 h) exporting 5 kW at $0, then a material discharge -> only those 2 h count,
    even though the nominal look-ahead is much longer."""
    m, _hass = _mgr()
    plan = (_slots([(0.3, 0.0, 5000.0)] * 4)
            + _slots([(0.3, 0.28, 0.0, 0.0, BatteryAction.DISCHARGE, 5000.0)] * 4,
                     start=_T0 + timedelta(hours=2)))
    m.set_plan(plan, updated_at=_T0)
    kwh, hours = m._forecast_surplus_budget(0, _T0)
    assert abs(hours - 2.0) < 1e-6
    assert abs(kwh - 10.0) < 1e-6  # 5 kW * 2 h, none of the post-reservation export counted


def test_manager_forecast_surplus_budget_small_discharge_is_not_a_reservation():
    """A tiny discharge (below _RESERVED_DISCHARGE_MIN_W — e.g. topping up house load) is
    not the plan "spending the battery on something it values" and must not clip the
    window; only a material one does."""
    m, _hass = _mgr()
    plan = (_slots([(0.3, 0.0, 5000.0)] * 4)
            + _slots([(0.3, 0.0, 5000.0, 0.0, BatteryAction.DISCHARGE, 50.0)] * 4,
                     start=_T0 + timedelta(hours=2)))
    m.set_plan(plan, updated_at=_T0)
    kwh, hours = m._forecast_surplus_budget(0, _T0)
    assert abs(hours - 4.0) < 1e-6
    assert abs(kwh - 20.0) < 1e-6  # all 8 slots counted


def test_manager_forecast_surplus_budget_reservation_at_now_fails_closed():
    """The plan is already materially discharging the battery THIS slot -> the safe
    window is empty; the condition must fail closed, not silently judge nothing wasted."""
    m, _hass = _mgr()
    plan = _slots([(0.3, 0.28, 0.0, 0.0, BatteryAction.DISCHARGE, 5000.0)] * 4)
    m.set_plan(plan, updated_at=_T0)
    assert m._forecast_surplus_budget(0, _T0) == (None, 0.0)


def test_manager_forecast_surplus_budget_no_plan():
    m, _hass = _mgr()
    assert m._forecast_surplus_budget(0, _T0) == (None, 0.0)


def test_manager_greedy_forecast_surplus_roundtrip():
    m, _hass = _mgr()

    async def go():
        assert m.is_greedy_forecast_surplus(0) is False
        assert await m.set_greedy_forecast_surplus(0, True) is True
        assert m.is_greedy_forecast_surplus(0) is True
        assert await m.set_greedy_forecast_surplus(99, True) is False
        assert m.is_greedy_forecast_surplus(99) is False

    _run_async(go)


def _forecast_battery_data(**over):
    """Battery config that gives the forecast-surplus condition a real, but not
    unlimited, buffer: 30 kWh pack at 60% SOC / 10% min -> 15 kWh headroom, 5 kW rated
    discharge -> plenty of rate for a 2 kW test device."""
    data = {
        "battery_soc_sensor": "sensor.battery_soc",
        "battery_charge_power_sensor": "sensor.battery_power",
        "battery_max_discharge_rate": 5.0,  # kW
        "battery_capacity": 30.0,  # kWh
    }
    data.update(over)
    return data


async def _run_manager_end_to_end_surplus_tick():
    """Full integration: nothing is free right now (priced import, priced export, house
    importing) and the plan wants the device off, but a forecast spill whose average rate
    covers the device's full draw still starts it — the battery has both the discharge
    rate and the energy-to-min-SOC to actually supply it without creating new grid
    import. 4 slots (2 h) at 6 kW $0 export -> 12 kWh over 2 h = a 6 kW rate."""
    m, hass = _mgr(grid_power_sensor="sensor.grid_power", extra_data=_forecast_battery_data())
    hass.states.set("sensor.grid_power", "500")  # importing
    hass.states.set("sensor.battery_soc", "60")
    hass.states.set("sensor.battery_power", "0")
    _NOW[0] = _T0
    m.set_plan(_slots([(0.3, 0.0, 6000.0)] * 4, start=_T0 - timedelta(minutes=1)),
               updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)  # first tick establishes "off" (plan wants off, no greedy yet)
    assert len(_turn_ons(hass)) == 0
    await m.set_greedy(0, True)
    await m.set_greedy_forecast_surplus(0, True)
    later = _T0 + timedelta(minutes=16)  # past the 15-min min-off debounce
    await m._tick_device(0, later)
    assert len(_turn_ons(hass)) == 1


async def _run_manager_end_to_end_forecast_below_floor_tick():
    """Same shape as the surplus tick, but the forecast export is priced at 3c, not $0 —
    below the user's 5c Minimum Export Price (2026-09-11). Condition #3 now treats that
    as wasted and starts the device early off the battery, where before it saw nothing to
    chase."""
    m, hass = _mgr(grid_power_sensor="sensor.grid_power",
                   extra_data=_forecast_battery_data(min_export_price=5.0))  # c/kWh
    hass.states.set("sensor.grid_power", "500")   # importing right now — nothing live-free
    hass.states.set("sensor.battery_soc", "60")
    hass.states.set("sensor.battery_power", "0")
    _NOW[0] = _T0
    m.set_plan(_slots([(0.3, 0.03, 6000.0)] * 4, start=_T0 - timedelta(minutes=1)),
               updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    assert len(_turn_ons(hass)) == 0
    await m.set_greedy(0, True)
    await m.set_greedy_forecast_surplus(0, True)
    await m._tick_device(0, _T0 + timedelta(minutes=16))
    assert len(_turn_ons(hass)) == 1

    # Floor left at its 0 default -> 3c export is priced, condition #3 sees nothing.
    m2, hass2 = _mgr(grid_power_sensor="sensor.grid_power", extra_data=_forecast_battery_data())
    hass2.states.set("sensor.grid_power", "500")
    hass2.states.set("sensor.battery_soc", "60")
    hass2.states.set("sensor.battery_power", "0")
    _NOW[0] = _T0
    m2.set_plan(_slots([(0.3, 0.03, 6000.0)] * 4, start=_T0 - timedelta(minutes=1)),
                updated_at=_T0)
    await m2.set_entitled(True)
    await m2.enable(0)
    await m2.set_greedy(0, True)
    await m2.set_greedy_forecast_surplus(0, True)
    await m2._tick_device(0, _T0 + timedelta(minutes=16))
    assert len(_turn_ons(hass2)) == 0


def test_manager_notify_on_every_tick():
    """The control switch entity's state listener fires on every tick, not just on user
    actions (2026-09-11) — so the Load Control card shows live greedy state instead of
    whatever it read at startup."""
    m, hass = _mgr()
    fired = [0]
    m.set_state_listener(0, lambda: fired.__setitem__(0, fired[0] + 1))

    async def go():
        _NOW[0] = _T0
        m.set_plan(_slots([(0.3, 0.3, 0.0)] * 8, start=_T0 - timedelta(minutes=1)),
                   updated_at=_T0)
        await m.set_entitled(True)
        await m.enable(0)
        before = fired[0]
        await m._tick_device(0, _T0 + timedelta(minutes=16))
        assert fired[0] > before  # tick pushed a fresh state, no override touched

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


async def _run_manager_end_to_end_below_floor_export_tick():
    """Full integration for the 2026-09-11 change: import is priced and export is priced
    at 3c, but the user's Minimum Export Price is 5c, so the below-floor spill is "wasted"
    and the pool pump (LP wants it OFF) is switched on off a real tick."""
    m, hass = _mgr(grid_power_sensor="sensor.grid_power",
                   extra_data={"min_export_price": 5.0})  # c/kWh
    _NOW[0] = _T0
    hass.states.set("sensor.grid_power", "-4000")  # exporting 4 kW, > the 2 kW pump
    plan = [DispatchInterval(start=_T0 - timedelta(minutes=1), action=BatteryAction.SELF_USE,
                              deferrable_w=[0.0], import_rate=0.35, export_rate=0.03)]
    m.set_plan(plan, updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    assert len(_turn_ons(hass)) == 0
    await m.set_greedy(0, True)
    later = _T0 + timedelta(minutes=16)  # past the 15-min min-off debounce
    await m._tick_device(0, later)
    assert len(_turn_ons(hass)) == 1
    assert m.controllers[0].status()["greedy_reason"] == "export_surplus"

    # Same tick, floor left at its 0 default -> priced 3c export, nothing wasted, no run.
    m2, hass2 = _mgr(grid_power_sensor="sensor.grid_power")
    _NOW[0] = _T0
    hass2.states.set("sensor.grid_power", "-4000")
    m2.set_plan([DispatchInterval(start=_T0 - timedelta(minutes=1),
                                  action=BatteryAction.SELF_USE, deferrable_w=[0.0],
                                  import_rate=0.35, export_rate=0.03)], updated_at=_T0)
    await m2.set_entitled(True)
    await m2.enable(0)
    await m2.set_greedy(0, True)
    await m2._tick_device(0, _T0 + timedelta(minutes=16))
    assert len(_turn_ons(hass2)) == 0


if __name__ == "__main__":
    tests = [
        ("greedy_import_free_turns_on", lambda: _run_async(_run_greedy_import_free_turns_on)),
        ("greedy_export_surplus_turns_on", lambda: _run_async(_run_greedy_export_surplus_turns_on)),
        ("greedy_export_insufficient_no_effect", lambda: _run_async(_run_greedy_export_insufficient_no_effect)),
        ("greedy_export_below_floor_turns_on", lambda: _run_async(_run_greedy_export_below_floor_turns_on)),
        ("greedy_export_below_floor_disabled_by_default", lambda: _run_async(_run_greedy_export_below_floor_disabled_by_default)),
        ("greedy_export_above_floor_no_effect", lambda: _run_async(_run_greedy_export_above_floor_no_effect)),
        ("greedy_below_floor_still_needs_power_cover", lambda: _run_async(_run_greedy_below_floor_still_needs_power_cover)),
        ("greedy_disabled_no_effect", lambda: _run_async(_run_greedy_disabled_no_effect)),
        ("greedy_none_inputs_fail_closed", lambda: _run_async(_run_greedy_none_inputs_fail_closed)),
        ("greedy_respects_schedule_suppresses", lambda: _run_async(_run_greedy_respects_schedule_suppresses)),
        ("greedy_ignores_schedule_when_flag_off", lambda: _run_async(_run_greedy_ignores_schedule_when_flag_off)),
        ("override_suppresses_greedy", lambda: _run_async(_run_override_suppresses_greedy)),
        ("greedy_honours_debounce", lambda: _run_async(_run_greedy_honours_debounce)),
        ("status_reports_greedy_state", test_status_reports_greedy_state),
        ("surplus_turns_on_when_rate_covers_draw", lambda: _run_async(_run_surplus_turns_on_when_rate_covers_draw)),
        ("surplus_needs_battery_headroom", lambda: _run_async(_run_surplus_needs_battery_headroom)),
        ("surplus_ac_output_headroom_clamps_and_blocks", lambda: _run_async(_run_surplus_ac_output_headroom_clamps_and_blocks)),
        ("surplus_insufficient_no_effect", lambda: _run_async(_run_surplus_insufficient_no_effect)),
        ("surplus_needs_its_own_toggle", lambda: _run_async(_run_surplus_needs_its_own_toggle)),
        ("surplus_needs_master_greedy", lambda: _run_async(_run_surplus_needs_master_greedy)),
        ("surplus_missing_forecast_fails_closed", lambda: _run_async(_run_surplus_missing_forecast_fails_closed)),
        ("surplus_respects_schedule", lambda: _run_async(_run_surplus_respects_schedule)),
        ("surplus_bar_scales_with_covered_span", test_surplus_bar_scales_with_covered_span),
        ("status_reports_greedy_reason", lambda: _run_async(_run_status_reports_greedy_reason)),
        ("greedy_reason_cleared_when_plan_already_wants_on", lambda: _run_async(_run_greedy_reason_cleared_when_plan_already_wants_on)),
        ("status_reports_greedy_blocked", lambda: _run_async(_run_status_reports_greedy_blocked)),
        ("status_reports_no_grid_power_block", lambda: _run_async(_run_status_reports_no_grid_power_block)),
        ("no_grid_power_block_yields_to_forecast_surplus", lambda: _run_async(_run_no_grid_power_block_yields_to_forecast_surplus)),
        ("manager_set_greedy_roundtrip", test_manager_set_greedy_roundtrip),
        ("manager_reads_grid_power_sensor", test_manager_reads_grid_power_sensor),
        ("manager_no_grid_power_sensor_configured", test_manager_no_grid_power_sensor_configured),
        ("manager_min_export_price", test_manager_min_export_price),
        ("manager_battery_headroom_no_battery_configured", test_manager_battery_headroom_no_battery_configured),
        ("manager_battery_headroom_reads_soc_and_charge_sensors", test_manager_battery_headroom_reads_soc_and_charge_sensors),
        ("manager_ac_output_headroom_unconfigured", test_manager_ac_output_headroom_unconfigured),
        ("manager_ac_output_headroom_reads_load_and_grid_sensors", test_manager_ac_output_headroom_reads_load_and_grid_sensors),
        ("manager_ac_output_headroom_no_sensors_configured", test_manager_ac_output_headroom_no_sensors_configured),
        ("manager_schedule_default_unrestricted", test_manager_schedule_default_unrestricted),
        ("manager_schedule_store_restricts_hours", test_manager_schedule_store_restricts_hours),
        ("manager_forecast_surplus_budget_counts_spilled_export", test_manager_forecast_surplus_budget_counts_spilled_export),
        ("manager_forecast_surplus_budget_ignores_paid_export", test_manager_forecast_surplus_budget_ignores_paid_export),
        ("manager_forecast_surplus_budget_counts_below_floor_export", test_manager_forecast_surplus_budget_counts_below_floor_export),
        ("manager_notify_on_every_tick", test_manager_notify_on_every_tick),
        ("manager_forecast_surplus_budget_counts_unused_free_import", test_manager_forecast_surplus_budget_counts_unused_free_import),
        ("manager_forecast_surplus_budget_clips_to_lookahead_and_now", test_manager_forecast_surplus_budget_clips_to_lookahead_and_now),
        ("manager_forecast_surplus_budget_short_plan_fails_closed", test_manager_forecast_surplus_budget_short_plan_fails_closed),
        ("manager_forecast_surplus_budget_reservation_clips_the_window", test_manager_forecast_surplus_budget_reservation_clips_the_window),
        ("manager_forecast_surplus_budget_small_discharge_is_not_a_reservation", test_manager_forecast_surplus_budget_small_discharge_is_not_a_reservation),
        ("manager_forecast_surplus_budget_reservation_at_now_fails_closed", test_manager_forecast_surplus_budget_reservation_at_now_fails_closed),
        ("manager_forecast_surplus_budget_no_plan", test_manager_forecast_surplus_budget_no_plan),
        ("manager_greedy_forecast_surplus_roundtrip", test_manager_greedy_forecast_surplus_roundtrip),
        ("manager_end_to_end_greedy_tick", lambda: _run_async(_run_manager_end_to_end_greedy_tick)),
        ("manager_end_to_end_below_floor_export_tick", lambda: _run_async(_run_manager_end_to_end_below_floor_export_tick)),
        ("manager_end_to_end_surplus_tick", lambda: _run_async(_run_manager_end_to_end_surplus_tick)),
        ("manager_end_to_end_forecast_below_floor_tick", lambda: _run_async(_run_manager_end_to_end_forecast_below_floor_tick)),
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
