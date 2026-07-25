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
    entities: list[SwitchEntity] = []
    manager = hass.data[DOMAIN].get(f"{entry.entry_id}_control")
    if manager is not None:
        entities.append(GridLensBatteryControlSwitch(manager, entry))

    # One master switch per controllable deferrable load (device with a control switch
    # configured). Default OFF (opt-in) — unlike the battery switch's default ON — since
    # physically toggling a real appliance has more consequence than a battery mode change.
    load_mgr = hass.data[DOMAIN].get(f"{entry.entry_id}_load_control")
    if load_mgr is not None:
        for index, controller in load_mgr.controllers.items():
            entities.append(
                GridLensDeferrableLoadSwitch(load_mgr, entry, index, controller.name)
            )

    if entities:
        async_add_entities(entities)


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


class GridLensDeferrableLoadSwitch(RestoreEntity, SwitchEntity):
    """ON = GridLens turns this simple deferrable load's ``switch.*`` on/off per the plan.

    Restores its last state across restarts but **defaults OFF** (opt-in) — unlike the
    battery switch's default ON. Turning a real appliance on/off has more direct real-world
    consequence than a battery mode change, so a fresh install never actuates a load until
    the user explicitly enables it. Enabling is still refused (and reflected here) until the
    account's control entitlement is confirmed; set_entitled()'s auto-resume flips it on a
    little after boot without a manual re-toggle.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:power-plug"

    def __init__(self, manager, entry: ConfigEntry, index: int, device_name: str) -> None:
        self._manager = manager
        self._index = index
        self._attr_name = f"{device_name} Control"
        self._attr_unique_id = f"{entry.entry_id}_deferrable_control_{index}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._manager.set_state_listener(self._index, self._on_manager_change)
        last = await self.async_get_last_state()
        want_on = last is not None and last.state == "on"  # default OFF if no prior state
        if want_on:
            await self._manager.enable(self._index)
        self._attr_is_on = self._manager.is_enabled(self._index)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._manager.set_state_listener(self._index, None)
        await super().async_will_remove_from_hass()

    def _on_manager_change(self) -> None:
        self._attr_is_on = self._manager.is_enabled(self._index)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        return self._manager.status().get("devices", {}).get(self._index, {})

    async def async_turn_on(self, **kwargs) -> None:
        await self._manager.enable(self._index)
        self._attr_is_on = self._manager.is_enabled(self._index)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._manager.disable(self._index)
        self._attr_is_on = False
        self.async_write_ha_state()
