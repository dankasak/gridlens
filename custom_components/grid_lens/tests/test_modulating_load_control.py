"""Offline tests for modulating (current-controlled) deferrable-load control.

Covers ModulatingLoadController + the LoadControlManager wiring that drives it, without HA
or scipy (neither importable in this container): unit inference and the explicit override,
phase auto-derivation, watts->setpoint conversion and step quantisation, the semi-continuous
floor with hysteresis, write economy (deadband / min-write-interval / boundary crossing),
fail-open plug detection, manual override, the user current cap, the relaxed export-surplus
greedy bar (asserted against the UNCHANGED on/off bar, so a regression in the parent is
caught here too), the manager's target policy, the 30-second timer lifecycle including the
pre-feature backward-compatibility guarantee, and the leave-as-is deadman.

Run: python3 tests/test_modulating_load_control.py

The three ``regr_*`` checks at the end of the list were written as failing tests against
bugs this suite found on 2026-08-03 (a Force On that force-*off*s a charger whose ceiling
isn't knowable yet; a Force On that clamps up past a user cap set below the 6 A floor; a
master switch that writes 0 A — cutting a manual charge — when enabled before the first
plan arrives). All three are fixed; the checks remain as regressions. Each was a silent
failure with no error trail, which is exactly why they are first-class checks here.

The ``known_bugs`` list is the mechanism for the next round: put a check there to assert
the contract (MODULATING_CONTRACT.md) rather than current behaviour and have it reported
as XFAIL without failing the suite. It is empty right now.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import traceback
import types
from datetime import datetime, timedelta, timezone

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_T0 = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)  # Friday
_NOW = [_T0]  # mutable "current time" the dt.now()/as_local() stubs read

# Registered async_track_time_interval calls (the 30-second modulation loop). Each entry is
# a dict; its cancel callable flips "cancelled". Reset per test by _reset_timers().
_INTERVALS: list[dict] = []
# Per-device power sensor the stubbed entity_lookup.resolve_power_sensor hands back, keyed
# by the device's energy sensor id (the manager's first anchor).
_POWER_SENSORS: dict[str, str] = {}


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

    def _track_interval(hass, action, delta, **kwargs):
        rec = {"delta": delta, "action": action, "cancelled": False}
        _INTERVALS.append(rec)

        def _cancel():
            rec["cancelled"] = True

        return _cancel

    # load_control_manager imports this function-locally (module-scope would break these
    # stubs) — it still resolves through this module object at call time.
    event.async_track_time_interval = _track_interval
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
    # Stub entity_lookup (pulls in homeassistant.helpers.entity_registry which we don't
    # stub). resolve_power_sensor is a real input here — the surplus term's "add back what
    # this device already draws" correction reads whatever it returns.
    el_stub = types.ModuleType("gl.entity_lookup")
    el_stub.resolve_device_name = lambda hass, *anchors: next((a for a in anchors if a), None)
    el_stub.resolve_power_sensor = (
        lambda hass, *anchors: _POWER_SENSORS.get(anchors[0] if anchors else "")
    )
    sys.modules["gl.entity_lookup"] = el_stub
    # Stub runtime_settings (pulls in homeassistant.helpers.entity_registry, unstubbed).
    # LoadControlManager._min_export_price() imports get_live_number from it lazily;
    # returning the passed default reproduces "no number entity registered yet".
    rs_stub = types.ModuleType("gl.runtime_settings")
    rs_stub.get_live_number = lambda hass, entry_id, suffix, default: default
    sys.modules["gl.runtime_settings"] = rs_stub
    lc = _load(os.path.join(_COMPONENT, "control", "load_controller.py"),
               "gl.control.load_controller", package="gl.control")
    # Preloaded so load_control_manager's function-local
    # `from .modulating_controller import ModulatingLoadController` resolves from sys.modules.
    mc = _load(os.path.join(_COMPONENT, "control", "modulating_controller.py"),
               "gl.control.modulating_controller", package="gl.control")
    lcm = _load(os.path.join(_COMPONENT, "control", "load_control_manager.py"),
                "gl.control.load_control_manager", package="gl.control")
    ex = sys.modules["gl.control.executor"]
    const = sys.modules["gl.const"]
    return (lc.DeferrableLoadController, mc.ModulatingLoadController, lcm.LoadControlManager,
            ex.DispatchInterval, const)


(DeferrableLoadController, ModulatingLoadController, LoadControlManager, DispatchInterval,
 CONST) = _bootstrap()
from gl.inverters.base import BatteryAction  # noqa: E402  (loaded above)

MODULATION_INTERVAL_SECONDS = CONST.MODULATION_INTERVAL_SECONDS
DEFAULT_SUPPLY_VOLTAGE = CONST.DEFAULT_SUPPLY_VOLTAGE
DEFAULT_MIN_CHARGE_CURRENT_A = CONST.DEFAULT_MIN_CHARGE_CURRENT_A


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

    def remove(self, eid):
        self._d.pop(eid, None)


class FakeServices:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def async_call(self, domain, service, data, blocking=False):
        if self.fail:
            raise RuntimeError("service boom")
        eid = data.get("entity_id")
        self.calls.append((domain, service, eid, dict(data)))


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


def _reset_timers():
    _INTERVALS.clear()
    _POWER_SENSORS.clear()


def _sets(hass):
    """Every number.set_value call, in order."""
    return [c for c in hass.services.calls if (c[0], c[1]) == ("number", "set_value")]


def _values(hass):
    return [c[3]["value"] for c in _sets(hass)]


def _last_value(hass):
    vals = _values(hass)
    return vals[-1] if vals else None


def _turn_ons(hass):
    return [c for c in hass.services.calls if c[1] == "turn_on"]


def _turn_offs(hass):
    return [c for c in hass.services.calls if c[1] == "turn_off"]


def _presses(hass, entity_id=None):
    calls = [c for c in hass.services.calls if (c[0], c[1]) == ("button", "press")]
    if entity_id is not None:
        calls = [c for c in calls if c[2] == entity_id]
    return calls


def _run_async(coro):
    asyncio.new_event_loop().run_until_complete(coro())


# A 7.4 kW single-phase EVSE: 32 A ceiling, 6 A floor, 1 A steps. The default rig for most
# checks below — deliberately NOT this install's hardware (this install has no modulating
# charger at all), just the shape every charger integration publishes.
def _evse(hass, eid="number.evse", state="0", unit="A", mn=6, mx=32, step=1):
    attrs = {}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    if mn is not None:
        attrs["min"] = mn
    if mx is not None:
        attrs["max"] = mx
    if step is not None:
        attrs["step"] = step
    hass.states.set(eid, state, attrs)


def _mk(hass, **kw):
    defaults = dict(name="EVSE", setpoint_entity_id="number.evse", max_w=7400.0)
    defaults.update(kw)
    return ModulatingLoadController(hass, **defaults)


# ================================================================= units
def test_unit_inference_from_entity():
    hass = FakeHass()
    _evse(hass, unit="A")
    assert _mk(hass)._unit() == "a"

    hass2 = FakeHass()
    _evse(hass2, unit="W", mn=1400, mx=7400)
    assert _mk(hass2)._unit() == "w"

    hass3 = FakeHass()
    _evse(hass3, unit="kW", mn=1.4, mx=7.4, step=0.1)
    assert _mk(hass3)._unit() == "kw"

    # Anything unrecognised falls back to amps (what every surveyed charger publishes).
    hass4 = FakeHass()
    _evse(hass4, unit="percent")
    assert _mk(hass4)._unit() == "a"


def test_unit_fallback_not_cached_until_entity_readable():
    """A charger integration that hasn't started yet must not pin the amps fallback."""
    hass = FakeHass()
    c = _mk(hass)  # no setpoint entity in the state machine yet
    assert c._unit() == "a"
    assert c._unit_cache is None  # NOT cached — still undecided
    _evse(hass, unit="W", mn=1400, mx=7400)
    assert c._unit() == "w"


def test_explicit_setpoint_unit_wins():
    hass = FakeHass()
    _evse(hass, unit="A")  # entity says amps...
    c = _mk(hass, setpoint_unit="kW")  # ...config says kW, config wins
    assert c._unit() == "kw"
    assert c.target_w_to_setpoint(3450.0) == 3.45
    c2 = _mk(hass, setpoint_unit="w")
    assert c2._unit() == "w"
    assert c2.target_w_to_setpoint(3450.0) == 3450.0


# ================================================================= phases
def test_phase_autoderive_single_vs_three():
    """The case only max_kw can disambiguate: both chargers advertise 32 A."""
    hass = FakeHass()
    _evse(hass, mx=32)
    single = _mk(hass, max_w=7400.0, phases=0)   # 7.4 kW @ 32 A -> 1 phase
    three = _mk(hass, max_w=22000.0, phases=0)   # 22 kW  @ 32 A -> 3 phases
    assert single._phase_count() == 1
    assert three._phase_count() == 3
    # ...and that is exactly what makes the A<->W conversion differ for the same amps.
    assert abs(single._amps_to_w(16.0) - 3680.0) < 1e-6
    assert abs(three._amps_to_w(16.0) - 11040.0) < 1e-6


def test_phase_fallback_when_entity_max_unreadable():
    hass = FakeHass()
    c = _mk(hass, max_w=22000.0, phases=0)  # entity absent
    assert c._phase_count() == 1            # never 0 — that would zero every conversion
    assert c._phase_cache is None           # and not cached, so it can still be derived
    _evse(hass, mx=32)
    assert c._phase_count() == 3
    # An entity that publishes no max at all keeps the fallback.
    hass2 = FakeHass()
    _evse(hass2, mx=None)
    assert _mk(hass2, max_w=22000.0, phases=0)._phase_count() == 1


def test_explicit_phases_win_and_clamp():
    hass = FakeHass()
    _evse(hass, mx=32)
    assert _mk(hass, max_w=22000.0, phases=1)._phase_count() == 1
    assert _mk(hass, max_w=7400.0, phases=3)._phase_count() == 3


def test_voltage_default_and_override():
    hass = FakeHass()
    _evse(hass)
    assert _mk(hass).voltage == DEFAULT_SUPPLY_VOLTAGE
    assert _mk(hass, voltage=0.0).voltage == DEFAULT_SUPPLY_VOLTAGE
    c = _mk(hass, voltage=240.0, phases=1)
    assert c.voltage == 240.0
    assert abs(c._amps_to_w(10.0) - 2400.0) < 1e-6


# ================================================================= conversion & rounding
def test_target_w_to_setpoint_is_pure():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)  # amps, 1 phase, 230 V
    assert abs(c.target_w_to_setpoint(3450.0) - 15.0) < 1e-9
    assert abs(c.target_w_to_setpoint(3600.0) - 3600.0 / 230.0) < 1e-9  # no rounding
    assert c.target_w_to_setpoint(-500.0) == 0.0                        # no negatives
    assert abs(c.target_w_to_setpoint(9999.0) - 9999.0 / 230.0) < 1e-9  # no clamping


def test_quantise_to_entity_step():
    hass = FakeHass()
    _evse(hass, mx=32, step=1)
    c = _mk(hass)
    assert c._quantised_setpoint(3600.0) == 16.0   # 15.65 A -> nearest 1 A
    assert c._quantised_setpoint(3450.0) == 15.0
    assert c._quantised_setpoint(0.0) == 0.0

    hass5 = FakeHass()
    _evse(hass5, mx=32, step=5)
    assert _mk(hass5)._quantised_setpoint(3600.0) == 15.0  # nearest 5 A

    # Sub-amp step: the float-dust trim must produce a clean value for the service call.
    hass01 = FakeHass()
    _evse(hass01, mx=32, step=0.1)
    assert _mk(hass01)._quantised_setpoint(3600.0) == 15.7

    # Rounding must not push past the ceiling.
    assert c._quantised_setpoint(8000.0) == 32.0


# ================================================================= floor + hysteresis
async def _run_floor_below_min_while_off_stays_off():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)                        # min_w = 6 A * 230 V = 1380 W
    assert abs(c.min_w - 1380.0) < 1e-6
    await c.modulate(1000.0, _T0)        # below the floor, and we're not delivering
    assert _values(hass) == [0.0]        # first command establishes a known state: 0
    assert c._commanded is False
    _NOW[0] = _T0 + timedelta(seconds=30)
    await c.modulate(1200.0, _NOW[0])    # still below the floor -> still off, no re-write
    assert _values(hass) == [0.0]


async def _run_floor_hysteresis_holds_then_drops():
    """Below min while ON: hold at min down to 0.6*min, only then drop to 0."""
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)                                   # floor 1380 W, hold-down bar 828 W
    await c.modulate(3000.0, _T0)                   # 13 A
    assert _values(hass) == [13.0] and c._commanded is True

    t = _T0 + timedelta(seconds=30)
    await c.modulate(1000.0, t)                     # < floor but >= 828 -> hold at the floor
    assert _values(hass) == [13.0, 6.0]
    assert c._commanded is True                     # session NOT dropped

    t += timedelta(seconds=30)
    await c.modulate(900.0, t)                      # still above the hold bar
    assert _values(hass) == [13.0, 6.0]             # same setpoint -> nothing written
    assert c._commanded is True

    t += timedelta(seconds=30)
    await c.modulate(800.0, t)                      # below 0.6 * floor -> genuinely stop
    assert _values(hass) == [13.0, 6.0, 0.0]
    assert c._commanded is False


async def _run_floor_hysteresis_upward_boundary():
    """The other direction across the same boundary: off stays off until the target
    reaches the floor itself (the hold band is not a start band)."""
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    await c.modulate(0.0, _T0)                      # establish off
    assert _values(hass) == [0.0]
    t = _T0 + timedelta(seconds=30)
    await c.modulate(1000.0, t)                     # inside the hold band, but we're OFF
    assert _values(hass) == [0.0]
    t += timedelta(seconds=30)
    await c.modulate(1379.0, t)                     # a whisker under the floor -> still off
    assert _values(hass) == [0.0]
    t += timedelta(seconds=30)
    await c.modulate(1380.0, t)                     # at the floor -> start
    assert _values(hass) == [0.0, 6.0]
    assert c._commanded is True


async def _run_floor_from_native_min_for_power_setpoint():
    """A W/kW setpoint publishes its own floor; that beats any amps-derived figure."""
    hass = FakeHass()
    _evse(hass, unit="W", mn=1400, mx=7400, step=1)
    c = _mk(hass)
    assert c.min_w == 1400.0
    await c.modulate(1300.0, _T0)      # below the device's own floor, off -> stay off
    assert _values(hass) == [0.0]


# ================================================================= write economy
async def _run_write_deadband_skip():
    # W setpoint so the 0.5 A deadband (= 115 W at 230 V, 1 phase) is expressible in the
    # entity's own units; with 1 A steps every real change already exceeds it.
    hass = FakeHass()
    _evse(hass, unit="W", mn=1400, mx=7400, step=1)
    c = _mk(hass)
    await c.modulate(3000.0, _T0)
    assert _values(hass) == [3000.0]
    t = _T0 + timedelta(seconds=30)
    await c.modulate(3050.0, t)                 # 50 W change < 115 W deadband -> skipped
    assert _values(hass) == [3000.0]
    assert "deadband" in c._note
    t += timedelta(seconds=30)
    await c.modulate(3200.0, t)                 # 200 W change -> worth a write
    assert _values(hass) == [3000.0, 3200.0]


async def _run_write_min_interval_skip():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    await c.modulate(3000.0, _T0)               # 13 A
    assert _values(hass) == [13.0]
    await c.modulate(4600.0, _T0 + timedelta(seconds=10))   # only 10 s since the last write
    assert _values(hass) == [13.0]
    assert "rate_limit" in c._note
    await c.modulate(4600.0, _T0 + timedelta(seconds=30))   # 30 s >= 20 s -> allowed
    assert _values(hass) == [13.0, 20.0]


async def _run_boundary_crossing_always_writes():
    """An on/off transition is safety-relevant, not a trim: neither the deadband nor the
    rate limit may delay it."""
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    await c.modulate(3000.0, _T0)
    assert _values(hass) == [13.0]
    await c.modulate(0.0, _T0 + timedelta(seconds=1))       # 1 s later -> still writes
    assert _values(hass) == [13.0, 0.0]
    await c.modulate(3000.0, _T0 + timedelta(seconds=2))    # and back on
    assert _values(hass) == [13.0, 0.0, 13.0]


# ================================================================= plug detection
def test_plug_states():
    hass = FakeHass()
    _evse(hass)
    assert _mk(hass).plugged_in() is None            # no plug entity configured
    c = _mk(hass, plug_entity_id="sensor.evse_status")
    assert c.plugged_in() is None                    # entity doesn't exist (yet)
    for state in ("available", "disconnected", "unplugged", "no_vehicle", "idle", "off",
                  "false", "faulted", "reserved"):
        hass.states.set("sensor.evse_status", state)
        assert c.plugged_in() is False, state
    for state in ("charging", "preparing", "suspendedev", "on", "true", "finishing"):
        hass.states.set("sensor.evse_status", state)
        assert c.plugged_in() is True, state
    # Fail OPEN: an entity outage is "don't know", never "unplugged".
    for state in ("unknown", "unavailable", "", "none"):
        hass.states.set("sensor.evse_status", state)
        assert c.plugged_in() is None, state


async def _run_unplugged_commands_zero():
    hass = FakeHass()
    _evse(hass, mx=32)
    hass.states.set("sensor.evse_status", "charging")
    c = _mk(hass, plug_entity_id="sensor.evse_status")
    await c.modulate(5000.0, _T0)
    assert _values(hass) == [22.0]
    hass.states.set("sensor.evse_status", "available")   # car unplugged
    await c.modulate(5000.0, _T0 + timedelta(seconds=30))
    assert _values(hass) == [22.0, 0.0]
    assert c._commanded is False


async def _run_unknown_plug_is_treated_as_plugged():
    """GridLens must never withhold charging because it couldn't confirm a plug."""
    hass = FakeHass()
    _evse(hass, mx=32)
    hass.states.set("sensor.evse_status", "unavailable")
    c = _mk(hass, plug_entity_id="sensor.evse_status")
    await c.modulate(3000.0, _T0)
    assert _values(hass) == [13.0]
    # Same for a plug entity that doesn't exist at all, and for none configured.
    hass2 = FakeHass()
    _evse(hass2, mx=32)
    c2 = _mk(hass2, plug_entity_id="binary_sensor.nope")
    await c2.modulate(3000.0, _T0)
    assert _values(hass2) == [13.0]
    hass3 = FakeHass()
    _evse(hass3, mx=32)
    c3 = _mk(hass3)
    await c3.modulate(3000.0, _T0)
    assert _values(hass3) == [13.0]


# ================================================================= reassert on connect
# Regression coverage for the incident this feature exists for (GRIDLENS_CHECKLIST.md
# 2026-09-11): the household's Wattpilot starts charging on its own the instant a car is
# plugged in, at whatever current it was last left at — GridLens's own target never
# changed, so write economy (correctly, by its own logic) never re-sent its "off" decision,
# and the session ran uncontrolled until a human noticed and forced it off by hand 28
# minutes later. Generic, not Wattpilot-specific: exercised here against both actuation
# shapes this controller supports (button pair, and a plain switch/setpoint device).
async def _run_reconnect_reasserts_off_via_stop_button():
    hass = FakeHass()
    _evse(hass, mx=32)
    hass.states.set("sensor.evse_status", "available")               # not connected yet
    c = _mk(
        hass, plug_entity_id="sensor.evse_status",
        start_button_entity_id="button.start", stop_button_entity_id="button.stop",
    )
    await c.modulate(0.0, _T0)                                       # first tick: establish off
    assert len(_presses(hass, "button.stop")) == 1
    assert c._commanded is False

    # Car plugs in; the plan still wants this device off (nothing about GridLens's own
    # decision changed). Without the fix this would be silently skipped — same "on/off
    # unchanged" no-op that let tonight's session run uncontrolled.
    hass.states.set("sensor.evse_status", "charging")
    t = _T0 + timedelta(seconds=30)
    await c.modulate(0.0, t)
    assert len(_presses(hass, "button.stop")) == 2, "did not reassert off on the connect edge"
    assert 0.0 not in _values(hass), "wrote 0 to a setpoint that would reject it"
    assert "reconnected" in c._note

    # And it does NOT re-fire on the next ordinary tick — only the edge itself forces it.
    await c.modulate(0.0, t + timedelta(seconds=30))
    assert len(_presses(hass, "button.stop")) == 2


async def _run_reconnect_forces_write_past_rate_limit():
    """Same edge, plain switch/setpoint device: the forced reassert must bypass the
    deadband/min-write-interval trim exactly like any other on/off crossing — a stale
    'nothing changed' skip is exactly the bug this exists to close."""
    hass = FakeHass()
    _evse(hass, mx=32)
    hass.states.set("switch.evse", "off")
    hass.states.set("sensor.evse_status", "available")
    c = _mk(hass, plug_entity_id="sensor.evse_status", switch_entity_id="switch.evse")
    await c.modulate(0.0, _T0)
    assert _values(hass) == [0.0]

    hass.states.set("sensor.evse_status", "charging")
    t = _T0 + timedelta(seconds=1)                                   # well inside the rate limit
    await c.modulate(0.0, t)
    assert _values(hass) == [0.0, 0.0], "reassert was skipped by the write-economy trim"
    assert "reconnected" in c._note


async def _run_already_plugged_at_startup_is_not_a_reconnect():
    """A restart with the car already connected must not read as 'just connected' — no
    prior confirmed-False reading means no edge, same fail-open posture as plugged_in()
    itself (see _plugged_in_prev's docstring)."""
    hass = FakeHass()
    _evse(hass, mx=32)
    hass.states.set("sensor.evse_status", "charging")                # already connected
    c = _mk(
        hass, plug_entity_id="sensor.evse_status",
        start_button_entity_id="button.start", stop_button_entity_id="button.stop",
    )
    await c.modulate(0.0, _T0)                                       # first tick only
    assert len(_presses(hass, "button.stop")) == 1
    await c.modulate(0.0, _T0 + timedelta(seconds=30))
    assert len(_presses(hass, "button.stop")) == 1, "startup misread as a fresh connect"


# ================================================================= manual override
async def _run_override_force_on_commands_cap():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)                       # cap_w = min(7400, 32 A -> 7360) = 7360
    assert abs(c.cap_w - 7360.0) < 1e-6
    await c.set_override(True, _T0)
    assert _values(hass) == [32.0]      # "maximum allowed current", not "close a relay"
    assert c.override is True
    assert c._commanded is True
    assert c.status()["modulation_source"] == "override"


async def _run_override_force_off_commands_zero():
    hass = FakeHass()
    _evse(hass, mx=32, state="16")
    hass.states.set("switch.evse", "on")
    c = _mk(hass, switch_entity_id="switch.evse")
    await c.set_override(False, _T0)
    assert _values(hass) == [0.0]
    assert len(_turn_offs(hass)) == 1               # companion switch de-energised after
    assert hass.services.calls[-1][:3] == ("switch", "turn_off", "switch.evse")
    assert c.override is False


async def _run_button_pair_presses_on_crossing_only():
    """A charger with no stateful switch and a setpoint that refuses 0 outright (the
    household's own Fronius Wattpilot, ha-wattpilot integration: max_charging_current has
    native_min_value=6, confirmed 2026-09-11) needs start/stop buttons instead of a switch.
    The start button fires once on the off->on crossing, never again on a plain ramp — a
    button has no readable state to gate on, so re-pressing "force start" every 30 s while
    already charging would be as bad as never pressing it at all."""
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass, start_button_entity_id="button.start", stop_button_entity_id="button.stop")
    await c.modulate(3000.0, _T0)                                    # off -> on: crossing
    assert len(_presses(hass, "button.start")) == 1
    assert _values(hass) == [13.0]
    t = _T0 + timedelta(seconds=30)
    await c.modulate(4000.0, t)                                      # still on: no crossing
    assert len(_presses(hass, "button.start")) == 1, "re-pressed start on a plain ramp"
    assert _values(hass) == [13.0, 17.0]


async def _run_button_pair_stop_skips_the_zero_write():
    """The whole reason this exists: an entity like ha-wattpilot's max_charging_current
    rejects a literal 0 (HA's number platform is expected to reject/clamp a set_value
    below native_min_value, not accept it). Turning off via the stop button must never
    even attempt that write — before this mechanism existed, the write raised and _write
    caught it *before* updating self._commanded, so GridLens believed it had turned the
    charger off while the hardware kept drawing at its last setpoint (GRIDLENS_CHECKLIST.md
    2026-09-11)."""
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass, start_button_entity_id="button.start", stop_button_entity_id="button.stop")
    await c.modulate(3000.0, _T0)                                    # on, 13 A
    await c.modulate(0.0, _T0 + timedelta(seconds=30))               # on -> off: crossing
    assert len(_presses(hass, "button.stop")) == 1
    assert 0.0 not in _values(hass), "wrote 0 to a setpoint that would reject it"
    assert c._commanded is False


async def _run_button_pair_repeated_off_does_not_repress_stop():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass, start_button_entity_id="button.start", stop_button_entity_id="button.stop")
    await c.modulate(3000.0, _T0)
    await c.modulate(0.0, _T0 + timedelta(seconds=30))
    await c.modulate(0.0, _T0 + timedelta(seconds=60))               # still off
    await c.modulate(1000.0, _T0 + timedelta(seconds=90))            # below floor -> stays off
    assert len(_presses(hass, "button.stop")) == 1


async def _run_button_pair_override_uses_buttons():
    hass = FakeHass()
    _evse(hass, mx=32, state="16")
    c = _mk(hass, start_button_entity_id="button.start", stop_button_entity_id="button.stop")
    await c.set_override(True, _T0)                                  # Force On
    assert len(_presses(hass, "button.start")) == 1
    assert _values(hass) == [32.0]
    await c.set_override(False, _T0 + timedelta(seconds=5))          # Force Off
    assert len(_presses(hass, "button.stop")) == 1
    assert 0.0 not in _values(hass)


async def _run_button_pair_takes_priority_over_switch():
    """If a load somehow has both wired (the config flow's both-or-neither validation
    doesn't stop a switch being set alongside a complete button pair), the button pair
    wins and the switch is left untouched — see _write_setpoint's elif."""
    hass = FakeHass()
    _evse(hass, mx=32)
    hass.states.set("switch.evse", "off")
    c = _mk(
        hass, switch_entity_id="switch.evse",
        start_button_entity_id="button.start", stop_button_entity_id="button.stop",
    )
    await c.modulate(3000.0, _T0)
    assert len(_presses(hass, "button.start")) == 1
    assert not _turn_ons(hass), "switch was actuated even though buttons are configured"


async def _run_modulate_inert_under_override():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    await c.set_override(True, _T0)
    n = len(hass.services.calls)
    await c.modulate(1000.0, _T0 + timedelta(seconds=30))
    await c.modulate(7000.0, _T0 + timedelta(seconds=60))
    await c.modulate(0.0, _T0 + timedelta(seconds=90))
    assert len(hass.services.calls) == n            # a human has it; hands off


async def _run_override_clear_returns_to_plan():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    await c.set_override(True, _T0)
    await c.set_override(None, _T0 + timedelta(seconds=30))
    assert c.override is None
    assert c._commanded is None                     # forces a clean re-establish
    assert _values(hass) == [32.0]                  # clearing itself writes nothing
    await c.modulate(3000.0, _T0 + timedelta(seconds=31))  # ...and the plan is obeyed at once
    assert _values(hass) == [32.0, 13.0]


# ================================================================= cap / user ceiling
async def _run_current_cap_narrows_envelope():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    c.set_current_cap_a(10.0)
    assert abs(c.cap_w - 2300.0) < 1e-6             # 10 A * 230 V beats both other caps
    await c.modulate(7000.0, _T0)
    assert _values(hass) == [10.0]
    c.set_current_cap_a(None)                       # unrestricted again
    assert abs(c.cap_w - 7360.0) < 1e-6
    await c.modulate(7000.0, _T0 + timedelta(seconds=30))
    assert _values(hass) == [10.0, 30.0]
    # The ceiling bounds what GridLens commands — it must not bound its own entity's range.
    assert c.native_max_a == 32.0


async def _run_cap_zero_means_unknown_not_zero_allowed():
    """No max_kw configured and the entity hasn't published its own max: the device must
    still charge, not be silently pinned off."""
    hass = FakeHass()
    _evse(hass, mx=None)
    c = _mk(hass, max_w=0.0)
    assert c.cap_w == 0.0
    await c.modulate(3000.0, _T0)
    assert _values(hass) == [13.0]


async def _run_cap_below_floor_commands_zero():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    c.set_current_cap_a(3.0)                        # 690 W, below the 1380 W floor
    await c.modulate(7000.0, _T0)
    assert _values(hass) == [0.0]                   # no feasible non-zero current


# ================================================================= state readback
def test_actual_state_reads_setpoint_not_switch():
    hass = FakeHass()
    _evse(hass, mx=32, state="16")
    c = _mk(hass)
    assert c._actual_state() is True
    hass.states.set("number.evse", "3", {"unit_of_measurement": "A", "min": 6, "max": 32})
    assert c._actual_state() is False               # below the floor = not delivering
    hass.states.set("number.evse", "0", {"unit_of_measurement": "A", "min": 6, "max": 32})
    assert c._actual_state() is False
    hass.states.set("number.evse", "unavailable", {"unit_of_measurement": "A", "max": 32})
    assert c._actual_state() is None
    # A companion switch can only veto.
    _evse(hass, mx=32, state="16")
    c2 = _mk(hass, switch_entity_id="switch.evse")
    hass.states.set("switch.evse", "off")
    assert c2._actual_state() is False
    hass.states.set("switch.evse", "unavailable")
    assert c2._actual_state() is True               # unreadable switch is ignored, not "off"


# ================================================================= greedy threshold
async def _run_export_surplus_bar_is_min_for_modulating():
    """A modulating load can absorb ANY surplus, so its bar is min_w — while the on/off
    load's bar stays the full draw. Both asserted so a regression in either is caught."""
    hass = FakeHass()
    _evse(hass, mx=32)
    mod = _mk(hass)                                 # 7.4 kW, floor 1380 W
    mod.set_greedy(True)
    assert mod._export_surplus_threshold_w() == mod.min_w
    await mod.apply(0.0, _T0, import_rate=0.5, export_rate=0.0, grid_power_w=-2000.0)
    assert mod.status()["greedy_reason"] == "export_surplus"
    assert mod._want_on is True

    # Below its own floor -> even a modulating load can't use it.
    mod2 = _mk(hass)
    mod2.set_greedy(True)
    await mod2.apply(0.0, _T0, import_rate=0.5, export_rate=0.0, grid_power_w=-1000.0)
    assert mod2.status()["greedy_reason"] is None

    # The UNCHANGED parent bar: an on/off load of the same size must NOT fire on 2 kW.
    hass2 = FakeHass()
    hass2.states.set("switch.x", "off")
    onoff = DeferrableLoadController(hass2, name="X", switch_entity_id="switch.x", max_w=7400.0)
    onoff.set_greedy(True)
    assert onoff._export_surplus_threshold_w() == 7400.0
    await onoff.apply(0.0, _T0, import_rate=0.5, export_rate=0.0, grid_power_w=-2000.0)
    assert len(_turn_ons(hass2)) == 0
    assert onoff.status()["greedy_reason"] is None


async def _run_apply_records_but_never_writes():
    """The 5-minute tick decides; only the fast loop writes (one writer = unbypassable
    write economy)."""
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    await c.apply(5000.0, _T0, import_rate=0.3, export_rate=0.1)
    assert hass.services.calls == []
    assert c._planned_w == 5000.0 and c._want_on is True
    assert c.status()["planned_w"] == 5000.0


# ================================================================= identity / status
def test_join_key_falls_back_to_setpoint():
    hass = FakeHass()
    _evse(hass, mx=32)
    switchless = _mk(hass)
    assert switchless.join_key == "number.evse"     # never "" — that would collide
    assert switchless.status()["switch"] == "number.evse"
    withswitch = _mk(hass, switch_entity_id="switch.evse")
    assert withswitch.join_key == "switch.evse"
    # And the on/off controller's key is unchanged.
    onoff = DeferrableLoadController(hass, name="P", switch_entity_id="switch.pool", max_w=2000.0)
    assert onoff.join_key == "switch.pool"
    # Regression 2026-09-11: join_key must stay the setpoint entity for a button-actuated,
    # switchless device — NOT fall through to the start button. Every card that pairs a
    # master switch/override-select/greedy-toggle to a device computes its own join key as
    # `d.switch_entity || d.setpoint_entity` from the deferrable_loads sensor attribute,
    # which never carries the button entities at all. Preferring a button here made the two
    # disagree, so no auxiliary entity ever matched and the Load Control card showed
    # permanently greyed-out buttons and "Not controlling" for the household's own Wattpilot.
    buttons = _mk(
        hass, start_button_entity_id="button.start", stop_button_entity_id="button.stop"
    )
    assert buttons.join_key == "number.evse"
    assert buttons.status()["switch"] == "number.evse"
    assert onoff.status()["control_type"] == "onoff"


def test_status_publishes_modulating_fields():
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    st = c.status()
    for key in ("control_type", "setpoint_entity", "setpoint_unit", "phases", "voltage",
                "min_w", "cap_w", "commanded_w", "commanded_setpoint", "plugged_in",
                "last_write", "modulation_source"):
        assert key in st, key
    assert st["control_type"] == "modulating"
    assert st["setpoint_entity"] == "number.evse"
    assert st["setpoint_unit"] == "a"
    assert st["phases"] == 1
    assert st["min_w"] == 1380.0
    assert st["cap_w"] == 7360.0
    assert st["modulation_source"] == "off"
    # The inherited surface must still be there — the manager, switch.py, select.py and
    # the card all key off it.
    for key in ("name", "switch", "max_w", "commanded", "override", "greedy",
                "greedy_respects_schedule", "greedy_forecast_surplus", "note"):
        assert key in st, key


# ================================================================= failure discipline
async def _run_failed_service_call_is_caught():
    hass = FakeHass()
    _evse(hass, mx=32)
    hass.services.fail = True
    c = _mk(hass)
    await c.modulate(3000.0, _T0)          # must not raise
    assert c._commanded is None            # a failed write doesn't claim success
    assert c._note.startswith("setpoint_error")
    assert c._last_setpoint is None
    # ...and an override write fails just as safely.
    await c.set_override(True, _T0)
    assert c._note.startswith("command_error")


# ================================================================= manager
def _mod_data(**over):
    data = {
        "deferrable_load_sensors": ["sensor.evse"],
        "deferrable_load_max_kw": [7.4],
        "deferrable_load_switches": [""],
        "deferrable_load_setpoint": ["number.evse"],
    }
    data.update(over)
    return data


def _mod_mgr(**over):
    """A manager with one switchless modulating 7.4 kW charger."""
    _reset_timers()
    hass = FakeHass()
    _evse(hass, mx=32)
    _POWER_SENSORS["sensor.evse"] = "sensor.evse_power"
    m = LoadControlManager(hass, FakeEntry(_mod_data(**over)))
    return m, hass


def _plan(import_rate=0.30, export_rate=0.10, dev_w=1000.0, start=None):
    return [DispatchInterval(
        start=(start or _T0) - timedelta(minutes=1), action=BatteryAction.SELF_USE,
        import_rate=import_rate, export_rate=export_rate, deferrable_w=[dev_w],
    )]


def test_manager_builds_modulating_controller():
    m, _hass = _mod_mgr()
    assert m.is_modulating(0) is True
    assert isinstance(m.controllers[0], ModulatingLoadController)
    assert m.controllers[0].join_key == "number.evse"
    assert m._device_power_sensors[0] == "sensor.evse_power"
    assert m.current_limits_a(0) == (DEFAULT_MIN_CHARGE_CURRENT_A, 32.0)


def test_manager_setpoint_wins_over_switch():
    """A setpoint plus a switch = modulating, with the switch demoted to a companion."""
    _reset_timers()
    hass = FakeHass()
    _evse(hass, mx=32)
    hass.states.set("switch.evse", "off")
    m = LoadControlManager(hass, FakeEntry(_mod_data(deferrable_load_switches=["switch.evse"])))
    assert m.is_modulating(0) is True
    assert m.controllers[0].switch_entity_id == "switch.evse"
    assert m.controllers[0].join_key == "switch.evse"


def test_manager_reads_modulating_config():
    m, _hass = _mod_mgr(**{
        "deferrable_load_setpoint_unit": ["kw"],
        "deferrable_load_phases": [3],
        "deferrable_load_voltage": [240.0],
        "deferrable_load_min_current": [10.0],
        "deferrable_load_plug_sensor": ["sensor.evse_status"],
        "deferrable_load_start_button": ["button.evse_start"],
        "deferrable_load_stop_button": ["button.evse_stop"],
    })
    c = m.controllers[0]
    assert c._unit() == "kw"
    assert c._phase_count() == 3
    assert c.voltage == 240.0
    assert c.min_current_a == 10.0
    assert c.plug_entity_id == "sensor.evse_status"
    assert c.start_button_entity_id == "button.evse_start"
    assert c.stop_button_entity_id == "button.evse_stop"


async def _run_manager_target_plan_only():
    m, _hass = _mod_mgr()
    m.set_plan(_plan(dev_w=1000.0), updated_at=_T0)
    target, source = await m._modulation_target_w(0, _T0)   # greedy off by default
    assert target == 1000.0 and source == "plan"


async def _run_manager_target_surplus():
    m, hass = _mod_mgr(grid_power_sensor="sensor.grid")
    m.set_plan(_plan(export_rate=0.0, dev_w=1000.0), updated_at=_T0)
    await m.set_greedy(0, True)
    hass.states.set("sensor.grid", "-2000")          # exporting 2 kW
    hass.states.set("sensor.evse_power", "1000")     # of which this device already eats 1 kW
    target, source = await m._modulation_target_w(0, _T0)
    # device_w - grid_w - _EXPORT_BIAS_W = 1000 - (-2000) - 150 (deliberate small-export
    # undershoot, added 2026-09-11 — see module-level _EXPORT_BIAS_W).
    assert target == 2850.0 and source == "surplus"

    # Regression 2026-09-11: while genuinely importing, surplus must claim less than the
    # device's current draw, not the full amount. Old formula (`max(0,-grid_w) + device_w`)
    # zeroed the import term instead of subtracting it, so it claimed the full 1 kW even
    # though 500 W of it was real, unbuffered grid import — the exact bug (surplus
    # "not accounting for" a real shortfall) found live against the household's own
    # Wattpilot. Only 500 W of the device's 1 kW draw is backed by real spare solar here
    # (device_w - grid_w = 1000 - 500); the rest is import. That 500 W ties the plan's own
    # 500 W allocation, so surplus doesn't exceed it and the plan term wins the max().
    m2, hass2 = _mod_mgr(grid_power_sensor="sensor.grid")
    m2.set_plan(_plan(export_rate=0.0, dev_w=500.0), updated_at=_T0)
    await m2.set_greedy(0, True)
    hass2.states.set("sensor.grid", "500")
    hass2.states.set("sensor.evse_power", "1000")
    target2, source2 = await m2._modulation_target_w(0, _T0)
    assert target2 == 500.0 and source2 == "plan"


async def _run_manager_target_surplus_below_floor():
    """Greedy condition #2's price bar is the user's Minimum Export Price, not a hard $0
    (added 2026-09-11). A 3c export with the floor set to 5c counts as "being wasted", so
    a modulating load ramps to soak it; with the floor at its 0 default nothing changes;
    an export above the floor is worth selling and is left alone."""
    m, hass = _mod_mgr(grid_power_sensor="sensor.grid", min_export_price=5.0)  # c/kWh
    m.set_plan(_plan(export_rate=0.03, dev_w=1000.0), updated_at=_T0)
    await m.set_greedy(0, True)
    hass.states.set("sensor.grid", "-2000")          # exporting 2 kW below the 5c floor
    hass.states.set("sensor.evse_power", "1000")
    target, source = await m._modulation_target_w(0, _T0)
    # device_w(1000) - grid_w(-2000) - _EXPORT_BIAS_W(150) = 2850, not a flat 3000 —
    # the deliberate small-export-over-small-import undershoot (added 2026-09-11).
    assert target == 2850.0 and source == "surplus", (target, source)

    m2, hass2 = _mod_mgr(grid_power_sensor="sensor.grid")   # floor left at 0 (disabled)
    m2.set_plan(_plan(export_rate=0.03, dev_w=1000.0), updated_at=_T0)
    await m2.set_greedy(0, True)
    hass2.states.set("sensor.grid", "-2000")
    hass2.states.set("sensor.evse_power", "1000")
    assert await m2._modulation_target_w(0, _T0) == (1000.0, "plan")

    m3, hass3 = _mod_mgr(grid_power_sensor="sensor.grid", min_export_price=5.0)
    m3.set_plan(_plan(export_rate=0.08, dev_w=1000.0), updated_at=_T0)  # 8c > 5c floor
    await m3.set_greedy(0, True)
    hass3.states.set("sensor.grid", "-2000")
    hass3.states.set("sensor.evse_power", "1000")
    assert await m3._modulation_target_w(0, _T0) == (1000.0, "plan")


async def _run_manager_target_forecast_surplus():
    """Greedy condition #3 must actually reach the setpoint on a modulating device.

    It is the one greedy condition with no live figure to meter against — it is
    forward-looking, evaluated once per 5-minute tick by apply(), which stashes the
    proportional power it wants in ``_greedy_forecast_target_w`` (already clamped into
    this device's envelope by ``_forecast_surplus_snap_w``). Before 2026-08-03
    ``_modulation_target_w`` implemented only conditions #1 and #2, so #3 was silently
    inert here: the card's badge would light up while the charger stayed at plan_w."""
    m, _hass = _mod_mgr()
    m.set_plan(_plan(dev_w=0.0), updated_at=_T0)     # plan wants nothing this slot
    await m.set_greedy(0, True)
    await m.set_greedy_forecast_surplus(0, True)
    c = m.controllers[0]
    c._greedy_reason = "forecast_surplus"            # as apply() would have set it
    c._greedy_forecast_target_w = c.cap_w            # ...alongside the target power
    target, source = await m._modulation_target_w(0, _T0)
    assert target == c.cap_w and source == "surplus", (target, source, c.cap_w)

    # A target below cap_w (a smaller forecast rate) reaches the setpoint proportionally,
    # not rounded up to the full envelope.
    c._greedy_forecast_target_w = c.cap_w / 2.0
    target2, source2 = await m._modulation_target_w(0, _T0)
    assert target2 == c.cap_w / 2.0 and source2 == "surplus"

    # Opt-in: the master greedy switch still gates it, even with a target stashed.
    m2, _h2 = _mod_mgr()
    m2.set_plan(_plan(dev_w=0.0), updated_at=_T0)
    await m2.set_greedy(0, False)
    m2.controllers[0]._greedy_reason = "forecast_surplus"
    m2.controllers[0]._greedy_forecast_target_w = m2.controllers[0].cap_w
    assert await m2._modulation_target_w(0, _T0) == (0.0, "off")

    # And Greedy Respects Schedule still confines it to the device's allowed window.
    m3, _h3 = _mod_mgr()
    m3.set_plan(_plan(dev_w=0.0), updated_at=_T0)
    await m3.set_greedy(0, True)
    await m3.set_greedy_respects_schedule(0, True)
    m3.controllers[0]._greedy_reason = "forecast_surplus"
    m3.controllers[0]._greedy_forecast_target_w = m3.controllers[0].cap_w
    m3._schedule_allows_now = _never_allowed
    assert await m3._modulation_target_w(0, _T0) == (0.0, "off")


async def _run_manager_target_ac_output_cap():
    """Live inverter AC output ceiling clamp (found 2026-09-12 against the household's
    own Sigenergy + Wattpilot — GRIDLENS_CHECKLIST.md): applied LAST, after plan/surplus/
    battery-priority, regardless of which term produced the pre-clamp target — and
    re-evaluated every 30s here, unlike ``fc_target_w`` which is only refreshed on the
    5-minute ``apply()`` tick. Numbers mirror the actual incident: PV alone had the
    ~10kW plant at its ceiling, the Wattpilot was already drawing 6.6kW, and the
    forecast-surplus condition (unaware of any of this) wanted the full envelope."""
    m, hass = _mod_mgr(
        grid_power_sensor="sensor.grid",
        load_power_sensor="sensor.load",
        max_ac_output_kw=10.0,
    )
    m.set_plan(_plan(dev_w=1000.0), updated_at=_T0)
    await m.set_greedy(0, True)
    await m.set_greedy_forecast_surplus(0, True)
    c = m.controllers[0]
    c._greedy_reason = "forecast_surplus"
    c._greedy_forecast_target_w = c.cap_w          # forecast wants the full envelope
    hass.states.set("sensor.load", "13500")        # whole-house consumption
    hass.states.set("sensor.grid", "3500")         # importing 3.5 kW
    hass.states.set("sensor.evse_power", "6600")   # this device's OWN current draw
    target, source = await m._modulation_target_w(0, _T0)
    # Plant output (load - grid) = 10000 W, exactly at the configured 10 kW ceiling ->
    # zero AC headroom, so the target is capped to the device's own current draw, not
    # the forecast's full-envelope figure.
    assert target == 6600.0 and source == "ac_output_cap", (target, source)

    # No ceiling configured (the default) -> a pure no-op, unaffected by any of this.
    m2, hass2 = _mod_mgr(grid_power_sensor="sensor.grid", load_power_sensor="sensor.load")
    m2.set_plan(_plan(dev_w=1000.0), updated_at=_T0)
    await m2.set_greedy(0, True)
    await m2.set_greedy_forecast_surplus(0, True)
    c2 = m2.controllers[0]
    c2._greedy_reason = "forecast_surplus"
    c2._greedy_forecast_target_w = c2.cap_w
    hass2.states.set("sensor.load", "13500")
    hass2.states.set("sensor.grid", "3500")
    hass2.states.set("sensor.evse_power", "6600")
    target2, source2 = await m2._modulation_target_w(0, _T0)
    assert target2 == c2.cap_w and source2 == "surplus"


async def _run_manager_target_ac_output_cap_credits_current_export():
    """Regression for a bug in the fix above, found live the same day it shipped: the
    household was exporting ~3.4kW (PV essentially at the plant's ~10kW cap) and the
    clamp throttled the Wattpilot DOWN anyway, because ``plant_output`` alone looked
    maxed. Redirecting power already being exported to a load doesn't need the plant to
    produce one extra watt — it must never be treated as unavailable. Numbers are the
    real incident's: 6.54kW house load, 3.385kW export, this device already drawing
    ~5.93kW, 10kW cap."""
    m, hass = _mod_mgr(
        grid_power_sensor="sensor.grid",
        load_power_sensor="sensor.load",
        max_ac_output_kw=10.0,
    )
    m.set_plan(_plan(dev_w=1000.0), updated_at=_T0)
    await m.set_greedy(0, True)
    await m.set_greedy_forecast_surplus(0, True)
    c = m.controllers[0]
    c._greedy_reason = "forecast_surplus"
    c._greedy_forecast_target_w = c.cap_w          # forecast wants the full envelope
    hass.states.set("sensor.load", "6540")         # whole-house consumption
    hass.states.set("sensor.grid", "-3385")        # EXPORTING 3.385 kW
    hass.states.set("sensor.evse_power", "5932")   # this device's own current draw
    target, source = await m._modulation_target_w(0, _T0)
    # Plant is producing 9925W (load - grid = 6540 - -3385), just 75W under the 10kW
    # ceiling — but 3385W of that is being wasted as export, all of it redirectable.
    # allowed = device_w + (cap - plant_output) + export = 5932 + 75 + 3385 = 9392,
    # comfortably above the forecast's full-envelope ask — so no clamping at all.
    assert target == c.cap_w and source == "surplus", (target, source, c.cap_w)


def test_greedy_reason_exposed_on_onoff_controller():
    """greedy_reason is public because _modulation_target_w reads it — but it must read
    identically on a plain on/off load, whose behaviour this feature did not change."""
    hass = FakeHass()
    c = DeferrableLoadController(hass, name="pool", switch_entity_id="switch.pool", max_w=1000.0)
    assert c.greedy_reason is None
    c.set_greedy(True)
    assert c._greedy_wants_on(0.0, None, None, None) is True   # free import
    assert c.greedy_reason == "import_free"
    assert c.status()["greedy_reason"] == "import_free"


async def _never_allowed(_index, _now):
    return False


async def _run_manager_target_free_import_takes_cap():
    m, hass = _mod_mgr(grid_power_sensor="sensor.grid")
    m.set_plan(_plan(import_rate=0.0, export_rate=0.10, dev_w=1000.0), updated_at=_T0)
    await m.set_greedy(0, True)
    hass.states.set("sensor.grid", "3000")           # importing — irrelevant, it's free
    target, source = await m._modulation_target_w(0, _T0)
    assert target == m.controllers[0].cap_w == 7360.0
    assert source == "surplus"


# ================================================================= battery discharge masking
# Found live 2026-09-11 against the household's own Sigenergy + Wattpilot: a battery
# discharging to hold grid flow near zero (its own self-consumption loop, or GridLens's own
# SELF_USE battery action) makes "no import" look exactly like free solar surplus. Sigenergy
# splits charge/discharge across two always-positive sensors rather than one signed one, so
# reading only the charge sensor (0 W while discharging) made both the live-surplus term and
# _battery_headroom_w() blind to a real, live discharge.
async def _run_manager_target_surplus_masked_by_battery_discharge():
    m, hass = _mod_mgr(
        grid_power_sensor="sensor.grid",
        battery_charge_power_sensor="sensor.batt_charge",
        battery_discharge_power_sensor="sensor.batt_discharge",
    )
    m.set_plan(_plan(export_rate=0.0, dev_w=1000.0), updated_at=_T0)
    await m.set_greedy(0, True)
    hass.states.set("sensor.grid", "0")               # looks perfectly balanced...
    hass.states.set("sensor.batt_charge", "0")
    hass.states.set("sensor.batt_discharge", "1000")  # ...only because the battery covers 1 kW
    hass.states.set("sensor.evse_power", "5000")
    target, source = await m._modulation_target_w(0, _T0)
    # Surplus term alone: device_w(5000) - grid_w(0) - discharge_w(1000) -
    # _EXPORT_BIAS_W(150) = 3850. Then the battery-priority correction (added
    # 2026-09-11, given its own bias 2026-09-12) relieves the *final* target by the same
    # live discharge again, plus _BATTERY_PRIORITY_BIAS_W(150): 3850 - 1000 - 150 = 2700 —
    # not double-counted against the same term twice, just applied once each to whichever
    # of plan_w/surplus_w actually won the max().
    assert target == 2700.0 and source == "battery_priority", (target, source)


async def _run_manager_target_battery_priority_below_plan():
    """The core case this correction exists for, found live 2026-09-11: plan_w assumed
    enough solar for both the EV and the battery, real solar fell short, and the battery
    alone absorbed the whole gap while the device kept its full plan-driven draw with
    nothing to pull it back. plan_w here is the *dominant* term (no greedy condition
    fires — export_rate above the waste ceiling), so this is the one case none of the
    other terms in this function can reach: only the post-max battery-priority
    correction can relieve the device below what the plan itself allocated."""
    m, hass = _mod_mgr(
        grid_power_sensor="sensor.grid",
        battery_charge_power_sensor="sensor.batt_charge",
        battery_discharge_power_sensor="sensor.batt_discharge",
    )
    m.set_plan(_plan(export_rate=0.20, dev_w=2100.0), updated_at=_T0)  # rate well above 0
    await m.set_greedy(0, True)
    hass.states.set("sensor.grid", "0")
    hass.states.set("sensor.batt_charge", "0")
    hass.states.set("sensor.batt_discharge", "562")   # the battery is covering the shortfall
    hass.states.set("sensor.evse_power", "2189")
    target, source = await m._modulation_target_w(0, _T0)
    assert target == 1388.0 and source == "battery_priority", (target, source)  # 2100-562-150


async def _run_manager_target_battery_priority_ignores_greedy_toggle():
    """The correction is a priority/safety rule, not an opportunistic add-on — it must
    still apply even with Greedy Consumption switched off for this device."""
    m, hass = _mod_mgr(
        grid_power_sensor="sensor.grid",
        battery_charge_power_sensor="sensor.batt_charge",
        battery_discharge_power_sensor="sensor.batt_discharge",
    )
    m.set_plan(_plan(export_rate=0.20, dev_w=2100.0), updated_at=_T0)
    # greedy left at its default-off state — no set_greedy(0, True) call.
    hass.states.set("sensor.grid", "0")
    hass.states.set("sensor.batt_charge", "0")
    hass.states.set("sensor.batt_discharge", "562")
    hass.states.set("sensor.evse_power", "2189")
    target, source = await m._modulation_target_w(0, _T0)
    assert target == 1388.0 and source == "battery_priority", (target, source)  # 2100-562-150


async def _run_manager_target_battery_priority_bias_applied_even_for_small_discharge():
    """Found live 2026-09-12: exact cancellation (pre-fix `target_w - discharge_w`) only
    drives discharge to zero in the limit, since every real tick lags the reading it's
    correcting against — a full hour of live household stats showed the battery still
    funding a residual ~0.25-0.5 kW the whole time. _BATTERY_PRIORITY_BIAS_W makes the
    correction over-shoot on purpose, even for a small discharge reading, so it settles
    toward a little export instead of chasing an exact zero it never quite reaches."""
    m, hass = _mod_mgr(
        grid_power_sensor="sensor.grid",
        battery_charge_power_sensor="sensor.batt_charge",
        battery_discharge_power_sensor="sensor.batt_discharge",
    )
    m.set_plan(_plan(export_rate=0.20, dev_w=2100.0), updated_at=_T0)
    hass.states.set("sensor.grid", "0")
    hass.states.set("sensor.batt_charge", "0")
    hass.states.set("sensor.batt_discharge", "50")    # a small, live discharge
    hass.states.set("sensor.evse_power", "2189")
    target, source = await m._modulation_target_w(0, _T0)
    assert target == 1900.0 and source == "battery_priority", (target, source)  # 2100-50-150


async def _run_manager_target_battery_priority_bias_floors_at_zero():
    """The bias must never push the correction negative -- max(0.0, ...) still floors the
    result at 0 (falls through to the function's own `target_w <= 0.0 -> (0.0, "off")`)
    even when discharge_w + the bias together exceed target_w."""
    m, hass = _mod_mgr(
        grid_power_sensor="sensor.grid",
        battery_charge_power_sensor="sensor.batt_charge",
        battery_discharge_power_sensor="sensor.batt_discharge",
    )
    m.set_plan(_plan(export_rate=0.20, dev_w=200.0), updated_at=_T0)
    hass.states.set("sensor.grid", "0")
    hass.states.set("sensor.batt_charge", "0")
    hass.states.set("sensor.batt_discharge", "100")   # 200 - 100 - 150(bias) would go negative
    hass.states.set("sensor.evse_power", "0")
    target, source = await m._modulation_target_w(0, _T0)
    assert target == 0.0 and source == "off", (target, source)


async def _run_manager_target_no_discharge_no_correction():
    """Battery idle or charging must leave plan_w untouched by this correction — it only
    ever fires on a live discharge, never as a general-purpose plan override."""
    m, hass = _mod_mgr(
        grid_power_sensor="sensor.grid",
        battery_charge_power_sensor="sensor.batt_charge",
        battery_discharge_power_sensor="sensor.batt_discharge",
    )
    m.set_plan(_plan(export_rate=0.20, dev_w=2100.0), updated_at=_T0)
    await m.set_greedy(0, True)
    hass.states.set("sensor.grid", "0")
    hass.states.set("sensor.batt_charge", "500")   # battery charging, not discharging
    hass.states.set("sensor.batt_discharge", "0")
    hass.states.set("sensor.evse_power", "2189")
    target, source = await m._modulation_target_w(0, _T0)
    assert target == 2100.0 and source == "plan", (target, source)


async def _run_manager_target_surplus_battery_charging_not_credited():
    """Asymmetry, added 2026-09-11 on explicit household instruction: battery *discharge*
    reduces this device's claim (test above), but battery *charging* must NOT inflate it —
    the battery gets first claim on genuine surplus, this device only sees what's left
    over, never a bonus for what the battery is currently absorbing.

    plan_w is set between the two possible answers so the two cases are actually
    distinguishable through the final max(plan_w, surplus_w): if charging were still
    (wrongly) credited, surplus would hit 2850 and *win* over the plan, handing the
    battery's own 2 kW to this device; correctly excluded, surplus is capped at 850 and
    the plan's own, smaller allocation is what governs instead."""
    m, hass = _mod_mgr(
        grid_power_sensor="sensor.grid",
        battery_charge_power_sensor="sensor.batt_charge",
        battery_discharge_power_sensor="sensor.batt_discharge",
    )
    m.set_plan(_plan(export_rate=0.0, dev_w=1500.0), updated_at=_T0)
    await m.set_greedy(0, True)
    hass.states.set("sensor.grid", "0")              # balanced...
    hass.states.set("sensor.batt_charge", "2000")     # ...because the battery is soaking up 2 kW
    hass.states.set("sensor.batt_discharge", "0")
    hass.states.set("sensor.evse_power", "1000")
    target, source = await m._modulation_target_w(0, _T0)
    # device_w(1000) - grid_w(0) - discharge_w(0) - _EXPORT_BIAS_W(150) = 850, capped below
    # plan_w(1500) — so the plan governs, proving surplus never claimed the battery's 2 kW.
    assert target == 1500.0 and source == "plan", (target, source)


async def _run_manager_target_surplus_no_battery_configured_unchanged():
    """No battery sensors configured must skip only the discharge correction — the
    export-bias margin still applies, it isn't battery-specific."""
    m, hass = _mod_mgr(grid_power_sensor="sensor.grid")
    m.set_plan(_plan(export_rate=0.0, dev_w=1000.0), updated_at=_T0)
    await m.set_greedy(0, True)
    hass.states.set("sensor.grid", "0")
    hass.states.set("sensor.evse_power", "5000")
    target, source = await m._modulation_target_w(0, _T0)
    assert target == 4850.0 and source == "surplus"  # 5000 - 0 - _EXPORT_BIAS_W(150)


async def _run_battery_headroom_unipolar_discharge_sensor():
    """_battery_headroom_w() must see a live discharge even when it's split onto a second,
    always-positive sensor rather than going negative on the charge sensor."""
    m, hass = _mod_mgr(
        battery_soc_sensor="sensor.soc",
        battery_charge_power_sensor="sensor.batt_charge",
        battery_discharge_power_sensor="sensor.batt_discharge",
        battery_max_discharge_rate=5.0,
    )
    hass.states.set("sensor.soc", "50")
    hass.states.set("sensor.batt_charge", "0")
    hass.states.set("sensor.batt_discharge", "1000")
    # Without the discharge sensor netted in, this would misread as "not discharging at
    # all" and report the full 5000 W rated rate as headroom.
    assert m._battery_headroom_w() == 4000.0


async def _run_battery_headroom_signed_single_sensor_unchanged():
    """A battery with one genuinely signed sensor (no discharge sensor configured) —
    the original, still-common shape — must behave exactly as before this fix."""
    m, hass = _mod_mgr(
        battery_soc_sensor="sensor.soc",
        battery_charge_power_sensor="sensor.batt_charge",
        battery_max_discharge_rate=5.0,
    )
    hass.states.set("sensor.soc", "50")
    hass.states.set("sensor.batt_charge", "-1000")  # signed: discharging 1 kW
    assert m._battery_headroom_w() == 4000.0


async def _run_manager_target_fails_closed():
    # (a) no grid power sensor configured at all -> surplus never engages.
    m, _hass = _mod_mgr()
    m.set_plan(_plan(export_rate=0.0, dev_w=1000.0), updated_at=_T0)
    await m.set_greedy(0, True)
    assert await m._modulation_target_w(0, _T0) == (1000.0, "plan")

    # (b) configured but unavailable / unparseable -> same.
    m2, hass2 = _mod_mgr(grid_power_sensor="sensor.grid")
    m2.set_plan(_plan(export_rate=0.0, dev_w=1000.0), updated_at=_T0)
    await m2.set_greedy(0, True)
    assert await m2._modulation_target_w(0, _T0) == (1000.0, "plan")   # never set
    hass2.states.set("sensor.grid", "unavailable")
    assert await m2._modulation_target_w(0, _T0) == (1000.0, "plan")
    hass2.states.set("sensor.grid", "not_a_number")
    assert await m2._modulation_target_w(0, _T0) == (1000.0, "plan")

    # (c) greedy off -> the surplus term never applies however much is spilling.
    m3, hass3 = _mod_mgr(grid_power_sensor="sensor.grid")
    m3.set_plan(_plan(export_rate=0.0, dev_w=1000.0), updated_at=_T0)
    hass3.states.set("sensor.grid", "-5000")
    assert await m3._modulation_target_w(0, _T0) == (1000.0, "plan")

    # (d) export price is positive -> spilling isn't free, no surplus term.
    m4, hass4 = _mod_mgr(grid_power_sensor="sensor.grid")
    m4.set_plan(_plan(export_rate=0.08, dev_w=1000.0), updated_at=_T0)
    await m4.set_greedy(0, True)
    hass4.states.set("sensor.grid", "-5000")
    assert await m4._modulation_target_w(0, _T0) == (1000.0, "plan")


async def _run_manager_target_respects_schedule_gate():
    week_9_to_17 = [[1 if 18 <= s < 34 else 0 for s in range(48)] for _ in range(7)]
    m, hass = _mod_mgr(grid_power_sensor="sensor.grid")
    hass.data[CONST.DOMAIN] = {f"{m.entry.entry_id}_deferrable_schedules":
                               _FakeScheduleStore({"sensor.evse": week_9_to_17})}
    m.set_plan(_plan(export_rate=0.0, dev_w=500.0, start=_T0.replace(hour=20)),
               updated_at=_T0.replace(hour=20))
    await m.set_greedy(0, True)
    await m.set_greedy_respects_schedule(0, True)
    hass.states.set("sensor.grid", "-3000")
    at_20 = _T0.replace(hour=20)
    assert await m._modulation_target_w(0, at_20) == (500.0, "plan")  # outside the window
    await m.set_greedy_respects_schedule(0, False)
    target, source = await m._modulation_target_w(0, at_20)
    # device_w(0, no power sensor configured) - grid_w(-3000) - _EXPORT_BIAS_W(150) = 2850.
    assert target == 2850.0 and source == "surplus"


class _FakeScheduleStore:
    def __init__(self, weeks=None):
        self._weeks = weeks or {}

    async def async_get(self, sensor_id):
        return self._weeks.get(sensor_id)


async def _run_manager_fast_timer_lifecycle():
    m, _hass = _mod_mgr()
    _NOW[0] = _T0
    m.set_plan(_plan(dev_w=5000.0), updated_at=_T0)
    await m.set_entitled(True)
    assert _INTERVALS == []                       # nothing enabled yet -> no fast timer
    await m.enable(0)
    assert len(_INTERVALS) == 1
    assert _INTERVALS[0]["delta"] == timedelta(seconds=MODULATION_INTERVAL_SECONDS)
    assert m._fast_cancel is not None
    await m.disable(0)
    assert _INTERVALS[0]["cancelled"] is True
    assert m._fast_cancel is None
    # Re-enable starts a fresh one rather than leaking the old.
    await m.enable(0)
    assert len(_INTERVALS) == 2 and m._fast_cancel is not None
    await m.set_entitled(False)                   # revocation also stops it
    assert _INTERVALS[1]["cancelled"] is True
    assert m._fast_cancel is None


async def _run_pre_feature_entry_has_no_fast_timer():
    """Backward-compatibility guarantee: an entry saved before this feature existed has
    none of the new keys and must behave exactly as it did — on/off only, no 30 s timer."""
    _reset_timers()
    hass = FakeHass()
    hass.states.set("switch.pool", "off")
    m = LoadControlManager(hass, FakeEntry({
        "deferrable_load_sensors": ["sensor.pool"],
        "deferrable_load_max_kw": [2.0],
        "deferrable_load_switches": ["switch.pool"],
    }))
    assert m._modulating == set()
    assert m.is_modulating(0) is False
    assert type(m.controllers[0]) is DeferrableLoadController
    assert m.current_limits_a(0) is None
    assert m.get_current_cap_a(0) is None
    assert await m.set_current_cap_a(0, 10.0) is False
    _NOW[0] = _T0
    m.set_plan([DispatchInterval(start=_T0 - timedelta(minutes=1),
                                action=BatteryAction.SELF_USE, deferrable_w=[2000.0])],
               updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    assert len(_turn_ons(hass)) == 1              # the on/off path still works, unchanged
    assert _INTERVALS == []                       # ...and no fast timer was ever created
    assert m._fast_cancel is None
    await m._fast_tick(_T0)                       # a stray fast tick is harmless
    assert len(hass.services.calls) == 1


async def _run_manager_fast_tick_drives_setpoint():
    m, hass = _mod_mgr()
    _NOW[0] = _T0
    m.set_plan(_plan(dev_w=5000.0), updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    # 5000 W / 230 V / 1 phase = 21.74 A, snapped to the entity's 1 A step -> 22 A.
    assert _values(hass) == [22.0], _values(hass)
    _NOW[0] = _T0 + timedelta(seconds=30)
    m.set_plan(_plan(dev_w=2300.0), updated_at=_NOW[0])
    await m._fast_tick(_NOW[0])
    # 2300 W / 230 V = exactly 10 A — a genuine change well past the 0.5 A deadband, and
    # 30 s is past min_write_interval_s, so the fast tick writes it.
    assert _values(hass) == [22.0, 10.0], _values(hass)


async def _run_manager_user_cap_reaches_controller():
    m, hass = _mod_mgr()
    _NOW[0] = _T0
    m.set_plan(_plan(dev_w=7000.0), updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    n = len(_values(hass))
    assert await m.set_current_cap_a(0, 10.0) is True
    assert m.get_current_cap_a(0) == 10.0
    assert len(_values(hass)) == n                # setting the cap writes nothing itself
    _NOW[0] = _T0 + timedelta(seconds=30)
    await m._fast_tick(_NOW[0])                   # ...it binds on the next fast tick
    assert _last_value(hass) == 10.0


async def _run_manager_fast_tick_skips_override():
    m, hass = _mod_mgr()
    _NOW[0] = _T0
    m.set_plan(_plan(dev_w=5000.0), updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    await m.set_override(0, "off")
    n = len(hass.services.calls)
    _NOW[0] = _T0 + timedelta(seconds=30)
    await m._fast_tick(_NOW[0])
    assert len(hass.services.calls) == n          # the fast loop leaves an override alone


async def _run_manager_fast_tick_stale_plan_no_writes():
    m, hass = _mod_mgr()
    _NOW[0] = _T0
    m.set_plan(_plan(dev_w=5000.0), updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    n = len(hass.services.calls)
    _NOW[0] = _T0 + timedelta(hours=2)            # plan is now 2 h old (max age 30 min)
    await m._fast_tick(_NOW[0])
    assert len(hass.services.calls) == n          # leave as-is: no write at all
    m._plan = None
    await m._fast_tick(_NOW[0])
    assert len(hass.services.calls) == n


async def _run_manager_hass_stop_leaves_setpoint_as_is():
    m, hass = _mod_mgr()
    _NOW[0] = _T0
    m.set_plan(_plan(dev_w=5000.0), updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)
    n = len(hass.services.calls)
    for _event, cb in hass.bus.listeners:
        await cb(None)
    assert len(hass.services.calls) == n          # never winds an EV mid-charge back to 0
    assert _last_value(hass) != 0.0
    assert m._cancel_timer is None and m._fast_cancel is None


async def _run_manager_bad_device_does_not_kill_timer():
    m, hass = _mod_mgr()
    _NOW[0] = _T0
    m.set_plan(_plan(dev_w=5000.0), updated_at=_T0)
    await m.set_entitled(True)
    await m.enable(0)

    async def _boom(*a, **k):
        raise RuntimeError("device exploded")

    m.controllers[0].modulate = _boom
    await m._fast_tick(_T0)                       # must not propagate
    # A failing service call is likewise swallowed by the controller itself.
    hass.services.fail = True
    m.controllers[0] = m.controllers[0]
    del m.controllers[0].modulate
    _NOW[0] = _T0 + timedelta(seconds=30)
    m.set_plan(_plan(dev_w=2300.0), updated_at=_NOW[0])
    await m._fast_tick(_NOW[0])


# ================================================================= KNOWN BUGS
# These assert MODULATING_CONTRACT.md, not current behaviour. See the report.
async def _bug_enable_writes_on_missing_plan():
    """CONTRACT §4: "_fast_tick: skip if plan is None/stale (leave as-is deadman, same as
    _tick)". LoadControlManager.enable() and set_override(clear) call _fast_tick_device()
    DIRECTLY, bypassing _fast_tick's plan guard — so flipping the master switch on before
    the first plan arrives (e.g. right after an HA restart) computes plan_w = 0 and writes
    setpoint 0 to the charger, cutting a charge in progress. That is a forced-off, which
    the deadman policy forbids outright."""
    m, hass = _mod_mgr()
    _NOW[0] = _T0
    await m.set_entitled(True)
    await m.enable(0)                             # no plan has been set at all
    assert hass.services.calls == [], f"wrote {hass.services.calls} with no plan"

    # Same via a stale plan (here the write is a spurious ON rather than an off, but it is
    # still actuation from a plan the manager has already judged untrustworthy).
    m2, hass2 = _mod_mgr()
    m2.set_plan(_plan(dev_w=5000.0), updated_at=_T0 - timedelta(hours=2))
    await m2.set_entitled(True)
    await m2.enable(0)
    assert hass2.services.calls == [], f"wrote {hass2.services.calls} on a stale plan"


async def _bug_force_on_with_unknown_cap_commands_zero():
    """CONTRACT §3.1.3 + §6: cap_w == 0 means "no ceiling knowable yet", never "0 W
    allowed" — modulate() honours that, but _actuate() (the override path) does not:
    Force On resolves to cap_w = 0, writes setpoint 0 and turns the companion switch OFF,
    i.e. a Force On performs a force off. Reachable when max_kw is missing from the entry
    (shorter than the sensor list) and the setpoint entity hasn't published its max."""
    hass = FakeHass()
    _evse(hass, mx=None)
    c = _mk(hass, max_w=0.0, switch_entity_id="switch.evse")
    hass.states.set("switch.evse", "off")
    await c.set_override(True, _T0)
    assert _last_value(hass) != 0.0, "Force On commanded 0 W"
    assert not _turn_offs(hass), "Force On turned the companion switch off"


async def _bug_force_on_ignores_user_cap_below_floor():
    """CONTRACT §6: the max-current entity is "a user ceiling on the current GridLens may
    command". With a ceiling below the device floor, modulate() correctly commands 0, but
    _actuate()'s Force On re-clamps up to the floor (_quantised_setpoint's `else lo`
    branch) and commands 6 A — above the ceiling the user explicitly set."""
    hass = FakeHass()
    _evse(hass, mx=32)
    c = _mk(hass)
    c.set_current_cap_a(3.0)                      # ceiling 3 A, floor 6 A
    await c.set_override(True, _T0)
    assert _last_value(hass) in (0.0, 3.0), f"commanded {_last_value(hass)} A above a 3 A cap"


# ================================================================= runner
if __name__ == "__main__":
    tests = [
        # units
        ("unit_inference_from_entity", test_unit_inference_from_entity),
        ("unit_fallback_not_cached", test_unit_fallback_not_cached_until_entity_readable),
        ("explicit_setpoint_unit_wins", test_explicit_setpoint_unit_wins),
        # phases
        ("phase_autoderive_1p_vs_3p", test_phase_autoderive_single_vs_three),
        ("phase_fallback_unreadable_max", test_phase_fallback_when_entity_max_unreadable),
        ("explicit_phases_win", test_explicit_phases_win_and_clamp),
        ("voltage_default_and_override", test_voltage_default_and_override),
        # conversion
        ("target_w_to_setpoint_pure", test_target_w_to_setpoint_is_pure),
        ("quantise_to_entity_step", test_quantise_to_entity_step),
        # floor + hysteresis
        ("floor_below_min_while_off", lambda: _run_async(_run_floor_below_min_while_off_stays_off)),
        ("floor_hysteresis_hold_then_drop", lambda: _run_async(_run_floor_hysteresis_holds_then_drops)),
        ("floor_hysteresis_upward", lambda: _run_async(_run_floor_hysteresis_upward_boundary)),
        ("floor_from_native_min", lambda: _run_async(_run_floor_from_native_min_for_power_setpoint)),
        # write economy
        ("write_deadband_skip", lambda: _run_async(_run_write_deadband_skip)),
        ("write_min_interval_skip", lambda: _run_async(_run_write_min_interval_skip)),
        ("boundary_crossing_always_writes", lambda: _run_async(_run_boundary_crossing_always_writes)),
        # plug
        ("plug_states", test_plug_states),
        ("unplugged_commands_zero", lambda: _run_async(_run_unplugged_commands_zero)),
        ("unknown_plug_is_plugged", lambda: _run_async(_run_unknown_plug_is_treated_as_plugged)),
        # reassert on connect
        ("reconnect_reasserts_off_via_stop_button", lambda: _run_async(_run_reconnect_reasserts_off_via_stop_button)),
        ("reconnect_forces_write_past_rate_limit", lambda: _run_async(_run_reconnect_forces_write_past_rate_limit)),
        ("already_plugged_at_startup_not_reconnect", lambda: _run_async(_run_already_plugged_at_startup_is_not_a_reconnect)),
        # override
        ("override_force_on_is_cap", lambda: _run_async(_run_override_force_on_commands_cap)),
        ("override_force_off_is_zero", lambda: _run_async(_run_override_force_off_commands_zero)),
        ("modulate_inert_under_override", lambda: _run_async(_run_modulate_inert_under_override)),
        ("override_clear_returns_to_plan", lambda: _run_async(_run_override_clear_returns_to_plan)),
        # button-pair actuation (no stateful switch, and/or a setpoint that refuses 0)
        ("button_pair_crossing_only", lambda: _run_async(_run_button_pair_presses_on_crossing_only)),
        ("button_pair_skips_zero_write", lambda: _run_async(_run_button_pair_stop_skips_the_zero_write)),
        ("button_pair_no_repress_off", lambda: _run_async(_run_button_pair_repeated_off_does_not_repress_stop)),
        ("button_pair_override", lambda: _run_async(_run_button_pair_override_uses_buttons)),
        ("button_pair_beats_switch", lambda: _run_async(_run_button_pair_takes_priority_over_switch)),
        # cap
        ("current_cap_narrows", lambda: _run_async(_run_current_cap_narrows_envelope)),
        ("cap_zero_means_unknown", lambda: _run_async(_run_cap_zero_means_unknown_not_zero_allowed)),
        ("cap_below_floor_is_zero", lambda: _run_async(_run_cap_below_floor_commands_zero)),
        # readback / greedy / identity
        ("actual_state_reads_setpoint", test_actual_state_reads_setpoint_not_switch),
        ("export_bar_min_vs_max", lambda: _run_async(_run_export_surplus_bar_is_min_for_modulating)),
        ("apply_records_never_writes", lambda: _run_async(_run_apply_records_but_never_writes)),
        ("join_key_falls_back_to_setpoint", test_join_key_falls_back_to_setpoint),
        ("status_publishes_modulating", test_status_publishes_modulating_fields),
        ("failed_service_call_caught", lambda: _run_async(_run_failed_service_call_is_caught)),
        # manager
        ("manager_builds_modulating", test_manager_builds_modulating_controller),
        ("manager_setpoint_beats_switch", test_manager_setpoint_wins_over_switch),
        ("manager_reads_modulating_config", test_manager_reads_modulating_config),
        ("manager_target_plan_only", lambda: _run_async(_run_manager_target_plan_only)),
        ("manager_target_surplus", lambda: _run_async(_run_manager_target_surplus)),
        ("manager_target_surplus_below_floor", lambda: _run_async(_run_manager_target_surplus_below_floor)),
        ("manager_target_free_import", lambda: _run_async(_run_manager_target_free_import_takes_cap)),
        ("surplus_masked_by_battery_discharge", lambda: _run_async(_run_manager_target_surplus_masked_by_battery_discharge)),
        ("battery_priority_below_plan", lambda: _run_async(_run_manager_target_battery_priority_below_plan)),
        ("battery_priority_ignores_greedy_toggle", lambda: _run_async(_run_manager_target_battery_priority_ignores_greedy_toggle)),
        ("battery_priority_bias_small_discharge", lambda: _run_async(_run_manager_target_battery_priority_bias_applied_even_for_small_discharge)),
        ("battery_priority_bias_floors_at_zero", lambda: _run_async(_run_manager_target_battery_priority_bias_floors_at_zero)),
        ("no_discharge_no_correction", lambda: _run_async(_run_manager_target_no_discharge_no_correction)),
        ("surplus_battery_charging_not_credited", lambda: _run_async(_run_manager_target_surplus_battery_charging_not_credited)),
        ("surplus_no_battery_unchanged", lambda: _run_async(_run_manager_target_surplus_no_battery_configured_unchanged)),
        ("battery_headroom_unipolar_discharge", lambda: _run_async(_run_battery_headroom_unipolar_discharge_sensor)),
        ("battery_headroom_signed_unchanged", lambda: _run_async(_run_battery_headroom_signed_single_sensor_unchanged)),
        ("manager_target_forecast_surplus", lambda: _run_async(_run_manager_target_forecast_surplus)),
        ("manager_target_ac_output_cap", lambda: _run_async(_run_manager_target_ac_output_cap)),
        ("manager_target_ac_output_cap_credits_current_export", lambda: _run_async(_run_manager_target_ac_output_cap_credits_current_export)),
        ("greedy_reason_on_onoff_controller", test_greedy_reason_exposed_on_onoff_controller),
        ("manager_target_fails_closed", lambda: _run_async(_run_manager_target_fails_closed)),
        ("manager_target_schedule_gate", lambda: _run_async(_run_manager_target_respects_schedule_gate)),
        ("manager_fast_timer_lifecycle", lambda: _run_async(_run_manager_fast_timer_lifecycle)),
        ("pre_feature_entry_no_fast_timer", lambda: _run_async(_run_pre_feature_entry_has_no_fast_timer)),
        ("manager_fast_tick_drives_setpoint", lambda: _run_async(_run_manager_fast_tick_drives_setpoint)),
        ("manager_user_cap_reaches_controller", lambda: _run_async(_run_manager_user_cap_reaches_controller)),
        ("manager_fast_tick_skips_override", lambda: _run_async(_run_manager_fast_tick_skips_override)),
        ("manager_stale_plan_no_writes", lambda: _run_async(_run_manager_fast_tick_stale_plan_no_writes)),
        ("manager_hass_stop_leaves_as_is", lambda: _run_async(_run_manager_hass_stop_leaves_setpoint_as_is)),
        ("manager_bad_device_survives", lambda: _run_async(_run_manager_bad_device_does_not_kill_timer)),
    ]
    # Regression tests for three bugs this suite found on 2026-08-03, all since fixed.
    # They stay first-class checks, not a "known bugs" annex — each one is a silent,
    # user-visible failure with no error trail (a Force On that forces off; a master
    # switch that cuts a manual charge).
    tests += [
        ("regr_enable_no_write_without_plan", lambda: _run_async(_bug_enable_writes_on_missing_plan)),
        ("regr_force_on_unknown_cap_charges", lambda: _run_async(_bug_force_on_with_unknown_cap_commands_zero)),
        ("regr_force_on_respects_user_cap", lambda: _run_async(_bug_force_on_ignores_user_cap_below_floor)),
    ]
    known_bugs = []

    passed, failures = 0, []
    for name, fn in tests:
        _NOW[0] = _T0
        _reset_timers()
        try:
            fn()
        except Exception as err:  # noqa: BLE001
            print(f"FAIL {name}: {err}")
            traceback.print_exc()
            failures.append(name)
            continue
        print(f"ok   {name}")
        passed += 1

    xfail, xpass = [], []
    for name, fn, note in known_bugs:
        _NOW[0] = _T0
        _reset_timers()
        try:
            fn()
        except AssertionError as err:
            print(f"XFAIL {name}: {note} -- {err}")
            xfail.append(name)
            continue
        except Exception as err:  # noqa: BLE001 — an unexpected error is still a finding
            print(f"XFAIL {name}: {note} -- raised {err!r}")
            xfail.append(name)
            continue
        print(f"XPASS {name}: FIXED? promote this into the main list")
        xpass.append(name)

    print(f"\n{passed}/{len(tests)} passed")
    if xfail:
        print(f"{len(xfail)} known bug(s) still failing (expected): {', '.join(xfail)}")
    if xpass:
        print(f"{len(xpass)} known bug(s) now passing: {', '.join(xpass)}")
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
