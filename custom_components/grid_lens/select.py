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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import charge_timing_preference as ctp
from .const import (
    DOMAIN,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_SOC_SENSORS,
    CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH,
)
from .entity_lookup import resolve_device_name

_LOGGER = logging.getLogger(__name__)

OPTION_AUTO = "Auto"
OPTION_FORCE_ON = "Force On"
OPTION_FORCE_OFF = "Force Off"
_OPTION_TO_MODE = {OPTION_AUTO: None, OPTION_FORCE_ON: "on", OPTION_FORCE_OFF: "off"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entities = []

    load_mgr = hass.data[DOMAIN].get(f"{entry.entry_id}_load_control")
    if load_mgr is not None:
        entities.extend(
            GridLensLoadOverrideSelect(load_mgr, entry, index, controller)
            for index, controller in load_mgr.controllers.items()
        )

    battery_mgr = hass.data[DOMAIN].get(f"{entry.entry_id}_control")
    if battery_mgr is not None:
        entities.append(GridLensBatteryControlOverrideSelect(battery_mgr, entry))

    # One "charge timing preference" per SOC-tracked deferrable device — same
    # gating as number.py's ad-hoc charge-target percent entity (needs a live
    # SOC reading + capacity for the floor this preference is tie-breaking to
    # be meaningful at all).
    sensors = entry.data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
    soc_sensors = entry.data.get(CONF_DEFERRABLE_LOAD_SOC_SENSORS, [])
    soc_capacities = entry.data.get(CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH, [])
    if soc_sensors:
        timing_store = hass.data.get(DOMAIN, {}).get(
            f"{entry.entry_id}_charge_timing_preferences"
        )
        for i, soc_sensor_id in enumerate(soc_sensors):
            capacity = soc_capacities[i] if i < len(soc_capacities) else 0.0
            if not soc_sensor_id or not capacity:
                continue
            if i >= len(sensors) or not sensors[i]:
                continue
            name = resolve_device_name(hass, sensors[i])
            entities.append(
                GridLensChargeTimingPreferenceSelect(entry, timing_store, sensors[i], name)
            )

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
        # Keyed by the device's own sensor_id (its CONF_DEFERRABLE_LOAD_SENSORS entry),
        # not `index` — `index` is just this device's current position in the config
        # list and shifts on a reorder/insertion, which would otherwise re-arm a
        # restored Force On/Off meant for a different device (see
        # __init__.py._migrate_deferrable_positional_unique_ids).
        self._attr_unique_id = (
            f"{entry.entry_id}_deferrable_override_mode_{controller.sensor_id or index}"
        )
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


class GridLensChargeTimingPreferenceSelect(SelectEntity):
    """Prefer early / No preference / Prefer just-in-time for one SOC-tracked
    deferrable load's floor-satisfying charge — the everyday day-0 ceiling, or
    an active ad-hoc charge target (§9a) — see charge_timing_preference.py for
    the maths and why this exists (in short: without it, the LP is free to
    front-load the charge across cost-tied slots, leaving the device sitting
    fully charged for hours before it's actually needed).

    Reads/writes through the shared ChargeTimingPreferenceStore rather than
    RestoreEntity, same reasoning as GridLensChargeTargetPercentNumber: the
    store is the single source of truth AdvisoryCoordinator reads from
    directly, so there is nothing for RestoreEntity to usefully duplicate.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:battery-clock-outline"
    _attr_options = list(ctp.LABELS.values())

    def __init__(self, entry: ConfigEntry, store, sensor_id: str, name: str) -> None:
        self._store = store
        self._entry_id = entry.entry_id
        self._sensor_id = sensor_id
        self._attr_name = f"{name} Charge Timing Preference"
        self._attr_unique_id = f"{entry.entry_id}_charge_timing_preference_{sensor_id}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_current_option = ctp.LABELS[ctp.DEFAULT]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._refresh_from_store()
        if self._store is not None:
            from .charge_timing_preference_store import update_signal

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
        if self._store is None:
            return
        preference = await self._store.async_get(self._sensor_id)
        self._attr_current_option = ctp.LABELS.get(preference, ctp.LABELS[ctp.DEFAULT])

    @property
    def extra_state_attributes(self) -> dict:
        # Real state attributes so grid-lens-charge-target-card.js can pair this with the
        # matching percent/time row, the same way number.py's charge_target_role
        # disambiguates its own deferrable_sensor_id use from Today Boost's. Plain
        # deferrable_sensor_id alone isn't a safe fingerprint for a select entity — the
        # Force On/Auto/Off override select (GridLensLoadOverrideSelect) is also a
        # select.* entity on a deferrable device, so charge_timing_preference_role is
        # the actual discriminator the card scans for.
        return {
            "deferrable_sensor_id": self._sensor_id,
            "charge_timing_preference_role": "select",
        }

    async def async_select_option(self, option: str) -> None:
        preference = ctp.VALUES_BY_LABEL.get(option, ctp.DEFAULT)
        if self._store is not None:
            await self._store.async_set(self._sensor_id, preference)
        self._attr_current_option = ctp.LABELS[preference]
        self.async_write_ha_state()


OPTION_BATTERY_AUTO = "Auto"
OPTION_BATTERY_PAUSE = "Pause"
OPTION_BATTERY_FORCE_SELF_USE = "Maximum Self-consumption"
OPTION_BATTERY_FORCE_IDLE = "Standby"
OPTION_BATTERY_FORCE_CHARGE = "Force Charge"
OPTION_BATTERY_FORCE_DISCHARGE = "Force Discharge"

# Maps a forced-action option directly to the BatteryAction value ControlManager/
# BatteryController use — see control/manager.py's override_force() and
# ../inverters/base.py's BatteryAction enum, whose values are these exact strings.
_FORCE_ACTION_BY_OPTION = {
    OPTION_BATTERY_FORCE_SELF_USE: "self_use",
    OPTION_BATTERY_FORCE_IDLE: "idle",
    OPTION_BATTERY_FORCE_CHARGE: "charge",
    OPTION_BATTERY_FORCE_DISCHARGE: "discharge",
}


class GridLensBatteryControlOverrideSelect(SelectEntity):
    """Override GridLens's battery actuation for a duration, then resume Auto (the
    plan) automatically — the timed counterpart to the indefinite Battery Control
    switch. That switch's OFF has no natural end and once sat off for a full day with
    nobody noticing (see control/manager.py's async_initialize() docstring for the
    2026-08-01 incident); this gives a one-tap way to say "something else for the next
    N hours" without having to remember to flip it back.

    Two different kinds of override share this one entity (distinguished by
    ControlManager.override_action — None for the first, set for the second):

    * **Pause** — hands actuation off to the native EMS entirely, same as flipping the
      switch off, just timed (control/manager.py's override_pause()).
    * **Maximum Self-consumption / Standby / Force Charge / Force Discharge** — GridLens
      stays in control and actively commands that specific action
      (control/manager.py's override_force()), the same four guardrailed
      BatteryController primitives the automatic plan executor itself uses. Force
      Charge/Discharge read their rate from number.py's
      GridLensBatteryOverridePowerNumber; every option reads its duration from
      GridLensBatteryOverrideMinutesNumber.

    Not RestoreEntity — ControlManager's own Store is the source of truth for
    override_until/override_label/override_action (so it survives a restart with the
    real remaining time, not whatever label happened to be selected last — see
    ControlManager.async_initialize()). This entity just mirrors it, same relationship
    as the switch has with `enabled`.
    """

    _attr_has_entity_name = True
    _attr_name = "Battery Control Override"
    _attr_icon = "mdi:timer-pause-outline"
    _attr_options = [
        OPTION_BATTERY_AUTO,
        OPTION_BATTERY_PAUSE,
        OPTION_BATTERY_FORCE_SELF_USE,
        OPTION_BATTERY_FORCE_IDLE,
        OPTION_BATTERY_FORCE_CHARGE,
        OPTION_BATTERY_FORCE_DISCHARGE,
    ]

    def __init__(self, manager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_battery_control_override"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_current_option = OPTION_BATTERY_AUTO

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._manager.set_override_listener(self._on_manager_change)
        self._sync_from_manager()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._manager.set_override_listener(None)
        await super().async_will_remove_from_hass()

    def _on_manager_change(self) -> None:
        self._sync_from_manager()
        self.async_write_ha_state()

    def _sync_from_manager(self) -> None:
        if self._manager.override_until is None:
            self._attr_current_option = OPTION_BATTERY_AUTO
            return
        label = self._manager.override_label
        self._attr_current_option = label if label in self._attr_options else OPTION_BATTERY_PAUSE

    @property
    def extra_state_attributes(self) -> dict:
        until = self._manager.override_until
        return {
            "override_until": until.isoformat() if until else None,
            "override_action": self._manager.override_action,
            "override_power_w": self._manager.override_power_w,
            "battery_control_override_role": "select",
        }

    async def async_select_option(self, option: str) -> None:
        if option == OPTION_BATTERY_AUTO:
            await self._manager.cancel_override()
        elif option == OPTION_BATTERY_PAUSE:
            await self._manager.override_pause(
                self._manager.custom_override_minutes, label=option
            )
        elif option in _FORCE_ACTION_BY_OPTION:
            await self._manager.override_force(
                _FORCE_ACTION_BY_OPTION[option],
                self._manager.custom_override_minutes,
                power_w=self._manager.custom_override_power_w,
                label=option,
            )
        else:
            return
        self._attr_current_option = option
        self.async_write_ha_state()
