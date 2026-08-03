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
            entities.append(
                GridLensDeferrableGreedySwitch(load_mgr, entry, index, controller.name)
            )
            entities.append(
                GridLensDeferrableGreedyScheduleSwitch(load_mgr, entry, index, controller.name)
            )
            entities.append(
                GridLensDeferrableGreedySurplusSwitch(load_mgr, entry, index, controller.name)
            )

    if entities:
        async_add_entities(entities)


class GridLensBatteryControlSwitch(SwitchEntity):
    """ON = GridLens actuates the battery per the advisory plan (guardrailed).

    User intent persists across restarts and defaults ON, so control persists (an HA
    restart doesn't silently stop optimising the battery) — but via ControlManager's own
    Store (see async_initialize()), NOT this entity's RestoreEntity state. The manager
    restores itself before this entity is even created (async_setup_entry calls
    async_initialize() ahead of forwarding the switch platform), so by the time
    async_added_to_hass runs here, `self._manager.enabled` already reflects the outcome —
    this entity just displays it. (RestoreEntity was tried first and dropped: the reload
    deadman always forces this switch to "off" before teardown, so its restored state
    reflected transient reload timing rather than genuine user intent — see
    ControlManager.async_initialize()'s docstring for the 2026-08-01 incident.) The
    HA-stop deadman still hands back to native during the shutdown window; the manager
    re-engages on the next startup from its own persisted intent.
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


class GridLensDeferrableGreedySwitch(RestoreEntity, SwitchEntity):
    """ON = this device opportunistically turns on whenever import price is free or
    export is being wasted (Greedy Consumption), on top of whatever Auto/plan control
    already does. Defaults OFF (opt-in, same reasoning as the load-control enable switch).
    Has no effect while a Force On/Off override is active (see select.py) — greedy only
    applies in Auto/managed mode.

    Deliberately does NOT wire a manager state-listener callback: LoadControlManager's
    ``set_state_listener(index, cb)`` slot is one-callback-per-device-index and is already
    claimed by GridLensDeferrableLoadSwitch (see ``_on_manager_change`` above) — attaching
    here would silently overwrite that registration and break the enable switch's live sync.
    Greedy is rarely toggled from anywhere except this entity itself, so skipping push-update
    wiring is a deliberate, low-risk simplification rather than an oversight.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:leaf"

    def __init__(self, manager, entry: ConfigEntry, index: int, device_name: str) -> None:
        self._manager = manager
        self._index = index
        self._attr_name = f"{device_name} Greedy Consumption"
        self._attr_unique_id = f"{entry.entry_id}_deferrable_greedy_{index}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        want_on = last is not None and last.state == "on"  # default OFF if no prior state
        if want_on:
            await self._manager.set_greedy(self._index, True)
        self._attr_is_on = self._manager.is_greedy(self._index)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        # `switch` is the same join key select.py's override selector and the load-control
        # card already use to pair auxiliary entities with a device's physical appliance
        # switch; `role` disambiguates this entity from GridLensDeferrableGreedyScheduleSwitch
        # below, since both otherwise carry an identically-shaped `switch` attribute.
        return {"switch": self._manager.controllers[self._index].join_key, "role": "greedy"}

    async def async_turn_on(self, **kwargs) -> None:
        await self._manager.set_greedy(self._index, True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._manager.set_greedy(self._index, False)
        self._attr_is_on = False
        self.async_write_ha_state()


class GridLensDeferrableGreedyScheduleSwitch(RestoreEntity, SwitchEntity):
    """ON = Greedy Consumption only fires during this device's own configured availability
    window/weekly schedule; OFF = greedy can fire any time the price/export condition is
    true, ignoring the schedule. Defaults OFF (greedy is opportunistic by default — don't
    leave free energy on the table).

    Same deliberate simplification as GridLensDeferrableGreedySwitch above: no manager
    state-listener wiring, since that callback slot is already owned by
    GridLensDeferrableLoadSwitch and this toggle is rarely changed from elsewhere.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, manager, entry: ConfigEntry, index: int, device_name: str) -> None:
        self._manager = manager
        self._index = index
        self._attr_name = f"{device_name} Greedy Respects Schedule"
        self._attr_unique_id = f"{entry.entry_id}_deferrable_greedy_schedule_{index}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        want_on = last is not None and last.state == "on"  # default OFF if no prior state
        if want_on:
            await self._manager.set_greedy_respects_schedule(self._index, True)
        self._attr_is_on = self._manager.is_greedy_respects_schedule(self._index)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        return {"switch": self._manager.controllers[self._index].join_key,
                "role": "greedy_schedule"}

    async def async_turn_on(self, **kwargs) -> None:
        await self._manager.set_greedy_respects_schedule(self._index, True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._manager.set_greedy_respects_schedule(self._index, False)
        self._attr_is_on = False
        self.async_write_ha_state()


class GridLensDeferrableGreedySurplusSwitch(RestoreEntity, SwitchEntity):
    """ON = Greedy Consumption may ALSO fire on forecast surplus: start this device now
    when the plan says more free energy will be thrown away over the next few hours than
    the device could consume running flat out for that whole window.

    Separate from the master Greedy switch (and requires it) because this is the one
    greedy condition that can genuinely cost money in the moment — the other two only
    fire on energy that is already free right now, this one starts early to catch a spill
    that hasn't arrived yet. Defaults OFF, like every other load-control opt-in.

    Same deliberate simplification as the two greedy switches above: no manager
    state-listener wiring (that slot is owned by GridLensDeferrableLoadSwitch).
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-sunny-alert"

    def __init__(self, manager, entry: ConfigEntry, index: int, device_name: str) -> None:
        self._manager = manager
        self._index = index
        self._attr_name = f"{device_name} Greedy Forecast Surplus"
        self._attr_unique_id = f"{entry.entry_id}_deferrable_greedy_surplus_{index}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        want_on = last is not None and last.state == "on"  # default OFF if no prior state
        if want_on:
            await self._manager.set_greedy_forecast_surplus(self._index, True)
        self._attr_is_on = self._manager.is_greedy_forecast_surplus(self._index)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        return {"switch": self._manager.controllers[self._index].join_key,
                "role": "greedy_surplus"}

    async def async_turn_on(self, **kwargs) -> None:
        await self._manager.set_greedy_forecast_surplus(self._index, True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._manager.set_greedy_forecast_surplus(self._index, False)
        self._attr_is_on = False
        self.async_write_ha_state()
