"""Battery optimization for electricity plan comparison.

Uses linear programming (PuLP/CBC) to find the globally-optimal charge/discharge
schedule for each tariff plan.  Falls back to a greedy heuristic when PuLP is
not yet installed (first boot after adding the requirement).

LP formulation (per hour t):
  Variables : P_imp[t], P_exp[t], P_cha[t], P_dis[t], E_bat[t], def_i[t]  (all ≥ 0)
  Objective : minimise Σ rate_imp[t]·P_imp[t] − Σ rate_exp[t]·P_exp[t]
              (def_i has no direct cost — priced implicitly via imp/exp)
  Constraints:
    Energy balance : P_imp[t] + P_dis[t] − P_exp[t] − P_cha[t] − Σ_i def_i[t] = load[t] − solar[t]
    SOC update     : E_bat[t] = E_bat[t-1] + η·P_cha[t] − P_dis[t]/η
    SOC bounds     : E_min ≤ E_bat[t] ≤ E_max
    Power limits   : P_cha[t] ≤ max_charge,  P_dis[t] ≤ max_discharge
    Terminal SOC   : E_bat[T-1] ≥ E0  (battery must end no emptier than it started,
                     otherwise the initial charge is free energy that suppresses
                     grid import and hides differences between plans)
    Availability   : def_i[t] = 0 outside the device's allowed hours (hour_mask)
    Daily totals   : Σ_{t in day d} def_i[t] = daily_kwh_i  (per device per day,
                     capped at what the availability window can physically deliver)
    Capped rates   : for hours inside a capped-rate window w (e.g. GloBird ZEROHERO's
                     50 kWh/day free import window), P_imp[t] splits into a free
                     tranche and an over-cap tranche: P_imp[t] = free_w[t] + over_w[t],
                     priced at the window's normal rate and rate_after_cap respectively,
                     with Σ_{t in w, day d} free_w[t] ≤ daily_cap_kwh_w. Symmetric for
                     P_exp (e.g. a capped Super Export credit reverting to a lower FiT).
                     Because the free tranche is always the cheaper choice (0 < rate_after_cap
                     for import; the capped export credit > its reverting rate), a plain LP
                     fills it first with no extra ordering constraint needed — the standard
                     convex block-tariff trick. Off (zero extra variables) unless the plan
                     actually has a capped rate.

Including def_i in the energy balance lets the LP correctly price deferrable load
scheduling: during solar surplus, def_i reduces P_exp (opportunity cost = rate_exp);
outside solar hours, def_i increases P_imp (cost = rate_imp).  The LP therefore
prefers running deferrable loads from solar when rate_exp < rate_imp.

η is the one-way efficiency (sqrt of round-trip).  Charging 1 kWh stores η kWh;
delivering 1 kWh discharges 1/η kWh from the battery.
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List

_LOGGER = logging.getLogger(__name__)


def _reprice_slot(row: Dict, delta_kwh: float) -> tuple:
    """Recompute a slot's import/export kWh and $ cost after shifting
    ``delta_kwh`` of deferrable-load energy into (positive) or out of
    (negative) it, holding battery charge/discharge and solar/load fixed.
    Returns ``(new_import_kwh, new_export_kwh, new_cost)`` where
    ``cost = import_cost - export_credit``.
    """
    net = (row['import_kwh'] - row['export_kwh']) + delta_kwh
    new_i = max(0.0, net)
    new_e = max(0.0, -net)
    cost = new_i * row['import_rate'] - new_e * row['export_rate']
    return new_i, new_e, cost


def _slot_marginal_tiers(schedule: List[Dict], dev_idx: int, slots: List[int], cap_kwh: float) -> list:
    """Split each slot's [0, cap_kwh] deferrable capacity into a cheap tier —
    the amount absorbable without inducing any grid import at all, i.e. the
    solar/export headroom already sitting in that slot, priced at its
    opportunity cost (export_rate) — and an expensive tier for any remainder,
    priced at import_rate. This mirrors the LP's own capped-rate tranche
    trick (free tranche + over-cap tranche) at the level of a single slot.

    Returns a flat list of ``(unit_cost, slot, tier_capacity)`` so a caller can
    sort by cost and fill greedily — that greedy fill finds the true
    cost-minimising arrangement of a *fixed total* across these slots, so a
    consolidation built from it can only cost the same or less than whatever
    arrangement the LP originally happened to land on.
    """
    tiers = []
    for t in slots:
        row = schedule[t]
        old_def = row['deferrable_per_device'][dev_idx]
        # s = the deferrable level at which this slot's net import/export
        # would sit at exactly zero — i.e. how much of [0, cap_kwh] is
        # "free" (comes from otherwise-exported solar) rather than grid.
        s = old_def + row['export_kwh'] - row['import_kwh']
        s = max(0.0, min(cap_kwh, s))
        if s > 1e-9:
            tiers.append((row['export_rate'], t, s))
        if cap_kwh - s > 1e-9:
            tiers.append((row['import_rate'], t, cap_kwh - s))
    return tiers


def _front_load_device_day(schedule: List[Dict], dev_idx: int, slots: List[int], cap_kwh: float) -> None:
    """Rearrange device ``dev_idx``'s deferrable energy across ``slots`` (one
    calendar day's eligible slots) into the cheapest possible arrangement of
    the SAME total, only committing if doing so does not increase total cost
    across the touched slots. Ties are expected and are the point: def_i has
    zero direct objective cost, so the LP is often indifferent to how it's
    split across equally-free hours — this picks the least-fragmented of the
    equally-good options, using chronological order as the tie-break so
    equally-cheap capacity fills into the earliest slots first (favouring one
    contiguous block over a scattered one).
    """
    if not slots:
        return
    total = sum(schedule[t]['deferrable_per_device'][dev_idx] for t in slots)
    if total <= 1e-9:
        return

    tiers = _slot_marginal_tiers(schedule, dev_idx, slots, cap_kwh)
    tiers.sort(key=lambda tup: (tup[0], tup[1]))  # cheapest first, then earliest

    target = {t: 0.0 for t in slots}
    remaining = total
    for unit_cost, t, capacity in tiers:
        if remaining <= 1e-9:
            break
        take = min(capacity, remaining)
        target[t] += take
        remaining -= take

    old_cost = 0.0
    new_cost = 0.0
    changes = {}
    for t in slots:
        row = schedule[t]
        old_def = row['deferrable_per_device'][dev_idx]
        new_def = target[t]
        delta = new_def - old_def
        if abs(delta) < 1e-9:
            continue
        new_i, new_e, cost_t = _reprice_slot(row, delta)
        old_cost += row['import_cost'] - row['export_credit']
        new_cost += cost_t
        changes[t] = (new_def, new_i, new_e)

    if not changes or new_cost > old_cost + 1e-6:
        return  # no-op, or would make the plan more expensive — leave as solved

    for t, (new_def, new_i, new_e) in changes.items():
        row = schedule[t]
        row['deferrable_per_device'][dev_idx] = new_def
        row['deferrable_kwh'] = sum(row['deferrable_per_device'])
        row['import_kwh'] = new_i
        row['export_kwh'] = new_e
        row['import_cost'] = new_i * row['import_rate']
        row['export_credit'] = new_e * row['export_rate']


def consolidate_deferrable_schedule(
    schedule: List[Dict],
    deferrable_loads: List[Dict],
    *,
    dt: float,
    slots_per_day: int,
    protected_hours=None,
) -> None:
    """Post-process pass: collapse each deferrable device's fragmented per-slot
    LP allocation into the fewest contiguous blocks per calendar day, without
    ever increasing the plan's total cost.

    Why this exists: def_i has zero direct objective cost in the LP (see the
    module docstring) — only Σ def_i[t] per device per day is constrained — so
    whenever several slots in a day carry equal marginal cost for that
    device's energy (e.g. a whole solar-surplus stretch, or a flat off-peak
    window), the solver is mathematically indifferent to how it splits the
    day's total between them. In practice this produces schedules that flick a
    real EV charger or pool pump on and off across the day — provably no
    cheaper than one continuous block, but with none of the real-world
    wear/inconvenience of that modelled. Found live 2026-07-24: the advisory
    card's per-device on/off timeline showed an EV charger toggling full-power
    on/off/on/off across an afternoon with identical $0 marginal cost either
    way (GRIDLENS_CHECKLIST.md).

    This is a mutate-in-place, pure-Python pass over the already-solved
    ``schedule`` (no re-solve, no scipy/numpy dependency — testable without
    the LXC). It re-derives each touched slot's import/export kWh from the
    same energy-balance equation the LP itself uses (holding battery
    charge/discharge and solar/load fixed) and only commits a consolidation
    when the recomputed cost across the touched slots is <= the original —
    ties (the common case) are accepted, anything that would raise the bill
    is rejected outright. Hard guarantee: net_cost from the returned schedule
    can only stay the same or improve, never regress.

    Demand-window hours, capped-rate hours, and conditional-credit masked
    hours are passed in as ``protected_hours`` and left untouched entirely —
    correctly re-deriving their side constraints (peak-kW, daily free-tranche
    totals, per-hour import ceilings for a credit) is real LP surgery, out of
    scope for this cheap post-process. Those slots keep whatever the solver
    originally produced.

    A real day almost always mixes a cheap/free stretch (solar surplus) with
    an expensive one (overnight import), so slots are never just grouped by
    time — each eligible slot's own marginal cost (via
    ``_slot_marginal_tiers``) decides where energy actually lands; a plain
    "fill the earliest slots" pass would happily shove energy into an
    expensive overnight hour and get correctly rejected for the whole day.
    """
    protected = protected_hours or set()
    T = len(schedule)
    n_days = (T + slots_per_day - 1) // slots_per_day
    for i, dev in enumerate(deferrable_loads):
        mask = dev.get('hour_mask')
        cap_kwh = dev['max_kw'] * dt
        for d in range(n_days):
            t0 = d * slots_per_day
            t1 = min(t0 + slots_per_day, T)
            eligible = [
                t for t in range(t0, t1)
                if (mask[t] if mask else True) and t not in protected
            ]
            _front_load_device_day(schedule, i, eligible, cap_kwh)


class BatteryOptimizer:
    """LP-based battery scheduler with greedy fallback."""

    def __init__(
        self,
        capacity_kwh: float,
        max_charge_rate_kw: float,
        max_discharge_rate_kw: float,
        efficiency_percent: float,
        min_soc_percent: float = 10.0,
        max_soc_percent: float = 90.0,
    ):
        self.capacity_kwh = capacity_kwh
        self.max_charge_rate_kw = max_charge_rate_kw
        self.max_discharge_rate_kw = max_discharge_rate_kw
        # One-way efficiency derived from round-trip efficiency
        self.eta = math.sqrt(efficiency_percent / 100.0)
        self.min_soc_kwh = capacity_kwh * min_soc_percent / 100.0
        self.max_soc_kwh = capacity_kwh * max_soc_percent / 100.0

        _LOGGER.info(
            "BatteryOptimizer: %.1f kWh, charge %.1f kW, discharge %.1f kW, "
            "η %.3f (%.0f%% round-trip), SOC %.0f%%–%.0f%%",
            capacity_kwh, max_charge_rate_kw, max_discharge_rate_kw,
            self.eta, efficiency_percent, min_soc_percent, max_soc_percent,
        )

    # ------------------------------------------------------------------
    # Public API (same signature as the old greedy version)
    # ------------------------------------------------------------------

    def optimize_hourly_schedule(
        self,
        solar_profile: List[float],
        load_profile: List[float],
        import_rates: List[float],
        export_rates: List[float],
        initial_soc_percent: float = 50.0,
        deferrable_loads: List[Dict] = None,
        demand_rate: float = 0.0,
        demand_window_mask: List[int] = None,
        timestep_hours: float = 1.0,
        soc_reward: float = 0.0,
        export_penalty: float = 0.0,
        no_grid_charge: bool = False,
        terminal_soc_value: float = None,
        import_caps: List[Dict] = None,
        export_caps: List[Dict] = None,
        conditional_credits: List[Dict] = None,
        min_export_price: float = 0.0,
    ) -> Dict:
        """Return an optimal hourly schedule minimising net energy cost.

        conditional_credits: optional list of day-scoped all-or-nothing bonus
          descriptors, each {'label': str, 'condition': 'max_import_kwh',
          'threshold_kwh': float (a $/kWh-style RATE, kWh per clock-hour —
          scaled by dt internally, not a per-slot cap), 'amount_per_day':
          float, 'hour_mask': list[int] len T} (built by
          retailer_plans.build_conditional_credits).
          Models plans like GloBird ZEROHERO's "$1/day when imports are 0.03
          kWh/hour or less, 6pm-9pm" — a reward earned only if EVERY masked
          hour's import stays at/under threshold_kwh for that calendar day, not
          a continuous price signal like import_caps/export_caps above. Needs
          a MILP binary indicator per (credit, day), so the scipy solve path
          switches from linprog to scipy.optimize.milp whenever this is
          non-empty (see _lp_scipy) — left at the default (None) the model
          behaves exactly as before, still solved as a pure continuous LP.

        deferrable_loads is a list of per-device dicts, each with:
          'daily_kwh': float  — energy the device must consume per day
          'max_kw':    float  — maximum power draw per hour for that device
          'hour_mask': optional list[int] of length T (1 = device available at
                       that LP hour, 0 = unavailable).  Missing/None = always
                       available.  Built by the caller from local hour-of-day.

        Each device gets its own LP variable with its own power cap, so a
        1.8 kW EV charger and a 4.7 kW hot water system are scheduled
        independently and cannot exceed their rated power in any single hour.

        terminal_soc_value ($/kWh, optional) softens the terminal-SOC constraint for
        rolling-horizon (advisory/control) use. Left None (the default), the LP enforces
        the hard floor soc[T-1] >= E0 — correct and required for PLAN COMPARISON, where a
        battery must not drain to empty for free energy. Set to a non-negative $/kWh, the
        hard floor is dropped and end-of-horizon stored energy is instead VALUED in the
        objective at that rate, so the LP neither buys grid at the horizon tail to force a
        refill (Bug 2 artifact) nor treats an empty battery as free. Use a conservative
        value (e.g. the export/FiT rate, well below import/eta) so it can never make
        grid-charging worthwhile.

        demand_rate ($/kW/day) and demand_window_mask (list[int] length T,
        1 = hour is inside the network's demand window) enable peak-demand
        shaving: when set, the LP adds a peak-kW variable to the objective so it
        actively lowers the highest in-window grid import (e.g. by discharging
        the battery or shifting deferrable loads out of the window). Left at the
        default (rate 0 / mask None) the model behaves exactly as before.

        import_caps / export_caps: optional list of capped-rate-window descriptors, each
          {'daily_cap_kwh': float, 'rate_after_cap': float, 'hour_mask': list[int] len T}
          (1 = this LP hour falls inside the window). Within a window, cumulative import
          (or export) at the window's normal rate is capped at daily_cap_kwh per calendar
          day (hours grouped in chunks of 24/timestep_hours, matching the deferrable-load
          daily-total grouping below); hours/energy beyond the cap that day are priced at
          rate_after_cap instead. Mirrors daily_cap_kwh/rate_after_cap on PlanFromData's
          rate windows — build these with retailer_plans.build_rate_caps(). Left at the
          default (None) the model behaves exactly as before.

        min_export_price ($/kWh, optional): below this price, export is treated as
          valueless in the objective — still legal (it remains the sink of last resort
          when nothing else can absorb surplus solar), but no longer rewarded, so the
          LP prefers routing surplus into a deferrable load or holding battery charge
          instead of selling cheap. Does not affect capped export-rate windows
          (export_caps above), which price their own tranches independently. Left at
          the default (0.0) the model behaves exactly as before.

        Tries LP first; falls back to greedy if scipy is unavailable or infeasible.
        """
        if deferrable_loads is None:
            deferrable_loads = []

        T = min(len(solar_profile), len(load_profile),
                len(import_rates), len(export_rates))
        if T == 0:
            return self._empty_result()

        solar = [max(0.0, solar_profile[t]) for t in range(T)]
        load  = [max(0.0, load_profile[t])  for t in range(T)]
        r_imp = import_rates[:T]
        r_exp = export_rates[:T]
        E0    = initial_soc_percent / 100.0 * self.capacity_kwh
        dmask = demand_window_mask[:T] if demand_window_mask else None

        try:
            return self._lp_optimize(solar, load, r_imp, r_exp, E0, T, deferrable_loads,
                                     demand_rate=demand_rate, demand_window_mask=dmask,
                                     timestep_hours=timestep_hours,
                                     soc_reward=soc_reward, export_penalty=export_penalty,
                                     no_grid_charge=no_grid_charge,
                                     terminal_soc_value=terminal_soc_value,
                                     import_caps=import_caps, export_caps=export_caps,
                                     conditional_credits=conditional_credits,
                                     min_export_price=min_export_price)
        except ImportError:
            _LOGGER.warning(
                "PuLP not yet installed — using greedy fallback. "
                "Restart HA again after the first boot to enable LP optimisation."
            )
            return self._greedy_optimize(solar, load, r_imp, r_exp, E0, T,
                                         timestep_hours=timestep_hours)
        except Exception as exc:
            _LOGGER.warning("LP optimisation failed (%s) — using greedy fallback.", exc)
            return self._greedy_optimize(solar, load, r_imp, r_exp, E0, T,
                                         timestep_hours=timestep_hours)

    def calculate_no_battery_cost(
        self,
        solar_profile: List[float],
        load_profile: List[float],
        import_rates: List[float],
        export_rates: List[float],
    ) -> Dict:
        """Baseline cost with no battery (all surplus exported, all deficit imported)."""
        total_import_kwh = total_export_kwh = 0.0
        total_import_cost = total_export_credit = 0.0
        for t in range(min(len(solar_profile), len(load_profile))):
            net = solar_profile[t] - load_profile[t]
            if net >= 0:
                total_export_kwh   += net
                total_export_credit += net * export_rates[t]
            else:
                total_import_kwh   += -net
                total_import_cost  += -net * import_rates[t]
        return {
            'total_import_kwh':    total_import_kwh,
            'total_export_kwh':    total_export_kwh,
            'total_import_cost':   total_import_cost,
            'total_export_credit': total_export_credit,
            'net_cost':            total_import_cost - total_export_credit,
        }

    # ------------------------------------------------------------------
    # MILP implementation — tries HiGHS, scipy, PuLP/CBC in order
    # ------------------------------------------------------------------

    def _lp_optimize(self, solar, load, r_imp, r_exp, E0, T, deferrable_loads,
                     demand_rate=0.0, demand_window_mask=None, timestep_hours=1.0,
                     soc_reward=0.0, export_penalty=0.0, no_grid_charge=False,
                     terminal_soc_value=None, import_caps=None, export_caps=None,
                     conditional_credits=None, min_export_price=0.0):
        """Build and solve the LP. Raises on failure so caller can fall back."""
        # HiGHS/PuLP paths model none of the extras below; only the scipy path (the
        # complete, production path) does. Skip straight to scipy for any of them.
        # (terminal_soc_value softens the terminal-SOC constraint — scipy-only.)
        demand_active = demand_rate > 0 and demand_window_mask and any(demand_window_mask)
        caps_active = bool(import_caps) or bool(export_caps)
        credits_active = bool(conditional_credits)
        if (not demand_active and not caps_active and timestep_hours == 1.0
                and soc_reward == 0.0 and export_penalty == 0.0 and not no_grid_charge
                and terminal_soc_value is None and not credits_active
                and min_export_price == 0.0):
            try:
                return self._lp_highspy(solar, load, r_imp, r_exp, E0, T, deferrable_loads)
            except ImportError:
                pass  # highspy not installed — try scipy
            except Exception as exc:
                _LOGGER.warning("HiGHS MILP failed (%s) — trying scipy", exc)

        try:
            return self._lp_scipy(solar, load, r_imp, r_exp, E0, T, deferrable_loads,
                                  demand_rate=demand_rate,
                                  demand_window_mask=demand_window_mask,
                                  timestep_hours=timestep_hours,
                                  soc_reward=soc_reward, export_penalty=export_penalty,
                                  no_grid_charge=no_grid_charge,
                                  terminal_soc_value=terminal_soc_value,
                                  import_caps=import_caps, export_caps=export_caps,
                                  conditional_credits=conditional_credits,
                                  min_export_price=min_export_price)
        except ImportError:
            pass  # scipy not available — try PuLP
        except Exception as exc:
            _LOGGER.warning("scipy LP failed (%s) — trying PuLP/CBC", exc)

        try:
            return self._lp_pulp(solar, load, r_imp, r_exp, E0, T, deferrable_loads)
        except Exception:
            raise  # let caller catch and fall back to greedy

    # ---- scipy LP (uses HiGHS internally, no external binary needed) ----
    # Pure LP (no integer variables) so it solves in milliseconds even for
    # 700+ hour windows.  The LP has no mutual-exclusivity constraint between
    # import and export, so for plans where FiT > import rate in some hours
    # (OVO free period, GloBird overnight) the solver may simultaneously
    # import and export.  We prevent the problem becoming unbounded by capping
    # import at a physical grid limit (M), then post-process to net any
    # simultaneous import/export to a single direction.

    def _lp_scipy(self, solar, load, r_imp, r_exp, E0, T, deferrable_loads,
                  demand_rate=0.0, demand_window_mask=None, timestep_hours=1.0,
                  soc_reward=0.0, export_penalty=0.0, no_grid_charge=False,
                  terminal_soc_value=None, import_caps=None, export_caps=None,
                  conditional_credits=None, min_export_price=0.0):
        import numpy as np
        from scipy.optimize import linprog
        from scipy.sparse import lil_matrix

        eta = self.eta
        dt = timestep_hours  # slot length in hours; variables are ENERGY (kWh) per slot
        M = (self.max_charge_rate_kw + self.max_discharge_rate_kw) * 2.0 * dt
        N = len(deferrable_loads)       # number of individual deferrable devices
        slots_per_day = int(round(24 / dt))
        n_days = math.ceil(T / slots_per_day)
        # A day-chunk is "truncated" when the rolling horizon ends partway through it
        # (chunks are anchored to horizon start, not midnight — see the deferrable
        # daily-total constraint below for why only such chunks get relaxed). Given
        # t1 = min(t0+slots_per_day, T), only the LAST chunk can ever be short.
        truncated_days = {
            d for d in range(n_days)
            if min((d + 1) * slots_per_day, T) - d * slots_per_day < slots_per_day
        }

        # Peak-demand shaving: add one auxiliary variable P (peak kW), constrained
        # to be ≥ grid import in every demand-window hour and priced at the demand
        # charge over the horizon (rate $/kW/day × days). Minimising P drives the
        # LP to flatten the highest in-window import — by discharging the battery
        # or shifting deferrable loads out of the window. Off by default.
        demand_active = demand_rate > 0 and demand_window_mask and any(demand_window_mask)

        # Variable layout:
        #   [imp(T) | exp(T) | cha(T) | dis(T) | soc(T) | def_0(T) | ... | def_{N-1}(T) | P? | cap tranches...]
        # Each device i has its own block of T variables starting at (5+i)*T.
        # P (peak kW) is a single trailing scalar, present only when demand_active.
        I, X, C, D, S = 0, T, 2*T, 3*T, 4*T
        P_idx = (5 + N) * T
        n = (5 + N) * T + (1 if demand_active else 0)

        # Capped-rate windows (e.g. GloBird ZEROHERO's 50 kWh/day free import window):
        # for each hour inside a window, imp[t] (or exp[t]) is decomposed into a free
        # tranche and an over-cap tranche via a linking equality, each block sized to
        # exactly the window's hours (not all T) so uncapped plans pay zero extra cost.
        cap_blocks = []
        for direction, caps in (("import", import_caps or []), ("export", export_caps or [])):
            for cw in caps:
                mask = cw.get("hour_mask") or []
                hours = [t for t in range(T) if t < len(mask) and mask[t]]
                if not hours:
                    continue
                free_idx0 = n
                n += len(hours)
                over_idx0 = n
                n += len(hours)
                cap_blocks.append({
                    "direction": direction, "hours": hours,
                    "free_idx0": free_idx0, "over_idx0": over_idx0,
                    "rate_after_cap": cw["rate_after_cap"],
                    "daily_cap_kwh": cw["daily_cap_kwh"],
                })

        # Conditional day-credits (e.g. GloBird ZEROHERO's "$1/day when imports
        # are <=0.03 kWh/hour, 6pm-9pm"): unlike the cap tranches above (a
        # continuous price signal), this is all-or-nothing per calendar day, so
        # it needs a binary indicator y[credit,day] rather than another LP
        # variable — hence the milp() solve below whenever credit_blocks is
        # non-empty. One binary per (credit, day) that has at least one masked
        # hour in this horizon; a day only partially visible at the horizon's
        # far edge still gets one (it'll be corrected on the next rolling
        # replan once fully in view) — the caller is responsible for zeroing a
        # day's mask entirely (not passing partial-day hours) when that day's
        # credit is already unattainable (e.g. real grid import already
        # exceeded the threshold earlier today).
        credit_blocks = []
        for cc in conditional_credits or []:
            mask = cc.get("hour_mask") or []
            day_index = cc.get("day_index") or []
            hours = [t for t in range(T) if t < len(mask) and mask[t]]
            if not hours:
                continue
            # Group by real calendar date (day_index), not t // slots_per_day —
            # see build_conditional_credits for why (the LP horizon starts at
            # "now", not local midnight, so a fixed 24h chunk can split one
            # calendar day's window across two chunks). Falls back to the old
            # chunking only if a caller doesn't supply day_index.
            by_day: dict = {}
            for t in hours:
                key = day_index[t] if t < len(day_index) else t // slots_per_day
                by_day.setdefault(key, []).append(t)
            for d, day_hours in by_day.items():
                credit_blocks.append({
                    "hours": day_hours,
                    "y_idx": n,
                    "threshold_kwh": cc.get("threshold_kwh", 0.0),
                    "amount_per_day": cc.get("amount_per_day", 0.0),
                    "label": cc.get("label", "Conditional Credit"),
                    "day": d,
                })
                n += 1

        # Minimum export price floor: below this, export earns nothing in the
        # objective (still legal — it remains the sink of last resort when nothing
        # else can absorb surplus solar — just no longer rewarded), so the LP
        # prefers routing surplus into a deferrable load or holding battery charge
        # instead of selling cheap. Only affects the base per-slot price built here;
        # r_exp itself is untouched, so every reporting path below (ic/ec, tranche
        # rates, cap_blocks' free-tier pricing further down) still uses the real
        # rate — a below-floor export that does happen is still credited at what it
        # actually earns. Capped export-rate windows overwrite this slot's cost with
        # their own tranche pricing regardless, so the floor has no effect there.
        r_exp_priced = (
            [0.0 if r < min_export_price else r for r in r_exp]
            if min_export_price > 0 else r_exp
        )

        c_obj = np.zeros(n)
        c_obj[I:I+T] = r_imp
        c_obj[X:X+T] = [-r for r in r_exp_priced]
        if demand_active:
            c_obj[P_idx] = demand_rate * n_days
        # Degeneracy regularizers (tiny, << the price signal). export_penalty makes a
        # $0-value export cost a hair, so the LP prefers to CHARGE surplus solar rather
        # than dump it. soc_reward gives stored energy a tiny intrinsic value, so the LP
        # holds charge (imports to cover pre-peak load instead of self-consuming) and
        # keeps the battery full for the paid export window. Both must stay far below the
        # real spread so the peak export still dominates.
        if export_penalty:
            c_obj[X:X+T] += export_penalty
        if soc_reward:
            c_obj[S:S+T] -= soc_reward
        # Capped hours: the base imp[t]/exp[t] cost above (including any export_penalty
        # just added) is replaced by the tranche costs below — zeroed here, last, so it
        # can't be double-counted. Free tranche is priced at the window's normal rate
        # (same value r_imp[t]/r_exp[t] already carries) plus the same tiny export_penalty
        # tie-breaker (so capped export hours keep the same degeneracy nudge as uncapped
        # ones); over-cap tranche at rate_after_cap.
        for cb in cap_blocks:
            base = I if cb["direction"] == "import" else X
            rates = r_imp if cb["direction"] == "import" else r_exp
            sign = 1.0 if cb["direction"] == "import" else -1.0
            penalty = export_penalty if cb["direction"] == "export" else 0.0
            for j, t in enumerate(cb["hours"]):
                c_obj[base + t] = 0.0
                c_obj[cb["free_idx0"] + j] = sign * rates[t] + penalty
                c_obj[cb["over_idx0"] + j] = sign * cb["rate_after_cap"] + penalty
        # y=1 (credit earned) subtracts amount_per_day from cost — the solver
        # only sets it when the big-M constraint below (imp[t] <= threshold)
        # can be satisfied for every hour of that credit's day.
        for cb2 in credit_blocks:
            c_obj[cb2["y_idx"]] -= cb2["amount_per_day"]
        # Soft terminal-SOC valuation (Bug 2 fix, rolling-horizon use only). When set, the
        # hard terminal floor (soc[T-1] >= E0) is dropped below and end-of-horizon stored
        # energy is instead valued here at terminal_soc_value $/kWh — so the LP is not forced
        # to buy grid at the tail to refill, but empty-at-end still costs its intrinsic value.
        # This is ADDITIVE with soc_reward on the final slot (soc_reward stays the tiny
        # per-slot tie-breaker; terminal_soc_value is the boundary valuation) and is deliberately
        # kept far below import_rate/eta so it can never make grid-charging profitable.
        soft_terminal = terminal_soc_value is not None
        if soft_terminal:
            c_obj[S+T-1] -= max(0.0, terminal_soc_value)
        # def_i has NO direct cost in the objective.  Its cost is implicit:
        # when solar is sufficient, def_i reduces exp → opportunity cost = r_exp[t];
        # when solar is insufficient, def_i increases imp → cost = r_imp[t].
        # This lets the LP correctly prefer solar over grid for deferrable loads.

        lb = np.zeros(n)
        ub = np.full(n, np.inf)
        ub[I:I+T] = M
        # Per-slot energy caps = rated power × slot length (kWh).
        ub[C:C+T] = self.max_charge_rate_kw * dt
        ub[D:D+T] = self.max_discharge_rate_kw * dt
        lb[S:S+T] = self.min_soc_kwh
        ub[S:S+T] = self.max_soc_kwh
        for i, dev in enumerate(deferrable_loads):
            # Each device's per-slot draw is capped at its own rated max energy
            # (max_kw × dt), and forced to 0 in slots the device is unavailable.
            mask = dev.get('hour_mask')
            if mask:
                for t in range(T):
                    ub[(5+i)*T+t] = dev['max_kw'] * dt if mask[t] else 0.0
            else:
                ub[(5+i)*T:(5+i)*T+T] = dev['max_kw'] * dt
        for cb2 in credit_blocks:
            ub[cb2["y_idx"]] = 1.0

        # Equality constraints: T (energy balance) + T (SOC update) + N per
        # non-truncated day (per-device daily totals — truncated days are a ≤ cap
        # in A_ub instead, built further down) + one linking row per capped hour
        # (imp[t] or exp[t] = free tranche + over-cap tranche for that hour).
        n_full_days = n_days - len(truncated_days)
        cap_link_rows = sum(len(cb["hours"]) for cb in cap_blocks)
        n_eq = 2*T + N * n_full_days + cap_link_rows
        A_eq = lil_matrix((n_eq, n))
        b_eq = np.zeros(n_eq)

        for t in range(T):
            # Energy balance: imp + dis - exp - cha = (load + Σ def_i) - solar
            # Including def_i in the balance means the LP naturally chooses the
            # cheapest power source for deferrable loads:
            #   • solar-surplus hours: def_i reduces exp → effective cost = r_exp[t]
            #   • non-solar hours:     def_i increases imp → effective cost = r_imp[t]
            # def_i has zero direct objective cost; it is priced entirely via imp/exp.
            A_eq[t, I+t] =  1.0
            A_eq[t, X+t] = -1.0
            A_eq[t, C+t] = -1.0
            A_eq[t, D+t] =  1.0
            for i in range(N):
                A_eq[t, (5+i)*T+t] = -1.0
            b_eq[t] = load[t] - solar[t]

            # SOC update
            row = T + t
            A_eq[row, S+t] =  1.0
            A_eq[row, C+t] = -eta
            A_eq[row, D+t] =  1.0 / eta
            if t > 0:
                A_eq[row, S+t-1] = -1.0
                b_eq[row] = 0.0
            else:
                b_eq[row] = E0

        # Per-device, per-day energy total constraints.
        # Device i on day d must consume exactly dev['daily_kwh'] (prorated for
        # partial days), capped at what its availability window can physically
        # deliver in that chunk so a narrow window cannot make the LP infeasible.
        #
        # EXCEPT the horizon's truncated day-chunk (if any): chunks are anchored to
        # horizon start, not midnight, so the final chunk can land entirely outside a
        # device's solar window (e.g. a rolling-horizon solve starting mid-afternoon
        # ends its last day-chunk overnight, with no solar in it at all). Forcing that
        # chunk's prorated total as a hard equality manufactures a "must run overnight"
        # recommendation that has nothing to do with the real optimum — the real
        # opportunity (tomorrow's solar, past this horizon) shows up on the next
        # rolling replan a couple of minutes later. So a truncated chunk gets a ≤ cap
        # (built into A_ub below) instead: the LP may leave it unmet rather than
        # inventing a time to run it. Found live 2026-07-24 (GRIDLENS_CHECKLIST.md) —
        # the advisory card's deferrable timeline showed both devices dumped to full
        # power right at the horizon's last slot, well after their real cheap window.
        daily_ub_specs: list[tuple[list[int], float]] = []
        eq_row = 2 * T
        for i, dev in enumerate(deferrable_loads):
            mask = dev.get('hour_mask')
            for d in range(n_days):
                t0 = d * slots_per_day
                t1 = min(t0 + slots_per_day, T)
                avail_slots = (
                    sum(1 for t in range(t0, t1) if mask[t]) if mask else (t1 - t0)
                )
                target = dev['daily_kwh'] * (t1 - t0) / slots_per_day
                target = min(target, avail_slots * dev['max_kw'] * dt)
                cols = [(5 + i) * T + t for t in range(t0, t1)]
                if d in truncated_days:
                    daily_ub_specs.append((cols, target))
                else:
                    for col in cols:
                        A_eq[eq_row, col] = 1.0
                    b_eq[eq_row] = target
                    eq_row += 1

        # Cap-tranche linking: imp[t] (or exp[t]) = free[t] + over[t] for every hour
        # inside a capped-rate window, so the tranche split always matches the total
        # grid flow already constrained everywhere else (energy balance, big-M, demand
        # window, no-grid-charge).
        row = eq_row
        for cb in cap_blocks:
            base = I if cb["direction"] == "import" else X
            for j, t in enumerate(cb["hours"]):
                A_eq[row, base + t] = 1.0
                A_eq[row, cb["free_idx0"] + j] = -1.0
                A_eq[row, cb["over_idx0"] + j] = -1.0
                b_eq[row] = 0.0
                row += 1

        # Terminal SOC: the battery must end the window at least as full as it
        # started, so its initial charge is a loan, not free energy.  Clamped to
        # the SOC bounds in case the reported initial SOC lies outside them.
        # linprog uses A_ub x ≤ b_ub, so encode soc[T-1] ≥ E_end as -soc[T-1] ≤ -E_end.
        # This HARD floor is used for plan comparison; in soft-terminal mode it is dropped
        # entirely (the SOC lower bound lb[S:S+T]=min_soc_kwh still keeps soc[T-1] ≥ min_soc)
        # and terminal energy is valued in the objective instead (see soft_terminal above).
        E_end = min(max(E0, self.min_soc_kwh), self.max_soc_kwh)
        term_rows = 0 if soft_terminal else 1
        # Row 0 (when present) is the terminal-SOC bound. When a demand charge is active, add
        # one row per demand-window hour: import[t] - P ≤ 0  (P ≥ every in-window import).
        demand_hours = [t for t in range(T) if demand_window_mask[t]] if demand_active else []
        # no_grid_charge adds T rows forbidding grid import from charging the battery:
        # imp[t] - Σ def_i[t] ≤ load[t]  ⇒  grid may cover house load + deferrable devices,
        # but any battery charge must come from solar surplus only.
        ngc_rows = T if no_grid_charge else 0
        # Daily cumulative cap: one row per (cap window, calendar day) covered by that
        # window's hours, using the same slots_per_day chunking as the deferrable-load
        # daily totals above. Σ free[j] over that day's hours ≤ daily_cap_kwh.
        cap_day_groups = []
        for cb in cap_blocks:
            days: dict[int, list[int]] = {}
            for j, t in enumerate(cb["hours"]):
                days.setdefault(t // slots_per_day, []).append(j)
            for js in days.values():
                cap_day_groups.append((cb, js))
        # One big-M row per masked slot of each credit-day: imp[t] <= threshold
        # when y=1 (imp[t] + M*y <= threshold + M; y=0 relaxes it to imp[t] <= M,
        # already the physical ceiling everywhere else). threshold_kwh is a
        # $/kWh-style RATE ("0.03 kWh/hour" per GloBird's fact sheet), so it's
        # scaled by dt to a per-slot energy cap — same convention as
        # deferrable devices' max_kw * dt above (matters because slot length
        # varies: 30-min slots here, vs the 1-hour slots the plan's fact sheet
        # language assumes).
        credit_rows = sum(len(cb2["hours"]) for cb2 in credit_blocks)
        n_ub = (
            term_rows + len(demand_hours) + ngc_rows + len(cap_day_groups)
            + credit_rows + len(daily_ub_specs)
        )
        A_ub = lil_matrix((n_ub, n)) if n_ub else None
        b_ub = np.zeros(n_ub) if n_ub else None
        r = 0
        if not soft_terminal:
            A_ub[0, S+T-1] = -1.0
            b_ub[0] = -E_end
            r = 1
        for cols, b_val in daily_ub_specs:
            for col in cols:
                A_ub[r, col] = 1.0
            b_ub[r] = b_val
            r += 1
        for t in demand_hours:
            # P is peak kW; import[t] is energy per slot → power = energy / dt.
            A_ub[r, I+t]   =  1.0 / dt
            A_ub[r, P_idx] = -1.0
            r += 1
        if no_grid_charge:
            for t in range(T):
                A_ub[r, I+t] = 1.0
                for i in range(N):
                    A_ub[r, (5 + i) * T + t] = -1.0
                b_ub[r] = load[t]
                r += 1
        for cb, js in cap_day_groups:
            for j in js:
                A_ub[r, cb["free_idx0"] + j] = 1.0
            b_ub[r] = cb["daily_cap_kwh"]
            r += 1
        for cb2 in credit_blocks:
            for t in cb2["hours"]:
                A_ub[r, I+t] = 1.0
                A_ub[r, cb2["y_idx"]] = M
                b_ub[r] = cb2["threshold_kwh"] * dt + M
                r += 1

        # A conditional credit needs an integer y[credit,day] ∈ {0,1} — the plain
        # LP path (linprog, used for every other plan) can't express that, so
        # this switches to scipy.optimize.milp (same HiGHS backend, mixed
        # integer/continuous) only when credit_blocks is non-empty. Every other
        # plan takes the linprog path exactly as before.
        if credit_blocks:
            from scipy.optimize import milp, LinearConstraint, Bounds
            integrality = np.zeros(n)
            for cb2 in credit_blocks:
                integrality[cb2["y_idx"]] = 1
            constraints = [LinearConstraint(A_eq.tocsr(), b_eq, b_eq)]
            if A_ub is not None:
                constraints.append(LinearConstraint(A_ub.tocsr(), -np.inf, b_ub))
            result = milp(c_obj, constraints=constraints, integrality=integrality,
                         bounds=Bounds(lb, ub), options={'time_limit': 30.0})
            solver_label = 'lp/scipy-milp'
        else:
            result = linprog(c_obj,
                             A_ub=(A_ub.tocsr() if A_ub is not None else None),
                             b_ub=b_ub,
                             A_eq=A_eq.tocsr(), b_eq=b_eq,
                             bounds=list(zip(lb.tolist(), ub.tolist())),
                             method='highs', options={'time_limit': 30.0})
            solver_label = 'lp/scipy'

        if result.status not in (0, 1):
            raise RuntimeError(f"scipy solve status {result.status}: {result.message}")

        x = result.x
        soc_vals = x[S:S+T]
        schedule = []
        total_import_kwh = total_export_kwh = 0.0
        total_import_cost = total_export_credit = 0.0

        # Per-hour tranche split for capped hours, keyed by hour: (free_kwh, over_kwh,
        # free_rate, over_rate). Used below to report the true blended cost/rate instead
        # of the flat r_imp[t]/r_exp[t] (which only reflects the free-tier rate).
        import_tranche = {}
        export_tranche = {}
        for cb in cap_blocks:
            rates = r_imp if cb["direction"] == "import" else r_exp
            target = import_tranche if cb["direction"] == "import" else export_tranche
            for j, t in enumerate(cb["hours"]):
                free_val = max(0.0, x[cb["free_idx0"] + j])
                over_val = max(0.0, x[cb["over_idx0"] + j])
                target[t] = (free_val, over_val, rates[t], cb["rate_after_cap"])

        for t in range(T):
            i_raw = max(0.0, x[I+t])
            e = max(0.0, x[X+t])
            # Sum across all devices for this hour (for schedule display only)
            deferred = sum(max(0.0, x[(5+i)*T+t]) for i in range(N))
            # i_raw already reflects all grid import (including deferrable shortfall
            # when solar is insufficient) because def_i is in the energy balance.
            i = i_raw
            # Net out simultaneous import/export
            if i > 1e-6 and e > 1e-6:
                net = i - e
                i, e = (net, 0.0) if net >= 0 else (0.0, -net)
            ch = max(0.0, x[C+t])
            di = max(0.0, x[D+t])
            so = max(self.min_soc_kwh, min(self.max_soc_kwh, soc_vals[t]))

            # Capped hours: cost/rate come from the tranche split (pre-netting values —
            # more accurate than post-net flat-rate multiplication, and the only way to
            # correctly price an hour where the day's cap boundary falls mid-hour).
            imp_free = imp_over = exp_free = exp_over = 0.0
            imp_free_rate = imp_over_rate = exp_free_rate = exp_over_rate = 0.0
            if t in import_tranche:
                imp_free, imp_over, imp_free_rate, imp_over_rate = import_tranche[t]
                ic = imp_free * imp_free_rate + imp_over * imp_over_rate
                imp_rate_out = ic / (imp_free + imp_over) if (imp_free + imp_over) > 1e-9 else imp_free_rate
            else:
                ic = i * r_imp[t]
                imp_rate_out = r_imp[t]
            if t in export_tranche:
                exp_free, exp_over, exp_free_rate, exp_over_rate = export_tranche[t]
                ec = exp_free * exp_free_rate + exp_over * exp_over_rate
                exp_rate_out = ec / (exp_free + exp_over) if (exp_free + exp_over) > 1e-9 else exp_free_rate
            else:
                ec = e * r_exp[t]
                exp_rate_out = r_exp[t]

            total_import_kwh   += i;  total_export_kwh    += e
            total_import_cost  += ic; total_export_credit += ec
            schedule.append({
                'hour': t, 'solar_kwh': solar[t], 'load_kwh': load[t],
                'charge_kwh': ch, 'discharge_kwh': di,
                'import_kwh': i, 'export_kwh': e,
                'deferrable_kwh': deferred,
                'deferrable_per_device': [max(0.0, x[(5+ii)*T+t]) for ii in range(N)],
                'soc_percent': so / self.capacity_kwh * 100.0,
                'import_rate': imp_rate_out, 'export_rate': exp_rate_out,
                'import_cost': ic, 'export_credit': ec,
                'import_cap_free_kwh': imp_free, 'import_cap_over_kwh': imp_over,
                'import_cap_free_rate': imp_free_rate, 'import_cap_over_rate': imp_over_rate,
                'export_cap_free_kwh': exp_free, 'export_cap_over_kwh': exp_over,
                'export_cap_free_rate': exp_free_rate, 'export_cap_over_rate': exp_over_rate,
            })

        if N:
            protected_hours = (
                set(demand_hours)
                | {t for cb in cap_blocks for t in cb["hours"]}
                | {t for cb2 in credit_blocks for t in cb2["hours"]}
            )
            consolidate_deferrable_schedule(
                schedule, deferrable_loads, dt=dt, slots_per_day=slots_per_day,
                protected_hours=protected_hours,
            )
            total_import_kwh = sum(r['import_kwh'] for r in schedule)
            total_export_kwh = sum(r['export_kwh'] for r in schedule)
            total_import_cost = sum(r['import_cost'] for r in schedule)
            total_export_credit = sum(r['export_credit'] for r in schedule)

        _LOGGER.warning(
            "%s solved %d hours, %d deferrable devices %s, status=%s",
            solver_label, T, N,
            [(f"{d['daily_kwh']:.1f}kWh/d@{d['max_kw']}kW") for d in deferrable_loads],
            result.status,
        )
        if min_export_price > 0:
            floored_hours = sum(1 for r in r_exp if r < min_export_price)
            _LOGGER.warning(
                "min export price floor: $%.3f/kWh, %d/%d hours below floor "
                "(unrewarded in objective, still export if nothing else absorbs surplus)",
                min_export_price, floored_hours, T,
            )
        conditional_credit_totals: dict = {}
        for cb2 in credit_blocks:
            earned = x[cb2["y_idx"]] > 0.5
            entry = conditional_credit_totals.setdefault(cb2["label"], {
                "days_earned": 0, "days_total": 0, "amount": 0.0,
                "amount_per_day": cb2["amount_per_day"],
            })
            entry["days_total"] += 1
            if earned:
                entry["days_earned"] += 1
                entry["amount"] += cb2["amount_per_day"]
        if credit_blocks:
            _LOGGER.warning("conditional credits: %s", conditional_credit_totals)
        if cap_blocks:
            for cb in cap_blocks:
                target = import_tranche if cb["direction"] == "import" else export_tranche
                free_total = sum(v[0] for t, v in target.items() if t in cb["hours"])
                over_total = sum(v[1] for t, v in target.items() if t in cb["hours"])
                _LOGGER.warning(
                    "cap block %s: %d hours, daily_cap=%.1f, rate_after_cap=%.3f, "
                    "free=%.2fkWh, over_cap=%.2fkWh",
                    cb["direction"], len(cb["hours"]), cb["daily_cap_kwh"],
                    cb["rate_after_cap"], free_total, over_total,
                )
        return {
            'schedule':            schedule,
            'total_import_kwh':    total_import_kwh,
            'total_export_kwh':    total_export_kwh,
            'total_import_cost':   total_import_cost,
            'total_export_credit': total_export_credit,
            'net_cost':            total_import_cost - total_export_credit,
            'final_soc_percent':   max(0.0, soc_vals[T-1]) / self.capacity_kwh * 100.0,
            'demand_peak_kw':      (max(0.0, x[P_idx]) if demand_active else None),
            'conditional_credits': conditional_credit_totals,
            'solver':              solver_label,
        }

    # ---- HiGHS (preferred — ships its own binary) ----

    def _lp_highspy(self, solar, load, r_imp, r_exp, E0, T, deferrable_loads=None):
        from highspy import Highs  # type: ignore

        h = Highs()
        h.setOptionValue("output_flag", False)
        h.setOptionValue("time_limit", 120.0)

        eta = self.eta
        INF = 1e30

        # Variable layout: [imp(T) | exp(T) | cha(T) | dis(T) | soc(T) | z(T)]
        # z[t] is a binary variable: 1 = grid importing, 0 = grid exporting.
        # This enforces mutual exclusivity of import and export in the same hour,
        # preventing the LP from exploiting plans where FiT > off-peak import rate
        # by simultaneously buying and selling (which is physically impossible on a
        # single-phase connection).
        I, X, C, D, S, Z = 0, T, 2*T, 3*T, 4*T, 5*T

        # Big-M: safe upper bound on any single-hour grid flow (kWh).
        # A 10 kW single-phase connection can pass at most 10 kWh/hour.
        M = max(self.max_charge_rate_kw, self.max_discharge_rate_kw) * 3.0

        lb = [0.0] * (6 * T)
        ub = [INF] * (6 * T)
        costs = [0.0] * (6 * T)
        for t in range(T):
            costs[I + t] =  r_imp[t]
            costs[X + t] = -r_exp[t]
            ub[C + t] = self.max_charge_rate_kw
            ub[D + t] = self.max_discharge_rate_kw
            lb[S + t] = self.min_soc_kwh
            ub[S + t] = self.max_soc_kwh
            ub[Z + t] = 1.0  # binary: 0 or 1

        h.addVars(6 * T, lb, ub)
        h.changeColsCostByRange(0, 6 * T - 1, costs)

        # Mark z[t] variables as integer (binary since bounds are 0 and 1)
        for t in range(T):
            h.changeColIntegrality(Z + t, 1)  # 1 = kInteger

        for t in range(T):
            # Energy balance: imp + dis - exp - cha = load - solar
            rhs = load[t] - solar[t]
            h.addRow(rhs, rhs, 4,
                     [I+t, X+t, C+t, D+t],
                     [1.0, -1.0, -1.0, 1.0])

        for t in range(T):
            # SOC update: soc[t] - eta*cha[t] + (1/eta)*dis[t] - soc[t-1] = 0
            # (for t=0: soc[t-1] = E0, moved to RHS)
            if t == 0:
                h.addRow(E0, E0, 3,
                         [S+t, C+t, D+t],
                         [1.0, -eta, 1.0/eta])
            else:
                h.addRow(0.0, 0.0, 4,
                         [S+t, S+t-1, C+t, D+t],
                         [1.0, -1.0, -eta, 1.0/eta])

        for t in range(T):
            # imp[t] <= M * z[t]  →  imp[t] - M*z[t] <= 0
            h.addRow(-INF, 0.0, 2, [I+t, Z+t], [1.0, -M])
            # exp[t] <= M * (1 - z[t])  →  exp[t] + M*z[t] <= M
            h.addRow(-INF, M, 2, [X+t, Z+t], [1.0, M])

        # Terminal SOC: battery must end no emptier than it started.
        E_end = min(max(E0, self.min_soc_kwh), self.max_soc_kwh)
        h.addRow(E_end, INF, 1, [S+T-1], [1.0])

        h.run()

        status_str = str(h.getModelStatus())
        if "Optimal" not in status_str and "Feasible" not in status_str:
            raise RuntimeError(f"HiGHS status: {status_str}")

        vals = list(h.getSolution().col_value)
        _LOGGER.info("HiGHS MILP solved %d hours, status=%s", T, status_str)

        return self._build_result_from_arrays(
            T, solar, load, r_imp, r_exp,
            imp=[vals[I+t] for t in range(T)],
            exp=[vals[X+t] for t in range(T)],
            cha=[vals[C+t] for t in range(T)],
            dis=[vals[D+t] for t in range(T)],
            soc=[vals[S+t] for t in range(T)],
            solver="milp/highs",
        )

    # ---- PuLP / CBC fallback ----

    def _lp_pulp(self, solar, load, r_imp, r_exp, E0, T, deferrable_loads=None):
        import pulp

        prob = pulp.LpProblem("battery", pulp.LpMinimize)
        eta = self.eta
        M = max(self.max_charge_rate_kw, self.max_discharge_rate_kw) * 3.0

        P_imp = [pulp.LpVariable(f"imp_{t}", lowBound=0) for t in range(T)]
        P_exp = [pulp.LpVariable(f"exp_{t}", lowBound=0) for t in range(T)]
        P_cha = [pulp.LpVariable(f"cha_{t}", 0, self.max_charge_rate_kw) for t in range(T)]
        P_dis = [pulp.LpVariable(f"dis_{t}", 0, self.max_discharge_rate_kw) for t in range(T)]
        E_bat = [pulp.LpVariable(f"soc_{t}", self.min_soc_kwh, self.max_soc_kwh) for t in range(T)]
        z     = [pulp.LpVariable(f"z_{t}", cat='Binary') for t in range(T)]

        prob += pulp.lpSum(r_imp[t]*P_imp[t] - r_exp[t]*P_exp[t] for t in range(T))

        for t in range(T):
            prob += P_imp[t] + solar[t] + P_dis[t] == load[t] + P_cha[t] + P_exp[t]
            E_prev = E0 if t == 0 else E_bat[t-1]
            prob += E_bat[t] == E_prev + eta*P_cha[t] - P_dis[t]/eta
            # Mutual exclusivity: import and export cannot both be non-zero
            prob += P_imp[t] <= M * z[t]
            prob += P_exp[t] <= M * (1 - z[t])

        # Terminal SOC: battery must end no emptier than it started.
        prob += E_bat[T-1] >= min(max(E0, self.min_soc_kwh), self.max_soc_kwh)

        status = prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=120))
        if pulp.LpStatus[status] not in ("Optimal", "Feasible"):
            raise RuntimeError(f"PuLP/CBC status: {pulp.LpStatus[status]}")

        def v(var):
            val = pulp.value(var)
            return max(0.0, val) if val is not None else 0.0

        _LOGGER.info("PuLP/CBC MILP solved %d hours", T)
        return self._build_result_from_arrays(
            T, solar, load, r_imp, r_exp,
            imp=[v(P_imp[t]) for t in range(T)],
            exp=[v(P_exp[t]) for t in range(T)],
            cha=[v(P_cha[t]) for t in range(T)],
            dis=[v(P_dis[t]) for t in range(T)],
            soc=[v(E_bat[t]) for t in range(T)],
            solver="milp/cbc",
        )

    def _build_result_from_arrays(self, T, solar, load, r_imp, r_exp,
                                   imp, exp, cha, dis, soc, solver):
        schedule = []
        total_import_kwh = total_export_kwh = 0.0
        total_import_cost = total_export_credit = 0.0

        for t in range(T):
            i, e, c, d, s = (max(0.0, x) for x in (imp[t], exp[t], cha[t], dis[t], soc[t]))
            ic = i * r_imp[t]
            ec = e * r_exp[t]
            total_import_kwh   += i;  total_export_kwh    += e
            total_import_cost  += ic; total_export_credit += ec
            schedule.append({
                'hour': t, 'solar_kwh': solar[t], 'load_kwh': load[t],
                'charge_kwh': c, 'discharge_kwh': d,
                'import_kwh': i, 'export_kwh': e,
                'soc_percent': s / self.capacity_kwh * 100.0,
                'import_rate': r_imp[t], 'export_rate': r_exp[t],
                'import_cost': ic, 'export_credit': ec,
            })

        return {
            'schedule':            schedule,
            'total_import_kwh':    total_import_kwh,
            'total_export_kwh':    total_export_kwh,
            'total_import_cost':   total_import_cost,
            'total_export_credit': total_export_credit,
            'net_cost':            total_import_cost - total_export_credit,
            'final_soc_percent':   max(0.0, soc[T-1]) / self.capacity_kwh * 100.0,
            'solver':              solver,
        }

    # ------------------------------------------------------------------
    # Greedy fallback
    # ------------------------------------------------------------------

    def _greedy_optimize(self, solar, load, r_imp, r_exp, E0, T, timestep_hours=1.0):
        avg_imp = sum(r_imp) / T if T else 0.15
        avg_exp = sum(r_exp) / T if T else 0.05
        eta = self.eta
        dt = timestep_hours  # per-slot energy caps = rated power × dt
        max_cha = self.max_charge_rate_kw * dt
        max_dis = self.max_discharge_rate_kw * dt

        soc_kwh = E0
        schedule = []
        total_import_kwh = total_export_kwh = 0.0
        total_import_cost = total_export_credit = 0.0

        for t in range(T):
            net = solar[t] - load[t]
            cha = dis = imp = exp = 0.0

            fit_profitable = r_exp[t] > 0 and r_exp[t] > avg_imp * 0.9

            if fit_profitable:
                # Profitable FiT window: discharge battery to maximise export.
                can_dis = min(max_dis,
                              (soc_kwh - self.min_soc_kwh) * eta)
                dis = max(0.0, can_dis)
                available = net + dis          # solar surplus + battery
                if available >= 0:
                    exp = available
                else:
                    imp = -available           # can't fully cover load from battery
                    exp = 0.0
            elif net >= 0:
                if r_exp[t] < avg_exp * 0.9:
                    can_charge = min(net, max_cha,
                                     (self.max_soc_kwh - soc_kwh) / eta)
                    cha = max(0.0, can_charge)
                    exp = net - cha
                else:
                    exp = net
            else:
                deficit = -net
                if r_imp[t] > avg_imp * 1.1:
                    can_dis = min(deficit, max_dis,
                                  (soc_kwh - self.min_soc_kwh) * eta)
                    dis = max(0.0, can_dis)
                    imp = deficit - dis
                else:
                    imp = deficit

            soc_kwh = max(self.min_soc_kwh,
                          min(self.max_soc_kwh, soc_kwh + eta * cha - dis / eta))

            imp_cost   = imp * r_imp[t]
            exp_credit = exp * r_exp[t]
            total_import_kwh   += imp
            total_export_kwh   += exp
            total_import_cost  += imp_cost
            total_export_credit += exp_credit

            schedule.append({
                'hour': t, 'solar_kwh': solar[t], 'load_kwh': load[t],
                'charge_kwh': cha, 'discharge_kwh': dis,
                'import_kwh': imp, 'export_kwh': exp,
                'soc_percent': soc_kwh / self.capacity_kwh * 100.0,
                'import_rate': r_imp[t], 'export_rate': r_exp[t],
                'import_cost': imp_cost, 'export_credit': exp_credit,
            })

        return {
            'schedule':            schedule,
            'total_import_kwh':    total_import_kwh,
            'total_export_kwh':    total_export_kwh,
            'total_import_cost':   total_import_cost,
            'total_export_credit': total_export_credit,
            'net_cost':            total_import_cost - total_export_credit,
            'final_soc_percent':   soc_kwh / self.capacity_kwh * 100.0,
            'solver':              'greedy',
        }

    def _empty_result(self):
        return {
            'schedule': [], 'total_import_kwh': 0.0, 'total_export_kwh': 0.0,
            'total_import_cost': 0.0, 'total_export_credit': 0.0,
            'net_cost': 0.0, 'final_soc_percent': 50.0, 'solver': 'none',
        }
