"""Pure carry-over logic for the Daily Target per-device/master percent targets.

No HA imports — kept separate from daily_targets.py (the Store-backed wrapper)
so this rule is unit-testable in a plain Python container that has no
`homeassistant` package installed, same reasoning as override_expiry.py (see
tests/test_daily_targets.py).

Deliberately NOT a copy of override_expiry.py's write_value, because that
module's "0 (or negative) clears the entry" convention doesn't hold here: 0 kWh
is a meaningless "Today Boost" (nothing to apply), but 0% is a *meaningful*
Daily Target ("skip this device today/tomorrow, it's going to rain").
Clearing an entry (falling back to following the master slider) is therefore
its own explicit action (`clear_percent`), never implied by writing a
particular numeric value — see FEATURES.md §9b.

Named "Daily Target", not "Tomorrow" (its original name, changed 2026-09-22):
the LP applies the scaled figure to EVERY day-chunk in its rolling horizon, not
one calendar date, and it takes effect from the next ~2-min advisory tick
regardless of what time of day you set it — set it at 8am on a rainy morning
and it applies to whatever's left of today (and every day after, until you
change it back), not just "tomorrow". See FEATURES.md §9b for the full
mechanics, including the important caveat that it only affects energy not yet
drawn — it can't claw back a charge that already finished earlier today.

Like Today Boost (and unlike the ad-hoc charge target), a set percent does NOT
auto-expire at local midnight. It used to for Today Boost, and that was reverted
after a real incident: the advisory LP plans on a rolling horizon, so a plan
built before midnight can already be relying on a scaled target for a
still-future (post-midnight) slot — auto-expiring it mid-plan would silently
revert daily_kwh back to 100% under a plan that already committed to the lower
number, with no notice. Instead `should_notify_carryover` flags — once per
calendar day — that an active override has carried over past the day it was
set for, so the caller can surface a reminder rather than silently reverting.
"""
from __future__ import annotations

MASTER_KEY = "__master__"


def read_percent(data: dict, key: str) -> float | None:
    """The stored percent for `key` (device sensor_id, or MASTER_KEY), or None if
    unset. Unlike override_expiry.read_value this can legitimately return 0.0 —
    callers must check for None (unset) separately, not falsiness."""
    entry = data.get(key)
    if not entry:
        return None
    try:
        return max(0.0, float(entry.get("percent")))
    except (TypeError, ValueError):
        return None


def should_notify_carryover(data: dict, key: str, today: str) -> bool:
    """True the first time `today` differs from both the target's set_date and
    the date it was last flagged — i.e. once per calendar day that an explicit
    override is still in effect past the day it was originally set. False for
    an unset key (nothing to carry over) — note this checks *presence*, not
    percent > 0, since 0% is a legitimate active override."""
    entry = data.get(key)
    if not entry or read_percent(data, key) is None:
        return False
    if entry.get("set_date") == today:
        return False
    return entry.get("last_notified_date") != today


def mark_notified(data: dict, key: str, today: str) -> dict:
    """Record that today's carry-over notice has been shown for `key`."""
    data = dict(data)
    entry = data.get(key)
    if entry:
        data[key] = {**entry, "last_notified_date": today}
    return data


def write_percent(data: dict, key: str, percent: float, today: str) -> dict:
    """Return a new dict with `key`'s target set to (percent, today). Always
    stores — including 0.0 — since 0% is meaningful here. A fresh write drops
    any prior `last_notified_date`, so a later re-set is free to notify again
    on its own schedule. Negative input is clamped to 0.0 rather than treated
    as a clear (use `clear_percent` for that)."""
    data = dict(data)
    data[key] = {"percent": max(0.0, float(percent)), "set_date": today}
    return data


def clear_percent(data: dict, key: str) -> dict:
    """Remove `key`'s explicit override entirely, so it goes back to following
    the master percent (or, for the master key itself, back to the 100% default)."""
    data = dict(data)
    data.pop(key, None)
    return data
