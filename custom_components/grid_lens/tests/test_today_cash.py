"""Offline tests for advisory/today_cash.py — today's metered cost and FiT credit.

These figures are what the price chart's cost/credit bars draw for the elapsed part
of the day, and they are the one number in that chart a user can check against a real
bill. Two ways to get them wrong, both covered here:

  * pricing metered kWh at the wrong rate, in particular ignoring a capped tier once
    the day's allowance is used up (a boosted export rate for the first N kWh/day is
    a real and common structure — see _split_capped_kwh in plan_calculator.py);
  * mis-bucketing 5-minute statistics into 30-minute slots, which silently shifts
    money into the wrong half-hour and, at a TOU boundary, prices it wrongly too.

Run: python3 tests/test_today_cash.py
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import types
from datetime import datetime, timedelta, timezone

logging.disable(logging.WARNING)
_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TZ = timezone(timedelta(hours=11))  # fixed offset stands in for Australia/Sydney


def _stub_ha():
    def _mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    for name in (
        "homeassistant", "homeassistant.core", "homeassistant.util",
        "homeassistant.util.dt", "homeassistant.components",
        "homeassistant.components.recorder",
        "homeassistant.components.recorder.statistics",
    ):
        if name not in sys.modules:
            _mod(name)

    sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
    sys.modules["homeassistant.components.recorder"].get_instance = lambda hass: None
    sys.modules["homeassistant.components.recorder.statistics"].statistics_during_period = None
    dt_mod = sys.modules["homeassistant.util.dt"]
    dt_mod.as_local = lambda d: d.astimezone(TZ)
    dt_mod.parse_datetime = lambda s: datetime.fromisoformat(s)


def _load():
    """Load advisory/today_cash.py with just enough package context for its
    ``from ..cap_periods import ...`` to resolve, without importing the real
    component (which would drag in Home Assistant proper). cap_periods.py is
    pure, so it loads from source as-is and these tests exercise the same
    bucketing/allowance code the bill breakdown uses."""
    _stub_ha()
    pkg = types.ModuleType("_gl")
    pkg.__path__ = [_COMPONENT]
    sys.modules["_gl"] = pkg
    sub = types.ModuleType("_gl.advisory")
    sub.__path__ = [os.path.join(_COMPONENT, "advisory")]
    sys.modules["_gl.advisory"] = sub

    def _load_src(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    caps = _load_src("_gl.cap_periods", os.path.join(_COMPONENT, "cap_periods.py"))
    mod = _load_src("_gl.advisory.today_cash",
                    os.path.join(_COMPONENT, "advisory", "today_cash.py"))
    mod._caps = caps
    return mod


class FlatPlan:
    """Single import/export rate, no caps — the common plan shape."""

    def __init__(self, imp=0.40, exp=0.05):
        self._imp, self._exp = imp, exp

    def get_import_rate_info(self, dt):
        return {"rate": self._imp, "label": "Usage", "daily_cap_kwh": None,
                "rate_after_cap": None, "cap_period": "day",
                "cap_application": "strict"}

    def get_export_rate_info(self, dt):
        return {"rate": self._exp, "label": "Feed-in", "daily_cap_kwh": None,
                "rate_after_cap": None, "cap_period": "day",
                "cap_application": "strict"}


class CappedExportPlan(FlatPlan):
    """Boosted feed-in for the first `cap` kWh each day, base rate after."""

    def __init__(self, boosted=0.20, base=0.03, cap=5.0, period="day",
                 application="strict"):
        super().__init__()
        self._boosted, self._base, self._cap = boosted, base, cap
        self._period, self._application = period, application

    def get_export_rate_info(self, dt):
        return {"rate": self._boosted, "label": "Feed-in", "daily_cap_kwh": self._cap,
                "rate_after_cap": self._base, "cap_period": self._period,
                "cap_application": self._application}


class WindowCappedPlan(FlatPlan):
    """Boosted export capped only inside a window — outside it there's no cap at
    all, so a cap scan must not conclude from one slot that there's nothing to
    seed (or from another that there is)."""

    def __init__(self, lo=10, hi=15, period="day", application="pooled"):
        super().__init__()
        self._lo, self._hi = lo, hi
        self._period, self._application = period, application

    def get_export_rate_info(self, dt):
        if not (self._lo <= dt.hour < self._hi):
            return {"rate": 0.05, "label": "Feed-in", "daily_cap_kwh": None,
                    "rate_after_cap": None, "cap_period": "day",
                    "cap_application": "strict"}
        return {"rate": 0.20, "label": "Super Export", "daily_cap_kwh": 5.0,
                "rate_after_cap": 0.03, "cap_period": self._period,
                "cap_application": self._application}


class TouPlan(FlatPlan):
    """Peak 15:00-21:00 local, off-peak otherwise — to catch slot misalignment."""

    def get_import_rate_info(self, dt):
        rate = 0.60 if 15 <= dt.hour < 21 else 0.20
        return {"rate": rate, "label": "Peak" if rate == 0.60 else "Off-peak",
                "daily_cap_kwh": None, "rate_after_cap": None,
                "cap_period": "day", "cap_application": "strict"}


def _rows(n, start_hour=0, slot_minutes=30):
    """`n` consecutive rate rows from local midnight, as _elapsed_today_rates emits."""
    t0 = datetime(2026, 10, 18, start_hour, 0, tzinfo=TZ)
    return [
        {"start": (t0 + timedelta(minutes=slot_minutes * i)).isoformat(),
         "import_rate": 0.40, "export_rate": 0.05}
        for i in range(n)
    ]


def _stat(local_dt, change):
    """One 5-minute statistics record, as the recorder returns it (epoch start)."""
    return {"start": local_dt.astimezone(timezone.utc).timestamp(), "change": change}


FAILURES = []


def check(name, got, want, tol=1e-6):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


def test_flat_pricing(m):
    print("flat plan: metered kWh x posted rate")
    rows = _rows(2)
    m.attach_cash(FlatPlan(imp=0.40, exp=0.05), rows, [2.0, 0.0], [0.0, 3.0])
    check("slot 0 cost", rows[0]["cost"], 0.8)
    check("slot 0 credit", rows[0]["credit"], 0.0)
    check("slot 1 credit", rows[1]["credit"], 0.15)
    check("slot 1 cost", rows[1]["cost"], 0.0)
    check("buy_kwh echoed", rows[0]["buy_kwh"], 2.0)
    check("sell_kwh echoed", rows[1]["sell_kwh"], 3.0)
    check("rows flagged actual", rows[0]["actual"], True)


def test_import_and_export_same_slot(m):
    print("one slot that both bought and sold (normal across 30 min)")
    rows = _rows(1)
    m.attach_cash(FlatPlan(imp=0.40, exp=0.05), rows, [1.0], [2.0])
    check("cost", rows[0]["cost"], 0.4)
    check("credit", rows[0]["credit"], 0.1)


def test_daily_export_cap(m):
    print("capped feed-in: boosted for first 5 kWh/day, base after")
    rows = _rows(4)
    # 2 + 2 + 2 + 2 kWh exported: 5 kWh at 20c, the remaining 3 kWh at 3c.
    m.attach_cash(CappedExportPlan(boosted=0.20, base=0.03, cap=5.0),
                  rows, [0, 0, 0, 0], [2.0, 2.0, 2.0, 2.0])
    check("slot 0 fully boosted", rows[0]["credit"], 0.40)
    check("slot 1 fully boosted", rows[1]["credit"], 0.40)
    check("slot 2 straddles the cap", rows[2]["credit"], 0.20 + 0.03)  # 1 free + 1 over
    check("slot 3 fully post-cap", rows[3]["credit"], 0.06)
    check("day total", round(sum(r["credit"] for r in rows), 6), 1.09)
    # The straddling slot's published rate must be the blend actually charged, so the
    # drawn line doesn't claim a rate the bar beside it contradicts.
    check("slot 2 rate blended", rows[2]["export_rate"], round(0.23 / 2.0, 4))
    check("slot 3 rate is post-cap", rows[3]["export_rate"], 0.03)


def test_pooled_cap_pools_over_the_billing_period(m):
    print("pooled day cap: allowance is the per-day figure x the billing period")
    # 5 kWh/day pooled over a 31-day October = 155 kWh for the month, so 8 kWh
    # exported today is nowhere near it and every kWh earns the boosted rate.
    rows = _rows(4)
    m.attach_cash(CappedExportPlan(boosted=0.20, base=0.03, cap=5.0,
                                   period="day", application="pooled"),
                  rows, [0] * 4, [2.0] * 4, None, 31)
    check("all 8 kWh boosted", round(sum(r["credit"] for r in rows), 6), 1.60)
    # Priced as STRICT daily it would have hit the 5 kWh cap mid-morning — the
    # bug this fixes drew that as a credit collapse the retailer never applied.
    strict = _rows(4)
    m.attach_cash(CappedExportPlan(boosted=0.20, base=0.03, cap=5.0),
                  strict, [0] * 4, [2.0] * 4)
    check("strict daily would have under-credited", round(sum(r["credit"] for r in strict), 6), 1.09)


def test_pooled_cap_with_period_to_date_seed(m):
    print("pooled cap, allowance mostly spent earlier in the month")
    plan = CappedExportPlan(boosted=0.20, base=0.03, cap=5.0,
                            period="day", application="pooled")
    # 150 of the month's 155 kWh already exported before today -> 5 kWh left.
    seed = {("export", None, "Feed-in"): 150.0}
    rows = _rows(4)
    m.attach_cash(plan, rows, [0] * 4, [2.0] * 4, seed, 31)
    check("slot 0 fully boosted", rows[0]["credit"], 0.40)
    check("slot 1 fully boosted", rows[1]["credit"], 0.40)
    check("slot 2 straddles the pool", rows[2]["credit"], 0.20 + 0.03)
    check("slot 3 fully post-cap", rows[3]["credit"], 0.06)
    check("day total", round(sum(r["credit"] for r in rows), 6), 1.09)
    # Over-stating the credit is exactly the symptom that was reported; an
    # exhausted pool must price at the base rate, not the posted one.
    spent = {("export", None, "Feed-in"): 155.0}
    rows2 = _rows(4)
    m.attach_cash(plan, rows2, [0] * 4, [2.0] * 4, spent, 31)
    check("exhausted pool prices at the base rate",
          round(sum(r["credit"] for r in rows2), 6), 0.24)


def test_strict_monthly_cap(m):
    print("strict monthly cap: one allowance for the whole month")
    rows = _rows(4)
    m.attach_cash(CappedExportPlan(boosted=0.20, base=0.03, cap=5.0,
                                   period="month", application="strict"),
                  rows, [0] * 4, [2.0] * 4)
    check("5 kWh free then base rate", round(sum(r["credit"] for r in rows), 6), 1.09)
    seeded = {("export", (2026, 10), "Feed-in"): 5.0}
    rows2 = _rows(4)
    m.attach_cash(CappedExportPlan(boosted=0.20, base=0.03, cap=5.0,
                                   period="month", application="strict"),
                  rows2, [0] * 4, [2.0] * 4, seeded)
    check("month's allowance already gone", round(sum(r["credit"] for r in rows2), 6), 0.24)


def test_cap_period_start(m):
    print("cap_period_start: where the seed walk begins")
    caps = m._caps
    d = datetime(2026, 10, 18, 14, 30, tzinfo=TZ)   # a Sunday
    check("strict day = today's midnight",
          caps.cap_period_start("day", "strict", d).isoformat(),
          datetime(2026, 10, 18, tzinfo=TZ).isoformat())
    check("strict week = Monday",
          caps.cap_period_start("week", "strict", d).isoformat(),
          datetime(2026, 10, 12, tzinfo=TZ).isoformat())
    check("strict month = the 1st",
          caps.cap_period_start("month", "strict", d).isoformat(),
          datetime(2026, 10, 1, tzinfo=TZ).isoformat())
    check("strict quarter = 1 Oct",
          caps.cap_period_start("quarter", "strict", d).isoformat(),
          datetime(2026, 10, 1, tzinfo=TZ).isoformat())
    check("strict year = 1 Jan",
          caps.cap_period_start("year", "strict", d).isoformat(),
          datetime(2026, 1, 1, tzinfo=TZ).isoformat())
    # A pooled cap ignores cap_period entirely — its bucket is the billing
    # period however the figure is expressed (GloBird's is "per day", pooled).
    check("pooled day = billing period start",
          caps.cap_period_start("day", "pooled", d).isoformat(),
          datetime(2026, 10, 1, tzinfo=TZ).isoformat())
    check("October bills 31 days", caps.billing_period_days(d), 31)
    check("strict daily needs no history",
          caps.cap_needs_history("day", "strict"), False)
    check("pooled daily does", caps.cap_needs_history("day", "pooled"), True)
    check("strict monthly does", caps.cap_needs_history("month", "strict"), True)


def test_caps_needing_history_scan(m):
    print("caps_needing_history: which directions need a seed walk, and from when")
    slots = [datetime(2026, 10, 18, 0, 0, tzinfo=TZ) + timedelta(minutes=30 * i)
             for i in range(48)]
    need = m.caps_needing_history(FlatPlan(), slots)
    check("no caps at all -> no walk", need, {})
    need = m.caps_needing_history(CappedExportPlan(), slots)
    check("strict daily cap -> no walk", need, {})
    need = m.caps_needing_history(WindowCappedPlan(), slots)
    check("pooled cap found on the export side only", sorted(need), ["export"])
    check("walks from the billing period start", need["export"].isoformat(),
          datetime(2026, 10, 1, tzinfo=TZ).isoformat())
    # The capped rate only exists inside 10:00-15:00; slots outside it report no
    # cap, and must not cancel out the ones that do.
    need = m.caps_needing_history(WindowCappedPlan(), slots[:4])  # 00:00-01:30
    check("no in-window slot today -> nothing to seed", need, {})


def test_seed_from_hourly(m):
    print("seed_from_hourly: spend the allowance the month already used")
    plan = CappedExportPlan(boosted=0.20, base=0.03, cap=5.0,
                            period="day", application="pooled")
    midnight = datetime(2026, 10, 18, 0, 0, tzinfo=TZ)
    recs = [_stat(midnight - timedelta(hours=h), 3.0) for h in range(1, 11)]
    recs.append(_stat(midnight - timedelta(hours=3), -9.0))   # meter reset
    recs.append(_stat(midnight + timedelta(hours=2), 7.0))    # today: not ours
    cap_used = {}
    walked = m.seed_from_hourly(plan, "export", recs, midnight, cap_used, 31)
    check("walked only the pre-today hours", round(walked, 6), 30.0)
    check("allowance spent matches", round(cap_used[("export", None, "Feed-in")], 6), 30.0)
    # And the seed then prices today correctly: 155 - 30 = 125 kWh still free.
    rows = _rows(2)
    m.attach_cash(plan, rows, [0, 0], [2.0, 2.0], dict(cap_used), 31)
    check("today still in the free tier", round(sum(r["credit"] for r in rows), 6), 0.80)


def test_seed_falls_back_to_sum_diff(m):
    print("hourly rows without `change`: differenced from `sum` instead")
    plan = CappedExportPlan(boosted=0.20, base=0.03, cap=5.0,
                            period="day", application="pooled")
    midnight = datetime(2026, 10, 18, 0, 0, tzinfo=TZ)
    # Cumulative sums only — no `change` key at all. 100 -> 130 over 3 hours.
    recs = [{"start": (midnight - timedelta(hours=h)).astimezone(timezone.utc).timestamp(),
             "sum": s}
            for h, s in ((4, 100.0), (3, 110.0), (2, 120.0), (1, 130.0))]
    cap_used = {}
    walked = m.seed_from_hourly(plan, "export", recs, midnight, cap_used, 31)
    check("differenced, first row is the baseline", round(walked, 6), 30.0)
    check("allowance spent", round(cap_used[("export", None, "Feed-in")], 6), 30.0)
    # A reset mid-window (sum drops) must not subtract.
    reset = recs + [{"start": (midnight - timedelta(minutes=30)).astimezone(timezone.utc).timestamp(),
                     "sum": 5.0}]
    cap2 = {}
    check("a sum that goes backwards is discarded, not subtracted",
          round(m.seed_from_hourly(plan, "export", reset, midnight, cap2, 31), 6), 30.0)
    # `change` wins when both are present.
    both = [{"start": (midnight - timedelta(hours=1)).astimezone(timezone.utc).timestamp(),
             "sum": 999.0, "change": 2.0}]
    cap3 = {}
    check("change preferred over sum when both present",
          round(m.seed_from_hourly(plan, "export", both, midnight, cap3, 31), 6), 2.0)


def test_seed_not_consumed_by_today(m):
    print("a cached seed must still mean 'before today' after pricing today")
    plan = CappedExportPlan(boosted=0.20, base=0.03, cap=5.0,
                            period="day", application="pooled")
    seed = {("export", None, "Feed-in"): 150.0}
    for _ in range(3):
        rows = _rows(4)
        # The entry point hands attach_cash a copy for exactly this reason; if it
        # handed over the cache itself, each tick would re-spend today's export
        # and the bars would decay through the afternoon.
        m.attach_cash(plan, rows, [0] * 4, [2.0] * 4, dict(seed), 31)
        check("same total on every tick", round(sum(r["credit"] for r in rows), 6), 1.09)
    check("seed untouched", seed[("export", None, "Feed-in")], 150.0)


def test_zero_kwh_keeps_posted_rate(m):
    print("a slot that traded nothing keeps the plan's posted rate on the line")
    rows = _rows(1)
    rows[0]["export_rate"] = 0.20
    m.attach_cash(CappedExportPlan(boosted=0.20, base=0.03, cap=5.0), rows, [0.0], [0.0])
    check("credit", rows[0]["credit"], 0.0)
    check("rate untouched", rows[0]["export_rate"], 0.20)


def test_bucketing(m):
    print("5-minute statistics summed into 30-minute slots")
    slot_starts = [datetime(2026, 10, 18, 0, 0, tzinfo=TZ) + timedelta(minutes=30 * i)
                   for i in range(3)]
    recs = [
        _stat(slot_starts[0], 0.1),                         # slot 0
        _stat(slot_starts[0] + timedelta(minutes=25), 0.2),  # slot 0, last 5 min
        _stat(slot_starts[1], 0.5),                         # slot 1
        _stat(slot_starts[1] + timedelta(minutes=30), 1.0),  # slot 2
        _stat(slot_starts[2] + timedelta(minutes=30), 9.9),  # past the end, dropped
        {"start": slot_starts[1].astimezone(timezone.utc).timestamp(), "change": -4.0},
    ]
    got = m.bucket_changes_into_slots(recs, slot_starts, timedelta(minutes=30))
    check("slot 0 sums its 5-min parts", round(got[0], 6), 0.3)
    check("slot 1 ignores the meter reset", round(got[1], 6), 0.5)
    check("slot 2", round(got[2], 6), 1.0)
    check("out-of-range record dropped", len(got), 3)


def test_bucketing_accepts_datetime_starts(m):
    print("statistics `start` may be a datetime, not an epoch float")
    slot_starts = [datetime(2026, 10, 18, 6, 0, tzinfo=TZ)]
    naive_utc = slot_starts[0].astimezone(timezone.utc).replace(tzinfo=None)
    got = m.bucket_changes_into_slots(
        [{"start": slot_starts[0], "change": 1.0},
         {"start": naive_utc, "change": 2.0},      # naive => treated as UTC
         {"start": None, "change": 5.0},
         {"change": 5.0}],
        slot_starts, timedelta(minutes=30))
    check("both forms land in the slot, bad rows skipped", round(got[0], 6), 3.0)


def test_tou_boundary(m):
    print("TOU boundary: each slot priced by its own local time")
    rows = _rows(4, start_hour=14)  # 14:00, 14:30, 15:00, 15:30 — peak starts 15:00
    m.attach_cash(TouPlan(), rows, [1.0, 1.0, 1.0, 1.0], [0] * 4)
    check("14:00 off-peak", rows[0]["cost"], 0.20)
    check("14:30 off-peak", rows[1]["cost"], 0.20)
    check("15:00 peak", rows[2]["cost"], 0.60)
    check("15:30 peak", rows[3]["cost"], 0.60)


def test_shorter_kwh_lists(m):
    print("fewer kWh values than rows (partial statistics) => remaining slots zeroed")
    rows = _rows(3)
    m.attach_cash(FlatPlan(), rows, [1.0], [])
    check("slot 0 priced", rows[0]["cost"], 0.4)
    check("slot 2 zero cost", rows[2]["cost"], 0.0)
    check("slot 2 still flagged actual", rows[2]["actual"], True)


def main():
    m = _load()
    for fn in (test_flat_pricing, test_import_and_export_same_slot, test_daily_export_cap,
               test_pooled_cap_pools_over_the_billing_period,
               test_pooled_cap_with_period_to_date_seed, test_strict_monthly_cap,
               test_cap_period_start, test_caps_needing_history_scan,
               test_seed_from_hourly, test_seed_falls_back_to_sum_diff,
               test_seed_not_consumed_by_today,
               test_zero_kwh_keeps_posted_rate,
               test_bucketing, test_bucketing_accepts_datetime_starts,
               test_tou_boundary, test_shorter_kwh_lists):
        fn(m)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all today_cash tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
