#!/usr/bin/env python3
"""Tests for bill_model.py — the paper-bill energy reconstruction.

`bill_model` imports nothing but stdlib on purpose (see its docstring), so this
loads it straight by path with no Home Assistant stubs at all. The numbers it is
checked against come from a real Origin/Endeavour quarterly bill (Jun-Sep 2026),
the same bill `gridlens-api/tools/bill_sandbox.py` was built against.

What is being asserted, and why each one matters:

  1. The arithmetic self-check catches a wrong parse. This is the gate on
     accepting a PDF extraction at all, so a self-check that passes a corrupted
     bill is worse than no self-check.
  2. Calibration reproduces BOTH billed registers when the PV size is given.
  3. PV size is NOT identified without the nameplate, and the model says so
     rather than printing a confident midpoint. This is the finding the whole
     design rests on; a regression here turns the product into a fiction.
  4. Moving controlled load onto the main circuit conserves energy and actually
     competes with solar — i.e. the scenario axis is wired to the physics, not
     just relabelling a total.

Run:  python3 test_bill_model.py
"""
from __future__ import annotations

import datetime as dt_mod
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_COMPONENT, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bm = _load("gl_bill_model", "bill_model.py")

FAILURES: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


# A real bill, with every identifier removed and its location and dates blurred.
#
# BillFacts has no field for a name, address, NMI or account number — that is the
# point, and `test_schema_refuses_identifiers` holds it. But a postcode plus an
# exact billing cycle plus 92 days of exact consumption still narrows to one
# household, and this is a public repo, so:
#
#   - the postcode is the region's (2500, Wollongong), not the household's;
#   - the latitude is rounded to 0.1° (~11 km);
#   - both period starts are shifted by the same 5 days, which blurs the cycle
#     while keeping the periods contiguous and the day counts intact.
#
# Every kWh, rate and charge is untouched, so the oracle below still has to
# reproduce the issued $676.78 exactly. Shifting the dates does move the solar
# geometry the model calibrates against — that it still reconciles is the point
# of the test, not a coincidence to be tuned away.
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
        {
            "start": "2026-06-01", "days": 25,
            "general_kwh": 310.356, "general_rate": 0.300410,
            "cl_kwh": 430.032, "cl_rate": 0.158950,
            "export_kwh": 158.538, "fit_rate": 0.03,
            "supply_per_day": 0.722260, "cl_supply_per_day": 0.098450,
        },
        {
            "start": "2026-06-26", "days": 67,
            "general_kwh": 896.517, "general_rate": 0.268070,
            "cl_kwh": 1104.910, "cl_rate": 0.158950,
            "export_kwh": 728.581, "fit_rate": 0.03,
            "supply_per_day": 1.462560, "cl_supply_per_day": 0.109747,
        },
    ],
}


def test_schema_refuses_identifiers() -> None:
    print("\nSchema: no field for personal identifiers")
    for bad_key in ("name", "address", "nmi", "account_number", "meter_number"):
        raw = dict(ORIGIN_BILL)
        raw[bad_key] = "something the extractor scraped"
        try:
            bm.BillFacts.from_dict(raw)
            check(f"rejects {bad_key}", False, "accepted it")
        except ValueError as exc:
            check(f"rejects {bad_key}", bad_key in str(exc))

    try:
        bm.BillFacts.from_dict({"state": "NSW", "periods": []})
        check("rejects a bill with no periods", False, "accepted it")
    except ValueError:
        check("rejects a bill with no periods", True)


def test_self_check() -> None:
    print("\nArithmetic self-check")
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    res = facts.self_check()
    check("the real bill reconstructs its printed total", res.ok,
          f"${facts.billed_total():,.2f} vs ${facts.printed_energy_total:,.2f}; "
          f"{res.problems}")
    check("and reports itself as verifiable", res.verifiable,
          f"checked={res.checked}")

    # A parse that read the general rate as 0.26 instead of 0.268070 — the
    # single most likely kind of extraction error, and invisible by eye.
    corrupted = {**ORIGIN_BILL, "periods": [
        dict(ORIGIN_BILL["periods"][0]),
        {**ORIGIN_BILL["periods"][1], "general_rate": 0.26},
    ]}
    bad = bm.BillFacts.from_dict(corrupted).self_check()
    check("a mis-parsed rate is caught", not bad.ok,
          "; ".join(bad.problems) or "no problem reported")

    # No printed amounts captured: ok is vacuously True, and `verifiable` is
    # the field that stops a caller reading that as "verified".
    unchecked = {k: v for k, v in ORIGIN_BILL.items() if k != "printed_energy_total"}
    res = bm.BillFacts.from_dict(unchecked).self_check()
    check("an unverifiable parse is flagged, not blessed",
          res.ok and not res.verifiable, f"checked={res.checked}")


def test_aggregates() -> None:
    print("\nBill aggregates")
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    check("92 days", facts.days == 92, str(facts.days))
    check("start 2026-06-01", facts.start_date == dt_mod.date(2026, 6, 1))
    check("end (exclusive) 2026-09-01",
          facts.end_date == dt_mod.date(2026, 9, 1), str(facts.end_date))
    # _fit_periods walks the periods by cumulative day offset over one basis
    # built from start_date, so a gap between them would be silently ignored
    # and period 2 would be priced against the wrong days. The blurring above
    # shifts both starts equally for exactly this reason.
    check("the periods are contiguous",
          facts.periods[0].start_date
          + dt_mod.timedelta(days=facts.periods[0].days)
          == facts.periods[1].start_date,
          f"{facts.periods[0].start_date} +{facts.periods[0].days}d "
          f"vs {facts.periods[1].start_date}")
    check("1,206.9 kWh general", abs(facts.general_kwh - 1206.873) < 0.01)
    check("1,534.9 kWh controlled load", abs(facts.cl_kwh - 1534.942) < 0.01)
    check("controlled load is 56% of consumption",
          abs(facts.cl_kwh / (facts.cl_kwh + facts.general_kwh) - 0.56) < 0.01)
    check("has solar", facts.has_solar)
    check("not a demand bill", not facts.is_demand_bill)


def test_archetypes() -> None:
    print("\nLoad archetypes")
    for name, meta in bm.ARCHETYPES.items():
        shape = bm.archetype_shape(name)
        check(f"{name} sums to 1.0", abs(sum(shape) - 1.0) < 1e-9)
        check(f"{name} is non-negative", min(shape) >= 0.0)
        nat = bm.natural_day_share(name)
        check(f"{name} natural day share in range", 0.0 < nat < 1.0, f"{nat:.3f}")
        # Passing the natural share back in must be a no-op, or the day_share
        # parameter is not measured on the same scale it is reported on.
        reshaped = bm.archetype_shape(name, nat)
        check(f"{name} round-trips its own day share",
              max(abs(a - b) for a, b in zip(shape, reshaped)) < 1e-9)

    out = bm.natural_day_share("evening_peak")
    home = bm.natural_day_share("daytime_occupied")
    night = bm.natural_day_share("shift_worker")
    check("out-during-day has the lowest daytime share",
          out < home and night < home, f"out={out:.2f} home={home:.2f} night={night:.2f}")
    check("the flat control is ~10/24 daytime",
          abs(bm.natural_day_share("flat") - 10.0 / 24.0) < 1e-9)


def test_solar_geometry() -> None:
    print("\nSolar geometry")
    lat = -34.4
    jun = bm.clear_sky_shape(lat, dt_mod.date(2026, 6, 21))
    dec = bm.clear_sky_shape(lat, dt_mod.date(2026, 12, 21))
    jun_hours = sum(1 for v in jun if v > 0.01) * bm.SLOT_MIN / 60.0
    dec_hours = sum(1 for v in dec if v > 0.01) * bm.SLOT_MIN / 60.0
    check("the summer day is longer than the winter day",
          dec_hours > jun_hours + 2.0, f"Jun {jun_hours:.1f}h vs Dec {dec_hours:.1f}h")
    check("winter generates less per day than summer",
          sum(jun) < sum(dec), f"{sum(jun):.2f} vs {sum(dec):.2f}")
    check("both peak at 1.0", abs(max(jun) - 1.0) < 1e-9 and abs(max(dec) - 1.0) < 1e-9)
    check("nothing is generated at 02:00", jun[4] == 0.0 and dec[4] == 0.0)

    # Azimuth has to be a real term — a west array peaking at the same hour as a
    # north one would make "which way should the new panels face" unanswerable.
    north = bm.clear_sky_shape(lat, dt_mod.date(2026, 10, 15), azimuth=0.0)
    west = bm.clear_sky_shape(lat, dt_mod.date(2026, 10, 15), azimuth=90.0)
    check("a west array peaks later than a north array",
          west.index(max(west)) > north.index(max(north)),
          f"north slot {north.index(max(north))}, west slot {west.index(max(west))}")

    peaky = bm.clear_sky_shape(lat, dt_mod.date(2026, 10, 15), cloud_flatten=1.0)
    blunt = bm.clear_sky_shape(lat, dt_mod.date(2026, 10, 15), cloud_flatten=0.6)
    check("cloud_flatten blunts the curve without changing its peak",
          sum(blunt) > sum(peaky) and abs(max(blunt) - max(peaky)) < 1e-9)


def test_calibration_with_nameplate() -> None:
    print("\nCalibration with a declared nameplate")
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    answers = bm.IntakeAnswers(pv_kw=6.6, archetype="evening_peak")
    model = bm.calibrate(facts, answers)

    check("reproduces the billed import", abs(model.import_err_pct) < 1.0,
          f"{model.fit_import_kwh:,.1f} vs {model.target_import_kwh:,.1f} "
          f"({model.import_err_pct:+.2f}%)")
    check("reproduces the billed export", abs(model.export_err_pct) < 1.0,
          f"{model.fit_export_kwh:,.1f} vs {model.target_export_kwh:,.1f} "
          f"({model.export_err_pct:+.2f}%)")
    check("PV size is reported as identified", model.identified)
    check("one day per billed day", len(model.dates) == facts.days)
    check("every day carries its real date",
          model.dates[0] == facts.start_date
          and model.dates[-1] == facts.end_date - dt_mod.timedelta(days=1))
    check("implied kW lands near the declared nameplate",
          abs(model.pv_kw_implied - 6.6) < 0.3, f"{model.pv_kw_implied:.2f} kW")

    # Energy balance: general consumption = import + self-consumed solar.
    total_general = sum(sum(d) for d in model.general)
    self_cons = model.pv_total_kwh - model.fit_export_kwh
    check("general load balances import + self-consumption",
          abs(total_general - (model.fit_import_kwh + self_cons)) < 1.0,
          f"{total_general:,.1f} vs {model.fit_import_kwh + self_cons:,.1f}")

    # Seasonal distribution: a Jun-Sep bill must put more generation in its last
    # fortnight than its first, or the per-day geometry is not being used.
    first = sum(sum(d) for d in model.solar[:14])
    last = sum(sum(d) for d in model.solar[-14:])
    check("September generates more than June",
          last > first * 1.15, f"first 14d {first:.0f} kWh, last 14d {last:.0f} kWh")


def test_pv_not_identifiable() -> None:
    print("\nPV size is not identifiable from a bill (the central finding)")
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    answers = bm.IntakeAnswers(pv_kw=None)
    lo, hi = bm.admissible_pv_range(facts, answers)
    yk = bm.seasonal_yield(facts.latitude,
                           facts.start_date + dt_mod.timedelta(days=facts.days // 2))
    lo_kw, hi_kw = lo / facts.days / yk, hi / facts.days / yk
    check("the admissible range is wide, not a point",
          hi_kw > lo_kw * 2.0, f"{lo_kw:.1f}-{hi_kw:.1f} kW")

    model = bm.calibrate(facts, answers)
    check("the model admits PV was not identified", not model.identified)
    check("and carries the range", model.pv_range_kw is not None,
          str(model.pv_range_kw))
    check("and warns about it in words",
          any("NOT IDENTIFIED" in w for w in model.warnings),
          "; ".join(model.warnings)[:120])
    # The midpoint still has to fit, or the warning would be the only output and
    # the model itself would be unusable.
    check("the midpoint still reproduces both registers",
          abs(model.import_err_pct) < 1.5 and abs(model.export_err_pct) < 1.5,
          f"imp {model.import_err_pct:+.2f}% exp {model.export_err_pct:+.2f}%")


def test_no_solar_is_the_clean_case() -> None:
    print("\nA no-solar bill: import IS consumption")
    raw = {k: v for k, v in ORIGIN_BILL.items() if k != "printed_energy_total"}
    raw["periods"] = [{**p, "export_kwh": 0.0, "fit_rate": 0.0}
                      for p in ORIGIN_BILL["periods"]]
    facts = bm.BillFacts.from_dict(raw)
    model = bm.calibrate(facts, bm.IntakeAnswers(archetype="evening_peak"))
    check("no solar synthesised", model.pv_total_kwh == 0.0)
    check("import matches the register exactly",
          abs(model.fit_import_kwh - facts.general_kwh) < 1e-6)
    check("general total matches the register",
          abs(sum(sum(d) for d in model.general) - facts.general_kwh) < 0.01)
    check("warns that the shape is unconstrained",
          any("unconstrained" in w for w in model.warnings),
          "; ".join(model.warnings)[:100])
    check("the archetype's own day share is used unmoved",
          abs(model.day_share - bm.natural_day_share("evening_peak")) < 1e-9)


def test_cl_scenarios() -> None:
    print("\nControlled-load placement scenarios")
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    answers = bm.IntakeAnswers(pv_kw=6.6)
    model = bm.calibrate(facts, answers)
    scen = {s.id: s for s in bm.scenarios_for(facts)}
    check("three scenarios offered for a CL household", len(scen) == 3,
          ", ".join(scen))

    status = bm.synthesise(model, answers, scen["status_quo"])
    midday = bm.synthesise(model, answers, scen["cl_to_main_solar"])
    night = bm.synthesise(model, answers, scen["cl_to_main_overnight"])

    check("status quo keeps CL on its own register",
          status.cl_on_own_register and status.cl_supply_charged
          and abs(status.cl_daily_kwh - facts.cl_daily_kwh()) < 1e-9)
    check("status quo import matches the billed register",
          abs(status.totals()["import_kwh"] - facts.general_kwh) < 15.0,
          f"{status.totals()['import_kwh']:,.1f} vs {facts.general_kwh:,.1f}")

    for name, prof in (("midday", midday), ("overnight", night)):
        check(f"{name} drops the CL register and its supply charge",
              not prof.cl_on_own_register and not prof.cl_supply_charged
              and prof.cl_daily_kwh == 0.0)

    cl_total = facts.cl_kwh
    # Overnight on the main circuit never meets solar, so every CL kWh becomes
    # an imported kWh 1:1. That is the control: it isolates re-wiring from
    # re-timing.
    delta_night = night.totals()["import_kwh"] - status.totals()["import_kwh"]
    check("overnight-on-main adds CL energy to import 1:1",
          abs(delta_night - cl_total) < 5.0,
          f"import +{delta_night:,.0f} kWh vs {cl_total:,.0f} kWh of CL")
    check("overnight-on-main does not touch export",
          abs(night.totals()["export_kwh"] - status.totals()["export_kwh"]) < 1.0)

    # Midday competes with PV: part of the CL energy is served by solar that
    # would otherwise have been exported, so import rises by LESS than the CL
    # total and export falls. That is the whole upside of the scenario.
    delta_mid = midday.totals()["import_kwh"] - status.totals()["import_kwh"]
    export_lost = status.totals()["export_kwh"] - midday.totals()["export_kwh"]
    check("midday-on-main raises import by less than the CL total",
          0 < delta_mid < cl_total * 0.95,
          f"+{delta_mid:,.0f} kWh of {cl_total:,.0f} kWh")
    check("midday-on-main consumes solar that was being exported",
          export_lost > 50.0, f"export down {export_lost:,.0f} kWh")
    check("energy is conserved across the move",
          abs((delta_mid + export_lost) - cl_total) < 5.0,
          f"import +{delta_mid:,.0f} + export -{export_lost:,.0f} "
          f"vs CL {cl_total:,.0f}")


def test_element_rating_is_respected() -> None:
    print("\nElement kW rating caps absorption")
    facts = bm.BillFacts.from_dict(ORIGIN_BILL)
    # 16.7 kWh/day into a 5h window needs 3.3 kW. Declare a 2 kW element and the
    # window has to widen, or the model has silently invented a bigger element.
    answers = bm.IntakeAnswers(pv_kw=6.6, cl_max_kw=2.0,
                               cl_main_window=(10.0, 15.0))
    model = bm.calibrate(facts, answers)
    scen = {s.id: s for s in bm.scenarios_for(facts)}
    before = len(model.warnings)
    prof = bm.synthesise(model, answers, scen["cl_to_main_solar"])
    check("a window too short for the element is flagged",
          len(model.warnings) > before,
          "; ".join(model.warnings[before:])[:140])
    hours_used = sum(1 for v in prof.grid_import[0] if v > 0) * bm.SLOT_MIN / 60.0
    check("and the energy still all lands", hours_used > 0,
          f"{hours_used:.1f} h with import")
    needed = facts.cl_daily_kwh() / 2.0
    lit = sum(1 for g, s in zip(prof.general[45], model.general[45]) if g - s > 1e-9)
    check("the widened window is long enough for the element",
          lit * bm.SLOT_MIN / 60.0 >= needed - 0.5,
          f"{lit * bm.SLOT_MIN / 60.0:.1f} h available, {needed:.1f} h needed")


def main() -> int:
    print("=" * 72)
    print("  bill_model.py — paper-bill energy reconstruction")
    print("=" * 72)
    test_schema_refuses_identifiers()
    test_self_check()
    test_aggregates()
    test_archetypes()
    test_solar_geometry()
    test_calibration_with_nameplate()
    test_pv_not_identifiable()
    test_no_solar_is_the_clean_case()
    test_cl_scenarios()
    test_element_rating_is_respected()
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"  {len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES))
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
