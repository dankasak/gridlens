"""Offline tests for the per-load deferrable-load wizard in the options flow.

Reuses test_config_flow's Home Assistant/voluptuous stubs (importing it installs them
and loads `gl.config_flow`), then drives `GridLensOptionsFlow` step by step.

What matters here is not that the forms render, but that:
  * the parallel arrays every consumer reads survive a round trip through the wizard
    unchanged, and stay index-aligned — a ragged list shifts one appliance's setting
    onto another
  * a load is only asked what its kind and control style actually use
  * changing a load's control style clears the fields the old style owned, rather than
    leaving a stale setpoint entity still driving LoadControlManager down the
    modulating path
  * entering from the menu saves without walking the plan/API steps, and merges over
    the entry instead of replacing it

Run: python3 tests/test_deferrable_wizard.py
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_config_flow as base  # noqa: E402  — installs stubs, loads gl.config_flow

cf = base.cf
run = base.run
import gl.const as const  # noqa: E402
import gl.deferrable_loads as dl  # noqa: E402


# ----------------------------------------------------------------- fixtures
_ENTRY = {
    const.CONF_ENERGY_SENSOR: "sensor.import_energy",
    const.CONF_STATE: "NSW",
    const.CONF_GRIDLENS_API_KEY: "gl_existing",
    # Two monitored loads: a plain forecast-only pump and a modulating EV charger.
    const.CONF_DEFERRABLE_LOAD_SENSORS: ["sensor.pool", "sensor.evse"],
    const.CONF_DEFERRABLE_LOAD_MAX_KW: [1.2, 7.4],
    const.CONF_DEFERRABLE_LOAD_SWITCHES: ["", "switch.evse"],
    const.CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE: ["", ""],
    const.CONF_DEFERRABLE_LOAD_SOC_SENSORS: ["", "sensor.ev_soc"],
    const.CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT: [100.0, 90.0],
    const.CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH: [0.0, 64.0],
    const.CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD: ["", ""],
    const.CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE: [False, False],
    const.CONF_DEFERRABLE_LOAD_SETPOINT: ["", "number.evse_current"],
    const.CONF_DEFERRABLE_LOAD_SETPOINT_UNIT: ["", "a"],
    const.CONF_DEFERRABLE_LOAD_PHASES: [0, 3],
    const.CONF_DEFERRABLE_LOAD_VOLTAGE: [0.0, 240.0],
    const.CONF_DEFERRABLE_LOAD_MIN_CURRENT: [0.0, 6.0],
    const.CONF_DEFERRABLE_LOAD_PLUG_SENSOR: ["", "binary_sensor.evse_plug"],
    # One declared load.
    const.CONF_DEFERRABLE_LOAD_DUMMY_NAMES: ["Hot Water"],
    const.CONF_DEFERRABLE_LOAD_DUMMY_KWH: [8.0],
    const.CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW: [3.6],
    const.CONF_DEFERRABLE_LOAD_DUMMY_HOURS: ["22-06"],
    const.CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD: ["controlled_load_1"],
    const.CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE: [False],
    # One estimated load.
    const.CONF_DEFERRABLE_LOAD_EST_NAMES: ["Aircon"],
    const.CONF_DEFERRABLE_LOAD_EST_CONTROL: ["climate.aircon"],
    const.CONF_DEFERRABLE_LOAD_EST_KW: [2.5],
    const.CONF_DEFERRABLE_LOAD_EST_AUTO: [True],
    const.CONF_HAS_CONTROLLED_LOAD_1: True,
}

_DEVICE_OPTIONS = [
    {"value": "sensor.pool", "label": "Pool Pump (sensor.pool)"},
    {"value": "sensor.evse", "label": "EV Charger (sensor.evse)"},
    {"value": "sensor.dishwasher", "label": "Dishwasher (sensor.dishwasher)"},
]


class _FakeConfigEntries:
    def __init__(self):
        self.updated = None

    def async_update_entry(self, entry, data=None):
        self.updated = data
        entry.data = data


def _flow(entry_data=None, device_options=None):
    entry = types.SimpleNamespace(data=dict(entry_data or _ENTRY), entry_id="e1")
    f = cf.GridLensOptionsFlow(entry)
    f.hass = base.FakeHass(base.FakeSession(base._routes()))
    f.hass.config_entries = _FakeConfigEntries()
    f._device_options = list(_DEVICE_OPTIONS if device_options is None else device_options)
    return f


def _fields(result):
    return result["data_schema"].keys()


def _hub_values(result):
    """The hub's action SelectSelector options, as raw values."""
    marker = result["data_schema"].marker("action")
    return marker  # options live on the selector, which the stub discards


# ----------------------------------------------------------------- tests
def test_round_trip_preserves_every_array():
    """The whole point of the accessor seam: what goes in comes back out, unchanged."""
    written = dl.write_loads(dl.read_loads(_ENTRY))
    for key in dl.ALL_KEYS:
        assert written[key] == _ENTRY[key], (key, written[key], _ENTRY[key])
    print("  ✓ read_loads/write_loads round-trips all 22 parallel arrays unchanged")


def test_arrays_stay_index_aligned():
    """Downstream code indexes these lists by device position — a short list silently
    shifts one appliance's setting onto another."""
    loads = dl.read_loads(_ENTRY)
    loads.append(dl.new_load(dl.MONITORED, sensor="sensor.dishwasher"))
    out = dl.write_loads(loads)
    n = len(out[const.CONF_DEFERRABLE_LOAD_SENSORS])
    assert n == 3
    for _, key, _ in dl._MONITORED_MAP:
        assert len(out[key]) == n, (key, len(out[key]))
    print("  ✓ adding a load keeps every monitored array the same length")


def test_hub_lists_every_configured_load():
    f = _flow()
    res = run(f.async_step_loads())
    assert res["type"] == "form" and res["step_id"] == "loads"
    summary = res["description_placeholders"]["summary"]
    assert "Pool Pump" in summary and "EV Charger" in summary
    assert "Hot Water" in summary and "Aircon" in summary
    # The label says how each is wired, so the list answers "which one is wrong".
    assert "metered, modulating" in summary
    assert "metered, forecast only" in summary
    assert "declared" in summary and "estimated" in summary
    print("  ✓ hub lists all four loads with how each is wired")


def test_forecast_only_load_asks_three_questions():
    """A plain metered appliance used to be shown 14 fields. It now gets max power,
    control style, and whether it has a battery — then goes straight back to the hub."""
    f = _flow()
    run(f.async_step_loads())
    res = run(f.async_step_loads({"action": "edit:0"}))  # Pool Pump
    assert res["step_id"] == "load_detail_monitored"
    assert set(_fields(res)) == {"max_kw", "control_style", "has_soc",
                                 "on_controlled_load", "remove"}
    res = run(f.async_step_load_detail_monitored({
        "max_kw": 1.5, "control_style": dl.CONTROL_NONE,
        "has_soc": False, "on_controlled_load": False, "remove": False,
    }))
    assert res["step_id"] == "loads", res["step_id"]
    assert f._loads[0]["max_kw"] == 1.5
    print("  ✓ a forecast-only load answers 3 questions and returns to the hub")


def test_modulating_load_walks_the_setpoint_step():
    f = _flow()
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "edit:1"}))  # EV Charger
    res = run(f.async_step_load_detail_monitored({
        "max_kw": 7.4, "control_style": dl.CONTROL_MODULATING,
        "has_soc": True, "on_controlled_load": False, "remove": False,
    }))
    assert res["step_id"] == "load_control"
    res = run(f.async_step_load_control({"switch": "switch.evse", "climate_on_mode": ""}))
    assert res["step_id"] == "load_modulating"
    assert set(_fields(res)) == {"setpoint", "setpoint_unit", "phases",
                                 "voltage", "min_current", "plug_sensor"}
    res = run(f.async_step_load_modulating({
        "setpoint": "number.evse_current", "setpoint_unit": "a", "phases": "3",
        "voltage": 240.0, "min_current": 6.0, "plug_sensor": "binary_sensor.evse_plug",
    }))
    assert res["step_id"] == "load_soc"
    res = run(f.async_step_load_soc({
        "soc_sensor": "sensor.ev_soc", "soc_max_percent": 90.0, "soc_capacity_kwh": 64.0,
    }))
    assert res["step_id"] == "loads"
    load = f._loads[1]
    assert load["setpoint"] == "number.evse_current" and load["phases"] == 3
    assert load["soc_capacity_kwh"] == 64.0
    print("  ✓ a modulating load walks detail → control → modulating → soc")


def test_downgrading_from_modulating_clears_the_setpoint():
    """Leaving a setpoint entity behind would keep LoadControlManager treating this as a
    modulating load even though the user just told us it is on/off."""
    f = _flow()
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "edit:1"}))
    run(f.async_step_load_detail_monitored({
        "max_kw": 7.4, "control_style": dl.CONTROL_ONOFF,
        "has_soc": False, "on_controlled_load": False, "remove": False,
    }))
    load = f._loads[1]
    assert load["setpoint"] == "" and load["setpoint_unit"] == ""
    assert load["plug_sensor"] == "" and load["phases"] == 0
    assert load["min_current"] == 0.0 and load["voltage"] == 0.0
    # ...and unchecking "has its own battery" resets SOC tracking to its no-op defaults.
    assert load["soc_sensor"] == "" and load["soc_max_percent"] == 100.0
    assert load["soc_capacity_kwh"] == 0.0
    assert dl.control_style(load) == dl.CONTROL_ONOFF
    print("  ✓ switching a load off modulating clears its setpoint and SOC fields")


def test_forecast_only_clears_the_control_entity():
    f = _flow()
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "edit:1"}))
    run(f.async_step_load_detail_monitored({
        "max_kw": 7.4, "control_style": dl.CONTROL_NONE,
        "has_soc": False, "on_controlled_load": False, "remove": False,
    }))
    assert f._loads[1]["switch"] == ""
    assert dl.control_style(f._loads[1]) == dl.CONTROL_NONE
    print("  ✓ setting a load to forecast-only clears its control entity")


def test_declared_load_fields_and_hours_validation():
    f = _flow()
    run(f.async_step_loads())
    res = run(f.async_step_loads({"action": "edit:2"}))  # Hot Water
    assert res["step_id"] == "load_detail_declared"
    assert set(_fields(res)) == {"name", "daily_kwh", "max_kw", "hours",
                                 "on_controlled_load", "remove"}
    res = run(f.async_step_load_detail_declared({
        "name": "Hot Water", "daily_kwh": 8.0, "max_kw": 3.6,
        "hours": "not-an-hour-spec", "on_controlled_load": True, "remove": False,
    }))
    assert res["errors"] == {"hours": "invalid_hours"}
    print("  ✓ a declared load's availability spec is validated")


def test_duplicate_name_is_rejected_at_entry():
    """Declared and estimated loads share one namespace; a duplicate silently merges
    two loads downstream."""
    f = _flow()
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "edit:3"}))  # Aircon (estimated)
    res = run(f.async_step_load_detail_estimated({
        "name": "hot water", "control": "climate.aircon", "est_kw": 2.5,
        "auto": True, "remove": False,
    }))
    assert res["errors"] == {"name": "load_duplicate_name"}, res["errors"]
    print("  ✓ a name already used by another load is rejected, case-insensitively")


def test_estimated_load_requires_both_name_and_control():
    """The old fixed-slot form let a name-without-control slot save, and it was silently
    inert — that cost a real misconfiguration (Daikin AC, 2026-08-06)."""
    f = _flow()
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "add"}))
    run(f.async_step_load_add_estimated())
    res = run(f.async_step_load_detail_estimated({
        "name": "Towel Rail", "control": "", "est_kw": 0.5, "auto": False, "remove": False,
    }))
    assert res["errors"] == {"control": "load_control_required"}
    res = run(f.async_step_load_detail_estimated({
        "name": "", "control": "switch.towel_rail", "est_kw": 0.5,
        "auto": False, "remove": False,
    }))
    assert res["errors"] == {"name": "load_name_required"}
    print("  ✓ an estimated load cannot be saved half-configured")


def test_adding_a_monitored_load_excludes_already_configured_devices():
    f = _flow()
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "add"}))
    res = run(f.async_step_load_add_monitored())
    # sensor.pool and sensor.evse are already loads; only the dishwasher is offered.
    assert res["step_id"] == "load_add_monitored"
    res = run(f.async_step_load_add_monitored({"sensor": "sensor.dishwasher"}))
    assert res["step_id"] == "load_detail_monitored"
    assert f._loads[-1]["sensor"] == "sensor.dishwasher"
    assert f._loads[-1]["max_kw"] == 3.5  # documented default
    print("  ✓ adding a monitored load offers only devices not already configured")


def test_removing_a_load_drops_it_from_the_arrays():
    f = _flow()
    f._loads_only = True
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "edit:0"}))
    res = run(f.async_step_load_detail_monitored({"remove": True}))
    assert res["step_id"] == "loads"
    run(f.async_step_loads({"action": "done"}))
    saved = f.hass.config_entries.updated
    assert saved[const.CONF_DEFERRABLE_LOAD_SENSORS] == ["sensor.evse"]
    assert saved[const.CONF_DEFERRABLE_LOAD_MAX_KW] == [7.4]
    assert saved[const.CONF_DEFERRABLE_LOAD_SETPOINT] == ["number.evse_current"]
    print("  ✓ removing a load drops its entry from every parallel array")


def test_menu_entry_saves_without_walking_the_rest_of_the_wizard():
    f = _flow()
    f._loads_only = True
    run(f.async_step_loads())
    res = run(f.async_step_loads({"action": "done"}))
    assert res["type"] == "create_entry", res
    saved = f.hass.config_entries.updated
    # Merged over the entry, not replacing it — everything untouched survives.
    assert saved[const.CONF_ENERGY_SENSOR] == "sensor.import_energy"
    assert saved[const.CONF_GRIDLENS_API_KEY] == "gl_existing"
    assert saved[const.CONF_DEFERRABLE_LOAD_SENSORS] == ["sensor.pool", "sensor.evse"]
    print("  ✓ the menu path saves and exits, merging rather than replacing entry data")


def test_full_reconfigure_path_continues_to_the_plan_step():
    f = _flow()
    run(f.async_step_loads())
    res = run(f.async_step_loads({"action": "done"}))
    assert res["step_id"] == "current_plan", res.get("step_id")
    print("  ✓ the full-reconfigure path still walks on to the plan step")


def test_controlled_load_questions_are_seeded_from_the_entry():
    """Jumping straight to the wizard never runs async_step_controlled_load, and a
    False there would hide the CL question from a household that has a CL register."""
    f = _flow()
    assert f._has_cl1 is True and f._has_cl2 is False
    run(f.async_step_loads())
    res = run(f.async_step_loads({"action": "edit:0"}))
    assert "on_controlled_load" in _fields(res)
    print("  ✓ CL flags are seeded from the entry, not defaulted to off")


def test_no_controlled_load_means_no_controlled_load_questions():
    entry = dict(_ENTRY)
    entry[const.CONF_HAS_CONTROLLED_LOAD_1] = False
    f = _flow(entry)
    run(f.async_step_loads())
    res = run(f.async_step_loads({"action": "edit:0"}))
    assert "on_controlled_load" not in _fields(res)
    print("  ✓ a household with no CL register is never asked about one")


def test_controlled_load_step_runs_when_opted_in():
    f = _flow()
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "edit:0"}))
    res = run(f.async_step_load_detail_monitored({
        "max_kw": 1.2, "control_style": dl.CONTROL_NONE,
        "has_soc": False, "on_controlled_load": True, "remove": False,
    }))
    assert res["step_id"] == "load_cl"
    res = run(f.async_step_load_cl({
        "controlled_load": "controlled_load_1", "in_aggregate": True,
    }))
    assert res["step_id"] == "loads"
    assert f._loads[0]["controlled_load"] == "controlled_load_1"
    assert f._loads[0]["in_aggregate"] is True
    print("  ✓ opting a load into a CL register runs the CL step and stores it")


def test_opting_out_clears_the_controlled_load_wiring():
    f = _flow()
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "edit:2"}))  # Hot Water, on controlled_load_1
    run(f.async_step_load_detail_declared({
        "name": "Hot Water", "daily_kwh": 8.0, "max_kw": 3.6, "hours": "22-06",
        "on_controlled_load": False, "remove": False,
    }))
    assert f._loads[2]["controlled_load"] == ""
    assert f._loads[2]["in_aggregate"] is False
    print("  ✓ opting a load out of controlled load clears its register")


def test_blank_load_added_then_abandoned_is_not_saved():
    """Adding a declared load and walking to 'done' without naming it must not write an
    empty slot — read_loads would skip it anyway, but the array would be ragged."""
    f = _flow()
    f._loads_only = True
    run(f.async_step_loads())
    run(f.async_step_loads({"action": "add"}))
    run(f.async_step_load_add_declared())
    run(f.async_step_loads({"action": "done"}))
    saved = f.hass.config_entries.updated
    assert saved[const.CONF_DEFERRABLE_LOAD_DUMMY_NAMES] == ["Hot Water"]
    assert saved[const.CONF_DEFERRABLE_LOAD_DUMMY_KWH] == [8.0]
    print("  ✓ an unnamed load abandoned mid-wizard is not written")


def test_entry_written_before_the_wizard_existed_reads_correctly():
    """An entry saved before the modulating/SOC fields existed simply has shorter
    lists. Every missing field must land on its documented default, not crash."""
    legacy = {
        const.CONF_DEFERRABLE_LOAD_SENSORS: ["sensor.pool", "sensor.evse"],
        const.CONF_DEFERRABLE_LOAD_MAX_KW: [1.2],  # deliberately short
    }
    loads = dl.read_loads(legacy)
    assert len(loads) == 2
    assert loads[0]["max_kw"] == 1.2
    assert loads[1]["max_kw"] == 3.5
    assert loads[1]["soc_max_percent"] == 100.0
    assert loads[1]["phases"] == 0
    assert dl.control_style(loads[1]) == dl.CONTROL_NONE
    out = dl.write_loads(loads)
    assert len(out[const.CONF_DEFERRABLE_LOAD_MAX_KW]) == 2
    print("  ✓ a pre-wizard entry reads with defaults and writes back full-length")


def test_more_loads_than_the_old_fixed_slot_limits():
    """The 2 declared / 3 estimated slot caps were a rendering artifact of the old
    forms — nothing downstream depends on them."""
    loads = dl.read_loads(_ENTRY)
    for i in range(4):
        load = dl.new_load(dl.DECLARED, name=f"Declared {i}")
        loads.append(load)
    out = dl.write_loads(loads)
    assert len(out[const.CONF_DEFERRABLE_LOAD_DUMMY_NAMES]) == 5
    assert len(out[const.CONF_DEFERRABLE_LOAD_DUMMY_KWH]) == 5
    assert len(out[const.CONF_DEFERRABLE_LOAD_DUMMY_HOURS]) == 5
    print("  ✓ more declared loads than the old 2-slot cap round-trip fine")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"Running {len(tests)} deferrable-load wizard tests\n")
    for t in tests:
        t()
    print(f"\n✅ all {len(tests)} passed")
