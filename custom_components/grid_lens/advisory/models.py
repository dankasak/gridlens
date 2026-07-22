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
    # {label: {days_earned, days_total, amount, amount_per_day}} — e.g. GloBird
    # ZEROHERO's credit. Empty for plans without a conditional credit.
    conditional_credits: dict = field(default_factory=dict)

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
            "trajectory": self.trajectory,
            "conditional_credits": self.conditional_credits,
        }
