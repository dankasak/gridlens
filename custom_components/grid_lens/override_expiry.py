"""Pure expiry logic for per-day deferrable load overrides.

No HA imports — kept separate from deferrable_overrides.py (the Store-backed
wrapper) so this rule is unit-testable in a plain Python container that has no
`homeassistant` package installed (see tests/test_deferrable_override.py).
"""
from __future__ import annotations


def read_value(data: dict, sensor_id: str, today: str) -> float:
    """0.0 if unset or set on a different local date than `today`, else the stored kWh."""
    entry = data.get(sensor_id)
    if not entry or entry.get("set_date") != today:
        return 0.0
    try:
        return max(0.0, float(entry.get("value_kwh", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def write_value(data: dict, sensor_id: str, value_kwh: float, today: str) -> dict:
    """Return a new dict with sensor_id's override set to (value_kwh, today).

    0 (or negative) clears the override entirely rather than storing a zero entry,
    so a cleared device reads back as "no override" with no expiry to reason about.
    """
    data = dict(data)
    if value_kwh <= 0:
        data.pop(sensor_id, None)
    else:
        data[sensor_id] = {"value_kwh": value_kwh, "set_date": today}
    return data
