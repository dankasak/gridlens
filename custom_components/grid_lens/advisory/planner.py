"""AdvisoryPlanner — runs the LP optimizer over a forecast and builds a dispatch plan.

Advisory mode is **read-only**: this produces a plan + SOC trajectory for publishing and
verification. Nothing here writes to the battery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.util import dt as dt_util

from ..battery_optimizer import BatteryOptimizer
from ..control.executor import DispatchInterval
from ..inverters.base import BatteryAction
from .models import AdvisoryResult, ForecastBundle

_LOGGER = logging.getLogger(__name__)


class AdvisoryPlanner:
    """Convert an optimizer schedule into a ``DispatchInterval`` plan + SOC trajectory."""

    def __init__(
        self,
        optimizer: BatteryOptimizer,
        *,
        power_threshold_kw: float = 0.05,
        soc_reward: float = 0.0003,
        no_grid_charge: bool = False,
        soft_terminal_soc: bool = True,
        min_export_price: float = 0.0,
    ) -> None:
        self.optimizer = optimizer
        # Below this, an hour's charge/discharge is treated as self-consumption.
        self.threshold = power_threshold_kw
        # Tiny intrinsic value on stored energy — breaks LP degeneracy toward the
        # sensible plan (charge surplus solar rather than $0-export; hold charge for the
        # paid export window instead of self-consuming). Calibrated on the LXC: 0.0003 is
        # pure tie-breaking (net cost identical to the true optimum); 0.001 starts to
        # distort. See GRIDLENS_CHECKLIST.md.
        self.soc_reward = soc_reward
        # Home battery: forbid the LP from grid-charging the battery. Grid still covers
        # house load + deferrable devices; the battery only ever charges from solar surplus.
        # (Blocks the buy@import→export arbitrage the LP would otherwise find.)
        self.no_grid_charge = no_grid_charge
        # Rolling-horizon advisory: soften the terminal-SOC constraint. The hard floor
        # soc[T-1] >= E0 (needed only for plan comparison) otherwise forces the battery to
        # buy grid energy at the horizon tail to refill to its starting SOC — the phantom
        # "charge burst right after the export window". Instead we value the energy left in
        # the battery at horizon end (see _terminal_soc_value), so the LP keeps solar it
        # would otherwise dump but never grid-charges purely to hit a forced end-state.
        self.soft_terminal_soc = soft_terminal_soc
        # Below this price ($/kWh), export earns nothing in the objective — the LP
        # prefers routing surplus into a deferrable load or holding charge instead of
        # selling cheap (still exports if nothing else can absorb the surplus). 0.0 =
        # disabled, unchanged behaviour. See battery_optimizer.optimize_hourly_schedule.
        self.min_export_price = min_export_price

    def plan(
        self,
        bundle: ForecastBundle,
        initial_soc_percent: float,
        *,
        deferrable_loads: Optional[list[dict]] = None,
        demand_rate: float = 0.0,
        demand_window_mask: Optional[list[int]] = None,
        import_caps: Optional[list[dict]] = None,
        export_caps: Optional[list[dict]] = None,
        conditional_credits: Optional[list[dict]] = None,
    ) -> AdvisoryResult:
        dt_h = bundle.dt_hours
        terminal_value = (
            self._terminal_soc_value(bundle) if self.soft_terminal_soc else None
        )
        result = self.optimizer.optimize_hourly_schedule(
            bundle.solar_kwh,
            bundle.load_kwh,
            bundle.import_rate,
            bundle.export_rate,
            initial_soc_percent=initial_soc_percent,
            deferrable_loads=deferrable_loads or [],
            demand_rate=demand_rate,
            demand_window_mask=demand_window_mask,
            timestep_hours=dt_h,
            soc_reward=self.soc_reward,
            no_grid_charge=self.no_grid_charge,
            terminal_soc_value=terminal_value,
            import_caps=import_caps,
            export_caps=export_caps,
            conditional_credits=conditional_credits,
            min_export_price=self.min_export_price,
        )

        devs = deferrable_loads or []
        defer_names = [d.get("name") or f"Load {i + 1}" for i, d in enumerate(devs)]

        schedule = result.get("schedule", [])
        plan: list[DispatchInterval] = []
        trajectory: list[dict] = []

        for step in schedule:
            t = int(step["hour"])
            start = bundle.start + timedelta(minutes=t * bundle.slot_minutes)
            action, power_w, grid_charge_w, export_w = self._classify(step, dt_h)
            plan.append(
                DispatchInterval(
                    start=start,
                    action=action,
                    power_w=power_w,
                    grid_charge_w=grid_charge_w,
                    export_w=export_w,
                    import_rate=step.get("import_rate"),
                )
            )
            row = {
                "start": start.isoformat(),
                "soc_percent": round(step.get("soc_percent", 0.0), 1),
                "action": action.value,
                "power_w": round(power_w, 1),
                # Grid contribution to a CHARGE slot (0 = solar-only). The executor runs
                # solar-only charge as Maximum Self-consumption, so the card's EMS
                # timeline needs this to show the mode that will actually be commanded.
                "grid_charge_w": round(grid_charge_w, 1),
                # Export contribution of a DISCHARGE slot (0 = pure load coverage). The
                # executor runs load-covering discharge as self-consumption, so the card's
                # EMS timeline needs this to show the mode that will actually be commanded.
                "export_w": round(export_w, 1),
                "solar_kwh": round(step.get("solar_kwh", 0.0), 3),
                "load_kwh": round(step.get("load_kwh", 0.0), 3),
                "deferrable_kwh": round(step.get("deferrable_kwh", 0.0), 3),  # total (tooltip)
                "buy_kwh": round(step.get("import_kwh", 0.0), 3),      # grid import
                "sell_kwh": round(step.get("export_kwh", 0.0), 3),     # grid export
                "import_rate": round(step.get("import_rate", 0.0), 4),
                "export_rate": round(step.get("export_rate", 0.0), 4),
                "cost": round(step.get("import_cost", 0.0), 4),        # $ spent buying
                "credit": round(step.get("export_credit", 0.0), 4),   # $ earned selling
            }
            # One key per deferrable device (defer_0, defer_1, …) so the card can plot
            # them as separate series.
            per_dev = step.get("deferrable_per_device", []) or []
            for j in range(len(defer_names)):
                row[f"defer_{j}"] = round(per_dev[j], 3) if j < len(per_dev) else 0.0
            trajectory.append(row)

        return AdvisoryResult(
            generated_at=dt_util.now(),
            start=bundle.start,
            horizon_hours=len(schedule),
            initial_soc_percent=initial_soc_percent,
            final_soc_percent=result.get("final_soc_percent", initial_soc_percent),
            net_cost=result.get("net_cost", 0.0),
            solver=result.get("solver", "unknown"),
            plan=plan,
            trajectory=trajectory,
            deferrable_names=defer_names,
            conditional_credits=result.get("conditional_credits", {}),
        )

    @staticmethod
    def _terminal_soc_value(bundle: ForecastBundle) -> float:
        """Fair intrinsic value ($/kWh) of energy left in the battery at horizon end.

        Used to soften the terminal-SOC constraint. We price it at the horizon's mean
        export rate — a conservative "you could have sold it" valuation:
          * It is > 0, so the LP holds surplus solar into the far tail instead of dumping
            it at $0 export (no wasteful end-of-horizon discharge).
          * It is well below import_rate / eta (the grid-charge break-even), so it can never
            make buying grid to bank terminal energy worthwhile — killing the Bug 2 tail
            refill while leaving genuine buy-low/sell-high arbitrage (priced by the real
            in-window export rate) untouched.
        The mean is used (not a single slot's rate) so the value is stable and not gameable
        by whatever rate happens to fall on the last slot.
        """
        rates = [max(0.0, r) for r in bundle.export_rate]
        if not rates:
            return 0.0
        return sum(rates) / len(rates)

    def _classify(self, step: dict, dt_h: float) -> tuple[BatteryAction, float, float, float]:
        """Map an optimizer slot to a battery action + average power (W) + grid-charge (W)
        + export (W).

        Schedule values are ENERGY (kWh) per slot, so power = energy / slot-hours.

        The third element, ``grid_charge_w``, is the portion of a CHARGE slot the plan
        intends to draw **from the grid**: grid import beyond what house load + deferrable
        devices consume must be feeding the battery (the optimizer nets simultaneous
        import/export, so any import above load+deferrable is battery charging). It is 0
        for solar-only charge slots (import only covers load) and for non-charge slots.
        The executor uses it to pick self-consumption vs a real grid force-charge.

        The fourth element, ``export_w``, is the portion of a DISCHARGE slot the plan
        intends to sell **to the grid** (the slot's export energy, capped at the discharge
        itself). It is 0 for load-covering discharge slots and for non-discharge slots. The
        executor uses it to pick self-consumption vs a real forced "battery first" discharge.
        """
        charge = float(step.get("charge_kwh", 0.0))
        discharge = float(step.get("discharge_kwh", 0.0))
        # Threshold is a kWh-per-slot floor; scale it by dt so it's the same power.
        floor = self.threshold * dt_h
        if charge > floor and charge >= discharge:
            grid_charge_w = self._grid_charge_w(step, charge, dt_h)
            return BatteryAction.CHARGE, charge / dt_h * 1000.0, grid_charge_w, 0.0
        if discharge > floor:
            export_w = self._export_w(step, discharge, dt_h)
            return BatteryAction.DISCHARGE, discharge / dt_h * 1000.0, 0.0, export_w
        return BatteryAction.SELF_USE, 0.0, 0.0, 0.0

    def _grid_charge_w(self, step: dict, charge_kwh: float, dt_h: float) -> float:
        """Grid contribution to *battery* charge for a CHARGE slot, in watts.

        = max(0, import_kwh - (load_kwh + deferrable_kwh)), capped at the slot's total
        charge energy, converted to average power. Floored to 0 when at/below the
        classification threshold so float noise never fabricates a grid-import command.
        """
        import_kwh = float(step.get("import_kwh", 0.0))
        load_kwh = float(step.get("load_kwh", 0.0))
        deferrable_kwh = float(step.get("deferrable_kwh", 0.0))
        grid_to_battery = import_kwh - (load_kwh + deferrable_kwh)
        # Can't grid-charge more than the battery actually charges this slot.
        grid_to_battery = min(max(0.0, grid_to_battery), charge_kwh)
        if grid_to_battery <= self.threshold * dt_h:
            return 0.0
        return grid_to_battery / dt_h * 1000.0

    def _export_w(self, step: dict, discharge_kwh: float, dt_h: float) -> float:
        """Export contribution of *battery* discharge for a DISCHARGE slot, in watts.

        = export_kwh capped at the slot's total discharge energy (the battery can't sell
        more than it discharges), converted to average power. Floored to 0 when at/below
        the classification threshold so float noise never fabricates a forced discharge.
        """
        export_kwh = float(step.get("export_kwh", 0.0))
        export_kwh = min(max(0.0, export_kwh), discharge_kwh)
        if export_kwh <= self.threshold * dt_h:
            return 0.0
        return export_kwh / dt_h * 1000.0
