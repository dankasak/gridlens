"""Drive the Plan Comparison page from an uploaded bill instead of the recorder.

`bill_model.py` reconstructs a half-hourly energy model from a bill's totals.
This module is the thin HA-side bridge that feeds that model into
`PlanCalculator` without touching anything else:

  * `BillUsageSource` satisfies the `UsageSource` protocol, so it answers the
    single `_get_usage_data` seam every energy series in a comparison arrives
    through. Ranking, `_compute_bill_items`, the caps, the LP, the charts and
    the What-If panel are all downstream of that seam and do not care where the
    numbers came from.
  * `SyntheticEntry` stands in for the config entry the calculator reads sensor
    ids, battery specs, CL wiring and postcode from. It is deliberately NOT a
    real config entry: creating one would spawn entities, register a second API
    key and show up in the user's integrations list.
  * `BillGates` refuses the plans a bill genuinely cannot price, **with a stated
    reason per plan**, rather than returning empty data and letting them look
    cheap. This is the part most likely to be "simplified" later and must not
    be: a demand plan priced without its demand charge, or a controlled-load
    household priced against a plan that has no CL rate, comes out as the
    cheapest offer on the market, which is exactly backwards.

The household's own usage never leaves the HA instance. Nothing here calls out
to the API except the plan catalogue and plan-version history the live path
already fetches.
"""

from __future__ import annotations

import datetime as dt_mod
import logging
from dataclasses import dataclass, field

from .bill_model import (
    SLOT_MIN,
    SLOTS,
    BillFacts,
    EnergyModel,
    IntakeAnswers,
    Scenario,
    SynthesisedProfiles,
    calibrate,
    scenarios_for,
    synthesise,
)
from .const import (
    CONF_BATTERY_CHARGE_POWER_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE,
    CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD,
    CONF_DEFERRABLE_LOAD_DUMMY_HOURS,
    CONF_DEFERRABLE_LOAD_DUMMY_KWH,
    CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW,
    CONF_DEFERRABLE_LOAD_DUMMY_NAMES,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DISTRIBUTOR,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_PRICE_SENSOR,
    CONF_GRIDLENS_API_KEY,
    CONF_GRIDLENS_API_URL,
    CONF_GRID_EXPORT_SENSOR,
    CONF_HAS_BATTERY,
    CONF_HAS_DEMAND_TARIFF,
    CONF_IMPORT_PRICE_SENSOR,
    CONF_NETWORK_TARIFF_CODES,
    CONF_POSTCODE,
    CONF_SOLAR_SENSOR,
    CONF_STATE,
)

_LOGGER = logging.getLogger(__name__)

# Placeholder entity ids. They name a role, never a real entity — the usage
# source is keyed on the role, so these only have to be distinct and obviously
# fake in a log line. Nothing ever calls hass.states.get() on them.
BILL_IMPORT_SENSOR = "sensor.bill_grid_import"
BILL_SOLAR_SENSOR = "sensor.bill_solar"
BILL_EXPORT_SENSOR = "sensor.bill_grid_export"

STORAGE_KEY = "grid_lens.bills"
STORAGE_VERSION = 1


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

class BillUsageSource:
    """Serves synthesised series in the exact shape `_get_usage_data` returns.

    Timestamps are tz-aware UTC and `hour` is the UTC hour, mirroring the
    recorder path byte for byte — including the fact that `hour` is UTC and so
    not the local hour. Everything downstream converts with
    `.astimezone(tz)` on `timestamp` and ignores `hour`; deliberately matching
    the live path rather than "fixing" it here keeps the two sources from
    quietly disagreeing about what the field means.
    """

    def __init__(self, profiles: SynthesisedProfiles, tz) -> None:
        self._tz = tz
        self._hourly = {
            BILL_IMPORT_SENSOR: _to_hourly(profiles.dates, profiles.grid_import, tz),
            BILL_SOLAR_SENSOR: _to_hourly(profiles.dates, profiles.solar, tz),
            BILL_EXPORT_SENSOR: _to_hourly(profiles.dates, profiles.grid_export, tz),
        }
        # 5-minute export, for sub-hourly feed-in windows (Flow Power's
        # 17:30-19:30 and similar). The model's own resolution is half-hourly,
        # so each slot is split into six equal buckets: that is the same shape
        # at finer granularity, not invented detail. Without it
        # `_compute_bill_items` falls back to pro-rating hourly buckets, which
        # splits a window-crossing hour by clock time rather than by where the
        # export actually sat.
        self._fine = {
            BILL_EXPORT_SENSOR: _to_fine(profiles.dates, profiles.grid_export, tz),
        }

    async def series(self, start_time, end_time, sensor_id: str | None,
                     period: str = "hour") -> list:
        table = self._fine if period != "hour" else self._hourly
        rows = table.get(sensor_id or BILL_IMPORT_SENSOR)
        if rows is None:
            if period == "hour":
                _LOGGER.debug("Bill source has no series for %s", sensor_id)
            return []
        return [r for r in rows if start_time <= r["timestamp"] < end_time]


def _to_hourly(dates: list, grid: list, tz) -> list:
    """Collapse half-hourly slots into the hourly buckets the recorder serves."""
    per_hour = 60 // SLOT_MIN
    out = []
    for day, slots in zip(dates, grid):
        midnight = dt_mod.datetime.combine(day, dt_mod.time(), tzinfo=tz)
        for hour in range(24):
            kwh = sum(slots[hour * per_hour:(hour + 1) * per_hour])
            ts = (midnight + dt_mod.timedelta(hours=hour)).astimezone(dt_mod.timezone.utc)
            out.append({"timestamp": ts, "hour": ts.hour,
                        "value": kwh, "hourly_rate": kwh})
    return out


def _to_fine(dates: list, grid: list, tz) -> list:
    """Split each half-hourly slot into six 5-minute buckets of equal size."""
    out = []
    for day, slots in zip(dates, grid):
        midnight = dt_mod.datetime.combine(day, dt_mod.time(), tzinfo=tz)
        for slot, kwh in enumerate(slots):
            share = kwh / 6.0
            base = midnight + dt_mod.timedelta(minutes=slot * SLOT_MIN)
            for j in range(6):
                ts = (base + dt_mod.timedelta(minutes=5 * j)).astimezone(
                    dt_mod.timezone.utc)
                out.append({"timestamp": ts, "hour": ts.hour,
                            "value": share, "hourly_rate": share})
    return out


# ---------------------------------------------------------------------------
# The synthetic config entry
# ---------------------------------------------------------------------------

@dataclass
class SyntheticEntry:
    """Just enough of a `ConfigEntry` for `PlanCalculator` to read.

    `entry_id` is prefixed so it can never collide with a real entry id, and so
    a stray `get_live_number` / entity-registry lookup against it resolves to
    nothing and falls back to the value in `.data` — which is the behaviour we
    want, not an error.
    """

    data: dict
    entry_id: str
    options: dict = field(default_factory=dict)
    title: str = "Bill comparison"
    domain: str = "grid_lens"


def build_synthetic_entry(real_entry, facts: BillFacts, answers: IntakeAnswers,
                          profiles: SynthesisedProfiles, bill_id: str):
    """A config entry describing the billed household, not this one.

    Carried over from the real entry: only the API credentials and base URL, so
    the plan catalogue and plan-version history can be fetched exactly as the
    live path fetches them. Nothing about this install's hardware, sensors,
    battery, deferrable loads or plan comes across — if it did, the comparison
    would silently be about the owner's house rather than the billed one.

    Forced off, with reasons:
      * `has_battery` — a bill cannot show battery behaviour at all (the actuals
        come from `state_changes_during_period` on power/SOC, which bypasses the
        usage seam). Battery scenarios go through the existing
        `whatif_battery_kwh` path instead, which already handles a household
        with no real battery.
      * price sensors — absent, so market-linked plans are excluded by
        `BillGates` rather than priced against a sensor that does not exist.
      * `deferrable_load_sensors` — there are none; a declared controlled-load
        device carries the CL register instead, and keeping this empty also
        avoids the entity-registry read in `_deferrable_visibility`.
    """
    real = real_entry.data if real_entry is not None else {}
    data = {
        CONF_GRIDLENS_API_KEY: real.get(CONF_GRIDLENS_API_KEY, ""),
        CONF_GRIDLENS_API_URL: real.get(CONF_GRIDLENS_API_URL,
                                        "https://api.gridlens.au"),

        CONF_ENERGY_SENSOR: BILL_IMPORT_SENSOR,
        CONF_SOLAR_SENSOR: BILL_SOLAR_SENSOR if facts.has_solar else None,
        CONF_GRID_EXPORT_SENSOR: BILL_EXPORT_SENSOR if facts.has_solar else None,
        CONF_IMPORT_PRICE_SENSOR: None,
        CONF_EXPORT_PRICE_SENSOR: None,

        CONF_STATE: facts.state,
        CONF_POSTCODE: facts.postcode,
        CONF_DISTRIBUTOR: facts.network,
        # Deliberately unset: a bill does not print the DNSP tariff code, and
        # the calculator's contract is "don't know, don't filter". Guessing one
        # would silently drop whole families of plans.
        CONF_NETWORK_TARIFF_CODES: "",

        CONF_HAS_BATTERY: False,
        CONF_BATTERY_SOC_SENSOR: None,
        CONF_BATTERY_CHARGE_POWER_SENSOR: None,

        CONF_HAS_DEMAND_TARIFF: facts.is_demand_bill,
        CONF_DEFERRABLE_LOAD_SENSORS: [],
        "current_plan": facts.plan_id,
    }

    # The controlled-load register, modelled as a declared ("dummy") deferrable
    # load — a load with an estimated daily kWh, no sensor and a CL register.
    # That machinery already exists for a genuine DNSP-switched circuit, which
    # is neither visible to nor controllable from HA, so the CL scenario needs
    # no new pricing path: `_compute_bill_items` itemises it at the plan's own
    # CL rate plus the register's daily supply charge, as its own bill line.
    if profiles.cl_on_own_register and profiles.cl_daily_kwh > 0:
        data.update({
            CONF_DEFERRABLE_LOAD_DUMMY_NAMES: ["Controlled load (from bill)"],
            CONF_DEFERRABLE_LOAD_DUMMY_KWH: [profiles.cl_daily_kwh],
            CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW: [answers.cl_max_kw or 0.0],
            CONF_DEFERRABLE_LOAD_DUMMY_HOURS: [""],
            CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD: [facts.cl_register],
            # False: this energy is on a separate register the synthesised
            # import series never saw, so there is nothing to subtract out of
            # general consumption. Setting it True would deduct it twice.
            CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE: [False],
        })

    # `bill_` not `bill:` — this id becomes part of a `Store` key, and a Store
    # key becomes a filename under .storage/.
    return SyntheticEntry(data=data, entry_id=f"bill_{bill_id}")


# ---------------------------------------------------------------------------
# Gates — plans a bill cannot price, and why
# ---------------------------------------------------------------------------

# A bill prints its rates to five or six decimals and our plan data is stored
# the same way, so these tolerances cover genuine rounding only — not "close
# enough". Whichever allowance is larger applies, so a tiny rate (a 3c feed-in)
# is not failed by a relative test and a large one (a $1.85/day supply charge)
# is not passed by an absolute one.
DRIFT_REL_TOL = 0.015       # 1.5% of the billed figure
DRIFT_ABS_TOL = 0.0015      # $/kWh or $/day

_DRIFT_LABELS = {
    "general_rate": ("general usage", "$/kWh"),
    "cl_rate": ("controlled load", "$/kWh"),
    "supply_per_day": ("daily supply", "$/day"),
    "cl_supply_per_day": ("controlled-load supply", "$/day"),
    "fit_rate": ("feed-in tariff", "$/kWh"),
}


def _plan_own_rates(plan, facts: BillFacts) -> dict:
    """Our stored rates for `plan`, in the shape `BillFacts.own_rates` returns.

    `general_rate` is only filled for a single-rate plan. A bill from a flat
    tariff prints one anytime rate, and comparing that against a TOU plan's
    peak/shoulder/off-peak tiers would be comparing a scalar with a schedule —
    the time-independent components (supply, controlled load, feed-in) are
    still directly comparable either way, so those are always returned.
    """
    imports = plan.get_import_rate_defs() or []
    exports = plan.get_export_rate_defs() or []
    cl = plan.get_controlled_load_rate(facts.cl_register) or {}
    single = len(imports) == 1
    return {
        "general_rate": (imports[0].get("rate") if single else None),
        "cl_rate": cl.get("rate"),
        "supply_per_day": getattr(plan, "daily_supply_charge", None),
        "cl_supply_per_day": cl.get("daily_supply_charge"),
        "fit_rate": (exports[0].get("rate") if len(exports) == 1 else None),
    }


def plan_data_drift(plan, facts: BillFacts) -> dict:
    """Our rates for the plan a bill is held on, against what it was charged.

    An uploaded bill is the only independent check we have on our own plan
    data. For every other plan in the ranking our rates are compared against
    nothing but themselves — PRD ingestion verifies them against PRD — but for
    the one plan the household actually holds, the bill prints the rates the
    retailer really applied. Spending that check is free and it is the only
    thing standing between a plan-data error and a confidently wrong ranking.

    Both sides are GST-inclusive: a retail bill is, and our plan data is
    stored that way too (see `BillFacts.own_rates`). The bill side is its
    *latest* rate period, not an average over the bill — our plan data holds
    one current rate set, and a bill spanning a price change prints two (see
    `BillFacts.latest_rates`).

    Returns `{}` when the two agree, or when there is nothing to compare.
    Otherwise `{component: {"ours", "billed", "pct"}}`, worst first.
    """
    billed = facts.latest_rates()
    ours = _plan_own_rates(plan, facts)
    out = {}
    for key, b in billed.items():
        o = ours.get(key)
        if b is None or o is None:
            continue
        # A zero on either side is a real difference in kind (a plan with no
        # feed-in vs a bill that was paid one), not a rounding question, so it
        # is reported rather than skipped — but it cannot be a percentage.
        allowed = max(abs(b) * DRIFT_REL_TOL, DRIFT_ABS_TOL)
        if abs(o - b) <= allowed:
            continue
        out[key] = {
            "ours": round(o, 6),
            "billed": round(b, 6),
            "pct": (None if not b else round((o - b) / b * 100.0, 1)),
        }
    return dict(sorted(out.items(),
                       key=lambda kv: -abs(kv[1]["pct"] or 0.0)))


def describe_drift(drift: dict) -> str:
    """One human sentence naming the worst components and both figures."""
    bits = []
    for key, d in list(drift.items())[:3]:
        label, unit = _DRIFT_LABELS.get(key, (key, ""))
        pct = "" if d["pct"] is None else f", {d['pct']:+.1f}%"
        bits.append(f"{label} {d['ours']:.5f} vs {d['billed']:.5f} {unit}{pct}")
    return "; ".join(bits)


@dataclass
class BillGates:
    """Decides which plans a bill-sourced comparison may rank.

    Every refusal is an explicit reason the UI shows, never an empty series. A
    plan quietly priced on missing data does not disappear from the ranking — it
    rises to the top of it.
    """

    facts: BillFacts
    allow_market_linked: bool = False
    assumed_cl_rate: float | None = None
    assumed_cl_supply: float = 0.0

    # Filled by `reason()` when the bill's own plan is checked, so the banner
    # can show the comparison whatever the outcome — an agreement is worth
    # stating too, because it is the product's only external evidence that its
    # plan data is right.
    own_plan_drift: dict = field(default_factory=dict)
    own_plan_checked: bool = False

    def reason(self, plan) -> str | None:
        """None = priceable. A string = refused, with this reason.

        Every reason reads "<short kind>: <detail>" so a UI can group refusals
        by kind without parsing the prose.
        """
        # Wrong distributor. A plan's rates bundle one DNSP's network charges,
        # so pricing an Endeavour household against an Ausgrid plan is simply
        # the wrong number — it is not conservative, and it can top the ranking.
        # Matched loosely both ways because the two sides are free text from
        # different sources ("Endeavour" vs "Endeavour Energy"); a blank on
        # either side means we do not know and so do not filter.
        want = (self.facts.network or "").strip().lower().replace("_", " ")
        got = (getattr(plan, "network", "") or "").strip().lower().replace("_", " ")
        if want and got and want not in got and got not in want:
            return (f"Other distributor: priced for {getattr(plan, 'network', '')} "
                    f"network charges, and this bill is on {self.facts.network}.")

        if not self.allow_market_linked and (
                getattr(plan, "is_market_linked", False)
                or getattr(plan, "spot_export_pricing", False)
                or getattr(plan, "has_spot_pricing", False)):
            return ("Market-linked: priced against AEMO spot prices, which a "
                    "bill does not carry. Historical spot data for the billed "
                    "period could unblock this later.")

        # A demand tariff bills the household's peak half-hour kW inside a
        # seasonal window. A bill from a non-demand customer never prints that
        # peak and it cannot be recovered from kWh totals — the whole point of a
        # demand charge is that it depends on a maximum, which summing destroys.
        # A demand bill DOES print the peak, so those plans stay priceable.
        if not self.facts.is_demand_bill and (
                getattr(plan, "demand_periods", None)
                or getattr(plan, "demand_charge_active", False)
                or getattr(plan, "demand_charge_per_kw_per_day", 0.0)):
            return ("Demand tariff: priced on peak kW, which this bill does "
                    "not print and kWh totals cannot recover.")

        # The plan the household actually holds, priced from plan data that
        # contradicts the bill in their hand. Refused rather than shown,
        # because this row is the one the user checks first and the one they
        # can least afford to be wrong: it is labelled as the plan they are on,
        # so a bad figure here reads as "Grid Lens thinks I pay $X" and silently
        # mis-states every saving measured against it. Which side is wrong is
        # genuinely open — our data may be a newer price set than the bill, or
        # the household may hold a legacy discount the retailer no longer
        # publishes under this plan's name — so the reason reports the
        # discrepancy and does not adjudicate it.
        if self.facts.plan_id and getattr(plan, "plan_id", "") == self.facts.plan_id:
            self.own_plan_checked = True
            drift = plan_data_drift(plan, self.facts)
            if drift:
                self.own_plan_drift = drift
                return ("Plan data disagrees with this bill: our rates for "
                        f"{self.facts.plan_name or 'this plan'} are not the "
                        "rates it was charged at — "
                        f"{describe_drift(drift)}. Both figures are GST-"
                        "inclusive, so this is a real difference and not a "
                        "tax or rounding artefact. Until it is resolved we "
                        "cannot price your current plan, and the baseline "
                        "below is the bill's own printed total instead.")

        if self.facts.has_controlled_load and self.assumed_cl_rate is None:
            if plan.get_controlled_load_rate(self.facts.cl_register) is None:
                pct = (self.facts.cl_kwh
                       / (self.facts.cl_kwh + self.facts.general_kwh) * 100.0)
                # Every reason starts "<short kind>: <detail>" so the UI can
                # group refusals by kind without parsing prose.
                return (f"No controlled-load rate: we hold no "
                        f"{self.facts.cl_register.replace('_', ' ')} rate for "
                        f"this plan, and {pct:.0f}% of this household's usage "
                        f"is on that register. Pricing it would silently drop "
                        f"that energy from the bill.")
        return None

    def cl_fallback(self) -> dict | None:
        """The assumed controlled-load rate, in the shape a plan's own returns.

        Handed to `PlanCalculator.cl_rate_fallback`, which uses it ONLY where a
        plan has no CL rate of its own and labels every line that used it. Must
        stay None unless the user explicitly opted in: a silently assumed rate
        on 56% of a household's usage is the single easiest way to produce a
        confident, wrong answer.
        """
        if self.assumed_cl_rate is None:
            return None
        return {
            self.facts.cl_register: {
                "rate": self.assumed_cl_rate,
                "daily_supply_charge": self.assumed_cl_supply,
                "assumed": True,
            }
        }


def gate_plans(plans: list, gates: BillGates) -> tuple:
    """Split plans into (priceable, {plan_id: reason})."""
    keep, refused = [], {}
    for plan in plans:
        why = gates.reason(plan)
        if why:
            refused[plan.plan_id] = why
        else:
            keep.append(plan)
    return keep, refused


# ---------------------------------------------------------------------------
# Assembling a run
# ---------------------------------------------------------------------------

@dataclass
class BillRun:
    """Everything one scenario's comparison needs, plus how to describe it."""

    facts: BillFacts
    answers: IntakeAnswers
    scenario: Scenario
    model: EnergyModel
    profiles: SynthesisedProfiles
    entry: SyntheticEntry
    source: BillUsageSource
    gates: BillGates

    @property
    def start(self) -> dt_mod.date:
        return self.facts.start_date

    @property
    def end(self) -> dt_mod.date:
        return self.facts.end_date

    def banner(self) -> dict:
        """The confidence banner's payload — what is assumed and how hard it is.

        `fidelity` is the headline:
          EXACT    arithmetic on the bill's own totals. A single-rate plan
                   charges the same c/kWh at every hour, so its bill depends
                   only on kWh and days, both printed. Switching between flat
                   plans is answered exactly.
          MODELLED depends on the reconstructed half-hourly shape: every
                   time-of-use plan and every battery or solar what-if.

        A bill-sourced run is MODELLED whenever anything about the shape can
        move the answer, which is almost always — the flag exists so the UI can
        stop showing a battery payback at the same visual weight as a flat-rate
        saving, which is the core presentational mistake to avoid.
        """
        check = self.facts.self_check()
        return {
            "source": "bill",
            "fidelity": "MODELLED",
            "retailer": self.facts.retailer,
            "plan_name": self.facts.plan_name,
            "network": self.facts.network,
            "state": self.facts.state,
            "days": self.facts.days,
            "period": f"{self.start.isoformat()} to "
                      f"{(self.end - dt_mod.timedelta(days=1)).isoformat()}",
            "billed_total": round(self.facts.billed_total(), 2),
            "printed_total": self.facts.printed_energy_total,
            "self_check": check.to_dict(),
            "scenario": {"id": self.scenario.id, "label": self.scenario.label,
                         "note": self.scenario.note,
                         "cl_placement": self.scenario.cl_placement},
            "model": self.model.summary(),
            "totals": {k: round(v, 1) for k, v in self.profiles.totals().items()},
            # Controlled load: whether the comparison is resting on an assumed
            # CL rate, and what it would be. `suggested_rate` is the rate this
            # household is already being charged — a number they can check on
            # their own bill — offered as the starting point if they choose to
            # turn the assumption on.
            "controlled_load": {
                "present": self.facts.has_controlled_load,
                "register": self.facts.cl_register,
                "daily_kwh": round(self.facts.cl_daily_kwh(), 2),
                "share_pct": (
                    None if not self.facts.has_controlled_load else
                    round(self.facts.cl_kwh
                          / (self.facts.cl_kwh + self.facts.general_kwh) * 100, 1)),
                "assumed_rate": self.answers.assume_cl_rate,
                "assumed_supply": self.answers.assume_cl_supply,
                "suggested_rate": self.facts.own_cl_rate(),
                "suggested_supply": round(self.facts.own_cl_supply(), 6),
            },
            # Our plan data against the rates this bill was actually charged
            # at — the one external check the product gets on its own plan
            # data. `checked` is False when the bill carries no plan_id (we
            # never identified which of our plans they hold) or when that plan
            # was not among the candidates, in which case no check happened and
            # the UI must not imply one passed.
            "plan_data_check": {
                "plan_id": self.facts.plan_id,
                "checked": self.gates.own_plan_checked,
                "agrees": (self.gates.own_plan_checked
                           and not self.gates.own_plan_drift),
                "drift": self.gates.own_plan_drift,
                # The bill's latest rate period — the basis `drift` is
                # measured on, so the two always agree. `own_rates()` is the
                # household's effective average over the whole bill and is a
                # different question; it is offered under controlled_load
                # above, where a suggested assumption genuinely wants it.
                "billed_rates": {k: (None if v is None else round(v, 6))
                                 for k, v in self.facts.latest_rates().items()},
                "basis": "latest rate period on the bill",
            },
            "one_season_caveat": (
                "This is one billing period, so every annual figure is that "
                "season's usage projected over a year. A winter quarter "
                "over-weights heating and under-weights solar; a battery sized "
                "on it alone will be wrong for summer. Add the other quarters' "
                "bills for a real annual answer."),
        }


def build_bill_run(real_entry, facts: BillFacts, answers: IntakeAnswers,
                   scenario_id: str | None, tz, bill_id: str,
                   model: EnergyModel | None = None) -> BillRun:
    """Calibrate (unless a cached model is supplied) and assemble one scenario.

    `model` lets a caller reuse a calibration across scenarios: calibration
    depends only on the bill and the intake answers, never on the CL placement,
    because controlled load is held out of the solve entirely (on its overnight
    window it cannot overlap solar, so it cannot affect either residual).
    """
    scenarios = scenarios_for(facts)
    chosen = next((s for s in scenarios if s.id == scenario_id), scenarios[0])
    model = model or calibrate(facts, answers)
    profiles = synthesise(model, answers, chosen)
    entry = build_synthetic_entry(real_entry, facts, answers, profiles, bill_id)
    return BillRun(
        facts=facts, answers=answers, scenario=chosen, model=model,
        profiles=profiles, entry=entry,
        source=BillUsageSource(profiles, tz),
        gates=BillGates(facts=facts, assumed_cl_rate=answers.assume_cl_rate,
                        assumed_cl_supply=answers.assume_cl_supply),
    )


def attach_to_calculator(calculator, run: BillRun) -> None:
    """Point a `PlanCalculator` at a bill run instead of the recorder."""
    calculator._usage_source = run.source
    calculator.plan_filter = run.gates.reason
    calculator.cl_rate_fallback = run.gates.cl_fallback()

    # The baseline is the bill, not a re-derivation of it. Two reasons: the
    # printed total is a fact and nothing we compute can beat it, and the bill's
    # own plan is routinely gated out (most plans hold no controlled-load rate),
    # which would otherwise leave every saving measured against a fallback.
    facts = run.facts
    supply = sum(p.days * (p.supply_per_day + p.cl_supply_per_day)
                 for p in facts.periods)
    calculator.baseline_total_override = facts.billed_total()
    calculator.baseline_energy_override = facts.billed_total() - supply
    calculator.baseline_label = (
        f"{facts.retailer} - {facts.plan_name} (as billed)".strip(" -"))
