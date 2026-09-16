"""RateForecaster — forward import/export price curves from GridLens plan data.

Uses our own authoritative plan definition (``PlanFromData`` via ``get_import_rate`` /
``get_export_rate``), NOT any retailer integration's computed rate. The rate *structure*
(ToU windows, FiT) is deterministic from the plan; for wholesale-exposed plans (a PEA
block) an optional overlay adds the marginal wholesale exposure from a forward wholesale
series (market data), off by default until calibrated against an actual bill.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Protocol

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class RateForecaster(Protocol):
    """Return (import_rate, export_rate) $/kWh lists of length ``n_slots`` from ``start``,
    one value per slot of ``slot_minutes``."""

    def rates(
        self, start: datetime, n_slots: int, slot_minutes: int = 60
    ) -> tuple[list[float], list[float]]: ...


class PlanRateForecaster:
    """Forward rates from a GridLens plan.

    Args:
        plan: a ``PlanFromData``-like object exposing ``get_import_rate(dt)``,
            ``get_export_rate(dt)``, and (optionally) ``aemo_price_sensor`` + ``bpea``.
        wholesale_by_hour: {hour-aligned dt: $/kWh} forward wholesale curve (market data).
        enable_pea_overlay: add the marginal wholesale-exposure term for PEA plans.
            **Off by default** — enabling before calibration risks double-counting the
            energy component already baked into the plan's base rate.
    """

    def __init__(
        self,
        plan,
        *,
        wholesale_by_hour: Optional[dict[datetime, float]] = None,
        enable_pea_overlay: bool = False,
    ) -> None:
        self.plan = plan
        self.wholesale = wholesale_by_hour or {}
        self.enable_pea_overlay = enable_pea_overlay
        self.bpea = float(getattr(plan, "bpea", 0.0) or 0.0)
        self.has_pea = bool(getattr(plan, "aemo_price_sensor", None))

    def rates(
        self, start: datetime, n_slots: int, slot_minutes: int = 60
    ) -> tuple[list[float], list[float]]:
        imp: list[float] = []
        exp: list[float] = []
        overlay_on = self.enable_pea_overlay and self.has_pea
        for i in range(n_slots):
            dt = start + timedelta(minutes=i * slot_minutes)
            ir = float(self.plan.get_import_rate(dt))
            er = float(self.plan.get_export_rate(dt))
            if overlay_on:
                w = self.wholesale.get(self._hour_key(dt))
                if w is not None:
                    # Marginal wholesale exposure vs the PEA benchmark. May be negative
                    # (paid to import) in cheap/negative-price periods — intended.
                    ir += w - self.bpea
            imp.append(ir)
            exp.append(er)
        return imp, exp

    @staticmethod
    def _hour_key(dt: datetime) -> datetime:
        return dt_util.as_local(dt).replace(minute=0, second=0, microsecond=0)
