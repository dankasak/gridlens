"""Retailer plan classes — driven by API JSON data."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import Dict

_LOGGER = logging.getLogger(__name__)


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
                "daily_cap_kwh": None, "rate_after_cap": None}

    def get_export_rate_info(self, dt: datetime) -> Dict:
        return {"rate": self.get_export_rate(dt), "label": None,
                "daily_cap_kwh": None, "rate_after_cap": None}

    @abstractmethod
    def describe_strategy(self) -> str:
        pass

    @abstractmethod
    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        pass

    def get_conditional_credits(self) -> list:
        return self._conditional_credits

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

        self._import_rates = plan_data.get("import_rates", [])
        self._export_rates = plan_data.get("export_rates", [])
        self._strategy     = plan_data.get("strategy", "")
        self._conditional_credits = plan_data.get("conditional_credits") or []

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
            return {"rate": 0.0, "label": None, "daily_cap_kwh": None, "rate_after_cap": None}
        return {
            "rate": float(rate_def["rate"]),
            "label": rate_def.get("label"),
            "daily_cap_kwh": rate_def.get("daily_cap_kwh"),
            "rate_after_cap": rate_def.get("rate_after_cap"),
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

        sections = []
        for label, b in buckets.items():
            kwh, cost = b["kwh"], b["cost"]
            sections.append({
                "title": label,
                "kwh":  round(kwh, 2),
                "rate": round(cost / kwh, 4) if kwh > 0 else 0.0,
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
    """Copy plan JSON with id defaulted and network-operator demand data merged
    (only for plans with demand_charge_active, only where the plan JSON doesn't
    already carry the fields)."""
    data = dict(plan_data)
    data.setdefault("id", plan_id)
    if network_operators and data.get("flags", {}).get("demand_charge_active"):
        network_key = data.get("network", "").lower()
        operator = network_operators.get(network_key) or {}
        if operator:
            data.setdefault("charges", {})
            if "demand_charge_per_kw_per_day" not in data["charges"]:
                data["charges"]["demand_charge_per_kw_per_day"] = operator.get("demand_charge_per_kw_per_day", 0.0)
            if "demand_window" not in data:
                data["demand_window"] = operator.get("demand_window")
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
    """
    result: list[RetailerPlan] = []
    for plan_id, plan_data in plan_dict.items():
        versions = (history or {}).get(plan_id) or []
        if len(versions) <= 1:
            result.append(PlanFromData(
                _prepare_plan_data(plan_id, plan_data, network_operators)))
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

    Also returns cap_labels: {round(rate_after_cap, 4): "<label> (over cap)"} for
    callers building a cost breakdown by rate value that want a real label for the
    post-cap tier instead of a generic one — mirrors PlanCalculator._split_capped_kwh's
    labelling for the actual-usage bill-reporting path.

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
                "hour_mask": [0] * n_slots,
            })
            group["hour_mask"][t] = 1
            cap_labels.setdefault(round(after, 4), f"{label} (over cap)")
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
