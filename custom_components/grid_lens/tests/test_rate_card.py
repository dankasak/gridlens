#!/usr/bin/env python3
"""Tests for retailer_plans.build_rate_card — the plan rate-card snapshot the
advisory card's buy/sell popup renders (``plan_rates`` on the dispatch sensor).

What matters here is not the formatting but the two things a wrong rate card
would get wrong silently:

  * the HIGHLIGHT. Each tier carries a per-slot bitmask of when it is the tier
    actually being charged, sampled through the plan's own rate matcher. Two
    tiers whose windows overlap must not both claim a slot — only the one the
    matcher (and therefore the bill) picks.
  * TIER IDENTITY. Two tiers of one plan can share a rate value (AGL Battery
    Rewards: a 3c morning-peak FiT and a 3c off-peak FiT). Keying by rate value
    collapses them, which is exactly how one of those windows disappeared from
    the bill breakdown on 2026-11-11. Each must keep its own mask.

Run:  python3 test_rate_card.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

import retailer_plans  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


TZ = retailer_plans.local_tz()


def _at(hour, minute=0, day=11):
    """A local-time instant on a Wednesday (2026-11-11) unless told otherwise."""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def _slot(hour, minute=0):
    return (hour * 60 + minute) // retailer_plans.RATE_CARD_SLOT_MINUTES


def _tou_plan():
    """A TOU import plan plus the AGL-shaped FiT tier set: three export tiers,
    two of which share a 3c rate."""
    return retailer_plans.PlanFromData({
        "id": "test_tou",
        "retailer": "Testco",
        "name": "Time Of Use",
        "network": "Ausgrid",
        "charges": {"daily_supply_charge": 1.1395, "monthly_subscription": 0.0},
        "import_rates": [
            {"label": "Peak", "rate": 0.5417,
             "windows": [{"start": "14:00", "end": "20:00", "days": "all"}]},
            {"label": "Off-Peak", "rate": 0.2160,
             "windows": [{"start": "20:00", "end": "14:00", "days": "all"}]},
        ],
        "export_rates": [
            {"label": "Morning peak export", "rate": 0.03,
             "windows": [{"start": "07:00", "end": "08:00", "days": "all"}]},
            {"label": "Day export", "rate": 0.28,
             "windows": [{"start": "17:00", "end": "20:00", "days": "all"}]},
            {"label": "Off-peak export", "rate": 0.03,
             "windows": [{"hours": "all"}]},
        ],
        "strategy": "Shift load out of the afternoon peak.",
    })


def test_tiers_and_ranges():
    card = retailer_plans.build_rate_card(_tou_plan(), _at(16, 45))
    check("plan name and retailer carried", card["plan_name"] == "Time Of Use"
          and card["retailer"] == "Testco")
    check("network carried", card["network"] == "Ausgrid")
    check("supply charge carried", abs(card["daily_supply_charge"] - 1.1395) < 1e-9)
    check("mask date is the local day asked about",
          card["mask_date"] == "2026-11-11", card["mask_date"])
    check("two import tiers", len(card["import"]) == 2, str(card["import"]))
    peak = card["import"][0]
    check("peak tier keeps its label", peak["label"] == "Peak")
    check("peak tier gets a human time range", peak["range"] == "2pm–8pm", str(peak["range"]))
    flat = [t for t in card["export"] if t["label"] == "Off-peak export"][0]
    check("an all-hours tier has no time range (it is not period-based)",
          flat["range"] is None, str(flat["range"]))
    check("masks are one character per slot",
          all(len(t["mask"]) == 48 for t in card["import"] + card["export"]))


def test_highlight_follows_the_matcher_not_the_windows():
    card = retailer_plans.build_rate_card(_tou_plan(), _at(16, 45))
    peak = [t for t in card["import"] if t["label"] == "Peak"][0]
    off = [t for t in card["import"] if t["label"] == "Off-Peak"][0]
    check("peak is in force at 4:45pm", peak["mask"][_slot(16, 30)] == "1")
    check("off-peak is not, at the same instant", off["mask"][_slot(16, 30)] == "0")
    check("peak starts at 2pm, not 1:30pm",
          peak["mask"][_slot(13, 30)] == "0" and peak["mask"][_slot(14)] == "1")
    check("and ends at 8pm (end-exclusive)",
          peak["mask"][_slot(19, 30)] == "1" and peak["mask"][_slot(20)] == "0")
    check("every slot is claimed by exactly one import tier",
          all((peak["mask"][i] == "1") != (off["mask"][i] == "1") for i in range(48)))

    # The overlap case: "Day export" 5pm-8pm is declared BEFORE an all-hours
    # tier, so the matcher charges it inside its window; the all-hours tier must
    # not also claim those slots or the popup would highlight two export rates.
    day = [t for t in card["export"] if t["label"] == "Day export"][0]
    flat = [t for t in card["export"] if t["label"] == "Off-peak export"][0]
    check("the narrower export tier wins its own window",
          day["mask"][_slot(17)] == "1" and flat["mask"][_slot(17)] == "0")
    check("the all-hours export tier covers the rest of the day",
          flat["mask"][_slot(12)] == "1" and day["mask"][_slot(12)] == "0")


def test_same_rate_two_tiers_keep_separate_masks():
    """The AGL Battery Rewards collision — 3c twice, two different windows."""
    card = retailer_plans.build_rate_card(_tou_plan(), _at(16, 45))
    threes = [t for t in card["export"] if abs(t["rate"] - 0.03) < 1e-9]
    check("both 3c export tiers survive as separate rows", len(threes) == 2,
          str([t["label"] for t in threes]))
    morning = [t for t in threes if t["label"] == "Morning peak export"][0]
    flat = [t for t in threes if t["label"] == "Off-peak export"][0]
    check("the 7-8am tier holds exactly its two half-hour slots",
          morning["mask"] == "".join(
              "1" if i in (_slot(7), _slot(7, 30)) else "0" for i in range(48)),
          morning["mask"])
    check("the all-hours tier does not claim 7am", flat["mask"][_slot(7)] == "0")
    check("but does claim 6am", flat["mask"][_slot(6)] == "1")


def test_day_split_windows_track_pricing_not_the_label():
    """⚠ CHARACTERISATION, not an endorsement.

    ``PlanFromData._in_window`` — every energy-rate pricing path — checks
    season and time of day but NOT a window's ``days`` spec, even though 180 of
    this install's cached import windows carry ``weekdays``/``weekends`` (e.g.
    alinta_solarbalance_choice_demand_time_of_use). So a weekday-only Peak is
    charged on Saturday too, and the rate card, which samples the mask through
    that same matcher deliberately, highlights it on Saturday too.

    That is the right call for THIS card — a highlight that disagreed with what
    the bill charges would be worse than one that faithfully shows a pricing
    bug — but it is a pricing bug, logged in OPEN_ITEMS.md (found 2026-11-11
    while building the rate card). When it is fixed, this test should fail and
    be rewritten to assert the day-aware behaviour: that is the point of it.
    """
    plan = retailer_plans.PlanFromData({
        "id": "wd", "retailer": "T", "name": "Weekday Peak",
        "charges": {"daily_supply_charge": 1.0},
        "import_rates": [
            {"label": "Weekday peak", "rate": 0.60,
             "windows": [{"start": "15:00", "end": "21:00", "days": "weekdays"}]},
            {"label": "Anytime", "rate": 0.30, "windows": [{"hours": "all"}]},
        ],
        "export_rates": [],
    })
    wed = retailer_plans.build_rate_card(plan, _at(16, 0, day=11))   # Wednesday
    sun = retailer_plans.build_rate_card(plan, _at(16, 0, day=15))   # Sunday
    wpeak = [t for t in wed["import"] if t["label"] == "Weekday peak"][0]
    speak = [t for t in sun["import"] if t["label"] == "Weekday peak"][0]
    check("the weekday peak is in force on Wednesday afternoon",
          wpeak["mask"][_slot(16)] == "1")
    check("the day spec reaches the displayed time range either way",
          speak["range"] == "3pm–9pm (weekdays)", str(speak["range"]))
    check("the mask matches what pricing does on Sunday (days spec ignored)",
          speak["mask"] == wpeak["mask"], speak["mask"])


def test_capped_tier_and_extras_round_trip():
    plan = retailer_plans.PlanFromData({
        "id": "capped", "retailer": "T", "name": "Capped FiT",
        "charges": {"daily_supply_charge": 0.95, "monthly_subscription": 5.0},
        "flags": {"is_market_linked": False},
        "import_rates": [{"label": "Usage", "rate": 0.30, "windows": [{"hours": "all"}]}],
        "export_rates": [
            {"label": "Solar Feed-in", "rate": 0.12, "daily_cap_kwh": 10.0,
             "rate_after_cap": 0.02, "cap_period": "day", "cap_application": "pooled",
             "windows": [{"hours": "all"}]},
        ],
        "controlled_load_rates": [
            {"register": "controlled_load_1", "label": "Controlled load 1",
             "rate": 0.21, "note": "Hot water"},
        ],
        "conditional_credits": [
            {"label": "Quiet evening credit", "condition": "max_import_kwh",
             "threshold_kwh": 0.03, "amount_per_day": 1.0,
             "window": {"start": "18:00", "end": "21:00", "days": "all"}},
        ],
        "vpp": {"monthly_credit": 30.44},
    })
    card = retailer_plans.build_rate_card(plan, _at(19, 0))
    fit = card["export"][0]
    check("cap fields carried", fit["daily_cap_kwh"] == 10.0
          and abs(fit["rate_after_cap"] - 0.02) < 1e-9)
    check("cap period/application carried",
          fit["cap_period"] == "day" and fit["cap_application"] == "pooled")
    check("subscription carried", abs(card["monthly_subscription"] - 5.0) < 1e-9)
    check("a VPP monthly credit shows as a daily credit",
          abs(card["fixed_daily_credit"] - 1.0) < 0.01, str(card["fixed_daily_credit"]))
    check("controlled load listed", card["controlled_load"][0]["rate"] == 0.21
          and card["controlled_load"][0]["note"] == "Hot water")
    cred = card["credits"][0]
    check("conditional credit listed with its window",
          cred["amount_per_day"] == 1.0 and cred["range"] == "6pm–9pm", str(cred))
    check("and marked active at 7pm (inside its window)", cred["active"] is True)
    check("not active at 10pm",
          retailer_plans.build_rate_card(plan, _at(22, 0))["credits"][0]["active"] is False)
    check("no demand section when the plan has no demand charge", "demand" not in card)
    check("no spot section when the plan is not market-linked", "spot" not in card)


def test_market_linked_plan_declares_its_spot_model():
    plan = retailer_plans.PlanFromData({
        "id": "spot", "retailer": "T", "name": "Wholesale",
        "charges": {"daily_supply_charge": 1.0},
        "flags": {"is_market_linked": True, "spot_export_pricing": True},
        "import_rates": [{"label": "Wholesale + margin", "rate": 0.25,
                          "windows": [{"hours": "all"}]}],
        "export_rates": [],
        "spot_pricing": {"region": "NSW1",
                         "import": {"adder_c_per_kwh": 9.5, "cap_c_per_kwh": 100.0},
                         "export": {"adder_c_per_kwh": -1.0}},
    })
    card = retailer_plans.build_rate_card(plan, _at(12, 0))
    check("market_linked flag surfaced", card["market_linked"] is True)
    check("spot export flag surfaced", card["spot_export"] is True)
    check("the import adder is published so the card can say what it is",
          card["spot"]["import_adder_c"] == 9.5, str(card.get("spot")))
    check("region carried", card["spot"]["region"] == "NSW1")


def test_versioned_plan_shows_only_todays_version():
    """A VersionedPlan's own rate-def list spans every version (old tiers tagged
    "(until …)") and averages the fixed charges across the billing period. The
    rate card must describe the version in force today instead."""
    old = {"id": "v", "retailer": "T", "name": "Versioned",
           "charges": {"daily_supply_charge": 1.00},
           "import_rates": [{"label": "Usage", "rate": 0.3113,
                             "windows": [{"hours": "all"}]}],
           "export_rates": []}
    new = dict(old, charges={"daily_supply_charge": 2.00},
               import_rates=[{"label": "Usage", "rate": 0.2805,
                              "windows": [{"hours": "all"}]}])
    # Billing period deliberately SPANS the 1 Nov price change, which is when
    # the wrapper's period-weighted fixed charges differ from any single day's.
    vp = retailer_plans.VersionedPlan(
        [(None, "2026-11-01", retailer_plans.PlanFromData(old)),
         ("2026-11-01", None, retailer_plans.PlanFromData(new))],
        dt.datetime(2026, 10, 17, 0, 0, tzinfo=TZ),
        dt.datetime(2026, 11, 16, 0, 0, tzinfo=TZ),
    )
    check("the wrapper itself averages the supply charge (unchanged behaviour)",
          1.0 < vp.daily_supply_charge < 2.0, str(vp.daily_supply_charge))
    card = retailer_plans.build_rate_card(vp, _at(12, 0))
    check("the card uses today's un-averaged supply charge",
          abs(card["daily_supply_charge"] - 2.00) < 1e-9,
          str(card["daily_supply_charge"]))
    check("and lists only today's version's tier", len(card["import"]) == 1
          and abs(card["import"][0]["rate"] - 0.2805) < 1e-9,
          str(card["import"]))
    check("which is in force all day", card["import"][0]["mask"] == "1" * 48)


def test_a_plan_with_no_rates_still_builds():
    plan = retailer_plans.PlanFromData({
        "id": "bare", "retailer": "T", "name": "Bare",
        "charges": {"daily_supply_charge": 1.0},
    })
    card = retailer_plans.build_rate_card(plan, _at(12, 0))
    check("import/export are empty lists, not missing",
          card["import"] == [] and card["export"] == [])


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"{name}:")
            fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — " + ", ".join(FAILURES))
        sys.exit(1)
    print("OK — rate card describes the plan the way the bill prices it.")


if __name__ == "__main__":
    main()
