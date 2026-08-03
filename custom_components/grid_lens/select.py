"""Manual override selector for each controllable deferrable load.

One select entity per deferrable device that has a control switch configured, with three
options: Auto (GridLens drives the load from the plan, subject to its enable switch),
Force On, and Force Off. Selecting a Force option issues ONE immediate switch command
and then suspends all plan-driven control of that load — including drift re-asserts, so
a human at the physical switch always wins afterwards — until Auto is selected again
("restore control"), which re-establishes the planned state immediately.

Use-case (the reason this exists): the EV is charging on the optimizer's schedule but
the user needs to drive it now — Force Off stops the charger without GridLens flipping
it straight back on; Auto later hands control back.

Persistence: RestoreEntity. A restored Force state re-arms the override WITHOUT touching
the hardware (actuate=False — the leave-as-is deadman discipline on restart).
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

OPTION_AUTO = "Auto"
OPTION_FORCE_ON = "Force On"
OPTION_FORCE_OFF = "Force Off"
_OPTION_TO_MODE = {OPTION_AUTO: None, OPTION_FORCE_ON: "on", OPTION_FORCE_OFF: "off"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    load_mgr = hass.data[DOMAIN].get(f"{entry.entry_id}_load_control")
    if load_mgr is None:
        return
    entities = [
        GridLensLoadOverrideSelect(load_mgr, entry, index, controller)
        for index, controller in load_mgr.controllers.items()
    ]
    if entities:
        async_add_entities(entities)


class GridLensLoadOverrideSelect(RestoreEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:hand-back-right"
    _attr_options = [OPTION_AUTO, OPTION_FORCE_ON, OPTION_FORCE_OFF]

    def __init__(self, manager, entry: ConfigEntry, index: int, controller) -> None:
        self._manager = manager
        self._index = index
        self._controller = controller
        self._attr_name = f"{controller.name} Override"
        self._attr_unique_id = f"{entry.entry_id}_deferrable_override_mode_{index}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_current_option = OPTION_AUTO

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in _OPTION_TO_MODE:
            self._attr_current_option = last.state
            mode = _OPTION_TO_MODE[last.state]
            if mode is not None:
                # Re-arm a persisted override across restart without commanding the
                # hardware — the override's job is to keep GridLens hands-off, and
                # forcing a switch write during startup would violate leave-as-is.
                await self._manager.set_override(self._index, mode, actuate=False)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        # `switch` doubles as the join key the load-control card uses to pair this
        # selector with the device's control switch entity (both expose the same
        # physical switch entity_id), so no install-specific config is needed.
        return {
            "name": self._controller.name,
            "switch": self._controller.join_key,
            "override": self._manager.get_override(self._index) or "auto",
        }

    async def async_select_option(self, option: str) -> None:
        if option not in _OPTION_TO_MODE:
            return
        await self._manager.set_override(self._index, _OPTION_TO_MODE[option])
        self._attr_current_option = option
        self.async_write_ha_state()
