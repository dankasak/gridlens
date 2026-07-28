"""Advisory-mode data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ForecastBundle:
    """Hour-aligned forecast inputs for the optimizer, all length ``hours``.

    ``start`` is the top of the first hour (tz-aware). ``solar_kwh`` and ``load_kwh``
    are per-hour energy; ``import_rate``/``export_rate`` are $/kWh for that hour.
    """

    start: datetime
    solar_kwh: list[float]
    load_kwh: list[float]
    import_rate: list[float]
    export_rate: list[float]
    slot_minutes: int = 60
    sources: dict[str, Any] = field(default_factory=dict)

    @property
    def slots(self) -> int:
        return min(
            len(self.solar_kwh),
            len(self.load_kwh),
            len(self.import_rate),
            len(self.export_rate),
        )

    @property
    def dt_hours(self) -> float:
        return self.slot_minutes / 60.0

    # Back-compat alias (was hourly-only).
    @property
    def hours(self) -> int:
        return self.slots


@dataclass
class AdvisoryResult:
    """Output of one advisory optimization run — a plan plus a SOC trajectory.

    In advisory mode this is *published only* (no battery writes). The ``plan`` can
    later be handed to :class:`ScheduleExecutor` once control is enabled.
    """

    generated_at: datetime
    start: datetime
    horizon_hours: int
    initial_soc_percent: float
    final_soc_percent: float
    net_cost: float
    solver: str
    plan: list  # list[DispatchInterval]
    trajectory: list[dict]  # per-hour {start, soc_percent, action, power_w, ...}
    deferrable_names: list[str] = field(default_factory=list)
    # Each device's rated power (kW) — lets a consumer (e.g. the advisory card) judge
    # whether a slot's continuous def_i kWh output amounts to a real "recommended on"
    # for a device that's physically only ever fully-on or off (an EV charger, a pool
    # pump), rather than guessing from the raw kWh figure alone.
    deferrable_max_kw: list[float] = field(default_factory=list)
    # Same order as deferrable_names/deferrable_max_kw — each device's configured energy
    # sensor entity_id (plan_calculator._get_deferrable_data's 'sensor_id'). deferrable_names
    # is a cleaned-up *display* string (raw friendly_name, not run through the same
    # suffix-trimming/dashboard-rename logic sensor.py's `deferrable_loads` attribute uses),
    # so two different GridLens sensors can legitimately show different names for the same
    # device. This id is the only reliable join key a card has for matching "this trajectory
    # slot" to "this device's real-time power sensor" — see _deferPowerEntities() in
    # grid-lens-power-chart-card.js.
    deferrable_sensor_ids: list[str] = field(default_factory=list)
    # {label: {days_earned, days_total, amount, amount_per_day}} — e.g. GloBird
    # ZEROHERO's credit. Empty for plans without a conditional credit.
    conditional_credits: dict = field(default_factory=dict)
    # The optimizer's own battery power limits (kW), straight from this install's config
    # entry (CONF_BATTERY_MAX_CHARGE_RATE/CONF_BATTERY_MAX_DISCHARGE_RATE) — brand-agnostic,
    # since it's whatever the user entered during config_flow, not a vendor-specific
    # register. Lets a consumer (e.g. the power flow card) size things against this
    # install's actual battery ceiling without hardcoding any inverter brand's entities.
    battery_max_charge_kw: float = 0.0
    battery_max_discharge_kw: float = 0.0

    def to_attributes(self) -> dict[str, Any]:
        """Shape for a HA sensor's attributes (JSON-serialisable)."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "start": self.start.isoformat(),
            "horizon_hours": self.horizon_hours,
            "initial_soc_percent": round(self.initial_soc_percent, 1),
            "final_soc_percent": round(self.final_soc_percent, 1),
            "net_cost": round(self.net_cost, 4),
            "solver": self.solver,
            "deferrable_names": self.deferrable_names,
            "deferrable_max_kw": self.deferrable_max_kw,
            "deferrable_sensor_ids": self.deferrable_sensor_ids,
            "trajectory": self.trajectory,
            "conditional_credits": self.conditional_credits,
            "battery_max_charge_kw": self.battery_max_charge_kw,
            "battery_max_discharge_kw": self.battery_max_discharge_kw,
        }
