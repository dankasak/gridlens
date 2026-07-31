"""Offline tests for BatteryController's hardware discharge-floor push.

Covers the read-current-before-writing behaviour: never lower a discharge floor
that's already at least as conservative as our own configured value, only ever
tighten one that's lower (or unreadable). No HA needed (not importable in this
container).

Run: python3 tests/test_hardware_floor.py
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
    core.callback = lambda fn: fn
    ha.core = core

    helpers = _mod("homeassistant.helpers")
    event = _mod("homeassistant.helpers.event")
    event.async_call_later = lambda *a, **k: (lambda: None)
    helpers.event = event
    ha.helpers = helpers

    util = _mod("homeassistant.util")
    dt = _mod("homeassistant.util.dt")
    dt.now = lambda: None
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
    base = _load(os.path.join(_COMPONENT, "inverters", "base.py"), "gl.inverters.base",
                 package="gl.inverters")
    bc = _load(os.path.join(_COMPONENT, "control", "battery_controller.py"),
               "gl.control.battery_controller", package="gl.control")
    return base.InverterController, base.InverterState, base.InverterStatus, bc.BatteryController, bc.GuardrailConfig


InverterController, InverterState, InverterStatus, BatteryController, GuardrailConfig = _bootstrap()


class FakeDriver(InverterController):
    brand = "Fake"
    supports_battery_control = True

    def __init__(self, current_floor):
        self._current_floor = current_floor
        self.set_calls: list[float] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return

    async def get_status(self) -> InverterState:
        return InverterState(status=InverterStatus.ONLINE, soc_pct=50.0)

    async def get_discharge_floor(self):
        return self._current_floor

    async def set_discharge_floor(self, soc_pct: float) -> bool:
        self.set_calls.append(soc_pct)
        self._current_floor = soc_pct
        return True


def _controller(current_floor, min_soc_pct=10.0):
    driver = FakeDriver(current_floor)
    cfg = GuardrailConfig(min_soc_pct=min_soc_pct)
    return BatteryController(hass=object(), driver=driver, config=cfg), driver


def test_pushes_when_current_lower_than_configured():
    ctl, driver = _controller(current_floor=5.0, min_soc_pct=10.0)
    asyncio.run(ctl.async_push_safety_floor())
    assert driver.set_calls == [10.0]
    assert ctl.status()["hardware_floor_set"] is True


def test_pushes_when_current_unreadable():
    ctl, driver = _controller(current_floor=None, min_soc_pct=10.0)
    asyncio.run(ctl.async_push_safety_floor())
    assert driver.set_calls == [10.0]


def test_leaves_alone_when_current_already_more_conservative():
    ctl, driver = _controller(current_floor=20.0, min_soc_pct=10.0)
    asyncio.run(ctl.async_push_safety_floor())
    assert driver.set_calls == [], "must never lower an existing higher floor"
    assert ctl.status()["hardware_floor_set"] is True


def test_leaves_alone_when_current_equals_configured():
    ctl, driver = _controller(current_floor=10.0, min_soc_pct=10.0)
    asyncio.run(ctl.async_push_safety_floor())
    assert driver.set_calls == []


def test_idempotent_second_call_is_a_noop():
    ctl, driver = _controller(current_floor=5.0, min_soc_pct=10.0)
    asyncio.run(ctl.async_push_safety_floor())
    asyncio.run(ctl.async_push_safety_floor())
    assert driver.set_calls == [10.0], "second call must not re-read or re-push"


def test_get_discharge_floor_exception_falls_back_to_push():
    class RaisingDriver(FakeDriver):
        async def get_discharge_floor(self):
            raise ConnectionError("MQTT timeout")

    driver = RaisingDriver(current_floor=None)
    ctl = BatteryController(hass=object(), driver=driver, config=GuardrailConfig(min_soc_pct=10.0))
    asyncio.run(ctl.async_push_safety_floor())
    assert driver.set_calls == [10.0]


def _run_all():
    tests = [obj for name, obj in globals().items()
             if name.startswith("test_") and callable(obj)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
