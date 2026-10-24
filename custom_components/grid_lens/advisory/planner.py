"""AdvisoryPlanner — runs the LP optimizer over a forecast and builds a dispatch plan.

Advisory mode is **read-only**: this produces a plan + SOC trajectory for publishing and
verification. Nothing here writes to the battery.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from homeassistant.util import dt as dt_util

from ..battery_optimizer import BatteryOptimizer
from ..control.executor import DispatchInterval
from ..inverters.base import BatteryAction
from ..retailer_plans import slot_calendar_day_index
from .models import AdvisoryResult, ForecastBundle

_LOGGER = logging.getLogger(__name__)

# $/kWh the shadow solve credits for each kWh of bonus (beyond-target) deferrable draw.
# Must sit clearly above the LP's tie-breakers — soc_reward (0.0003 per slot held, so
# lending battery charge to a device for a few hours and refilling it from spill costs
# ~0.002-0.004) and the charge-timing nudge — or the LP would rather bank energy it is
# about to spill anyway. Must sit far below any real price, so it only buys energy that
# is genuinely ~free: spill at/below the Minimum Export Price (which the LP values at $0)
# or import at <1c. Any export the LP does value (at/above the floor) that pays more
# than 1c still wins over bonus.
_BONUS_VALUE_PER_KWH = 0.01


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
        demand_peak_kw_month_to_date: float = 0.0,
        demand_days_remaining: float = 0.0,
        import_caps: Optional[list[dict]] = None,
        export_caps: Optional[list[dict]] = None,
        conditional_credits: Optional[list[dict]] = None,
        export_floor_kwh: Optional[float] = None,
        export_floor_slot: Optional[int] = None,
    ) -> AdvisoryResult:
        dt_h = bundle.dt_hours
        terminal_value = (
            self._terminal_soc_value(bundle) if self.soft_terminal_soc else None
        )
        # Real calendar-day boundaries for the LP's per-device daily-total constraint —
        # this horizon starts at "now" (bundle.start), not local midnight, so without
        # this the LP would treat "day 0" as a rolling 24h window and could satisfy a
        # same-day-scoped target (e.g. a Daily Target lowered because today is cloudy)
        # out of TOMORROW's cheaper solar instead. See battery_optimizer.py's
        # slot_day_index docstring / GRIDLENS_CHECKLIST.md.
        slot_day_index = slot_calendar_day_index(
            bundle.start, bundle.slots, bundle.slot_minutes
        )
        grid_charge_cap_kwh = self._grid_charge_hedge_cap(bundle, initial_soc_percent)
        solve_kwargs = dict(
            initial_soc_percent=initial_soc_percent,
            demand_rate=demand_rate,
            demand_window_mask=demand_window_mask,
            demand_peak_kw_month_to_date=demand_peak_kw_month_to_date,
            demand_days_remaining=demand_days_remaining,
            timestep_hours=dt_h,
            soc_reward=self.soc_reward,
            no_grid_charge=self.no_grid_charge,
            terminal_soc_value=terminal_value,
            import_caps=import_caps,
            export_caps=export_caps,
            conditional_credits=conditional_credits,
            min_export_price=self.min_export_price,
            slot_day_index=slot_day_index,
            export_floor_kwh=export_floor_kwh,
            export_floor_slot=export_floor_slot,
            grid_charge_cap_kwh=grid_charge_cap_kwh,
        )
        result = self.optimizer.optimize_hourly_schedule(
            bundle.solar_kwh, bundle.load_kwh, bundle.import_rate, bundle.export_rate,
            deferrable_loads=deferrable_loads or [], **solve_kwargs,
        )

        devs = deferrable_loads or []
        shadow_bonus = self._shadow_bonus(bundle, devs, solve_kwargs)
        defer_names = [d.get("name") or f"Load {i + 1}" for i, d in enumerate(devs)]
        defer_max_kw = [float(d.get("max_kw") or 0.0) for d in devs]
        defer_sensor_ids = [d.get("sensor_id") or "" for d in devs]

        schedule = result.get("schedule", [])
        plan: list[DispatchInterval] = []
        trajectory: list[dict] = []

        for step in schedule:
            t = int(step["hour"])
            start = bundle.start + timedelta(minutes=t * bundle.slot_minutes)
            action, power_w, grid_charge_w, export_w = self._classify(step, dt_h)
            # Per-device deferrable energy (kWh/slot) → average power (W) for the control
            # channel. Same device order as the config lists; missing entries → 0.
            per_dev = step.get("deferrable_per_device", []) or []
            deferrable_w = [
                (float(per_dev[j]) / dt_h * 1000.0) if j < len(per_dev) else 0.0
                for j in range(len(defer_names))
            ]
            plan.append(
                DispatchInterval(
                    start=start,
                    action=action,
                    power_w=power_w,
                    grid_charge_w=grid_charge_w,
                    export_w=export_w,
                    import_rate=step.get("import_rate"),
                    export_rate=step.get("export_rate"),
                    # Whole-slot grid export (PV spill + battery), not just the battery
                    # share carried by export_w — Greedy Consumption's forecast-surplus
                    # condition needs the total the plan expects to push out the meter.
                    total_export_w=max(0.0, float(step.get("export_kwh", 0.0))) / dt_h * 1000.0,
                    deferrable_w=deferrable_w,
                    # Same source as the trajectory row's own "soc_percent" below — the
                    # LP's own forecasted SOC at this slot's start, for Greedy Consumption's
                    # plan-aware battery-safe-rate calculation (see DispatchInterval's
                    # docstring). float(...) rather than the row's rounded copy: this one
                    # feeds a calculation, not a display.
                    forecast_soc_percent=(
                        float(step["soc_percent"]) if step.get("soc_percent") is not None
                        else None
                    ),
                    shadow_bonus_w=(
                        [kwh / dt_h * 1000.0 for kwh in shadow_bonus[t]]
                        if shadow_bonus and t < len(shadow_bonus) else []
                    ),
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
                # Net battery flow for the slot: +charge (into battery) / -discharge (out).
                # Lets the power chart plot one signed battery line instead of two positive
                # ones, matching how buy/sell are combined client-side into a net grid line.
                "battery_kwh": round(step.get("charge_kwh", 0.0) - step.get("discharge_kwh", 0.0), 3),
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
            # Predicted per-device SOC (%) — present only on day-0 slots of a device that
            # has an SOC model (soc_capacity_kwh + a live reading), so the power chart can
            # draw its own SOC curve on the right axis and stop it at the end of day 0.
            soc_map = step.get("deferrable_soc_percent") or {}
            for j, pct in soc_map.items():
                if pct is not None and 0 <= j < len(defer_names):
                    row[f"defer_{j}_soc"] = round(pct, 1)
            # Shadow-mode bonus kWh per device (see _shadow_bonus) — only written where
            # non-zero, so the (already large) trajectory attribute doesn't grow a key per
            # device per slot for a feature that is idle most of the day.
            if shadow_bonus and t < len(shadow_bonus):
                for j, kwh in enumerate(shadow_bonus[t]):
                    if kwh > 0.001:
                        row[f"bonus_{j}"] = round(kwh, 3)
            trajectory.append(row)

        demand_summary = None
        if demand_rate and demand_window_mask and any(demand_window_mask):
            demand_summary = {
                "planned_peak_kw": (
                    round(result["demand_peak_kw"], 3)
                    if result.get("demand_peak_kw") is not None else None
                ),
                "prior_peak_kw": round(float(demand_peak_kw_month_to_date), 3),
                "rate_per_kw_per_day": round(float(demand_rate), 5),
                "days_remaining": round(float(demand_days_remaining), 1),
                "window_slots": sum(1 for m in demand_window_mask if m),
            }

        return AdvisoryResult(
            generated_at=dt_util.now(),
            start=bundle.start,
            horizon_hours=len(schedule),
            initial_soc_percent=initial_soc_percent,
            final_soc_percent=result.get("final_soc_percent", initial_soc_percent),
            net_cost=result.get("net_cost", 0.0),
            solver=result.get("solver", "unknown"),
            solver_note=result.get("solver_note"),
            plan=plan,
            trajectory=trajectory,
            deferrable_names=defer_names,
            deferrable_max_kw=defer_max_kw,
            deferrable_sensor_ids=defer_sensor_ids,
            conditional_credits=result.get("conditional_credits", {}),
            battery_max_charge_kw=self.optimizer.max_charge_rate_kw,
            battery_max_discharge_kw=self.optimizer.max_discharge_rate_kw,
            ev_soc_status=result.get("ev_soc_status", []),
            demand=demand_summary,
        )

    def _shadow_bonus(
        self, bundle: ForecastBundle, devs: list[dict], solve_kwargs: dict
    ) -> Optional[list[list[float]]]:
        """Per-slot, per-device bonus kWh from a second, SHADOW solve — or None.

        The shadow solve is the live plan's exact inputs plus ``bonus_value`` on every
        device the coordinator marked ``bonus_eligible`` (Greedy + Forecast Surplus on).
        It is solved separately rather than folded into the live plan so evaluating it
        cannot move the battery schedule the executor is acting on: only its bonus column
        is published, next to Greedy Consumption's own forecast-surplus target, to compare
        the two before the LP version takes over (FEATURES.md §7). Any failure is logged
        and swallowed — the live plan is already solved and must never depend on this.
        """
        if not any(d.get("bonus_eligible") for d in devs):
            return None
        shadow_devs = [
            {**d, "bonus_value": _BONUS_VALUE_PER_KWH} if d.get("bonus_eligible") else d
            for d in devs
        ]
        try:
            shadow = self.optimizer.optimize_hourly_schedule(
                bundle.solar_kwh, bundle.load_kwh, bundle.import_rate, bundle.export_rate,
                deferrable_loads=shadow_devs, **solve_kwargs,
            )
        except Exception:  # noqa: BLE001 — shadow must never break the live plan
            _LOGGER.exception("Shadow bonus solve failed")
            return None
        if not str(shadow.get("solver", "")).startswith("lp/"):
            # The greedy fallback models no deferrable loads at all, so it has no bonus
            # to report — publishing its zeros would read as "the LP saw no surplus".
            return None
        rows = shadow.get("schedule", [])
        out = [
            [float(v) for v in (row.get("bonus_per_device") or [0.0] * len(devs))]
            for row in rows
        ]
        _LOGGER.debug(
            "Shadow bonus solve: %s",
            {d.get("name") or i: round(sum(r[i] for r in out if i < len(r)), 2)
             for i, d in enumerate(devs) if d.get("bonus_eligible")},
        )
        return out

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

    def _grid_charge_hedge_cap(
        self, bundle: ForecastBundle, initial_soc_percent: float
    ) -> Optional[list[float]]:
        """Per-slot grid-charge allowance (kWh), beyond that slot's own solar, fed to
        ``battery_optimizer``'s ``grid_charge_cap_kwh``.

        Why this exists: a plain point (P50) solar forecast makes the LP's "should I
        grid-charge as insurance against today's solar coming up short" decision a
        cliff rather than a graduated response to how uncertain the forecast actually
        is -- reproduced 2026-10-15 (GRIDLENS_CHECKLIST.md): feeding the real solver
        today's actual rates/deferrables and a midday solar forecast at 100% of what
        materialised chose zero pre-dawn grid-charge, but at 70% of that same actual
        outcome it swung to ~9kWh, and at 50% to ~18kWh -- a bang-bang response with no
        proportionality to forecast confidence, which is exactly what let a single
        pessimistic-but-plausible morning burn real import money charging a battery that
        solar alone would have filled within hours (then had to dump at a near-floor
        export rate all afternoon).

        The fix: restrict how much grid import may fund battery charge (on top of
        load/deferrables, which are untouched) ONLY for the pre-solar slots at the
        start of the horizon, sized from Solcast's own uncertainty band rather than
        the single P50 estimate --
          * skip the hedge entirely if the P90 (optimistic-but-plausible) forecast,
            net of load, would already close the gap to the battery's ceiling before
            the next genuinely-paid (>= min_export_price) export window;
          * otherwise size it to the P10 (pessimistic-but-plausible) shortfall against
            that same gap -- never more.
        A genuinely low-solar day (P10/P50/P90 all low together, no forecast *error*,
        just weak sun) still hedges at full strength, since P90 alone won't cover the
        gap either -- this only suppresses the hedge when the optimistic case is
        plausible enough to make pre-charging pointless.

        Returns None (no restriction -- unchanged behaviour) when the forecast
        provider has no percentile bands (``bundle.solar_kwh_p10``/``_p90`` unset --
        see ``ForecastProvider._has_percentile_fields``), never a false "nothing to
        hedge" from treating a missing band as zero.
        """
        if bundle.solar_kwh_p10 is None or bundle.solar_kwh_p90 is None:
            return None
        T = bundle.slots
        if T == 0:
            return None
        solar_p50 = bundle.solar_kwh[:T]
        solar_p90 = bundle.solar_kwh_p90[:T]
        solar_p10 = bundle.solar_kwh_p10[:T]
        load = bundle.load_kwh[:T]
        export_rate = bundle.export_rate[:T]
        dt_h = bundle.dt_hours
        opt = self.optimizer
        hardware_cap = opt.max_charge_rate_kw * dt_h
        caps = [float("inf")] * T

        # The pre-solar window: slots from "now" up to (not including) the first slot
        # whose own P50 solar, or the running total before it, is non-negligible
        # (0.05 kWh -- a rounding/noise floor, not a real contribution). Only this
        # window's grid-charge decision is a blind bet on a forecast with nothing yet
        # observed to correct it; once solar is flowing the rolling re-solve has real
        # data to react to every ~2 minutes. A horizon that starts with solar already
        # up therefore has NO pre-solar window and gets no restriction at all.
        cum_p50 = 0.0
        pre_solar_slots: list[int] = []
        for t in range(T):
            # Two separate exits, and the second one matters: a slot that ALREADY has
            # meaningful solar of its own is not a blind bet on a forecast, so it does
            # not belong in this window at all. Testing only the running total let the
            # CURRENT slot in unconditionally (cum_p50 is 0 before slot 0 is added),
            # which made every mid-day re-solve treat "now" as pre-dawn and, on a sunny
            # day, cap its grid-charge allowance at 0. Harmless on its own — nobody
            # wants to grid-charge at 08:00 in full sun — but it silently removed the
            # only way slot 0 could reach an active export floor, and the whole LP went
            # INFEASIBLE by 269 Wh (found 2026-10-23, see GRIDLENS_CHECKLIST.md).
            if cum_p50 > 0.05 or solar_p50[t] > 0.05:
                break
            cum_p50 += solar_p50[t]
            pre_solar_slots.append(t)
        if not pre_solar_slots or pre_solar_slots[-1] + 1 >= T:
            return caps

        start = pre_solar_slots[-1] + 1
        cutoff = T
        for t in range(start, T):
            if export_rate[t] >= self.min_export_price:
                cutoff = t
                break

        gap_to_ceiling = max(
            0.0, opt.max_soc_kwh - opt.capacity_kwh * initial_soc_percent / 100.0
        )
        cum_p90 = sum(solar_p90[start:cutoff])
        cum_p10 = sum(solar_p10[start:cutoff])
        cum_load = sum(load[start:cutoff])

        if cum_p90 - cum_load >= gap_to_ceiling:
            hedge_kwh = 0.0
        else:
            shortfall = gap_to_ceiling - max(0.0, cum_p10 - cum_load)
            hedge_kwh = max(0.0, min(shortfall, gap_to_ceiling))

        per_slot = min(hardware_cap, hedge_kwh / len(pre_solar_slots))
        for t in pre_solar_slots:
            caps[t] = per_slot
        return caps

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
