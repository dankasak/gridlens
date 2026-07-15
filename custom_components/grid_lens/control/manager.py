"""ControlManager — wires the Sigenergy driver → guardrail BatteryController → executor,
gated by the master enable switch. Owns the actuation lifecycle: enable() starts the loop,
disable() (and HA shutdown) is the deadman that restores native EMS.

Nothing here writes to the battery until enable() is called (master switch ON).
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from ..inverters.sigenergy_mqtt import SigenergyMqttController
from .battery_controller import BatteryController, GuardrailConfig
from .executor import ScheduleExecutor

_LOGGER = logging.getLogger(__name__)


class ControlManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        d = entry.data

        # Entity-proxy driver over sigenergy2mqtt (no second Modbus master).
        driver = SigenergyMqttController(hass)
        cfg = GuardrailConfig(
            min_soc_pct=float(d.get("battery_min_soc", 10.0)),
            charge_cutoff_pct=float(d.get("battery_max_soc", 100.0)),
            max_charge_w=float(d.get("battery_max_charge_rate", 5.0)) * 1000.0,
            max_discharge_w=float(d.get("battery_max_discharge_rate", 5.0)) * 1000.0,
        )
        self.controller = BatteryController(hass, driver, cfg)
        self.executor = ScheduleExecutor(hass, self.controller, interval_minutes=5)
        self._enabled = False

        # Deadman: restore native control if HA shuts down while control is active.
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_hass_stop)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_plan(self, plan) -> None:
        """Fresh advisory plan (list[DispatchInterval]) — the executor acts on it only
        while enabled, but we keep it current so enable() acts immediately."""
        self.executor.set_plan(plan)

    async def enable(self) -> bool:
        if self._enabled:
            return True
        _LOGGER.warning("GridLens battery control ENABLED — actuating the battery per plan")
        self._enabled = True
        await self.executor.start()
        return True

    async def disable(self) -> None:
        if not self._enabled:
            return
        _LOGGER.warning("GridLens battery control DISABLED — restoring native EMS")
        self._enabled = False
        await self.executor.stop(restore_normal=True)

    async def _on_hass_stop(self, _event) -> None:
        if self._enabled:
            _LOGGER.warning("HA stopping with control active — deadman: restoring native EMS")
            await self.executor.stop(restore_normal=True)

    def status(self) -> dict:
        return {"enabled": self._enabled, **self.executor.status(), **self.controller.status()}
