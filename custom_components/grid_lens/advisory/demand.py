"""Demand-charge peak-shaving inputs for the rolling advisory horizon.

Pure helpers, no Home Assistant imports, so the maths (window mask, days
remaining, month-to-date peak from hourly totals) is unit-testable in a bare
container without numpy / scipy / HA.

Background: a peak-demand charge is billed on the single highest in-window grid
demand over the whole billing period, at ``$/kW/day x days``. The advisory LP
only sees a 24-48 h horizon, so on its own it would (a) keep spending battery
cycles shaving a peak that is already locked in for this month, and (b) value
NOT setting a new peak at only ``rate x horizon-days`` instead of
``rate x days-left-in-the-period``. These helpers produce the two numbers that
fix both: the month-to-date in-window peak (a floor on the LP's peak variable)
and the days still to run in the billing period (the price on that variable).

Billing period == calendar month. Ausgrid and most AU DNSP residential demand
tariffs reset on the 1st; a retailer bill that straddles a month boundary will
be a few days out each cycle. Documented approximation, see docs/OPEN_ITEMS.md.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from typing import Callable, Iterable

# Mirrors const.DEFAULT_DEMAND_WINDOW_HOURS — duplicated here only so this module
# stays import-free; the caller passes the plan's real window in every live path.
_DEFAULT_WINDOW_HOURS = [15, 16, 17, 18, 19, 20]


def demand_window_predicate(hours, days_spec: str) -> Callable[[datetime], bool]:
    """``(hours, days_spec) -> fn(local_dt) -> bool``.

    ``hours`` is a list of local hour-of-day ints, or the string ``"all"``.
    ``days_spec`` is ``"all"`` / ``"weekends"`` / anything else (= weekdays).
    Same semantics as ``plan_calculator``'s demand-window predicate so the
    advisory mask and the plan-comparison mask can never disagree.
    """
    hset = None if hours == "all" else set(hours or _DEFAULT_WINDOW_HOURS)

    def ok(local_dt: datetime) -> bool:
        h_ok = True if hset is None else (local_dt.hour in hset)
        wd = local_dt.weekday()  # 0=Mon .. 6=Sun
        if days_spec == "all":
            d_ok = True
        elif days_spec == "weekends":
            d_ok = wd >= 5
        else:
            d_ok = wd < 5
        return h_ok and d_ok

    return ok


def build_demand_window_mask(start_local: datetime, slot_minutes: int, slots: int,
                             hours, days_spec: str) -> list[int]:
    """Per-slot 0/1 mask, length ``slots``, 1 = slot is inside the demand window."""
    pred = demand_window_predicate(hours, days_spec)
    return [
        1 if pred(start_local + timedelta(minutes=t * slot_minutes)) else 0
        for t in range(slots)
    ]


def billing_days_remaining(now_local: datetime) -> int:
    """Whole days from ``now_local`` to the end of its calendar month, inclusive
    of today (so the 1st of a 30-day month returns 30, the last day returns 1)."""
    _, last = calendar.monthrange(now_local.year, now_local.month)
    return max(1, last - now_local.day + 1)


def days_to_season_end(now_local: datetime, season_end: str | None) -> int | None:
    """Whole days from ``now_local`` to the last day of a season, inclusive of
    today. ``season_end`` is ``"MM-DD"`` (the season may wrap the new year).
    ``None`` season_end → ``None`` (year-round, no season limit). Returns at
    least 1. Caller takes ``min(this, billing_days_remaining)`` — a monthly
    demand charge only bills the peak set today for as long as BOTH the billing
    month and the season it was set in still run."""
    if not season_end:
        return None
    em, ed = int(season_end[:2]), int(season_end[3:5])
    year = now_local.year
    end = now_local.replace(month=em, day=ed, hour=23, minute=59, second=59,
                            microsecond=0)
    if end < now_local:  # season end already passed this year → it's next year
        end = end.replace(year=year + 1)
    return max(1, (end.date() - now_local.date()).days + 1)


def month_start_local(now_local: datetime) -> datetime:
    """Midnight on the 1st of ``now_local``'s month, same tzinfo."""
    return now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def peak_from_hourly_samples(samples: Iterable[tuple[datetime, float]],
                             pred: Callable[[datetime], bool]) -> float:
    """Largest single-hour import among ``samples`` whose hour satisfies ``pred``.

    ``samples`` is ``(local_dt, kwh_that_hour)`` pairs — one per elapsed clock
    hour so far this billing period, ``kwh_that_hour`` the grid import during it
    (a reset-aware consecutive-hour delta of HA's ``sum`` statistic). 1 kWh over
    1 h == 1 kW average, matching ``_compute_demand_charge``'s approximation, so
    the return value is directly comparable to the LP's peak-kW variable. Returns
    0.0 when nothing qualifies (no history, window not yet entered this month).
    """
    peak = 0.0
    for local_dt, kwh in samples:
        if kwh is not None and kwh > 0 and pred(local_dt):
            peak = max(peak, float(kwh))
    return peak
