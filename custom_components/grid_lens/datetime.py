"""The deadline half of an ad-hoc "charge to X% by a datetime" target — paired with
number.py's GridLensChargeTargetPercentNumber for the percent half. Its own platform
file because HA's datetime entity is a separate platform from number, not because the
feature is otherwise distinct — see charge_target.py for the shared maths and
charge_target_store.py for the shared Store both entities read/write through.
"""
from __future__ import annotations

import datetime as dt
import logging

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_SOC_SENSORS,
    CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH,
)
from .number import _device_display_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    sensors = entry.data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
    soc_sensors = entry.data.get(CONF_DEFERRABLE_LOAD_SOC_SENSORS, [])
    soc_capacities = entry.data.get(CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH, [])
    if not soc_sensors:
        return
    store = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_charge_targets")
    entities: list[DateTimeEntity] = []
    for i, soc_sensor_id in enumerate(soc_sensors):
        capacity = soc_capacities[i] if i < len(soc_capacities) else 0.0
        if not soc_sensor_id or not capacity:
            continue
        if i >= len(sensors) or not sensors[i]:
            continue
        name = _device_display_name(hass, sensors[i])
        entities.append(GridLensChargeTargetTimeDateTime(entry, store, sensors[i], name))
    if entities:
        async_add_entities(entities)


class GridLensChargeTargetTimeDateTime(DateTimeEntity):
    """The deadline half of an ad-hoc charge target — see number.py's
    GridLensChargeTargetPercentNumber for the full explanation (percent/datetime pair,
    auto-clear-on-reach-or-expiry, merge-on-write). Unset (native_value None) reads as
    "no target" regardless of what the percent entity holds.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, entry: ConfigEntry, store, sensor_id: str, name: str) -> None:
        self._store = store
        self._entry_id = entry.entry_id
        self._sensor_id = sensor_id
        self._attr_name = f"{name} Charge Target Time"
        self._attr_unique_id = f"{entry.entry_id}_charge_target_time_{sensor_id}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_native_value: dt.datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._refresh_from_store()
        if self._store is not None:
            from .charge_target_store import update_signal
            from homeassistant.helpers.dispatcher import async_dispatcher_connect

            async def _on_update(sensor_id: str) -> None:
                if sensor_id == self._sensor_id:
                    await self._refresh_from_store()
                    self.async_write_ha_state()

            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass, update_signal(self._entry_id), _on_update,
                )
            )

    async def _refresh_from_store(self) -> None:
        # See number.py's GridLensChargeTargetPercentNumber._refresh_from_store — same
        # reasoning, kept in sync via charge_target_store.update_signal.
        if self._store is None:
            return
        raw = await self._store.async_get_raw(self._sensor_id)
        if raw:
            parsed = dt_util.parse_datetime(raw["target_iso"])
            self._attr_native_value = dt_util.as_utc(parsed) if parsed else None
        else:
            self._attr_native_value = None

    @property
    def extra_state_attributes(self) -> dict:
        # See number.py's GridLensChargeTargetPercentNumber for charge_target_role's role
        # (grid-lens-charge-target-card.js pairs percent+time entities by
        # deferrable_sensor_id, distinguished from other entities by this attribute).
        return {"deferrable_sensor_id": self._sensor_id, "charge_target_role": "time"}

    async def async_set_value(self, value: dt.datetime) -> None:
        value_utc = dt_util.as_utc(value)
        if self._store is not None:
            existing = await self._store.async_get_raw(self._sensor_id)
            percent = existing["percent"] if existing else 0.0
            await self._store.async_set(self._sensor_id, percent, value_utc.isoformat())
        self._attr_native_value = value_utc
        self.async_write_ha_state()
