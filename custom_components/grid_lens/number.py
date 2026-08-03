"""Live-adjustable settings exposed as HA number entities — deliberately on the
dashboard/device page rather than only in the config-flow options, since these
are tuning knobs a user changes often, not one-time setup values.
"""
from __future__ import annotations

import inspect
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
    CONF_DEFERRABLE_LOAD_SETPOINT,
    CONF_DEFERRABLE_LOAD_MIN_CURRENT,
    DEFAULT_SUPPLY_VOLTAGE,
    DEFAULT_MIN_CHARGE_CURRENT_A,
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


def _resolve_max_current_a(hass: HomeAssistant, controller, setpoint_id: str, max_kw: float) -> float:
    """Ceiling for the Max Current number's own ``native_max_value`` — the setpoint
    entity's own advertised max (A) if it publishes one (standard HA number-entity state
    attributes ``max``/``native_max_value``), else this device's configured max_kw
    converted to A via the controller's already-resolved voltage/phases
    (``controller.status()`` — see MODULATING_CONTRACT.md §3.4), falling back to
    DEFAULT_SUPPLY_VOLTAGE/single-phase if no controller instance exists yet (setup
    ordering — the modulating controller may not be constructed the very first time
    entities are added). This is only the entity's *default ceiling value* (the user can
    always dial it down), not a control-loop parameter, so an approximate fallback here
    is harmless — the controller's own clamp (cap_w) is what actually protects the
    hardware, using its own voltage/phases, independent of what this number shows.
    """
    state = hass.states.get(setpoint_id)
    if state is not None:
        raw = state.attributes.get("max", state.attributes.get("native_max_value"))
        try:
            if raw is not None and float(raw) > 0:
                return float(raw)
        except (TypeError, ValueError):
            pass
    voltage = DEFAULT_SUPPLY_VOLTAGE
    phases = 1
    if controller is not None:
        try:
            status = controller.status()
            voltage = float(status.get("voltage") or DEFAULT_SUPPLY_VOLTAGE)
            phases = int(status.get("phases") or 1)
        except Exception:  # noqa: BLE001 — a status() hiccup must not block entity setup
            voltage, phases = DEFAULT_SUPPLY_VOLTAGE, 1
    if voltage <= 0:
        voltage = DEFAULT_SUPPLY_VOLTAGE
    if phases <= 0:
        phases = 1
    if max_kw <= 0:
        return DEFAULT_MIN_CHARGE_CURRENT_A
    return round(max_kw * 1000.0 / (voltage * phases), 1)


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

    # One "max current" ceiling per modulating ("type 2") deferrable device — a user cap
    # on the current GridLens may command, on top of whatever the plan/greedy logic
    # decides (MODULATING_CONTRACT.md §6). Gated on CONF_DEFERRABLE_LOAD_SETPOINT[i] being
    # set, exactly like ModulatingLoadController's own type-2 test — this key is absent
    # from every config entry saved before this feature, so an existing on/off or
    # forecast-only device gains no new entity here.
    setpoints = entry.data.get(CONF_DEFERRABLE_LOAD_SETPOINT, [])
    if setpoints:
        min_currents = entry.data.get(CONF_DEFERRABLE_LOAD_MIN_CURRENT, [])
        max_kws = entry.data.get(CONF_DEFERRABLE_LOAD_MAX_KW, [])
        # LoadControlManager builds one ModulatingLoadController per modulating device,
        # keyed by the same list index as everywhere else (control/load_control_manager.py).
        # It may legitimately be absent (setup failed, or this is the very first entity
        # pass before the manager exists) — GridLensModulatingMaxCurrentNumber tolerates a
        # None controller and simply can't push its value anywhere until one shows up.
        load_mgr = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_load_control")
        for i, setpoint_id in enumerate(setpoints):
            if not setpoint_id:
                continue
            controller = load_mgr.controllers.get(i) if load_mgr is not None else None
            name = _device_display_name(hass, sensors[i]) if i < len(sensors) else setpoint_id
            min_a = min_currents[i] if i < len(min_currents) and min_currents[i] else DEFAULT_MIN_CHARGE_CURRENT_A
            max_kw = max_kws[i] if i < len(max_kws) else 0.0
            max_a = _resolve_max_current_a(hass, controller, setpoint_id, float(max_kw))
            entities.append(
                GridLensModulatingMaxCurrentNumber(
                    entry, controller, i, name, setpoint_id, float(min_a), max_a,
                    sensor_id=sensors[i] if i < len(sensors) else "",
                )
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
    """Override of one deferrable device's daily kWh target — e.g. "charge the EV
    more today, I'm driving far". Beats the 14-day historical average
    AdvisoryCoordinator would otherwise use, until explicitly cleared back to 0
    (see deferrable_overrides.DeferrableOverrideStore) — it does NOT auto-revert
    at midnight. A carried-over boost raises a persistent notification once per
    day instead of silently reverting or silently running forever.

    Reads/writes through the shared DeferrableOverrideStore rather than RestoreEntity:
    the store is the single source of truth for whether an override is set, so
    mirroring it through RestoreEntity would just be a second, potentially
    inconsistent copy of the same value.
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

    @property
    def extra_state_attributes(self) -> dict:
        # Real state attribute (unlike unique_id, which is registry-only and invisible
        # to the frontend) so Lovelace cards can auto-discover boost entities by
        # fingerprint — see grid-lens-boost-tuning-card.js's _resolveBoosts().
        return {"deferrable_sensor_id": self._sensor_id}

    async def async_set_native_value(self, value: float) -> None:
        if self._store is not None:
            await self._store.async_set(self._sensor_id, value)
        self._attr_native_value = value if value > 0 else 0.0
        self.async_write_ha_state()


class GridLensModulatingMaxCurrentNumber(RestoreEntity, NumberEntity):
    """User ceiling (A) on the current GridLens may command a modulating ("type 2") EV
    charger — a `number.*` setpoint entity configured per MODULATING_CONTRACT.md §2/§6,
    as opposed to a plain on/off `switch.*`/`climate.*` load. Wired straight through to
    the device's ``ModulatingLoadController.set_current_cap_a()``, which folds it into
    the commanded envelope on every fast (30 s) tick: ``cap_w = min(max_w, this ceiling,
    the setpoint entity's own native max)`` — see ``modulate()``'s clamp step in
    control/modulating_controller.py. This entity never talks to hardware itself; it only
    ever narrows what the controller is allowed to command.

    RestoreEntity, not the shared-Store pattern GridLensDeferrableOverrideNumber uses
    above: this is a durable "don't let GridLens command more than X A on this circuit"
    limit a user sets once and rarely revisits, not a rolling daily override with its own
    carry-over-notification semantics — a plain restored number is the right amount of
    persistence here (see feedback_restore_entity_deadman_race in project memory for the
    one case where RestoreEntity was rejected for GridLens control state: that one had a
    deadman that force-clears intent on every reload, which doesn't apply to this value —
    nothing here ever resets it out from under the user).

    Defaults to its own max (no restriction out of the box) — the point of this control
    is "let the user turn it DOWN from what the hardware/plan would otherwise use", not
    "start pre-throttled and confuse everyone about why charging is slow".
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:current-ac"
    _attr_native_unit_of_measurement = "A"
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        entry: ConfigEntry,
        controller,
        index: int,
        name: str,
        setpoint_entity_id: str,
        min_a: float,
        max_a: float,
        sensor_id: str = "",
    ) -> None:
        self._controller = controller
        self._index = index
        self._setpoint_entity_id = setpoint_entity_id
        self._sensor_id = sensor_id
        self._attr_name = f"{name} Max Current"
        self._attr_unique_id = f"{entry.entry_id}_deferrable_max_current_{index}"
        self._attr_native_min_value = min_a
        self._attr_native_max_value = max_a
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }
        self._attr_native_value = max_a  # unrestricted out of the box

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable"):
            try:
                self._attr_native_value = max(
                    self._attr_native_min_value,
                    min(self._attr_native_max_value, float(last.state)),
                )
            except ValueError:
                pass
        # Push the restored (or default) ceiling into the controller now — otherwise a
        # restart would silently leave the controller at ITS OWN default (unrestricted)
        # until the user happens to touch this slider again, quietly discarding a
        # previously-set limit for a whole session.
        await self._push_to_controller(self._attr_native_value)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        # Three join keys, because a card has to find this entity by attribute
        # fingerprint alone (never by naming convention — FEATURES.md §10):
        # * `deferrable_sensor_id` is THE canonical per-device key across this
        #   integration — the schedule store, Today Boost and the deferrable_loads sensor
        #   attribute all key off the device's energy sensor id, so a card that already
        #   resolved a device has this in hand.
        # * `role` disambiguates which of a device's several GridLens-owned numbers this
        #   is, matching the greedy switches' own `role` discriminator in switch.py.
        # * `setpoint_entity` is the fallback for a modulating device whose energy sensor
        #   is a synthetic/estimated one, where the card may only know the setpoint.
        return {
            "deferrable_sensor_id": self._sensor_id,
            "role": "max_current",
            "setpoint_entity": self._setpoint_entity_id,
        }

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        await self._push_to_controller(value)
        self.async_write_ha_state()

    async def _push_to_controller(self, value: float) -> None:
        # The controller may not exist yet (setup ordering, entitlement pending) or may
        # not be a modulating one (config drift, e.g. the setpoint entity was removed out
        # from under an existing device) — set_current_cap_a() is only guaranteed on
        # ModulatingLoadController, never on the plain on/off DeferrableLoadController.
        # Guard rather than assume, and never let a controller-side hiccup break this
        # entity's own write — same "never raise" discipline as every actuation path in
        # this integration.
        setter = getattr(self._controller, "set_current_cap_a", None)
        if setter is None:
            return
        try:
            result = setter(value)
            if inspect.isawaitable(result):
                await result
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Max Current ceiling push failed for %s: %s", self._attr_name, err)
