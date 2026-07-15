"""GridLens inverter HAL — abstract controller contract and canonical state.

Clean-room implementation from ``INVERTER_HAL_SPEC.md`` (this repo). All vendor
knowledge lives in concrete drivers below this contract; the optimizer, controller,
and UI never learn a brand name.

Canonical sign conventions (normalise every driver into these — never leak a vendor
convention upward):

* ``battery_power_w``  > 0 = **charging**,  < 0 = discharging
* ``grid_power_w``     > 0 = **importing**, < 0 = exporting
* ``pv_power_w``       >= 0 (production)
* ``load_power_w``     >= 0 (house consumption)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Optional

_LOGGER = logging.getLogger(__name__)


class InverterStatus(str, Enum):
    """Connection / operational status of an inverter."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    CURTAILED = "curtailed"
    ERROR = "error"


class BatteryAction(str, Enum):
    """Abstract battery actions the optimizer emits (see spec §4)."""

    IDLE = "idle"          # hold SOC — no charge, no discharge
    CHARGE = "charge"      # grid/PV -> battery at a target rate
    DISCHARGE = "discharge"  # battery -> home/grid at a target rate
    SELF_USE = "self_use"  # autonomous self-consumption (the "home" mode)


@dataclass
class InverterState:
    """Canonical inverter/battery snapshot (superset; ``None`` where unsupported)."""

    status: InverterStatus = InverterStatus.UNKNOWN
    is_curtailed: bool = False
    # Battery
    soc_pct: Optional[float] = None
    battery_power_w: Optional[float] = None   # charge-positive
    battery_capacity_wh: Optional[float] = None
    soh_pct: Optional[float] = None
    # Flows
    pv_power_w: Optional[float] = None
    grid_power_w: Optional[float] = None      # import-positive
    load_power_w: Optional[float] = None
    # Limits / diagnostics
    power_limit_pct: Optional[float] = None
    backup_reserve_pct: Optional[float] = None
    error_message: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "status": self.status.value,
            "is_curtailed": self.is_curtailed,
            "soc_pct": self.soc_pct,
            "battery_power_w": self.battery_power_w,
            "battery_capacity_wh": self.battery_capacity_wh,
            "soh_pct": self.soh_pct,
            "pv_power_w": self.pv_power_w,
            "grid_power_w": self.grid_power_w,
            "load_power_w": self.load_power_w,
            "power_limit_pct": self.power_limit_pct,
            "backup_reserve_pct": self.backup_reserve_pct,
            "error_message": self.error_message,
        }
        d.update(self.extra)
        return d


class InverterController(ABC):
    """Abstract base for all inverter drivers.

    Subclasses declare their capabilities via the class flags below. Solar-only
    (curtail) drivers leave ``supports_battery_control`` False and need only implement
    the connection + curtail/restore + status methods; battery drivers additionally
    implement the dispatch methods.
    """

    #: Human-readable brand, set by each subclass (e.g. "Sigenergy").
    brand: ClassVar[str] = "unknown"
    #: True if the driver can force charge/discharge/idle the battery.
    supports_battery_control: ClassVar[bool] = False
    #: True if the driver can suppress solar export (negative-price protection).
    supports_curtailment: ClassVar[bool] = False

    # ------------------------------------------------------------------ lifecycle
    @abstractmethod
    async def connect(self) -> bool:
        """Establish/verify the transport. Returns True on success."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Release the transport (no-op for stateless drivers)."""

    @abstractmethod
    async def get_status(self) -> InverterState:
        """Read a canonical snapshot. Read-only; must not change device state."""

    # ---------------------------------------------------------------- curtailment
    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Suppress solar export (negative-price protection). Override if supported."""
        raise NotImplementedError(f"{self.brand}: curtailment not supported")

    async def restore(self) -> bool:
        """Undo :meth:`curtail`. Override if supported."""
        raise NotImplementedError(f"{self.brand}: curtailment not supported")

    # ------------------------------------------------------------ battery dispatch
    async def force_charge(self, power_w: float) -> bool:
        """Enter forced-charge at ≈ ``power_w``. Override if battery-capable."""
        raise NotImplementedError(f"{self.brand}: battery control not supported")

    async def force_discharge(self, power_w: float) -> bool:
        """Enter forced-discharge/export at ≈ ``power_w``, clamped to the export limit."""
        raise NotImplementedError(f"{self.brand}: battery control not supported")

    async def set_idle(self) -> bool:
        """Hold current SOC — no charge, no discharge (see spec §6.1)."""
        raise NotImplementedError(f"{self.brand}: battery control not supported")

    async def set_self_consumption_mode(self) -> bool:
        """Return the battery to autonomous self-consumption (the "home" mode)."""
        raise NotImplementedError(f"{self.brand}: battery control not supported")

    async def restore_normal(self) -> bool:
        """Full handback: release forced dispatch AND restore any export limit.

        Called on executor stop, integration unload, and any watchdog timeout
        (the deadman). Must always return the device to native/VPP control.
        """
        raise NotImplementedError(f"{self.brand}: battery control not supported")

    # ------------------------------------------------- safety hooks (optional)
    # Defaults let the guardrail call these unconditionally; drivers override
    # where the hardware exposes them (defense-in-depth for the SOC floor).
    async def get_backup_reserve(self) -> Optional[float]:
        """Return the battery backup-reserve SOC (%), or None if unavailable."""
        return None

    async def set_backup_reserve(self, soc_pct: float) -> bool:
        """Set the battery backup-reserve SOC (%). Returns False if unsupported."""
        return False

    async def set_discharge_floor(self, soc_pct: float) -> bool:
        """Set the **hardware** discharge cut-off SOC (%) — an inverter-enforced floor
        that holds even if HA crashes mid-discharge. Returns False if unsupported.
        """
        return False

    # --------------------------------------------------------------------- helper
    async def test_connection(self) -> tuple[bool, str]:
        try:
            if await self.connect():
                state = await self.get_status()
                await self.disconnect()
                return True, f"Connected. Status: {state.status.value}"
            return False, "Failed to establish connection"
        except Exception as err:  # noqa: BLE001 — surfaced to the config flow
            _LOGGER.error("%s connection test failed: %s", self.brand, err)
            return False, f"Connection error: {err}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}(brand={self.brand})"
