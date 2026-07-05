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
    ) -> Dict:
        """Return an optimal hourly schedule minimising net energy cost.

        deferrable_loads is a list of per-device dicts, each with:
          'daily_kwh': float  — energy the device must consume per day
          'max_kw':    float  — maximum power draw per hour for that device
          'hour_mask': optional list[int] of length T (1 = device available at
                       that LP hour, 0 = unavailable).  Missing/None = always
                       available.  Built by the caller from local hour-of-day.

        Each device gets its own LP variable with its own power cap, so a
        1.8 kW EV charger and a 4.7 kW hot water system are scheduled
        independently and cannot exceed their rated power in any single hour.

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

        try:
            return self._lp_optimize(solar, load, r_imp, r_exp, E0, T, deferrable_loads)
        except ImportError:
            _LOGGER.warning(
                "PuLP not yet installed — using greedy fallback. "
                "Restart HA again after the first boot to enable LP optimisation."
            )
            return self._greedy_optimize(solar, load, r_imp, r_exp, E0, T)
        except Exception as exc:
            _LOGGER.warning("LP optimisation failed (%s) — using greedy fallback.", exc)
            return self._greedy_optimize(solar, load, r_imp, r_exp, E0, T)

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

    def _lp_optimize(self, solar, load, r_imp, r_exp, E0, T, deferrable_loads):
        """Build and solve the LP. Raises on failure so caller can fall back."""
        try:
            return self._lp_highspy(solar, load, r_imp, r_exp, E0, T, deferrable_loads)
        except ImportError:
            pass  # highspy not installed — try scipy
        except Exception as exc:
            _LOGGER.warning("HiGHS MILP failed (%s) — trying scipy", exc)

        try:
            return self._lp_scipy(solar, load, r_imp, r_exp, E0, T, deferrable_loads)
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
    # (OVO free period, Glowbird overnight) the solver may simultaneously
    # import and export.  We prevent the problem becoming unbounded by capping
    # import at a physical grid limit (M), then post-process to net any
    # simultaneous import/export to a single direction.

    def _lp_scipy(self, solar, load, r_imp, r_exp, E0, T, deferrable_loads):
        import numpy as np
        from scipy.optimize import linprog
        from scipy.sparse import lil_matrix

        eta = self.eta
        M = (self.max_charge_rate_kw + self.max_discharge_rate_kw) * 2.0
        N = len(deferrable_loads)       # number of individual deferrable devices
        hours_per_day = 24

        # Variable layout:
        #   [imp(T) | exp(T) | cha(T) | dis(T) | soc(T) | def_0(T) | def_1(T) | ... | def_{N-1}(T)]
        # Each device i has its own block of T variables starting at (5+i)*T.
        I, X, C, D, S = 0, T, 2*T, 3*T, 4*T
        n = (5 + N) * T

        c_obj = np.zeros(n)
        c_obj[I:I+T] = r_imp
        c_obj[X:X+T] = [-r for r in r_exp]
        # def_i has NO direct cost in the objective.  Its cost is implicit:
        # when solar is sufficient, def_i reduces exp → opportunity cost = r_exp[t];
        # when solar is insufficient, def_i increases imp → cost = r_imp[t].
        # This lets the LP correctly prefer solar over grid for deferrable loads.

        lb = np.zeros(n)
        ub = np.full(n, np.inf)
        ub[I:I+T] = M
        ub[C:C+T] = self.max_charge_rate_kw
        ub[D:D+T] = self.max_discharge_rate_kw
        lb[S:S+T] = self.min_soc_kwh
        ub[S:S+T] = self.max_soc_kwh
        for i, dev in enumerate(deferrable_loads):
            # Each device's per-hour draw is capped at its own rated max kW,
            # and forced to 0 in hours the device is unavailable (hour_mask).
            mask = dev.get('hour_mask')
            if mask:
                for t in range(T):
                    ub[(5+i)*T+t] = dev['max_kw'] if mask[t] else 0.0
            else:
                ub[(5+i)*T:(5+i)*T+T] = dev['max_kw']

        # Equality constraints: T (energy balance) + T (SOC update) + N*n_days (per-device daily totals)
        n_days = math.ceil(T / hours_per_day)
        n_eq = 2*T + N * n_days
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
        for i, dev in enumerate(deferrable_loads):
            mask = dev.get('hour_mask')
            for d in range(n_days):
                t0 = d * hours_per_day
                t1 = min(t0 + hours_per_day, T)
                row = 2*T + i * n_days + d
                for t in range(t0, t1):
                    A_eq[row, (5+i)*T+t] = 1.0
                avail_hours = (
                    sum(1 for t in range(t0, t1) if mask[t]) if mask else (t1 - t0)
                )
                target = dev['daily_kwh'] * (t1 - t0) / hours_per_day
                b_eq[row] = min(target, avail_hours * dev['max_kw'])

        # Terminal SOC: the battery must end the window at least as full as it
        # started, so its initial charge is a loan, not free energy.  Clamped to
        # the SOC bounds in case the reported initial SOC lies outside them.
        # linprog uses A_ub x ≤ b_ub, so encode soc[T-1] ≥ E_end as -soc[T-1] ≤ -E_end.
        E_end = min(max(E0, self.min_soc_kwh), self.max_soc_kwh)
        A_ub = lil_matrix((1, n))
        A_ub[0, S+T-1] = -1.0
        b_ub = np.array([-E_end])

        result = linprog(c_obj, A_ub=A_ub.tocsr(), b_ub=b_ub,
                         A_eq=A_eq.tocsr(), b_eq=b_eq,
                         bounds=list(zip(lb.tolist(), ub.tolist())),
                         method='highs', options={'time_limit': 30.0})

        if result.status not in (0, 1):
            raise RuntimeError(f"scipy linprog status {result.status}: {result.message}")

        x = result.x
        soc_vals = x[S:S+T]
        schedule = []
        total_import_kwh = total_export_kwh = 0.0
        total_import_cost = total_export_credit = 0.0

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
            ic = i * r_imp[t]
            ec = e * r_exp[t]
            total_import_kwh   += i;  total_export_kwh    += e
            total_import_cost  += ic; total_export_credit += ec
            schedule.append({
                'hour': t, 'solar_kwh': solar[t], 'load_kwh': load[t],
                'charge_kwh': ch, 'discharge_kwh': di,
                'import_kwh': i, 'export_kwh': e,
                'deferrable_kwh': deferred,
                'deferrable_per_device': [max(0.0, x[(5+ii)*T+t]) for ii in range(N)],
                'soc_percent': so / self.capacity_kwh * 100.0,
                'import_rate': r_imp[t], 'export_rate': r_exp[t],
                'import_cost': ic, 'export_credit': ec,
            })

        _LOGGER.warning(
            "scipy LP solved %d hours, %d deferrable devices %s, status=%s",
            T, N,
            [(f"{d['daily_kwh']:.1f}kWh/d@{d['max_kw']}kW") for d in deferrable_loads],
            result.status,
        )
        return {
            'schedule':            schedule,
            'total_import_kwh':    total_import_kwh,
            'total_export_kwh':    total_export_kwh,
            'total_import_cost':   total_import_cost,
            'total_export_credit': total_export_credit,
            'net_cost':            total_import_cost - total_export_credit,
            'final_soc_percent':   max(0.0, soc_vals[T-1]) / self.capacity_kwh * 100.0,
            'solver':              'lp/scipy',
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

    def _greedy_optimize(self, solar, load, r_imp, r_exp, E0, T):
        avg_imp = sum(r_imp) / T if T else 0.15
        avg_exp = sum(r_exp) / T if T else 0.05
        eta = self.eta

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
                can_dis = min(self.max_discharge_rate_kw,
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
                    can_charge = min(net, self.max_charge_rate_kw,
                                     (self.max_soc_kwh - soc_kwh) / eta)
                    cha = max(0.0, can_charge)
                    exp = net - cha
                else:
                    exp = net
            else:
                deficit = -net
                if r_imp[t] > avg_imp * 1.1:
                    can_dis = min(deficit, self.max_discharge_rate_kw,
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
