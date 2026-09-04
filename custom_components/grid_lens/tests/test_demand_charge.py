#!/usr/bin/env python3
"""Demand-charge peak-shaving inputs for the rolling advisory LP.

A peak-demand charge is billed on the single highest in-window grid demand over
the whole billing period (``$/kW/day x days``). The advisory LP only sees a
24-48 h horizon, so without help it would keep shaving a peak that's already
locked in for the month, and under-value NOT setting a new one. The fix is two
numbers passed into ``optimize_hourly_schedule``:

  demand_peak_kw_month_to_date  -> a floor on the LP's peak variable
  demand_days_remaining         -> the price on that variable (rate x days-left)

This file tests the pure maths that produces them (``advisory/demand.py``) and
source-checks that ``battery_optimizer._lp_scipy`` actually consumes both. The
LP *solve* itself needs scipy, which isn't importable in this container, so that
part is verified on the live HA instance (same split as test_pooled_caps.py).

Run:  python3 test_demand_charge.py
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

# Load advisory/demand.py by path — importing `advisory` as a package pulls in
# forecast.py, which needs Home Assistant. demand.py itself is import-free.
_spec = importlib.util.spec_from_file_location(
    "grid_lens_demand", os.path.join(_COMPONENT, "advisory", "demand.py"))
_demand = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_demand)
billing_days_remaining = _demand.billing_days_remaining
build_demand_window_mask = _demand.build_demand_window_mask
demand_window_predicate = _demand.demand_window_predicate
days_to_season_end = _demand.days_to_season_end
month_start_local = _demand.month_start_local
peak_from_hourly_samples = _demand.peak_from_hourly_samples

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# A Wednesday and a Saturday, 3pm and 8am, for window tests.
WED_3PM = dt.datetime(2026, 9, 2, 15, 30)
WED_8AM = dt.datetime(2026, 9, 2, 8, 0)
SAT_3PM = dt.datetime(2026, 9, 5, 15, 0)


def test_window_predicate_hours_and_days():
    pred = demand_window_predicate([15, 16, 17, 18, 19, 20], "weekdays")
    check("in-window weekday hour matches", pred(WED_3PM))
    check("out-of-window weekday hour rejected", not pred(WED_8AM))
    check("in-window hour on a weekend rejected (weekdays spec)", not pred(SAT_3PM))

    pred_all = demand_window_predicate("all", "all")
    check("hours='all' days='all' matches any time",
          pred_all(WED_8AM) and pred_all(SAT_3PM))

    pred_wknd = demand_window_predicate([15], "weekends")
    check("weekends spec matches Saturday", pred_wknd(SAT_3PM))
    check("weekends spec rejects Wednesday", not pred_wknd(WED_3PM))


def test_build_demand_window_mask():
    # 6 half-hour slots from Wed 14:30 -> covers 14:30,15:00,15:30,16:00,16:30,17:00
    start = dt.datetime(2026, 9, 2, 14, 30)
    mask = build_demand_window_mask(start, 30, 6, [15, 16], "weekdays")
    check("mask length matches slot count", len(mask) == 6)
    check("14:30 slot out of window", mask[0] == 0)
    check("15:00 / 15:30 / 16:00 / 16:30 slots in window", mask[1:5] == [1, 1, 1, 1])
    check("17:00 slot out of window", mask[5] == 0)


def test_billing_days_remaining_calendar_month():
    check("1st of a 30-day month -> 30 days left",
          billing_days_remaining(dt.datetime(2026, 9, 1, 10, 0)) == 30)
    check("last day of the month -> 1 day left",
          billing_days_remaining(dt.datetime(2026, 9, 30, 23, 0)) == 1)
    check("mid-month -> counts today",
          billing_days_remaining(dt.datetime(2026, 9, 4, 8, 0)) == 27)
    check("February leap year handled",
          billing_days_remaining(dt.datetime(2024, 2, 15, 0, 0)) == 15)


def test_month_start_local_keeps_tzinfo():
    tzed = dt.datetime(2026, 9, 17, 13, 45, tzinfo=dt.timezone.utc)
    ms = month_start_local(tzed)
    check("month start is midnight on the 1st",
          (ms.year, ms.month, ms.day, ms.hour, ms.minute, ms.second) == (2026, 9, 1, 0, 0, 0))
    check("tzinfo preserved", ms.tzinfo is dt.timezone.utc)


def test_peak_from_hourly_samples():
    pred = demand_window_predicate([15, 16, 17], "weekdays")
    samples = [
        (dt.datetime(2026, 9, 1, 14, 0), 9.0),   # out of window — ignored though largest
        (dt.datetime(2026, 9, 1, 15, 0), 2.4),   # in window
        (dt.datetime(2026, 9, 1, 16, 0), 3.1),   # in window — the real in-window peak
        (dt.datetime(2026, 9, 2, 17, 0), -0.5),  # meter reset / negative — ignored
        (dt.datetime(2026, 9, 5, 16, 0), 8.0),   # Saturday — ignored (weekdays spec)
    ]
    check("returns the largest IN-WINDOW hourly import",
          peak_from_hourly_samples(samples, pred) == 3.1)
    check("empty history -> 0.0", peak_from_hourly_samples([], pred) == 0.0)
    check("all-out-of-window -> 0.0",
          peak_from_hourly_samples([(dt.datetime(2026, 9, 1, 2, 0), 5.0)], pred) == 0.0)


# --------------------------------------------------------------- optimizer wiring
def _optimizer_src() -> str:
    return open(os.path.join(_COMPONENT, "battery_optimizer.py")).read()


def test_optimizer_signature_chain_forwards_both_params():
    src = _optimizer_src()
    for fn in ("def optimize_hourly_schedule", "def _lp_optimize", "def _lp_scipy"):
        i = src.index(fn)
        sig = src[i:src.index(")", i) + 1] if fn != "def optimize_hourly_schedule" \
            else src[i:src.index("-> Dict", i)]
        check(f"{fn} takes demand_peak_kw_month_to_date",
              "demand_peak_kw_month_to_date" in sig)
        check(f"{fn} takes demand_days_remaining", "demand_days_remaining" in sig)


def test_lp_prices_peak_at_days_remaining_with_n_days_fallback():
    src = _optimizer_src()
    # objective: rate * (days_remaining if given else n_days)
    seg = src[src.index("if demand_active:"):src.index("if demand_active:") + 700]
    check("objective falls back to n_days when days_remaining is 0/absent",
          "demand_days_remaining" in seg and "n_days" in seg
          and "c_obj[P_idx] = demand_rate * demand_days" in seg)


def test_lp_clamps_peak_var_to_month_to_date_floor():
    src = _optimizer_src()
    check("lb[P_idx] is set from demand_peak_kw_month_to_date",
          re.search(r"lb\[P_idx\]\s*=\s*float\(demand_peak_kw_month_to_date\)", src)
          is not None)
    check("the floor only applies when a positive prior peak was passed",
          "demand_active and demand_peak_kw_month_to_date and demand_peak_kw_month_to_date > 0"
          in src)


def test_result_exposes_prior_peak():
    src = _optimizer_src()
    check("scipy result carries demand_peak_kw_prior",
          "'demand_peak_kw_prior'" in src)


# ------------------------------------------------------------- coordinator wiring
def test_coordinator_wires_demand_inputs_into_planner():
    src = open(os.path.join(_COMPONENT, "advisory", "coordinator.py")).read()
    check("coordinator builds the demand inputs",
          "self._demand_inputs(bundle)" in src)
    check("and passes month-to-date peak + days remaining to the planner",
          "demand_peak_kw_month_to_date=demand_mtd_peak" in src
          and "demand_days_remaining=demand_days_left" in src)
    check("gated on the demand-tariff config toggle + an active plan charge",
          "CONF_HAS_DEMAND_TARIFF" in src and 'getattr(plan, "demand_charge_active"' in src)
    check("month-to-date peak read from the import energy sensor's hourly stats",
          'self._cfg("energy_sensor"' in src and '"hour", None, {"sum"}' in src)


def test_days_to_season_end():
    D = dt.datetime
    # Mid-season: 2026-07-15, "High Season" ending 08-31 → 15,16..31 Aug = 48 days.
    check("mid-season count is inclusive of today and the end day",
          days_to_season_end(D(2026, 7, 15), "08-31") == (31 - 15 + 1) + 31, )
    check("last day of the season → 1", days_to_season_end(D(2026, 8, 31), "08-31") == 1)
    check("year-round (no end) → None", days_to_season_end(D(2026, 7, 15), None) is None)
    # Wrapping season: on 2026-12-10, a season ending 03-31 rolls to next year.
    got = days_to_season_end(D(2026, 12, 10), "03-31")
    check("a season end already past this year rolls to next year",
          got == (D(2027, 3, 31).date() - D(2026, 12, 10).date()).days + 1, got)
    check("never returns 0", days_to_season_end(D(2026, 9, 1), "08-31") >= 1)


def test_coordinator_demand_inputs_season_aware_for_demand_periods():
    src = open(os.path.join(_COMPONENT, "advisory", "coordinator.py")).read()
    i = src.index("async def _demand_inputs")
    body = src[i:src.index("\n    async def ", i + 10)]
    check("branches on the plan's demand_periods",
          'getattr(plan, "demand_periods"' in body)
    check("season+window+day predicate uses PlanFromData.demand_period_covers",
          "plan.demand_period_covers(pd, local_dt)" in body)
    check("LP rate is the blend of covering periods' rates over in-window slots",
          "plan.demand_rate_at(slot_dt)" in body
          and "rate = (rate_sum / rate_n)" in body)
    check("days priced = min(days left in month, days left in season)",
          "min(month_left, season_left)" in body
          and "days_to_season_end(now_local" in body)
    check("legacy single-window path still there for network-level demand charges",
          "demand_window_predicate(hours, days_spec)" in body
          and "legacy_rate" in body)
    # A demand_periods plan is modelled regardless of the has_demand_tariff
    # toggle; the toggle now guards only the legacy (elif) branch.
    check("early-return no longer requires CONF_HAS_DEMAND_TARIFF up front",
          "if not getattr(plan, \"demand_charge_active\", False):" in body)
    pre_periods = body.split("if periods:")[0]
    check("toggle not checked before the demand_periods branch",
          "CONF_HAS_DEMAND_TARIFF" not in pre_periods)
    check("toggle gates the legacy elif only",
          "elif not (self._cfg(CONF_HAS_DEMAND_TARIFF, False) and legacy_rate > 0):" in body)


def test_advisory_current_plan_merges_operator_demand_fields():
    """`_current_plan` must run the raw plan dict through `_prepare_plan_data`
    (like the comparison path) or the network operator's demand rate/window never
    reaches the advisory LP — demand_charge_per_kw_per_day stays 0 and nothing is
    shaved. Found live 2026-09-04 when the `demand` attribute stayed null on a
    demand-tariff plan."""
    src = open(os.path.join(_COMPONENT, "advisory", "coordinator.py")).read()
    check("_current_plan routes through _prepare_plan_data with the operators",
          "_prepare_plan_data(plan_id, plan_data[plan_id], network_operators)" in src)
    check("and sources the operators registry from the calculator",
          'getattr(calc, "network_operators"' in src)

    # The behaviour _current_plan now depends on: _prepare_plan_data + PlanFromData
    # fills demand fields from the operator when the plan flags demand_charge_active.
    rp_spec = importlib.util.spec_from_file_location(
        "grid_lens_retailer_plans", os.path.join(_COMPONENT, "retailer_plans.py"))
    rp = importlib.util.module_from_spec(rp_spec)
    rp_spec.loader.exec_module(rp)

    ops = {"ausgrid": {"demand_charge_per_kw_per_day": 0.42343,
                       "demand_window": {"hours": [15, 16, 17, 18, 19, 20],
                                         "days": "weekdays", "label": "Peak demand"}}}
    raw = {"name": "VPP Advantage", "network": "Ausgrid",
           "flags": {"demand_charge_active": True},
           "charges": {"daily_supply_charge": 1.7}}
    merged = rp._prepare_plan_data("engie_vpp_advantage_ea111", raw, ops)
    plan = rp.PlanFromData(merged)
    check("operator demand rate reaches the plan",
          abs(plan.demand_charge_per_kw_per_day - 0.42343) < 1e-9)
    check("operator demand window reaches the plan",
          (plan.demand_window or {}).get("hours") == [15, 16, 17, 18, 19, 20])

    # A plan WITHOUT the flag is untouched (no spurious demand charge).
    raw_flat = {"name": "Flat", "network": "Ausgrid", "flags": {},
                "charges": {"daily_supply_charge": 1.7}}
    flat = rp.PlanFromData(rp._prepare_plan_data("x", raw_flat, ops))
    check("a non-demand plan gets no operator demand rate",
          (flat.demand_charge_per_kw_per_day or 0.0) == 0.0)


def test_planner_forwards_and_summarises_demand():
    src = open(os.path.join(_COMPONENT, "advisory", "planner.py")).read()
    check("planner forwards both params to the optimizer",
          "demand_peak_kw_month_to_date=demand_peak_kw_month_to_date" in src
          and "demand_days_remaining=demand_days_remaining" in src)
    check("planner emits a demand summary on the result",
          "demand=demand_summary" in src and '"planned_peak_kw"' in src
          and '"prior_peak_kw"' in src and '"days_remaining"' in src)


# --------------------------------------------- plan comparison (re-verify, no regress)
def _compute_demand_charge(has_demand_tariff=True):
    """PlanCalculator._compute_demand_charge (+ _compute_demand_charge_periods),
    lifted out of the HA-importing module."""
    src = open(os.path.join(_COMPONENT, "plan_calculator.py")).read()
    i = src.index("    def _compute_demand_charge")
    j = src.index("    # How many of a rate's cap periods", i)
    ns: dict = {
        "DEFAULT_DEMAND_WINDOW_HOURS": [15, 16, 17, 18, 19, 20],
        "format_window_range": lambda w=None, *_a, **_k: (
            "3pm-9pm" if not w or w.get("start") else "3pm-9pm"),
        "timedelta": dt.timedelta,
    }
    exec(f"class C:\n    has_demand_tariff = {has_demand_tariff!r}\n" + src[i:j], ns)
    return ns["C"]()


class _Plan:
    demand_charge_per_kw_per_day = 0.12
    demand_charge_active = True
    demand_window = {"hours": [15, 16, 17, 18, 19, 20], "days": "weekdays",
                     "label": "Peak demand"}


def test_plan_comparison_demand_charge_arithmetic():
    c = _compute_demand_charge()
    # LP path: bill exactly the peak the optimiser solved, x rate x days.
    line = c._compute_demand_charge(_Plan(), [], {"demand_peak_kw": 4.0}, 30, None)
    check("amount == peak_kw * rate * days",
          line and abs(line["amount"] - 4.0 * 0.12 * 30) < 1e-9, line)
    check("source is the LP peak", line["source"] == "optimised-lp")
    check("line marked approximate", line["approximate"] is True)

    # Always emit the line, even at a fully-shaved $0 peak.
    zero = c._compute_demand_charge(_Plan(), [], {"demand_peak_kw": 0.0}, 30, None)
    check("a $0 peak still emits the line", zero is not None and zero["amount"] == 0.0)

    # No demand charge on the plan -> no line.
    class _Flat:
        demand_charge_per_kw_per_day = 0.0
        demand_charge_active = False
        demand_window = None
    check("a non-demand plan gets no line",
          c._compute_demand_charge(_Flat(), [], {"demand_peak_kw": 9.0}, 30, None) is None)


def test_has_demand_tariff_gates_only_the_legacy_charge():
    """`has_demand_tariff` (the customer's current-meter toggle) gates the legacy
    network-level charge only. A plan carrying its own demand_periods is priced
    regardless — choosing that plan IS being on a demand tariff, so its cost
    must not vanish just because the customer's current meter isn't a demand one
    (Amber "Smart Shift: Demand Tariff" would otherwise tie plain "Smart Shift").
    """
    off = _compute_demand_charge(has_demand_tariff=False)

    # legacy network-level plan, toggle off -> no line
    check("legacy demand charge suppressed when toggle off",
          off._compute_demand_charge(_Plan(), [], {"demand_peak_kw": 4.0}, 30, None) is None)

    # demand_periods plan, toggle off -> STILL priced
    tz = dt.timezone(dt.timedelta(hours=11))

    class _PeriodsPlan:
        demand_charge_active = True
        demand_charge_per_kw_per_day = 0.0
        demand_window = None
        demand_periods = [{
            "rate_per_kw_per_day": 0.43434, "days": "all",
            "start": "15:00:00", "end": "21:00:00",
            "season_label": "Summer", "season": {"start": "11-01", "end": "03-31"},
        }]

        @staticmethod
        def demand_period_covers(period, d):
            s = period["season"]
            probe = f"{d.month:02d}-{d.day:02d}"
            in_season = (s["start"] <= probe <= s["end"]) if s["start"] <= s["end"] \
                else (probe >= s["start"] or probe <= s["end"])
            return in_season and 15 <= d.hour < 21

    usage = [{"timestamp": dt.datetime(2026, 1, 10, 18, tzinfo=tz), "value": 5.0}]
    out = off._compute_demand_charge(_PeriodsPlan(), usage, None, 31, tz, prefer_actual=True)
    check("demand_periods plan still priced with toggle off", out is not None)
    summer = next((ln for ln in (out or {}).get("lines", []) if ln["label"] == "Summer"), None)
    check("Summer line present and non-zero", summer is not None and summer["amount"] > 0,
          summer)


def test_lp_feed_demand_periods_not_gated_by_toggle():
    """Source pin: the LP-feed demand block keys on demand_charge_active, and the
    has_demand_tariff check now guards only the legacy (elif) branch."""
    src = open(os.path.join(_COMPONENT, "plan_calculator.py")).read()
    i = src.index("# Demand-charge peak-shaving inputs")
    body = src[i:i + 1600]
    check("outer guard is demand_charge_active, not the toggle",
          "if getattr(plan, 'demand_charge_active', False):" in body)
    check("demand_periods branch has no has_demand_tariff check",
          "if _demand_periods:" in body
          and "self.has_demand_tariff" not in body.split("if _demand_periods:")[1]
              .split("elif")[0])
    check("legacy elif still gated by has_demand_tariff",
          "elif self.has_demand_tariff and getattr(plan, 'demand_charge_per_kw_per_day'" in body)


def _load_rp():
    spec = importlib.util.spec_from_file_location(
        "grid_lens_retailer_plans", os.path.join(_COMPONENT, "retailer_plans.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demand_periods_per_season_bill_lines():
    """A plan carrying demand_periods gets one bill sub-line per season/window,
    each with the peak-kW from actual usage inside THAT season+window and a day
    count of only the days that season covers in the bill."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Sydney")
    rp = _load_rp()
    c = _compute_demand_charge()

    plan = rp.PlanFromData({
        "name": "Acme Demand", "network": "Ausgrid",
        "flags": {"demand_charge_active": True},
        "charges": {"daily_supply_charge": 1.5},
        "demand_periods": [
            {"season_label": "High Season", "season": {"start": "06-01", "end": "08-31"},
             "rate_per_kw_per_day": 0.40, "days": "weekdays",
             "start": "15:00:00", "end": "21:00:00"},
            {"season_label": "High Season", "season": {"start": "11-01", "end": "03-31"},
             "rate_per_kw_per_day": 0.40, "days": "weekdays",
             "start": "15:00:00", "end": "21:00:00"},
            {"season_label": "Low Season", "season": {"start": "04-01", "end": "05-31"},
             "rate_per_kw_per_day": 0.15, "days": "weekdays",
             "start": "15:00:00", "end": "21:00:00"},
        ],
    })

    # Mon 2026-07-06 .. Fri 2026-07-17 — 12 days, all inside "Jun-Aug".
    usage = []
    for day in range(6, 18):
        d0 = dt.datetime(2026, 7, day, 0, 0, tzinfo=tz)
        usage.append({"timestamp": d0.replace(hour=16), "value": 5.0})   # in-window peak
        usage.append({"timestamp": d0.replace(hour=9), "value": 8.0})    # out of window
        usage.append({"timestamp": d0.replace(hour=23), "value": 7.0})   # out of window

    out = c._compute_demand_charge(plan, usage, None, 12, tz, prefer_actual=True)
    check("multi-period result carries a lines[] list", bool(out and out.get("lines")))
    lines = out["lines"]
    check("one line per demand_periods entry", len(lines) == 3, len(lines))

    hi_junaug = lines[0]
    check("Jun-Aug line: peak is the in-window 5.0 kW (not the 8.0 at 9am)",
          abs(hi_junaug["peak_kw"] - 5.0) < 1e-9, hi_junaug)
    check("Jun-Aug line: 12 in-season days", hi_junaug["days"] == 12, hi_junaug["days"])
    check("Jun-Aug line: amount == 5.0 * 0.40 * 12",
          abs(hi_junaug["amount"] - 5.0 * 0.40 * 12) < 1e-9, hi_junaug["amount"])

    check("Nov-Mar line: 0 in-season days this bill", lines[1]["days"] == 0)
    check("Nov-Mar line: $0", lines[1]["amount"] == 0.0)
    check("Low Season line: 0 in-season days this bill", lines[2]["days"] == 0)
    check("Low Season line: $0", lines[2]["amount"] == 0.0)

    check("total demand amount == sum of the season lines",
          abs(out["amount"] - sum(l["amount"] for l in lines)) < 1e-9)
    check("total == just the active Jun-Aug line here",
          abs(out["amount"] - 5.0 * 0.40 * 12) < 1e-9, out["amount"])


def test_demand_periods_override_network_fallback():
    """_prepare_plan_data must NOT merge the network-level demand rate/window
    onto a plan that carries its own demand_periods."""
    rp = _load_rp()
    ops = {"ausgrid": {"demand_charge_per_kw_per_day": 0.42343,
                       "demand_window": {"hours": [15, 16], "days": "weekdays"}}}
    raw = {"name": "P", "network": "Ausgrid",
           "flags": {"demand_charge_active": True},
           "charges": {"daily_supply_charge": 1.7},
           "demand_periods": [
               {"season_label": "All year", "rate_per_kw_per_day": 0.30,
                "days": "weekdays", "start": "15:00:00", "end": "21:00:00"}]}
    plan = rp.PlanFromData(rp._prepare_plan_data("p", raw, ops))
    check("network demand rate NOT merged over demand_periods",
          (plan.demand_charge_per_kw_per_day or 0.0) == 0.0)
    check("demand_periods preserved", len(plan.demand_periods) == 1)
    check("year-round period (no season) always covers",
          plan.demand_period_covers(plan.demand_periods[0],
                                    dt.datetime(2026, 2, 3, 16, 0)) is True)
    check("outside the window it does not cover",
          plan.demand_period_covers(plan.demand_periods[0],
                                    dt.datetime(2026, 2, 3, 9, 0)) is False)


def test_plan_comparison_path_keeps_full_period_pricing():
    """The comparison LP solves ONE window over the whole period. For a legacy
    (network-level) demand charge it keeps demand_rate x n_days — the exact
    formula there — by passing demand_days_remaining=0.0 (the LP's n_days
    fallback). It must still NOT get the month-to-date floor (that'd double-
    count). A demand_periods plan instead passes the in-season day count so a
    seasonal charge isn't priced over the whole year."""
    src = open(os.path.join(_COMPONENT, "plan_calculator.py")).read()
    i = src.index("async def _calculate_plan_cost_with_battery_optimization")
    m = re.search(r"\n    (?:async )?def ", src[i + 10:])
    body = src[i:i + 10 + m.start()] if m else src[i:]
    check("comparison LP still passes demand_rate", "demand_rate=demand_rate" in body)
    check("comparison LP still passes the window mask",
          "demand_window_mask=demand_window_mask" in body)
    check("comparison LP does NOT pass month-to-date peak",
          "demand_peak_kw_month_to_date" not in body)
    check("comparison LP passes in-season days (0.0 => n_days fallback for legacy)",
          "demand_days_remaining=demand_days_in_season" in body)
    check("in-season day count only accrues for demand_periods plans",
          "_demand_periods and in_win" in body
          and "demand_days_in_season = float(len(_dp_inseason_dates))" in body)
    check("demand amount is summed into the bill total",
          "+ demand_amount +" in src or "demand_amount +" in src)


def main():
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        print(f"{name}:")
        getattr(mod, name)()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} - " + ", ".join(FAILURES))
        sys.exit(1)
    print("OK - demand-charge peak-shaving inputs behave.")


if __name__ == "__main__":
    main()
