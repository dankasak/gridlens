"""Offline tests for LoadEstimator (load_estimation.py) — the HA-wired layer around
load_estimate_math.py's pure sampling logic: on/off state reading (switch vs climate),
current_power_w (backs GridLensEstimatedPowerSensor), the contamination guard, the
track_energy=False mode used for a device that already has its own real energy sensor
(__init__.py._ensure_load_estimators's "step 2" — power-only inference), and restoring
persisted state. Mirrors test_deferrable_load_control.py's HA-stub-and-drive-internals
approach — no real homeassistant/scipy needed.

Run: python3 tests/test_load_estimator.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_T0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
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

    helpers = _mod("homeassistant.helpers")

    event = _mod("homeassistant.helpers.event")
    event.async_call_later = lambda hass, delay, cb: (lambda: None)
    event.async_track_state_change_event = lambda hass, eids, cb: (lambda: None)
    event.async_track_time_interval = lambda hass, cb, interval: (lambda: None)
    helpers.event = event

    storage = _mod("homeassistant.helpers.storage")
    storage.Store = type("Store", (), {})  # never instantiated in these tests
    helpers.storage = storage
    ha.helpers = helpers

    util = _mod("homeassistant.util")
    dt = _mod("homeassistant.util.dt")
    dt.now = lambda: _NOW[0]

    def _parse_datetime(s):
        return datetime.fromisoformat(s) if s else None

    dt.parse_datetime = _parse_datetime
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
    pkg = types.ModuleType("gl")
    pkg.__path__ = []
    sys.modules["gl"] = pkg
    _load(os.path.join(_COMPONENT, "const.py"), "gl.const", package="gl")
    mod = _load(os.path.join(_COMPONENT, "load_estimate_math.py"), "gl.load_estimate_math", package="gl")
    sys.modules["load_estimate_math"] = mod  # load_estimation.py imports it as a top-level sibling
    le = _load(os.path.join(_COMPONENT, "load_estimation.py"), "gl.load_estimation", package="gl")
    return le.LoadEstimator


LoadEstimator = _bootstrap()


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
    def __init__(self, seed: dict | None = None):
        self._data = dict(seed or {})
        self.saved = {}

    async def async_get(self, key):
        return dict(self._data.get(key) or {})

    async def async_set(self, key, value):
        self._data[key] = value
        self.saved[key] = value


def _estimator(hass, store=None, **kw):
    defaults = dict(
        store=store or FakeStore(),
        store_key="k1",
        name="Test Device",
        control_entity_id="switch.x",
        seed_kw=1.2,
        auto_refine=True,
        load_power_sensor="sensor.house_load",
        other_control_entity_ids=[],
    )
    defaults.update(kw)
    return LoadEstimator(hass, **defaults)


def _run(coro):
    asyncio.new_event_loop().run_until_complete(coro())


# ----------------------------------------------------------------- current_power_w
def test_current_power_w_switch_domain():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    e = _estimator(hass, control_entity_id="switch.x")
    assert e.current_power_w == 0.0
    hass.states.set("switch.x", "on")
    assert e.current_power_w == e.estimated_w == 1200.0


def test_current_power_w_climate_domain():
    hass = FakeHass()
    hass.states.set("climate.ac", "off")
    e = _estimator(hass, control_entity_id="climate.ac")
    assert e.current_power_w == 0.0
    hass.states.set("climate.ac", "cool")  # any non-off hvac_mode counts as on
    assert e.current_power_w == e.estimated_w


def test_current_power_w_unknown_state_is_zero_not_a_guess():
    hass = FakeHass()
    hass.states.set("switch.x", "unavailable")
    e = _estimator(hass)
    assert e.current_power_w == 0.0


# ----------------------------------------------------------------- sampling / EMA wiring
async def _run_accepted_sample_updates_estimate_and_persists():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("sensor.house_load", "500", {"unit_of_measurement": "W"})
    store = FakeStore()
    e = _estimator(hass, store=store)
    await e._arm_sample(_T0)
    hass.states.set("switch.x", "on")
    hass.states.set("sensor.house_load", "1700", {"unit_of_measurement": "W"})  # +1200W, plausible for a 1.2kW seed
    await e._on_settle(_T0 + timedelta(minutes=3))
    assert e.sample_count == 1
    assert e.last_sample_delta_w == 1200.0
    # EMA moved from the 1200W seed toward the 1200W sample — stays at 1200 (no-op here,
    # but proves the accept path ran and persisted).
    assert store.saved["k1"]["sample_count"] == 1


async def _run_contaminated_sample_is_discarded():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("switch.other", "off")
    hass.states.set("sensor.house_load", "500", {"unit_of_measurement": "W"})
    e = _estimator(hass, other_control_entity_ids=["switch.other"])
    await e._arm_sample(_T0)
    # Simulate the other device actually flipping on during the settle window.
    e._on_other_state_change(types.SimpleNamespace(data={
        "old_state": FakeState("off"),
        "new_state": FakeState("on"),
    }))
    hass.states.set("switch.x", "on")
    hass.states.set("sensor.house_load", "1700", {"unit_of_measurement": "W"})
    await e._on_settle(_T0 + timedelta(minutes=3))
    assert e.sample_count == 0  # discarded, contamination guard fired


async def _run_other_device_attribute_only_update_does_not_contaminate():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("climate.other", "off")
    hass.states.set("sensor.house_load", "500", {"unit_of_measurement": "W"})
    e = _estimator(hass, other_control_entity_ids=["climate.other"])
    await e._arm_sample(_T0)
    # Simulate the other device's state object updating (e.g. a temperature poll)
    # while its .state value ("off") stays the same — not a real on/off transition.
    e._on_other_state_change(types.SimpleNamespace(data={
        "old_state": FakeState("off", {"current_temperature": 21.0}),
        "new_state": FakeState("off", {"current_temperature": 21.5}),
    }))
    hass.states.set("switch.x", "on")
    hass.states.set("sensor.house_load", "1700", {"unit_of_measurement": "W"})
    await e._on_settle(_T0 + timedelta(minutes=3))
    assert e.sample_count == 1  # not contaminated — no real state change occurred


async def _run_device_off_at_settle_is_discarded():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("sensor.house_load", "500", {"unit_of_measurement": "W"})
    e = _estimator(hass)
    await e._arm_sample(_T0)
    hass.states.set("sensor.house_load", "1700", {"unit_of_measurement": "W"})
    # Device turned back off before the settle window elapsed.
    await e._on_settle(_T0 + timedelta(minutes=3))
    assert e.sample_count == 0


async def _run_implausible_delta_is_discarded():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("sensor.house_load", "500", {"unit_of_measurement": "W"})
    e = _estimator(hass, seed_kw=1.2)  # ceiling = max(1.2*1000*4, 6000) = 6000W
    await e._arm_sample(_T0)
    hass.states.set("switch.x", "on")
    hass.states.set("sensor.house_load", str(500 + 9000), {"unit_of_measurement": "W"})  # way over ceiling
    await e._on_settle(_T0 + timedelta(minutes=3))
    assert e.sample_count == 0


async def _run_no_load_power_sensor_never_arms():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    e = _estimator(hass, load_power_sensor="")
    await e._arm_sample(_T0)
    assert e._pending_since is None  # never armed — nothing to read


# ----------------------------------------------------------------- own-meter (energy_sensor) sampling
async def _run_energy_sensor_full_cycle_updates_estimate():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("sensor.meter", "10.0", {"unit_of_measurement": "kWh"})
    store = FakeStore()
    e = _estimator(hass, store=store, energy_sensor="sensor.meter", seed_kw=1.2)
    e._arm_energy_sample(_T0)
    hass.states.set("switch.x", "on")
    # On for 1h, counter rose 1.0kWh -> 1000W average, house-load sensor never read.
    hass.states.set("sensor.meter", "11.0", {"unit_of_measurement": "kWh"})
    await e._complete_energy_sample(_T0 + timedelta(hours=1))
    assert e.sample_count == 1
    assert e.last_sample_delta_w == 1000.0
    assert store.saved["k1"]["sample_count"] == 1


async def _run_energy_sensor_ignores_house_load_contamination():
    # The scenario this was built for: a second, unrelated HVAC unit's compressor swings
    # the whole-house load sensor during the on-period. Own-meter sampling never reads
    # sensor.house_load at all, so it can't be contaminated by it.
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("sensor.meter", "10.0", {"unit_of_measurement": "kWh"})
    hass.states.set("sensor.house_load", "500", {"unit_of_measurement": "W"})
    e = _estimator(hass, energy_sensor="sensor.meter", seed_kw=1.2)
    e._arm_energy_sample(_T0)
    hass.states.set("switch.x", "on")
    hass.states.set("sensor.house_load", "9000", {"unit_of_measurement": "W"})  # other HVAC
    hass.states.set("sensor.meter", "11.0", {"unit_of_measurement": "kWh"})
    await e._complete_energy_sample(_T0 + timedelta(hours=1))
    assert e.last_sample_delta_w == 1000.0  # unaffected by the house_load spike


async def _run_energy_sensor_short_on_period_is_discarded():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("sensor.meter", "10.0", {"unit_of_measurement": "kWh"})
    e = _estimator(hass, energy_sensor="sensor.meter")
    e._arm_energy_sample(_T0)
    hass.states.set("switch.x", "on")
    hass.states.set("sensor.meter", "10.1", {"unit_of_measurement": "kWh"})
    # Only 30s on — below MIN_ENERGY_SAMPLE_HOURS, discard rather than trust a coarse
    # counter's rounding.
    await e._complete_energy_sample(_T0 + timedelta(seconds=30))
    assert e.sample_count == 0


async def _run_energy_sensor_counter_reset_is_discarded():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("sensor.meter", "10.0", {"unit_of_measurement": "kWh"})
    e = _estimator(hass, energy_sensor="sensor.meter")
    e._arm_energy_sample(_T0)
    hass.states.set("switch.x", "on")
    hass.states.set("sensor.meter", "0.5", {"unit_of_measurement": "kWh"})  # device rebooted
    await e._complete_energy_sample(_T0 + timedelta(hours=1))
    assert e.sample_count == 0


async def _run_energy_sensor_unavailable_blip_does_not_fragment_sample():
    # The bug this guards: a flaky integration (e.g. ECHONET Lite) reporting the control
    # entity as unavailable mid-run must NOT be treated as the device switching off (which
    # would end the in-flight sample early) nor switching back on when it reconnects
    # (which would re-arm a fresh one) — either would chop one continuous on-period into
    # short fragments a coarse energy counter reads wildly inflated deltas over.
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("sensor.meter", "10.0", {"unit_of_measurement": "kWh"})
    _NOW[0] = _T0
    e = _estimator(hass, energy_sensor="sensor.meter", seed_kw=1.2)
    await e._on_control_state_change(types.SimpleNamespace(data={
        "old_state": FakeState("off"), "new_state": FakeState("on"),
    }))
    start_at = e._energy_on_start_at
    assert start_at == _T0

    # Blip to unavailable a minute later, then back to on a minute after that — neither
    # end should touch the in-flight sample.
    _NOW[0] = _T0 + timedelta(minutes=1)
    await e._on_control_state_change(types.SimpleNamespace(data={
        "old_state": FakeState("on"), "new_state": FakeState("unavailable"),
    }))
    assert e._energy_on_start_at == start_at  # sample was NOT completed by the blip

    _NOW[0] = _T0 + timedelta(minutes=2)
    await e._on_control_state_change(types.SimpleNamespace(data={
        "old_state": FakeState("unavailable"), "new_state": FakeState("on"),
    }))
    assert e._energy_on_start_at == start_at  # reconnect did NOT re-arm a new sample

    # Genuine off, one hour after the ORIGINAL arm — 1.0kWh/1h = 1000W, matching the
    # device's real sustained draw, not some short-fragment artifact.
    _NOW[0] = _T0 + timedelta(hours=1)
    hass.states.set("sensor.meter", "11.0", {"unit_of_measurement": "kWh"})
    await e._on_control_state_change(types.SimpleNamespace(data={
        "old_state": FakeState("on"), "new_state": FakeState("off"),
    }))
    assert e.sample_count == 1
    assert e.last_sample_delta_w == 1000.0


async def _run_energy_sensor_takes_priority_over_house_load_arming():
    # With energy_sensor set, an off->on transition must never arm the house-load path.
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    hass.states.set("sensor.meter", "10.0", {"unit_of_measurement": "kWh"})
    hass.states.set("sensor.house_load", "500", {"unit_of_measurement": "W"})
    e = _estimator(hass, energy_sensor="sensor.meter")
    await e._on_control_state_change(types.SimpleNamespace(data={
        "old_state": FakeState("off"), "new_state": FakeState("on"),
    }))
    assert e._pending_since is None
    assert e._energy_on_start_kwh == 10.0


# ----------------------------------------------------------------- track_energy
async def _run_track_energy_false_skips_integration_timer():
    hass = FakeHass()
    e = _estimator(hass, track_energy=False)
    e.start()
    assert e._cancel_integration_timer is None
    e.stop()


async def _run_track_energy_true_creates_integration_timer():
    hass = FakeHass()
    e = _estimator(hass, track_energy=True)
    e.start()
    assert e._cancel_integration_timer is not None
    e.stop()


async def _run_integration_tick_advances_kwh_only_while_on():
    hass = FakeHass()
    hass.states.set("switch.x", "off")
    e = _estimator(hass, track_energy=True)
    e.estimated_w = 1500.0
    e._last_integrated_at = _T0
    await e._on_integration_tick(_T0 + timedelta(hours=1))
    assert e.running_kwh == 0.0  # off — no energy added
    hass.states.set("switch.x", "on")
    await e._on_integration_tick(_T0 + timedelta(hours=2))
    assert e.running_kwh == 1.5  # 1.5kW for 1h


# ----------------------------------------------------------------- persistence
async def _run_async_load_restores_state():
    hass = FakeHass()
    store = FakeStore({"k1": {
        "estimated_w": 900.0, "sample_count": 4, "running_kwh": 12.5,
        "last_integrated_at": _T0.isoformat(),
    }})
    e = _estimator(hass, store=store)
    await e.async_load()
    assert e.estimated_w == 900.0
    assert e.sample_count == 4
    assert e.running_kwh == 12.5


async def _run_status_reports_new_fields():
    hass = FakeHass()
    hass.states.set("switch.x", "on")
    e = _estimator(hass, track_energy=False)
    s = e.status()
    assert s["track_energy"] is False
    assert s["current_power_w"] == e.estimated_w


if __name__ == "__main__":
    tests = [
        ("current_power_w_switch_domain", test_current_power_w_switch_domain),
        ("current_power_w_climate_domain", test_current_power_w_climate_domain),
        ("current_power_w_unknown_state_is_zero", test_current_power_w_unknown_state_is_zero_not_a_guess),
        ("accepted_sample_updates_and_persists", lambda: _run(_run_accepted_sample_updates_estimate_and_persists)),
        ("contaminated_sample_is_discarded", lambda: _run(_run_contaminated_sample_is_discarded)),
        ("other_device_attribute_only_update_does_not_contaminate", lambda: _run(_run_other_device_attribute_only_update_does_not_contaminate)),
        ("device_off_at_settle_is_discarded", lambda: _run(_run_device_off_at_settle_is_discarded)),
        ("implausible_delta_is_discarded", lambda: _run(_run_implausible_delta_is_discarded)),
        ("no_load_power_sensor_never_arms", lambda: _run(_run_no_load_power_sensor_never_arms)),
        ("energy_sensor_full_cycle_updates_estimate", lambda: _run(_run_energy_sensor_full_cycle_updates_estimate)),
        ("energy_sensor_ignores_house_load_contamination", lambda: _run(_run_energy_sensor_ignores_house_load_contamination)),
        ("energy_sensor_short_on_period_is_discarded", lambda: _run(_run_energy_sensor_short_on_period_is_discarded)),
        ("energy_sensor_counter_reset_is_discarded", lambda: _run(_run_energy_sensor_counter_reset_is_discarded)),
        ("energy_sensor_unavailable_blip_does_not_fragment_sample", lambda: _run(_run_energy_sensor_unavailable_blip_does_not_fragment_sample)),
        ("energy_sensor_takes_priority_over_house_load_arming", lambda: _run(_run_energy_sensor_takes_priority_over_house_load_arming)),
        ("track_energy_false_skips_timer", lambda: _run(_run_track_energy_false_skips_integration_timer)),
        ("track_energy_true_creates_timer", lambda: _run(_run_track_energy_true_creates_integration_timer)),
        ("integration_tick_advances_only_while_on", lambda: _run(_run_integration_tick_advances_kwh_only_while_on)),
        ("async_load_restores_state", lambda: _run(_run_async_load_restores_state)),
        ("status_reports_new_fields", lambda: _run(_run_status_reports_new_fields)),
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
