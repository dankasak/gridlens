"""Live-adjustable settings exposed as HA number entities — deliberately on the
dashboard/device page rather than only in the config-flow options, since these
are tuning knobs a user changes often, not one-time setup values.
"""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    CONF_HAS_BATTERY,
    CONF_MIN_EXPORT_PRICE,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_MAX_KW,
)

_LOGGER = logging.getLogger(__name__)


def _device_display_name(hass: HomeAssistant, sensor_id: str) -> str:
    """Best-effort human name for a deferrable device's source sensor.

    Tries the entity registry first (populated from disk at startup, so it's
    available even before the owning integration — sigen/evconduit/etc. — has
    finished setup and published a live state with friendly_name). Falls back to
    the live state's friendly_name, then to a humanized object_id rather than the
    raw entity_id, so a not-yet-loaded sensor still gets a readable label instead
    of "sensor.ev_charger_energy Today Boost".
    """
    entry = er.async_get(hass).async_get(sensor_id)
    if entry and (entry.name or entry.original_name):
        return entry.name or entry.original_name
    state_obj = hass.states.get(sensor_id)
    if state_obj and state_obj.attributes.get("friendly_name"):
        return state_obj.attributes["friendly_name"]
    object_id = sensor_id.split(".", 1)[-1]
    return object_id.replace("_", " ").title()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entities: list[NumberEntity] = []

    # min_export_price only affects the battery LP's export pricing — without a
    # battery, optimize_hourly_schedule never runs, so the entity would do nothing.
    if entry.data.get(CONF_HAS_BATTERY, False):
        entities.append(GridLensMinExportPriceNumber(entry))

    # One "today boost" override per configured deferrable device (Feature 2) —
    # independent of has_battery, since deferrable scheduling doesn't require one.
    sensors = entry.data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
    if sensors:
        store = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_deferrable_overrides")
        max_kws = entry.data.get(CONF_DEFERRABLE_LOAD_MAX_KW, [])
        for i, sensor_id in enumerate(sensors):
            name = _device_display_name(hass, sensor_id)
            max_kw = max_kws[i] if i < len(max_kws) else 3.5
            entities.append(
                GridLensDeferrableOverrideNumber(entry, store, sensor_id, name, max_kw)
            )

    if entities:
        async_add_entities(entities)


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


class GridLensDeferrableOverrideNumber(NumberEntity):
    """Today-only override of one deferrable device's daily kWh target — e.g. "charge
    the EV more today, I'm driving far". Beats the 14-day historical average
    AdvisoryCoordinator would otherwise use for the rest of the local calendar day,
    then reverts automatically (see deferrable_overrides.DeferrableOverrideStore).

    Reads/writes through the shared DeferrableOverrideStore rather than RestoreEntity:
    the store's own set_date expiry already decides whether a persisted value still
    applies, so mirroring it through RestoreEntity would just be a second, potentially
    inconsistent source of truth for the same question.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-clock"
    _attr_native_min_value = 0.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "kWh"
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, store, sensor_id: str, name: str, max_kw: float) -> None:
        self._store = store
        self._sensor_id = sensor_id
        self._attr_name = f"{name} Today Boost"
        self._attr_unique_id = f"{entry.entry_id}_deferrable_override_{sensor_id}"
        self._attr_native_max_value = max(1.0, max_kw * 24)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._store is not None:
            self._attr_native_value = await self._store.async_get(self._sensor_id)

    async def async_set_native_value(self, value: float) -> None:
        if self._store is not None:
            await self._store.async_set(self._sensor_id, value)
        self._attr_native_value = value if value > 0 else 0.0
        self.async_write_ha_state()
