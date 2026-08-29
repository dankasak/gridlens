"""One-dict-per-load view over the parallel arrays that store deferrable loads.

Config-entry data keeps every deferrable-load attribute in its own list, index-aligned
with a "spine" list — `CONF_DEFERRABLE_LOAD_SENSORS` for monitored loads,
`CONF_DEFERRABLE_LOAD_DUMMY_NAMES` for declared ones, `CONF_DEFERRABLE_LOAD_EST_NAMES`
for estimated ones. Thirteen parallel lists for the monitored kind alone. Every consumer
(`plan_calculator.py`, `__init__.py`, `control/load_control_manager.py`, `sensor.py`,
`number.py`) zips them back together positionally, so the on-disk shape cannot change
without touching all of them at once.

This module is the seam that avoids that. `read_loads()` turns the arrays into one dict
per load; `write_loads()` turns them back, same length and index-aligned, byte-for-byte
the shape they were. Nothing downstream sees a difference — but the config flow gets to
work with a list of loads, which is what a per-load wizard needs and what the parallel
arrays make painful (the options flow previously spent ~100 lines rebuilding
`sensor_id -> value` dicts before it could render a single field).

Deliberately free of Home Assistant imports so it can be exercised offline.

Slot counts: `read_loads` / `write_loads` impose no limit on how many loads of a kind
may exist. The `DEFERRABLE_LOAD_DUMMY_SLOTS` / `DEFERRABLE_LOAD_ESTIMATED_SLOTS`
constants were only ever a config-flow rendering artifact (how many fixed slots to draw
on one form) — every consumer iterates whatever length it is handed.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .const import (
    CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE,
    CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE,
    CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD,
    CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE,
    CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD,
    CONF_DEFERRABLE_LOAD_DUMMY_HOURS,
    CONF_DEFERRABLE_LOAD_DUMMY_KWH,
    CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW,
    CONF_DEFERRABLE_LOAD_DUMMY_NAMES,
    CONF_DEFERRABLE_LOAD_EST_AUTO,
    CONF_DEFERRABLE_LOAD_EST_CONTROL,
    CONF_DEFERRABLE_LOAD_EST_KW,
    CONF_DEFERRABLE_LOAD_EST_NAMES,
    CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_MIN_CURRENT,
    CONF_DEFERRABLE_LOAD_PHASES,
    CONF_DEFERRABLE_LOAD_PLUG_SENSOR,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_SETPOINT,
    CONF_DEFERRABLE_LOAD_SETPOINT_UNIT,
    CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH,
    CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT,
    CONF_DEFERRABLE_LOAD_SOC_SENSORS,
    CONF_DEFERRABLE_LOAD_SWITCHES,
    CONF_DEFERRABLE_LOAD_VOLTAGE,
)

# --- load kinds -------------------------------------------------------------------
# What the user actually has, which is what decides every field the wizard asks for:
MONITORED = "monitored"  # an HA energy sensor measures it
DECLARED = "declared"    # no sensor and nothing to actuate — an estimated daily kWh
ESTIMATED = "estimated"  # controllable, but no sensor — LoadEstimator infers its draw

# --- control styles (monitored loads only) ------------------------------------------
# Derived from which entities are set rather than stored separately, so an entry saved
# before this module existed classifies correctly with no migration.
CONTROL_NONE = "none"              # forecast-only; Grid Lens never actuates it
CONTROL_ONOFF = "onoff"            # switch.*/climate.* turned on and off ("type 1")
CONTROL_MODULATING = "modulating"  # number.* setpoint ramped up and down ("type 2")

# Per-field defaults. These are the values the pre-wizard config flow wrote for an
# unanswered field, and the values every consumer treats as "not configured" — keep them
# in step with the `.get(..., default)` calls in plan_calculator.py / __init__.py.
_MONITORED_DEFAULTS: dict[str, Any] = {
    "max_kw": 3.5,
    "switch": "",
    "climate_on_mode": "",
    "soc_sensor": "",
    "soc_max_percent": 100.0,
    "soc_capacity_kwh": 0.0,
    "controlled_load": "",
    "in_aggregate": False,
    "setpoint": "",
    "setpoint_unit": "",
    "phases": 0,
    "voltage": 0.0,
    "min_current": 0.0,
    "plug_sensor": "",
}

_DECLARED_DEFAULTS: dict[str, Any] = {
    "daily_kwh": 0.0,
    "max_kw": 3.5,
    "hours": "all",
    "controlled_load": "",
    "in_aggregate": False,
}

_ESTIMATED_DEFAULTS: dict[str, Any] = {
    "control": "",
    "est_kw": 1.0,
    "auto": False,
}

# field name -> (config key, default). Order is the on-disk array order and is not
# significant, but keeping monitored fields together keeps write_loads readable.
_MONITORED_MAP: tuple[tuple[str, str, Any], ...] = (
    ("max_kw", CONF_DEFERRABLE_LOAD_MAX_KW, 3.5),
    ("switch", CONF_DEFERRABLE_LOAD_SWITCHES, ""),
    ("climate_on_mode", CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE, ""),
    ("soc_sensor", CONF_DEFERRABLE_LOAD_SOC_SENSORS, ""),
    ("soc_max_percent", CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT, 100.0),
    ("soc_capacity_kwh", CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH, 0.0),
    ("controlled_load", CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD, ""),
    ("in_aggregate", CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE, False),
    ("setpoint", CONF_DEFERRABLE_LOAD_SETPOINT, ""),
    ("setpoint_unit", CONF_DEFERRABLE_LOAD_SETPOINT_UNIT, ""),
    ("phases", CONF_DEFERRABLE_LOAD_PHASES, 0),
    ("voltage", CONF_DEFERRABLE_LOAD_VOLTAGE, 0.0),
    ("min_current", CONF_DEFERRABLE_LOAD_MIN_CURRENT, 0.0),
    ("plug_sensor", CONF_DEFERRABLE_LOAD_PLUG_SENSOR, ""),
)

_DECLARED_MAP: tuple[tuple[str, str, Any], ...] = (
    ("daily_kwh", CONF_DEFERRABLE_LOAD_DUMMY_KWH, 0.0),
    ("max_kw", CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW, 3.5),
    ("hours", CONF_DEFERRABLE_LOAD_DUMMY_HOURS, "all"),
    ("controlled_load", CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD, ""),
    ("in_aggregate", CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE, False),
)

_ESTIMATED_MAP: tuple[tuple[str, str, Any], ...] = (
    ("control", CONF_DEFERRABLE_LOAD_EST_CONTROL, ""),
    ("est_kw", CONF_DEFERRABLE_LOAD_EST_KW, 1.0),
    ("auto", CONF_DEFERRABLE_LOAD_EST_AUTO, False),
)

# Every config key this module owns. `write_loads` always emits all of them, so a load
# removed in the wizard actually disappears from the entry rather than lingering because
# its key went unwritten.
ALL_KEYS: tuple[str, ...] = (
    (CONF_DEFERRABLE_LOAD_SENSORS,)
    + tuple(key for _, key, _ in _MONITORED_MAP)
    + (CONF_DEFERRABLE_LOAD_DUMMY_NAMES,)
    + tuple(key for _, key, _ in _DECLARED_MAP)
    + (CONF_DEFERRABLE_LOAD_EST_NAMES,)
    + tuple(key for _, key, _ in _ESTIMATED_MAP)
)


def _at(seq: Sequence[Any], i: int, default: Any) -> Any:
    """Positional read with the same "short list means default" tolerance every
    consumer already applies — an entry saved before a field existed simply has a
    shorter list for it."""
    if i < len(seq):
        value = seq[i]
        # `None` reaches here from a cleared EntitySelector round-tripped through
        # storage; treat it as unset rather than propagating None into a float().
        if value is not None:
            return value
    return default


def _coerce(value: Any, default: Any) -> Any:
    """Coerce to the default's type, falling back to the default on junk. Guards the
    float()/int() calls that the old per-field list comprehensions did inline."""
    try:
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, int):
            return int(value)
        return str(value or "")
    except (TypeError, ValueError):
        return default


def read_loads(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return one dict per configured deferrable load, in a stable order:
    monitored first, then declared, then estimated.

    Each dict carries `kind`, an identity (`sensor` for monitored, `name` for the
    other two) and only the fields that kind uses. Blank spine entries are skipped —
    the fixed-slot forms wrote `""` for unused slots, and those are not loads.
    """
    loads: list[dict[str, Any]] = []

    for i, sensor in enumerate(data.get(CONF_DEFERRABLE_LOAD_SENSORS, []) or []):
        if not sensor:
            continue
        load: dict[str, Any] = {"kind": MONITORED, "sensor": str(sensor)}
        for field, key, default in _MONITORED_MAP:
            load[field] = _coerce(_at(data.get(key, []) or [], i, default), default)
        loads.append(load)

    for i, name in enumerate(data.get(CONF_DEFERRABLE_LOAD_DUMMY_NAMES, []) or []):
        if not str(name or "").strip():
            continue
        load = {"kind": DECLARED, "name": str(name).strip()}
        for field, key, default in _DECLARED_MAP:
            load[field] = _coerce(_at(data.get(key, []) or [], i, default), default)
        loads.append(load)

    for i, name in enumerate(data.get(CONF_DEFERRABLE_LOAD_EST_NAMES, []) or []):
        if not str(name or "").strip():
            continue
        load = {"kind": ESTIMATED, "name": str(name).strip()}
        for field, key, default in _ESTIMATED_MAP:
            load[field] = _coerce(_at(data.get(key, []) or [], i, default), default)
        loads.append(load)

    return loads


def write_loads(loads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Flatten loads back into the parallel arrays, index-aligned and equal-length.

    Returns every key in `ALL_KEYS`, so the result can be merged straight over config
    entry data. Any field a load omits is written at its documented default rather than
    left short — downstream code indexes these lists by device position and a ragged
    list silently shifts one device's setting onto another.
    """
    monitored = [l for l in loads if l.get("kind") == MONITORED]
    declared = [l for l in loads if l.get("kind") == DECLARED]
    estimated = [l for l in loads if l.get("kind") == ESTIMATED]

    out: dict[str, Any] = {
        CONF_DEFERRABLE_LOAD_SENSORS: [str(l.get("sensor", "")) for l in monitored],
        CONF_DEFERRABLE_LOAD_DUMMY_NAMES: [str(l.get("name", "")).strip() for l in declared],
        CONF_DEFERRABLE_LOAD_EST_NAMES: [str(l.get("name", "")).strip() for l in estimated],
    }
    for group, field_map in (
        (monitored, _MONITORED_MAP),
        (declared, _DECLARED_MAP),
        (estimated, _ESTIMATED_MAP),
    ):
        for field, key, default in field_map:
            out[key] = [_coerce(l.get(field, default), default) for l in group]
    return out


def new_load(kind: str, *, sensor: str = "", name: str = "") -> dict[str, Any]:
    """A fresh load of `kind`, every field at the default the old fixed-slot forms
    wrote. Callers supply the identity: `sensor` for monitored, `name` for the rest."""
    if kind == MONITORED:
        return {"kind": MONITORED, "sensor": sensor, **_MONITORED_DEFAULTS}
    if kind == DECLARED:
        return {"kind": DECLARED, "name": name, **_DECLARED_DEFAULTS}
    if kind == ESTIMATED:
        return {"kind": ESTIMATED, "name": name, **_ESTIMATED_DEFAULTS}
    raise ValueError(f"unknown deferrable load kind: {kind!r}")


def control_style(load: Mapping[str, Any]) -> str:
    """Classify a monitored load's control wiring from the entities it has set.

    Derived rather than stored: a setpoint entity is what makes a load modulating (see
    `control/load_control_manager.py`), and a switch alone makes it on/off. This keeps
    entries written before the wizard existed classifying correctly with no migration.
    """
    if load.get("setpoint"):
        return CONTROL_MODULATING
    if load.get("switch"):
        return CONTROL_ONOFF
    return CONTROL_NONE


def apply_control_style(load: dict[str, Any], style: str) -> None:
    """Clear the fields a style doesn't use, so switching a load from modulating back to
    on/off doesn't leave a stale setpoint entity behind still driving `LoadControlManager`
    down the modulating path. Mutates `load` in place.
    """
    if style != CONTROL_MODULATING:
        for field in ("setpoint", "setpoint_unit", "plug_sensor"):
            load[field] = _MONITORED_DEFAULTS[field]
        for field in ("phases", "voltage", "min_current"):
            load[field] = _MONITORED_DEFAULTS[field]
    if style == CONTROL_NONE:
        load["switch"] = ""
        load["climate_on_mode"] = ""


def clear_soc(load: dict[str, Any]) -> None:
    """Reset the SOC-tracking block to its no-op defaults."""
    load["soc_sensor"] = ""
    load["soc_max_percent"] = _MONITORED_DEFAULTS["soc_max_percent"]
    load["soc_capacity_kwh"] = _MONITORED_DEFAULTS["soc_capacity_kwh"]


def clear_controlled_load(load: dict[str, Any]) -> None:
    """Reset the Controlled Load wiring to 'not on controlled load'."""
    load["controlled_load"] = ""
    load["in_aggregate"] = False
