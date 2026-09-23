#!/usr/bin/env python3
"""Tests for _day_groups (battery_optimizer.py) — the shared calendar-day
chunking helper behind both the LP's per-device daily-total constraint and
consolidate_deferrable_schedule's post-process.

Regression coverage for the day-boundary fix (2026-09-23): the LP horizon
starts at "now", not local midnight, so the old t // slots_per_day chunking
treated "day 0" as a rolling 24h window from whenever the solve started — a
same-day-scoped Daily Target (e.g. lowered because today is cloudy) could be
satisfied out of TOMORROW's cheaper solar instead, defeating the point of a
same-day target. Live evidence: Wattpilot scaled to 25% (~2.5kWh) on a cloudy
day had its entire target scheduled for the following (sunny) morning instead
of that day — see GRIDLENS_CHECKLIST.md. _day_groups groups by a real
calendar-day key the caller supplies (see retailer_plans.slot_calendar_day_index)
instead of position, fixing this at the source.

Pure Python, no scipy/HA dependency — _day_groups is a module-level helper
with no solver import at module scope (same reasoning as
test_deferrable_consolidation.py — see GRIDLENS_CHECKLIST.md's "no scipy in
the Claude add-on container" note).

Run:  python3 test_day_groups.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_COMPONENT))
sys.path.insert(0, _COMPONENT)

from battery_optimizer import _day_groups  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def test_midnight_start_full_days_no_truncation():
    """Horizon starting exactly at local midnight: every day-chunk is a full
    slots_per_day slots, none truncated — same shape as the old positional
    chunking for this one aligned case."""
    slots_per_day = 4
    slot_day_index = [100] * 4 + [101] * 4 + [102] * 4  # 3 full calendar days
    groups = _day_groups(12, slots_per_day, slot_day_index)
    check("3 day-groups", len(groups) == 3, f"got {len(groups)}")
    check("every group is a full day",
          all(len(slots) == slots_per_day for _, slots in groups))


def test_midday_start_first_and_last_day_short():
    """Horizon starting mid-afternoon: day 0 (today) is short because "now"
    isn't midnight, and the horizon's last day is short because T runs out
    mid-day. Both are real, distinct day-groups — this is the crux of the fix:
    day 0 must be identifiable as "today, correctly bounded", not conflated
    with the old rolling-24h-window definition."""
    slots_per_day = 4
    # today: 2 slots left; tomorrow: a full 4; the day after: only 1 slot in view
    slot_day_index = [200, 200] + [201] * 4 + [202]
    groups = _day_groups(7, slots_per_day, slot_day_index)
    check("3 day-groups", len(groups) == 3, f"got {len(groups)}")
    keys = [k for k, _ in groups]
    check("chronological key order preserved", keys == [200, 201, 202], f"got {keys}")
    check("day 0 (today) is short", len(groups[0][1]) == 2, f"got {len(groups[0][1])}")
    check("day 1 (tomorrow) is a full day", len(groups[1][1]) == slots_per_day)
    check("day 2 (horizon-truncated) is short", len(groups[2][1]) == 1)


def test_single_day_horizon():
    slots_per_day = 4
    slot_day_index = [300, 300, 300]
    groups = _day_groups(3, slots_per_day, slot_day_index)
    check("exactly one day-group", len(groups) == 1, f"got {len(groups)}")
    check("that group holds all 3 slots", groups[0][1] == [0, 1, 2], f"got {groups[0][1]}")


def test_horizon_end_lands_exactly_at_midnight_no_truncation():
    """T landing exactly on a real midnight boundary: the last day-group is a
    full day too, even though the horizon starts mid-day and day 0 is short —
    "short" and "last" are independent facts, both must be tracked correctly."""
    slots_per_day = 4
    slot_day_index = [400, 400] + [401] * 4  # today: 2 slots (short, not last); tomorrow: full
    groups = _day_groups(6, slots_per_day, slot_day_index)
    check("2 day-groups", len(groups) == 2, f"got {len(groups)}")
    check("last day-group is full length", len(groups[-1][1]) == slots_per_day,
          f"got {len(groups[-1][1])}")


def test_none_fallback_reproduces_old_positional_chunking():
    """slot_day_index=None must reproduce the exact old t // slots_per_day
    behaviour byte-for-byte, so any caller not yet updated to supply a real
    calendar day index is completely unaffected by this change."""
    T, slots_per_day = 10, 4
    groups = _day_groups(T, slots_per_day, None)
    expected = [
        (0, [0, 1, 2, 3]),
        (1, [4, 5, 6, 7]),
        (2, [8, 9]),
    ]
    check("matches old positional chunking exactly", groups == expected, f"got {groups}")


if __name__ == "__main__":
    test_midnight_start_full_days_no_truncation()
    test_midday_start_first_and_last_day_short()
    test_single_day_horizon()
    test_horizon_end_lands_exactly_at_midnight_no_truncation()
    test_none_fallback_reproduces_old_positional_chunking()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all _day_groups tests passed.")
