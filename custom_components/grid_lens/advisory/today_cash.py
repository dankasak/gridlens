"""Actual (metered) dollars spent and earned in each elapsed slot of today.

The price chart draws the plan's buy/sell *rates*; this module supplies the money
those rates actually turned into, per 30-min slot, for the part of today that has
already happened. It exists so the chart can answer "what has today cost me, and
what has the feed-in earned me" rather than only "what is the rate right now".

**Actual, never the optimizer.** Every figure here comes from the household's own
metered import/export energy statistics priced against the plan's own tariff
structure — the rule in CLAUDE.md's "Current-plan cost" section. The LP's forward
trajectory already publishes its own ``cost``/``credit`` per slot and the card
draws those for the *future* half of the chart, visually de-emphasised; nothing in
this module touches them. Conflating the two is exactly the mistake that once
showed $0.99 for a period the retailer billed $21.04 (checklist, 2026-08-04).

Metered kWh comes from HA's 5-minute short-term statistics for the configured grid
import/export energy sensors, summed into slots. Those sensors are whatever the
install nominated during setup (this dev rig's happen to be daily-resetting
Sigenergy counters; a lifetime cumulative counter works identically, since only
``change`` is read and a negative change — a meter reset — is discarded).

Capped rate tiers are priced with the same bucketing and pooled-allowance rules
as the bill breakdown (``cap_periods.py``, shared with
``PlanCalculator._split_capped_kwh``). A cap whose period started before today —
pooled over the billing period, as GloBird's and Origin's are, or resetting
weekly/monthly — needs to know how much allowance is already spent, so
``async_seed_cap_usage`` walks hourly statistics from the period start to
midnight first. That walk is cached for the day: its answer covers
[period start, today 00:00), which doesn't change until tomorrow.

Both sensors are optional. With neither configured, or with no statistics yet, the
rows come back exactly as they went in and the card simply draws no bars for the
elapsed half — the chart is unchanged from before this feature existed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..cap_periods import (
    billing_period_days,
    cap_allowance,
    cap_bucket,
    cap_needs_history,
    cap_period_start,
    month_start,
)

_LOGGER = logging.getLogger(__name__)

# Resolution of the pre-today cap seed walk. 5-minute statistics are only
# retained for days, so a month-to-date walk has to read hourly ones; each hour
# is then priced as two half-hour halves so a rate window that opens on the half
# hour doesn't swallow the whole hour's energy into one tier.
_SEED_PERIOD = "hour"
_SEED_SPLIT = 2


def _rate_info(plan, direction: str, local_dt: datetime) -> dict:
    """The plan's own rate structure at ``local_dt``, or ``{}`` if it won't say."""
    get_info = (plan.get_import_rate_info if direction == "import"
                else plan.get_export_rate_info)
    try:
        return get_info(local_dt) or {}
    except Exception:  # noqa: BLE001 — a rate-lookup quirk must not sink the chart
        _LOGGER.debug("today-cash: %s rate lookup failed at %s", direction, local_dt,
                      exc_info=True)
        return {}


def _cap_spec(info: dict) -> tuple | None:
    """``(cap, after_rate, period, application)`` if ``info`` has a usable cap."""
    cap = info.get("daily_cap_kwh")
    after_rate = info.get("rate_after_cap")
    if not cap or after_rate is None:
        return None
    return (float(cap), float(after_rate),
            info.get("cap_period") or "day",
            info.get("cap_application") or "strict")


def _price_kwh(plan, direction: str, local_dt: datetime, kwh: float,
               cap_used: dict, period_days: float | None = None) -> tuple[float, float]:
    """Return ``(dollars, effective_rate)`` for ``kwh`` traded at ``local_dt``.

    Honours every capped-tier shape the plan data can express, using the same
    bucketing and pooled-allowance sizing as the bill breakdown (cap_periods.py,
    shared with ``PlanCalculator._split_capped_kwh``): a strict cap resets on its
    own calendar boundary, a pooled one accrues its stated figure per day across
    the billing period so unused headroom banks.

    ``cap_used`` carries free-tier kWh already spent, keyed per
    ``(direction, bucket, label)`` exactly as the bill's accumulator is. For a
    cap whose bucket started before today it must arrive **pre-seeded** with the
    period-to-date usage (``async_seed_cap_usage``) or the day will be priced as
    though the allowance were untouched; the caller walks today in time order,
    which is what keeps the accumulation correct from there.

    The effective rate is returned so the caller can republish it on the row: the
    forward trajectory already blends a tranche-split slot's rate the same way
    (see ``exp_rate_out`` in battery_optimizer.py), and the two halves of one line
    must not switch definitions at "now".
    """
    info = _rate_info(plan, direction, local_dt)
    if not info:
        return 0.0, 0.0
    rate = float(info.get("rate") or 0.0)
    if kwh <= 0:
        return 0.0, rate

    spec = _cap_spec(info)
    if spec is None:
        return kwh * rate, rate
    cap, after_rate, period, application = spec

    # A pooled cap has ONE bucket for the whole billing period, so its key
    # carries no time component and unused allowance carries across days.
    bucket = None if application == "pooled" else cap_bucket(period, local_dt)
    key = (direction, bucket, info.get("label") or "Energy")
    if application == "pooled" and period_days is None:
        period_days = billing_period_days(local_dt)
    allowance = cap_allowance(cap, period, application, period_days)
    used = cap_used.get(key, 0.0)
    free_kwh = min(kwh, max(0.0, allowance - used))
    over_kwh = kwh - free_kwh
    cap_used[key] = used + free_kwh
    amount = free_kwh * rate + over_kwh * after_rate
    return amount, (amount / kwh)


def bucket_changes_into_slots(records, slot_starts: list[datetime],
                              slot_delta: timedelta) -> list[float]:
    """Sum statistics ``change`` values into ``slot_starts``-aligned buckets.

    ``records`` are 5-minute statistic rows; ``slot_starts`` are the local slot
    boundaries, ascending. A record lands in the last slot whose start is <= its
    own, so sub-slot resolution is simply added up. A negative change is a
    cumulative-meter reset artifact (these counters reset at midnight on some
    installs) and is discarded rather than subtracted — the same call the daily
    archive makes for the same reason.
    """
    out = [0.0] * len(slot_starts)
    if not slot_starts:
        return out
    t0 = slot_starts[0]
    step = slot_delta.total_seconds()
    for record in (records or []):
        change = record.get("change")
        if change is None:
            continue
        change = float(change)
        if change < 0:
            continue
        ts = record.get("start")
        if ts is None:
            continue
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        idx = int((dt_util.as_local(ts) - t0).total_seconds() // step)
        if 0 <= idx < len(out):
            out[idx] += change
    return out


def attach_cash(plan, rows: list[dict], buy_kwh: list[float],
                sell_kwh: list[float], cap_used: dict | None = None,
                period_days: float | None = None) -> list[dict]:
    """Add metered kWh and its priced cost/credit to each elapsed-today row.

    Pure, so the cap walk and the pricing are testable without a recorder. Rows
    are mutated in place (and returned) in time order, which is what makes the
    cap accumulator correct.

    ``cap_used`` is the free-tier accumulator. Pass the dict
    ``async_seed_cap_usage`` built to price a cap whose period began before today
    (pooled, or resetting weekly/monthly/…); omit it and every cap is treated as
    starting the day untouched, which is only right for a strict daily cap. It is
    mutated, so a caller reusing a seed across ticks must hand over a copy.
    """
    if cap_used is None:
        cap_used = {}
    for i, row in enumerate(rows):
        try:
            local_dt = dt_util.as_local(dt_util.parse_datetime(row["start"]))
        except Exception:  # noqa: BLE001
            continue
        imp = max(0.0, buy_kwh[i] if i < len(buy_kwh) else 0.0)
        exp = max(0.0, sell_kwh[i] if i < len(sell_kwh) else 0.0)
        cost, imp_rate = _price_kwh(plan, "import", local_dt, imp, cap_used, period_days)
        credit, exp_rate = _price_kwh(plan, "export", local_dt, exp, cap_used, period_days)
        row["buy_kwh"] = round(imp, 3)
        row["sell_kwh"] = round(exp, 3)
        row["cost"] = round(cost, 4)
        row["credit"] = round(credit, 4)
        # Marks the row as measured rather than planned. The card keys its
        # actual-vs-forecast styling off this, instead of comparing the slot to
        # "now" — the current slot is partly elapsed and partly forecast, and only
        # the publisher knows which side of that line a row was built from.
        row["actual"] = True
        # Republish the rate the money was actually priced at, so a capped tier's
        # blended rate shows on the line as well as in the bar (see _price_kwh).
        if imp > 0:
            row["import_rate"] = round(imp_rate, 4)
        if exp > 0:
            row["export_rate"] = round(exp_rate, 4)
    return rows


def caps_needing_history(plan, slot_starts: list[datetime]) -> dict:
    """``{direction: earliest local datetime to read history from}``.

    Scans the rate structure today's own slots resolve to — those are the caps
    that can affect today's pricing — and keeps, per direction, the earliest
    bucket start among the ones whose period began before today. Empty for the
    overwhelmingly common case (no cap, or a strict daily cap), which is what
    makes the seed walk cost nothing on most plans.
    """
    need: dict = {}
    for direction in ("import", "export"):
        earliest = None
        for local_dt in slot_starts:
            spec = _cap_spec(_rate_info(plan, direction, local_dt))
            if spec is None:
                continue
            _cap, _after, period, application = spec
            if not cap_needs_history(period, application):
                continue
            start = cap_period_start(period, application, local_dt)
            if earliest is None or start < earliest:
                earliest = start
        if earliest is not None:
            need[direction] = earliest
    return need


def _hourly_kwh(records) -> list[tuple[datetime, float]]:
    """``[(local_dt, kwh)]`` from statistics rows, ascending.

    Prefers each row's ``change``, and falls back to differencing consecutive
    ``sum`` values when it's absent. The fallback matters more than it looks: if
    ``change`` were ever missing at this resolution, a silent empty result would
    seed no usage at all, which is indistinguishable from a brand-new billing
    period and puts the credit back to over-stated — the exact bug the seed walk
    exists to fix. Differencing ``sum`` is also how ``advisory/demand.py`` reads
    the same hourly rows for its month-to-date peak.
    """
    parsed = []
    for record in (records or []):
        ts = record.get("start")
        if ts is None:
            continue
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        parsed.append((dt_util.as_local(ts), record.get("change"),
                       record.get("sum")))
    parsed.sort(key=lambda r: r[0])

    out: list[tuple[datetime, float]] = []
    prev_sum = None
    for local_dt, change, total in parsed:
        if change is not None:
            kwh = float(change)
        elif total is not None and prev_sum is not None:
            kwh = float(total) - prev_sum
        else:
            # Nothing to difference against yet — the first sum-only row of the
            # window is the baseline, not an hour's usage.
            kwh = None
        if total is not None:
            prev_sum = float(total)
        if kwh is None or kwh < 0:
            # Negative is a cumulative-meter reset artifact, not negative
            # energy — discarded rather than subtracted, as the 5-minute path
            # and the daily archive both do for the same reason.
            continue
        out.append((local_dt, kwh))
    return out


def seed_from_hourly(plan, direction: str, records, until_local: datetime,
                     cap_used: dict, period_days: float | None) -> float:
    """Spend the free-tier allowance that ``records`` already used up.

    Walks hourly statistics in time order through the same ``_price_kwh`` used
    for today, discarding the dollars and keeping only what it did to
    ``cap_used`` — the point is to arrive at today's first slot with the right
    amount of allowance already gone. Returns the kWh walked, for logging.

    Each hour is priced as ``_SEED_SPLIT`` equal parts so an hour straddling a
    rate-window boundary splits its energy between the two tiers instead of
    charging it all to whichever one the hour starts in. Within an hour that
    resolves to a single rate (the normal case) the split changes nothing.
    """
    rows = _hourly_kwh(records)
    total = 0.0
    part = timedelta(hours=1) / _SEED_SPLIT
    for local_dt, change in rows:
        if local_dt >= until_local:
            # Today's own slots are priced by attach_cash; double-counting them
            # here would spend the allowance twice.
            continue
        total += change
        for k in range(_SEED_SPLIT):
            _price_kwh(plan, direction, local_dt + part * k,
                       change / _SEED_SPLIT, cap_used, period_days)
    return total


async def async_seed_cap_usage(
    hass: HomeAssistant, plan, slot_starts: list[datetime],
    sensor_for: dict, period_days: float | None,
) -> dict:
    """Free-tier kWh already spent before today, per cap key.

    ``sensor_for`` maps ``"import"``/``"export"`` to the configured energy
    sensor. Returns ``{}`` when no cap needs history, when the sensor isn't
    configured, or when the recorder can't answer — in which case the caps are
    priced as if the period started today, which over-states the free tier.
    That is the same direction ``cap_allowance`` errs when it has no billing
    period to pool over, and it is only ever a fallback now rather than the
    behaviour for every pooled plan.
    """
    need = caps_needing_history(plan, slot_starts)
    if not need or not slot_starts:
        return {}
    midnight = slot_starts[0].replace(hour=0, minute=0, second=0, microsecond=0)

    cap_used: dict = {}
    for direction, start in need.items():
        sensor = sensor_for.get(direction)
        if not sensor or start >= midnight:
            continue
        try:
            stats = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass, start, midnight, {sensor}, _SEED_PERIOD, None,
                {"change", "sum"},
            )
        except Exception as err:  # noqa: BLE001 — recorder is best-effort
            _LOGGER.debug("today-cash: cap seed fetch failed for %s: %s", sensor, err)
            continue
        walked = seed_from_hourly(plan, direction, (stats or {}).get(sensor),
                                  midnight, cap_used, period_days)
        _LOGGER.debug(
            "today-cash: cap seed %s from %s: %.2f kWh period-to-date", direction,
            start.isoformat(timespec="minutes"), walked,
        )
    return cap_used


async def async_attach_actual_cash(
    hass: HomeAssistant, plan, rows: list[dict], slot_minutes: int,
    import_sensor: str | None, export_sensor: str | None,
    seed_cache: dict | None = None,
) -> list[dict]:
    """Augment ``rows`` (``_elapsed_today_rates``' output) with metered cost/credit.

    Returns the same list, unchanged, whenever the data isn't there — no sensors
    configured, no statistics, or a recorder error. The caller publishes it either
    way, so a failure here costs the bars and nothing else.

    ``seed_cache`` is an opaque dict the caller keeps across updates. The
    period-to-date cap seed only changes at local midnight (it covers
    [period start, today 00:00), both fixed for the whole day), so caching it
    turns a month-long hourly history walk from something that would run on every
    coordinator tick into something that runs once a day.
    """
    if not rows or plan is None:
        return rows
    sensors = {s for s in (import_sensor, export_sensor) if s}
    if not sensors:
        return rows

    slot_delta = timedelta(minutes=slot_minutes)
    try:
        slot_starts = [dt_util.as_local(dt_util.parse_datetime(r["start"])) for r in rows]
    except Exception:  # noqa: BLE001
        _LOGGER.debug("today-cash: unparseable slot start", exc_info=True)
        return rows

    try:
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            slot_starts[0],
            slot_starts[-1] + slot_delta,
            sensors,
            "5minute",
            None,
            {"change"},
        )
    except Exception as err:  # noqa: BLE001 — recorder is best-effort
        _LOGGER.debug("today-cash: statistics fetch failed for %s: %s", sensors, err)
        return rows

    stats = stats or {}
    if not any(stats.get(s) for s in sensors):
        # Short-term statistics are retained for days, not months, so this is a
        # genuinely-empty-today case (fresh install, sensor just added) rather
        # than something to warn about on every tick.
        _LOGGER.debug("today-cash: no 5-minute statistics yet for %s", sensors)
        return rows

    buy = (bucket_changes_into_slots(stats.get(import_sensor), slot_starts, slot_delta)
           if import_sensor else [0.0] * len(rows))
    sell = (bucket_changes_into_slots(stats.get(export_sensor), slot_starts, slot_delta)
            if export_sensor else [0.0] * len(rows))

    # Grid Lens treats the calendar month as the billing period wherever it has
    # to assume one (same convention as advisory/demand.py's demand charge), so
    # a pooled cap's allowance is its stated per-day figure times this month's
    # length, spent from the 1st onwards.
    period_days = billing_period_days(slot_starts[0])
    sensor_for = {"import": import_sensor, "export": export_sensor}
    seed = await _async_cached_seed(
        hass, plan, slot_starts, sensor_for, period_days, seed_cache,
    )
    # dict(seed): attach_cash spends the allowance as it walks today, and the
    # cached seed must keep meaning "spent BEFORE today" for the next tick.
    return attach_cash(plan, rows, buy, sell, dict(seed), period_days)


async def _async_cached_seed(
    hass: HomeAssistant, plan, slot_starts: list[datetime], sensor_for: dict,
    period_days: float | None, seed_cache: dict | None,
) -> dict:
    """``async_seed_cap_usage``, computed once per local day per sensor set."""
    key = (
        slot_starts[0].date(),
        tuple(sorted(s for s in sensor_for.values() if s)),
        period_days,
        month_start(slot_starts[0]).date(),
    )
    if seed_cache is not None and seed_cache.get("key") == key:
        return seed_cache.get("seed") or {}
    seed = await async_seed_cap_usage(hass, plan, slot_starts, sensor_for, period_days)
    if seed_cache is not None:
        seed_cache.clear()
        seed_cache.update({"key": key, "seed": seed})
    return seed
