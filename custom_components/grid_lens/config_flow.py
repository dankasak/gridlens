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
    CONF_GRID_POWER_SENSOR,
    CONF_IMPORT_PRICE_SENSOR,
    CONF_EXPORT_PRICE_SENSOR,
    CONF_DISTRIBUTOR,
    CONF_HAS_DEMAND_TARIFF,
    CONF_HAS_CONTROLLED_LOAD_1,
    CONF_HAS_CONTROLLED_LOAD_2,
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
    CONF_INVERTER_BRAND,
    CONF_INVERTER_TRANSPORT,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_SWITCHES,
    CONF_DEFERRABLE_LOAD_SOC_SENSORS,
    CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD,
    CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE,
    DEFERRABLE_LOAD_DUMMY_SLOTS,
    CONF_DEFERRABLE_LOAD_DUMMY_NAMES,
    CONF_DEFERRABLE_LOAD_DUMMY_KWH,
    CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW,
    CONF_DEFERRABLE_LOAD_DUMMY_HOURS,
    CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD,
    CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE,
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
from .inverters import INVERTER_BRANDS, detect_inverter_brand

_LOGGER = logging.getLogger(__name__)

# Transient form field for the brand:transport dropdown — split into
# CONF_INVERTER_BRAND / CONF_INVERTER_TRANSPORT on submit, not persisted as-is.
_FIELD_INVERTER_SELECT = "inverter_select"


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
        opt(CONF_GRID_POWER_SENSOR): entity_sel(),
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
        self._has_cl1: bool = False
        self._has_cl2: bool = False
        self._discovered: dict = {}
        self._device_options: list = []
        self._sensor_data: dict = {}
        self._email: str = ""
        self._api_url: str = GRIDLENS_DEFAULT_API_URL
        self._api_plans: list[dict] = []
        self._api_vpp_programs: list[dict] = []
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
            return await self.async_step_controlled_load()

        distributors = DISTRIBUTORS.get(self._state, [])
        return self.async_show_form(
            step_id="distributor",
            data_schema=vol.Schema({
                vol.Required(CONF_DISTRIBUTOR): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=distributors, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
            }),
        )

    async def async_step_controlled_load(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Ask whether the household's meter has Controlled Load 1/2 switched on.

        Network/meter fact set by the DNSP, not the retail plan — same
        self-declare pattern as CONF_HAS_DEMAND_TARIFF. Answered here (before
        devices) so async_step_device_power below can offer the
        wired_to_controlled_load dropdown only for registers the household
        actually has.
        """
        if user_input is not None:
            self._has_cl1 = user_input.get(CONF_HAS_CONTROLLED_LOAD_1, False)
            self._has_cl2 = user_input.get(CONF_HAS_CONTROLLED_LOAD_2, False)
            return await self.async_step_sensors()

        return self.async_show_form(
            step_id="controlled_load",
            data_schema=vol.Schema({
                vol.Optional(CONF_HAS_CONTROLLED_LOAD_1, default=False): selector.BooleanSelector(),
                vol.Optional(CONF_HAS_CONTROLLED_LOAD_2, default=False): selector.BooleanSelector(),
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
                    CONF_HAS_CONTROLLED_LOAD_1: self._has_cl1,
                    CONF_HAS_CONTROLLED_LOAD_2: self._has_cl2,
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
        errors: dict[str, str] = {}

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
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="battery",
            data_schema=_battery_schema({}),
            errors=errors,
        )

    async def async_step_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Pick which inverter brand/transport ControlManager should dispatch to."""
        if user_input is not None:
            brand, _, transport = user_input[_FIELD_INVERTER_SELECT].partition(":")
            self._sensor_data = {
                **self._sensor_data,
                CONF_INVERTER_BRAND: brand,
                CONF_INVERTER_TRANSPORT: transport,
            }
            return await self.async_step_devices()

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
            data_schema=_inverter_schema({}, detected),
            description_placeholders=description_placeholders,
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
        """Capture max power draw for each selected deferrable load. Availability
        windows are no longer set here — paint them on the Deferrable Loads dashboard
        card's weekly schedule after setup (a device is unrestricted, any hour, until
        you do)."""
        selected = self._sensor_data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
        if not selected:
            return await self.async_step_declared_loads()

        if user_input is not None:
            max_kw_list = [float(user_input.get(f"max_kw_{i}", 3.5)) for i in range(len(selected))]
            # Optional control switch per device ("" = forecast-only, no actuation).
            switches_list = [
                str(user_input.get(f"switch_{i}", "") or "") for i in range(len(selected))
            ]
            # Optional battery/EV SOC sensor per device ("" = none, e.g. a pool pump).
            soc_list = [
                str(user_input.get(f"soc_{i}", "") or "") for i in range(len(selected))
            ]
            # Optional Controlled Load register wiring per device ("" = not on CL).
            # Only present in user_input at all when at least one CL register was
            # offered (self._has_cl1/_has_cl2) — otherwise every device defaults "".
            cl_list = [
                str(user_input.get(f"cl_{i}", "") or "") for i in range(len(selected))
            ]
            # Whether that device's energy is currently mixed into the main
            # energy_sensor reading (needs subtracting before CL-pricing) — same
            # gating as cl_list above.
            in_agg_list = [
                bool(user_input.get(f"in_aggregate_{i}", False)) for i in range(len(selected))
            ]
            self._sensor_data[CONF_DEFERRABLE_LOAD_MAX_KW] = max_kw_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SWITCHES] = switches_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SOC_SENSORS] = soc_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD] = cl_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE] = in_agg_list
            return await self.async_step_declared_loads()

        # Controlled Load register choices, gated on the flags collected in
        # async_step_controlled_load. Offered per-device below only if non-empty.
        cl_register_options = []
        if self._has_cl1:
            cl_register_options.append({"value": "controlled_load_1", "label": "Controlled Load 1"})
        if self._has_cl2:
            cl_register_options.append({"value": "controlled_load_2", "label": "Controlled Load 2"})

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
            # Optional: a switch.* entity GridLens turns on/off to actuate this load. Leave
            # unset for forecast-only devices (e.g. an ESS-managed port with no HA switch).
            schema_dict[vol.Optional(f"switch_{i}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")
            )
            # Optional: a sensor.* entity reporting this device's own battery state of charge
            # (%) — most relevant for an EV charger (the vehicle's SOC), shown on the Power
            # Flow card the same way the home battery's SOC is. Leave unset for loads with no
            # battery of their own (pool pump, hot water, etc).
            schema_dict[vol.Optional(f"soc_{i}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            )
            # Optional: which Controlled Load register (if any) this device is physically
            # wired to. Only shown when the household declared at least one CL register in
            # async_step_controlled_load.
            # TODO: filter cl_register_options by which device types the network actually
            # confirms for that register (NetworkIR.controlled_load_eligible_devices in the
            # API's plan_transform.py / get_network_operators()) — no live network
            # eligible-device lookup is wired into the config flow yet, so every configured
            # register is offered regardless of this device's type.
            if cl_register_options:
                schema_dict[vol.Optional(f"cl_{i}", default="")] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": "", "label": "Not on controlled load"}] + cl_register_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
                # Whether this device's energy is currently mixed into the main
                # energy_sensor reading (e.g. a general-circuit appliance being
                # considered for a move to Controlled Load) vs already on a genuinely
                # separate register the main sensor never sees (the normal case — a
                # real CL circuit is wired separately from an inverter's CT clamp).
                schema_dict[vol.Optional(f"in_aggregate_{i}", default=False)] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="device_power",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"devices": "\n".join(device_lines)},
        )

    async def async_step_declared_loads(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Declare loads with no HA-visible energy sensor at all — the common case for
        a genuine Controlled Load circuit (wired separately from whatever an inverter's
        CT clamp monitors, so it normally can't be pointed at a sensor via the devices
        step above; a household would need to have deliberately added a Shelly or
        similar on that specific circuit). Estimated average daily kWh stands in for a
        real sensor. Fixed DEFERRABLE_LOAD_DUMMY_SLOTS slots (config-flow schemas are
        static — no native "add another" UX); a slot with a blank name is unused."""
        errors: dict[str, str] = {}
        if user_input is not None:
            names = [
                str(user_input.get(f"name_{i}", "") or "").strip()
                for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
            ]
            hours_list = [
                str(user_input.get(f"hours_{i}", "all")).strip() or "all"
                for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
            ]
            try:
                for name, spec in zip(names, hours_list):
                    if name:
                        parse_hours_spec(spec)
            except ValueError:
                errors["base"] = "invalid_hours"
            if not errors:
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_NAMES] = names
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_KWH] = [
                    float(user_input.get(f"kwh_{i}", 0.0) or 0.0)
                    for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
                ]
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW] = [
                    float(user_input.get(f"max_kw_{i}", 3.5))
                    for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
                ]
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_HOURS] = hours_list
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD] = [
                    str(user_input.get(f"cl_{i}", "") or "")
                    for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
                ]
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE] = [
                    bool(user_input.get(f"in_aggregate_{i}", False))
                    for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
                ]
                return await self.async_step_current_plan()

        cl_register_options = []
        if self._has_cl1:
            cl_register_options.append({"value": "controlled_load_1", "label": "Controlled Load 1"})
        if self._has_cl2:
            cl_register_options.append({"value": "controlled_load_2", "label": "Controlled Load 2"})

        schema_dict = {}
        for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS):
            schema_dict[vol.Optional(f"name_{i}", default="")] = selector.TextSelector()
            schema_dict[vol.Optional(f"kwh_{i}", default=0.0)] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=100.0, step=0.1,
                    unit_of_measurement="kWh/day",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[vol.Optional(f"max_kw_{i}", default=3.5)] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=100.0, step=0.1,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[vol.Optional(f"hours_{i}", default="all")] = selector.TextSelector()
            if cl_register_options:
                schema_dict[vol.Optional(f"cl_{i}", default="")] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": "", "label": "Not on controlled load"}] + cl_register_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
                schema_dict[vol.Optional(f"in_aggregate_{i}", default=False)] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="declared_loads",
            data_schema=vol.Schema(schema_dict),
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

        # VPP bolt-on programs are independent of plan choice and this endpoint is
        # public — a fetch failure shouldn't block plan setup, so it just leaves the
        # dropdown at "None / not enrolled" rather than setting `errors`.
        if not self._api_vpp_programs:
            try:
                async with aiohttp.ClientSession() as session:
                    resp = await session.get(
                        f"{self._api_url}/vpp-programs/list",
                        params={"state": self._state},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if resp.status == 200:
                        self._api_vpp_programs = await resp.json()
            except Exception as exc:
                _LOGGER.warning("Could not fetch VPP program list: %s", exc)

        if user_input is not None and not errors:
            plan_id = user_input[CONF_CURRENT_PLAN]
            has_demand_tariff = user_input.get(CONF_HAS_DEMAND_TARIFF, False)
            vpp_program = user_input.get(CONF_VPP_PROGRAM) or None
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
                            CONF_HAS_DEMAND_TARIFF: has_demand_tariff,
                            CONF_VPP_PROGRAM: vpp_program,
                            CONF_GRIDLENS_EMAIL: self._email,
                            CONF_GRIDLENS_API_URL: self._api_url,
                            CONF_GRIDLENS_API_KEY: self._api_key,
                        })
                        return await self.async_step_subscribe()
                    elif resp.status == 409:
                        self._sensor_data[CONF_CURRENT_PLAN] = plan_id
                        self._sensor_data[CONF_HAS_DEMAND_TARIFF] = has_demand_tariff
                        self._sensor_data[CONF_VPP_PROGRAM] = vpp_program
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
        self._has_cl1: bool = False
        self._has_cl2: bool = False

    async def async_step_init(self, user_input=None):
        """Entry point for Configure — a menu so a quick task (pasting a new API
        key after subscribing/re-subscribing) doesn't require walking the entire
        reconfigure wizard from the top just to reach the field at the end."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["api_key", "full_reconfigure"],
        )

    async def async_step_full_reconfigure(self, user_input=None):
        """The pre-existing full wizard, now reached via the menu above instead
        of unconditionally."""
        return await self.async_step_controlled_load()

    async def async_step_controlled_load(self, user_input=None):
        """Ask whether the household's meter has Controlled Load 1/2 switched on.

        Network/meter fact set by the DNSP, not the retail plan — same
        self-declare pattern as CONF_HAS_DEMAND_TARIFF. Answered here (before
        devices) so async_step_device_power below can offer the
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
            for key in (CONF_ENERGY_SENSOR, CONF_SOLAR_SENSOR, CONF_GRID_EXPORT_SENSOR,
                        CONF_GRID_POWER_SENSOR, CONF_IMPORT_PRICE_SENSOR, CONF_EXPORT_PRICE_SENSOR):
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
                return await self.async_step_devices()

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
            return await self.async_step_devices()

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
        # Drop any previously-saved sensor no longer offered by the discovery scan
        # (e.g. renamed/deleted since last configured) — an invalid default fails
        # SelectSelector validation for the whole field, blocking new selections too.
        valid_values = {opt["value"] for opt in self._device_options}
        current = [c for c in current if c in valid_values]

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
        """Capture max power draw for each selected deferrable load. Availability
        windows are no longer set here — paint them on the Deferrable Loads dashboard
        card's weekly schedule (a device is unrestricted, any hour, until you do)."""
        entry_data = self._config_entry.data
        selected = self._sensor_data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
        if not selected:
            return await self.async_step_declared_loads()

        existing_max_kw = entry_data.get(CONF_DEFERRABLE_LOAD_MAX_KW, [])
        existing_switches = entry_data.get(CONF_DEFERRABLE_LOAD_SWITCHES, [])
        existing_soc = entry_data.get(CONF_DEFERRABLE_LOAD_SOC_SENSORS, [])
        existing_cl = entry_data.get(CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD, [])
        existing_in_agg = entry_data.get(CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE, [])
        # Existing lists are keyed by position in the previously saved sensor
        # list; map by sensor_id so reordering/removing devices keeps defaults.
        prev_sensors = entry_data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
        prev_kw = {s: existing_max_kw[i] for i, s in enumerate(prev_sensors) if i < len(existing_max_kw)}
        prev_switch = {
            s: existing_switches[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_switches) and existing_switches[i]
        }
        prev_soc = {
            s: existing_soc[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_soc) and existing_soc[i]
        }
        prev_cl = {
            s: existing_cl[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_cl) and existing_cl[i]
        }
        prev_in_agg = {
            s: existing_in_agg[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_in_agg)
        }

        if user_input is not None:
            max_kw_list = [float(user_input.get(f"max_kw_{i}", 3.5)) for i in range(len(selected))]
            switches_list = [
                str(user_input.get(f"switch_{i}", "") or "") for i in range(len(selected))
            ]
            soc_list = [
                str(user_input.get(f"soc_{i}", "") or "") for i in range(len(selected))
            ]
            cl_list = [
                str(user_input.get(f"cl_{i}", "") or "") for i in range(len(selected))
            ]
            in_agg_list = [
                bool(user_input.get(f"in_aggregate_{i}", False)) for i in range(len(selected))
            ]
            self._sensor_data[CONF_DEFERRABLE_LOAD_MAX_KW] = max_kw_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SWITCHES] = switches_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SOC_SENSORS] = soc_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD] = cl_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE] = in_agg_list
            return await self.async_step_declared_loads()

        # Controlled Load register choices, gated on the flags collected in
        # async_step_controlled_load. Offered per-device below only if non-empty.
        cl_register_options = []
        if self._has_cl1:
            cl_register_options.append({"value": "controlled_load_1", "label": "Controlled Load 1"})
        if self._has_cl2:
            cl_register_options.append({"value": "controlled_load_2", "label": "Controlled Load 2"})

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
            # Optional control switch; pre-fill the previously saved one (vol.Optional with
            # no default when unset, so the field renders empty rather than forcing a value).
            prev_sw = prev_switch.get(sensor_id)
            switch_key = (
                vol.Optional(f"switch_{i}", default=prev_sw)
                if prev_sw else vol.Optional(f"switch_{i}")
            )
            schema_dict[switch_key] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")
            )
            # Optional battery/EV SOC sensor; pre-fill the previously saved one, same pattern
            # as the control switch above.
            prev_soc_sensor = prev_soc.get(sensor_id)
            soc_key = (
                vol.Optional(f"soc_{i}", default=prev_soc_sensor)
                if prev_soc_sensor else vol.Optional(f"soc_{i}")
            )
            schema_dict[soc_key] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            )
            # Optional Controlled Load register wiring; only shown when the household
            # declared at least one CL register in async_step_controlled_load.
            # TODO: filter cl_register_options by which device types the network actually
            # confirms for that register (NetworkIR.controlled_load_eligible_devices in
            # the API's plan_transform.py / get_network_operators()) — no live network
            # eligible-device lookup is wired into the config flow yet.
            if cl_register_options:
                schema_dict[vol.Optional(f"cl_{i}", default=prev_cl.get(sensor_id, ""))] = (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[{"value": "", "label": "Not on controlled load"}] + cl_register_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                )
                schema_dict[vol.Optional(
                    f"in_aggregate_{i}", default=prev_in_agg.get(sensor_id, False),
                )] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="device_power",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"devices": "\n".join(device_lines)},
        )

    async def async_step_declared_loads(self, user_input=None):
        """Declare loads with no HA-visible energy sensor at all — see the setup-flow
        docstring on this same step name for the full rationale (genuine Controlled
        Load circuits normally have none)."""
        entry_data = self._config_entry.data
        existing_names = entry_data.get(CONF_DEFERRABLE_LOAD_DUMMY_NAMES, [])
        existing_kwh = entry_data.get(CONF_DEFERRABLE_LOAD_DUMMY_KWH, [])
        existing_max_kw = entry_data.get(CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW, [])
        existing_hours = entry_data.get(CONF_DEFERRABLE_LOAD_DUMMY_HOURS, [])
        existing_cl = entry_data.get(CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD, [])
        existing_in_agg = entry_data.get(CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE, [])

        errors: dict[str, str] = {}
        if user_input is not None:
            names = [
                str(user_input.get(f"name_{i}", "") or "").strip()
                for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
            ]
            hours_list = [
                str(user_input.get(f"hours_{i}", "all")).strip() or "all"
                for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
            ]
            try:
                for name, spec in zip(names, hours_list):
                    if name:
                        parse_hours_spec(spec)
            except ValueError:
                errors["base"] = "invalid_hours"
            if not errors:
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_NAMES] = names
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_KWH] = [
                    float(user_input.get(f"kwh_{i}", 0.0) or 0.0)
                    for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
                ]
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW] = [
                    float(user_input.get(f"max_kw_{i}", 3.5))
                    for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
                ]
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_HOURS] = hours_list
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD] = [
                    str(user_input.get(f"cl_{i}", "") or "")
                    for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
                ]
                self._sensor_data[CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE] = [
                    bool(user_input.get(f"in_aggregate_{i}", False))
                    for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS)
                ]
                return await self.async_step_current_plan()

        cl_register_options = []
        if self._has_cl1:
            cl_register_options.append({"value": "controlled_load_1", "label": "Controlled Load 1"})
        if self._has_cl2:
            cl_register_options.append({"value": "controlled_load_2", "label": "Controlled Load 2"})

        schema_dict = {}
        for i in range(DEFERRABLE_LOAD_DUMMY_SLOTS):
            schema_dict[vol.Optional(
                f"name_{i}", default=existing_names[i] if i < len(existing_names) else "",
            )] = selector.TextSelector()
            schema_dict[vol.Optional(
                f"kwh_{i}", default=existing_kwh[i] if i < len(existing_kwh) else 0.0,
            )] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=100.0, step=0.1,
                    unit_of_measurement="kWh/day",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[vol.Optional(
                f"max_kw_{i}", default=existing_max_kw[i] if i < len(existing_max_kw) else 3.5,
            )] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=100.0, step=0.1,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[vol.Optional(
                f"hours_{i}", default=existing_hours[i] if i < len(existing_hours) else "all",
            )] = selector.TextSelector()
            if cl_register_options:
                schema_dict[vol.Optional(
                    f"cl_{i}", default=existing_cl[i] if i < len(existing_cl) else "",
                )] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": "", "label": "Not on controlled load"}] + cl_register_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
                schema_dict[vol.Optional(
                    f"in_aggregate_{i}", default=existing_in_agg[i] if i < len(existing_in_agg) else False,
                )] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="declared_loads",
            data_schema=vol.Schema(schema_dict),
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

        # VPP bolt-on programs are independent of plan choice and this endpoint is
        # public — a fetch failure just leaves the dropdown at "None / not enrolled".
        vpp_options = [{"value": "", "label": "None / not enrolled"}]
        try:
            async with aiohttp.ClientSession() as _s:
                async with _s.get(
                    f"{api_url}/vpp-programs/list",
                    params={"state": state},
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
                # Base on the entry's own current data, not just self._sensor_data —
                # reached directly from the menu (the common case now), _sensor_data
                # is still empty (only the full wizard path below populates it), and
                # {**self._sensor_data, ...} alone would silently wipe every other
                # setting on the entry down to just the new key.
                new_data = {**self._config_entry.data, **self._sensor_data,
                            CONF_GRIDLENS_API_KEY: new_key or current_key}
                self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="api_key",
            data_schema=vol.Schema({
                vol.Optional(CONF_GRIDLENS_API_KEY, default=current_key): cv.string,
            }),
            errors=errors,
        )
