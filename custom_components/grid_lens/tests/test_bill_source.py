#!/usr/bin/env python3
"""End-to-end tests for the bill-sourced comparison path (bill_source.py).

Home Assistant is not importable here, so `bill_source` is loaded as a member of
a stub package whose `__path__` is the component directory — that makes its
`from .const import ...` and `from .bill_model import ...` resolve for real
(neither imports HA), without stubbing the things under test.

Two things are being proven, and the second is the one that matters:

  1. `BillUsageSource` serves exactly what the recorder path serves: hourly
     buckets, tz-aware UTC timestamps, the same keys, and totals that survive
     the half-hourly -> hourly -> 5-minute conversions.

  2. The whole reconstruction, priced against the household's REAL plan using
     the integration's own `retailer_plans.py`, reproduces the bill the
     retailer actually issued. This is the end-to-end oracle for the design: if
     the synthesised series plus the declared controlled-load device do not
     reproduce $676.78 on the plan the household is actually on, then no other
     plan's figure can be trusted either.

Also checked: the gates. A plan a bill cannot price honestly must be refused
with a reason, never priced on missing data — a demand plan billed without its
demand charge, or a controlled-load household priced against a plan with no CL
rate, does not drop out of the ranking, it tops it.

Run:  python3 test_bill_source.py
      (the plan-pricing tests need /homeassistant/tariff_compare/temp/
       plans_nsw_2026-10-25.json — a saved GET /plans response; they are
       skipped, loudly, if it is absent)
"""
from __future__ import annotations

import datetime as dt_mod
import importlib.util
import json
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
_PLANS = "/homeassistant/tariff_compare/temp/plans_nsw_2026-10-25.json"

FAILURES: list = []
SKIPPED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


def _load_package() -> types.ModuleType:
    """Load the component as a package so relative imports resolve."""
    pkg = types.ModuleType("glpkg")
    pkg.__path__ = [_COMPONENT]
    sys.modules["glpkg"] = pkg
    for name in ("const", "bill_model", "bill_source"):
        spec = importlib.util.spec_from_file_location(
            f"glpkg.{name}", os.path.join(_COMPONENT, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"glpkg.{name}"] = mod
        spec.loader.exec_module(mod)
        setattr(pkg, name, mod)
    return pkg


def _load_standalone(name: str, filename: str):
    """Load a module that imports only stdlib, outside the package.

    retailer_plans.py has to be loaded by path and NOT via sys.path, because
    the integration ships a HA platform file called datetime.py that would
    shadow the stdlib module and break its own `from datetime import ...`.
    """
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_COMPONENT, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gl = _load_package()
bm = gl.bill_model
bs = gl.bill_source

try:
    from zoneinfo import ZoneInfo
    SYD = ZoneInfo("Australia/Sydney")
except Exception:                                   # pragma: no cover
    SYD = dt_mod.timezone(dt_mod.timedelta(hours=11))


# Location and dates blurred, every figure intact — see the same fixture's
# comment in test_bill_model.py for what was changed and why.
ORIGIN_BILL = {
    "state": "NSW",
    "network": "Endeavour Energy",
    "postcode": "2500",
    "latitude": -34.4,
    "retailer": "Origin Energy",
    "plan_name": "Origin Go Variable",
    "plan_id": "origin_go_variable_ongoing_single_endeavour",
    "printed_energy_total": 676.78,
    "periods": [
        {"start": "2026-06-01", "days": 25,
         "general_kwh": 310.356, "general_rate": 0.300410,
         "cl_kwh": 430.032, "cl_rate": 0.158950,
         "export_kwh": 158.538, "fit_rate": 0.03,
         "supply_per_day": 0.722260, "cl_supply_per_day": 0.098450},
        {"start": "2026-06-26", "days": 67,
         "general_kwh": 896.517, "general_rate": 0.268070,
         "cl_kwh": 1104.910, "cl_rate": 0.158950,
         "export_kwh": 728.581, "fit_rate": 0.03,
         "supply_per_day": 1.462560, "cl_supply_per_day": 0.109747},
    ],
}


def _run(scenario_id: str = "status_quo", pv_kw: float | None = 6.6,
         **answer_kw):
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    answers = bm.IntakeAnswers(pv_kw=pv_kw, **answer_kw)
    return facts, bs.build_bill_run(None, facts, answers, scenario_id, SYD, "test")


# ---------------------------------------------------------------------------

def test_usage_source_contract() -> None:
    print("\nBillUsageSource matches the recorder's contract")
    facts, run = _run()
    src = run.source
    start = dt_mod.datetime.combine(facts.start_date, dt_mod.time(), tzinfo=SYD)
    end = dt_mod.datetime.combine(facts.end_date, dt_mod.time(), tzinfo=SYD)

    import asyncio
    rows = asyncio.run(src.series(start, end, bs.BILL_IMPORT_SENSOR, "hour"))
    check("one hourly bucket per hour of the period",
          len(rows) == facts.days * 24, f"{len(rows)} rows for {facts.days} days")
    check("every row has the recorder's four keys",
          all(set(r) == {"timestamp", "hour", "value", "hourly_rate"} for r in rows))
    check("timestamps are tz-aware UTC",
          all(r["timestamp"].tzinfo == dt_mod.timezone.utc for r in rows))
    check("timestamps are strictly increasing",
          all(a["timestamp"] < b["timestamp"] for a, b in zip(rows, rows[1:])))
    check("value and hourly_rate agree",
          all(r["value"] == r["hourly_rate"] for r in rows))
    check("no negative energy", min(r["value"] for r in rows) >= 0.0)

    imp_total = sum(r["value"] for r in rows)
    check("hourly import totals the billed register",
          abs(imp_total - facts.general_kwh) < 1.0,
          f"{imp_total:,.1f} vs {facts.general_kwh:,.1f} kWh")

    exp_rows = asyncio.run(src.series(start, end, bs.BILL_EXPORT_SENSOR, "hour"))
    fine = asyncio.run(src.series(start, end, bs.BILL_EXPORT_SENSOR, "5minute"))
    check("the 5-minute export series totals the same as the hourly one",
          abs(sum(r["value"] for r in fine) - sum(r["value"] for r in exp_rows)) < 0.01,
          f"{sum(r['value'] for r in fine):,.2f} vs "
          f"{sum(r['value'] for r in exp_rows):,.2f} kWh")
    check("5-minute series has 288 buckets per day",
          len(fine) == facts.days * 288, str(len(fine)))

    # A narrower window must return only that window — the calculator asks for
    # sub-ranges when it splits a bill period.
    week = asyncio.run(src.series(start, start + dt_mod.timedelta(days=7),
                                  bs.BILL_IMPORT_SENSOR, "hour"))
    check("a sub-range returns only that range", len(week) == 7 * 24, str(len(week)))

    # Roles the bill has no series for must come back empty, not raise — the
    # calculator probes sensors it may not have.
    none_rows = asyncio.run(src.series(start, end, "sensor.something_else", "hour"))
    check("an unknown role returns an empty series", none_rows == [])


def test_synthetic_entry() -> None:
    print("\nThe synthetic entry describes the billed household, not this one")
    facts, run = _run()
    data = run.entry.data
    check("entry_id is namespaced and cannot collide",
          run.entry.entry_id.startswith("bill_"), run.entry.entry_id)
    check("battery is forced off", data[gl.const.CONF_HAS_BATTERY] is False)
    check("no battery sensors",
          data[gl.const.CONF_BATTERY_SOC_SENSOR] is None
          and data[gl.const.CONF_BATTERY_CHARGE_POWER_SENSOR] is None)
    check("no price sensors",
          data[gl.const.CONF_IMPORT_PRICE_SENSOR] is None
          and data[gl.const.CONF_EXPORT_PRICE_SENSOR] is None)
    check("no deferrable load sensors",
          data[gl.const.CONF_DEFERRABLE_LOAD_SENSORS] == [])
    check("postcode and state come from the bill",
          data[gl.const.CONF_POSTCODE] == ORIGIN_BILL["postcode"]
          and data[gl.const.CONF_STATE] == "NSW")
    check("the network tariff code is left unset, so nothing is filtered on a guess",
          data[gl.const.CONF_NETWORK_TARIFF_CODES] == "")
    check("demand tariff follows the bill",
          data[gl.const.CONF_HAS_DEMAND_TARIFF] is False)
    check("the current plan is the bill's plan",
          data["current_plan"] == ORIGIN_BILL["plan_id"])

    check("controlled load becomes a declared CL device",
          data.get(gl.const.CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD)
          == ["controlled_load_1"])
    check("with the bill's daily kWh",
          abs(data[gl.const.CONF_DEFERRABLE_LOAD_DUMMY_KWH][0]
              - facts.cl_daily_kwh()) < 1e-9,
          f"{data[gl.const.CONF_DEFERRABLE_LOAD_DUMMY_KWH][0]:.2f} kWh/day")
    check("and NOT marked in_aggregate (it is a separate register)",
          data[gl.const.CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE] == [False])

    # Moving CL to the main circuit must remove the declared device, or the
    # energy is billed twice: once in the import series, once as a CL line.
    _, moved = _run("cl_to_main_solar")
    check("the CL scenario drops the declared CL device",
          gl.const.CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD not in moved.entry.data)


def test_banner() -> None:
    print("\nThe confidence banner carries the assumptions")
    _, run = _run()
    b = run.banner()
    check("tagged as a bill source", b["source"] == "bill")
    check("tagged MODELLED", b["fidelity"] == "MODELLED")
    check("carries the self-check result", b["self_check"]["ok"] is True)
    check("names the scenario", b["scenario"]["id"] == "status_quo")
    check("carries the model's assumptions",
          "day_share_pct" in b["model"] and "pv_kw_implied" in b["model"])
    check("states the one-season caveat in words",
          "one billing period" in b["one_season_caveat"])

    _, unknown_pv = _run(pv_kw=None)
    ub = unknown_pv.banner()
    check("an unidentified PV size is flagged in the banner",
          ub["model"]["pv_identified"] is False
          and ub["model"]["pv_range_kw"] is not None,
          str(ub["model"]["pv_range_kw"]))


# ---------------------------------------------------------------------------
# Against real plan data
# ---------------------------------------------------------------------------

def _all_plans():
    """Every plan in the saved catalogue, unfiltered by network."""
    rp = _load_standalone("gl_retailer_plans_t", "retailer_plans.py")
    with open(_PLANS) as fh:
        raw = json.load(fh)
    return rp.plans_from_api_data(raw["plans"], raw.get("network_operators"))


def _real_plans():
    """Only the plans priced for this bill's distributor."""
    return [p for p in _all_plans()
            if not p.network or "endeavour" in p.network.lower()]


def test_network_gate() -> None:
    """A plan priced for another distributor must be refused, not ranked.

    A plan's rates bundle one DNSP's network charges, so an Ausgrid plan is
    simply the wrong price for an Endeavour household — and being wrong in the
    cheap direction puts it at the TOP of the ranking. Found live 2026-10-25:
    a CovaU Ausgrid plan was being ranked for an Endeavour bill, because
    PlanFromData did not carry `network` at all and the two filters the live
    path relies on (DNSP tariff code, postcode) are both deliberately unset for
    a bill run.
    """
    print("\nOther distributors are refused, not ranked")
    if not os.path.exists(_PLANS):
        print(f"  SKIP  no plan data at {_PLANS}")
        SKIPPED.append("network-gate")
        return
    plans = _all_plans()
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    check("plans carry their distributor",
          any(p.network for p in plans),
          f"{sum(1 for p in plans if p.network)} of {len(plans)} named")

    gates = bs.BillGates(facts=facts, assumed_cl_rate=facts.own_cl_rate())
    keep, refused = bs.gate_plans(plans, gates)
    foreign = [p for p in plans
               if p.network and "endeavour" not in p.network.lower()]
    check("every other-distributor plan is refused",
          all(p.plan_id in refused for p in foreign),
          f"{len(foreign)} such plans, "
          f"{sum(1 for p in foreign if p.plan_id not in refused)} leaked")
    check("and the survivors are all on this bill's network",
          all(not p.network or "endeavour" in p.network.lower() for p in keep),
          f"{len(keep)} priceable")
    check("a plan with no stated network is NOT refused on that ground",
          all(bs.BillGates(facts=facts).reason(p) is None
              or not str(bs.BillGates(facts=facts).reason(p)).startswith("Other distributor")
              for p in plans if not p.network))


def test_gates() -> None:
    print("\nGates: refuse with a reason, never price on missing data")
    if not os.path.exists(_PLANS):
        print(f"  SKIP  no plan data at {_PLANS}")
        SKIPPED.append("gates")
        return
    plans = _real_plans()
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    gates = bs.BillGates(facts=facts)
    keep, refused = bs.gate_plans(plans, gates)

    check("some plans are priceable", len(keep) > 0, f"{len(keep)} of {len(plans)}")
    check("every refusal carries a reason",
          all(r and isinstance(r, str) for r in refused.values()))

    kinds = {}
    for why in refused.values():
        kinds[why.split(":")[0]] = kinds.get(why.split(":")[0], 0) + 1
    print(f"        refused {len(refused)} of {len(plans)}: {kinds}")

    market = [p for p in plans
              if getattr(p, "is_market_linked", False)
              or getattr(p, "spot_export_pricing", False)
              or getattr(p, "has_spot_pricing", False)]
    check("every market-linked plan is refused",
          all(p.plan_id in refused for p in market), f"{len(market)} such plans")

    demand = [p for p in plans
              if getattr(p, "demand_periods", None)
              or getattr(p, "demand_charge_active", False)
              or getattr(p, "demand_charge_per_kw_per_day", 0.0)]
    check("every demand plan is refused on a non-demand bill",
          all(p.plan_id in refused for p in demand), f"{len(demand)} such plans")

    no_cl = [p for p in plans
             if p.get_controlled_load_rate("controlled_load_1") is None]
    check("every plan without a CL rate is refused for a CL household",
          all(p.plan_id in refused for p in no_cl), f"{len(no_cl)} such plans")
    check("the plans that survive all hold a CL rate",
          all(p.get_controlled_load_rate("controlled_load_1") is not None
              for p in keep))

    # A demand bill DOES print the peak, so those plans become priceable — the
    # gate has to be conditional, not a blanket ban.
    demand_bill = {**ORIGIN_BILL, "periods": [
        {**ORIGIN_BILL["periods"][0], "demand_peak_kw": 4.2, "demand_rate": 0.18},
        {**ORIGIN_BILL["periods"][1], "demand_peak_kw": 5.1, "demand_rate": 0.18},
    ]}
    demand_facts = bm.BillFacts.from_dict(demand_bill)
    d_gates = bs.BillGates(facts=demand_facts)
    if demand:
        reasons = [d_gates.reason(p) for p in demand]
        check("a demand bill unblocks demand plans",
              not any(r and r.startswith("Demand") for r in reasons),
              f"{sum(1 for r in reasons if r and r.startswith('Demand'))} still blocked")


def _period_for(day: dt_mod.date, facts):
    for p in facts.periods:
        if p.start_date <= day < p.start_date + dt_mod.timedelta(days=p.days):
            return p
    return facts.periods[-1]


def test_reproduces_the_issued_bill() -> None:
    """The oracle: price the synthesised series with the BILL'S OWN rates.

    This is the end-to-end check on the reconstruction, and it has to use the
    bill's rates rather than the plan's current ones. The plan in our DB carries
    today's rates; the bill is Jun-Sep. Priced on the plan, the gap is a price
    change (real, and not a model error) and the check would be measuring plan-
    data freshness instead of the model. Priced on the bill's own rates, the
    only thing that can make this disagree is the reconstruction itself.

    The household's plan is flat single-rate, so the answer must come out at
    exactly the issued total: a flat rate has no time dependence, so no load
    shape can move it. A mismatch here means energy was lost or double-counted
    in the half-hourly -> hourly conversion, or a period boundary was mishandled.
    """
    print("\nReproducing the issued bill from the reconstruction")
    import asyncio
    facts, run = _run()
    start = dt_mod.datetime.combine(facts.start_date, dt_mod.time(), tzinfo=SYD)
    end = dt_mod.datetime.combine(facts.end_date, dt_mod.time(), tzinfo=SYD)
    imp = asyncio.run(run.source.series(start, end, bs.BILL_IMPORT_SENSOR, "hour"))
    exp = asyncio.run(run.source.series(start, end, bs.BILL_EXPORT_SENSOR, "hour"))

    energy = credit = 0.0
    for r in imp:
        p = _period_for(r["timestamp"].astimezone(SYD).date(), facts)
        energy += r["value"] * p.general_rate
    for r in exp:
        p = _period_for(r["timestamp"].astimezone(SYD).date(), facts)
        credit += r["value"] * p.fit_rate
    supply = sum(p.days * (p.supply_per_day + p.cl_supply_per_day)
                 for p in facts.periods)
    cl_cost = sum(p.cl_kwh * p.cl_rate for p in facts.periods)
    total = energy - credit + supply + cl_cost
    billed = facts.billed_total()

    print(f"        usage ${energy:,.2f}  feed-in -${credit:,.2f}  "
          f"supply ${supply:,.2f}  controlled load ${cl_cost:,.2f}")
    print(f"        reconstruction ${total:,.2f}   issued bill ${billed:,.2f}")
    check("the reconstruction reproduces the issued bill to the cent",
          abs(total - billed) < 0.02,
          f"${total - billed:+,.4f}")

    # Every archetype must give the same answer on a flat tariff, because the
    # rate does not vary by hour. Any dependence means shape is leaking into a
    # figure that cannot depend on it — the exact failure the EXACT/MODELLED
    # split exists to prevent.
    totals = []
    for arch in bm.ARCHETYPES:
        _, r2 = _run(pv_kw=6.6, archetype=arch)
        i2 = asyncio.run(r2.source.series(start, end, bs.BILL_IMPORT_SENSOR, "hour"))
        e2 = asyncio.run(r2.source.series(start, end, bs.BILL_EXPORT_SENSOR, "hour"))
        t = supply + cl_cost
        for r in i2:
            t += r["value"] * _period_for(
                r["timestamp"].astimezone(SYD).date(), facts).general_rate
        for r in e2:
            t -= r["value"] * _period_for(
                r["timestamp"].astimezone(SYD).date(), facts).fit_rate
        totals.append((arch, t))
    spread = max(t for _, t in totals) - min(t for _, t in totals)
    check("a flat tariff's total does not depend on the assumed load shape",
          spread < 0.02,
          "; ".join(f"{a}=${t:,.2f}" for a, t in totals))


def test_plan_rate_drift_is_visible() -> None:
    """Pricing the same usage on the plan's CURRENT rates is a different number.

    Informational but asserted, because the difference is the product's actual
    answer ("what would you pay today at this usage") and a reader comparing
    the dashboard against the paper bill will see it. If the two were ever
    identical, the DB's rates would have stopped tracking the retailer.
    """
    print("\nThe same usage on today's rates for the same plan")
    if not os.path.exists(_PLANS):
        print(f"  SKIP  no plan data at {_PLANS}")
        SKIPPED.append("drift")
        return
    plans = {p.plan_id: p for p in _real_plans()}
    plan = plans.get(ORIGIN_BILL["plan_id"])
    if plan is None:
        print(f"  SKIP  {ORIGIN_BILL['plan_id']} not in the saved plan data")
        SKIPPED.append("drift-plan")
        return

    import asyncio
    facts, run = _run()
    start = dt_mod.datetime.combine(facts.start_date, dt_mod.time(), tzinfo=SYD)
    end = dt_mod.datetime.combine(facts.end_date, dt_mod.time(), tzinfo=SYD)
    imp = asyncio.run(run.source.series(start, end, bs.BILL_IMPORT_SENSOR, "hour"))
    exp = asyncio.run(run.source.series(start, end, bs.BILL_EXPORT_SENSOR, "hour"))
    energy = sum(r["value"] * plan.get_import_rate(r["timestamp"].astimezone(SYD))
                 for r in imp)
    credit = sum(r["value"] * plan.get_export_rate(r["timestamp"].astimezone(SYD))
                 for r in exp)
    supply = plan.daily_supply_charge * facts.days
    cl_info = plan.get_controlled_load_rate(facts.cl_register)
    print(f"        usage ${energy:,.2f}  feed-in -${credit:,.2f}  "
          f"supply ${supply:,.2f}  "
          f"CL rate held: {'yes' if cl_info else 'NO'}")
    check("the feed-in credit is unchanged (3c then and now)",
          abs(credit - sum(p.export_kwh * p.fit_rate for p in facts.periods)) < 0.50,
          f"${credit:,.2f}")
    check("this plan holds no controlled-load rate, so the gate must refuse it",
          cl_info is None and bs.BillGates(facts=facts).reason(plan) is not None,
          "cl_info present" if cl_info else "refused as expected")


def test_assumed_cl_rate_unblocks_with_a_label() -> None:
    """Opting into one assumed CL rate makes the comparison answerable.

    Without it, 153 of 178 Endeavour plans — the household's own included —
    are refused for having no controlled-load rate, so there is nothing to
    compare. With it, they are priceable, and every figure that used it has to
    be traceable to the assumption.
    """
    print("\nThe assumed controlled-load rate")
    if not os.path.exists(_PLANS):
        print(f"  SKIP  no plan data at {_PLANS}")
        SKIPPED.append("assumed-cl")
        return
    plans = _real_plans()
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)

    own = facts.own_cl_rate()
    check("the bill's own CL rate is recovered as the suggested value",
          own is not None and abs(own - 0.15895) < 1e-6, f"${own:.6f}/kWh")
    check("and its CL supply charge is day-weighted",
          abs(facts.own_cl_supply() - (25 * 0.098450 + 67 * 0.109747) / 92) < 1e-9,
          f"${facts.own_cl_supply():.6f}/day")

    off = bs.BillGates(facts=facts)
    _, refused_off = bs.gate_plans(plans, off)
    on = bs.BillGates(facts=facts, assumed_cl_rate=own,
                      assumed_cl_supply=facts.own_cl_supply())
    keep_on, refused_on = bs.gate_plans(plans, on)
    print(f"        assumption off: {len(refused_off)} of {len(plans)} refused")
    print(f"        assumption on:  {len(refused_on)} of {len(plans)} refused")
    check("the assumption unblocks the controlled-load refusals",
          len(keep_on) > len(plans) - len(refused_off) + 100,
          f"{len(keep_on)} priceable, was {len(plans) - len(refused_off)}")
    # The bill's own plan is excepted: it is refused for a different and
    # unrelated reason (our rates for it disagree with the bill — see
    # test_own_plan_rates_are_checked_against_the_bill), which no controlled-load
    # assumption can or should unblock.
    check("but it does NOT unblock demand or market-linked plans",
          all("Demand" in r or "Market" in r
              for pid, r in refused_on.items() if pid != facts.plan_id),
          "; ".join(sorted({r.split(':')[0] for r in refused_on.values()})))

    fb = on.cl_fallback()
    check("the fallback is shaped like a plan's own CL rate info",
          fb and set(fb[facts.cl_register]) >= {"rate", "daily_supply_charge"})
    check("and is flagged as assumed",
          fb[facts.cl_register]["assumed"] is True)
    check("the default (opt-out) carries no fallback at all",
          bs.BillGates(facts=facts).cl_fallback() is None)

    # The run built from unmodified intake answers must not assume anything.
    _, run = _run()
    check("a default bill run has the assumption off",
          run.gates.cl_fallback() is None
          and run.banner()["controlled_load"]["assumed_rate"] is None)
    check("but the banner offers the household's own rate as the suggestion",
          abs(run.banner()["controlled_load"]["suggested_rate"] - 0.15895) < 1e-6)


def test_cl_move_is_priced_as_a_package() -> None:
    """Moving CL to the main circuit must cost the CL rate, not just gain solar."""
    print("\nControlled load -> main circuit, priced as a package")
    if not os.path.exists(_PLANS):
        print(f"  SKIP  no plan data at {_PLANS}")
        SKIPPED.append("cl-package")
        return
    plans = {p.plan_id: p for p in _real_plans()}
    plan = plans.get(ORIGIN_BILL["plan_id"])
    if plan is None:
        SKIPPED.append("cl-package-plan")
        print("  SKIP  plan missing")
        return

    import asyncio
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    start = dt_mod.datetime.combine(facts.start_date, dt_mod.time(), tzinfo=SYD)
    end = dt_mod.datetime.combine(facts.end_date, dt_mod.time(), tzinfo=SYD)

    def price(scenario_id):
        _, run = _run(scenario_id)
        imp = asyncio.run(run.source.series(start, end, bs.BILL_IMPORT_SENSOR, "hour"))
        exp = asyncio.run(run.source.series(start, end, bs.BILL_EXPORT_SENSOR, "hour"))
        total = (sum(r["value"] * plan.get_import_rate(r["timestamp"].astimezone(SYD))
                     for r in imp)
                 - sum(r["value"] * plan.get_export_rate(r["timestamp"].astimezone(SYD))
                       for r in exp)
                 + plan.daily_supply_charge * facts.days)
        cl = plan.get_controlled_load_rate(facts.cl_register)
        if run.profiles.cl_on_own_register and cl:
            total += run.profiles.cl_daily_kwh * facts.days * float(cl["rate"])
            total += float(cl.get("daily_supply_charge") or 0.0) * facts.days
        return total, run

    status, _ = price("status_quo")
    midday, mid_run = price("cl_to_main_solar")
    night, _ = price("cl_to_main_overnight")
    yr = 365.0 / facts.days
    print(f"        as billed        ${status:,.2f}  (${status * yr:,.0f}/yr)")
    print(f"        CL -> main, midday    ${midday:,.2f}  (${midday * yr:,.0f}/yr)")
    print(f"        CL -> main, overnight ${night:,.2f}  (${night * yr:,.0f}/yr)")

    check("re-wiring without re-timing is strictly worse than today",
          night > status, f"${night - status:+,.2f}")
    check("the midday move beats the overnight control",
          midday < night, f"${midday - night:+,.2f}")
    check("the CL supply charge is dropped when the register goes",
          not mid_run.profiles.cl_supply_charged)
    # On this flat plan the general rate (26.8c) is well above the CL rate
    # (15.9c), so even with the solar offset the move should LOSE money. A
    # scenario that only counted the solar upside would show a saving here, and
    # that is the failure mode this check exists for.
    check("on a flat plan whose general rate exceeds the CL rate, the move loses",
          midday > status, f"${midday - status:+,.2f} over {facts.days} days")


def test_own_plan_rates_are_checked_against_the_bill() -> None:
    """A bill is our only external check on our own plan data — spend it.

    For every other plan in a ranking our rates are only ever verified against
    PRD, which is to say against themselves. For the one plan the household
    actually holds, the bill prints the rates the retailer really applied, so
    the two can be compared directly — and if they disagree, the row labelled
    "your current plan" is wrong and every saving measured against it inherits
    that error.

    Found 2026-10-26 on the first real bill run: our rates for
    `origin_go_variable_ongoing_single_endeavour` priced this household's own
    92 days at $807.81 against a printed $676.78, and the comparison reported
    that as "+$131.02 vs current" — the plan they are on, shown as $131 worse
    than itself. Nothing in the output said so.
    """
    print("\nOur plan data against the rates the bill was charged at")
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)

    # Synthetic first, so the check is pinned independently of plan data on disk.
    avg = facts.own_rates()
    check("the household's effective rates are kWh- and day-weighted",
          abs(avg["general_rate"] - (310.356 * 0.300410 + 896.517 * 0.268070)
              / (310.356 + 896.517)) < 1e-9
          and abs(avg["supply_per_day"] - (25 * 0.722260 + 67 * 1.462560) / 92) < 1e-9,
          f"{avg['general_rate']:.5f} $/kWh, {avg['supply_per_day']:.5f} $/day")

    # But drift is measured against the LAST rate period, because our plan data
    # holds one current rate set. On this bill the day-weighted supply charge is
    # $1.26/day and neither period charged anything near it.
    own = facts.latest_rates()
    check("but drift compares against the bill's latest rate period",
          own["general_rate"] == 0.26807 and own["supply_per_day"] == 1.46256
          and own["cl_rate"] == 0.15895 and own["fit_rate"] == 0.03,
          f"{own['general_rate']:.5f} $/kWh, {own['supply_per_day']:.5f} $/day")
    check("and that is not the blended average it would otherwise use",
          abs(own["supply_per_day"] - avg["supply_per_day"]) > 0.15,
          f"latest {own['supply_per_day']:.5f} vs blended "
          f"{avg['supply_per_day']:.5f} $/day")

    class _Plan:
        plan_id = ORIGIN_BILL["plan_id"]
        def __init__(self, rate, supply):
            self.daily_supply_charge = supply
            self._r = rate
        def get_import_rate_defs(self): return [{"rate": self._r}]
        def get_export_rate_defs(self): return [{"rate": 0.03}]
        def get_controlled_load_rate(self, _): return None

    agree = _Plan(own["general_rate"], own["supply_per_day"])
    check("rates that match the bill produce no drift",
          bs.plan_data_drift(agree, facts) == {})
    check("and rounding at the fifth decimal is not drift",
          bs.plan_data_drift(_Plan(round(own["general_rate"], 5),
                                   round(own["supply_per_day"], 5)), facts) == {})

    # The real shape of the Origin finding: our stored figure is the PRD rate
    # times 1.1, and the bill is already GST-inclusive, so the gap is ~16%.
    off = _Plan(0.31020, 1.70324)
    drift = bs.plan_data_drift(off, facts)
    check("a 16% gap on the same plan IS drift", bool(drift),
          bs.describe_drift(drift))
    check("both figures are reported, not just a verdict",
          drift["supply_per_day"]["ours"] == 1.70324
          and drift["supply_per_day"]["billed"] == 1.46256
          and drift["supply_per_day"]["pct"] == 16.5)
    check("the worst component is named first",
          next(iter(drift)) in ("supply_per_day", "general_rate"),
          next(iter(drift)))

    gates = bs.BillGates(facts=facts)
    why = gates.reason(off)
    check("the gate refuses the household's own plan rather than pricing it",
          why is not None and why.startswith("Plan data disagrees"), (why or "")[:60])
    check("and records the comparison for the banner",
          gates.own_plan_checked and gates.own_plan_drift == drift)
    check("a plan that is not the bill's own plan is not drift-checked",
          bs.BillGates(facts=facts).reason(_other_plan(off)) is None
          or "Plan data disagrees" not in
             (bs.BillGates(facts=facts).reason(_other_plan(off)) or ""))

    # And the banner carries it either way, so an agreement is visible too:
    # it is the product's only external evidence that its plan data is right.
    _, run = _run()
    pdc = run.banner()["plan_data_check"]
    check("the banner exposes the check and the billed rates",
          pdc["plan_id"] == ORIGIN_BILL["plan_id"]
          and set(pdc) >= {"checked", "agrees", "drift", "billed_rates"}
          and pdc["billed_rates"]["cl_rate"] == 0.15895,
          f"checked={pdc['checked']} agrees={pdc['agrees']}")

    if not os.path.exists(_PLANS):
        print(f"  SKIP  no plan data at {_PLANS} — live check not run")
        SKIPPED.append("own-plan-live")
        return
    live = {p.plan_id: p for p in _real_plans()}.get(ORIGIN_BILL["plan_id"])
    if live is None:
        print(f"  SKIP  {ORIGIN_BILL['plan_id']} not in the saved plan data")
        SKIPPED.append("own-plan-live-plan")
        return
    live_drift = bs.plan_data_drift(live, facts)
    print(f"        saved plan data: {bs.describe_drift(live_drift) or 'agrees'}")
    check("the saved plan data is flagged against this bill (the live finding)",
          bool(live_drift), bs.describe_drift(live_drift))


def _other_plan(template):
    """The same rates on a different plan_id, to prove the check is targeted."""
    class _Other(type(template)):
        plan_id = "some_other_plan"
    return _Other(0.31020, 1.70324)


def main() -> int:
    print("=" * 72)
    print("  bill_source.py — bill-sourced comparison path")
    print("=" * 72)
    test_usage_source_contract()
    test_synthetic_entry()
    test_banner()
    test_gates()
    test_network_gate()
    test_reproduces_the_issued_bill()
    test_plan_rate_drift_is_visible()
    test_own_plan_rates_are_checked_against_the_bill()
    test_assumed_cl_rate_unblocks_with_a_label()
    test_cl_move_is_priced_as_a_package()
    print("\n" + "=" * 72)
    if SKIPPED:
        print(f"  skipped: {', '.join(SKIPPED)}")
    if FAILURES:
        print(f"  {len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES))
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
