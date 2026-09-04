#!/usr/bin/env python3
"""Spot (market-linked) plan pricing — SPOT_PRICING_DESIGN.md Phase 1.

Market-linked ALTERNATIVES (Amber Smart Shift) were priced from the plan DB's
static "(estimate)" bands + a flat 5c export, never from real 5-minute AEMO
spot — which made Amber's Solar Sharer standing offer out-rank Smart Shift on a
solar+battery install. Phase 1 adds:

  retailer_plans.PlanFromData    -> parses a `spot_pricing` block, `has_spot_pricing`
  PlanCalculator._spot_retail_rates -> RRP 5-min series -> per-hour retail $/kWh
  PlanCalculator._resolve_aemo_rrp_sensor -> region/state -> AEMO sensor id

battery_optimizer imports scipy (absent in this container), so the LP *solve*
that consumes the retail series is verified on the live HA instance. The pure
pieces are exercised here directly, same split as test_pooled_caps.py.

Run:  python3 test_spot_pricing.py
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


# --- helpers -----------------------------------------------------------------

def _spot_calc():
    """PlanCalculator._spot_retail_rates + _AEMO_REGION_SENSOR, without importing
    Home Assistant (same source-slice trick as test_pooled_caps._cap_helpers)."""
    src = open(os.path.join(_COMPONENT, "plan_calculator.py")).read()
    i = src.index("    _AEMO_REGION_SENSOR = {")
    j = src.index("    @staticmethod\n    def _compute_pea_credit")
    ns: dict = {"timezone": dt.timezone}
    exec("import datetime\nfrom datetime import timezone\nclass C:\n" + src[i:j], ns)
    return ns["C"]()


def _plan(spot_pricing):
    data = {
        "id": "acme_spot", "name": "Acme Spot", "retailer": "Acme", "state": "NSW",
        "network": "Ausgrid", "type": "market_linked",
        "charges": {"daily_supply_charge": 1.0, "monthly_subscription": 25.0},
        "import_rates": [{"label": "Estimate", "rate": 0.25,
                         "windows": [{"days": "all", "hours": "all"}]}],
        "export_rates": [{"label": "Spot export", "rate": None}],
        "flags": {"is_market_linked": True, "spot_export_pricing": True},
    }
    if spot_pricing is not None:
        data["spot_pricing"] = spot_pricing
    return retailer_plans.PlanFromData(data)


def _rrp(*pairs):
    """(hh:mm UTC, $/kWh) -> [{timestamp, value}] on 2026-08-01."""
    base = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    out = []
    for hm, v in pairs:
        h, m = map(int, hm.split(":"))
        out.append({"timestamp": base.replace(hour=h, minute=m), "value": v})
    return out


FULL = {
    "region": "NSW1",
    "import": {"adder_c_per_kwh": 20.0, "multiplier": 1.0, "cap_c_per_kwh": 55.0},
    "export": {"adder_c_per_kwh": -0.6, "multiplier": 1.0, "floor_c_per_kwh": 0.0},
}


# --- parsing ---------------------------------------------------------------

def test_parse_and_has_spot_pricing():
    p = _plan(FULL)
    check("spot_pricing retained", p.spot_pricing is not None)
    check("has_spot_pricing True", p.has_spot_pricing is True)
    check("region parsed", (p.spot_pricing.get("region")) == "NSW1")


def test_no_block_means_no_spot():
    for sp in (None, {}, {"region": "NSW1"}, {"import": {"multiplier": 1.05}}):
        p = _plan(sp)
        check(f"has_spot_pricing False for {sp!r}", p.has_spot_pricing is False)


def test_estimate_bands_still_parse_alongside():
    p = _plan(FULL)
    d = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    check("static import band still 0.25 (LP fallback for gap hours)",
          abs(p.get_import_rate(d) - 0.25) < 1e-9)


# --- retail transform ---------------------------------------------------------

def test_flat_transform_import_and_export():
    c = _spot_calc()
    # one clock hour, two 5-min RRP samples: 4c and 10c
    imp, exp = c._spot_retail_rates(_plan(FULL), _rrp(("00:00", 0.04), ("00:05", 0.10)))
    hk = dt.datetime(2026, 8, 1, 0, 0, tzinfo=dt.timezone.utc)
    # import: (0.04+0.20 + 0.10+0.20)/2 = 0.27
    check("import mean = rrp_mean + adder", abs(imp[hk] - 0.27) < 1e-9, imp)
    # export: (0.04-0.006 + 0.10-0.006)/2 = 0.064
    check("export mean = rrp_mean + (neg) adder", abs(exp[hk] - 0.064) < 1e-9, exp)


def test_cap_bites_per_interval_not_on_hour_mean():
    c = _spot_calc()
    # spike interval 0.50 -> retail 0.70, capped to 0.55; calm interval 0.02 -> 0.22
    imp, _ = c._spot_retail_rates(_plan(FULL), _rrp(("01:00", 0.50), ("01:05", 0.02)))
    hk = dt.datetime(2026, 8, 1, 1, 0, tzinfo=dt.timezone.utc)
    check("per-interval cap: (0.55 + 0.22)/2 = 0.385", abs(imp[hk] - 0.385) < 1e-9, imp)


def test_export_floor_and_negative_passthrough():
    c = _spot_calc()
    # floored plan: negative spot cannot push export below 0
    floored, _e = _plan({"import": {"adder_c_per_kwh": 20.0},
                         "export": {"adder_c_per_kwh": -0.6, "floor_c_per_kwh": 0.0}}), None
    _, exp = c._spot_retail_rates(floored, _rrp(("02:00", -0.05), ("02:05", -0.05)))
    hk = dt.datetime(2026, 8, 1, 2, 0, tzinfo=dt.timezone.utc)
    check("floor clamps export to 0", abs(exp[hk] - 0.0) < 1e-9, exp)
    # unfloored plan: negative export passes through (you pay to export)
    unfl = _plan({"import": {"adder_c_per_kwh": 20.0},
                  "export": {"adder_c_per_kwh": -0.6}})
    _, exp2 = c._spot_retail_rates(unfl, _rrp(("02:00", -0.05), ("02:05", -0.05)))
    check("no floor: export stays negative", exp2[hk] < 0, exp2)


def test_multiplier_applied():
    c = _spot_calc()
    p = _plan({"import": {"adder_c_per_kwh": 0.0, "multiplier": 1.1}})
    imp, _ = c._spot_retail_rates(p, _rrp(("03:00", 0.10)))
    hk = dt.datetime(2026, 8, 1, 3, 0, tzinfo=dt.timezone.utc)
    check("import multiplier 1.1 applied", abs(imp[hk] - 0.11) < 1e-9, imp)


def test_missing_defaults_multiplier_1_adder_0():
    c = _spot_calc()
    p = _plan({"import": {"adder_c_per_kwh": 5.0}})   # no multiplier, no export block
    imp, exp = c._spot_retail_rates(p, _rrp(("04:00", 0.10)))
    hk = dt.datetime(2026, 8, 1, 4, 0, tzinfo=dt.timezone.utc)
    check("import = rrp + 5c, multiplier defaults 1.0", abs(imp[hk] - 0.15) < 1e-9, imp)
    check("export = raw rrp (adder defaults 0)", abs(exp[hk] - 0.10) < 1e-9, exp)


# --- sensor resolution ------------------------------------------------------

def test_region_to_sensor_map():
    c = _spot_calc()
    m = c._AEMO_REGION_SENSOR
    check("NSW -> nsw1 sensor", m["NSW"] == "sensor.aemo_nem_nsw1_current_5min_period_price")
    check("VIC1 alias present", m["VIC1"] == "sensor.aemo_nem_vic1_current_5min_period_price")
    check("all 5 NEM states mapped", {"NSW", "VIC", "QLD", "SA", "TAS"} <= set(m))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {fn.__name__}: {e!r}")
            FAILURES.append(fn.__name__)
    print(f"\n{len(fns)} test fns, {len(FAILURES)} failure(s)")
    sys.exit(1 if FAILURES else 0)
