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
    CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_SWITCHES,
)
from .executor import DispatchInterval
from .load_controller import DeferrableLoadController

_LOGGER = logging.getLogger(__name__)


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
        planned_w = self._device_power_now(index, now)
        try:
            await self.controllers[index].apply(planned_w, now)
        except Exception as err:  # noqa: BLE001 — a bad device tick must not kill the timer
            _LOGGER.error("Load control tick failed for %s: %s", self.controllers[index].name, err)

    def _device_power_now(self, index: int, now: datetime) -> float:
        """The planned power (W) for device ``index`` in the interval covering ``now``."""
        current: Optional[DispatchInterval] = None
        for iv in self._plan or []:
            if iv.start <= now:
                current = iv
            else:
                break
        if current is None:
            return 0.0
        dw = current.deferrable_w
        return float(dw[index]) if index < len(dw) else 0.0

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
