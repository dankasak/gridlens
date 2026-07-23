"""Live-adjustable settings exposed as HA number entities — deliberately on the
dashboard/device page rather than only in the config-flow options, since these
are tuning knobs a user changes often, not one-time setup values.
"""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, CONF_HAS_BATTERY, CONF_MIN_EXPORT_PRICE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    # min_export_price only affects the battery LP's export pricing — without a
    # battery, optimize_hourly_schedule never runs, so the entity would do nothing.
    if entry.data.get(CONF_HAS_BATTERY, False):
        async_add_entities([GridLensMinExportPriceNumber(entry)])


class GridLensMinExportPriceNumber(RestoreEntity, NumberEntity):
    """Below this feed-in price, surplus solar/battery power is routed to a
    deferrable load or held in the battery instead of exported cheaply (see
    battery_optimizer.py's min_export_price). 0 = disabled — always export at
    whatever the plan pays.

    Restores its last value across restarts (RestoreEntity); this entity's
    state IS the live setting — battery_optimizer picks up a change on the
    optimizer's next run (advisory re-plans every 2 minutes), no restart or
    reconfigure needed. See runtime_settings.get_live_number.
    """

    _attr_has_entity_name = True
    _attr_name = "Minimum Export Price"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 50.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "c/kWh"
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_min_export_price"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        # Only reachable on a fresh install before any prior state exists —
        # config-flow no longer sets this key, so it's always 0.0 in practice.
        self._attr_native_value = entry.data.get(CONF_MIN_EXPORT_PRICE, 0.0)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable"):
            try:
                self._attr_native_value = float(last.state)
            except ValueError:
                pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
