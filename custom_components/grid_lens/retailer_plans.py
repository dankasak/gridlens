"""Retailer plan classes — driven by API JSON data."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
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
        # PEA support: set by PlanFromData when plan JSON includes a "pea" block.
        self.aemo_price_sensor: str | None = None
        self.bpea: float = 0.017

    @abstractmethod
    def get_import_rate(self, dt: datetime) -> float:
        pass

    def get_export_rate(self, dt: datetime) -> float:
        return self.feed_in_tariff

    @abstractmethod
    def describe_strategy(self) -> str:
        pass

    @abstractmethod
    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        pass

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

    def _in_window(self, window: dict, dt: datetime) -> bool:
        # Sub-hour time range (from the DB's true TIME range) takes precedence.
        start, end = window.get("start"), window.get("end")
        if start is not None and end is not None:
            return self._in_time_range(start, end, dt.hour * 60 + dt.minute)
        hours = window.get("hours", "all")
        if hours == "all":
            return True
        return dt.hour in hours

    def _in_window_hour(self, window: dict, hour: int) -> bool:
        # For hour-granular display/labels: a time-range window counts for an hour if
        # it overlaps any part of that hour.
        start, end = window.get("start"), window.get("end")
        if start is not None and end is not None:
            return any(
                self._in_time_range(start, end, hour * 60 + m) for m in (0, 30, 59)
            )
        hours = window.get("hours", "all")
        if hours == "all":
            return True
        return hour in hours

    def _match_rate(self, rates: list, dt: datetime) -> float:
        for rate_def in rates:
            rate = rate_def.get("rate")
            if rate is None:
                continue
            for window in rate_def.get("windows", []):
                if self._in_window(window, dt):
                    return float(rate)
        return 0.0

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
        energy = optimization_result.get("net_cost", 0.0)
        total  = energy + supply + sub - credit

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
        return result


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
    result = []
    for plan_id, plan_data in plan_dict.items():
        data = dict(plan_data)
        data.setdefault("id", plan_id)

        # Merge network operator demand data if retailer passes it through
        if network_operators and data.get("flags", {}).get("demand_charge_active"):
            network_key = data.get("network", "").lower()
            operator = network_operators.get(network_key) or {}
            if operator:
                data.setdefault("charges", {})
                # Only inject operator data if not already in the plan JSON
                if "demand_charge_per_kw_per_day" not in data["charges"]:
                    data["charges"]["demand_charge_per_kw_per_day"] = operator.get("demand_charge_per_kw_per_day", 0.0)
                if "demand_window" not in data:
                    data["demand_window"] = operator.get("demand_window")

        result.append(PlanFromData(data))
    return result
