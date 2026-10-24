#!/usr/bin/env python3
"""entity_lookup.resolve_device_name(): a deferrable load's display name.

Two bug classes this guards, both hit on 2026-10-23 by one Meross smart plug
(device "Pool", entities "Outlet"/"Energy"/"Power") added as a deferrable load — it
showed up in the control panel as "Outlet":

1. `has_entity_name = True` integrations (Meross, Shelly, Tasmota, TP-Link, most
   modern plug/switch platforms) put the appliance identity in the DEVICE name and
   give each entity a bare role noun. `original_name` alone is therefore "Outlet",
   which names no appliance. Must fall through to the device name.
2. Resolution has to be priority-major, not anchor-major: a LOW-priority source on
   an early anchor (the control switch's generic registry name) must not beat a
   HIGH-priority one on a later anchor (the "Pool" the user typed on the Energy
   Dashboard against the energy sensor).

A multi-appliance device (one Sigenergy plant hosting several smart loads) must
NOT collapse onto the device name — each load keeps its own `original_name`.

Home Assistant isn't importable here, so the two registries and `hass.states` are
stubbed.

Run:  python3 test_device_name_resolution.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# --- registry / state stubs -------------------------------------------------


class Ent:
    def __init__(self, entity_id, *, name=None, original_name=None, device_id=None):
        self.entity_id = entity_id
        self.name = name
        self.original_name = original_name
        self.device_id = device_id
        self.device_class = None
        self.original_device_class = None


class Dev:
    def __init__(self, name=None, name_by_user=None):
        self.name = name
        self.name_by_user = name_by_user


class _St:
    def __init__(self, friendly_name):
        self.attributes = {"friendly_name": friendly_name}


class Hass:
    """Minimal stand-in: entities, devices and live friendly_names."""

    def __init__(self, entities=None, devices=None, live=None):
        self._entities = entities or {}
        self._devices = devices or {}
        self._live = live or {}
        self.states = self

    # hass.states.get()
    def get(self, entity_id):
        fn = self._live.get(entity_id)
        return _St(fn) if fn else None


def _load_module(hass_holder):
    """Import entity_lookup against stubbed homeassistant registries.

    The registries resolve through `hass_holder` so each test can swap the fixture
    without re-importing the module.
    """
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = Hass
    helpers = types.ModuleType("homeassistant.helpers")
    er = types.ModuleType("homeassistant.helpers.entity_registry")
    dr = types.ModuleType("homeassistant.helpers.device_registry")

    class _EReg:
        def async_get(self, entity_id):
            return hass_holder["hass"]._entities.get(entity_id)

    class _DReg:
        def async_get(self, device_id):
            return hass_holder["hass"]._devices.get(device_id)

    er.async_get = lambda hass: _EReg()
    er.async_entries_for_device = lambda *a, **k: []
    dr.async_get = lambda hass: _DReg()
    helpers.entity_registry = er
    helpers.device_registry = dr
    ha.core = core
    ha.helpers = helpers
    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.entity_registry": er,
            "homeassistant.helpers.device_registry": dr,
        }
    )
    spec = importlib.util.spec_from_file_location(
        "entity_lookup", os.path.join(_COMPONENT, "entity_lookup.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_HOLDER: dict = {"hass": Hass()}
el = _load_module(_HOLDER)


def _use(hass):
    _HOLDER["hass"] = hass
    return hass


def _rdn(*anchors, **kw):
    """resolve_device_name against whichever fixture _use() installed."""
    return el.resolve_device_name(_HOLDER["hass"], *anchors, **kw)


# --- fixtures ---------------------------------------------------------------


def _meross_plug(**ent_overrides):
    """Device "Pool"; entities named with the bare role noun (has_entity_name)."""
    return Hass(
        entities={
            "switch.pool_pool_outlet": Ent(
                "switch.pool_pool_outlet", original_name="Outlet", device_id="d1",
                **ent_overrides,
            ),
            "sensor.pool_pool_energy": Ent(
                "sensor.pool_pool_energy", original_name="Energy", device_id="d1"
            ),
        },
        devices={"d1": Dev(name="Pool")},
        live={
            "switch.pool_pool_outlet": "Pool Outlet",
            "sensor.pool_pool_energy": "Pool Energy",
        },
    )


# --- tests ------------------------------------------------------------------


def test_generic_entity_noun_falls_through_to_device_name():
    _use(_meross_plug())
    got = _rdn("switch.pool_pool_outlet", "sensor.pool_pool_energy"
    )
    check("plug with no Energy Dashboard label -> 'Pool' (not 'Outlet')", got == "Pool", got)


def test_dashboard_label_beats_generic_switch_name():
    """The 2026-10-23 regression: anchor-major iteration returned 'Outlet' here."""
    _use(_meross_plug())
    got = _rdn("switch.pool_pool_outlet",
        "sensor.pool_pool_energy",
        dashboard_names={"sensor.pool_pool_energy": "Pool Pump"},
    )
    check("Energy Dashboard label wins over switch's generic name", got == "Pool Pump", got)


def test_entity_rename_beats_dashboard_label():
    _use(_meross_plug(name="Filter Pump"))
    got = _rdn("switch.pool_pool_outlet",
        "sensor.pool_pool_energy",
        dashboard_names={"sensor.pool_pool_energy": "Pool Pump"},
    )
    check("explicit entity rename is top priority", got == "Filter Pump", got)


def test_device_rename_by_user_wins_over_device_name():
    hass = _use(_meross_plug())
    hass._devices["d1"] = Dev(name="MSS310", name_by_user="Pool Pump")
    got = _rdn("switch.pool_pool_outlet")
    check("device name_by_user preferred", got == "Pool Pump", got)


def test_multi_appliance_device_keeps_per_entity_name():
    """One Sigenergy plant, several smart loads — must not all read "Sigenergy Plant"."""
    _use(
        Hass(
            entities={
                "sensor.sigen_smart_load_01_total_consumption": Ent(
                    "sensor.sigen_smart_load_01_total_consumption",
                    original_name="Smart Load 01 Total Consumption",
                    device_id="d9",
                ),
                "sensor.sigen_smart_load_02_total_consumption": Ent(
                    "sensor.sigen_smart_load_02_total_consumption",
                    original_name="Smart Load 02 Total Consumption",
                    device_id="d9",
                ),
            },
            devices={"d9": Dev(name="Sigenergy Plant")},
        )
    )
    a = _rdn(None, "sensor.sigen_smart_load_01_total_consumption")
    b = _rdn(None, "sensor.sigen_smart_load_02_total_consumption")
    check("smart load 01 keeps its own name", a == "Smart Load 01", a)
    check("smart load 02 distinct from 01", b == "Smart Load 02", b)


def test_bare_metering_name_with_no_control_switch():
    """The ordinary "added the plug to the Energy Dashboard" shape: no switch configured,
    and the energy sensor's whole name is "Energy" — which no *suffix* match can trim."""
    hass = _use(_meross_plug())
    got = _rdn(None, "sensor.pool_pool_energy")
    check("energy sensor alone -> 'Pool' (not 'Energy')", got == "Pool", got)

    hass._entities["sensor.pool_pool_power"] = Ent(
        "sensor.pool_pool_power", original_name="Power", device_id="d1"
    )
    got = _rdn(None, "sensor.pool_pool_power")
    check("bare 'Power' -> 'Pool'", got == "Pool", got)


def test_indexed_role_noun_keeps_its_channel():
    """Two channels of one power strip must not resolve to the same name, so an indexed
    role noun keeps HA's concatenation instead of dropping to the bare device name."""
    _use(
        Hass(
            entities={
                "switch.strip_outlet_1": Ent(
                    "switch.strip_outlet_1", original_name="Outlet 1", device_id="d2"
                ),
                "switch.strip_outlet_2": Ent(
                    "switch.strip_outlet_2", original_name="Outlet 2", device_id="d2"
                ),
                "switch.shelly_switch_0": Ent(
                    "switch.shelly_switch_0", original_name="Switch 0", device_id="d3"
                ),
                "switch.tasmota_power1": Ent(
                    "switch.tasmota_power1", original_name="Power1", device_id="d4"
                ),
            },
            devices={
                "d2": Dev(name="Power Strip"),
                "d3": Dev(name="Towel Rail"),
                "d4": Dev(name="Heater"),
            },
        )
    )
    a = _rdn("switch.strip_outlet_1")
    b = _rdn("switch.strip_outlet_2")
    check("strip channel 1 -> 'Power Strip Outlet 1'", a == "Power Strip Outlet 1", a)
    check("strip channel 2 distinct from channel 1", b == "Power Strip Outlet 2", b)
    got = _rdn("switch.shelly_switch_0")
    check("'Switch 0' -> 'Towel Rail Switch 0'", got == "Towel Rail Switch 0", got)
    got = _rdn("switch.tasmota_power1")
    check("separator-less 'Power1' -> 'Heater Power1'", got == "Heater Power1", got)


def test_appliance_name_with_trailing_number_is_not_an_index():
    """"Smart Load 01" ends in digits too — but "Smart Load" is not a role noun, so the
    whole thing is an appliance name and must survive untouched."""
    _use(
        Hass(
            entities={
                "sensor.sigen_smart_load_01_total_consumption": Ent(
                    "sensor.sigen_smart_load_01_total_consumption",
                    original_name="Smart Load 01 Total Consumption",
                    device_id="d9",
                ),
                "switch.ev_charger_2": Ent(
                    "switch.ev_charger_2", original_name="Charger 2", device_id="d9"
                ),
            },
            devices={"d9": Dev(name="Sigenergy Plant")},
        )
    )
    got = _rdn(None, "sensor.sigen_smart_load_01_total_consumption")
    check("'Smart Load 01' keeps its own trailing number", got == "Smart Load 01", got)
    got = _rdn("switch.ev_charger_2")
    check("'Charger 2' is an appliance, not a role noun", got == "Charger 2", got)


def test_metering_suffixes_trimmed():
    _use(
        Hass(
            entities={
                "sensor.dishwasher_energy": Ent(
                    "sensor.dishwasher_energy", original_name="Dishwasher Energy"
                ),
                "sensor.dryer_power": Ent(
                    "sensor.dryer_power", original_name="Dryer Power"
                ),
                "sensor.oven_consumption": Ent(
                    "sensor.oven_consumption", original_name="Oven Consumption"
                ),
            }
        )
    )
    for eid, want in (
        ("sensor.dishwasher_energy", "Dishwasher"),
        ("sensor.dryer_power", "Dryer"),
        ("sensor.oven_consumption", "Oven"),
    ):
        got = _rdn(eid)
        check(f"{eid} -> {want}", got == want, got)


def test_live_friendly_name_then_object_id_fallbacks():
    _use(Hass(live={"switch.shelly_relay": "Towel Rail"}))
    got = _rdn("switch.shelly_relay")
    check("unregistered entity uses live friendly_name", got == "Towel Rail", got)

    _use(Hass())
    got = _rdn("switch.towel_rail")
    check("nothing known -> humanized object_id", got == "Towel Rail", got)
    check("no anchors -> None", _rdn() is None)
    check("only empty anchors -> None", _rdn(None, "") is None)


def test_deviceless_generic_name_is_not_dropped():
    """A bare role noun with no device to fall back on still beats nothing."""
    _use(
        Hass(
            entities={"switch.x": Ent("switch.x", original_name="Outlet")},
        )
    )
    got = _rdn("switch.x")
    check("deviceless 'Outlet' kept rather than lost", got == "Outlet", got)


def main():
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        print(f"{name}:")
        getattr(mod, name)()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} - " + ", ".join(FAILURES))
        sys.exit(1)
    print("OK - deferrable load names resolve to the appliance, not the entity's role.")


if __name__ == "__main__":
    main()
