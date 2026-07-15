"""GridLens inverter HAL — brand/transport registry and factory.

See ``INVERTER_HAL_SPEC.md`` for the full contract. Drivers are imported lazily so an
unused brand never pulls in its dependencies.
"""
from __future__ import annotations

import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from .base import (
    BatteryAction,
    InverterController,
    InverterState,
    InverterStatus,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "BatteryAction",
    "InverterController",
    "InverterState",
    "InverterStatus",
    "INVERTER_BRANDS",
    "get_inverter_controller",
]

# brand -> {transport -> "display name"}. Extend as drivers land (spec §8).
INVERTER_BRANDS: dict[str, dict[str, str]] = {
    "sigenergy": {"mqtt": "Sigenergy (via sigenergy2mqtt)"},
}


def get_inverter_controller(
    hass: HomeAssistant,
    brand: str,
    transport: str = "mqtt",
    *,
    config: Optional[dict] = None,
) -> Optional[InverterController]:
    """Return a driver instance for ``brand``/``transport``, or None if unsupported.

    Args:
        hass: Home Assistant instance.
        brand: e.g. "sigenergy".
        transport: e.g. "mqtt" (entity proxy), "modbus" (native).
        config: driver-specific options (entity overrides, caps, …).
    """
    brand_l = (brand or "").lower()
    transport_l = (transport or "").lower()
    cfg = config or {}

    if brand_l == "sigenergy" and transport_l == "mqtt":
        from .sigenergy_mqtt import SigenergyMqttController

        return SigenergyMqttController(
            hass,
            entities=cfg.get("entities"),
            max_export_kw=cfg.get("max_export_kw"),
            discharge_mode_pv_first=cfg.get("discharge_mode_pv_first", False),
        )

    _LOGGER.warning("No inverter driver for brand=%s transport=%s", brand, transport)
    return None
