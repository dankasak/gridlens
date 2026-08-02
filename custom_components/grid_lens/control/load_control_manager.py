"""LoadControlManager — actuation lifecycle for simple on/off deferrable loads.

Deliberately **decoupled** from ``ControlManager``/the inverter HAL (product decision
2026-07-23): deferrable-load control has zero brand-specific logic (any ``switch.*``
behaves the same) and must work for households with no battery configured at all. The
cost is a second ``async_track_time_change`` timer alongside the battery executor's,
accepted to keep this independent of ``has_battery``/inverter config.

Per-device opt-in: each controllable load has its own default-OFF master switch (see
``switch.py``); ``enable(i)``/``disable(i)`` gate that device's actuation. Entitlement is
shared with battery control (the existing ``battery_control`` ApiKey column — product
decision 2026-07-23), fails **closed** (no writes until the API confirms), and revoking it
stops actuation immediately.

Deadman = **leave as-is**: on disable, HA stop, or a stale plan, this NEVER forces a load
off — it just stops driving it, leaving the last commanded hardware state in place. Cutting
a real appliance mid-cycle has more consequence than reverting an inverter mode.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_DEFERRABLE_LOAD_HOURS,
    CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_SWITCHES,
    CONF_GRID_POWER_SENSOR,
    DOMAIN,
)
from .executor import DispatchInterval
from .load_controller import DeferrableLoadController

_LOGGER = logging.getLogger(__name__)

# How far ahead Greedy Consumption's forecast-surplus condition looks for free energy the
# plan expects to waste (see DeferrableLoadController's module docstring). Long enough to
# see past the "battery is still soaking everything up" morning shoulder into the real
# afternoon spill, short enough that the forecast is still worth believing and that a
# device started now is plausibly still running when the surplus lands.
GREEDY_SURPLUS_LOOKAHEAD_HOURS = 4.0


class LoadControlManager:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        interval_minutes: int = 5,
        max_plan_age_minutes: float = 30.0,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.interval_minutes = max(1, int(interval_minutes))
        self.max_plan_age = timedelta(minutes=max_plan_age_minutes)

        d = entry.data
        sensors: list[str] = list(d.get(CONF_DEFERRABLE_LOAD_SENSORS, []) or [])
        max_kw: list = list(d.get(CONF_DEFERRABLE_LOAD_MAX_KW, []) or [])
        switches: list = list(d.get(CONF_DEFERRABLE_LOAD_SWITCHES, []) or [])
        # Retained for Greedy Consumption's schedule lookup (_schedule_allows_now):
        # sensor_id is the schedule store's key, hours_cfg is the static fallback spec
        # for devices with no stored weekly grid — same two sources
        # advisory/coordinator.py._deferrable_for_horizon already reads.
        self._sensor_ids = sensors
        self._hours_cfg: list = list(d.get(CONF_DEFERRABLE_LOAD_HOURS, []) or [])
        # Live signed grid power sensor (W, +import/-export) for Greedy Consumption's
        # export-surplus condition. "" = not configured — that condition simply never
        # fires (the import-price-free condition still works without it).
        self._grid_power_sensor: str = d.get(CONF_GRID_POWER_SENSOR) or ""

        # One controller per device that has a control switch configured. Keyed by the
        # device's index in the deferrable lists, so DispatchInterval.deferrable_w[i] lines
        # up with controller i. Devices without a switch stay forecast-only (absent here).
        self.controllers: dict[int, DeferrableLoadController] = {}
        for i, sensor_id in enumerate(sensors):
            sw = switches[i] if i < len(switches) else ""
            if not sw:
                continue
            self.controllers[i] = DeferrableLoadController(
                hass,
                name=self._device_name(sw, sensor_id),
                switch_entity_id=sw,
                max_w=float(max_kw[i]) * 1000.0 if i < len(max_kw) else 0.0,
            )

        # Per-device state. _want_enabled = user/switch intent (persists across an
        # entitlement blip); _enabled = actually driving now (intent AND entitled).
        self._want_enabled: dict[int, bool] = {i: False for i in self.controllers}
        self._enabled: dict[int, bool] = {i: False for i in self.controllers}
        self._entitled = False  # fail closed until the API confirms

        self._plan: Optional[list[DispatchInterval]] = None
        self._plan_updated_at: Optional[datetime] = None
        self._degraded = False
        self._cancel_timer: Optional[Callable] = None
        # Per-device sync callbacks the switch entities register to refresh their state
        # when _enabled[i] changes from somewhere other than a direct toggle (entitlement).
        self._on_change: dict[int, Callable] = {}

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_hass_stop)

    def _device_name(self, switch_id: str, sensor_id: str) -> str:
        from ..entity_lookup import resolve_device_name
        return resolve_device_name(self.hass, switch_id, sensor_id) or sensor_id

    def has_controllable(self) -> bool:
        return bool(self.controllers)

    # ------------------------------------------------------------------ plan feed
    def set_plan(self, intervals: list[DispatchInterval], updated_at: Optional[datetime] = None) -> None:
        self._plan = sorted(intervals, key=lambda iv: iv.start)
        self._plan_updated_at = updated_at or dt_util.now()
        if self._degraded:
            _LOGGER.info("Load control: fresh plan received — clearing degraded state")
        self._degraded = False

    # ------------------------------------------------------------------ listeners
    def set_state_listener(self, index: int, callback: Optional[Callable]) -> None:
        if callback is None:
            self._on_change.pop(index, None)
        else:
            self._on_change[index] = callback

    def _notify(self, index: int) -> None:
        cb = self._on_change.get(index)
        if cb:
            cb()

    # ------------------------------------------------------------------ entitlement
    async def set_entitled(self, entitled: bool) -> None:
        was = self._entitled
        self._entitled = entitled
        if not entitled:
            # Revoked: stop driving every device (leave hardware as-is), keep intent so a
            # later re-grant auto-resumes without the user re-toggling each switch.
            if any(self._enabled.values()):
                _LOGGER.warning("Load control entitlement revoked — stopping actuation")
            for i in list(self._enabled):
                if self._enabled[i]:
                    self._enabled[i] = False
                    self.controllers[i]._commanded = None  # re-establish on resume
                    self._notify(i)
            self._stop_timer_if_idle()
        elif entitled and not was:
            # Granted: resume any device the user still wants enabled.
            for i in list(self._want_enabled):
                if self._want_enabled[i] and not self._enabled[i]:
                    await self.enable(i)

    # ------------------------------------------------------------------ per-device lifecycle
    async def enable(self, index: int) -> bool:
        if index not in self.controllers:
            return False
        self._want_enabled[index] = True
        if self._enabled[index]:
            return True
        if not self._entitled:
            _LOGGER.warning(
                "Load control for %s requested but account isn't entitled — refusing",
                self.controllers[index].name,
            )
            return False
        self._enabled[index] = True
        _LOGGER.warning("Load control ENABLED for %s", self.controllers[index].name)
        self._ensure_timer()
        self._notify(index)
        await self._tick_device(index, dt_util.now())
        return True

    async def disable(self, index: int) -> None:
        if index not in self.controllers:
            return
        self._want_enabled[index] = False
        if self._enabled[index]:
            _LOGGER.warning(
                "Load control DISABLED for %s — leaving load as-is",
                self.controllers[index].name,
            )
        self._enabled[index] = False
        self.controllers[index]._commanded = None  # re-establish cleanly if re-enabled
        self._notify(index)
        self._stop_timer_if_idle()

    def is_enabled(self, index: int) -> bool:
        return bool(self._enabled.get(index, False))

    # ------------------------------------------------------------------ manual override
    def get_override(self, index: int):
        """'on' / 'off' while a manual override is active for the device, else None."""
        c = self.controllers.get(index)
        if c is None or c.override is None:
            return None
        return "on" if c.override else "off"

    async def set_override(self, index: int, mode, *, actuate: bool = True) -> bool:
        """Set (mode='on'/'off') or clear (mode=None) a device's manual override.

        Deliberately NOT gated on the device's enable switch or on entitlement: a
        forced on/off is a direct user command — morally identical to the user toggling
        the appliance's own switch entity, which they can always do — and it must work
        precisely when GridLens control is active so the controller stops fighting it
        (plan re-assert would otherwise flip the switch right back). Clearing returns
        the device to whatever the enable switch + plan dictate; if it's enabled, state
        re-establishes on an immediate tick rather than waiting up to 5 minutes.
        """
        c = self.controllers.get(index)
        if c is None:
            return False
        want = None if mode is None else (mode == "on")
        await c.set_override(want, dt_util.now(), actuate=actuate)
        self._notify(index)
        if want is None and actuate and self._enabled.get(index, False):
            await self._tick_device(index, dt_util.now())
        return True

    # ------------------------------------------------------------------ timer
    def _ensure_timer(self) -> None:
        if self._cancel_timer is not None:
            return
        minutes = list(range(0, 60, self.interval_minutes))
        self._cancel_timer = async_track_time_change(
            self.hass, self._tick, minute=minutes, second=0
        )
        _LOGGER.info("LoadControlManager timer started (interval=%dmin)", self.interval_minutes)

    def _stop_timer_if_idle(self) -> None:
        if self._cancel_timer is not None and not any(self._enabled.values()):
            self._cancel_timer()
            self._cancel_timer = None
            _LOGGER.info("LoadControlManager timer stopped (no devices active)")

    async def _on_hass_stop(self, _event) -> None:
        # Deadman = leave loads as-is. Stop the timer, but never force a switch off.
        if any(self._enabled.values()):
            _LOGGER.warning("HA stopping with load control active — leaving loads as-is (no forced off)")
        self.shutdown()

    def shutdown(self) -> None:
        """Stop ticking (config-entry unload). Deadman = leave loads as-is: never forces a
        switch off — just stops driving them."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    # ------------------------------------------------------------------ tick
    async def _tick(self, now: Optional[datetime] = None) -> None:
        now = now or dt_util.now()
        if not any(self._enabled.values()):
            return
        if self._plan is None:
            return  # nothing to act on yet — leave loads as-is
        if self._plan_is_stale(now):
            if not self._degraded:
                _LOGGER.warning("Load control: plan stale — leaving loads as-is until a fresh plan")
                self._degraded = True
            return
        for i in list(self._enabled):
            if self._enabled[i]:
                await self._tick_device(i, now)

    async def _tick_device(self, index: int, now: datetime) -> None:
        if self._plan is None or self._plan_is_stale(now):
            return  # leave as-is
        controller = self.controllers[index]
        current = self._current_interval(now)
        planned_w = self._device_power_now(index, current)
        schedule_allows = None
        if controller.greedy_respects_schedule:
            schedule_allows = await self._schedule_allows_now(index, now)
        free_kwh, free_hours = (None, 0.0)
        if controller.greedy and controller.greedy_forecast_surplus:
            free_kwh, free_hours = self._forecast_free_kwh(index, now)
        try:
            await controller.apply(
                planned_w, now,
                import_rate=current.import_rate if current else None,
                export_rate=current.export_rate if current else None,
                grid_power_w=self._read_grid_power_w(),
                schedule_allows=schedule_allows,
                forecast_free_kwh=free_kwh,
                forecast_hours=free_hours,
            )
        except Exception as err:  # noqa: BLE001 — a bad device tick must not kill the timer
            _LOGGER.error("Load control tick failed for %s: %s", self.controllers[index].name, err)

    def _current_interval(self, now: datetime) -> Optional[DispatchInterval]:
        current: Optional[DispatchInterval] = None
        for iv in self._plan or []:
            if iv.start <= now:
                current = iv
            else:
                break
        return current

    def _device_power_now(self, index: int, current: Optional[DispatchInterval]) -> float:
        """The planned power (W) for device ``index`` in interval ``current`` (or 0.0 if none)."""
        if current is None:
            return 0.0
        dw = current.deferrable_w
        return float(dw[index]) if index < len(dw) else 0.0

    def _forecast_free_kwh(self, index: int, now: datetime) -> tuple[Optional[float], float]:
        """Free energy (kWh) the plan expects to leave UNCLAIMED over the next
        ``GREEDY_SURPLUS_LOOKAHEAD_HOURS``, plus the span (hours) actually covered.

        Two sources, per the user-facing definition of "free energy" (solar or free grid
        imports):

        * **Spilled export** — a slot whose export rate is $0 (or negative) and that the
          plan still exports into: that energy is being given away for nothing. Uses
          ``total_export_w`` (whole-house export, PV spill included), NOT ``export_w``
          (battery share only), and it's already net of every load the plan schedules in
          that slot — so nothing is subtracted from it here.
        * **Unused free-import window** — a slot whose import rate is $0: this device
          could run flat out for free then. Only the part the plan does NOT already have
          it running counts as unclaimed, hence the ``max_w - planned`` term.

        Returns ``(None, 0.0)`` when there isn't enough forecast left to judge — less
        than half the look-ahead window is covered by the plan. That's the same
        fail-closed discipline as the other greedy inputs: a sliver of horizon tail would
        otherwise shrink the controller's bar (which scales with the covered span) until
        a trivial surplus cleared it.
        """
        plan = self._plan
        controller = self.controllers.get(index)
        if not plan or controller is None:
            return None, 0.0
        window_end = now + timedelta(hours=GREEDY_SURPLUS_LOOKAHEAD_HOURS)
        total_kwh = 0.0
        covered_h = 0.0
        for pos, iv in enumerate(plan):
            slot_end = self._slot_end(plan, pos)
            o_start = max(iv.start, now)
            o_end = min(slot_end, window_end)
            if o_end <= o_start:
                continue
            hours = (o_end - o_start).total_seconds() / 3600.0
            free_w = 0.0
            if iv.export_rate is not None and iv.export_rate <= 0.0:
                free_w += max(0.0, iv.total_export_w)
            if iv.import_rate is not None and iv.import_rate <= 0.0:
                planned_w = self._device_power_now(index, iv)
                free_w += max(0.0, controller.max_w - max(0.0, planned_w))
            total_kwh += free_w * hours / 1000.0
            covered_h += hours
        if covered_h < GREEDY_SURPLUS_LOOKAHEAD_HOURS / 2.0:
            return None, 0.0
        return total_kwh, covered_h

    @staticmethod
    def _slot_end(plan: list[DispatchInterval], pos: int) -> datetime:
        """End of plan slot ``pos``. ``DispatchInterval`` carries only a start, so a
        slot runs until the next one starts; the final slot reuses the previous gap (and
        falls back to 30 min for a degenerate single-slot plan)."""
        if pos + 1 < len(plan):
            return plan[pos + 1].start
        if pos > 0:
            return plan[pos].start + (plan[pos].start - plan[pos - 1].start)
        return plan[pos].start + timedelta(minutes=30)

    def _read_grid_power_w(self) -> Optional[float]:
        """Live signed grid power (W, +import/-export) for Greedy Consumption's
        export-surplus condition. None if unconfigured/unavailable/unparseable — the
        controller treats that as "unknown", never guessing a value."""
        if not self._grid_power_sensor:
            return None
        st = self.hass.states.get(self._grid_power_sensor)
        if st is None or st.state in ("unknown", "unavailable", None):
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    async def _schedule_allows_now(self, index: int, now: datetime) -> bool:
        """Does device ``index``'s availability window/weekly schedule allow it to run
        right now? Reused verbatim from advisory/coordinator.py._deferrable_for_horizon's
        sourcing: the stored weekly grid (schedule editor) when set, else the static
        deferrable_load_hours config spec. Both layers fail OPEN on malformed data
        (schedule_grid.slot_allowed's own contract) — a broken store must never silently
        pin a device off."""
        from ..const import parse_hours_spec
        from ..schedule_grid import slot_allowed, week_from_hours

        store = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_deferrable_schedules")
        sensor_id = self._sensor_ids[index] if index < len(self._sensor_ids) else ""
        week = None
        if store is not None:
            try:
                week = await store.async_get(sensor_id)
            except Exception:  # noqa: BLE001 — a broken store must not block ticking
                week = None
        if week is None:
            spec = self._hours_cfg[index] if index < len(self._hours_cfg) else "all"
            try:
                allowed = parse_hours_spec(spec)
            except Exception:  # noqa: BLE001
                allowed = None
            week = week_from_hours(allowed)
        local = dt_util.as_local(now)
        return slot_allowed(week, local.weekday(), local.hour, local.minute)

    # ------------------------------------------------------------------ greedy consumption
    def is_greedy(self, index: int) -> bool:
        c = self.controllers.get(index)
        return bool(c.greedy) if c else False

    async def set_greedy(self, index: int, enabled: bool) -> bool:
        c = self.controllers.get(index)
        if c is None:
            return False
        c.set_greedy(enabled)
        return True

    def is_greedy_respects_schedule(self, index: int) -> bool:
        c = self.controllers.get(index)
        return bool(c.greedy_respects_schedule) if c else False

    async def set_greedy_respects_schedule(self, index: int, enabled: bool) -> bool:
        c = self.controllers.get(index)
        if c is None:
            return False
        c.set_greedy_respects_schedule(enabled)
        return True

    def is_greedy_forecast_surplus(self, index: int) -> bool:
        c = self.controllers.get(index)
        return bool(c.greedy_forecast_surplus) if c else False

    async def set_greedy_forecast_surplus(self, index: int, enabled: bool) -> bool:
        c = self.controllers.get(index)
        if c is None:
            return False
        c.set_greedy_forecast_surplus(enabled)
        return True

    def _plan_is_stale(self, now: datetime) -> bool:
        if self._plan_updated_at is None:
            return True
        return (now - self._plan_updated_at) > self.max_plan_age

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {
            "entitled": self._entitled,
            "degraded": self._degraded,
            "plan_updated_at": self._plan_updated_at.isoformat() if self._plan_updated_at else None,
            "devices": {
                i: {
                    "enabled": self._enabled.get(i, False),
                    "want_enabled": self._want_enabled.get(i, False),
                    **c.status(),
                }
                for i, c in self.controllers.items()
            },
        }
