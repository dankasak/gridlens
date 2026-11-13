#!/usr/bin/env python3
"""load_discovery: one appliance pick, every entity read off its device.

Each fixture is a shape that actually broke something on the development rig while
this module was being written (2026-11-13), so the assertions are incidents, not
hypotheticals:

* **TP-Link P110** — its energy counters are `entity_category: diagnostic`, so
  requiring an uncategorised sensor reported every TP-Link-metered appliance as
  unmetered and silently downgraded it from monitored to estimated.
* **ECHONET heat pump** — `climate.*` must beat the device's own
  `switch.…_power_saving_operation_setting`, or an aircon gets controlled through a
  settings toggle and (because the Power Flow card groups the Aircon node by climate
  entity) is drawn twice: once in the aircon group, once as its own node.
* **Mislabelled plug** — a switch renamed "Julian's Aircon switch" sitting on the
  device "Dishwasher". Discovery cannot know which is the lie, but it must surface
  the disagreement rather than present the wiring as fine. This is the real 2026-11-12
  misconfiguration that motivated the whole module.
* **Multi-circuit inverter** — 14 energy counters on one device. Its single primary
  switch belongs to the plant, not to the hot-water circuit being configured, so
  control/setpoint/SOC must NOT be auto-assigned.
* **Wrapper entities** — a schedule switch carrying `entities: [...]` actuates another
  entity; configuring it means Grid Lens fights the scheduler.

Home Assistant isn't importable here, so the registries and `hass.states` are stubbed,
following test_device_name_resolution.py.

Run:  python3 test_load_discovery.py
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


class Ent:
    def __init__(self, entity_id, *, name=None, original_name=None, device_id=None,
                 device_class=None, state_class=None, unit=None,
                 entity_category=None, config_entry_id=None):
        self.entity_id = entity_id
        self.name = name
        self.original_name = original_name
        self.device_id = device_id
        self.device_class = device_class
        self.original_device_class = device_class
        self.capabilities = {"state_class": state_class} if state_class else {}
        self.unit_of_measurement = unit
        self.entity_category = entity_category
        self.hidden_by = None
        self.disabled_by = None
        self.config_entry_id = config_entry_id


class Dev:
    def __init__(self, id, *, name=None, name_by_user=None, entry_type=None):
        self.id = id
        self.name = name
        self.name_by_user = name_by_user
        self.entry_type = entry_type


class _St:
    def __init__(self, attributes):
        self.attributes = attributes


class Hass:
    def __init__(self, entities=(), devices=(), live=None):
        self._entities = {e.entity_id: e for e in entities}
        self._devices = {d.id: d for d in devices}
        self._live = live or {}
        self.states = self

    def get(self, entity_id):
        attrs = self._live.get(entity_id)
        return _St(attrs) if attrs is not None else None


_HOLDER: dict = {"hass": Hass()}


def _load_module():
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = Hass
    helpers = types.ModuleType("homeassistant.helpers")
    er = types.ModuleType("homeassistant.helpers.entity_registry")
    dr = types.ModuleType("homeassistant.helpers.device_registry")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")

    class _EReg:
        @property
        def entities(self):
            return _HOLDER["hass"]._entities

        def async_get(self, entity_id):
            return _HOLDER["hass"]._entities.get(entity_id)

    class _DReg:
        @property
        def devices(self):
            return _HOLDER["hass"]._devices

        def async_get(self, device_id):
            return _HOLDER["hass"]._devices.get(device_id)

    er.async_get = lambda hass: _EReg()
    er.async_entries_for_device = lambda reg, device_id, include_disabled_entities=False: [
        e for e in _HOLDER["hass"]._entities.values() if e.device_id == device_id
    ]
    dr.async_get = lambda hass: _DReg()
    dr.async_entries_for_config_entry = lambda reg, entry_id: []
    dt.utcnow = lambda: None
    helpers.entity_registry = er
    helpers.device_registry = dr
    util.dt = dt
    ha.core, ha.helpers, ha.util = core, helpers, util
    sys.modules.update({
        "homeassistant": ha, "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_registry": er,
        "homeassistant.helpers.device_registry": dr,
        "homeassistant.util": util, "homeassistant.util.dt": dt,
    })
    # The module is part of the integration package, so its `from .x import y` need a
    # parent package whose __path__ points at the component directory.
    pkg = types.ModuleType("grid_lens")
    pkg.__path__ = [_COMPONENT]
    sys.modules["grid_lens"] = pkg
    for name in ("const", "deferrable_loads", "entity_lookup", "load_discovery"):
        spec = importlib.util.spec_from_file_location(
            f"grid_lens.{name}", os.path.join(_COMPONENT, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"grid_lens.{name}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["grid_lens.load_discovery"]


ld = _load_module()


def _use(hass):
    _HOLDER["hass"] = hass
    return hass


# --- fixtures ---------------------------------------------------------------


def _tplink_plug(*, switch_name=None, device_name="Dishwasher"):
    """TP-Link P110: energy counters categorised `diagnostic`, config switches too."""
    return Hass(
        entities=[
            Ent("switch.plug", name=switch_name, device_id="p1"),
            Ent("switch.plug_led", device_id="p1", entity_category="config"),
            Ent("switch.plug_auto_off", device_id="p1", entity_category="config"),
            Ent("sensor.plug_today", device_id="p1", device_class="energy",
                state_class="total_increasing", entity_category="diagnostic"),
            Ent("sensor.plug_month", device_id="p1", device_class="energy",
                state_class="total_increasing", entity_category="diagnostic"),
            Ent("sensor.plug_power", device_id="p1", device_class="power",
                state_class="measurement"),
        ],
        devices=[Dev("p1", name=device_name)],
    )


def _echonet_aircon():
    return Hass(
        entities=[
            Ent("climate.aircon", name="", original_name="Julian's Aircon", device_id="a1"),
            Ent("switch.aircon_power_saving", device_id="a1", entity_category="config"),
            Ent("sensor.aircon_cumulative", name="Measured cumulative power consumption",
                device_id="a1", device_class="energy", state_class="total_increasing"),
        ],
        devices=[Dev("a1", name_by_user="Julian's Aircon")],
        live={"climate.aircon": {"friendly_name": "Julian's Aircon"}},
    )


def _inverter_plant():
    """One device, 14 circuits, one plant-level switch and a plant battery SOC."""
    ents = [
        Ent("switch.plant_remote_ems", device_id="i1"),
        Ent("number.plant_max_charge", device_id="i1", unit="kW"),
        Ent("sensor.plant_battery_soc", device_id="i1", device_class="battery",
            state_class="measurement"),
    ]
    for i in range(1, 15):
        ents.append(Ent(f"sensor.smart_load_{i:02d}_total", device_id="i1",
                        device_class="energy", state_class="total_increasing"))
        ents.append(Ent(f"sensor.smart_load_{i:02d}_power", device_id="i1",
                        device_class="power", state_class="measurement"))
    return Hass(entities=ents, devices=[Dev("i1", name="Sigenergy Plant")])


def _schedule_wrapper():
    return Hass(
        entities=[
            Ent("switch.schedule_aircon", device_id="s1"),
            Ent("climate.real_aircon"),  # device-less, the thing actually controlled
        ],
        devices=[Dev("s1", name="Schedule Dan's Aircon")],
        live={
            "switch.schedule_aircon": {"entities": ["climate.real_aircon"]},
            "climate.real_aircon": {"friendly_name": "Dan's Aircon"},
        },
    )


def _wire(hass, device_id=None, control=None, dashboard=None):
    return ld.discover_wiring(hass, device_id=device_id, control_entity=control,
                              dashboard=dashboard or {})


# --- tests ------------------------------------------------------------------


def test_diagnostic_energy_counters_still_count_as_metering():
    hass = _use(_tplink_plug())
    w = _wire(hass, "p1", dashboard={"sensor.plug_today": "Dishwasher"})
    check("diagnostic energy counter is accepted", w["energy"] == "sensor.plug_today",
          w["energy"])
    check("load is monitored, not estimated", w["kind"] == "monitored", w["kind"])
    check("power sensor found", w["power"] == "sensor.plug_power", w["power"])


def test_config_switches_are_never_the_control():
    hass = _use(_tplink_plug())
    w = _wire(hass, "p1")
    check("control skips led/auto-off config switches", w["control"] == "switch.plug",
          w["control"])


def test_dashboard_label_breaks_energy_counter_tie():
    hass = _use(_tplink_plug())
    w = _wire(hass, "p1", dashboard={"sensor.plug_month": "Dishwasher"})
    check("tracked counter wins the tie", w["energy"] == "sensor.plug_month", w["energy"])
    untracked = _wire(_use(_tplink_plug()), "p1")
    check("no tracker -> ask, never guess", untracked["energy"] is None
          and "energy" in untracked["unresolved"], untracked)


def test_climate_beats_a_config_switch_on_an_aircon():
    hass = _use(_echonet_aircon())
    w = _wire(hass, "a1", dashboard={"sensor.aircon_cumulative": "Julian's Aircon"})
    check("climate entity is the control", w["control"] == "climate.aircon", w["control"])
    check("control_domain recorded", w["control_domain"] == "climate", w["control_domain"])
    check("name is the appliance, not the renamed meter",
          w["name"] == "Julian's Aircon", w["name"])
    check("aircon grouping is explained",
          any("Aircon group" in n for n in w["notes"]), w["notes"])


def test_renamed_control_on_a_foreign_device_is_flagged():
    hass = _use(_tplink_plug(switch_name="Julian's Aircon switch",
                             device_name="Dishwasher"))
    w = _wire(hass, "p1")
    check("mismatch between renamed control and its device is surfaced",
          any("Dishwasher" in n and "renamed" in n for n in w["notes"]), w["notes"])


def test_benign_name_differences_do_not_warn():
    hass = _use(_tplink_plug(switch_name="Westinghouse Fridge",
                             device_name="WestinghouseFridge"))
    w = _wire(hass, "p1")
    check("spelling/spacing tidy-up does not cry wolf",
          not any("renamed" in n for n in w["notes"]), w["notes"])


def test_multi_circuit_device_does_not_guess_control_setpoint_or_soc():
    hass = _use(_inverter_plant())
    w = _wire(hass, "i1", dashboard={"sensor.smart_load_01_total": "Hot Water"})
    check("the tracked circuit's own counter is used",
          w["energy"] == "sensor.smart_load_01_total", w["energy"])
    check("plant-level switch not assigned", w["control"] is None, w["control"])
    check("plant charge-limit setpoint not assigned", w["setpoint"] is None, w["setpoint"])
    check("plant battery SOC not assigned", w["soc"] is None, w["soc"])
    check("control is asked about", "control" in w["unresolved"], w["unresolved"])


def test_wrapper_entities_are_not_appliances():
    hass = _use(_schedule_wrapper())
    rows = ld.discover_appliances(hass)
    check("schedule wrapper is not offered",
          not any("Schedule" in r["label"] for r in rows), rows)
    w = _wire(hass, "s1")
    check("wrapper is never chosen as a control", w["control"] is None, w["control"])


def test_picker_tiers_and_self_exclusion():
    hass = _use(_tplink_plug())
    hass._entities["switch.ours"] = Ent("switch.ours", original_name="Grid Lens thing",
                                        config_entry_id="our_entry")
    rows = ld.discover_appliances(hass, exclude_entry_id="our_entry",
                                  dashboard={"sensor.plug_today": "Dishwasher"})
    check("our own entities are excluded",
          not any("ours" in r["value"] for r in rows), rows)
    check("metered device is offered", any(r["value"] == "device:p1" for r in rows), rows)
    taken = ld.discover_appliances(hass, taken={"sensor.plug_today"})
    check("an appliance already configured is not offered twice",
          not any(r["value"] == "device:p1" for r in taken), taken)


def test_deviceless_control_still_works():
    hass = _use(_schedule_wrapper())
    w = _wire(hass, control="climate.real_aircon")
    check("device-less entity yields an estimated load", w["kind"] == "estimated", w["kind"])
    check("control is what was passed in", w["control"] == "climate.real_aircon",
          w["control"])


def main():
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        print(f"{name}:")
        getattr(mod, name)()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} - " + ", ".join(FAILURES))
        sys.exit(1)
    print("OK - one appliance pick resolves every entity a deferrable load needs.")


if __name__ == "__main__":
    main()
