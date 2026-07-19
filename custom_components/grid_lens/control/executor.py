"""ScheduleExecutor — turns an optimizer dispatch plan into battery commands (spec §4).

The optimizer produces a **plan**: a time-ordered list of ``DispatchInterval`` s. This
executor ticks on interval boundaries, looks up the interval covering *now*, and issues
the corresponding :class:`BatteryController` command. It owns three responsibilities:

* **Transition economy** — self-consumption is only (re)issued on transition; active modes
  (charge/discharge/idle) are re-issued each tick so the guardrail's auto-expiry stays
  armed and power tracks the plan.
* **Watchdog** — if the plan goes stale (the optimizer stopped refreshing it), revert to
  native control once and stay degraded until a fresh plan arrives.
* **Deadman** — :meth:`stop` (and unload) calls ``restore_normal`` so turning the system
  off always hands the battery back to native/VPP control.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from ..inverters.base import BatteryAction
from .battery_controller import BatteryController

_LOGGER = logging.getLogger(__name__)


@dataclass
class DispatchInterval:
    """One planned action starting at ``start`` (until the next interval's start).

    ``power_w`` is the *total* battery charge/discharge rate the plan allocated to this
    slot (used for display / the SOC trajectory). ``grid_charge_w`` is the portion of a
    CHARGE slot that the plan intends to source **from the grid** (import beyond house +
    deferrable load). It is 0 for solar-only charge slots and for non-charge slots.

    The executor uses ``grid_charge_w`` — NOT ``power_w`` — to decide *how* to charge:

    * solar charge (``grid_charge_w`` immaterial — see ``_resolve_charge``): charge via
      self-consumption, which pulls the battery up from surplus PV at the hardware-max rate
      and **never imports grid to charge**. Commanding ``force_charge(power_w)`` here
      (PV-first at the full rate) would make the inverter import whenever instantaneous PV
      surplus < ``power_w`` — the 10 kW import-spike bug — and a *tiny* ``grid_charge_w``
      passed to ``force_charge`` would instead cap total charge power at that tiny value,
      throttling the solar charge and dumping surplus PV to a $0 export.
    * material grid charge (grid a real share of the slot): ``force_charge(grid_charge_w)``
      so the grid rate cap is the planned grid contribution, never the full charge rate.
    """

    start: datetime
    action: BatteryAction
    power_w: float = 0.0
    grid_charge_w: float = 0.0


@dataclass
class _ExecStatus:
    enabled: bool = False
    degraded: bool = False
    last_tick: Optional[datetime] = None
    # None until the first command lands — the battery's real mode is unknown at start
    # (could be a leftover forced mode), so the first tick must always issue a command.
    applied_action: Optional[BatteryAction] = None
    applied_power_w: float = 0.0
    applied_at: Optional[datetime] = None  # timestamp of the last applied command
    plan_intervals: int = 0
    plan_updated_at: Optional[datetime] = None
    note: str = "not_started"


class ScheduleExecutor:
    def __init__(
        self,
        hass: HomeAssistant,
        battery_controller: BatteryController,
        *,
        interval_minutes: int = 5,
        max_plan_age_minutes: float = 30.0,
    ) -> None:
        self.hass = hass
        self.bc = battery_controller
        self.interval_minutes = max(1, int(interval_minutes))
        self.max_plan_age = timedelta(minutes=max_plan_age_minutes)

        self._plan: Optional[list[DispatchInterval]] = None
        self._plan_updated_at: Optional[datetime] = None
        self._cancel_timer: Optional[Callable] = None
        self._status = _ExecStatus()

    # ------------------------------------------------------------------ plan feed
    def set_plan(
        self, intervals: list[DispatchInterval], updated_at: Optional[datetime] = None
    ) -> None:
        """Install a fresh plan (called after each optimizer run). Clears degraded state."""
        self._plan = sorted(intervals, key=lambda i: i.start)
        self._plan_updated_at = updated_at or dt_util.now()
        self._status.plan_intervals = len(self._plan)
        self._status.plan_updated_at = self._plan_updated_at
        if self._status.degraded:
            _LOGGER.info("Fresh plan received — clearing degraded state")
        self._status.degraded = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._status.enabled:
            return
        # Fire on interval-aligned minutes (:00, :05, … for interval_minutes=5).
        minutes = list(range(0, 60, self.interval_minutes))
        self._cancel_timer = async_track_time_change(
            self.hass, self._tick, minute=minutes, second=0
        )
        self._status.enabled = True
        self._status.note = "running"
        _LOGGER.info("ScheduleExecutor started (interval=%dmin)", self.interval_minutes)
        await self._tick()  # act immediately rather than waiting for the next boundary

    async def stop(self, restore_normal: bool = True) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        self._status.enabled = False
        self._status.note = "stopped"
        if restore_normal:
            await self.bc.restore_normal()  # deadman
            # None, not SELF_USE: native handback isn't the same real-world state as a
            # GridLens-commanded self-use (Remote EMS stays enabled for the latter), so the
            # next start() must always re-issue on its first tick rather than "hold".
            self._status.applied_action = None
            self._status.applied_power_w = 0.0

    # ------------------------------------------------------------------ core tick
    async def _tick(self, now: Optional[datetime] = None) -> None:
        now = now or dt_util.now()
        self._status.last_tick = now
        try:
            if self._plan is None:
                self._status.note = "no_plan"
                return  # nothing to execute yet — leave the battery as-is

            if self._plan_is_stale(now):
                await self._enter_safe_state("plan stale")
                return

            action, power_w = self._desired(now)

            # Transition economy: don't re-spam self-consumption every tick.
            if (
                action == BatteryAction.SELF_USE
                and self._status.applied_action == BatteryAction.SELF_USE
                and not self._status.degraded
            ):
                self._status.note = "holding_self_use"
                return

            if await self._apply(action, power_w):
                self._status.applied_action = action
                self._status.applied_power_w = power_w
                self._status.applied_at = dt_util.now()
                self._status.degraded = False
                self._status.note = f"applied_{action.value}"
        except Exception as err:  # noqa: BLE001 — a bad tick must not kill the timer
            _LOGGER.error("ScheduleExecutor tick failed: %s", err)
            self._status.note = f"tick_error:{err}"

    async def _apply(self, action: BatteryAction, power_w: float) -> bool:
        # Auto-expiry set to two intervals so a single missed tick doesn't drop the mode.
        duration = self.interval_minutes * 2
        if action == BatteryAction.CHARGE:
            return await self.bc.force_charge(power_w, duration)
        if action == BatteryAction.DISCHARGE:
            return await self.bc.force_discharge(power_w, duration)
        if action == BatteryAction.IDLE:
            return await self.bc.set_idle(duration)
        return await self.bc.set_self_consumption_mode()

    async def _enter_safe_state(self, reason: str) -> None:
        if self._status.degraded:
            return  # already handed back to native
        _LOGGER.warning("ScheduleExecutor entering safe state: %s", reason)
        await self.bc.restore_normal()
        self._status.degraded = True
        self._status.applied_action = None  # see stop(): handback isn't a commanded self-use
        self._status.applied_power_w = 0.0
        self._status.note = f"safe_state:{reason}"

    # ------------------------------------------------------------------ helpers
    def _plan_is_stale(self, now: datetime) -> bool:
        if self._plan_updated_at is None:
            return True
        return (now - self._plan_updated_at) > self.max_plan_age

    def _desired(self, now: datetime) -> tuple[BatteryAction, float]:
        """Return the *effective* (action, power) for the interval covering ``now``.

        No covering interval → safe self-consumption (a gap in an otherwise fresh plan).
        A CHARGE slot is resolved to its execution intent via :meth:`_resolve_charge`:
        solar-only charge becomes self-consumption; grid charge keeps CHARGE but with the
        grid contribution (not the full charge rate) as the commanded power.
        """
        current: Optional[DispatchInterval] = None
        for iv in self._plan or []:
            if iv.start <= now:
                current = iv
            else:
                break
        if current is None:
            return BatteryAction.SELF_USE, 0.0
        if current.action == BatteryAction.CHARGE:
            return self._resolve_charge(current)
        return current.action, current.power_w

    # A CHARGE slot only runs as a grid-charge (force_charge) when the plan wants a
    # *material* amount of grid import: a real share of the slot (fraction) AND above an
    # absolute floor. Everything else is treated as solar charging and runs as Maximum
    # Self-consumption, which (a) resets the charge-rate cap to hardware max and (b) lets
    # the battery absorb ALL surplus PV.
    #
    # A tiny grid nibble (LP rounding, e.g. ~135 W on a 3.2 kW solar charge) must NOT
    # become a force_charge: force_charge sets the ESS max-charging-limit to the commanded
    # watts, and that limit caps TOTAL battery charge power — so charging at ~135 W would
    # throttle the solar charge and dump the surplus PV to a $0 export while the battery
    # sits half-empty. Self-consumption instead pulls the battery up from all surplus PV.
    _GRID_CHARGE_MIN_W = 250.0
    _GRID_CHARGE_MIN_FRACTION = 0.5

    def _resolve_charge(self, iv: DispatchInterval) -> tuple[BatteryAction, float]:
        """Split a CHARGE slot into its real execution intent.

        * material grid contribution (e.g. the evening pre-peak grid top-up) → genuine
          grid charge: force-charge at the *grid* watts.
        * otherwise → solar charge: run self-consumption so the battery fills from PV
          surplus at the hardware-max rate and the inverter never imports grid to charge it.
        """
        grid_w = iv.grid_charge_w
        if grid_w > self._GRID_CHARGE_MIN_W and grid_w >= self._GRID_CHARGE_MIN_FRACTION * max(iv.power_w, 0.0):
            return BatteryAction.CHARGE, grid_w
        return BatteryAction.SELF_USE, 0.0

    def status(self) -> dict:
        s = self._status
        return {
            "enabled": s.enabled,
            "degraded": s.degraded,
            "note": s.note,
            "applied_action": s.applied_action.value if s.applied_action else "unknown",
            "applied_power_w": s.applied_power_w,
            "applied_at": s.applied_at.isoformat() if s.applied_at else None,
            "plan_intervals": s.plan_intervals,
            "plan_updated_at": s.plan_updated_at.isoformat() if s.plan_updated_at else None,
            "last_tick": s.last_tick.isoformat() if s.last_tick else None,
            "interval_minutes": self.interval_minutes,
        }
