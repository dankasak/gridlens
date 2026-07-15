"""BatteryController — the guardrail layer (spec §3).

Wraps a single ``InverterController`` and presents ONE safe interface to the executor,
regardless of brand. **All battery safety lives here**, enforced independently of the
optimizer:

1. **SOC floor** — never force-discharge below the configured reserve, and push that
   floor to hardware (``set_discharge_floor``) so it holds even if HA crashes.
2. **Rate clamping** — clamp requested power to configured/inverter limits.
3. **Forced-mode auto-expiry** — every forced command arms a software timer that reverts
   to self-consumption if the executor stops refreshing (belt-and-braces with the
   executor's own deadman). NOTE: a software timer does not survive a full HA crash — the
   *hardware* discharge floor is the crash backstop; a hardware EMS-command timeout is
   still an open question (spec §10).
4. **Backup-reserve provenance** — reads are tagged with a trust level + source.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..inverters.base import BatteryAction, InverterController

_LOGGER = logging.getLogger(__name__)


class ReserveTrust(str, Enum):
    """How much to trust a backup-reserve reading."""

    LIVE = "live"        # fresh from the device
    STALE = "stale"      # cached / possibly out of date
    UNKNOWN = "unknown"  # could not be read


@dataclass(frozen=True)
class ReserveReading:
    value_pct: Optional[float]
    trust: ReserveTrust
    source: str


@dataclass
class GuardrailConfig:
    """Safety limits. All independent of the optimizer's own constraints."""

    min_soc_pct: float = 10.0            # discharge floor (software + hardware)
    charge_cutoff_pct: float = 100.0     # do not force-charge above this SOC
    max_charge_w: Optional[float] = None  # None = defer to the driver's own clamp
    max_discharge_w: Optional[float] = None
    default_duration_minutes: float = 10.0  # forced-mode auto-expiry
    push_hardware_floor: bool = True     # write min_soc_pct to the inverter's cut-off


class BatteryController:
    """Guardrailed, brand-agnostic battery command interface for the executor."""

    def __init__(
        self,
        hass: HomeAssistant,
        driver: InverterController,
        config: Optional[GuardrailConfig] = None,
    ) -> None:
        self.hass = hass
        self.driver = driver
        self.cfg = config or GuardrailConfig()
        self._lock = asyncio.Lock()
        self._cancel_expiry: Optional[callback] = None
        self._hardware_floor_set = False

        # Status (read by sensors / diagnostics)
        self.current_action: BatteryAction = BatteryAction.SELF_USE
        self.current_power_w: float = 0.0
        self.last_command: Optional[datetime] = None
        self.last_refused_reason: Optional[str] = None

    @property
    def supports_battery_control(self) -> bool:
        return self.driver.supports_battery_control

    # ------------------------------------------------------------------ commands
    async def force_charge(
        self, power_w: float, duration_minutes: Optional[float] = None
    ) -> bool:
        async with self._lock:
            if not self._require_battery():
                return False

            soc = await self._read_soc()
            if soc is not None and soc >= self.cfg.charge_cutoff_pct:
                return await self._refuse(
                    f"charge refused: SOC {soc:.1f}% ≥ cutoff {self.cfg.charge_cutoff_pct:.1f}%"
                )

            power_w = self._clamp_power(power_w, self.cfg.max_charge_w)
            ok = await self.driver.force_charge(power_w)
            if ok:
                self._set_action(BatteryAction.CHARGE, power_w)
                self._arm_expiry(duration_minutes)
            return ok

    async def force_discharge(
        self, power_w: float, duration_minutes: Optional[float] = None
    ) -> bool:
        async with self._lock:
            if not self._require_battery():
                return False

            # SOC floor — the single most important guardrail.
            soc = await self._read_soc()
            if soc is not None and soc <= self.cfg.min_soc_pct:
                return await self._refuse(
                    f"discharge refused: SOC {soc:.1f}% ≤ floor {self.cfg.min_soc_pct:.1f}%"
                )

            await self._ensure_hardware_floor()
            power_w = self._clamp_power(power_w, self.cfg.max_discharge_w)
            ok = await self.driver.force_discharge(power_w)
            if ok:
                self._set_action(BatteryAction.DISCHARGE, power_w)
                self._arm_expiry(duration_minutes)
            return ok

    async def set_idle(self, duration_minutes: Optional[float] = None) -> bool:
        async with self._lock:
            if not self._require_battery():
                return False
            ok = await self.driver.set_idle()
            if ok:
                self._set_action(BatteryAction.IDLE, 0.0)
                self._arm_expiry(duration_minutes)  # revert to self-use if abandoned
            return ok

    async def set_self_consumption_mode(self) -> bool:
        async with self._lock:
            self._cancel_expiry_timer()
            if not self._require_battery():
                return False
            ok = await self.driver.set_self_consumption_mode()
            if ok:
                self._set_action(BatteryAction.SELF_USE, 0.0)
            return ok

    async def restore_normal(self) -> bool:
        """Deadman handback — always attempt, even if unsupported flags are off."""
        async with self._lock:
            self._cancel_expiry_timer()
            try:
                ok = await self.driver.restore_normal()
            except NotImplementedError:
                ok = await self.driver.set_self_consumption_mode()
            self._set_action(BatteryAction.SELF_USE, 0.0)
            return ok

    # ------------------------------------------------------------------ reserve
    async def read_backup_reserve(self) -> ReserveReading:
        try:
            value = await self.driver.get_backup_reserve()
        except NotImplementedError:
            value = None
        if value is None:
            return ReserveReading(None, ReserveTrust.UNKNOWN, "driver")
        return ReserveReading(value, ReserveTrust.LIVE, "driver")

    async def set_backup_reserve(self, soc_pct: float) -> bool:
        soc_pct = max(0.0, min(100.0, soc_pct))
        return await self.driver.set_backup_reserve(soc_pct)

    async def read_soc(self) -> Optional[float]:
        return await self._read_soc()

    # ------------------------------------------------------------------ internals
    def _require_battery(self) -> bool:
        if not self.driver.supports_battery_control:
            _LOGGER.error("%s driver has no battery control", self.driver.brand)
            return False
        return True

    async def _read_soc(self) -> Optional[float]:
        try:
            state = await self.driver.get_status()
            return state.soc_pct
        except Exception as err:  # noqa: BLE001 — read must not crash a control decision
            _LOGGER.error("SOC read failed: %s", err)
            return None

    async def _ensure_hardware_floor(self) -> None:
        if not self.cfg.push_hardware_floor or self._hardware_floor_set:
            return
        try:
            if await self.driver.set_discharge_floor(self.cfg.min_soc_pct):
                self._hardware_floor_set = True
                _LOGGER.info("Hardware discharge floor set to %.1f%%", self.cfg.min_soc_pct)
        except NotImplementedError:
            pass

    def _clamp_power(self, power_w: float, ceiling: Optional[float]) -> float:
        power_w = max(0.0, power_w)
        if ceiling is not None:
            power_w = min(power_w, ceiling)
        return power_w

    async def _refuse(self, reason: str) -> bool:
        _LOGGER.warning("BatteryController %s", reason)
        self.last_refused_reason = reason
        # Fall back to the safe neutral state.
        await self.driver.set_self_consumption_mode()
        self._set_action(BatteryAction.SELF_USE, 0.0)
        return False

    def _set_action(self, action: BatteryAction, power_w: float) -> None:
        self.current_action = action
        self.current_power_w = power_w
        self.last_command = dt_util.now()
        if action in (BatteryAction.CHARGE, BatteryAction.DISCHARGE):
            self.last_refused_reason = None

    # -------------------------------------------------------------- expiry timer
    def _arm_expiry(self, duration_minutes: Optional[float]) -> None:
        self._cancel_expiry_timer()
        minutes = duration_minutes or self.cfg.default_duration_minutes
        delay_s = max(1.0, minutes * 60.0)
        self._cancel_expiry = async_call_later(self.hass, delay_s, self._on_expiry)

    def _cancel_expiry_timer(self) -> None:
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None

    @callback
    def _on_expiry(self, _now) -> None:
        self._cancel_expiry = None
        _LOGGER.warning(
            "Forced mode (%s) auto-expired without refresh — reverting to self-consumption",
            self.current_action.value,
        )
        self.hass.async_create_task(self.set_self_consumption_mode())

    # --------------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "brand": self.driver.brand,
            "current_action": self.current_action.value,
            "current_power_w": self.current_power_w,
            "last_command": self.last_command.isoformat() if self.last_command else None,
            "last_refused_reason": self.last_refused_reason,
            "min_soc_floor_pct": self.cfg.min_soc_pct,
            "hardware_floor_set": self._hardware_floor_set,
        }
