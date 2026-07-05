"""Config flow for Grid Lens."""
from __future__ import annotations

import logging
import uuid
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import instance_id, selector
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    CONF_ENERGY_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_IMPORT_PRICE_SENSOR,
    CONF_EXPORT_PRICE_SENSOR,
    CONF_DISTRIBUTOR,
    CONF_HAS_DEMAND_TARIFF,
    CONF_STATE,
    CONF_POSTCODE,
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
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_HOURS,
    CONF_CURRENT_PLAN,
    parse_hours_spec,
    CONF_GRIDLENS_EMAIL,
    CONF_GRIDLENS_API_URL,
    CONF_GRIDLENS_API_KEY,
    GRIDLENS_DEFAULT_API_URL,
    STATES,
    DISTRIBUTORS,
)

_LOGGER = logging.getLogger(__name__)


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


def _energy_schema(defaults: dict) -> vol.Schema:
    """Build the energy sensors schema, pre-filling discovered values."""

    def entity_sel():
        return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))

    def opt(key):
        if defaults.get(key):
            return vol.Optional(key, default=defaults[key])
        return vol.Optional(key)

    def req(key):
        if defaults.get(key):
            return vol.Required(key, default=defaults[key])
        return vol.Required(key)

    return vol.Schema({
        req(CONF_ENERGY_SENSOR): entity_sel(),
        opt(CONF_SOLAR_SENSOR): entity_sel(),
        opt(CONF_GRID_EXPORT_SENSOR): entity_sel(),
        opt(CONF_IMPORT_PRICE_SENSOR): entity_sel(),
        opt(CONF_EXPORT_PRICE_SENSOR): entity_sel(),
    })


class GridLensConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Grid Lens."""

    VERSION = 1

    def __init__(self) -> None:
        self._state: str | None = None
        self._postcode: str | None = None
        self._distributor: str | None = None
        self._has_demand_tariff: bool = False
        self._discovered: dict = {}
        self._device_options: list = []
        self._sensor_data: dict = {}
        self._email: str = ""
        self._api_url: str = GRIDLENS_DEFAULT_API_URL
        self._api_plans: list[dict] = []
        self._api_key: str = ""
        self._ha_uuid: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return GridLensOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._state = user_input[CONF_STATE]
            self._postcode = user_input[CONF_POSTCODE]
            self._email = user_input[CONF_GRIDLENS_EMAIL]
            self._api_url = user_input.get(CONF_GRIDLENS_API_URL, GRIDLENS_DEFAULT_API_URL).rstrip("/")
            return await self.async_step_distributor()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_STATE): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=STATES, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(CONF_POSTCODE): cv.string,
                vol.Required(CONF_GRIDLENS_EMAIL): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
                ),
                vol.Optional(CONF_GRIDLENS_API_URL, default=GRIDLENS_DEFAULT_API_URL): cv.string,
            }),
        )

    async def async_step_distributor(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._distributor = user_input[CONF_DISTRIBUTOR]
            self._has_demand_tariff = user_input.get(CONF_HAS_DEMAND_TARIFF, False)
            return await self.async_step_sensors()

        distributors = DISTRIBUTORS.get(self._state, [])
        return self.async_show_form(
            step_id="distributor",
            data_schema=vol.Schema({
                vol.Required(CONF_DISTRIBUTOR): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=distributors, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional(CONF_HAS_DEMAND_TARIFF, default=False): selector.BooleanSelector(),
            }),
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        # Auto-discover on first visit
        if not self._discovered:
            self._discovered = await _discover_energy_sensors(self.hass)
            if self._discovered:
                _LOGGER.info("Auto-discovered energy sensors: %s", self._discovered)

        if user_input is not None:
            energy_sensor = user_input.get(CONF_ENERGY_SENSOR)
            if energy_sensor:
                state = self.hass.states.get(energy_sensor)
                if state:
                    unit = state.attributes.get("unit_of_measurement", "").lower()
                    state_class = state.attributes.get("state_class", "")
                    if unit in ("w", "kw", "mw"):
                        errors[CONF_ENERGY_SENSOR] = "wrong_unit_power"
                    elif unit not in ("kwh", "mwh"):
                        errors[CONF_ENERGY_SENSOR] = "wrong_unit"
                    elif state_class not in ("total", "total_increasing"):
                        errors[CONF_ENERGY_SENSOR] = "wrong_state_class"

            if not errors:
                data = {
                    CONF_STATE: self._state,
                    CONF_POSTCODE: self._postcode,
                    CONF_DISTRIBUTOR: self._distributor,
                    CONF_HAS_DEMAND_TARIFF: self._has_demand_tariff,
                    CONF_ENERGY_SENSOR: user_input.get(CONF_ENERGY_SENSOR),
                    CONF_SOLAR_SENSOR: user_input.get(CONF_SOLAR_SENSOR),
                    CONF_GRID_EXPORT_SENSOR: user_input.get(CONF_GRID_EXPORT_SENSOR),
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
        errors: dict[str, str] = {}

        if user_input is not None:
            has_battery = user_input.get(CONF_HAS_BATTERY, False)
            if has_battery:
                capacity = user_input.get(CONF_BATTERY_CAPACITY, 0)
                if not capacity or capacity <= 0:
                    errors[CONF_BATTERY_CAPACITY] = "invalid_capacity"

            if not errors:
                self._sensor_data = {**self._sensor_data, **user_input}
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="battery",
            data_schema=_battery_schema({}),
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
        """Capture max power draw and availability hours for each selected deferrable load."""
        selected = self._sensor_data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
        if not selected:
            return await self.async_step_current_plan()

        errors: dict[str, str] = {}
        if user_input is not None:
            hours_list = [
                str(user_input.get(f"hours_{i}", "all")).strip() or "all"
                for i in range(len(selected))
            ]
            try:
                for spec in hours_list:
                    parse_hours_spec(spec)
            except ValueError:
                errors["base"] = "invalid_hours"
            if not errors:
                max_kw_list = [float(user_input.get(f"max_kw_{i}", 3.5)) for i in range(len(selected))]
                self._sensor_data[CONF_DEFERRABLE_LOAD_MAX_KW] = max_kw_list
                self._sensor_data[CONF_DEFERRABLE_LOAD_HOURS] = hours_list
                return await self.async_step_current_plan()

        schema_dict = {}
        device_lines = []
        for i, sensor_id in enumerate(selected):
            state = self.hass.states.get(sensor_id)
            name = state.attributes.get("friendly_name", sensor_id) if state else sensor_id
            device_lines.append(f"{i + 1}. {name}")
            schema_dict[vol.Optional(f"max_kw_{i}", default=3.5)] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=100.0, step=0.1,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[vol.Optional(f"hours_{i}", default="all")] = selector.TextSelector()

        return self.async_show_form(
            step_id="device_power",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"devices": "\n".join(device_lines)},
            errors=errors,
        )

    async def async_step_current_plan(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Choose current plan, then register with GridLens API."""
        errors: dict[str, str] = {}

        if not self._api_plans:
            try:
                async with aiohttp.ClientSession() as session:
                    resp = await session.get(
                        f"{self._api_url}/plans/list",
                        params={"state": self._state, "network": self._distributor},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if resp.status == 200:
                        self._api_plans = await resp.json()
                    else:
                        errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"

        if user_input is not None and not errors:
            plan_id = user_input[CONF_CURRENT_PLAN]
            try:
                ha_uuid = str(uuid.UUID(await instance_id.async_get(self.hass)))
                self._ha_uuid = ha_uuid
                async with aiohttp.ClientSession() as session:
                    resp = await session.post(
                        f"{self._api_url}/register",
                        json={
                            "email": self._email,
                            "ha_installation_id": ha_uuid,
                            "current_plan": plan_id,
                            "state": self._state,
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if resp.status == 200:
                        data = await resp.json()
                        self._api_key = data["api_key"]
                        self._sensor_data.update({
                            CONF_CURRENT_PLAN: plan_id,
                            CONF_GRIDLENS_EMAIL: self._email,
                            CONF_GRIDLENS_API_URL: self._api_url,
                            CONF_GRIDLENS_API_KEY: self._api_key,
                        })
                        return await self.async_step_subscribe()
                    elif resp.status == 409:
                        self._sensor_data[CONF_CURRENT_PLAN] = plan_id
                        return await self.async_step_manual_key()
                    else:
                        errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"

        plan_options = [
            {"value": p["id"], "label": f"{p['retailer']} — {p['name']}"}
            for p in self._api_plans
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
            }),
            errors=errors,
        )

    async def async_step_subscribe(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Open gridlens.au/subscribe so the user can upgrade; proceed free if skipped."""
        if user_input is not None:
            # Callback arrived — pick up any api_key the subscribe page sent back
            pending = self.hass.data.get(DOMAIN, {}).get("pending_subscriptions", {})
            paid_key = pending.pop(self.flow_id, None)
            if paid_key:
                self._sensor_data[CONF_GRIDLENS_API_KEY] = paid_key
            return self.async_external_step_done(next_step_id="finalize")

        ha_url = self.hass.config.external_url or self.hass.config.internal_url
        if not ha_url:
            # No external URL configured — can't do the round-trip; skip straight to finalize
            return await self.async_step_finalize()

        callback_url = f"{ha_url.rstrip('/')}/api/grid_lens/subscribe_callback"
        subscribe_url = (
            f"https://gridlens.au/subscribe"
            f"?flow_id={self.flow_id}"
            f"&callback_url={callback_url}"
            f"&email={self._email}"
            f"&ha_installation_id={self._ha_uuid}"
        )
        return self.async_external_step(step_id="subscribe", url=subscribe_url)

    async def async_step_finalize(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=f"Grid Lens - {self._state}",
            data=self._sensor_data,
        )

    async def async_step_manual_key(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Fallback: enter existing API key if this installation is already registered."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_GRIDLENS_API_KEY]
            try:
                async with aiohttp.ClientSession() as session:
                    resp = await session.get(
                        f"{self._api_url}/plans/meta",
                        params={"state": self._state},
                        headers={"X-API-Key": api_key, "User-Agent": "GridLens-HA-Integration/1.0"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if resp.status == 200:
                        return self.async_create_entry(
                            title=f"Plan Comparison - {self._state}",
                            data={
                                **self._sensor_data,
                                CONF_GRIDLENS_EMAIL: self._email,
                                CONF_GRIDLENS_API_URL: self._api_url,
                                CONF_GRIDLENS_API_KEY: api_key,
                            },
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
        )


def _battery_schema(defaults: dict) -> vol.Schema:
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
        opt(CONF_BATTERY_EFFICIENCY, 95.0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=50.0, max=100.0, step=1.0, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX)
        ),
        opt(CONF_BATTERY_MIN_SOC, 10.0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.0, max=100.0, step=1.0, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX)
        ),
        opt(CONF_BATTERY_MAX_SOC, 90.0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.0, max=100.0, step=1.0, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX)
        ),
    }
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


class GridLensOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._sensor_data: dict = {}
        self._discovered: dict = {}
        self._device_options: list = []

    async def async_step_init(self, user_input=None):
        return await self.async_step_sensors()

    async def async_step_sensors(self, user_input=None):
        errors = {}
        entry_data = self._config_entry.data

        if not self._discovered:
            self._discovered = await _discover_energy_sensors(self.hass)
            # Merge: entry data takes precedence over fresh discovery (user may have overridden)
            merged = {**self._discovered}
            for key in (CONF_ENERGY_SENSOR, CONF_SOLAR_SENSOR, CONF_GRID_EXPORT_SENSOR,
                        CONF_IMPORT_PRICE_SENSOR, CONF_EXPORT_PRICE_SENSOR):
                if entry_data.get(key):
                    merged[key] = entry_data[key]
            self._discovered = merged

        if user_input is not None:
            energy_sensor = user_input.get(CONF_ENERGY_SENSOR)
            if energy_sensor:
                state = self.hass.states.get(energy_sensor)
                if state:
                    unit = state.attributes.get("unit_of_measurement", "").lower()
                    state_class = state.attributes.get("state_class", "")
                    if unit in ("w", "kw", "mw"):
                        errors[CONF_ENERGY_SENSOR] = "wrong_unit_power"
                    elif unit not in ("kwh", "mwh"):
                        errors[CONF_ENERGY_SENSOR] = "wrong_unit"
                    elif state_class not in ("total", "total_increasing"):
                        errors[CONF_ENERGY_SENSOR] = "wrong_state_class"

            if not errors:
                self._sensor_data = {
                    k: v for k, v in {**entry_data, **user_input}.items()
                }
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
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="battery",
            data_schema=_battery_schema(entry_data),
            errors=errors,
        )

    async def async_step_devices(self, user_input=None):
        """Select which Energy Dashboard appliances are deferrable loads."""
        entry_data = self._config_entry.data
        if not self._device_options:
            self._device_options = await _discover_dashboard_devices(self.hass)

        if not self._device_options:
            return await self.async_step_current_plan()

        if user_input is not None:
            self._sensor_data.update(user_input)
            return await self.async_step_device_power()

        current = entry_data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema({
                vol.Optional(CONF_DEFERRABLE_LOAD_SENSORS, default=current): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._device_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_device_power(self, user_input=None):
        """Capture max power draw and availability hours for each selected deferrable load."""
        entry_data = self._config_entry.data
        selected = self._sensor_data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
        if not selected:
            return await self.async_step_current_plan()

        existing_max_kw = entry_data.get(CONF_DEFERRABLE_LOAD_MAX_KW, [])
        existing_hours = entry_data.get(CONF_DEFERRABLE_LOAD_HOURS, [])
        # Existing lists are keyed by position in the previously saved sensor
        # list; map by sensor_id so reordering/removing devices keeps defaults.
        prev_sensors = entry_data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
        prev_kw = {s: existing_max_kw[i] for i, s in enumerate(prev_sensors) if i < len(existing_max_kw)}
        prev_hours = {s: existing_hours[i] for i, s in enumerate(prev_sensors) if i < len(existing_hours)}

        errors: dict[str, str] = {}
        if user_input is not None:
            hours_list = [
                str(user_input.get(f"hours_{i}", "all")).strip() or "all"
                for i in range(len(selected))
            ]
            try:
                for spec in hours_list:
                    parse_hours_spec(spec)
            except ValueError:
                errors["base"] = "invalid_hours"
            if not errors:
                max_kw_list = [float(user_input.get(f"max_kw_{i}", 3.5)) for i in range(len(selected))]
                self._sensor_data[CONF_DEFERRABLE_LOAD_MAX_KW] = max_kw_list
                self._sensor_data[CONF_DEFERRABLE_LOAD_HOURS] = hours_list
                return await self.async_step_current_plan()

        schema_dict = {}
        device_lines = []
        for i, sensor_id in enumerate(selected):
            state = self.hass.states.get(sensor_id)
            name = state.attributes.get("friendly_name", sensor_id) if state else sensor_id
            device_lines.append(f"{i + 1}. {name}")
            default_kw = prev_kw.get(sensor_id, 3.5)
            schema_dict[vol.Optional(f"max_kw_{i}", default=default_kw)] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=100.0, step=0.1,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[vol.Optional(f"hours_{i}", default=prev_hours.get(sensor_id, "all"))] = (
                selector.TextSelector()
            )

        return self.async_show_form(
            step_id="device_power",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"devices": "\n".join(device_lines)},
            errors=errors,
        )

    async def async_step_current_plan(self, user_input=None):
        """Choose which plan the user is currently on."""
        import aiohttp
        entry_data = self._config_entry.data
        api_url = entry_data.get(CONF_GRIDLENS_API_URL, GRIDLENS_DEFAULT_API_URL)
        state = entry_data.get(CONF_STATE, "NSW")
        plan_options = []
        try:
            async with aiohttp.ClientSession() as _s:
                async with _s.get(
                    f"{api_url}/plans/list",
                    params={"state": state},
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

        if user_input is not None:
            self._sensor_data = {**self._sensor_data, **user_input}
            return await self.async_step_api_key()

        current = entry_data.get(CONF_CURRENT_PLAN)
        schema = vol.Schema({
            vol.Optional(CONF_CURRENT_PLAN, **({'default': current} if current else {})): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=plan_options,
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

            if new_key and new_key != current_key:
                try:
                    async with aiohttp.ClientSession() as session:
                        resp = await session.get(
                            f"{api_url}/plans/meta",
                            params={"state": entry_data.get(CONF_STATE)},
                            headers={"X-API-Key": new_key, "User-Agent": "GridLens-HA-Integration/1.0"},
                            timeout=aiohttp.ClientTimeout(total=10),
                        )
                        if resp.status != 200:
                            errors[CONF_GRIDLENS_API_KEY] = "invalid_api_key"
                except Exception:
                    errors["base"] = "cannot_connect"

            if not errors:
                new_data = {**self._sensor_data, CONF_GRIDLENS_API_KEY: new_key or current_key}
                self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="api_key",
            data_schema=vol.Schema({
                vol.Optional(CONF_GRIDLENS_API_KEY, default=current_key): cv.string,
            }),
            errors=errors,
        )
