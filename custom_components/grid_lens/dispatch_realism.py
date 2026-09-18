"""Execution-realism filter for LP-optimised battery schedules.

The plan-comparison LP (``battery_optimizer.py``) finds the cost-minimal dispatch for
a tariff on the assumption that every kWh it schedules actually gets drawn or sold. In
real operation it doesn't: ``control/executor.py`` refuses to command a real grid
force-charge, or a forced "battery first" export, unless the grid/export share of that
slot is *material* — below ``GRID_CHARGE_MIN_W``/``GRID_CHARGE_MIN_FRACTION`` (charge)
or ``EXPORT_MIN_W``/``EXPORT_MIN_FRACTION`` (discharge) it runs plain self-consumption
instead, and the extra grid draw or forced sale never happens on the real battery (see
that module's docstring — the thresholds exist to stop a tiny LP grid/export nibble
from throttling a much larger solar charge/self-consumption discharge, the "10 kW
import-spike bug").

Without this filter, an alternative plan's projected cost can price in dispatch
behaviour Grid Lens's own live controller would refuse to execute — found 2026-09-18
comparing AGL Battery Rewards (current plan, actual usage) against Origin Battery
Maximiser (LP-optimised): the LP proposed several sub-threshold grid-charge slivers to
top the battery up before Origin's 22c evening export window, inflating projected grid
import ~3x over what the household actually drew. See GRIDLENS_CHECKLIST.md for the
full writeup.

These thresholds are duplicated from, not imported from, ``control/executor.py``:
that module operates on watts/``DispatchInterval`` for live, safety-critical control;
this one operates on kWh/schedule-step dicts for offline comparison. The four
threshold *values* are re-exported from here and imported by ``control/executor.py``
so they can't drift apart numerically — but the decision logic itself is intentionally
re-stated per module rather than shared, so a change to live dispatch behaviour can
never silently alter a past comparison's LP semantics or vice versa.

Known limitation: this is a post-hoc pass over energy/cost totals, not a re-solve —
it does not re-derive the SOC trajectory, so a chart built from the corrected schedule
may show a SOC curve very slightly inconsistent with the corrected charge/discharge
figures in a slot that got downgraded. Correcting that would mean encoding the
executor's materiality thresholds as constraints in the LP itself (binary "is this
slot material" decision variables) rather than filtering its output — a much larger
change, tracked in OPEN_ITEMS.md if the SOC-curve inconsistency turns out to matter in
practice. Capped-rate plans (e.g. GloBird ZEROHERO's daily free-import window) are
also out of scope for the same reason: reconciling this filter with the free/over-cap
tranche split it doesn't know about would risk corrupting that accounting, so a step
carrying any capped-rate tranche is passed through unfiltered.
"""
from __future__ import annotations

GRID_CHARGE_MIN_W = 250.0
GRID_CHARGE_MIN_FRACTION = 0.5
EXPORT_MIN_W = 250.0
EXPORT_MIN_FRACTION = 0.5
FREE_RATE_EPS = 1e-6


def realize_schedule(schedule: list[dict], dt_h: float = 1.0) -> list[dict]:
    """Return a copy of an LP schedule with each step's import/export/charge/discharge
    reduced to what ``control.executor.ScheduleExecutor`` would actually command.

    Steps with no charge or discharge, and steps carrying a capped-rate tranche
    (``import_cap_free_kwh``/``import_cap_over_kwh``/``export_cap_free_kwh``/
    ``export_cap_over_kwh``), are returned unchanged.
    """
    charge_floor = GRID_CHARGE_MIN_W / 1000.0 * dt_h
    export_floor = EXPORT_MIN_W / 1000.0 * dt_h
    out: list[dict] = []
    for step in schedule:
        capped = any(
            step.get(k, 0.0) for k in (
                "import_cap_free_kwh", "import_cap_over_kwh",
                "export_cap_free_kwh", "export_cap_over_kwh",
            )
        )
        charge = float(step.get("charge_kwh", 0.0))
        discharge = float(step.get("discharge_kwh", 0.0))
        imp = float(step.get("import_kwh", 0.0))
        exp = float(step.get("export_kwh", 0.0))

        if capped or (charge <= 1e-9 and discharge <= 1e-9):
            out.append(step)
            continue

        load = float(step.get("load_kwh", 0.0))
        deferrable = float(step.get("deferrable_kwh", 0.0))
        import_rate = step.get("import_rate")
        is_free = import_rate is not None and import_rate <= FREE_RATE_EPS

        new_step = dict(step)

        if charge > 1e-9:
            # Mirrors control/executor.py's _grid_charge_w: import beyond what house
            # load + deferrable devices consume must be feeding the battery.
            grid_to_battery = min(max(0.0, imp - (load + deferrable)), charge)
            material = (
                grid_to_battery > charge_floor
                and grid_to_battery >= GRID_CHARGE_MIN_FRACTION * charge
            )
            if not is_free and not material:
                # _resolve_charge falls through to self-consumption: the grid
                # contribution to charging never happens.
                new_step["charge_kwh"] = max(0.0, charge - grid_to_battery)
                new_step["import_kwh"] = max(0.0, imp - grid_to_battery)

        elif discharge > 1e-9:
            # Mirrors control/executor.py's _export_w: the battery's share of a
            # discharge slot's export, capped at what it actually discharges.
            battery_export = min(max(0.0, exp), discharge)
            material = (
                battery_export > export_floor
                and battery_export >= EXPORT_MIN_FRACTION * discharge
            )
            if not material:
                if is_free:
                    # _resolve_discharge goes IDLE: the battery holds charge and the
                    # load it was covering falls through to (free) import instead.
                    load_covering = discharge - battery_export
                    new_step["discharge_kwh"] = 0.0
                    new_step["export_kwh"] = max(0.0, exp - battery_export)
                    new_step["import_kwh"] = imp + load_covering
                else:
                    # _resolve_discharge falls through to self-consumption: discharge
                    # only ever matches the load/solar gap, so any forced-export
                    # component is dropped.
                    new_step["discharge_kwh"] = max(0.0, discharge - battery_export)
                    new_step["export_kwh"] = max(0.0, exp - battery_export)

        # Re-derive cost/credit from the corrected energy at the step's own rate so a
        # line's amount still equals its own kwh x rate (see plan_calculator.py's "no
        # reconciliation plug" note in _compute_bill_items).
        if new_step["import_kwh"] != imp:
            new_step["import_cost"] = new_step["import_kwh"] * (import_rate or 0.0)
        if new_step["export_kwh"] != exp:
            new_step["export_credit"] = new_step["export_kwh"] * (step.get("export_rate") or 0.0)

        out.append(new_step)
    return out
