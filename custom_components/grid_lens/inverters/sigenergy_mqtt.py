"""Sigenergy driver — HA-entity-proxy transport over the ``sigenergy2mqtt`` bridge.

Clean-room implementation from ``INVERTER_HAL_SPEC.md`` §5.3 / §8. Instead of opening a
second Modbus master (which would contend with sigenergy2mqtt's own connection), this
driver drives the entities that bridge already publishes:

  Controls (writable):
    switch.sigen_plant_remote_ems_enable            Remote EMS master enable
    select.sigen_0_plant_remote_ems_control_mode    EMS mode (strings below)
    number.sigen_plant_ess_max_charging_limit_2     charge-rate cap  (kW)
    number.sigen_plant_ess_max_discharging_limit_2  discharge-rate cap (kW)
    number.sigen_plant_grid_point_maximum_export_limitation  export cap (kW)

  Telemetry (read):
    sensor.sigen_plant_ess_soc                  % (SOC)
    sensor.sigen_plant_ess_power                kW, >0 charging / <0 discharging
    sensor.sigen_0_total_pv_power               kW
    sensor.sigen_plant_grid_sensor_active_power kW, >0 import / <0 export
    sensor.sigen_plant_general_load_power       kW
    sensor.sigen_plant_ess_rated_energy_capacity kWh
    sensor.sigen_plant_ess_soh                  %

All native values are kW; this driver converts to/from canonical watts.
"""
from __future__ import annotations

import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from .base import BatteryAction, InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

# Exact option strings on select.sigen_0_plant_remote_ems_control_mode (verified live).
MODE_STANDBY = "Standby"
MODE_SELF_CONSUMPTION = "Maximum Self-consumption (Default)"
MODE_CHARGE_GRID = "Command Charging (Consume power from the grid first)"
MODE_CHARGE_PV = "Command Charging (Consume power from the PV first)"
MODE_DISCHARGE_PV = "Command Discharging (Output power from PV first)"
MODE_DISCHARGE_ESS = "Command Discharging (Output power from the battery first)"

# Fallback rate caps (kW) if the number entity does not expose a ``max`` attribute.
_DEFAULT_CHARGE_CAP_KW = 12.6
_DEFAULT_DISCHARGE_CAP_KW = 14.4

# Expected mode string per abstract action, for verify_mode(). DISCHARGE isn't listed —
# it depends on ``discharge_mode_pv_first`` (instance config), so it's resolved from
# ``self._discharge_mode`` instead. IDLE/CHARGE/SELF_USE are fixed regardless of config.
_ACTION_MODES = {
    BatteryAction.IDLE: MODE_STANDBY,
    BatteryAction.CHARGE: MODE_CHARGE_PV,
    BatteryAction.SELF_USE: MODE_SELF_CONSUMPTION,
}

_DEFAULT_ENTITIES = {
    "enable": "switch.sigen_plant_remote_ems_enable",
    "mode": "select.sigen_0_plant_remote_ems_control_mode",
    "charge_limit": "number.sigen_plant_ess_max_charging_limit_2",
    "discharge_limit": "number.sigen_plant_ess_max_discharging_limit_2",
    "export_limit": "number.sigen_plant_grid_point_maximum_export_limitation",
    "backup_reserve": "number.sigen_plant_backup_soc",
    "discharge_floor": "number.sigen_plant_discharge_cut_off_soc",
    "soc": "sensor.sigen_plant_ess_soc",
    "battery_power": "sensor.sigen_plant_ess_power",
    "pv_power": "sensor.sigen_0_total_pv_power",
    "grid_power": "sensor.sigen_plant_grid_sensor_active_power",
    "load_power": "sensor.sigen_plant_general_load_power",
    "capacity": "sensor.sigen_plant_ess_rated_energy_capacity",
    "soh": "sensor.sigen_plant_ess_soh",
}


class SigenergyMqttController(InverterController):
    """Sigenergy plant control via the sigenergy2mqtt entity surface."""

    brand = "Sigenergy"
    supports_battery_control = True
    supports_curtailment = True

    def __init__(
        self,
        hass: HomeAssistant,
        entities: Optional[dict[str, str]] = None,
        *,
        max_export_kw: Optional[float] = None,
        discharge_mode_pv_first: bool = False,
    ) -> None:
        """Args:
        hass: Home Assistant instance.
        entities: Override map (see ``_DEFAULT_ENTITIES``); missing keys use defaults.
        max_export_kw: DNSP export cap applied to force_discharge (None = no extra cap).
        discharge_mode_pv_first: prefer PV-first discharge (mode 5) over battery-first (6).
        """
        self.hass = hass
        self._e = {**_DEFAULT_ENTITIES, **(entities or {})}
        self._max_export_kw = max_export_kw
        self._discharge_mode = (
            MODE_DISCHARGE_PV if discharge_mode_pv_first else MODE_DISCHARGE_ESS
        )
        # Remember the user's export-limit setpoint so curtail()/restore() is reversible.
        self._saved_export_kw: Optional[float] = None

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> bool:
        # "Connected" == the bridge is publishing the control + SOC surface.
        for key in ("mode", "enable", "soc"):
            if self._state(self._e[key]) is None:
                _LOGGER.warning("Sigenergy entity %s unavailable", self._e[key])
                return False
        return True

    async def disconnect(self) -> None:
        return  # entity proxy holds no connection

    async def get_status(self) -> InverterState:
        soc = self._float(self._e["soc"])
        running = self._state(self._e["mode"])
        status = InverterStatus.ONLINE if running is not None else InverterStatus.OFFLINE

        cap_kwh = self._float(self._e["capacity"])
        state = InverterState(
            status=status,
            is_curtailed=self._saved_export_kw is not None,
            soc_pct=soc,
            battery_power_w=self._kw_to_w(self._float(self._e["battery_power"])),  # charge+
            battery_capacity_wh=(cap_kwh * 1000.0) if cap_kwh is not None else None,
            soh_pct=self._float(self._e["soh"]),
            pv_power_w=self._kw_to_w(self._float(self._e["pv_power"])),
            grid_power_w=self._kw_to_w(self._float(self._e["grid_power"])),  # import+
            load_power_w=self._kw_to_w(self._float(self._e["load_power"])),
        )
        state.extra["ems_mode"] = running
        return state

    # ---------------------------------------------------------------- dispatch
    async def force_charge(self, power_w: float) -> bool:
        """Charge PV-first (mode 4). Rate cap is set before the mode commits.

        CHARGE_PV supplements PV with grid as needed; CHARGE_GRID (mode 3) would
        suppress solar entirely, so we never use it here (spec §6.2).
        """
        target_kw = self._clamp(power_w / 1000.0, self._e["charge_limit"], _DEFAULT_CHARGE_CAP_KW)
        if not await self._set_number(self._e["charge_limit"], target_kw):
            return False
        if not await self._enable_ems():
            return False
        ok = await self._set_mode(MODE_CHARGE_PV)
        if ok:
            _LOGGER.info("Sigenergy FORCE CHARGE (PV-first) @ %.2f kW", target_kw)
        return ok

    async def force_discharge(self, power_w: float) -> bool:
        """Discharge to grid, clamped to the DNSP export cap (spec §6.3)."""
        target_kw = power_w / 1000.0
        if self._max_export_kw is not None and target_kw > self._max_export_kw:
            _LOGGER.info(
                "Force discharge %.2f kW exceeds export cap %.2f kW — clamping",
                target_kw,
                self._max_export_kw,
            )
            target_kw = self._max_export_kw
        target_kw = self._clamp(target_kw, self._e["discharge_limit"], _DEFAULT_DISCHARGE_CAP_KW)
        if not await self._set_number(self._e["discharge_limit"], target_kw):
            return False
        if not await self._enable_ems():
            return False
        ok = await self._set_mode(self._discharge_mode)
        if ok:
            _LOGGER.info("Sigenergy FORCE DISCHARGE (%s) @ %.2f kW", self._discharge_mode, target_kw)
        return ok

    async def set_idle(self) -> bool:
        """Hold SOC via STANDBY (mode 1). Prevents firmware grid-charging to backup SOC."""
        if not await self._enable_ems():
            return False
        ok = await self._set_mode(MODE_STANDBY)
        return await self._reset_rate_limits() and ok

    async def set_self_consumption_mode(self) -> bool:
        if not await self._enable_ems():
            return False
        ok = await self._set_mode(MODE_SELF_CONSUMPTION)
        return await self._reset_rate_limits() and ok

    async def restore_normal(self) -> bool:
        """Deadman handback: restore export limit, then disable Remote EMS so the
        Sigenergy native/VPP scheduler resumes control."""
        ok = True
        if self._saved_export_kw is not None:
            ok = await self.restore() and ok
        # A previous force_charge/force_discharge leaves the plant's rate caps at the
        # last commanded (possibly tapered) value. Those caps are hard Modbus ceilings
        # that apply regardless of EMS mode, so leaving them low would silently throttle
        # native/self-consumption charging and discharging after handback.
        ok = await self._reset_rate_limits() and ok
        # Disabling the master enable hands control back to native EMS.
        ok = await self._switch(self._e["enable"], turn_on=False) and ok
        _LOGGER.info("Sigenergy Remote EMS disabled; native control restored")
        return ok

    async def verify_mode(self, action: BatteryAction) -> Optional[bool]:
        """Compare the live ``select.*_remote_ems_control_mode`` state against what
        ``action`` should have commanded. Used by the guardrail to catch a command
        that silently didn't land (e.g. lost across an MQTT bridge reconnect)."""
        expected = (
            self._discharge_mode if action == BatteryAction.DISCHARGE else _ACTION_MODES.get(action)
        )
        if expected is None:
            return None
        running = self._state(self._e["mode"])
        if running is None:
            return None
        return running.state == expected

    async def _reset_rate_limits(self) -> bool:
        """Restore charge/discharge rate caps to rated max — force_charge/force_discharge
        lower them as a side effect and nothing else raises them back on mode exit."""
        charge_cap = self._clamp(_DEFAULT_CHARGE_CAP_KW, self._e["charge_limit"], _DEFAULT_CHARGE_CAP_KW)
        discharge_cap = self._clamp(
            _DEFAULT_DISCHARGE_CAP_KW, self._e["discharge_limit"], _DEFAULT_DISCHARGE_CAP_KW
        )
        ok = await self._set_number(self._e["charge_limit"], charge_cap)
        return await self._set_number(self._e["discharge_limit"], discharge_cap) and ok

    # ---------------------------------------------------------------- curtailment
    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Zero-export (or load-following) via the grid export-limit setpoint."""
        if self._saved_export_kw is None:
            self._saved_export_kw = self._float(self._e["export_limit"])
        limit_kw = 0.0 if home_load_w is None else max(0.0, home_load_w / 1000.0)
        ok = await self._set_number(self._e["export_limit"], limit_kw)
        if ok:
            _LOGGER.info("Sigenergy export curtailed to %.2f kW", limit_kw)
        return ok

    async def restore(self) -> bool:
        restore_kw = self._saved_export_kw if self._saved_export_kw is not None else None
        if restore_kw is None:
            return True
        ok = await self._set_number(self._e["export_limit"], restore_kw)
        if ok:
            self._saved_export_kw = None
            _LOGGER.info("Sigenergy export limit restored to %.2f kW", restore_kw)
        return ok

    # ------------------------------------------------------- safety hooks
    async def get_backup_reserve(self) -> Optional[float]:
        return self._float(self._e["backup_reserve"])

    async def set_backup_reserve(self, soc_pct: float) -> bool:
        return await self._set_number(self._e["backup_reserve"], max(0.0, min(100.0, soc_pct)))

    async def set_discharge_floor(self, soc_pct: float) -> bool:
        """Hardware discharge cut-off — the inverter stops discharging at this SOC
        even if HA crashes while in a forced-discharge mode."""
        return await self._set_number(self._e["discharge_floor"], max(0.0, min(100.0, soc_pct)))

    # --------------------------------------------------------------------- I/O
    async def _enable_ems(self) -> bool:
        return await self._switch(self._e["enable"], turn_on=True)

    async def _set_mode(self, option: str) -> bool:
        return await self._call(
            "select", "select_option", self._e["mode"], {"option": option}
        )

    async def _set_number(self, entity_id: str, value: float) -> bool:
        return await self._call(
            "number", "set_value", entity_id, {"value": round(value, 2)}
        )

    async def _switch(self, entity_id: str, *, turn_on: bool) -> bool:
        return await self._call(
            "switch", "turn_on" if turn_on else "turn_off", entity_id, {}
        )

    async def _call(self, domain: str, service: str, entity_id: str, data: dict) -> bool:
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {"entity_id": entity_id, **data},
                blocking=True,
            )
            return True
        except Exception as err:  # noqa: BLE001 — control write must never crash the loop
            _LOGGER.error("Sigenergy %s.%s on %s failed: %s", domain, service, entity_id, err)
            return False

    # ----------------------------------------------------------------- read utils
    def _state(self, entity_id: str):
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", None):
            return None
        return st

    def _float(self, entity_id: str) -> Optional[float]:
        st = self._state(entity_id)
        if st is None:
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    def _clamp(self, value_kw: float, entity_id: str, fallback_max: float) -> float:
        st = self.hass.states.get(entity_id)
        cap = fallback_max
        if st is not None:
            try:
                cap = float(st.attributes.get("max", fallback_max))
            except (TypeError, ValueError):
                pass
        return max(0.0, min(value_kw, cap))

    @staticmethod
    def _kw_to_w(value_kw: Optional[float]) -> Optional[float]:
        return None if value_kw is None else value_kw * 1000.0
