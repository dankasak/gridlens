"""Pure enum + tie-break maths for the per-device "charge timing preference" —
whether a SOC-tracked deferrable load's floor-satisfying charge (the everyday
day-0 ceiling, or an active ad-hoc charge target — see charge_target.py) is
pushed toward the EARLY end of its available window, the LATE end, or left to
whatever the solver's own degenerate-vertex choice picks (this project's
original behaviour, still the default when a user picks "No preference").

No HA imports — kept separate from charge_timing_preference_store.py (the
Store-backed wrapper) so this is unit-testable in a plain Python container that
has no `homeassistant` package installed, same reasoning as charge_target.py /
daily_target_rules.py (see tests/test_charge_timing_preference.py).

Why this exists: battery_optimizer.py's floor constraint (`Σ def_i[t] for
t < floor_slot[i] >= target`) only bounds the CUMULATIVE energy charged by the
deadline — it says nothing about which slots. When several pre-deadline slots
are priced identically (a flat overnight tariff, most commonly), the LP has no
cost reason to prefer one slot over another, and in practice the solver's own
tie-break front-loads the charge — an EV reaches 100% hours before it's needed
and then sits there, which is bad for battery longevity. "Prefer just-in-time"
breaks that tie the other way; "Prefer early" breaks it the original way, on
purpose, for anyone who'd rather have headroom sooner than later.
"""
from __future__ import annotations

PREFER_EARLY = "prefer_early"
NO_PREFERENCE = "no_preference"
PREFER_JUST_IN_TIME = "prefer_just_in_time"

OPTIONS = (PREFER_EARLY, NO_PREFERENCE, PREFER_JUST_IN_TIME)
DEFAULT = PREFER_JUST_IN_TIME

# The label a select.py entity shows, keyed by the stored value — and the
# reverse, for translating a selected label back to the stored value.
LABELS = {
    PREFER_EARLY: "Prefer early",
    NO_PREFERENCE: "No preference",
    PREFER_JUST_IN_TIME: "Prefer just-in-time",
}
VALUES_BY_LABEL = {label: value for value, label in LABELS.items()}

# Total swing of the tie-break across a device's whole pre-floor window, in
# $/kWh — deliberately the SAME proven-safe magnitude as battery_optimizer.py's
# own soc_reward (calibrated there: 0.001 measurably distorts real decisions,
# 0.0003 doesn't — see FEATURES.md). battery_optimizer.py spreads this evenly
# across however many slots a device's own window has, so a long window (a
# next-morning deadline) doesn't get a bigger absolute nudge than a short one
# (a same-afternoon one) — both only ever resolve genuine ties.
TIE_BREAK_MAGNITUDE = 0.0003


def read(data: dict, sensor_id: str) -> str:
    """The stored preference for sensor_id, or DEFAULT if unset or unrecognised
    (e.g. a value written by a future version this one doesn't know)."""
    value = (data.get(sensor_id) or {}).get("preference")
    return value if value in OPTIONS else DEFAULT


def write(data: dict, sensor_id: str, preference: str) -> dict:
    """Return a new dict with sensor_id's preference set. An unrecognised value
    is coerced to DEFAULT rather than stored as garbage the optimizer would
    otherwise have to defend against on every read."""
    data = dict(data)
    data[sensor_id] = {"preference": preference if preference in OPTIONS else DEFAULT}
    return data


def rank(preference: str, t: int, floor_slot: int) -> int:
    """How much tie-break weight slot t (0-indexed, t < floor_slot) should
    carry: 0 is the cheapest/most-preferred slot, floor_slot - 1 the most
    expensive/least-preferred. "Prefer early" makes slot 0 cheapest; anything
    else (including an unrecognised value) makes the last slot before the
    floor cheapest, matching PREFER_JUST_IN_TIME. Callers skip NO_PREFERENCE
    entirely rather than calling this — it isn't a third ranking, it's "add no
    tie-break at all"."""
    return t if preference == PREFER_EARLY else (floor_slot - 1 - t)
