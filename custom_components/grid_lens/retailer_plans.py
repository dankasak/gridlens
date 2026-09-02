"""Retailer plan classes — driven by API JSON data."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import Dict

_LOGGER = logging.getLogger(__name__)


def _format_clock(value) -> str:
    """'HH:MM'[:SS] -> 12-hour clock string, e.g. '14:00' -> '2pm', '00:30' ->
    '12:30am'. '24:00' folds to '12am' (end-of-day), matching how a customer
    would read "10pm-12am" on a real bill."""
    parts = str(value).split(":")
    h = int(parts[0]) % 24
    m = int(parts[1]) if len(parts) > 1 else 0
    period = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{period}" if m else f"{h12}{period}"


def _hour_ints_to_ranges(hours: list) -> list:
    """[14,15,16,19] -> [(14,17), (19,20)] — contiguous runs, end-exclusive."""
    if not hours:
        return []
    hours = sorted(set(hours))
    ranges = []
    start = prev = hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
            continue
        ranges.append((start, prev + 1))
        start = prev = h
    ranges.append((start, prev + 1))
    return ranges


def format_window_range(window: dict) -> str | None:
    """Human time-range for one window dict, e.g. {'start': '14:00', 'end':
    '20:00', 'days': 'weekdays'} -> '2pm-8pm (weekdays)'. Returns None for a
    window with no real time restriction (hours == 'all', the flat-rate case)
    so callers can tell "period-based" apart from "applies all day"."""
    start, end = window.get("start"), window.get("end")
    if start is not None and end is not None:
        rng = f"{_format_clock(start)}–{_format_clock(end)}"
    else:
        hours = window.get("hours", "all")
        if hours == "all" or not hours:
            return None
        rng = ", ".join(
            f"{_format_clock(f'{h0:02d}:00')}–{_format_clock(f'{h1:02d}:00')}"
            for h0, h1 in _hour_ints_to_ranges(list(hours))
        )
    days = window.get("days", "all")
    if days and days != "all":
        rng += f" ({days})"
    return rng


def _format_rate_time_range(rate_def: dict) -> str | None:
    """Combine every window on a rate definition into one display string
    (e.g. a Peak rate split into a morning and evening block), deduplicating
    identical formatted windows (a weekday/weekend pair with the same hours
    formats identically). None when the rate has no time restriction."""
    parts = []
    for w in rate_def.get("windows") or []:
        r = format_window_range(w)
        if r and r not in parts:
            parts.append(r)
    return "; ".join(parts) if parts else None


def rate_time_ranges(rate_defs: list) -> dict:
    """Map each declared rate value -> human time-range string, for
    period-based (TOU) rates only — flat all-hours rates are simply absent
    from the returned dict. Keyed by round(rate, 4) to match the rate_to_label
    keying used throughout _compute_bill_items.

    A capped rate's after-cap tier (rate_after_cap) shares its parent's
    windows — the cap only splits pricing by kWh threshold, not by time — so
    it's mapped to the same range string under its own rate-value key."""
    out = {}
    for rate_def in rate_defs or []:
        if rate_def.get("rate") is None:
            continue
        rng = _format_rate_time_range(rate_def)
        if rng:
            out[round(float(rate_def["rate"]), 4)] = rng
            if rate_def.get("rate_after_cap") is not None:
                out[round(float(rate_def["rate_after_cap"]), 4)] = rng
    return out


class RetailerPlan(ABC):
    """Base class for electricity retailer plans."""

    def __init__(self):
        self.retailer = ""
        self.plan_name = ""
        self.plan_id = ""
        self.daily_supply_charge = 0.0
        self.feed_in_tariff = 0.05
        self.demand_charge_per_kw_per_day = 0.0
        self.is_market_linked = False
        self.spot_export_pricing = False
        self.demand_charge_window = None
        self.fixed_daily_credit = 0.0
        self.monthly_subscription_fee = 0.0
        # Day-scoped bonus credits (e.g. GloBird ZEROHERO's "$1/day when imports
        # are 0.03 kWh/hour or less, 6pm-9pm"). Raw API shape: {label, condition,
        # threshold_kwh, amount_per_day, window}. Base plans have none.
        self._conditional_credits: list = []
        # Controlled Load rates: [{register, label, rate, note}], served under the
        # plan JSON's own "controlled_load_rates" key. Deliberately separate from
        # _import_rates — see PlanFromData.__init__ for why. Base plans have none.
        self._controlled_load_rates: list = []
        # PEA support: set by PlanFromData when plan JSON includes a "pea" block.
        self.aemo_price_sensor: str | None = None
        self.bpea: float = 0.017

    @abstractmethod
    def get_import_rate(self, dt: datetime) -> float:
        pass

    def get_export_rate(self, dt: datetime) -> float:
        return self.feed_in_tariff

    def get_import_rate_info(self, dt: datetime) -> Dict:
        """Rate plus daily-cap metadata for the matched window. Base plans have
        no cap concept; ``PlanFromData`` overrides this with the real lookup."""
        return {"rate": self.get_import_rate(dt), "label": None,
                "daily_cap_kwh": None, "rate_after_cap": None,
                "cap_period": "day", "cap_application": "strict"}

    def get_export_rate_info(self, dt: datetime) -> Dict:
        return {"rate": self.get_export_rate(dt), "label": None,
                "daily_cap_kwh": None, "rate_after_cap": None,
                "cap_period": "day", "cap_application": "strict"}

    @abstractmethod
    def describe_strategy(self) -> str:
        pass

    @abstractmethod
    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        pass

    def get_conditional_credits(self) -> list:
        return self._conditional_credits

    def get_export_rate_defs(self) -> list:
        """Raw export rate definitions (rate/label/daily_cap_kwh/rate_after_cap
        per tier), including tiers with zero export attributed to them so far.
        Base plans have none — used by bill-item labelling to name every
        declared FiT tier, not just the ones actual export data happened to
        touch (see _compute_bill_items's fit_lines)."""
        return []

    def get_import_rate_defs(self) -> list:
        """Raw import rate definitions (rate/label/windows per tier). Base
        plans have none — used by bill-item labelling to attach a time range
        to each TOU energy line (see _compute_bill_items's energy_lines)."""
        return []

    def get_controlled_load_rate(self, register: str) -> dict | None:
        """Simple linear lookup of this plan's rate for a CL register
        ('controlled_load_1' | 'controlled_load_2'), or None if the plan
        doesn't publish one. Data holder only — deliberately NOT wired into
        get_import_rate/_match_rate_def (see _controlled_load_rates above)."""
        for entry in self._controlled_load_rates:
            if entry.get("register") == register:
                return entry
        return None

    def get_plan_info(self) -> Dict:
        return {
            'id': self.plan_id,
            'retailer': self.retailer,
            'plan_name': self.plan_name,
            'daily_supply_charge': self.daily_supply_charge,
            'feed_in_tariff': self.feed_in_tariff,
        }


class PlanFromData(RetailerPlan):
    """Generic plan driven entirely by API JSON data.

    Interprets the standard rate-window structure served by /plans:
      import_rates / export_rates — list of {label, rate, windows:[{hours:[...]}]}
      charges — {daily_supply_charge, monthly_subscription}
      flags   — {is_market_linked, spot_export_pricing}
      vpp     — {monthly_credit} (fixed $/month credit, e.g. VPP participation)
      pea     — {bpea, aemo_sensor}  (Flow Power PEA; optional)
      strategy — string
    """

    def __init__(self, plan_data: dict) -> None:
        super().__init__()
        self.plan_id   = plan_data.get("id", "")
        self.retailer  = plan_data.get("retailer", "")
        self.plan_name = plan_data.get("name", "")

        charges = plan_data.get("charges", {})
        self.daily_supply_charge    = charges.get("daily_supply_charge", 0.0)
        self.monthly_subscription_fee = charges.get("monthly_subscription", 0.0)
        self.demand_charge_per_kw_per_day = charges.get("demand_charge_per_kw_per_day", 0.0)

        flags = plan_data.get("flags", {})
        self.is_market_linked    = flags.get("is_market_linked", False)
        self.spot_export_pricing = flags.get("spot_export_pricing", False)
        self.demand_charge_active = flags.get("demand_charge_active", False)

        # Optional demand-charge metering window. {"hours": [15,...] | "all",
        # "days": "all" | "weekdays" | "weekends", "label": "..."}. When absent,
        # the calculator falls back to DEFAULT_DEMAND_WINDOW_HOURS.
        self.demand_window = plan_data.get("demand_window") or None

        vpp = plan_data.get("vpp") or {}
        mc = vpp.get("monthly_credit", 0.0)
        self.fixed_daily_credit = mc / 30.44 if mc else 0.0

        pea = plan_data.get("pea") or {}
        if pea.get("aemo_sensor"):
            self.aemo_price_sensor = pea["aemo_sensor"]
        if "bpea" in pea:
            self.bpea = pea["bpea"]

        # Network (DNSP) tariff code(s) this plan is restricted to, e.g. "EA116" or
        # "EA116,EA030" — public catalogue data, not customer data. None means "no
        # restriction"; plan_calculator only filters when both this AND the
        # household's own configured code are set and don't intersect.
        eligibility = plan_data.get("eligibility") or {}
        self.required_network_tariff_codes = eligibility.get("required_network_tariff_codes")

        self._import_rates = plan_data.get("import_rates", [])
        self._export_rates = plan_data.get("export_rates", [])
        self._strategy     = plan_data.get("strategy", "")
        self._conditional_credits = plan_data.get("conditional_credits") or []

        # Controlled Load rates: served as their OWN top-level JSON key, deliberately
        # never merged into self._import_rates. _match_rate_def/get_import_rate walk a
        # single flat list with no register concept and no windowless-fallback path — a
        # CL rate living there would need its own all-hours window to be matchable at
        # all, and would silently win every general-usage lookup it sorts ahead of (see
        # PlanControlledLoadRate's docstring in the API's app/plan_models.py, and
        # VPP_AND_CONTROLLED_LOAD_DESIGN.md Part B, for the full bug-risk rationale).
        # Parsing/holding only for now — no rate-matching or bill-splitting logic here;
        # that's staged for plan_calculator.py once a CL-bearing plan exists.
        self._controlled_load_rates = plan_data.get("controlled_load_rates") or []

        # Default feed_in_tariff: first non-null export rate that applies all hours.
        for r in self._export_rates:
            rate = r.get("rate")
            if rate is not None:
                windows = r.get("windows", [])
                if windows and windows[0].get("hours") == "all":
                    self.feed_in_tariff = float(rate)
                    break
                elif not windows:
                    self.feed_in_tariff = float(rate)
                    break

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _time_to_min(value) -> int:
        """'HH:MM'[:SS] → minutes since midnight. '24:00' → 1440."""
        parts = str(value).split(":")
        return int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)

    @classmethod
    def _in_time_range(cls, start, end, minute_of_day: int) -> bool:
        """End-exclusive minute-of-day membership; wraps midnight when end <= start."""
        s = cls._time_to_min(start)
        e = cls._time_to_min(end)
        if e <= s:  # wraps past midnight, e.g. 22:00 → 06:00
            return minute_of_day >= s or minute_of_day < e
        return s <= minute_of_day < e

    @staticmethod
    def _in_season(window: dict, dt: datetime) -> bool:
        """Seasonal windows carry {"season": {"start": "MM-DD", "end": "MM-DD"}}
        (inclusive both ends, wrapping the new year, e.g. 11-01..03-31).
        No season key = year-round."""
        season = window.get("season")
        if not season:
            return True
        start, end = season.get("start"), season.get("end")
        if not start or not end:
            return True
        probe = f"{dt.month:02d}-{dt.day:02d}"
        if start <= end:
            return start <= probe <= end
        return probe >= start or probe <= end

    @staticmethod
    def _in_window(window: dict, dt: datetime) -> bool:
        """Date-aware window membership — no instance state, so callable as
        ``PlanFromData._in_window(...)`` for plans/objects that carry a raw
        window dict but aren't necessarily a PlanFromData (e.g. conditional
        credits matched from ``build_conditional_credits``)."""
        if not PlanFromData._in_season(window, dt):
            return False
        # Sub-hour time range (from the DB's true TIME range) takes precedence.
        start, end = window.get("start"), window.get("end")
        if start is not None and end is not None:
            return PlanFromData._in_time_range(start, end, dt.hour * 60 + dt.minute)
        hours = window.get("hours", "all")
        if hours == "all":
            return True
        return dt.hour in hours

    def _in_window_hour(self, window: dict, hour: int) -> bool:
        # For hour-granular display/labels: a time-range window counts for an hour if
        # it overlaps any part of that hour. No date is available here, so seasonal
        # windows count when their season contains TODAY (display approximation;
        # all pricing paths go through the fully date-aware _in_window instead).
        if not self._in_season(window, datetime.now()):
            return False
        start, end = window.get("start"), window.get("end")
        if start is not None and end is not None:
            return any(
                self._in_time_range(start, end, hour * 60 + m) for m in (0, 30, 59)
            )
        hours = window.get("hours", "all")
        if hours == "all":
            return True
        return hour in hours

    def _match_rate_def(self, rates: list, dt: datetime) -> dict | None:
        for rate_def in rates:
            if rate_def.get("rate") is None:
                continue
            for window in rate_def.get("windows", []):
                if self._in_window(window, dt):
                    return rate_def
        return None

    def _match_rate(self, rates: list, dt: datetime) -> float:
        rate_def = self._match_rate_def(rates, dt)
        return float(rate_def["rate"]) if rate_def is not None else 0.0

    def _rate_info(self, rates: list, dt: datetime) -> Dict:
        rate_def = self._match_rate_def(rates, dt)
        if rate_def is None:
            return {"rate": 0.0, "label": None, "daily_cap_kwh": None,
                    "rate_after_cap": None, "cap_period": "day",
                    "cap_application": "strict"}
        return {
            "rate": float(rate_def["rate"]),
            "label": rate_def.get("label"),
            "daily_cap_kwh": rate_def.get("daily_cap_kwh"),
            "rate_after_cap": rate_def.get("rate_after_cap"),
            # The API omits these when they are at their defaults, so every
            # existing plan's JSON is unchanged — hence the fallbacks rather
            # than a bare .get(). "strict" = a hard limit inside each period;
            # "pooled" = the allowance accrues across the billing period, so
            # unused headroom banks (GloBird's step rates, EnergyAustralia's
            # Solar Sharer). See plan_rates.cap_period / cap_application.
            "cap_period": rate_def.get("cap_period") or "day",
            "cap_application": rate_def.get("cap_application") or "strict",
        }

    def _rate_label_for_hour(self, hour: int) -> str:
        for rate_def in self._import_rates:
            for window in rate_def.get("windows", []):
                if self._in_window_hour(window, hour):
                    return rate_def.get("label", "Energy")
        return "Energy"

    # ── RetailerPlan interface ────────────────────────────────────────────────

    def get_import_rate(self, dt: datetime) -> float:
        return self._match_rate(self._import_rates, dt)

    def get_export_rate(self, dt: datetime) -> float:
        return self._match_rate(self._export_rates, dt)

    def get_import_rate_info(self, dt: datetime) -> Dict:
        return self._rate_info(self._import_rates, dt)

    def get_export_rate_info(self, dt: datetime) -> Dict:
        return self._rate_info(self._export_rates, dt)

    def get_export_rate_defs(self) -> list:
        return list(self._export_rates)

    def get_import_rate_defs(self) -> list:
        return list(self._import_rates)

    def describe_strategy(self) -> str:
        return self._strategy

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get("schedule", [])
        days = len(schedule) / 24 if schedule else 30

        buckets: dict = {}
        export_kwh = export_credit = 0.0

        for slot in schedule:
            h   = slot.get("hour", 0) % 24
            imp = slot.get("import_kwh", 0.0)
            ic  = slot.get("import_cost", 0.0)
            exp = slot.get("export_kwh", 0.0)
            ec  = slot.get("export_credit", 0.0)
            export_kwh    += exp
            export_credit += ec
            label = self._rate_label_for_hour(h)
            if label not in buckets:
                buckets[label] = {"kwh": 0.0, "cost": 0.0}
            buckets[label]["kwh"]  += imp
            buckets[label]["cost"] += ic

        # Nominal rate per label, used below when a bucket has zero kWh (e.g. a
        # dummy all-zero schedule probing for labels) — cost/kwh would otherwise
        # collapse to 0.0 and erase which tier (peak/off-peak/free window/etc)
        # the label actually belongs to.
        label_rates = {}
        for rate_def in self._import_rates:
            lbl = rate_def.get("label", "Energy")
            label_rates.setdefault(lbl, float(rate_def.get("rate") or 0))

        sections = []
        for label, b in buckets.items():
            kwh, cost = b["kwh"], b["cost"]
            rate = round(cost / kwh, 4) if kwh > 0 else label_rates.get(label, 0.0)
            sections.append({
                "title": label,
                "kwh":  round(kwh, 2),
                "rate": rate,
                "cost": round(cost, 2),
            })

        # Inject zero-kwh entries for rate tiers not hit in this schedule —
        # needed so _compute_bill_items can build its rate→label mapping.
        known_labels = {s["title"] for s in sections}
        for rate_def in self._import_rates:
            lbl = rate_def.get("label", "Energy")
            if lbl not in known_labels:
                sections.append({
                    "title": lbl,
                    "kwh":  0.0,
                    "rate": float(rate_def.get("rate") or 0),
                    "cost": 0.0,
                })

        if export_kwh > 0:
            sections.append({
                "title": "Solar Export (Credit)",
                "kwh":  round(export_kwh, 2),
                "rate": round(export_credit / export_kwh, 4),
                "cost": round(-export_credit, 2),
            })

        supply = self.daily_supply_charge * days
        sub    = self.monthly_subscription_fee * (days / 30.44)
        credit = self.fixed_daily_credit * days
        conditional = optimization_result.get("conditional_credits") or {}
        conditional_total = sum(c.get("amount", 0.0) for c in conditional.values())
        energy = optimization_result.get("net_cost", 0.0)
        total  = energy + supply + sub - credit - conditional_total

        result: dict = {
            "sections":          sections,
            "total_energy_cost": round(energy, 2),
            "supply_charge":     round(supply, 2),
            "total":             round(total, 2),
            "days":              round(days, 1),
        }
        if sub:
            result["subscription_fee"] = round(sub, 2)
        if credit:
            result["vpp_credit"] = round(credit, 2)
        if conditional_total:
            # Per-credit detail (days earned vs. days in the schedule), e.g.
            # "ZEROHERO Credit: $3.00 (3/3 days)" — not just the total, so a
            # day the LP failed to earn the credit is visible, not hidden.
            result["conditional_credits"] = {
                label: {
                    "amount": round(c.get("amount", 0.0), 2),
                    "days_earned": c.get("days_earned", 0),
                    "days_total": c.get("days_total", 0),
                }
                for label, c in conditional.items()
            }
        return result


def _prepare_plan_data(plan_id: str, plan_data: dict,
                       network_operators: dict | None) -> dict:
    """Copy plan JSON with id defaulted and network-operator demand/controlled-load
    data merged in, only where the plan JSON doesn't already carry the fields.

    Demand-charge fields are merged only for plans with demand_charge_active (a
    plan-level flag). controlled_load has no plan-level flag equivalent — whether
    it applies is gated by household config (CONF_HAS_CONTROLLED_LOAD_1/_2), not
    anything the plan JSON declares — so it's merged unconditionally whenever the
    network operator publishes one, same "only if not already present" behaviour.
    """
    data = dict(plan_data)
    data.setdefault("id", plan_id)
    if network_operators:
        network_key = data.get("network", "").lower()
        operator = network_operators.get(network_key) or {}
        if operator and data.get("flags", {}).get("demand_charge_active"):
            data.setdefault("charges", {})
            if "demand_charge_per_kw_per_day" not in data["charges"]:
                data["charges"]["demand_charge_per_kw_per_day"] = operator.get("demand_charge_per_kw_per_day", 0.0)
            if "demand_window" not in data:
                data["demand_window"] = operator.get("demand_window")
        if operator and "controlled_load" not in data:
            controlled_load = operator.get("controlled_load")
            if controlled_load:
                data["controlled_load"] = controlled_load
    return data


def plans_from_api_data(plan_dict: dict, network_operators: dict | None = None) -> list[RetailerPlan]:
    """Create RetailerPlan objects from the /plans API response dict.

    plan_dict is the 'plans' value from the API response:
        {plan_id: plan_data, ...}

    network_operators is the 'network_operators' value from the API response:
        {operator_key: operator_data, ...}

    For plans with demand_charge_active=true, merges demand data from the network operator registry.

    Tier enforcement is done by the API — free tier returns only the locked plan,
    paid tier returns all plans.
    """
    return [PlanFromData(_prepare_plan_data(pid, pdata, network_operators))
            for pid, pdata in plan_dict.items()]


class VersionedPlan(RetailerPlan):
    """A plan with temporal versions (/plans/history): routes every
    date-sensitive rate lookup to the version in force at that instant, so a
    billing period spanning a retailer price change is priced correctly.

    Fixed per-day charges (supply, subscription, VPP credit) are exposed as
    period-weighted averages over the analysis window, so the calculator's
    ``daily_supply_charge * actual_days`` sites produce the exact
    across-versions total without modification. All other attributes mirror the
    latest version.
    """

    _TZ = None  # lazily resolved Australia/Sydney (effective dates are local)

    def __init__(self, segments: list[tuple[str | None, str | None, "PlanFromData"]],
                 period_start: datetime, period_end: datetime) -> None:
        # NOTE deliberately no super().__init__() — attributes are copied from
        # the newest version below, then fixed charges are re-weighted.
        parsed = [
            (date.fromisoformat(f) if f else None,
             date.fromisoformat(t) if t else None, p)
            for f, t, p in segments
        ]
        latest = parsed[-1][2]
        self.__dict__.update(latest.__dict__)
        self._segments = parsed
        self._latest = latest

        ps, pe = self._local_date(period_start), self._local_date(period_end)
        total_days = max((pe - ps).days, 1)
        for attr in ("daily_supply_charge", "monthly_subscription_fee",
                     "fixed_daily_credit"):
            weighted = 0.0
            for i, (eff_from, eff_to, p) in enumerate(self._segments):
                # Days before the first version are priced by the oldest
                # version (see _plan_at), so weight them to it as well.
                seg_start = ps if (i == 0 or not eff_from) else max(ps, eff_from)
                seg_end = min(pe, eff_to) if eff_to else pe
                overlap = max((seg_end - seg_start).days, 0)
                weighted += (getattr(p, attr, 0.0) or 0.0) * overlap
            self.__dict__[attr] = weighted / total_days

    @classmethod
    def _local_date(cls, dt: datetime) -> date:
        if cls._TZ is None:
            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo
            cls._TZ = ZoneInfo("Australia/Sydney")
        return dt.astimezone(cls._TZ).date() if dt.tzinfo else dt.date()

    def _plan_at(self, dt: datetime) -> "PlanFromData":
        d = self._local_date(dt)
        for eff_from, eff_to, p in self._segments:
            if (eff_from is None or d >= eff_from) and (eff_to is None or d < eff_to):
                return p
        # Before the first version's effectivity: the oldest version is the
        # best available stand-in; after the last: the latest.
        return self._segments[0][2] if (self._segments[0][0]
                                        and d < self._segments[0][0]) else self._latest

    def get_import_rate(self, dt: datetime) -> float:
        return self._plan_at(dt).get_import_rate(dt)

    def get_export_rate(self, dt: datetime) -> float:
        return self._plan_at(dt).get_export_rate(dt)

    def get_import_rate_info(self, dt: datetime) -> Dict:
        return self._plan_at(dt).get_import_rate_info(dt)

    def get_export_rate_info(self, dt: datetime) -> Dict:
        return self._plan_at(dt).get_export_rate_info(dt)

    def get_export_rate_defs(self) -> list:
        return self._latest.get_export_rate_defs()

    def get_import_rate_defs(self) -> list:
        return self._latest.get_import_rate_defs()

    def describe_strategy(self) -> str:
        return self._latest.describe_strategy()

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        return self._latest.get_display_breakdown(optimization_result)


def versioned_plans_from_history(plan_dict: dict, history: dict,
                                 network_operators: dict | None,
                                 period_start: datetime,
                                 period_end: datetime) -> list[RetailerPlan]:
    """Like plans_from_api_data, but plans with more than one version
    overlapping the period become VersionedPlan wrappers built from the
    /plans/history payload ({plan_id: [{effective_from, effective_to, plan}]}).
    Plans absent from the history payload fall back to their current data.

    A single overlapping version is NOT the same as "use current data": /plans/
    history only returns versions that actually overlap [period_start,
    period_end], so when a plan has rate history but the requested period falls
    entirely inside one *older* segment, that segment is correctly the only
    entry — and it must still be used verbatim, not swapped for plan_dict's
    current/live snapshot (a different, later version). Using current data here
    silently re-priced any wholly-historical period with today's rates whenever
    exactly one historical segment matched — caught 2026-08-31 when a plan with
    a 22 Jul rate change still showed the post-22-Jul rate for a 12-20 Jul bill.
    """
    result: list[RetailerPlan] = []
    for plan_id, plan_data in plan_dict.items():
        versions = (history or {}).get(plan_id) or []
        if not versions:
            result.append(PlanFromData(
                _prepare_plan_data(plan_id, plan_data, network_operators)))
            continue
        if len(versions) == 1:
            result.append(PlanFromData(
                _prepare_plan_data(plan_id, versions[0]["plan"], network_operators)))
            continue
        segments = [
            (v.get("effective_from"), v.get("effective_to"),
             PlanFromData(_prepare_plan_data(plan_id, v["plan"], network_operators)))
            for v in versions
        ]
        result.append(VersionedPlan(segments, period_start, period_end))
    return result


def build_rate_caps(
    plan: RetailerPlan, start: datetime, n_slots: int, slot_minutes: int = 60,
) -> tuple[list[Dict], list[Dict], Dict]:
    """Build BatteryOptimizer.optimize_hourly_schedule's import_caps/export_caps
    hour-mask descriptors from a plan's per-slot rate lookup, grouping slots by rate
    label so multiple slots sharing the same capped rate definition (e.g. every hour
    of GloBird ZEROHERO's daily free-import window) share one daily_cap_kwh/
    rate_after_cap budget rather than each getting its own.

    Also returns cap_labels: {round(rate, 4): "<label> (first N kWh/day)",
    round(rate_after_cap, 4): "<label> (after N kWh/day)"} for callers building a
    cost breakdown by rate value that want distinct, unambiguous labels for the
    free and post-cap tiers instead of two rows that otherwise look identical —
    mirrors PlanCalculator._split_capped_kwh's labelling for the actual-usage
    bill-reporting path.

    Returns ([], [], {}) for a plan with no capped rates (the common case) — the
    optimizer then behaves exactly as it did before caps existed.

    ``start`` is added to in its original tz (usually UTC) and only converted to
    Australia/Sydney per resulting instant — matching PlanCalculator's rate-window
    lookups elsewhere — because converting once up front and then adding hours to
    an already-localized datetime does not correctly track DST transitions.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Sydney")

    cap_labels: Dict = {}

    def _build(get_info) -> list[Dict]:
        groups: Dict[str, Dict] = {}
        for t in range(n_slots):
            dt = (start + timedelta(minutes=t * slot_minutes)).astimezone(tz)
            info = get_info(dt)
            cap = info.get("daily_cap_kwh")
            after = info.get("rate_after_cap")
            if not cap or after is None:
                continue
            label = info.get("label") or "Energy"
            group = groups.setdefault(label, {
                "daily_cap_kwh": cap, "rate_after_cap": after,
                # Carried through so the LP can widen a pooled cap from one row
                # per calendar day to one row across the horizon.
                "cap_period": info.get("cap_period") or "day",
                "cap_application": info.get("cap_application") or "strict",
                "hour_mask": [0] * n_slots,
            })
            group["hour_mask"][t] = 1
            _unit = {"day": "kWh/day", "week": "kWh/week", "month": "kWh/month",
                     "quarter": "kWh/quarter", "year": "kWh/year",
                     "billing_period": "kWh/bill"}.get(
                         info.get("cap_period") or "day", "kWh/day")
            _avg = " avg" if (info.get("cap_application") == "pooled") else ""
            cap_labels.setdefault(round(info["rate"], 4),
                                  f"{label} (first {cap:g} {_unit}{_avg})")
            cap_labels.setdefault(round(after, 4),
                                  f"{label} (after {cap:g} {_unit}{_avg})")
        return list(groups.values())

    import_caps = _build(plan.get_import_rate_info)
    export_caps = _build(plan.get_export_rate_info)
    return import_caps, export_caps, cap_labels


def build_conditional_credits(
    plan: RetailerPlan, start: datetime, n_slots: int, slot_minutes: int = 60,
) -> list[Dict]:
    """Build BatteryOptimizer.optimize_hourly_schedule's conditional_credits
    hour-mask descriptors from a plan's raw conditional-credit definitions —
    day-scoped all-or-nothing bonuses like GloBird ZEROHERO's "$1/day when
    imports are 0.03 kWh/hour or less, 6pm-9pm" (see PlanConditionalCredit in
    the API's plan_models.py). Mirrors build_rate_caps's shape/grouping
    approach, one entry per credit rather than grouped by label since each
    credit already has exactly one window.

    Returns [] for a plan with none (the common case).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Sydney")

    out: list[Dict] = []
    for credit in plan.get_conditional_credits():
        window = credit.get("window") or {}
        mask = [0] * n_slots
        # Real calendar-date ordinal per masked slot (-1 = unmasked), NOT
        # t // slots_per_day: the LP horizon starts at "now" rather than local
        # midnight, so a fixed-width slots_per_day chunk can land mid-window
        # (e.g. horizon starting 7pm would chunk-boundary at 7pm the next day,
        # splitting a 6-9pm window in two) — which would double the $1/day
        # credit across two binaries for what's really one calendar day. Real
        # dates group correctly regardless of what time the plan happens to run.
        day_index = [-1] * n_slots
        for t in range(n_slots):
            dt = (start + timedelta(minutes=t * slot_minutes)).astimezone(tz)
            if PlanFromData._in_window(window, dt):
                mask[t] = 1
                day_index[t] = dt.toordinal()
        if any(mask):
            out.append({
                "label": credit.get("label", "Conditional Credit"),
                "condition": credit.get("condition", "max_import_kwh"),
                "threshold_kwh": float(credit.get("threshold_kwh") or 0.0),
                "amount_per_day": float(credit.get("amount_per_day") or 0.0),
                "hour_mask": mask,
                "day_index": day_index,
            })
    return out


class VppProgramFromData:
    """Data holder for a VPP bolt-on program (e.g. AGL "Bring Your Own Battery",
    Diamond WATTBANK) — a retailer-level credit program layered on top of
    whatever plan the household is already on, independent of CONF_CURRENT_PLAN.
    Parsed from one entry of the /vpp-programs API response's "programs" dict
    (see app/plan_transform.py's serialize_vpp_program_ir for the exact JSON
    shape this mirrors).

    Not wired into any bill total yet — see VPP_AND_CONTROLLED_LOAD_DESIGN.md
    Part A for the eventual calculation ("flat_credit is contracted, add to the
    total; dispatch_credit is not knowable ahead of time, surface as a separate
    'potential additional credit' line instead"). This class only holds and
    exposes the data.
    """

    def __init__(self, program_id: str, data: dict) -> None:
        self.id = data.get("id", program_id)
        self.name = data.get("name", "")
        self.retailer = data.get("retailer", "")
        self.state = data.get("state", "")
        self.signup_bonus = data.get("signup_bonus")            # one-off $, or None
        self.establishment_fee = data.get("establishment_fee")  # one-off $, or None
        self.flat_credit = data.get("flat_credit")               # {amount, period} | None
        self.dispatch_credit = data.get("dispatch_credit")       # {rate_per_kwh, direction,
                                                                  #  annual_cap_kwh, predictable,
                                                                  #  window} | None
        self.contract_type = data.get("contract_type")
        self.battery_eligibility_note = data.get("battery_eligibility_note")
        # [] = unrestricted (any plan from this retailer qualifies) — see
        # VppProgramEligiblePlan's docstring in the API's app/plan_models.py.
        self.eligible_plans = list(data.get("eligible_plans") or [])
        self.notes = data.get("notes")
        self.source_url = data.get("source_url")
        self.last_verified = data.get("last_verified")

    def applies_to_plan(self, plan_slug: str) -> bool:
        """True if this program is unrestricted, or plan_slug is one of its
        eligible_plans."""
        return not self.eligible_plans or plan_slug in self.eligible_plans


def vpp_programs_from_api_data(response: dict) -> dict[str, VppProgramFromData]:
    """Parse a GET /vpp-programs response ({"state": ..., "programs":
    {slug: program_json}}) into {slug: VppProgramFromData}.

    Data holder only — nothing here computes a bill total; plan_calculator.py
    and battery_optimizer.py don't consume this yet (see
    VPP_AND_CONTROLLED_LOAD_DESIGN.md Part A).
    """
    programs = response.get("programs") or {}
    return {slug: VppProgramFromData(slug, data) for slug, data in programs.items()}
