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
    assert c.status()["greedy_forecast_surplus"] is False
    c.set_greedy(True)
    c.set_greedy_respects_schedule(True)
    c.set_greedy_forecast_surplus(True)
    assert c.status()["greedy"] is True
    assert c.status()["greedy_respects_schedule"] is True
    assert c.status()["greedy_forecast_surplus"] is True


# --------------------------------------------------- forecast-surplus (controller level)
async def _run_surplus_turns_on_when_waste_exceeds_need():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    # Nothing is free right now (import priced, export priced, importing), but the plan
    # forecasts 10 kWh wasted over 4 h vs this device's 2 kW * 4 h = 8 kWh need. The battery
    # has enough headroom (2000 W free discharge) to actually supply the device right now.
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_free_kwh=10.0, forecast_hours=4.0, battery_headroom_w=2000.0)
    assert len(_turn_ons(hass)) == 1
    assert c._note.endswith("_greedy")


async def _run_surplus_needs_battery_headroom():
    """The forecast clearing its bar is necessary but not sufficient — see the module
    docstring's "Forecast surplus" section: without battery headroom to draw on, firing
    would be real, unbuffered grid import, not a bet with a battery behind it."""
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    # Same forecast as the positive case above, but no battery headroom is known at all
    # (no battery configured / unreadable sensors) -> must fail closed.
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_free_kwh=10.0, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0
    assert c.status()["greedy_blocked"] == "no_battery_headroom"
    # Some headroom exists, but not enough to cover this device's full 2000 W draw.
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_free_kwh=10.0, forecast_hours=4.0, battery_headroom_w=500.0)
    assert len(_turn_ons(hass)) == 0
    assert c.status()["greedy_blocked"] == "no_battery_headroom"


async def _run_surplus_insufficient_no_effect():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    # 7 kWh forecast waste < 8 kWh the device would eat over the same 4 h -> not enough.
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_free_kwh=7.0, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0


async def _run_surplus_needs_its_own_toggle():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)  # master greedy on, forecast-surplus left OFF (default)
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_free_kwh=100.0, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0


async def _run_surplus_needs_master_greedy():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy_forecast_surplus(True)  # but master greedy stays OFF
    await c.apply(0.0, _T0, import_rate=0.35, export_rate=0.05, grid_power_w=500.0,
                  forecast_free_kwh=100.0, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0


async def _run_surplus_missing_forecast_fails_closed():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    await c.apply(0.0, _T0, import_rate=0.35, forecast_free_kwh=None, forecast_hours=4.0)
    assert len(_turn_ons(hass)) == 0
    # A covered span of 0 h can't justify anything either (bar would be 0 kWh).
    await c.apply(0.0, _T0, import_rate=0.35, forecast_free_kwh=5.0, forecast_hours=0.0)
    assert len(_turn_ons(hass)) == 0


async def _run_surplus_respects_schedule():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    c = DeferrableLoadController(hass, name="X", switch_entity_id="switch.x", max_w=2000.0)
    c.set_greedy(True)
    c.set_greedy_forecast_surplus(True)
    c.set_greedy_respects_schedule(True)
    await c.apply(0.0, _T0, import_rate=0.35, schedule_allows=False,
                  forecast_free_kwh=100.0, forecast_hours=4.0)
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
                  forecast_free_kwh=10.0, forecast_hours=4.0, battery_headroom_w=2000.0)
    st = c.status()
    assert st["greedy_reason"] == "forecast_surplus"
    assert st["forecast_free_kwh"] == 10.0
    assert st["forecast_needed_kwh"] == 8.0
    assert st["forecast_battery_headroom_w"] == 2000.0

    # Nothing free and nothing forecast -> reason clears, but the figures still publish
    # so the UI can show progress toward the bar.
    await c.apply(0.0, _T0, import_rate=0.5, export_rate=0.05, grid_power_w=500.0,
                  forecast_free_kwh=3.0, forecast_hours=4.0)
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
    # No grid reading (would record no_grid_power), but the forecast clears the bar:
    # 2 kW over 4 h needs 8 kWh and 10 kWh is forecast wasted, and the battery has the
    # headroom to actually supply it.
    await c.apply(0.0, _T0, export_rate=0.0, grid_power_w=None,
                  forecast_free_kwh=10.0, forecast_hours=4.0, battery_headroom_w=2000.0)
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
    (import_rate, export_rate, total_export_w[, deferrable_w]) tuples."""
    t = start or _T0
    out = []
    for j, spec in enumerate(specs):
        imp, exp, tot_exp = spec[0], spec[1], spec[2]
        dev = spec[3] if len(spec) > 3 else 0.0
        out.append(DispatchInterval(
            start=t + timedelta(minutes=j * minutes), action=BatteryAction.SELF_USE,
            import_rate=imp, export_rate=exp, total_export_w=tot_exp, deferrable_w=[dev],
        ))
    return out


def test_manager_forecast_free_kwh_counts_spilled_export():
    """8 half-hour slots (4 h) exporting 5 kW at a $0 export price -> 20 kWh wasted."""
    m, _hass = _mgr()
    m.set_plan(_slots([(0.3, 0.0, 5000.0)] * 8), updated_at=_T0)
    kwh, hours = m._forecast_free_kwh(0, _T0)
    assert abs(hours - 4.0) < 1e-6
    assert abs(kwh - 20.0) < 1e-6


def test_manager_forecast_free_kwh_ignores_paid_export():
    """Same spill, but the export actually earns money -> nothing is being wasted."""
    m, _hass = _mgr()
    m.set_plan(_slots([(0.3, 0.08, 5000.0)] * 8), updated_at=_T0)
    kwh, hours = m._forecast_free_kwh(0, _T0)
    assert abs(hours - 4.0) < 1e-6
    assert kwh == 0.0


def test_manager_forecast_free_kwh_counts_unused_free_import():
    """A free-import window the plan doesn't already use for this device counts as free
    energy on the table; the half-hour it DOES schedule the device (2 kW = full draw)
    contributes nothing."""
    m, _hass = _mgr()  # device max_kw = 2.0
    plan = _slots([(0.0, 0.4, 0.0)] * 7 + [(0.0, 0.4, 0.0, 2000.0)])
    m.set_plan(plan, updated_at=_T0)
    kwh, hours = m._forecast_free_kwh(0, _T0)
    assert abs(hours - 4.0) < 1e-6
    assert abs(kwh - 7.0) < 1e-6  # 7 slots * 0.5 h * 2 kW


def test_manager_forecast_free_kwh_clips_to_lookahead_and_now():
    """Only the part of the plan inside [now, now+4h) counts — earlier slots and slots
    past the window are excluded, and the current slot counts only its remainder."""
    m, _hass = _mgr()
    # 12 slots (6 h) starting 1 h before "now": 5 h remain, window clips it to 4 h.
    m.set_plan(_slots([(0.3, 0.0, 4000.0)] * 12, start=_T0 - timedelta(hours=1)),
               updated_at=_T0)
    kwh, hours = m._forecast_free_kwh(0, _T0)
    assert abs(hours - 4.0) < 1e-6
    assert abs(kwh - 16.0) < 1e-6


def test_manager_forecast_free_kwh_short_horizon_fails_closed():
    """Less than half the look-ahead left in the plan -> no judgement (None), rather than
    a shrunken bar a trivial surplus could clear."""
    m, _hass = _mgr()
    m.set_plan(_slots([(0.3, 0.0, 9000.0)] * 3), updated_at=_T0)  # 1.5 h < 2 h
    assert m._forecast_free_kwh(0, _T0) == (None, 0.0)


def test_manager_forecast_free_kwh_no_plan():
    m, _hass = _mgr()
    assert m._forecast_free_kwh(0, _T0) == (None, 0.0)


def test_manager_greedy_forecast_surplus_roundtrip():
    m, _hass = _mgr()

    async def go():
        assert m.is_greedy_forecast_surplus(0) is False
        assert await m.set_greedy_forecast_surplus(0, True) is True
        assert m.is_greedy_forecast_surplus(0) is True
        assert await m.set_greedy_forecast_surplus(99, True) is False
        assert m.is_greedy_forecast_surplus(99) is False

    _run_async(go)


async def _run_manager_end_to_end_surplus_tick():
    """Full integration: nothing is free right now (priced import, priced export, house
    importing) and the plan wants the device off, but a forecast spill bigger than the
    device could ever eat still starts it — because the battery also has enough headroom
    to actually supply the device without creating new grid import."""
    m, hass = _mgr(grid_power_sensor="sensor.grid_power", extra_data={
        "battery_soc_sensor": "sensor.battery_soc",
        "battery_charge_power_sensor": "sensor.battery_power",
        "battery_max_discharge_rate": 5.0,  # kW, well above the 2 kW device
    })
    hass.states.set("sensor.grid_power", "500")  # importing
    hass.states.set("sensor.battery_soc", "60")
    hass.states.set("sensor.battery_power", "0")
    _NOW[0] = _T0
    m.set_plan(_slots([(0.3, 0.0, 6000.0)] * 8, start=_T0 - timedelta(minutes=1)),
               updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)  # first tick establishes "off" (plan wants off, no greedy yet)
    assert len(_turn_ons(hass)) == 0
    await m.set_greedy(0, True)
    await m.set_greedy_forecast_surplus(0, True)
    later = _T0 + timedelta(minutes=16)  # past the 15-min min-off debounce
    await m._tick_device(0, later)
    assert len(_turn_ons(hass)) == 1


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
        ("surplus_turns_on_when_waste_exceeds_need", lambda: _run_async(_run_surplus_turns_on_when_waste_exceeds_need)),
        ("surplus_needs_battery_headroom", lambda: _run_async(_run_surplus_needs_battery_headroom)),
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
        ("manager_battery_headroom_no_battery_configured", test_manager_battery_headroom_no_battery_configured),
        ("manager_battery_headroom_reads_soc_and_charge_sensors", test_manager_battery_headroom_reads_soc_and_charge_sensors),
        ("manager_schedule_default_unrestricted", test_manager_schedule_default_unrestricted),
        ("manager_schedule_store_restricts_hours", test_manager_schedule_store_restricts_hours),
        ("manager_forecast_free_kwh_counts_spilled_export", test_manager_forecast_free_kwh_counts_spilled_export),
        ("manager_forecast_free_kwh_ignores_paid_export", test_manager_forecast_free_kwh_ignores_paid_export),
        ("manager_forecast_free_kwh_counts_unused_free_import", test_manager_forecast_free_kwh_counts_unused_free_import),
        ("manager_forecast_free_kwh_clips_to_lookahead_and_now", test_manager_forecast_free_kwh_clips_to_lookahead_and_now),
        ("manager_forecast_free_kwh_short_horizon_fails_closed", test_manager_forecast_free_kwh_short_horizon_fails_closed),
        ("manager_forecast_free_kwh_no_plan", test_manager_forecast_free_kwh_no_plan),
        ("manager_greedy_forecast_surplus_roundtrip", test_manager_greedy_forecast_surplus_roundtrip),
        ("manager_end_to_end_greedy_tick", lambda: _run_async(_run_manager_end_to_end_greedy_tick)),
        ("manager_end_to_end_surplus_tick", lambda: _run_async(_run_manager_end_to_end_surplus_tick)),
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
