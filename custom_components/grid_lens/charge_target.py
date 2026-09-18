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
    None unless BOTH halves are set. Deliberately stricter than write_target's own
    storage rule: a half-set entry (only percent, or only target_iso, committed so
    far — see read_raw) is real, persisted state while the user is still filling in
    the pair through the two separate tile entities, but it must never read as an
    active target here, since this is what gates the LP optimizer
    (ChargeTargetStore.async_get_active)."""
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


def read_raw(data: dict, sensor_id: str) -> dict | None:
    """The stored entry for sensor_id exactly as held — {"percent": float,
    "target_iso": str} — with no completeness check, unlike read_target. A half-set
    entry (only one of percent/target_iso ever committed) is returned as-is rather
    than None, so each of number.py/datetime.py's paired entities can restore and
    merge its own half without the other one appearing to have been cleared. Never
    use this to decide whether a target is actually live — that's read_target's job."""
    entry = data.get(sensor_id)
    if not entry:
        return None
    try:
        percent = float(entry.get("percent", 0.0))
    except (TypeError, ValueError):
        percent = 0.0
    return {"percent": percent, "target_iso": entry.get("target_iso") or ""}


def write_target(data: dict, sensor_id: str, percent: float, target_iso: str) -> dict:
    """Return a new dict with sensor_id's target set to (percent, target_iso).

    Only clears the entry entirely when BOTH halves are unset (percent <= 0 AND
    target_iso blank) — an explicit clear, as sent by
    services.handle_clear_charge_target and by async_get_active's own
    auto-clear-on-reach-or-expiry, both of which call this with (0.0, ""). A single
    missing half is stored as a genuine partial entry instead of being discarded:
    number.py/datetime.py's paired entities each write only their own half (after
    reading the other back via read_raw to carry it forward), so if percent is set
    before target_iso ever lands in the store, that percent write must survive
    rather than being wiped out the instant it's made because the other half isn't
    there yet. read_target still treats a partial entry as "no active target" for
    the optimizer — only write_target's storage rule changed, not what counts as
    live.
    """
    data = dict(data)
    if percent <= 0 and not target_iso:
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
