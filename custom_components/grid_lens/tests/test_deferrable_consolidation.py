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


def _row(import_kwh=0.0, export_kwh=0.0, import_rate=0.0, export_rate=0.0, defer=None,
         bonus=None, solar_kwh=0.0, load_kwh=0.0):
    defer = defer or [0.0]
    return {
        'import_kwh': import_kwh, 'export_kwh': export_kwh,
        'import_rate': import_rate, 'export_rate': export_rate,
        # Forecast solar/load for the slot (the real schedule rows always carry
        # these). solar - load is the surplus the solar-first tie-break ranks on;
        # defaulting both to 0 leaves every pre-existing fixture undifferentiated,
        # so those keep their original pure clock ordering.
        'solar_kwh': solar_kwh, 'load_kwh': load_kwh,
        'import_cost': import_kwh * import_rate, 'export_credit': export_kwh * export_rate,
        'deferrable_per_device': list(defer),
        'deferrable_kwh': sum(defer),
        # LP-bonus energy (battery_optimizer's bon_i[t]) already committed to this slot —
        # defaults to all-zero (same shape as defer) so every pre-existing fixture that
        # never mentions bonus is unaffected; see _slot_marginal_tiers for how this
        # shrinks the capacity left for def_i reshuffling (2026-11-01 fix).
        'bonus_per_device': list(bonus) if bonus is not None else [0.0] * len(defer),
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


def test_never_moves_energy_past_its_own_charge_target_deadline():
    """Regression for the live 2026-09-18 bug: a device with an active "charge
    to X% by Y" target (see charge_target.py) has its pre-deadline energy
    already placed by the raw LP solve to satisfy the hard floor constraint
    (Σ def_i[t] for t < floor_slot[i] — see optimize_hourly_schedule). Slot 0
    (before the deadline) is expensive grid import; slot 1 (at/after the
    deadline, floor_slot=1) is free export-covered — cheaper, so without a
    floor_slot bound this pass would happily move slot 0's energy into slot 1,
    which is exactly what was observed live: a 75%-by-11:30 target whose raw
    solve met the floor got displayed as finishing hours late, all on solar.
    """
    dev = [{'max_kw': 2.0, 'daily_kwh': 1.0, 'hour_mask': None}]
    schedule = [
        _row(import_kwh=0.5, import_rate=1.00, defer=[1.0]),  # before the deadline
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),  # at/after the deadline — cheaper
    ]
    consolidate_deferrable_schedule(
        schedule, dev, dt=0.5, slots_per_day=48, floor_slot={0: 1},
    )
    check("pre-deadline energy stays in the pre-deadline slot",
          schedule[0]['deferrable_per_device'][0] == 1.0,
          f"got {schedule[0]['deferrable_per_device'][0]}")
    check("nothing leaks into the at/after-deadline slot",
          schedule[1]['deferrable_per_device'][0] == 0.0,
          f"got {schedule[1]['deferrable_per_device'][0]}")


def test_floor_slot_does_not_restrict_days_outside_the_deadline():
    """A day entirely after the deadline must still consolidate normally —
    only the calendar day the deadline actually falls inside gets clipped.
    Here floor_slot sits exactly on the day-1 boundary (t0 == floor_slot), so
    day 1 is untouched by the clip (t0 < floor_slot < t1 is false) and should
    still front-load its own energy into its own cheapest slot."""
    dev = [{'max_kw': 2.0, 'daily_kwh': 1.0, 'hour_mask': None}]
    schedule = [
        _row(import_kwh=0.0, import_rate=0.10, defer=[0.0]),  # day 0
        _row(import_kwh=0.0, import_rate=0.10, defer=[0.0]),  # day 0
        _row(import_kwh=0.0, import_rate=0.10, defer=[0.0]),  # day 1 — cheap
        _row(import_kwh=1.0, import_rate=1.00, defer=[1.0]),  # day 1 — expensive
    ]
    consolidate_deferrable_schedule(
        schedule, dev, dt=0.5, slots_per_day=2, floor_slot={0: 2},
    )
    check("day-1 (entirely after the deadline) still consolidates into its own cheapest slot",
          schedule[2]['deferrable_per_device'][0] == 1.0,
          f"got {schedule[2]['deferrable_per_device'][0]}")
    check("day-1's expensive slot is emptied by the move",
          schedule[3]['deferrable_per_device'][0] == 0.0,
          f"got {schedule[3]['deferrable_per_device'][0]}")


def test_slot_day_index_confines_consolidation_to_real_calendar_days():
    """Day-boundary fix (2026-09-23) regression-guard: without slot_day_index,
    a horizon that starts mid-day still gets chunked positionally
    (t // slots_per_day), which can group part of TODAY together with part of
    TOMORROW into one "day" — letting this pass legally move energy across a
    real midnight boundary just because it's cheaper there. Supplying
    slot_day_index (real calendar-day keys, see
    retailer_plans.slot_calendar_day_index) must prevent that: today's slots
    and tomorrow's slots become separate groups, so a cheaper tomorrow slot is
    no longer "the same day" and energy must stay put.

    Concretely: slot 0 is today, expensive, holding the device's energy; slot
    2 is tomorrow, free/cheap. slots_per_day=4 nominally groups all 4 slots as
    one chunk when slot_day_index is absent (T==slots_per_day, unchanged from
    today's positional behaviour) — demonstrating the pre-fix shape. With
    slot_day_index=[today, today, tomorrow, tomorrow], the same starting
    schedule must NOT move slot 0's energy into slot 2.
    """
    dev = [{'max_kw': 1.0, 'daily_kwh': 1.0, 'hour_mask': None}]

    def _fresh_schedule():
        return [
            _row(import_kwh=1.0, import_rate=1.00, defer=[1.0]),  # today — expensive
            _row(import_kwh=0.0, import_rate=1.00, defer=[0.0]),  # today
            _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),   # tomorrow — free/cheap
            _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),   # tomorrow
        ]

    without_index = _fresh_schedule()
    consolidate_deferrable_schedule(without_index, dev, dt=1.0, slots_per_day=4)
    check("without slot_day_index, positional chunking treats all 4 slots as one day "
          "(pre-fix shape — energy free to move into the cheaper tomorrow slot)",
          without_index[2]['deferrable_per_device'][0] == 1.0,
          f"got {without_index[2]['deferrable_per_device'][0]}")

    with_index = _fresh_schedule()
    consolidate_deferrable_schedule(
        with_index, dev, dt=1.0, slots_per_day=4,
        slot_day_index=[900, 900, 901, 901],
    )
    check("with slot_day_index, today's energy stays within today's own slots",
          with_index[0]['deferrable_per_device'][0] == 1.0,
          f"got {with_index[0]['deferrable_per_device'][0]}")
    check("tomorrow's (cheaper) slots stay untouched by today's energy",
          with_index[2]['deferrable_per_device'][0] == 0.0
          and with_index[3]['deferrable_per_device'][0] == 0.0,
          f"got {with_index[2]['deferrable_per_device'][0]}, {with_index[3]['deferrable_per_device'][0]}")


def test_prefer_early_fills_earliest_tied_slots_first():
    """Explicit 'prefer_early' — same shape as the no-preference default test
    above, confirming the explicit value doesn't change anything."""
    dev = [{'max_kw': 1.8, 'daily_kwh': 3.6, 'hour_mask': None,
            'charge_timing_preference': 'prefer_early'}]
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),  # the gap
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
    ]
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    on_pattern = [r['deferrable_per_device'][0] > 0 for r in schedule]
    check("prefer_early fills the earliest tied slots — leading block",
          on_pattern == [True, True, True, True, False], f"pattern={on_pattern}")


def test_prefer_early_still_waits_for_a_slot_solar_can_actually_cover():
    """The 2026-11-12 live case (third occurrence; 2026-11-05/06 were the first
    two — GRIDLENS_CHECKLIST.md).

    Dawn slot 0 has a little sun (0.5 kWh of surplus) and the rest of the
    device's draw came from the house battery, so `_slot_marginal_tiers` sees
    its whole 0.9 kWh as "free" at the export rate — EXACTLY the same unit cost
    as the midday slots, where 0.9 kWh of real surplus is being exported. That
    tie is what clock-only `prefer_early` resolved toward slot 0, draining the
    battery at dawn (live: EV planned 2.949 kWh into an 08:00 slot holding
    1.08 kWh of surplus, battery 18.2% -> 10%, for a $0.0001 cost difference).

    `prefer_early` must still mean early — just the earliest slot whose OWN
    sunlight can run the device, which here is slot 2, not slot 0.
    """
    dev = [{'max_kw': 1.8, 'daily_kwh': 1.8, 'hour_mask': None,
            'charge_timing_preference': 'prefer_early'}]
    schedule = [
        # Dawn: 0.5 kWh surplus, device drawing 0.9 — the other 0.4 is battery.
        _row(export_rate=0.03, import_rate=0.25, defer=[0.9], solar_kwh=0.6, load_kwh=0.1),
        _row(export_rate=0.03, import_rate=0.25, defer=[0.0], solar_kwh=0.9, load_kwh=0.1),
        # Midday: 1.4 kWh of surplus, 0.9 of it being exported at the same 3c.
        _row(export_kwh=0.9, export_rate=0.03, import_rate=0.25, defer=[0.0],
             solar_kwh=1.5, load_kwh=0.1),
        _row(export_kwh=0.9, export_rate=0.03, import_rate=0.25, defer=[0.9],
             solar_kwh=1.5, load_kwh=0.1),
    ]
    before_cost = _total_cost(schedule)
    before_total = sum(r['deferrable_per_device'][0] for r in schedule)

    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)

    after = [round(r['deferrable_per_device'][0], 6) for r in schedule]
    check("the dawn slot solar cannot cover is emptied", after[0] == 0.0, f"got {after}")
    check("energy lands in the earliest SOLAR-COVERED slot (slot 2), not slot 0",
          after == [0.0, 0.0, 0.9, 0.9], f"got {after}")
    check("total energy is preserved",
          abs(sum(after) - before_total) < 1e-6, f"{sum(after)} vs {before_total}")
    check("cost did not increase (it was an exact tie)",
          _total_cost(schedule) <= before_cost + 1e-9,
          f"{_total_cost(schedule)} vs {before_cost}")


def test_overcast_day_with_no_covered_slot_still_prefers_the_sunniest():
    """Graceful degradation: when NOTHING clears the device's full rating the
    two groups collapse to one, and a naive implementation would silently fall
    back to pure earliest-first — the exact behaviour being fixed. Ordering the
    uncovered group by descending surplus keeps flexible load pointed at
    whatever sun there is.

    Note the fill necessarily fragments here: a slot that cannot cover the
    device's full draw can only absorb its own surplus at the export rate, so
    the sunniest slot fills to 0.7 and the remaining 0.2 stays in the earliest.
    That is correct — the point is only that 0.7 of it moved off the clock's
    first slot and onto the sun.
    """
    dev = [{'max_kw': 1.8, 'daily_kwh': 0.9, 'hour_mask': None,
            'charge_timing_preference': 'prefer_early'}]
    schedule = [
        # 0.5 kWh surplus, all of it going into the device (so nothing exported).
        _row(export_rate=0.03, import_rate=0.25, defer=[0.9], solar_kwh=0.6, load_kwh=0.1),
        # 0.7 kWh surplus, currently exported at 3c — the sunniest slot.
        _row(export_kwh=0.7, export_rate=0.03, import_rate=0.25, defer=[0.0],
             solar_kwh=0.8, load_kwh=0.1),
        # 0.3 kWh surplus, also exported at the same 3c.
        _row(export_kwh=0.3, export_rate=0.03, import_rate=0.25, defer=[0.0],
             solar_kwh=0.4, load_kwh=0.1),
    ]
    before_cost = _total_cost(schedule)
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    after = [round(r['deferrable_per_device'][0], 6) for r in schedule]
    check("no slot covers 0.9 kWh, so the sunniest (slot 1) fills before the earliest",
          after == [0.2, 0.7, 0.0], f"got {after}")
    check("overcast reshuffle still did not increase cost",
          _total_cost(schedule) <= before_cost + 1e-9,
          f"{_total_cost(schedule)} vs {before_cost}")


def test_no_preference_is_untouched_by_the_solar_ordering():
    """"No preference" means add no tie-break at all, so it keeps the original
    earliest-first fill even when the solar forecast would point elsewhere —
    it is an explicit opt-out, not a request for a different opinion."""
    dev = [{'max_kw': 1.8, 'daily_kwh': 0.9, 'hour_mask': None,
            'charge_timing_preference': 'no_preference'}]
    schedule = [
        _row(export_rate=0.03, import_rate=0.25, defer=[0.9], solar_kwh=0.2, load_kwh=0.1),
        _row(export_rate=0.03, import_rate=0.25, defer=[0.0], solar_kwh=1.5, load_kwh=0.1),
    ]
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    after = [round(r['deferrable_per_device'][0], 6) for r in schedule]
    check("no_preference stays earliest-first despite slot 1 having the sun",
          after == [0.9, 0.0], f"got {after}")


def test_solar_first_never_overrides_a_real_price_difference():
    """The whole safety argument: solar coverage re-breaks TIES only. A dark
    slot that is genuinely cheaper (a free import window) must still win,
    because unit cost stays the primary sort key."""
    dev = [{'max_kw': 1.8, 'daily_kwh': 0.9, 'hour_mask': None,
            'charge_timing_preference': 'prefer_early'}]
    schedule = [
        # Dark, but importing is FREE here — genuinely the cheapest slot.
        _row(export_rate=0.0, import_rate=0.0, defer=[0.0], solar_kwh=0.0, load_kwh=0.1),
        # Sunny, but its surplus is worth 3c/kWh in foregone export.
        _row(export_kwh=0.9, export_rate=0.03, import_rate=0.25, defer=[0.9],
             solar_kwh=1.5, load_kwh=0.1),
    ]
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    after = [round(r['deferrable_per_device'][0], 6) for r in schedule]
    check("the genuinely free dark slot still wins over the sunny one",
          after == [0.9, 0.0], f"got {after}")


def test_prefer_just_in_time_fills_latest_tied_slots_first():
    """The live 2026-09-28 bug this was found from: with charge_timing_preference
    set to prefer_just_in_time, a fragmented cost-tied allocation must
    consolidate into a TRAILING block (latest slots), not the leading one
    _front_load_device_day (now _consolidate_device_day) used to always
    produce regardless of preference."""
    dev = [{'max_kw': 1.8, 'daily_kwh': 3.6, 'hour_mask': None,
            'charge_timing_preference': 'prefer_just_in_time'}]
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),  # the gap
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
    ]
    before_total = sum(r['deferrable_per_device'][0] for r in schedule)
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    after_total = sum(r['deferrable_per_device'][0] for r in schedule)
    on_pattern = [r['deferrable_per_device'][0] > 0 for r in schedule]
    check("total energy is preserved", abs(after_total - before_total) < 1e-6)
    check("prefer_just_in_time fills the latest tied slots — trailing block",
          on_pattern == [False, True, True, True, True], f"pattern={on_pattern}")


def test_no_preference_behaves_like_the_original_earliest_first_default():
    """'no_preference' must NOT be treated as a third direction — it means
    "no special handling", which for this pass's own pre-existing behaviour
    is earliest-first, same as prefer_early and same as no field at all."""
    dev = [{'max_kw': 1.8, 'daily_kwh': 3.6, 'hour_mask': None,
            'charge_timing_preference': 'no_preference'}]
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
    ]
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    on_pattern = [r['deferrable_per_device'][0] > 0 for r in schedule]
    check("no_preference fills earliest tied slots first, like the original default",
          on_pattern == [True, True, True, True, False], f"pattern={on_pattern}")


def test_missing_preference_field_is_scoped_to_early_not_late():
    """A device with NO charge_timing_preference key at all (every non-SOC-
    tracked device — a pool pump, hot water — and every device on
    plan_calculator.py's plan-comparison path) must consolidate exactly like
    before this feature existed. Regression guard against the field's ''
    fallback accidentally defaulting to "prefer late" and silently changing
    consolidation direction for devices this feature was never meant to
    touch — found and fixed during the same 2026-09-28 session that added
    the preference-awareness in the first place."""
    dev = [{'max_kw': 1.8, 'daily_kwh': 3.6, 'hour_mask': None}]  # no key at all
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9]),
    ]
    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)
    on_pattern = [r['deferrable_per_device'][0] > 0 for r in schedule]
    check("a device with no preference field consolidates earliest-first, unchanged",
          on_pattern == [True, True, True, True, False], f"pattern={on_pattern}")


def test_prefer_just_in_time_still_respects_its_own_charge_target_deadline():
    """Combines the 2026-09-18 floor_slot regression guard with the new
    preference: even preferring late slots, the pass must never move energy
    to or past the device's own ad-hoc charge-target deadline — floor_slot's
    day clipping happens before _consolidate_device_day ever sees the
    eligible slot list, so this should hold regardless of preference."""
    dev = [{'max_kw': 2.0, 'daily_kwh': 1.0, 'hour_mask': None,
            'charge_timing_preference': 'prefer_just_in_time'}]
    schedule = [
        _row(import_kwh=0.5, import_rate=1.00, defer=[1.0]),  # before the deadline
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0]),  # at/after the deadline
    ]
    consolidate_deferrable_schedule(
        schedule, dev, dt=0.5, slots_per_day=48, floor_slot={0: 1},
    )
    check("pre-deadline energy stays in the pre-deadline slot even under prefer_just_in_time",
          schedule[0]['deferrable_per_device'][0] == 1.0,
          f"got {schedule[0]['deferrable_per_device'][0]}")
    check("nothing leaks past the deadline",
          schedule[1]['deferrable_per_device'][0] == 0.0,
          f"got {schedule[1]['deferrable_per_device'][0]}")


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


def test_bonus_is_treated_as_already_occupied_capacity():
    """2026-11-01 fix: consolidation used to be skipped entirely whenever any device had
    an active LP-bonus solve, specifically because reshuffling def_i with no knowledge of
    bon_i could push a slot's def+bonus total past the device's rating. Now it runs
    unconditionally, with _slot_marginal_tiers treating each slot's existing bonus as
    capacity already spoken for (``avail = cap_kwh - bonus``).

    1.8 kW device at 0.5 h slots -> cap_kwh = 0.9 kWh/slot. Slot 0 already carries 0.6 kWh
    of bonus (only 0.3 kWh of real room left); slots 1-2 are empty (0.9 kWh of room each).
    All three slots are cost-tied at $0, and the device's 1.5 kWh daily total is
    fragmented as 0.0/0.9/0.6 — the shape this pass exists to collapse. Filling
    earliest-first against the BONUS-AWARE capacities (0.3, 0.9, 0.9) must land
    0.3/0.9/0.3, never spilling past 0.3 into slot 0's already-reduced room."""
    dev = [{'max_kw': 1.8, 'daily_kwh': 1.5, 'hour_mask': None}]
    schedule = [
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.0], bonus=[0.6]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.9], bonus=[0.0]),
        _row(export_kwh=0.0, export_rate=0.0, defer=[0.6], bonus=[0.0]),
    ]
    before_total = sum(r['deferrable_per_device'][0] for r in schedule)

    consolidate_deferrable_schedule(schedule, dev, dt=0.5, slots_per_day=48)

    totals = [r['deferrable_per_device'][0] + r['bonus_per_device'][0] for r in schedule]
    after_total = sum(r['deferrable_per_device'][0] for r in schedule)
    check("daily total energy is preserved", abs(after_total - before_total) < 1e-6,
          f"before={before_total} after={after_total}")
    check("no slot's def+bonus exceeds the device's per-slot rating",
          all(t <= 0.9 + 1e-6 for t in totals), f"totals={totals}")
    check("slot 0's def is capped to its reduced (bonus-aware) room",
          schedule[0]['deferrable_per_device'][0] <= 0.3 + 1e-6,
          f"slot0 def={schedule[0]['deferrable_per_device'][0]}")
    check("the exact bonus-aware packing is 0.3/0.9/0.3",
          [round(r['deferrable_per_device'][0], 6) for r in schedule] == [0.3, 0.9, 0.3],
          f"got {[r['deferrable_per_device'][0] for r in schedule]}")


if __name__ == "__main__":
    test_fragmented_zero_cost_slots_get_consolidated()
    test_never_increases_cost_across_different_priced_slots()
    test_rejects_move_that_would_raise_cost()
    test_protected_hours_are_never_touched()
    test_respects_hour_mask()
    test_respects_max_kw_cap_per_slot()
    test_multi_day_and_multi_device_independence()
    test_never_moves_energy_past_its_own_charge_target_deadline()
    test_floor_slot_does_not_restrict_days_outside_the_deadline()
    test_slot_day_index_confines_consolidation_to_real_calendar_days()
    test_prefer_early_fills_earliest_tied_slots_first()
    test_prefer_early_still_waits_for_a_slot_solar_can_actually_cover()
    test_overcast_day_with_no_covered_slot_still_prefers_the_sunniest()
    test_no_preference_is_untouched_by_the_solar_ordering()
    test_solar_first_never_overrides_a_real_price_difference()
    test_prefer_just_in_time_fills_latest_tied_slots_first()
    test_no_preference_behaves_like_the_original_earliest_first_default()
    test_missing_preference_field_is_scoped_to_early_not_late()
    test_prefer_just_in_time_still_respects_its_own_charge_target_deadline()
    test_deferrable_kwh_field_kept_in_sync()
    test_bonus_is_treated_as_already_occupied_capacity()
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} failure(s): {_FAILURES}")
        sys.exit(1)
    print("\nOK — all deferrable-consolidation tests passed.")
