"""Pure carry-over logic for per-day deferrable load overrides.

No HA imports — kept separate from deferrable_overrides.py (the Store-backed
wrapper) so this rule is unit-testable in a plain Python container that has no
`homeassistant` package installed (see tests/test_deferrable_override.py).

An override no longer auto-expires at local midnight (it used to — a plan built
before midnight that relied on a boosted target for a still-future slot would
have the boost silently vanish out from under it the moment the calendar date
ticked over, mid-plan, with no notice). It now persists until the user
explicitly clears it (sets it back to 0). `should_notify_carryover` instead
flags — once per calendar day — that an active override has carried over past
the day it was set for, so the caller can surface a notification rather than
silently reverting it.
"""
from __future__ import annotations


def read_value(data: dict, sensor_id: str) -> float:
    """The stored override kWh for sensor_id, or 0.0 if unset."""
    entry = data.get(sensor_id)
    if not entry:
        return 0.0
    try:
        return max(0.0, float(entry.get("value_kwh", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def should_notify_carryover(data: dict, sensor_id: str, today: str) -> bool:
    """True the first time `today` differs from both the override's set_date and
    the date it was last flagged — i.e. once per calendar day that an active
    override is still in effect past the day it was originally set."""
    entry = data.get(sensor_id)
    if not entry or read_value(data, sensor_id) <= 0:
        return False
    if entry.get("set_date") == today:
        return False
    return entry.get("last_notified_date") != today


def mark_notified(data: dict, sensor_id: str, today: str) -> dict:
    """Record that today's carry-over notice has been shown for sensor_id."""
    data = dict(data)
    entry = data.get(sensor_id)
    if entry:
        data[sensor_id] = {**entry, "last_notified_date": today}
    return data


def write_value(data: dict, sensor_id: str, value_kwh: float, today: str) -> dict:
    """Return a new dict with sensor_id's override set to (value_kwh, today).

    0 (or negative) clears the override entirely rather than storing a zero entry,
    so a cleared device reads back as "no override" with nothing left to carry
    over. A fresh write also drops any prior `last_notified_date` (the new dict
    only carries `value_kwh`/`set_date`), so a re-boost on a later day is free to
    notify again on its own schedule.
    """
    data = dict(data)
    if value_kwh <= 0:
        data.pop(sensor_id, None)
    else:
        data[sensor_id] = {"value_kwh": value_kwh, "set_date": today}
    return data
