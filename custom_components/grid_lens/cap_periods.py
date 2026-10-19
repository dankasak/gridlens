"""How a capped rate tier's free allowance is bucketed, sized, and dated.

A capped rate ("the first N kWh at the boosted rate, the rest at the base rate")
needs three questions answered before any kWh can be priced against it:

  * **which bucket** does this moment's usage count towards (``cap_bucket``) —
    a strict cap resets each day/week/month/quarter/year, a pooled one has a
    single bucket for the whole billing period;
  * **how much free energy** that bucket is worth (``cap_allowance``) — a
    strict cap is worth exactly its stated figure, a pooled cap accrues the
    stated figure *per day* across the billing period, so unused headroom banks;
  * **when the bucket started** (``cap_period_start``) — needed by anything that
    has to go and fetch the usage already charged against it.

These lived as private methods on ``PlanCalculator`` while the bill breakdown
was the only thing pricing capped tiers. The price chart's cost/credit bars then
grew a second, simpler copy that handled only the strict-daily case and priced
everything else at the posted rate, which silently over-stated the credit on
every GloBird and Origin plan (both ``day`` caps applied ``pooled`` over the
billing period). Two implementations of a retailer's own rate structure is one
too many, so the primitives live here and both callers import them.

Pure: no Home Assistant imports, so an offline test can load this file directly.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta

# How many days one cap period spans, for sizing a POOLED allowance. Month and
# quarter are the average Gregorian lengths rather than the current month's own
# length — the figure divides a billing period, so a plan's allowance shouldn't
# depend on whether the bill happened to land in February.
CAP_PERIOD_DAYS = {"day": 1.0, "week": 7.0, "month": 30.44,
                   "quarter": 91.31, "year": 365.25}

# Cap periods that reset on a calendar boundary, smallest first. Anything not
# listed ('billing_period') has no calendar reset of its own.
_CALENDAR_PERIODS = ("day", "week", "month", "quarter", "year")


def cap_bucket(cap_period: str, local_dt: datetime):
    """The key a STRICT cap resets on."""
    if cap_period == "week":
        return local_dt.isocalendar()[:2]
    if cap_period == "month":
        return (local_dt.year, local_dt.month)
    if cap_period == "quarter":
        return (local_dt.year, (local_dt.month - 1) // 3)
    if cap_period == "year":
        return local_dt.year
    if cap_period == "billing_period":
        return "bill"          # one bucket for the whole bill
    return local_dt.date()     # 'day' — the default and the common case


def cap_allowance(cap: float, cap_period: str, cap_application: str,
                  period_days: int | float | None) -> float:
    """Total free kWh available to one cap key.

    STRICT: ``cap`` per bucket, which is what ``daily_cap_kwh`` has always meant.
    POOLED: the allowance accrues over the whole billing period — GloBird state
    it plainly ("if step one is allocated at 20kWh per day, and there are 30 days
    in the billing period, your step one allowance would be 600kWh"), and
    EnergyAustralia apply the same regulated Solar Sharer cap as "an average of
    24 kWh per day across your billing period". Pricing a pooled cap as strict
    understates the plan, because a quiet day can no longer subsidise a heavy one.
    """
    if cap_application != "pooled":
        return cap
    if not period_days:
        # No billing-period length to pool over: fall back to the strict reading
        # rather than inventing an allowance. Understates the plan, which is the
        # safe direction to be wrong in.
        return cap
    per = CAP_PERIOD_DAYS.get(cap_period)
    if per is None:          # 'billing_period' — already the whole period
        return cap
    return cap * (period_days / per)


def month_start(local_dt: datetime) -> datetime:
    """Midnight on the 1st of ``local_dt``'s month, same tzinfo.

    Grid Lens treats the calendar month as the billing period everywhere it has
    to assume one (see ``advisory/demand.py``'s ``billing_days_remaining`` /
    ``month_start_local``, which price the demand charge the same way). A user
    whose retailer bills 12th-to-11th therefore has their pool reset on the
    wrong day; that's a known, documented limitation of having no billing-cycle
    config, not something to guess at differently in each feature.
    """
    return local_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def billing_period_days(local_dt: datetime) -> int:
    """Length of the billing period containing ``local_dt``, in whole days."""
    return calendar.monthrange(local_dt.year, local_dt.month)[1]


def cap_period_start(cap_period: str, cap_application: str, local_dt: datetime,
                     billing_start: datetime | None = None) -> datetime:
    """Local datetime at which the current bucket's allowance began accruing.

    This is what a caller fetches metered history from in order to know how much
    of the allowance is already spent. A POOLED cap ignores ``cap_period``
    entirely — its one bucket is the billing period, however the stated figure
    happens to be expressed (GloBird's is "per day", pooled over the bill).
    """
    midnight = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if cap_application == "pooled" or cap_period == "billing_period":
        return billing_start or month_start(local_dt)
    if cap_period == "week":
        return midnight - timedelta(days=local_dt.weekday())
    if cap_period == "month":
        return month_start(local_dt)
    if cap_period == "quarter":
        first_month = ((local_dt.month - 1) // 3) * 3 + 1
        return midnight.replace(month=first_month, day=1)
    if cap_period == "year":
        return midnight.replace(month=1, day=1)
    return midnight            # 'day' — today's own usage is all that counts


def cap_needs_history(cap_period: str, cap_application: str) -> bool:
    """True when the current bucket started before today's midnight.

    The cheap gate for "do I have to go and read history to price this cap".
    False for the overwhelmingly common strict-daily cap (and for no cap at
    all), so a plan priced purely on today's own usage pays nothing for the
    machinery that exists for the pooled ones.
    """
    if cap_application == "pooled":
        return True
    return cap_period in ("week", "month", "quarter", "year", "billing_period")
