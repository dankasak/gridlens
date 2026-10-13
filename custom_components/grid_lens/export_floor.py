"""Pure maths for an ad-hoc, one-off "don't discharge/export below X% until a
datetime" reserve on the MAIN house battery (grid_lens.set_export_floor / the
paired number.*_export_floor_percent + datetime.*_export_floor_until entities)
— e.g. "keep at least 50% in reserve until 7am Tuesday" ahead of a planned
grid outage.

This is the mirror image of charge_target.py's "charge to X% by a datetime"
(which raises the LP's day-0 ceiling to force charging UP): this instead
raises the LP's per-slot lower bound so the optimizer may still discharge/
export freely for economics, but never below the floor, for every slot
between now and the deadline. Both features reuse the same
slot_for_datetime — the "which horizon slot does this deadline fall in"
question is identical either direction.

No HA imports — kept separate from export_floor_store.py (the Store-backed
wrapper) so this rule is unit-testable in a plain Python container with no
`homeassistant` package installed, matching charge_target.py's own split.

Single target per config entry (the main battery is singular, unlike
deferrable loads which are a list keyed by sensor_id) — so, unlike
charge_target's read_target(data, sensor_id), this reads/writes the whole
dict directly.

Deliberately does NOT import charge_target.slot_for_datetime, even though the
"which horizon slot does this deadline fall in" maths is identical either
direction — importing a sibling module here would break this file's own
standalone-testability (see charge_target.py's own docstring on why these
pure-math modules avoid even sibling imports). Callers (advisory/coordinator.py)
import charge_target.slot_for_datetime directly instead.
"""
from __future__ import annotations

import datetime as _dt


def read_target(data: dict) -> dict | None:
    """The stored floor — {"percent": float, "until_iso": str} — or None unless
    BOTH halves are set. Deliberately stricter than write_target's own storage
    rule, same reasoning as charge_target.read_target: a half-set entry is real
    persisted state while the user is still filling in the paired number/
    datetime entities, but must never read as an active floor here (this is
    what gates the LP optimizer, via ExportFloorStore.async_get_active)."""
    if not data:
        return None
    try:
        percent = float(data.get("percent", 0.0))
    except (TypeError, ValueError):
        return None
    until_iso = data.get("until_iso") or ""
    if percent <= 0 or not until_iso:
        return None
    return {"percent": percent, "until_iso": until_iso}


def read_raw(data: dict) -> dict:
    """The stored entry exactly as held — {"percent": float, "until_iso": str}
    — with no completeness check, unlike read_target. A half-set entry (only
    one of the two paired entities committed so far) is returned as-is so each
    entity can restore/merge its own half without the other appearing cleared.
    Never use this to gate control logic — see read_target/async_get_active."""
    if not data:
        return {"percent": 0.0, "until_iso": ""}
    try:
        percent = float(data.get("percent", 0.0))
    except (TypeError, ValueError):
        percent = 0.0
    return {"percent": percent, "until_iso": data.get("until_iso") or ""}


def write_target(data: dict, percent: float, until_iso: str) -> dict:
    """Return a new dict holding (percent, until_iso). Only clears entirely when
    BOTH halves are unset (percent <= 0 AND until_iso blank) — an explicit
    clear, as sent by services.handle_clear_export_floor and by
    async_get_active's own auto-clear-on-expiry, both of which call this with
    (0.0, ""). A single missing half is stored as a genuine partial entry
    instead of being discarded, same reasoning as charge_target.write_target."""
    if percent <= 0 and not until_iso:
        return {}
    return {"percent": float(percent), "until_iso": until_iso}


def is_expired(until_dt: _dt.datetime, now: _dt.datetime) -> bool:
    """True once the deadline itself has passed — the floor has done its job
    (or was never reached in time either way) and should auto-clear rather
    than keep pinning a reserve indefinitely. Unlike charge_target, there is
    no "is_reached" companion: a floor has nothing to reach, it just holds
    until the clock runs out."""
    return now >= until_dt
