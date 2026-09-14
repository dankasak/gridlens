"""Pure maths for an ad-hoc, one-off "charge to X% by a datetime" target on an
SOC-tracked deferrable load (grid_lens.set_charge_target / the paired
number.*_charge_target_percent + datetime.*_charge_target_time entities) —
e.g. "charge the EV to 100% by 7am Saturday" before a trip.

No HA imports — kept separate from charge_target_store.py (the Store-backed
wrapper) so this rule is unit-testable in a plain Python container that has no
`homeassistant` package installed (see tests/test_charge_target.py), matching
override_expiry.py's split for the same reason.

Deliberately NOT the same shape as the "Today Boost" override
(deferrable_overrides.py): a boost has no natural end condition (it's a daily
habit override) so it persists until manually cleared. A dated target DOES
have a natural end condition — the deadline itself — so it auto-clears once
either the target percent is reached or the deadline passes, no manual
clearing required. See is_reached / is_expired.
"""
from __future__ import annotations

import datetime as _dt
import math as _math


def read_target(data: dict, sensor_id: str) -> dict | None:
    """The stored target for sensor_id — {"percent": float, "target_iso": str} — or
    None if unset. A percent <= 0 or a missing/blank target_iso is treated as unset
    (mirrors write_target's own clearing rule)."""
    entry = data.get(sensor_id)
    if not entry:
        return None
    try:
        percent = float(entry.get("percent", 0.0))
    except (TypeError, ValueError):
        return None
    target_iso = entry.get("target_iso") or ""
    if percent <= 0 or not target_iso:
        return None
    return {"percent": percent, "target_iso": target_iso}


def write_target(data: dict, sensor_id: str, percent: float, target_iso: str) -> dict:
    """Return a new dict with sensor_id's target set to (percent, target_iso).

    Clears the target entirely (rather than storing a partial/zero entry) when
    either half is missing — percent <= 0, or target_iso blank — so a cleared
    device reads back as "no target" from read_target with nothing stale left over.
    """
    data = dict(data)
    if percent <= 0 or not target_iso:
        data.pop(sensor_id, None)
        return data
    data[sensor_id] = {"percent": float(percent), "target_iso": target_iso}
    return data


def is_reached(percent: float, current_percent: float | None) -> bool:
    """True once the device's live SOC has already met/passed the target — the target
    has done its job and should auto-clear rather than keep pinning a floor."""
    if current_percent is None:
        return False
    return current_percent >= percent - 1e-6


def is_expired(target_dt: _dt.datetime, now: _dt.datetime) -> bool:
    """True once the deadline itself has passed (whether or not it was ever met) —
    the target is stale either way and should auto-clear rather than linger."""
    return now >= target_dt


def slot_for_datetime(
    target_dt: _dt.datetime, horizon_start: _dt.datetime, slot_minutes: int, horizon_slots: int
) -> int | None:
    """The horizon slot index nearest target_dt, or None if it falls outside the
    current rolling horizon — before it (already due/expired for this solve) or
    beyond its last slot (not yet in view; the LP will pick it up once a later
    rolling replan's horizon reaches it, same as any other future information this
    optimizer has no way to act on early).

    Rounds UP (ceil) rather than to nearest: a target reached one slot early is a
    non-event, one slot late is a broken promise — so when the deadline falls
    inside a slot rather than exactly on a boundary, the floor must bind at the
    slot that ENDS at or after the deadline, never the one before it.
    """
    delta_minutes = (target_dt - horizon_start).total_seconds() / 60.0
    if delta_minutes <= 0:
        return None
    slot = _math.ceil(delta_minutes / slot_minutes)
    if slot <= 0 or slot > horizon_slots:
        return None
    return slot
