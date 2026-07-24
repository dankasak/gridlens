#!/usr/bin/env python3
"""Tests for consolidate_deferrable_schedule (Feature 3a follow-up,
DEFERRABLE_EXPORT_CONTROL_PLAN.md).

Pure Python, no scipy/HA dependency — battery_optimizer.py only imports numpy/
scipy inside the solver methods, so this module-level helper is directly
importable and testable in this container (see GRIDLENS_CHECKLIST.md's "no
scipy in the Claude add-on container" note). Covers the logic itself against
hand-built `schedule` fixtures; a real scipy solve producing this exact
fragmented pattern was observed live 2026-07-24 (EV charger toggling
full-power on/off across an afternoon at $0 marginal cost either way) — see
the checklist entry for that live evidence.

Run:  python3 test_deferrable_consolidation.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_COMPONENT))
sys.path.insert(0, _COMPONENT)

from battery_optimizer import consolidate_deferrable_schedule  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def _row(import_kwh=0.0, export_kwh=0.0, import_rate=0.0, export_rate=0.0, defer=None):
    defer = defer or [0.0]
    return {
        'import_kwh': import_kwh, 'export_kwh': export_kwh,
        'import_rate': import_rate, 'export_rate': export_rate,
        'import_cost': import_kwh * import_rate, 'export_credit': export_kwh * export_rate,
        'deferrable_per_device': list(defer),
        'deferrable_kwh': sum(defer),
    }


def _total_cost(schedule):
    return sum(r['import_cost'] - r['export_credit'] for r in schedule)


def test_fragmented_zero_cost_slots_get_consolidated():
    """Mirrors the live case: several $0-export-rate slots each carrying 0.9kWh
    of a device with 1.8kW max (0.9kWh/slot at 30-min = full power), with a gap
    slot at 0 in the middle. Same total, same cost either way — should merge
    into a single leading block."""
    dev = [{'max_kw': 1.8, 'daily_kwh': 3.6, 'hour_mask': None}]
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),  # the gap
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
    ]
    before_cost = _total_cost(schedule)
    before_total = sum(r['deferrable_per_device'][0] for r in schedule)

    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)

    after_total = sum(r['deferrable_per_device'][0] for r in schedule)
    after_cost = _total_cost(schedule)
    on_pattern = [r['deferrable_per_device'][0] > 0 for r in schedule]
    check("daily total energy is preserved", abs(after_total - before_total) < 1e-6,
          f"before={before_total} after={after_total}")
    check("cost never regresses", after_cost <= before_cost + 1e-6,
          f"before={before_cost} after={after_cost}")
    check("gap is closed — device now runs a single contiguous leading block",
          on_pattern == [True, True, True, True, False], f"pattern={on_pattern}")


def test_never_increases_cost_across_different_priced_slots():
    """If slots have genuinely different rates, consolidating must never make
    the plan more expensive — reject any move that would."""
    dev = [{'max_kw': 2.0, 'daily_kwh': 2.0, 'hour_mask': None}]
    # Slot 0 is cheap import (0.10/kWh), slot 1 is expensive import (0.50/kWh).
    # Moving slot 1's energy earlier (into slot 0, which has headroom) only
    # ever reduces cost here, so it should be accepted.
    schedule = [
        _row(import_kwh=0.0, import_rate=0.10, defer=[0.0]),
        _row(import_kwh=1.0, import_rate=0.50, defer=[1.0]),
    ]
    before_cost = _total_cost(schedule)
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    after_cost = _total_cost(schedule)
    check("cost does not increase when front-loading into a cheaper slot",
          after_cost <= before_cost + 1e-6, f"before={before_cost} after={after_cost}")


def test_rejects_move_that_would_raise_cost():
    """Construct a case where the only 'more contiguous' arrangement would cost
    more — the pass must leave the original schedule untouched rather than
    silently making the bill worse."""
    dev = [{'max_kw': 2.0, 'daily_kwh': 1.0, 'hour_mask': None}]
    # Slot 0 is expensive import-only (no export headroom to soak up more
    # deferrable cheaply); slot 1 is $0 export (currently absorbing the load
    # for free). Front-loading into slot 0 would turn a free slot's energy
    # into paid grid import — must be rejected.
    schedule = [
        _row(import_kwh=0.5, import_rate=1.00, defer=[0.0]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[1.0]),
    ]
    before = [dict(r) for r in schedule]
    before_cost = _total_cost(schedule)
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    after_cost = _total_cost(schedule)
    check("cost-increasing consolidation is rejected outright",
          after_cost <= before_cost + 1e-6, f"before={before_cost} after={after_cost}")
    check("schedule left unchanged when the only move available would cost more",
          all(schedule[i]['deferrable_per_device'] == before[i]['deferrable_per_device']
              for i in range(len(schedule))))


def test_protected_hours_are_never_touched():
    """Demand-window / capped-rate / conditional-credit hours must be excluded
    entirely — this pass has no way to correctly re-derive those side
    constraints, so it must leave them exactly as solved."""
    dev = [{'max_kw': 1.0, 'daily_kwh': 1.0, 'hour_mask': None}]
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.5]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),  # protected — should stay 0
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.5]),
    ]
    consolidate_deferrable_schedule(
        schedule, dev, dt=0.5, slots_per_day=48, protected_hours={1},
    )
    check("protected slot is never written to",
          schedule[1]['deferrable_per_device'][0] == 0.0,
          f"got {schedule[1]['deferrable_per_device'][0]}")


def test_respects_hour_mask():
    """A device unavailable in a slot (hour_mask False) must never receive
    consolidated energy there, even if it's otherwise the earliest slot."""
    dev = [{'max_kw': 1.0, 'daily_kwh': 1.0, 'hour_mask': [False, True, True]}]
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.5]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.5]),
    ]
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    check("masked-out slot never receives energy",
          schedule[0]['deferrable_per_device'][0] == 0.0)


def test_respects_max_kw_cap_per_slot():
    """Consolidation must never push a single slot's allocation above the
    device's own max_kw*dt ceiling."""
    dev = [{'max_kw': 1.0, 'daily_kwh': 1.0, 'hour_mask': None}]  # cap = 0.5kWh/slot @ dt=0.5
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.3]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.3]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.4]),
    ]
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    check("no slot exceeds max_kw*dt after consolidation",
          all(r['deferrable_per_device'][0] <= 0.5 + 1e-9 for r in schedule),
          f"values={[r['deferrable_per_device'][0] for r in schedule]}")
    check("total energy still preserved under the cap constraint",
          abs(sum(r['deferrable_per_device'][0] for r in schedule) - 1.0) < 1e-6)


def test_multi_day_and_multi_device_independence():
    """Two devices, two calendar days — consolidation for one device/day must
    not disturb another device's slots or a different day's totals."""
    devs = [
        {'max_kw': 1.0, 'daily_kwh': 1.0, 'hour_mask': None},
        {'max_kw': 1.0, 'daily_kwh': 1.0, 'hour_mask': None},
    ]
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.5, 0.2]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0, 0.2]),
        # day boundary at slot 2 (slots_per_day=2)
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.3, 0.0]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.3, 0.5]),
    ]
    before_totals = [sum(r['deferrable_per_device'][i] for r in schedule) for i in range(2)]
    consolidate_deferrable_schedule(schedule, devs, dt=0.5, slots_per_day=2)
    after_totals = [sum(r['deferrable_per_device'][i] for r in schedule) for i in range(2)]
    check("device 0's total energy unchanged", abs(after_totals[0] - before_totals[0]) < 1e-6)
    check("device 1's total energy unchanged", abs(after_totals[1] - before_totals[1]) < 1e-6)


def test_deferrable_kwh_field_kept_in_sync():
    """The aggregate 'deferrable_kwh' field (used for display) must always
    equal the sum of deferrable_per_device after consolidation."""
    dev = [{'max_kw': 1.0, 'daily_kwh': 1.0, 'hour_mask': None}]
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.5]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.5]),
    ]
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    check("deferrable_kwh matches sum(deferrable_per_device) on every row",
          all(abs(r['deferrable_kwh'] - sum(r['deferrable_per_device'])) < 1e-9
              for r in schedule))


if __name__ == "__main__":
    test_fragmented_zero_cost_slots_get_consolidated()
    test_never_increases_cost_across_different_priced_slots()
    test_rejects_move_that_would_raise_cost()
    test_protected_hours_are_never_touched()
    test_respects_hour_mask()
    test_respects_max_kw_cap_per_slot()
    test_multi_day_and_multi_device_independence()
    test_deferrable_kwh_field_kept_in_sync()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all deferrable-consolidation tests passed.")
