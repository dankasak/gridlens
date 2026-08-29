"""Config flow for Grid Lens."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import instance_id, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv

from .deferrable_loads import (
    CONTROL_MODULATING,
    CONTROL_NONE,
    CONTROL_ONOFF,
    DECLARED,
    ESTIMATED,
    MONITORED,
    apply_control_style,
    clear_controlled_load,
    clear_soc,
    control_style,
    new_load,
    read_loads,
    write_loads,
)
from .const import (
    DOMAIN,
    CONF_ENERGY_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_IMPORT_PRICE_SENSOR,
    CONF_EXPORT_PRICE_SENSOR,
    CONF_DISTRIBUTOR,
    CONF_HAS_DEMAND_TARIFF,
    CONF_HAS_CONTROLLED_LOAD_1,
    CONF_HAS_CONTROLLED_LOAD_2,
    CONF_STATE,
    CONF_HAS_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_MAX_CHARGE_RATE,
    CONF_BATTERY_MAX_DISCHARGE_RATE,
    CONF_BATTERY_EFFICIENCY,
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_CHARGE_POWER_SENSOR,
    CONF_BATTERY_DISCHARGE_POWER_SENSOR,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_MAX_SOC,
    CONF_INVERTER_BRAND,
    CONF_INVERTER_TRANSPORT,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_SWITCHES,
    CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE,
    CONF_DEFERRABLE_LOAD_SETPOINT,
    CONF_DEFERRABLE_LOAD_SETPOINT_UNIT,
    CONF_DEFERRABLE_LOAD_PHASES,
    CONF_DEFERRABLE_LOAD_VOLTAGE,
    CONF_DEFERRABLE_LOAD_MIN_CURRENT,
    CONF_DEFERRABLE_LOAD_PLUG_SENSOR,
    CONF_DEFERRABLE_LOAD_SOC_SENSORS,
    CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT,
    CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH,
    CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD,
    CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE,
    DEFERRABLE_LOAD_DUMMY_SLOTS,
    CONF_DEFERRABLE_LOAD_DUMMY_NAMES,
    CONF_DEFERRABLE_LOAD_DUMMY_KWH,
    CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW,
    CONF_DEFERRABLE_LOAD_DUMMY_HOURS,
    CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD,
    CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE,
    DEFERRABLE_LOAD_ESTIMATED_SLOTS,
    CONF_DEFERRABLE_LOAD_EST_NAMES,
    CONF_DEFERRABLE_LOAD_EST_CONTROL,
    CONF_DEFERRABLE_LOAD_EST_KW,
    CONF_DEFERRABLE_LOAD_EST_AUTO,
    CONF_LOAD_POWER_SENSOR,
    CONF_CURRENT_PLAN,
    CONF_VPP_PROGRAM,
    parse_hours_spec,
    CONF_GRIDLENS_EMAIL,
    CONF_GRIDLENS_API_URL,
    CONF_GRIDLENS_API_KEY,
    GRIDLENS_DEFAULT_API_URL,
    STATES,
    DISTRIBUTORS,
)
from .credentials import async_load_credentials, async_save_credentials
from .inverters import INVERTER_BRANDS, detect_inverter_brand

_LOGGER = logging.getLogger(__name__)

# Transient form field for the brand:transport dropdown — split into
# CONF_INVERTER_BRAND / CONF_INVERTER_TRANSPORT on submit, not persisted as-is.
_FIELD_INVERTER_SELECT = "inverter_select"

# Cloudflare bot protection in front of api.gridlens.au rejects requests that look
# like a bare Python client, so every call out of this module carries it.
_USER_AGENT = "GridLens-HA-Integration/1.0"

# Battery tuning parameters the setup flow no longer asks about (they remain editable
# in the options flow). Written explicitly into the entry rather than left absent —
# the optimiser and ControlManager's guardrails read these keys directly.
_BATTERY_ADVANCED_DEFAULTS = {
    CONF_BATTERY_EFFICIENCY: 95.0,
    CONF_BATTERY_MIN_SOC: 10.0,
    CONF_BATTERY_MAX_SOC: 90.0,
}

# Sensors that must be cumulative energy counters, not instantaneous power. All three
# carry the identical requirement; only the import sensor used to be checked, so a
# solar or export sensor reading watts was accepted here and quietly mispriced every
# comparison downstream.
_CUMULATIVE_ENERGY_KEYS = (
    CONF_ENERGY_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
)


def _validate_energy_sensors(hass: HomeAssistant, user_input: dict) -> dict[str, str]:
    """Return {field: error_key} for any energy sensor that isn't a kWh total."""
    errors: dict[str, str] = {}
    for key in _CUMULATIVE_ENERGY_KEYS:
        entity_id = user_input.get(key)
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if not state:
            continue
        unit = state.attributes.get("unit_of_measurement", "").lower()
        state_class = state.attributes.get("state_class", "")
        if unit in ("w", "kw", "mw"):
            errors[key] = "wrong_unit_power"
        elif unit not in ("kwh", "mwh"):
            errors[key] = "wrong_unit"
        elif state_class not in ("total", "total_increasing"):
            errors[key] = "wrong_state_class"
    return errors


async def _discover_dashboard_devices(hass: HomeAssistant) -> list[dict]:
    """Return Energy Dashboard device_consumption entries as SelectSelector options."""
    try:
        from homeassistant.components.energy import data as energy_data
        manager = await energy_data.async_get_manager(hass)
        if not manager.data:
            return []
        options = []
        for dev in manager.data.get("device_consumption", []):
            sensor_id = dev.get("stat_consumption")
            if not sensor_id:
                continue
            name = dev.get("name") or sensor_id
            options.append({"value": sensor_id, "label": f"{name} ({sensor_id})"})
        return options
    except Exception as exc:
        _LOGGER.warning("Could not read Energy Dashboard device list: %s", exc)
        return []


async def _discover_energy_sensors(hass: HomeAssistant) -> dict:
    """Read sensor entity IDs from the HA Energy dashboard configuration."""
    try:
        from homeassistant.components.energy import data as energy_data
        manager = await energy_data.async_get_manager(hass)
        if not manager.data:
            return {}

        result = {}
        for source in manager.data.get("energy_sources", []):
            stype = source.get("type")
            if stype == "grid":
                # HA stores grid import/export directly on the source object
                if source.get("stat_energy_from"):
                    result[CONF_ENERGY_SENSOR] = source["stat_energy_from"]
                if source.get("stat_energy_to"):
                    result[CONF_GRID_EXPORT_SENSOR] = source["stat_energy_to"]
                # Price sensors are also stored here
                if source.get("entity_energy_price"):
                    result[CONF_IMPORT_PRICE_SENSOR] = source["entity_energy_price"]
                if source.get("entity_energy_price_export"):
                    result[CONF_EXPORT_PRICE_SENSOR] = source["entity_energy_price_export"]
            elif stype == "solar":
                if source.get("stat_energy_from"):
                    result[CONF_SOLAR_SENSOR] = source["stat_energy_from"]
        return {k: v for k, v in result.items() if v}
    except Exception as exc:
        _LOGGER.warning("Could not read Energy dashboard config: %s", exc)
        return {}


async def _discover_grid_power_sensor(hass: HomeAssistant) -> str | None:
    """The live signed grid power entity, recovered from the user's own Power Flow card.

    ``grid_power_sensor`` is the one energy field ``_discover_energy_sensors`` can never
    supply: it reads the HA Energy dashboard, which stores cumulative energy statistics
    only — there is no live power entity in it to borrow. So the field starts blank on
    every install, and the feature that depends on it (Greedy Consumption's export-surplus
    condition, control/load_controller.py) is silently unavailable until someone notices.

    But an install that has set up the Power Flow card has *already told Grid Lens this
    exact fact*, under the card's own ``grid_power_entity`` option — same meaning, same
    sign convention, chosen by the same person for the same house. Asking a second time is
    the thing the user is entitled to object to. So: read it back.

    Deliberately NOT a name/device_class heuristic over ``hass.states``. "A power sensor
    with 'grid' in the name" would happily match an unsigned import-only register, and
    greedy would then read a positive import as "not exporting" forever — silently wrong
    beats visibly absent. This source is a real answer the user gave, or nothing.

    Returns None on any problem: no card configured, lovelace unavailable, storage-mode
    dashboards not readable. Callers pre-fill with it; they never depend on it.
    """
    lovelace_data = hass.data.get("lovelace")
    if not lovelace_data or not hasattr(lovelace_data, "dashboards"):
        return None
    try:
        for dashboard in (lovelace_data.dashboards or {}).values():
            if dashboard is None or not hasattr(dashboard, "async_load"):
                continue
            try:
                config = await dashboard.async_load(False)
            except Exception:  # noqa: BLE001 — a YAML-mode or empty dashboard just skips
                continue
            for view in (config or {}).get("views", []) or []:
                for card in _iter_cards(view.get("cards", []) or []):
                    entity = card.get("grid_power_entity")
                    if entity and hass.states.get(entity) is not None:
                        return entity
    except Exception as exc:  # noqa: BLE001 — discovery is a convenience, never a blocker
        _LOGGER.debug("Could not scan dashboards for a grid power entity: %s", exc)
    return None


def _iter_cards(cards):
    """Yield every card dict in a view, descending into nested/stacked cards.

    A Power Flow card is very often inside a grid/vertical-stack rather than at the top
    level of a view, so a flat scan would miss the common case.
    """
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        yield card
        yield from _iter_cards(card.get("cards", []) or [])


async def _discover_has_battery(hass: HomeAssistant) -> bool:
    """Whether HA's own Energy dashboard already knows about a home battery.

    Only used to pre-tick the battery checkbox — a household that has configured a
    battery source on the Energy dashboard shouldn't have to tell us again.
    """
    try:
        from homeassistant.components.energy import data as energy_data
        manager = await energy_data.async_get_manager(hass)
        if not manager.data:
            return False
        return any(
            source.get("type") == "battery"
            for source in manager.data.get("energy_sources", [])
        )
    except Exception as exc:
        _LOGGER.warning("Could not read Energy dashboard battery config: %s", exc)
        return False


# Every field _energy_schema presents. Anything listed here is fully user-editable,
# so an absent value on submit means "cleared" and must overwrite what's stored.
_ENERGY_SCHEMA_KEYS = (
    CONF_ENERGY_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_IMPORT_PRICE_SENSOR,
    CONF_EXPORT_PRICE_SENSOR,
)


def _energy_schema(defaults: dict) -> vol.Schema:
    """Build the energy sensors schema, pre-filling discovered values."""

    def entity_sel():
        return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))

    def opt(key):
        if defaults.get(key):
            # suggested_value, not default: an EntitySelector the user clears comes
            # back absent from user_input, and a `default` would silently re-apply
            # the old entity id — making an optional sensor impossible to remove.
            return vol.Optional(key, description={"suggested_value": defaults[key]})
        return vol.Optional(key)

    def req(key):
        if defaults.get(key):
            return vol.Required(key, default=defaults[key])
        return vol.Required(key)

    return vol.Schema({
        req(CONF_ENERGY_SENSOR): entity_sel(),
        opt(CONF_SOLAR_SENSOR): entity_sel(),
        opt(CONF_GRID_EXPORT_SENSOR): entity_sel(),
        opt(CONF_GRID_POWER_SENSOR): entity_sel(),
        opt(CONF_IMPORT_PRICE_SENSOR): entity_sel(),
        opt(CONF_EXPORT_PRICE_SENSOR): entity_sel(),
    })


class GridLensConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Grid Lens."""

    VERSION = 1

    def __init__(self) -> None:
        self._state: str | None = None
        self._distributor: str | None = None
        self._has_cl1: bool = False
        self._has_cl2: bool = False
        self._discovered: dict = {}
        self._device_options: list = []
        self._sensor_data: dict = {}
        self._email: str = ""
        self._api_url: str = GRIDLENS_DEFAULT_API_URL
        self._api_plans: list[dict] = []
        self._plans_by_network: dict[str, list[dict]] = {}
        self._api_vpp_programs: list[dict] = []
        self._api_key: str = ""
        self._ha_uuid: str = ""
        self._api_tier: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return GridLensOptionsFlow(config_entry)

    async def _load_coverage(self) -> tuple[list[str], bool]:
        """Which networks in `self._state` actually have plan data behind them.

        One `/plans/list` call per candidate network, run concurrently. The API has no
        coverage endpoint and plan rows carry no network field, so this is the cheapest
        way to ask "is this user's area supported?" using what's already deployed —
        1–5 requests, once, on the first submit.

        Doubles as a prefetch: the winning network's plan list is exactly what the
        final step's dropdown is built from, so that step no longer does any I/O and
        can no longer fail on a network error after the user has filled in everything.

        Returns `(covered_networks, fetch_ok)`. `fetch_ok` is False only when *every*
        request errored — an empty list with `fetch_ok` True means "we reached the API
        and there genuinely are no plans here", which is a very different message.
        """
        candidates = DISTRIBUTORS.get(self._state, [])
        if not candidates:
            return [], True

        session = async_get_clientsession(self.hass)

        async def _one(network: str) -> tuple[str, list | None]:
            try:
                async with session.get(
                    f"{self._api_url}/plans/list",
                    params={"state": self._state, "network": network},
                    headers={"User-Agent": _USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return network, None
                    return network, await resp.json()
            except Exception as exc:
                _LOGGER.warning(
                    "Coverage check failed for %s/%s: %s", self._state, network, exc
                )
                return network, None

        results = await asyncio.gather(*(_one(n) for n in candidates))
        if all(plans is None for _, plans in results):
            return [], False

        self._plans_by_network = {n: p for n, p in results if p}
        return [n for n, p in results if p], True

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Location and account — the only two answers with no sane default.

        Postcode used to be required here and was read by nothing (not the coordinator,
        not the API, whose endpoints take state and network only). The API URL moved to
        the options flow: a self-hosting override has no business being the fourth field
        a new user sees.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            self._state = user_input[CONF_STATE]
            self._email = user_input[CONF_GRIDLENS_EMAIL]

            covered, fetch_ok = await self._load_coverage()
            if not fetch_ok:
                errors["base"] = "cannot_connect"
            elif not covered:
                return self.async_abort(
                    reason="state_not_supported",
                    description_placeholders={"state": self._state},
                )
            elif len(covered) == 1:
                # Only one network in this state has plan data, so the distributor
                # question has exactly one possible answer — don't ask it.
                self._distributor = covered[0]
                self._api_plans = self._plans_by_network[self._distributor]
                return await self.async_step_sensors()
            else:
                return await self.async_step_distributor()

        schema: dict = {
            vol.Required(CONF_STATE, default=self._state or "NSW"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=STATES, mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
        }
        email_key = (
            vol.Required(CONF_GRIDLENS_EMAIL, description={"suggested_value": self._email})
            if self._email
            else vol.Required(CONF_GRIDLENS_EMAIL)
        )
        schema[email_key] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_distributor(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Only reached when more than one network in the state has plan data — the
        options are the covered ones, so every choice here leads somewhere."""
        if user_input is not None:
            self._distributor = user_input[CONF_DISTRIBUTOR]
            self._api_plans = self._plans_by_network.get(self._distributor, [])
            return await self.async_step_sensors()

        return self.async_show_form(
            step_id="distributor",
            data_schema=vol.Schema({
                vol.Required(CONF_DISTRIBUTOR): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=sorted(self._plans_by_network),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        # Auto-discover on first visit
        if not self._discovered:
            self._discovered = await _discover_energy_sensors(self.hass)
            # Grid power comes from a different source than the rest (see
            # _discover_grid_power_sensor) — the Energy dashboard has no live power entity.
            grid_power = await _discover_grid_power_sensor(self.hass)
            if grid_power:
                self._discovered.setdefault(CONF_GRID_POWER_SENSOR, grid_power)
            if self._discovered:
                _LOGGER.info("Auto-discovered energy sensors: %s", self._discovered)

        if user_input is not None:
            errors = _validate_energy_sensors(self.hass, user_input)

            if not errors:
                data = {
                    CONF_STATE: self._state,
                    CONF_DISTRIBUTOR: self._distributor,
                    # Controlled Load is a meter/DNSP fact most households can't answer
                    # off the top of their head, and it exists only to gate a dropdown
                    # on the advanced load steps. Defaulted off here; set it in
                    # Configure → Reconfigure when it's actually relevant.
                    CONF_HAS_CONTROLLED_LOAD_1: False,
                    CONF_HAS_CONTROLLED_LOAD_2: False,
                    CONF_ENERGY_SENSOR: user_input.get(CONF_ENERGY_SENSOR),
                    CONF_SOLAR_SENSOR: user_input.get(CONF_SOLAR_SENSOR),
                    CONF_GRID_EXPORT_SENSOR: user_input.get(CONF_GRID_EXPORT_SENSOR),
                    CONF_GRID_POWER_SENSOR: user_input.get(CONF_GRID_POWER_SENSOR),
                    CONF_IMPORT_PRICE_SENSOR: user_input.get(CONF_IMPORT_PRICE_SENSOR),
                    CONF_EXPORT_PRICE_SENSOR: user_input.get(CONF_EXPORT_PRICE_SENSOR),
                }
                self._sensor_data = data
                return await self.async_step_battery()

        discovered_count = len(self._discovered)
        description_placeholders = {
            "discovered": f"✓ Auto-detected {discovered_count} sensor(s) from your Energy dashboard — pre-filled below." if discovered_count else "No Energy dashboard configuration found — please select sensors manually.",
        }

        return self.async_show_form(
            step_id="sensors",
            data_schema=_energy_schema(self._discovered),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Battery basics only. Round-trip efficiency and the min/max SOC guardrails
        are tuning parameters with defaults right for almost every install — they stay
        available in Configure → Reconfigure rather than being three more numbers to
        stare at during setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            has_battery = user_input.get(CONF_HAS_BATTERY, False)
            if has_battery:
                capacity = user_input.get(CONF_BATTERY_CAPACITY, 0)
                if not capacity or capacity <= 0:
                    errors[CONF_BATTERY_CAPACITY] = "invalid_capacity"

            if not errors:
                # Advanced defaults are written explicitly, not left absent: the
                # optimiser and ControlManager's guardrails read these keys directly.
                self._sensor_data = {
                    **self._sensor_data,
                    **_BATTERY_ADVANCED_DEFAULTS,
                    **user_input,
                }
                if has_battery:
                    # The inverter question used to be its own screen. Battery control
                    # is a separately-entitled add-on that ships default-off, so the
                    # honest time to ask "where do I send commands?" is when it's
                    # switched on — not during free setup. Record a confident
                    # auto-detection if there is one, and otherwise leave it unset for
                    # the options flow to fill in.
                    detected = detect_inverter_brand(self.hass)
                    if detected:
                        self._sensor_data[CONF_INVERTER_BRAND] = detected[0]
                        self._sensor_data[CONF_INVERTER_TRANSPORT] = detected[1]
                        _LOGGER.info(
                            "Auto-detected inverter %s (%s)", detected[0], detected[1]
                        )
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="battery",
            data_schema=_battery_schema(
                {CONF_HAS_BATTERY: await _discover_has_battery(self.hass)}, basic=True
            ),
            errors=errors,
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Select which Energy Dashboard appliances are deferrable loads."""
        if not self._device_options:
            self._device_options = await _discover_dashboard_devices(self.hass)

        if not self._device_options:
            return await self.async_step_current_plan()

        if user_input is not None:
            self._sensor_data.update(user_input)
            return await self.async_step_device_power()

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema({
                vol.Optional(CONF_DEFERRABLE_LOAD_SENSORS, default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._device_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_device_power(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Max power draw and an optional control switch, per selected device.

        This step used to present ten to twelve fields *per device* — setpoint entity,
        setpoint unit, phase count, supply voltage, minimum current, plug sensor,
        controlled-load register, already-in-aggregate — shown for a dishwasher as
        readily as for a modulating EV charger, and every one of them has a default
        that is right for almost every device. Five selected appliances made a
        fifty-field screen. They all still exist in Configure → Reconfigure, where
        someone setting up a modulating charger will go looking for them.

        Availability windows aren't set here either — paint them on the Deferrable
        Loads dashboard card's weekly schedule after setup (a device is unrestricted,
        any hour, until you do).
        """
        selected = self._sensor_data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
        if not selected:
            return await self.async_step_current_plan()

        if user_input is not None:
            n = len(selected)
            self._sensor_data[CONF_DEFERRABLE_LOAD_MAX_KW] = [
                float(user_input.get(f"max_kw_{i}", 3.5)) for i in range(n)
            ]
            # Optional control entity per device — switch.* or climate.* (aircon);
            # "" = forecast-only, no actuation.
            self._sensor_data[CONF_DEFERRABLE_LOAD_SWITCHES] = [
                str(user_input.get(f"switch_{i}", "") or "") for i in range(n)
            ]
            # Every remaining per-device list is written at its default so all of them
            # stay index-aligned with CONF_DEFERRABLE_LOAD_SENSORS — downstream code
            # zips these together positionally.
            for key, blank in (
                (CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE, ""),
                (CONF_DEFERRABLE_LOAD_SOC_SENSORS, ""),
                (CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT, 100.0),
                (CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH, 0.0),
                (CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD, ""),
                (CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE, False),
                (CONF_DEFERRABLE_LOAD_SETPOINT, ""),
                (CONF_DEFERRABLE_LOAD_SETPOINT_UNIT, ""),
                (CONF_DEFERRABLE_LOAD_PHASES, 0),
                (CONF_DEFERRABLE_LOAD_VOLTAGE, 0.0),
                (CONF_DEFERRABLE_LOAD_MIN_CURRENT, 0.0),
                (CONF_DEFERRABLE_LOAD_PLUG_SENSOR, ""),
            ):
                self._sensor_data[key] = [blank] * n
            return await self.async_step_current_plan()

        schema_dict = {}
        name_placeholders: dict[str, str] = {}
        for i, sensor_id in enumerate(selected):
            state = self.hass.states.get(sensor_id)
            name = state.attributes.get("friendly_name", sensor_id) if state else sensor_id
            # Substituted into each field's own label below (see strings.json's
            # "{device_N_name} — ..." labels) so the form shows the actual device name
            # instead of "Device N".
            name_placeholders[f"device_{i}_name"] = name
            schema_dict[vol.Optional(f"max_kw_{i}", default=3.5)] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=100.0, step=0.1,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[vol.Optional(f"switch_{i}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "climate"])
            )

        return self.async_show_form(
            step_id="device_power",
            data_schema=vol.Schema(schema_dict),
            description_placeholders=name_placeholders,
        )

    async def async_step_current_plan(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Choose current plan, then register with GridLens API.

        The plan list was prefetched by `_load_coverage` on the very first submit, so
        this step does no plan I/O and its dropdown is never empty.
        """
        errors: dict[str, str] = {}
        session = async_get_clientsession(self.hass)

        # VPP bolt-on programs are independent of plan choice and this endpoint is
        # public — a fetch failure shouldn't block plan setup, so it just leaves the
        # dropdown at "None / not enrolled" rather than setting `errors`.
        if not self._api_vpp_programs:
            try:
                async with session.get(
                    f"{self._api_url}/vpp-programs/list",
                    params={"state": self._state},
                    headers={"User-Agent": _USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        self._api_vpp_programs = await resp.json()
            except Exception as exc:
                _LOGGER.warning("Could not fetch VPP program list: %s", exc)

        if user_input is not None:
            plan_id = user_input[CONF_CURRENT_PLAN]
            has_demand_tariff = user_input.get(CONF_HAS_DEMAND_TARIFF, False)
            vpp_program = user_input.get(CONF_VPP_PROGRAM) or None
            try:
                ha_uuid = str(uuid.UUID(await instance_id.async_get(self.hass)))
                self._ha_uuid = ha_uuid
                async with session.post(
                    f"{self._api_url}/register",
                    json={
                        "email": self._email,
                        "ha_installation_id": ha_uuid,
                        "current_plan": plan_id,
                        "state": self._state,
                    },
                    headers={"User-Agent": _USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._api_key = data["api_key"]
                        self._sensor_data.update({
                            CONF_CURRENT_PLAN: plan_id,
                            CONF_HAS_DEMAND_TARIFF: has_demand_tariff,
                            CONF_VPP_PROGRAM: vpp_program,
                            CONF_GRIDLENS_EMAIL: self._email,
                            CONF_GRIDLENS_API_URL: self._api_url,
                            CONF_GRIDLENS_API_KEY: self._api_key,
                        })
                        return await self.async_step_finalize()
                    elif resp.status == 409:
                        # Already registered — almost always a reinstall. The API stores
                        # only a hash and cannot hand the key back, but we mirrored it
                        # locally, and a 409 implies .storage survived (the installation
                        # UUID lives there), so the mirror is still present. Recover
                        # silently rather than dead-ending the user on manual_key.
                        self._sensor_data[CONF_CURRENT_PLAN] = plan_id
                        self._sensor_data[CONF_HAS_DEMAND_TARIFF] = has_demand_tariff
                        self._sensor_data[CONF_VPP_PROGRAM] = vpp_program
                        recovered = await self._async_recover_api_key(ha_uuid)
                        if recovered:
                            self._api_key = recovered
                            self._sensor_data.update({
                                CONF_GRIDLENS_EMAIL: self._email,
                                CONF_GRIDLENS_API_URL: self._api_url,
                                CONF_GRIDLENS_API_KEY: recovered,
                            })
                            return await self.async_step_finalize()
                        return await self.async_step_manual_key()
                    else:
                        errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"

        plan_options = [
            {"value": p["id"], "label": f"{p['retailer']} — {p['name']}"}
            for p in self._api_plans
        ]
        vpp_options = [{"value": "", "label": "None / not enrolled"}] + [
            {"value": p["id"], "label": f"{p['retailer']} — {p['name']}"}
            for p in self._api_vpp_programs
        ]

        return self.async_show_form(
            step_id="current_plan",
            data_schema=vol.Schema({
                vol.Required(CONF_CURRENT_PLAN): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=plan_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_HAS_DEMAND_TARIFF, default=False): selector.BooleanSelector(),
                vol.Optional(CONF_VPP_PROGRAM, default=""): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=vpp_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
            errors=errors,
        )

    async def async_step_finalize(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Create the config entry, and leave the upgrade pitch as a notification.

        Setup used to end on a blocking `async_external_step` to gridlens.au/subscribe.
        A paywall redirect in the middle of a wizard is friction at the worst possible
        moment — the user is trying to finish installing, not shop — and it silently
        did nothing at all on installs with no external/internal URL configured. The
        same pitch now waits in the notification drawer.
        """
        # Mirror the credentials so a future reinstall can recover without the user
        # having to have kept a copy of a key that is only ever displayed once.
        await async_save_credentials(
            self.hass,
            ha_uuid=self._ha_uuid,
            api_key=self._api_key,
            email=self._email,
            api_url=self._api_url,
        )

        # Only pitch the upgrade to accounts that would actually benefit. A fresh
        # /register always creates a free key, so the pitch is right there; a key
        # recovered on reinstall is frequently already paid, and telling a subscriber
        # their "free account is locked to that one plan" is both wrong and insulting.
        if self._api_tier and self._api_tier != "free":
            return self.async_create_entry(
                title=f"Grid Lens - {self._state}",
                data=self._sensor_data,
            )

        persistent_notification.async_create(
            self.hass,
            "Grid Lens is set up and comparing your current plan against itself, so "
            "you can see exactly where your bill goes.\n\n"
            "Your free account is locked to that one plan. "
            "[Upgrade](https://gridlens.au/pricing) to score every plan available on "
            "your network against your real metered usage and find out whether you're "
            "overpaying.\n\n"
            "Already subscribed? Paste your key under **Configure → API key & "
            "connection** on the Grid Lens integration.",
            title="Grid Lens: compare every plan",
            notification_id=f"{DOMAIN}_upgrade",
        )
        return self.async_create_entry(
            title=f"Grid Lens - {self._state}",
            data=self._sensor_data,
        )

    async def _async_recover_api_key(self, ha_uuid: str) -> str | None:
        """Return a locally-mirrored API key for this installation, if it still works.

        Returns None on anything unexpected so the caller falls back to asking the user;
        a stale key must never be written into a new entry, because the resulting install
        looks configured and then 401s on every refresh.
        """
        data = await async_load_credentials(self.hass, ha_uuid)
        if not data:
            return None
        api_key = data.get("api_key")
        if not api_key:
            return None
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{self._api_url}/plans/meta",
                params={"state": self._state},
                headers={"X-API-Key": api_key, "User-Agent": _USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    # Remember the tier: a recovered key is often a paid one, and
                    # finalize must not greet a subscriber with a free-tier upsell.
                    try:
                        self._api_tier = (await resp.json()).get("tier")
                    except Exception:
                        self._api_tier = None
                    _LOGGER.info(
                        "Recovered the existing Grid Lens API key from local storage "
                        "after a reinstall; no re-entry needed (tier=%s)",
                        self._api_tier or "unknown",
                    )
                    return api_key
                _LOGGER.debug(
                    "Mirrored API key did not validate (HTTP %s); asking the user",
                    resp.status,
                )
        except Exception:
            _LOGGER.debug("Could not validate the mirrored API key", exc_info=True)
        return None

    async def async_step_manual_key(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reached when /register 409s: this HA installation already has a key.

        Almost always a reinstall (the integration was removed and re-added), not a
        fraud attempt. The API stores only a hash of the key, so it cannot hand the
        existing one back — the user has to supply the copy they saved.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_GRIDLENS_API_KEY]
            try:
                session = async_get_clientsession(self.hass)
                async with session.get(
                    f"{self._api_url}/plans/meta",
                    params={"state": self._state},
                    headers={"X-API-Key": api_key, "User-Agent": _USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        self._sensor_data.update({
                            CONF_GRIDLENS_EMAIL: self._email,
                            CONF_GRIDLENS_API_URL: self._api_url,
                            CONF_GRIDLENS_API_KEY: api_key,
                        })
                        # Mirror it, so this is the last time it has to be typed in.
                        await async_save_credentials(
                            self.hass,
                            ha_uuid=self._ha_uuid,
                            api_key=api_key,
                            email=self._email,
                            api_url=self._api_url,
                        )
                        return self.async_create_entry(
                            title=f"Grid Lens - {self._state}",
                            data=self._sensor_data,
                        )
                    else:
                        errors[CONF_GRIDLENS_API_KEY] = "invalid_api_key"
            except Exception:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="manual_key",
            data_schema=vol.Schema({
                vol.Required(CONF_GRIDLENS_API_KEY): cv.string,
            }),
            errors=errors,
            description_placeholders={"email": self._email},
        )



def _battery_schema(defaults: dict, basic: bool = False) -> vol.Schema:
    """Battery form. `basic=True` (the setup flow) drops round-trip efficiency and the
    min/max SOC guardrails — three numbers whose defaults suit almost every install,
    and which mean nothing to someone who has just plugged in a battery. The options
    flow passes `basic=False` so all of them remain editable after setup."""

    def opt(key, default):
        v = defaults.get(key, default)
        return vol.Optional(key, default=v)

    def entity_sel():
        return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))

    schema_dict = {
        vol.Required(CONF_HAS_BATTERY, default=defaults.get(CONF_HAS_BATTERY, False)): selector.BooleanSelector(),
        opt(CONF_BATTERY_CAPACITY, 13.5): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1.0, max=1000.0, step=0.1, unit_of_measurement="kWh", mode=selector.NumberSelectorMode.BOX)
        ),
        opt(CONF_BATTERY_MAX_CHARGE_RATE, 5.0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.1, max=100.0, step=0.1, unit_of_measurement="kW", mode=selector.NumberSelectorMode.BOX)
        ),
        opt(CONF_BATTERY_MAX_DISCHARGE_RATE, 5.0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.1, max=100.0, step=0.1, unit_of_measurement="kW", mode=selector.NumberSelectorMode.BOX)
        ),
    }
    if not basic:
        schema_dict[opt(CONF_BATTERY_EFFICIENCY, 95.0)] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=50.0, max=100.0, step=1.0, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX)
        )
        schema_dict[opt(CONF_BATTERY_MIN_SOC, 10.0)] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.0, max=100.0, step=1.0, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX)
        )
        schema_dict[opt(CONF_BATTERY_MAX_SOC, 90.0)] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.0, max=100.0, step=1.0, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX)
        )
    if defaults.get(CONF_BATTERY_SOC_SENSOR):
        schema_dict[vol.Optional(CONF_BATTERY_SOC_SENSOR, default=defaults[CONF_BATTERY_SOC_SENSOR])] = entity_sel()
    else:
        schema_dict[vol.Optional(CONF_BATTERY_SOC_SENSOR)] = entity_sel()

    if defaults.get(CONF_BATTERY_CHARGE_POWER_SENSOR):
        schema_dict[vol.Optional(CONF_BATTERY_CHARGE_POWER_SENSOR, default=defaults[CONF_BATTERY_CHARGE_POWER_SENSOR])] = entity_sel()
    else:
        schema_dict[vol.Optional(CONF_BATTERY_CHARGE_POWER_SENSOR)] = entity_sel()

    if defaults.get(CONF_BATTERY_DISCHARGE_POWER_SENSOR):
        schema_dict[vol.Optional(CONF_BATTERY_DISCHARGE_POWER_SENSOR, default=defaults[CONF_BATTERY_DISCHARGE_POWER_SENSOR])] = entity_sel()
    else:
        schema_dict[vol.Optional(CONF_BATTERY_DISCHARGE_POWER_SENSOR)] = entity_sel()

    return vol.Schema(schema_dict)


def _inverter_schema(defaults: dict, detected: tuple[str, str] | None) -> vol.Schema:
    options = [
        {"value": f"{brand}:{transport}", "label": label}
        for brand, transports in INVERTER_BRANDS.items()
        for transport, label in transports.items()
    ]
    # A previously saved selection (options flow reconfigure) wins over a fresh guess.
    current = None
    if defaults.get(CONF_INVERTER_BRAND) and defaults.get(CONF_INVERTER_TRANSPORT):
        current = f"{defaults[CONF_INVERTER_BRAND]}:{defaults[CONF_INVERTER_TRANSPORT]}"
    elif detected:
        current = f"{detected[0]}:{detected[1]}"

    key = vol.Required(_FIELD_INVERTER_SELECT, default=current) if current else vol.Required(_FIELD_INVERTER_SELECT)
    return vol.Schema({
        key: selector.SelectSelector(
            selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
        ),
    })


class GridLensOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._sensor_data: dict = {}
        self._discovered: dict = {}
        self._device_options: list = []
        # Seeded from the entry, not left False: async_step_controlled_load overwrites
        # these on the full-reconfigure path, but jumping straight to the loads wizard
        # from the menu never runs that step — and False there would silently hide every
        # Controlled Load question from a household that has one.
        self._has_cl1: bool = bool(config_entry.data.get(CONF_HAS_CONTROLLED_LOAD_1, False))
        self._has_cl2: bool = bool(config_entry.data.get(CONF_HAS_CONTROLLED_LOAD_2, False))
        # Deferrable-load wizard state — see the "deferrable loads" section below.
        # `None` (not `[]`) means "not read from the entry yet"; an install with no
        # loads configured is a legitimate empty list.
        self._loads: list | None = None
        self._editing: int | None = None
        self._load_steps: list[str] = []
        # True when the wizard was entered straight from the menu, so "save and
        # continue" saves and exits instead of walking on to the plan/API steps.
        self._loads_only: bool = False

    async def async_step_init(self, user_input=None):
        """Entry point for Configure — a menu so a quick task (pasting a new API
        key after subscribing/re-subscribing) doesn't require walking the entire
        reconfigure wizard from the top just to reach the field at the end."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["deferrable_loads", "api_key", "full_reconfigure"],
        )

    async def async_step_deferrable_loads(self, user_input=None):
        """Jump straight to the loads wizard. Editing one appliance shouldn't mean
        walking back through energy sensors, battery specs and the plan picker."""
        self._loads_only = True
        return await self.async_step_loads()

    async def async_step_full_reconfigure(self, user_input=None):
        """The pre-existing full wizard, now reached via the menu above instead
        of unconditionally."""
        return await self.async_step_controlled_load()

    async def async_step_controlled_load(self, user_input=None):
        """Ask whether the household's meter has Controlled Load 1/2 switched on.

        Network/meter fact set by the DNSP, not the retail plan — same
        self-declare pattern as CONF_HAS_DEMAND_TARIFF. Answered here (before
        devices) so the per-load wizard below can offer the
        wired_to_controlled_load dropdown only for registers the household
        actually has. Stored on self rather than self._sensor_data because
        async_step_sensors below replaces self._sensor_data wholesale.
        """
        entry_data = self._config_entry.data

        if user_input is not None:
            self._has_cl1 = user_input.get(CONF_HAS_CONTROLLED_LOAD_1, False)
            self._has_cl2 = user_input.get(CONF_HAS_CONTROLLED_LOAD_2, False)
            return await self.async_step_sensors()

        return self.async_show_form(
            step_id="controlled_load",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_HAS_CONTROLLED_LOAD_1,
                    default=entry_data.get(CONF_HAS_CONTROLLED_LOAD_1, False),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_HAS_CONTROLLED_LOAD_2,
                    default=entry_data.get(CONF_HAS_CONTROLLED_LOAD_2, False),
                ): selector.BooleanSelector(),
            }),
        )

    async def async_step_sensors(self, user_input=None):
        errors = {}
        entry_data = self._config_entry.data

        if not self._discovered:
            self._discovered = await _discover_energy_sensors(self.hass)
            # Merge: entry data takes precedence over fresh discovery (user may have overridden)
            merged = {**self._discovered}
            for key in _ENERGY_SCHEMA_KEYS:
                # Membership, not truthiness: a stored None means the user deliberately
                # cleared that sensor. Testing `entry_data.get(key)` would treat cleared
                # the same as never-set and let Energy-dashboard discovery put it back.
                if key in entry_data:
                    merged[key] = entry_data[key]
            # Grid power is the exception to the rule above, and deliberately so. The
            # "stored None means deliberately cleared" reading was only ever safe for the
            # fields the Energy dashboard can re-supply; for this one, None is far more
            # likely to be damage (the absent-means-cleared path below, before it was
            # fixed) than intent — it cannot be auto-filled, so nothing has ever put a
            # value here except the user. Offer the card's answer back as a SUGGESTION
            # when the slot is empty; it still submits like any other field, and a user
            # who really wants it empty can clear it on a form that is now showing them
            # what they are clearing.
            if not merged.get(CONF_GRID_POWER_SENSOR):
                grid_power = await _discover_grid_power_sensor(self.hass)
                if grid_power:
                    merged[CONF_GRID_POWER_SENSOR] = grid_power
                    _LOGGER.info(
                        "Reconfigure: no grid power sensor stored — suggesting %s, "
                        "taken from your Power Flow card's grid_power_entity.", grid_power,
                    )
            self._discovered = merged

        if user_input is not None:
            errors = _validate_energy_sensors(self.hass, user_input)

            if not errors:
                self._sensor_data = {**entry_data, **user_input}
                # Every key in _energy_schema is user-editable, and a cleared
                # EntitySelector is absent from user_input rather than None. Spreading
                # entry_data first would inherit the old value for exactly those keys,
                # so re-assert each one explicitly: absent == cleared.
                #
                # ...but ONLY when "absent" actually carries the user's intent. An
                # EntitySelector seeded (via suggested_value) with an entity id that no
                # longer resolves renders EMPTY — the picker has nothing to show — and an
                # untouched empty picker submits absent, indistinguishable from a
                # deliberate clear. Honouring that turns a *transient* condition (the
                # inverter/integration hadn't finished loading when the wizard was opened,
                # an entity got renamed) into a PERMANENT deletion of a setting the user
                # never touched, on a step they only walked through to reach something
                # else. That is how this install lost `grid_power_sensor` — silently
                # disabling Greedy Consumption's export-surplus condition, with the load
                # sitting off through hours of free export (see GRIDLENS_CHECKLIST.md,
                # 2026-08-28).
                #
                # So: absent + we seeded a value the picker could render == cleared, honour
                # it. Absent + we seeded a value that doesn't currently resolve == the form
                # could not have shown it, so there was nothing for the user to clear; keep
                # what is stored. A user who genuinely wants it gone can clear a field that
                # is actually rendering.
                for key in _ENERGY_SCHEMA_KEYS:
                    if key == CONF_ENERGY_SENSOR:
                        continue  # required field — never clearable
                    submitted = user_input.get(key)
                    if submitted:
                        self._sensor_data[key] = submitted
                        continue
                    seeded = self._discovered.get(key)
                    if seeded and self.hass.states.get(seeded) is None:
                        _LOGGER.warning(
                            "Reconfigure: keeping %s = %s. It came back empty, but that "
                            "entity does not currently exist, so the picker could not have "
                            "shown it — treating this as 'not answered', not 'cleared'. "
                            "Clear the field while it is showing a value to remove it.",
                            key, seeded,
                        )
                        self._sensor_data[key] = seeded
                    else:
                        self._sensor_data[key] = None
                # entry_data is spread first above, so without this the freshly
                # answered controlled_load step would be silently clobbered back
                # to its old saved value.
                self._sensor_data[CONF_HAS_CONTROLLED_LOAD_1] = self._has_cl1
                self._sensor_data[CONF_HAS_CONTROLLED_LOAD_2] = self._has_cl2
                return await self.async_step_battery()

        discovered_count = sum(1 for k in (CONF_ENERGY_SENSOR, CONF_SOLAR_SENSOR, CONF_GRID_EXPORT_SENSOR) if self._discovered.get(k))
        description_placeholders = {
            "discovered": f"✓ {discovered_count} sensor(s) detected from Energy dashboard — pre-filled below." if discovered_count else "No Energy dashboard config found — select sensors manually.",
        }

        return self.async_show_form(
            step_id="sensors",
            data_schema=_energy_schema(self._discovered),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_battery(self, user_input=None):
        errors = {}
        entry_data = self._config_entry.data

        if user_input is not None:
            has_battery = user_input.get(CONF_HAS_BATTERY, False)
            if has_battery:
                capacity = user_input.get(CONF_BATTERY_CAPACITY, 0)
                if not capacity or capacity <= 0:
                    errors[CONF_BATTERY_CAPACITY] = "invalid_capacity"

            if not errors:
                self._sensor_data = {**self._sensor_data, **user_input}
                if has_battery:
                    return await self.async_step_inverter()
                return await self.async_step_loads()

        return self.async_show_form(
            step_id="battery",
            data_schema=_battery_schema(entry_data),
            errors=errors,
        )

    async def async_step_inverter(self, user_input=None):
        """Pick which inverter brand/transport ControlManager should dispatch to."""
        entry_data = self._config_entry.data

        if user_input is not None:
            brand, _, transport = user_input[_FIELD_INVERTER_SELECT].partition(":")
            self._sensor_data = {
                **self._sensor_data,
                CONF_INVERTER_BRAND: brand,
                CONF_INVERTER_TRANSPORT: transport,
            }
            return await self.async_step_loads()

        detected = detect_inverter_brand(self.hass)
        description_placeholders = {
            "detected": (
                f"✓ Auto-detected {INVERTER_BRANDS[detected[0]][detected[1]]} — pre-selected below."
                if detected
                else "Couldn't auto-detect your inverter — select your brand below."
            ),
        }
        return self.async_show_form(
            step_id="inverter",
            data_schema=_inverter_schema(entry_data, detected),
            description_placeholders=description_placeholders,
        )

    # ------------------------------------------------------------------ deferrable loads
    #
    # One wizard per load, asking only what that load's kind and control style actually
    # use. This replaces four steps that between them rendered every field for every
    # load on two enormous forms: `device_power` alone showed 14 fields per selected
    # device (translations/en.json carried 140 pre-baked labels, ten slots' worth, each
    # one prefixed with the device name because there was no other way to say which
    # device a field belonged to), and `declared_loads` / `estimated_loads` drew a fixed
    # 2 and 3 slots whether or not they were used.
    #
    # A forecast-only appliance now answers three questions. A modulating EV charger
    # still answers all of them, but across short titled steps instead of buried in a
    # fifty-field screen next to the dishwasher's.
    #
    # State: `self._loads` is the working list (one dict per load, see
    # deferrable_loads.py), read once on entering the hub and written back to the
    # parallel arrays only on "save and continue". `self._editing` indexes the load
    # being edited; `self._load_steps` is the queue of sub-steps that load's answers
    # earned, walked by `_next_load_step`.

    async def async_step_loads(self, user_input=None):
        """Hub: list the configured deferrable loads, pick one to edit, or add one."""
        if self._loads is None:
            self._loads = read_loads(self._config_entry.data)
        if not self._device_options:
            self._device_options = await _discover_dashboard_devices(self.hass)

        if user_input is not None:
            action = str(user_input.get("action") or "done")
            if action == "add":
                return await self.async_step_load_kind()
            if action == "load_power":
                return await self.async_step_load_power()
            if action.startswith("edit:"):
                self._editing = int(action.split(":", 1)[1])
                return await self._open_load_detail()
            # "done" — drop any load abandoned without an identity (added, then the
            # wizard was walked past without naming it) and flatten back to the
            # parallel arrays every consumer reads.
            keep = [
                load for load in self._loads
                if (load.get("sensor") if load.get("kind") == MONITORED else load.get("name"))
            ]
            self._sensor_data.update(write_loads(keep))
            if self._loads_only:
                # Merge over the entry's own data — _sensor_data holds only what this
                # wizard touched, and replacing data with it alone would wipe the rest.
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data={**self._config_entry.data, **self._sensor_data},
                )
                return self.async_create_entry(title="", data={})
            return await self.async_step_current_plan()

        options = [
            {"value": f"edit:{i}", "label": self._load_label(load)}
            for i, load in enumerate(self._loads)
        ]
        options.append({"value": "add", "label": "➕ Add a deferrable load"})
        if any(load.get("kind") == ESTIMATED for load in self._loads):
            options.append(
                {"value": "load_power", "label": "⚙ House load sensor (used to estimate loads)"}
            )
        options.append({"value": "done", "label": "✓ Save and continue"})

        summary = (
            "\n".join(f"• {self._load_label(load)}" for load in self._loads)
            if self._loads
            else "No deferrable loads configured yet."
        )
        return self.async_show_form(
            step_id="loads",
            data_schema=vol.Schema({
                vol.Optional("action", default="done"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
            description_placeholders={"summary": summary},
        )

    def _load_display_name(self, load) -> str:
        """Human name for a load — the Energy Dashboard's label for a monitored sensor
        (that rename lives on the dashboard, not the entity), else the entity's own
        friendly name, else what the user typed."""
        if load.get("kind") != MONITORED:
            return str(load.get("name") or "Unnamed load")
        sensor = load.get("sensor", "")
        for opt in self._device_options:
            if opt.get("value") == sensor:
                return str(opt.get("label", sensor)).split(" (")[0]
        state = self.hass.states.get(sensor)
        if state:
            return state.attributes.get("friendly_name", sensor)
        return sensor

    def _load_label(self, load) -> str:
        """One line per load for the hub list: name, then how it is wired, so the list
        answers "which one do I need to fix" without opening each in turn."""
        name = self._load_display_name(load)
        kind = load.get("kind")
        if kind == MONITORED:
            style = {
                CONTROL_MODULATING: "modulating",
                CONTROL_ONOFF: "on/off",
            }.get(control_style(load), "forecast only")
            bits = [f"metered, {style}", f"{load.get('max_kw', 3.5):g} kW"]
        elif kind == DECLARED:
            bits = [
                "declared",
                f"{load.get('daily_kwh', 0.0):g} kWh/day",
                f"{load.get('max_kw', 3.5):g} kW",
            ]
        else:
            bits = ["estimated", f"~{load.get('est_kw', 1.0):g} kW"]
        if load.get("controlled_load"):
            bits.append(load["controlled_load"].replace("_", " ").title())
        return f"{name} — {', '.join(bits)}"

    async def async_step_load_kind(self, user_input=None):
        """Which kind of load is being added. The distinction that decides every
        following question is what Home Assistant can already see and do: an energy
        sensor, a controllable entity, or neither."""
        menu_options = []
        if self._device_options:
            menu_options.append("load_add_monitored")
        menu_options += ["load_add_estimated", "load_add_declared"]
        return self.async_show_menu(step_id="load_kind", menu_options=menu_options)

    async def async_step_load_add_monitored(self, user_input=None):
        """Pick the Energy Dashboard device this load is measured by."""
        taken = {
            load.get("sensor") for load in self._loads if load.get("kind") == MONITORED
        }
        available = [opt for opt in self._device_options if opt["value"] not in taken]
        if not available:
            # Every discovered device is already configured — nothing to add, so say so
            # rather than showing an empty picker.
            return await self.async_step_loads()

        if user_input is not None:
            self._loads.append(new_load(MONITORED, sensor=user_input["sensor"]))
            self._editing = len(self._loads) - 1
            return await self._open_load_detail()

        return self.async_show_form(
            step_id="load_add_monitored",
            data_schema=vol.Schema({
                vol.Required("sensor"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=available,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_load_add_declared(self, user_input=None):
        self._loads.append(new_load(DECLARED))
        self._editing = len(self._loads) - 1
        return await self._open_load_detail()

    async def async_step_load_add_estimated(self, user_input=None):
        self._loads.append(new_load(ESTIMATED))
        self._editing = len(self._loads) - 1
        return await self._open_load_detail()

    def _cl_register_options(self) -> list[dict]:
        """Controlled Load registers the household declared in async_step_controlled_load.
        Empty when they declared none, which is what suppresses every CL question."""
        options = []
        if self._has_cl1:
            options.append({"value": "controlled_load_1", "label": "Controlled Load 1"})
        if self._has_cl2:
            options.append({"value": "controlled_load_2", "label": "Controlled Load 2"})
        return options

    async def _next_load_step(self):
        """Run the next sub-step this load's answers earned, or return to the hub."""
        if self._load_steps:
            step = self._load_steps.pop(0)
            return await getattr(self, f"async_step_load_{step}")()
        return await self.async_step_loads()

    async def _open_load_detail(self):
        """Show the detail form for whichever kind the load being edited is."""
        kind = self._loads[self._editing]["kind"]
        return await getattr(self, f"async_step_load_detail_{kind}")()

    # Three step ids over one implementation. The fields differ per kind, so the titles
    # and help text must too — a shared "load_detail" step would have shown a declared
    # load's form under a monitored load's heading, which is the near-duplicate-step
    # problem that made Declared vs Estimated Loads confusable in the first place.
    async def async_step_load_detail_monitored(self, user_input=None):
        return await self._async_load_detail("load_detail_monitored", user_input)

    async def async_step_load_detail_declared(self, user_input=None):
        return await self._async_load_detail("load_detail_declared", user_input)

    async def async_step_load_detail_estimated(self, user_input=None):
        return await self._async_load_detail("load_detail_estimated", user_input)

    async def _async_load_detail(self, step_id, user_input=None):
        """The one form every load gets. Its fields depend on the load's kind, and its
        answers decide which follow-up steps run."""
        load = self._loads[self._editing]
        kind = load["kind"]
        cl_options = self._cl_register_options()
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("remove"):
                del self._loads[self._editing]
                self._editing = None
                return await self.async_step_loads()

            if kind == MONITORED:
                load["max_kw"] = float(user_input.get("max_kw", 3.5))
                style = str(user_input.get("control_style", CONTROL_NONE))
                apply_control_style(load, style)
                self._load_steps = []
                if style != CONTROL_NONE:
                    self._load_steps.append("control")
                if style == CONTROL_MODULATING:
                    self._load_steps.append("modulating")
                if user_input.get("has_soc"):
                    self._load_steps.append("soc")
                else:
                    clear_soc(load)
            elif kind == DECLARED:
                name = str(user_input.get("name", "") or "").strip()
                if not name:
                    errors["name"] = "load_name_required"
                elif self._name_taken(name):
                    errors["name"] = "load_duplicate_name"
                spec = str(user_input.get("hours", "all") or "all").strip()
                if not errors:
                    try:
                        parse_hours_spec(spec)
                    except ValueError:
                        errors["hours"] = "invalid_hours"
                if not errors:
                    load["name"] = name
                    load["daily_kwh"] = float(user_input.get("daily_kwh", 0.0) or 0.0)
                    load["max_kw"] = float(user_input.get("max_kw", 3.5))
                    load["hours"] = spec
                    self._load_steps = []
            else:  # ESTIMATED
                name = str(user_input.get("name", "") or "").strip()
                control = str(user_input.get("control", "") or "")
                if not name:
                    errors["name"] = "load_name_required"
                elif self._name_taken(name):
                    errors["name"] = "load_duplicate_name"
                # A slot with a name but no control entity is silently inert —
                # `_ensure_load_estimators` skips it, and nothing tells the user. The
                # old fixed-slot form let this through and it cost a real
                # misconfiguration (Daikin AC, 2026-08-06, control/kw/auto all set with
                # a blank name). Here both are required, so it cannot be saved half-done.
                if not control:
                    errors["control"] = "load_control_required"
                if not errors:
                    load["name"] = name
                    load["control"] = control
                    load["est_kw"] = float(user_input.get("est_kw", 1.0) or 1.0)
                    load["auto"] = bool(user_input.get("auto", False))
                    self._load_steps = []

            if not errors:
                if cl_options and kind in (MONITORED, DECLARED):
                    if user_input.get("on_controlled_load"):
                        self._load_steps.append("cl")
                    else:
                        clear_controlled_load(load)
                return await self._next_load_step()

        # ---------------------------------------------------------------- render
        schema: dict = {}
        if kind == MONITORED:
            schema[vol.Optional("max_kw", default=load.get("max_kw", 3.5))] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1, max=100.0, step=0.1,
                        unit_of_measurement="kW",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            )
            schema[vol.Optional("control_style", default=control_style(load))] = (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": CONTROL_NONE,
                             "label": "Forecast only — Grid Lens never switches it"},
                            {"value": CONTROL_ONOFF,
                             "label": "On/off — a switch or climate entity"},
                            {"value": CONTROL_MODULATING,
                             "label": "Modulating — a charger with an adjustable current/power setpoint"},
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            )
            schema[vol.Optional("has_soc", default=bool(load.get("soc_sensor")))] = (
                selector.BooleanSelector()
            )
        elif kind == DECLARED:
            schema[vol.Required("name", default=load.get("name", ""))] = selector.TextSelector()
            schema[vol.Optional("daily_kwh", default=load.get("daily_kwh", 0.0))] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=100.0, step=0.1,
                        unit_of_measurement="kWh/day",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            )
            schema[vol.Optional("max_kw", default=load.get("max_kw", 3.5))] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1, max=100.0, step=0.1,
                        unit_of_measurement="kW",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            )
            schema[vol.Optional("hours", default=load.get("hours", "all"))] = (
                selector.TextSelector()
            )
        else:  # ESTIMATED
            schema[vol.Required("name", default=load.get("name", ""))] = selector.TextSelector()
            prev_control = load.get("control", "")
            control_key = (
                vol.Required("control", default=prev_control)
                if prev_control else vol.Required("control")
            )
            schema[control_key] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "climate"])
            )
            schema[vol.Optional("est_kw", default=load.get("est_kw", 1.0))] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.05, max=20.0, step=0.05,
                        unit_of_measurement="kW",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            )
            schema[vol.Optional("auto", default=bool(load.get("auto", False)))] = (
                selector.BooleanSelector()
            )

        if cl_options and kind in (MONITORED, DECLARED):
            schema[vol.Optional(
                "on_controlled_load", default=bool(load.get("controlled_load")),
            )] = selector.BooleanSelector()
        schema[vol.Optional("remove", default=False)] = selector.BooleanSelector()

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"name": self._load_display_name(load)},
        )

    def _name_taken(self, name: str) -> bool:
        """Declared and estimated loads share one namespace — a duplicate name silently
        merges two loads downstream, so reject it at the point of entry rather than
        letting the old cross-step check catch it several screens later."""
        lowered = name.strip().lower()
        return any(
            i != self._editing
            and load.get("kind") in (DECLARED, ESTIMATED)
            and str(load.get("name", "")).strip().lower() == lowered
            for i, load in enumerate(self._loads)
        )

    async def async_step_load_control(self, user_input=None):
        """Which entity Grid Lens turns on and off for this load."""
        load = self._loads[self._editing]

        if user_input is not None:
            load["switch"] = str(user_input.get("switch", "") or "")
            load["climate_on_mode"] = str(user_input.get("climate_on_mode", "") or "")
            return await self._next_load_step()

        prev = load.get("switch", "")
        switch_key = (
            vol.Required("switch", default=prev) if prev else vol.Required("switch")
        )
        return self.async_show_form(
            step_id="load_control",
            data_schema=vol.Schema({
                switch_key: selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["switch", "climate"])
                ),
                # Only consulted for a climate.* entity that doesn't support
                # climate.turn_on/turn_off (most do) — which hvac_mode means "on".
                vol.Optional(
                    "climate_on_mode", default=load.get("climate_on_mode", ""),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "", "label": "Auto (turn_on / restore last mode)"},
                            {"value": "cool", "label": "Cool"},
                            {"value": "heat", "label": "Heat"},
                            {"value": "heat_cool", "label": "Heat/Cool (auto)"},
                            {"value": "dry", "label": "Dry"},
                            {"value": "fan_only", "label": "Fan only"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
            description_placeholders={"name": self._load_display_name(load)},
        )

    async def async_step_load_modulating(self, user_input=None):
        """Setpoint wiring for a modulating ("type 2") load — an EV charger whose
        current or power can be ramped rather than only switched."""
        load = self._loads[self._editing]

        if user_input is not None:
            load["setpoint"] = str(user_input.get("setpoint", "") or "")
            load["setpoint_unit"] = str(user_input.get("setpoint_unit", "") or "")
            load["phases"] = int(user_input.get("phases", 0) or 0)
            load["voltage"] = float(user_input.get("voltage", 0.0) or 0.0)
            load["min_current"] = float(user_input.get("min_current", 0.0) or 0.0)
            load["plug_sensor"] = str(user_input.get("plug_sensor", "") or "")
            return await self._next_load_step()

        prev_setpoint = load.get("setpoint", "")
        setpoint_key = (
            vol.Required("setpoint", default=prev_setpoint)
            if prev_setpoint else vol.Required("setpoint")
        )
        prev_plug = load.get("plug_sensor", "")
        plug_key = (
            vol.Optional("plug_sensor", default=prev_plug)
            if prev_plug else vol.Optional("plug_sensor")
        )
        return self.async_show_form(
            step_id="load_modulating",
            data_schema=vol.Schema({
                setpoint_key: selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                vol.Optional(
                    "setpoint_unit", default=load.get("setpoint_unit", ""),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "", "label": "Auto (detect from the entity)"},
                            {"value": "a", "label": "Amps (A)"},
                            {"value": "w", "label": "Watts (W)"},
                            {"value": "kw", "label": "Kilowatts (kW)"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                # Stored as int (see CONF_DEFERRABLE_LOAD_PHASES); SelectSelector option
                # values are strings, so re-stringify the saved default to match.
                vol.Optional(
                    "phases", default=str(load.get("phases", 0)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "0", "label": "Auto-detect"},
                            {"value": "1", "label": "Single phase"},
                            {"value": "2", "label": "Two phase"},
                            {"value": "3", "label": "Three phase"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    "voltage", default=load.get("voltage", 0.0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=500.0, step=1,
                        unit_of_measurement="V",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    "min_current", default=load.get("min_current", 0.0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=32.0, step=0.1,
                        unit_of_measurement="A",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                plug_key: selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["binary_sensor", "sensor"])
                ),
            }),
            description_placeholders={"name": self._load_display_name(load)},
        )

    async def async_step_load_soc(self, user_input=None):
        """State-of-charge tracking for a load with its own battery — lets the optimizer
        stop scheduling charge once it nears the ceiling (an EV held at 90%, say),
        freeing that energy for other loads or export."""
        load = self._loads[self._editing]

        if user_input is not None:
            load["soc_sensor"] = str(user_input.get("soc_sensor", "") or "")
            load["soc_max_percent"] = float(user_input.get("soc_max_percent", 100.0))
            load["soc_capacity_kwh"] = float(user_input.get("soc_capacity_kwh", 0.0))
            return await self._next_load_step()

        prev = load.get("soc_sensor", "")
        soc_key = (
            vol.Required("soc_sensor", default=prev) if prev else vol.Required("soc_sensor")
        )
        return self.async_show_form(
            step_id="load_soc",
            data_schema=vol.Schema({
                soc_key: selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "soc_max_percent", default=load.get("soc_max_percent", 100.0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1.0, max=100.0, step=1,
                        unit_of_measurement="%",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                # The LP only activates SOC tracking when a sensor AND a positive
                # capacity are both present (advisory/coordinator._deferrable_for_horizon).
                vol.Optional(
                    "soc_capacity_kwh", default=load.get("soc_capacity_kwh", 0.0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=500.0, step=0.1,
                        unit_of_measurement="kWh",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }),
            description_placeholders={"name": self._load_display_name(load)},
        )

    async def async_step_load_cl(self, user_input=None):
        """Which Controlled Load register this load is wired to.

        TODO: filter these by the device types the network actually confirms for that
        register (NetworkIR.controlled_load_eligible_devices in the API's
        plan_transform.py) — no live eligible-device lookup is wired into the flow yet.
        """
        load = self._loads[self._editing]
        cl_options = self._cl_register_options()

        if user_input is not None:
            load["controlled_load"] = str(user_input.get("controlled_load", "") or "")
            load["in_aggregate"] = bool(user_input.get("in_aggregate", False))
            return await self._next_load_step()

        return self.async_show_form(
            step_id="load_cl",
            data_schema=vol.Schema({
                vol.Optional(
                    "controlled_load",
                    default=load.get("controlled_load", "") or cl_options[0]["value"],
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=cl_options,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(
                    "in_aggregate", default=bool(load.get("in_aggregate", False)),
                ): selector.BooleanSelector(),
            }),
            description_placeholders={"name": self._load_display_name(load)},
        )

    async def async_step_load_power(self, user_input=None):
        """Whole-house load power sensor. Not per-load: LoadEstimator subtracts the
        other known draws from this to infer what an estimated load is using."""
        if user_input is not None:
            self._sensor_data[CONF_LOAD_POWER_SENSOR] = str(
                user_input.get("load_power_sensor", "") or ""
            )
            return await self.async_step_loads()

        existing = self._sensor_data.get(
            CONF_LOAD_POWER_SENSOR, self._config_entry.data.get(CONF_LOAD_POWER_SENSOR, "")
        )
        key = (
            vol.Optional("load_power_sensor", default=existing)
            if existing else vol.Optional("load_power_sensor")
        )
        return self.async_show_form(
            step_id="load_power",
            data_schema=vol.Schema({
                key: selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
            }),
        )

    async def async_step_current_plan(self, user_input=None):
        """Choose which plan the user is currently on."""
        entry_data = self._config_entry.data
        api_url = entry_data.get(CONF_GRIDLENS_API_URL, GRIDLENS_DEFAULT_API_URL)
        state = entry_data.get(CONF_STATE, "NSW")
        session = async_get_clientsession(self.hass)
        plan_options = []
        try:
            async with session.get(
                f"{api_url}/plans/list",
                params={"state": state, "network": entry_data.get(CONF_DISTRIBUTOR)},
                headers={"User-Agent": _USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as _r:
                if _r.status == 200:
                    plan_meta = await _r.json()
                    plan_options = [
                        {"value": p["id"], "label": f"{p['retailer']} - {p['name']}"}
                        for p in plan_meta
                    ]
        except Exception:
            pass

        # VPP bolt-on programs are independent of plan choice and this endpoint is
        # public — a fetch failure just leaves the dropdown at "None / not enrolled".
        vpp_options = [{"value": "", "label": "None / not enrolled"}]
        try:
            async with session.get(
                f"{api_url}/vpp-programs/list",
                params={"state": state},
                headers={"User-Agent": _USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as _r:
                if _r.status == 200:
                    vpp_meta = await _r.json()
                    vpp_options += [
                        {"value": p["id"], "label": f"{p['retailer']} — {p['name']}"}
                        for p in vpp_meta
                    ]
        except Exception:
            pass

        if user_input is not None:
            self._sensor_data = {**self._sensor_data, **user_input}
            self._sensor_data[CONF_VPP_PROGRAM] = user_input.get(CONF_VPP_PROGRAM) or None
            return await self.async_step_api_key()

        current = entry_data.get(CONF_CURRENT_PLAN)
        schema = vol.Schema({
            vol.Optional(CONF_CURRENT_PLAN, **({'default': current} if current else {})): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=plan_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_HAS_DEMAND_TARIFF,
                default=entry_data.get(CONF_HAS_DEMAND_TARIFF, False),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_VPP_PROGRAM,
                default=entry_data.get(CONF_VPP_PROGRAM) or "",
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=vpp_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        })

        return self.async_show_form(
            step_id="current_plan",
            data_schema=schema,
        )

    async def async_step_api_key(self, user_input=None):
        """Update the Grid Lens API key."""
        errors: dict[str, str] = {}
        entry_data = self._config_entry.data
        current_key = entry_data.get(CONF_GRIDLENS_API_KEY, "")
        api_url = entry_data.get(CONF_GRIDLENS_API_URL, GRIDLENS_DEFAULT_API_URL)

        if user_input is not None:
            new_key = user_input.get(CONF_GRIDLENS_API_KEY, "").strip()
            # The API URL lives here rather than on the setup flow's first screen —
            # it only matters when self-hosting, and this is the connection step.
            api_url = (user_input.get(CONF_GRIDLENS_API_URL) or api_url).rstrip("/")

            if new_key and new_key != current_key:
                try:
                    session = async_get_clientsession(self.hass)
                    async with session.get(
                        f"{api_url}/plans/meta",
                        params={"state": entry_data.get(CONF_STATE)},
                        headers={"X-API-Key": new_key, "User-Agent": _USER_AGENT},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            errors[CONF_GRIDLENS_API_KEY] = "invalid_api_key"
                except Exception:
                    errors["base"] = "cannot_connect"

            if not errors:
                # Base on the entry's own current data, not just self._sensor_data —
                # reached directly from the menu (the common case now), _sensor_data
                # is still empty (only the full wizard path below populates it), and
                # {**self._sensor_data, ...} alone would silently wipe every other
                # setting on the entry down to just the new key.
                new_data = {**self._config_entry.data, **self._sensor_data,
                            CONF_GRIDLENS_API_KEY: new_key or current_key,
                            CONF_GRIDLENS_API_URL: api_url}
                self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="api_key",
            data_schema=vol.Schema({
                vol.Optional(CONF_GRIDLENS_API_KEY, default=current_key): cv.string,
                vol.Optional(CONF_GRIDLENS_API_URL, default=api_url): cv.string,
            }),
            errors=errors,
        )
