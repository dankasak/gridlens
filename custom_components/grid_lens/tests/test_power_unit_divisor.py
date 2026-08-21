"""Offline tests for power-sensor unit resolution.

Getting this wrong is a silent 1000x error in every alternative-plan cost. On
2026-08-21 this install reported 582,810 kWh of battery charge over 30 days (real
value: 582.8) because the plan comparison ran ~1 s after HA started, before the
Sigenergy MQTT sensors had any state, and the resolver quietly assumed kW.

Run: python3 tests/test_power_unit_divisor.py
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import types

logging.disable(logging.WARNING)
_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stub_ha():
    def _mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    for name in (
        "homeassistant", "homeassistant.core", "homeassistant.config_entries",
        "homeassistant.util", "homeassistant.util.dt", "homeassistant.const",
        "homeassistant.helpers", "homeassistant.helpers.entity_registry",
        "homeassistant.components", "homeassistant.components.recorder",
        "homeassistant.components.recorder.statistics",
        "homeassistant.components.recorder.history",
    ):
        if name not in sys.modules:
            _mod(name)

    sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
    sys.modules["homeassistant.core"].callback = lambda fn: fn
    sys.modules["homeassistant.config_entries"].ConfigEntry = type("ConfigEntry", (), {})
    sys.modules["homeassistant.components.recorder"].get_instance = lambda hass: None
    sys.modules["homeassistant.components.recorder.statistics"].statistics_during_period = None
    sys.modules["homeassistant.util.dt"].now = lambda: None

    # The registry lookup is imported inside the function under test; each test
    # installs its own async_get.
    return sys.modules["homeassistant.helpers.entity_registry"]


_ER = _stub_ha()


def _load():
    """Load plan_calculator's module-level code only.

    The module does `from .const import ...` and pulls in the whole integration, none
    of which power_unit_divisor needs. Rather than stub the entire dependency tree,
    execute just the function's source in a namespace with the two names it closes
    over — it is deliberately a module-level pure-ish function for exactly this reason.
    """
    src = open(os.path.join(_COMPONENT, "plan_calculator.py")).read()
    start = src.index("def power_unit_divisor(")
    end = src.index("\nclass ", start)
    ns = types.ModuleType("pc")
    ns._LOGGER = logging.getLogger("pc")
    exec(compile(src[start:end], "plan_calculator.py", "exec"), ns.__dict__)
    return ns


pc = _load()


class FakeState:
    def __init__(self, unit):
        self.attributes = {"unit_of_measurement": unit} if unit is not None else {}


class FakeStates:
    def __init__(self, d):
        self._d = d

    def get(self, eid):
        return self._d.get(eid)


class FakeHass:
    def __init__(self, states):
        self.states = FakeStates(states)


def _set_registry(entries: dict | None):
    """Install a fake entity registry; None means 'lookup raises'."""
    if entries is None:
        def _boom(hass):
            raise RuntimeError("registry unavailable")
        _ER.async_get = _boom
        return

    class _Entry:
        def __init__(self, unit):
            self.unit_of_measurement = unit

    class _Registry:
        def async_get(self, eid):
            return _Entry(entries[eid]) if eid in entries else None

    _ER.async_get = lambda hass: _Registry()


def test_watts_from_live_state():
    _set_registry({})
    hass = FakeHass({"sensor.p": FakeState("W")})
    assert pc.power_unit_divisor(hass, "sensor.p") == 1000.0
    print("  ✓ live state in W → ÷1000")


def test_kilowatts_from_live_state():
    _set_registry({})
    hass = FakeHass({"sensor.p": FakeState("kW")})
    assert pc.power_unit_divisor(hass, "sensor.p") == 1.0
    print("  ✓ live state in kW → ÷1")


def test_registry_used_when_state_missing():
    """The actual bug: no state yet at startup, but the registry knows."""
    _set_registry({"sensor.p": "W"})
    hass = FakeHass({})  # integration hasn't published a state yet
    assert pc.power_unit_divisor(hass, "sensor.p") == 1000.0, \
        "a watts sensor with no state must not be treated as kW"
    print("  ✓ no state at startup, registry says W → ÷1000 (the 1000x bug)")


def test_registry_kw_when_state_missing():
    _set_registry({"sensor.p": "kW"})
    assert pc.power_unit_divisor(FakeHass({}), "sensor.p") == 1.0
    print("  ✓ no state, registry says kW → ÷1")


def test_live_state_wins_over_registry():
    _set_registry({"sensor.p": "kW"})
    hass = FakeHass({"sensor.p": FakeState("W")})
    assert pc.power_unit_divisor(hass, "sensor.p") == 1000.0
    print("  ✓ live state takes precedence over a stale registry unit")


def test_state_without_unit_falls_through_to_registry():
    _set_registry({"sensor.p": "W"})
    hass = FakeHass({"sensor.p": FakeState(None)})
    assert pc.power_unit_divisor(hass, "sensor.p") == 1000.0
    print("  ✓ state present but unit-less → registry consulted, not assumed kW")


def test_unknown_everywhere_defaults_to_kw():
    """Last resort keeps the old behaviour, but the function logs a warning."""
    _set_registry({})
    assert pc.power_unit_divisor(FakeHass({}), "sensor.p") == 1.0
    print("  ✓ unknown unit → ÷1 fallback retained")


def test_registry_failure_is_not_fatal():
    _set_registry(None)  # raises
    assert pc.power_unit_divisor(FakeHass({}), "sensor.p") == 1.0
    print("  ✓ a broken registry lookup degrades instead of raising")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"Running {len(tests)} power-unit tests\n")
    for t in tests:
        t()
    print(f"\n✅ all {len(tests)} passed")
