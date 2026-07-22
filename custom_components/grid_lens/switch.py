"""Master battery-control switch. Default OFF on every startup (never auto-actuates) —
turning it ON starts the guardrailed control loop; OFF is the deadman (restore native EMS).
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = hass.data[DOMAIN].get(f"{entry.entry_id}_control")
    if manager is not None:
        async_add_entities([GridLensBatteryControlSwitch(manager, entry)])


class GridLensBatteryControlSwitch(RestoreEntity, SwitchEntity):
    """ON = GridLens actuates the battery per the advisory plan (guardrailed).

    Restores its last state across restarts and defaults ON, so control persists (an HA
    restart doesn't silently stop optimising the battery). The HA-stop deadman still hands
    back to native during the shutdown window; this re-engages on the next startup.
    """

    _attr_has_entity_name = True
    _attr_name = "Battery Control"
    _attr_icon = "mdi:battery-sync"

    def __init__(self, manager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_battery_control"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._manager.set_state_listener(self._on_manager_change)
        last = await self.async_get_last_state()
        want_on = True if last is None else (last.state == "on")  # default ON if no prior state
        if want_on:
            await self._manager.enable()
        # Reflect what actually happened, not just what was requested — enable() refuses
        # (and stays refused) until the account's battery-control entitlement is confirmed,
        # so a restored/default "on" doesn't lie about the real state. If entitlement lands
        # a bit later, _on_manager_change picks up the auto-retry in set_entitled().
        self._attr_is_on = self._manager.enabled
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._manager.set_state_listener(None)
        await super().async_will_remove_from_hass()

    def _on_manager_change(self) -> None:
        self._attr_is_on = self._manager.enabled
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        return self._manager.status()

    async def async_turn_on(self, **kwargs) -> None:
        await self._manager.enable()
        self._attr_is_on = self._manager.enabled
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._manager.disable()
        self._attr_is_on = False
        self.async_write_ha_state()
