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

_LOGGER = logging.getLogger(__name__)


def _price_kwh(plan, direction: str, local_dt: datetime, kwh: float,
               cap_used: dict) -> tuple[float, float]:
    """Return ``(dollars, effective_rate)`` for ``kwh`` traded at ``local_dt``.

    Honours a capped rate tier whose allowance resets each calendar day (e.g. a
    plan's "first 15 kWh/day of export at the boosted rate, the rest at the base
    rate"), because that is the one cap shape today's own metered usage is enough
    to evaluate — ``cap_used`` carries the running free-tier total and the caller
    walks the day in order. A cap pooled over, or resetting on, a longer period
    (week/month/billing period) needs usage from *before* today that this module
    deliberately doesn't fetch, so it is priced at the posted rate and left alone;
    that errs toward the free tier, which is the same safe direction
    ``PlanCalculator._cap_allowance`` chooses when it can't pool.

    The effective rate is returned so the caller can republish it on the row: the
    forward trajectory already blends a tranche-split slot's rate the same way
    (see ``exp_rate_out`` in battery_optimizer.py), and the two halves of one line
    must not switch definitions at "now".
    """
    get_info = (plan.get_import_rate_info if direction == "import"
                else plan.get_export_rate_info)
    try:
        info = get_info(local_dt) or {}
    except Exception:  # noqa: BLE001 — a rate-lookup quirk must not sink the chart
        _LOGGER.debug("today-cash: %s rate lookup failed at %s", direction, local_dt,
                      exc_info=True)
        return 0.0, 0.0
    rate = float(info.get("rate") or 0.0)
    if kwh <= 0:
        return 0.0, rate

    cap = info.get("daily_cap_kwh")
    after_rate = info.get("rate_after_cap")
    period = info.get("cap_period") or "day"
    application = info.get("cap_application") or "strict"
    if not cap or after_rate is None or period != "day" or application != "strict":
        return kwh * rate, rate

    key = (direction, info.get("label") or "Energy")
    used = cap_used.get(key, 0.0)
    free_kwh = min(kwh, max(0.0, float(cap) - used))
    over_kwh = kwh - free_kwh
    cap_used[key] = used + free_kwh
    amount = free_kwh * rate + over_kwh * float(after_rate)
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
                sell_kwh: list[float]) -> list[dict]:
    """Add metered kWh and its priced cost/credit to each elapsed-today row.

    Pure, so the cap walk and the pricing are testable without a recorder. Rows
    are mutated in place (and returned) in time order, which is what makes the
    daily-cap accumulator correct.
    """
    cap_used: dict = {}
    for i, row in enumerate(rows):
        try:
            local_dt = dt_util.as_local(dt_util.parse_datetime(row["start"]))
        except Exception:  # noqa: BLE001
            continue
        imp = max(0.0, buy_kwh[i] if i < len(buy_kwh) else 0.0)
        exp = max(0.0, sell_kwh[i] if i < len(sell_kwh) else 0.0)
        cost, imp_rate = _price_kwh(plan, "import", local_dt, imp, cap_used)
        credit, exp_rate = _price_kwh(plan, "export", local_dt, exp, cap_used)
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


async def async_attach_actual_cash(
    hass: HomeAssistant, plan, rows: list[dict], slot_minutes: int,
    import_sensor: str | None, export_sensor: str | None,
) -> list[dict]:
    """Augment ``rows`` (``_elapsed_today_rates``' output) with metered cost/credit.

    Returns the same list, unchanged, whenever the data isn't there — no sensors
    configured, no statistics, or a recorder error. The caller publishes it either
    way, so a failure here costs the bars and nothing else.
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
    return attach_cash(plan, rows, buy, sell)
