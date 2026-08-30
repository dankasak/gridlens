"""Offline tests for GreedyEnergyTracker (greedy_energy.py) — the HA-wired layer around
greedy_energy_math.py's pure accumulate() logic: reading/restoring persisted state, unit
conversion (Wh vs kWh), and gating each accepted delta on a controller's live
``greedy_reason`` at the moment its source energy sensor changes. Mirrors
test_load_estimator.py's HA-stub-and-drive-internals approach — no real homeassistant
package needed.

Run: python3 tests/test_greedy_energy.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_stubs() -> None:
    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    ha = _mod("homeassistant")
    core = _mod("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    ha.core = core

    helpers = _mod("homeassistant.helpers")

    event = _mod("homeassistant.helpers.event")
    event.async_track_state_change_event = lambda hass, eids, cb: (lambda: None)
    helpers.event = event

    storage = _mod("homeassistant.helpers.storage")
    storage.Store = type("Store", (), {})  # never instantiated in these tests
    helpers.storage = storage
    ha.helpers = helpers


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
    pkg = types.ModuleType("gl")
    pkg.__path__ = []
    sys.modules["gl"] = pkg
    _load(os.path.join(_COMPONENT, "const.py"), "gl.const", package="gl")
    mod = _load(os.path.join(_COMPONENT, "greedy_energy_math.py"), "gl.greedy_energy_math", package="gl")
    sys.modules["greedy_energy_math"] = mod  # greedy_energy.py imports it as a top-level sibling
    ge = _load(os.path.join(_COMPONENT, "greedy_energy.py"), "gl.greedy_energy", package="gl")
    return ge.GreedyEnergyTracker


GreedyEnergyTracker = _bootstrap()

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


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


class FakeHass:
    def __init__(self):
        self.states = FakeStates()


class FakeStore:
    """Duck-types GreedyEnergyStore's async_get/async_set — the tracker never touches
    anything else on it."""

    def __init__(self, seed: dict | None = None):
        self._data = dict(seed or {})
        self.saved = {}

    async def async_get(self, key):
        return dict(self._data.get(key) or {})

    async def async_set(self, key, value):
        self._data[key] = value
        self.saved[key] = value


class FakeController:
    def __init__(self, greedy_reason=None):
        self.greedy_reason = greedy_reason


class FakeEvent:
    def __init__(self, new_state):
        self.data = {"new_state": new_state}


def _tracker(hass, controller, store=None, source="sensor.device_energy", index=0):
    return GreedyEnergyTracker(
        hass, store=store or FakeStore(), index=index, name="Test Device",
        source_sensor_id=source, controller=controller,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ----------------------------------------------------------------- tests
def test_fresh_tracker_seeds_baseline_from_live_state_with_nothing_persisted():
    hass = FakeHass()
    hass.states.set("sensor.device_energy", "5.0", {"unit_of_measurement": "kWh"})
    controller = FakeController()
    t = _tracker(hass, controller)
    _run(t.async_load())
    check("running_kwh starts at 0", t.running_kwh == 0.0)
    check("baseline seeded from the live reading, not left None",
          t._last_value == 5.0)


def test_async_load_restores_persisted_state():
    hass = FakeHass()
    controller = FakeController()
    store = FakeStore(seed={"0": {"running_kwh": 3.25, "last_value": 12.0}})
    t = _tracker(hass, controller, store=store)
    _run(t.async_load())
    check("running_kwh restored from the store", t.running_kwh == 3.25)
    check("last_value restored from the store", t._last_value == 12.0)


def test_delta_counted_while_greedy():
    hass = FakeHass()
    controller = FakeController(greedy_reason=None)
    store = FakeStore(seed={"0": {"running_kwh": 0.0, "last_value": 10.0}})
    t = _tracker(hass, controller, store=store)
    _run(t.async_load())

    controller.greedy_reason = "import_free"
    _run(t._on_source_change(FakeEvent(FakeState("11.5", {"unit_of_measurement": "kWh"}))))
    check("delta added while greedy_reason is set", t.running_kwh == 1.5)
    check("baseline advances to the new reading", t._last_value == 11.5)
    check("the accepted delta is persisted", store.saved["0"]["running_kwh"] == 1.5)


def test_delta_ignored_while_not_greedy():
    hass = FakeHass()
    controller = FakeController(greedy_reason=None)
    store = FakeStore(seed={"0": {"running_kwh": 0.0, "last_value": 10.0}})
    t = _tracker(hass, controller, store=store)
    _run(t.async_load())

    _run(t._on_source_change(FakeEvent(FakeState("11.5", {"unit_of_measurement": "kWh"}))))
    check("no energy added while greedy_reason is None", t.running_kwh == 0.0)
    check("baseline still advances so the NEXT delta measures forward from here",
          t._last_value == 11.5)
    check("nothing persisted for a zero-contribution delta", "0" not in store.saved)


def test_only_the_reading_at_change_time_matters_not_history():
    # Coarse, event-driven attribution (documented approximation): whatever
    # greedy_reason reads WHEN the new state arrives decides the whole delta, not a
    # continuous integral. Flip greedy on partway through — the delta since the last
    # reading still counts in full.
    hass = FakeHass()
    controller = FakeController(greedy_reason=None)
    store = FakeStore(seed={"0": {"running_kwh": 0.0, "last_value": 10.0}})
    t = _tracker(hass, controller, store=store)
    _run(t.async_load())

    controller.greedy_reason = "export_surplus"
    _run(t._on_source_change(FakeEvent(FakeState("10.8", {"unit_of_measurement": "kWh"}))))
    check("full delta attributed once greedy_reason is truthy at update time",
          abs(t.running_kwh - 0.8) < 1e-9, detail=f"got {t.running_kwh}")


def test_wh_source_sensor_is_converted_to_kwh():
    hass = FakeHass()
    controller = FakeController(greedy_reason="forecast_surplus")
    # last_value is always stored already-normalized to kWh (see _read_kwh) — 1.0 here
    # means "1000 Wh so far", matching the raw sensor's own unit at seed time.
    store = FakeStore(seed={"0": {"running_kwh": 0.0, "last_value": 1.0}})
    t = _tracker(hass, controller, store=store)
    _run(t.async_load())

    _run(t._on_source_change(FakeEvent(FakeState("2500", {"unit_of_measurement": "Wh"}))))
    check("Wh delta converted to kWh (1500Wh -> 1.5kWh)",
          abs(t.running_kwh - 1.5) < 1e-9, detail=f"got {t.running_kwh}")


def test_counter_reset_is_discarded_and_resyncs_baseline():
    hass = FakeHass()
    controller = FakeController(greedy_reason="import_free")
    store = FakeStore(seed={"0": {"running_kwh": 2.0, "last_value": 10.0}})
    t = _tracker(hass, controller, store=store)
    _run(t.async_load())

    _run(t._on_source_change(FakeEvent(FakeState("1.0", {"unit_of_measurement": "kWh"}))))
    check("a backwards reading is not subtracted from running_kwh", t.running_kwh == 2.0)
    check("baseline resyncs to the new (lower) reading so the next delta is measured forward",
          t._last_value == 1.0)


def test_unavailable_reading_is_ignored():
    hass = FakeHass()
    controller = FakeController(greedy_reason="import_free")
    store = FakeStore(seed={"0": {"running_kwh": 0.0, "last_value": 10.0}})
    t = _tracker(hass, controller, store=store)
    _run(t.async_load())

    _run(t._on_source_change(FakeEvent(FakeState("unavailable"))))
    check("an unavailable reading changes nothing", t.running_kwh == 0.0)
    check("baseline is left untouched by an unreadable state", t._last_value == 10.0)


def test_stop_cancels_the_listener():
    hass = FakeHass()
    controller = FakeController()
    t = _tracker(hass, controller)
    cancelled = []
    t._cancel_listener = lambda: cancelled.append(True)
    t.stop()
    check("stop() invokes the cancel callback", cancelled == [True])
    check("stop() clears the cancel handle", t._cancel_listener is None)


if __name__ == "__main__":
    for fn in [
        test_fresh_tracker_seeds_baseline_from_live_state_with_nothing_persisted,
        test_async_load_restores_persisted_state,
        test_delta_counted_while_greedy,
        test_delta_ignored_while_not_greedy,
        test_only_the_reading_at_change_time_matters_not_history,
        test_wh_source_sensor_is_converted_to_kwh,
        test_counter_reset_is_discarded_and_resyncs_baseline,
        test_unavailable_reading_is_ignored,
        test_stop_cancels_the_listener,
    ]:
        fn()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all greedy-energy tests passed.")
