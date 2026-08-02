"""Pure weekly-availability-grid logic for deferrable loads — zero HA imports (same
testability convention as override_expiry.py).

A device's weekly schedule is a 7x48 grid of 0/1: ``week[weekday][slot]`` where
``weekday`` follows Python's ``date.weekday()`` (0 = Monday .. 6 = Sunday) and ``slot``
is a half-hour of the local day (slot 0 = 00:00-00:30, slot 47 = 23:30-24:00); 1 means
the device is allowed to run in that half-hour. 7x24 hourly grids are accepted on input
(each hour expands to its two half-hours) for compatibility, but the canonical stored
form is always 48 slots. A sensor-backed device with no stored grid yet is fully
unrestricted (``week_from_hours(None)``) until the user paints one on the dashboard
schedule card — that card is the only place a sensor-backed device's availability
window is set (the static per-device ``deferrable_load_hours`` config-flow field this
replaced was removed 2026-08-02). ``week_from_hours`` still accepts an explicit hours
set for other callers (e.g. a declared/dummy load with no schedule card of its own).

Storage shape (one dict per config entry, keyed by the device's energy sensor_id, same
keying as DeferrableOverrideStore):

    {sensor_id: {"week": [[0/1 x 48] x 7], "updated": "2026-07-30"}}

Consumers at coarser resolution use ``hour_fraction`` — the allowed fraction of a clock
hour (0, 0.5 or 1) — so an hourly LP can cap a half-allowed hour at half the device's
energy rather than rounding the half-hour away in either direction.
"""
from __future__ import annotations

WEEK_DAYS = 7
DAY_SLOTS = 48   # half-hour resolution
DAY_HOURS = 24   # accepted on input for compatibility; expanded to DAY_SLOTS


def normalize_week(raw) -> list[list[int]]:
    """Validate/coerce a weekly grid into strict 0/1 ints at half-hour resolution.

    Accepts 7 rows of 48 half-hour entries (canonical) or 24 hourly entries (each hour
    expands to two half-hours). Raises ValueError on any other shape — the service
    layer surfaces that to the caller instead of persisting a malformed grid.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != WEEK_DAYS:
        raise ValueError(f"week grid must have exactly {WEEK_DAYS} day rows")
    week: list[list[int]] = []
    for d, row in enumerate(raw):
        if not isinstance(row, (list, tuple)) or len(row) not in (DAY_SLOTS, DAY_HOURS):
            raise ValueError(
                f"day {d} must have {DAY_SLOTS} half-hour (or {DAY_HOURS} hourly) entries"
            )
        try:
            vals = [1 if int(v) else 0 for v in row]
        except (TypeError, ValueError) as err:
            raise ValueError(f"day {d} contains a non-numeric entry: {err}") from err
        if len(vals) == DAY_HOURS:
            vals = [v for v in vals for _ in (0, 1)]  # hour → its two half-hours
        week.append(vals)
    return week


def week_from_hours(allowed_hours) -> list[list[int]]:
    """Build a weekly grid from a static hours set (``parse_hours_spec`` output) — the
    same hours on every day. ``None`` (= "all") returns a fully-allowed grid; this is
    how sensor-backed devices with no painted schedule get their default."""
    if allowed_hours is None:
        return [[1] * DAY_SLOTS for _ in range(WEEK_DAYS)]
    allowed = {int(h) for h in allowed_hours}
    day = [1 if (s // 2) in allowed else 0 for s in range(DAY_SLOTS)]
    return [list(day) for _ in range(WEEK_DAYS)]


def slot_allowed(week: list[list[int]], weekday: int, hour: int, minute: int = 0) -> bool:
    """Is the device allowed to run at (weekday, hour, minute)? Malformed/short rows
    fail open (allowed) — a broken stored grid must never silently pin a device off
    forever."""
    try:
        return bool(week[weekday][hour * 2 + (1 if minute >= 30 else 0)])
    except (IndexError, TypeError):
        return True


def hour_fraction(week: list[list[int]], weekday: int, hour: int) -> float:
    """The allowed fraction (0, 0.5 or 1) of clock-hour ``hour`` on ``weekday`` — for
    hourly-resolution consumers (the plan-comparison LP caps a half-allowed hour's
    energy at half the device's rated hourly energy). Fails open (1.0) on a malformed
    grid, matching slot_allowed."""
    try:
        return (
            (1 if week[weekday][hour * 2] else 0)
            + (1 if week[weekday][hour * 2 + 1] else 0)
        ) / 2.0
    except (IndexError, TypeError):
        return 1.0


def max_daily_hours(week: list[list[int]]) -> float:
    """The largest number of allowed HOURS on any single weekday (half-hours count
    0.5) — used for the 'window too narrow for the daily kWh target' feasibility
    warning."""
    if not week:
        return 0.0
    return max((sum(1 for v in row if v) for row in week), default=0) / 2.0


def rolling_window_hours(week, weekday: int, hour: int, minute: int = 0,
                         span_slots: int = DAY_SLOTS) -> float:
    """Allowed HOURS in the ``span_slots`` half-hours starting at a given local
    weekday/time, wrapping around the week.

    This — not ``max_daily_hours`` — is what actually bounds a *today* target: the
    optimizer's day-chunks are anchored to the horizon start ("now"), not local
    midnight, so the relevant window straddles two weekdays and excludes the part of
    today that has already elapsed. A device allowed 00:00-09:00 + 21:00-24:00 has 12
    allowed hours on any given day, but only 11.5 of them are still reachable in the
    24 hours following 21:30 (see battery_optimizer's per-day target clamp).

    Fails open (counts a slot as allowed) on a malformed grid, matching slot_allowed.
    """
    if not week:
        return 0.0
    start = weekday * DAY_SLOTS + hour * 2 + (1 if minute >= 30 else 0)
    total = 0
    for k in range(int(span_slots)):
        d, s = divmod((start + k) % (WEEK_DAYS * DAY_SLOTS), DAY_SLOTS)
        try:
            allowed = bool(week[d][s])
        except (IndexError, TypeError):
            allowed = True
        total += 1 if allowed else 0
    return total / 2.0


def read_week(data: dict, sensor_id: str):
    """The stored weekly grid for sensor_id, or None if unset/malformed."""
    entry = (data or {}).get(sensor_id)
    if not isinstance(entry, dict):
        return None
    try:
        return normalize_week(entry.get("week"))
    except ValueError:
        return None


def write_week(data: dict, sensor_id: str, week, today: str) -> dict:
    """Return a new store dict with sensor_id's weekly grid set (or cleared if week is
    None). Raises ValueError on a malformed grid — never persists one."""
    out = dict(data or {})
    if week is None:
        out.pop(sensor_id, None)
    else:
        out[sensor_id] = {"week": normalize_week(week), "updated": today}
    return out
