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
    CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE,
    CONF_DEFERRABLE_LOAD_SETPOINT,
    CONF_DEFERRABLE_LOAD_SETPOINT_UNIT,
    CONF_DEFERRABLE_LOAD_PHASES,
    CONF_DEFERRABLE_LOAD_VOLTAGE,
    CONF_DEFERRABLE_LOAD_MIN_CURRENT,
    CONF_DEFERRABLE_LOAD_PLUG_SENSOR,
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
            # Optional control entity per device — switch.* or climate.* (aircon); "" =
            # forecast-only, no actuation.
            switches_list = [
                str(user_input.get(f"switch_{i}", "") or "") for i in range(len(selected))
            ]
            # Only consulted for a climate.* control entity that doesn't support
            # climate.turn_on/turn_off (see control/load_controller.py._actuate()); "" =
            # auto-pick. Meaningless for a switch.* control entity.
            climate_mode_list = [
                str(user_input.get(f"climate_on_mode_{i}", "") or "") for i in range(len(selected))
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
            # Modulating ("type 2") control, per device — see const.py's
            # CONF_DEFERRABLE_LOAD_SETPOINT block for the full rationale. A device is
            # only modulating when setpoint_i is non-empty; every other field here is
            # meaningless (and safely ignored) for an on/off or forecast-only device.
            setpoint_list = [
                str(user_input.get(f"setpoint_{i}", "") or "") for i in range(len(selected))
            ]
            setpoint_unit_list = [
                str(user_input.get(f"setpoint_unit_{i}", "") or "") for i in range(len(selected))
            ]
            # Submitted by the SelectSelector below as the string "0"/"1"/"2"/"3" (its
            # option values match the schema's own default type) — coerce to int for
            # storage, matching CONF_DEFERRABLE_LOAD_PHASES's documented "list of int".
            phases_list = [
                int(user_input.get(f"phases_{i}", 0) or 0) for i in range(len(selected))
            ]
            voltage_list = [
                float(user_input.get(f"voltage_{i}", 0.0) or 0.0) for i in range(len(selected))
            ]
            min_current_list = [
                float(user_input.get(f"min_current_{i}", 0.0) or 0.0) for i in range(len(selected))
            ]
            plug_sensor_list = [
                str(user_input.get(f"plug_sensor_{i}", "") or "") for i in range(len(selected))
            ]
            self._sensor_data[CONF_DEFERRABLE_LOAD_MAX_KW] = max_kw_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SWITCHES] = switches_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE] = climate_mode_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SOC_SENSORS] = soc_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD] = cl_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE] = in_agg_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SETPOINT] = setpoint_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SETPOINT_UNIT] = setpoint_unit_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_PHASES] = phases_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_VOLTAGE] = voltage_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_MIN_CURRENT] = min_current_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_PLUG_SENSOR] = plug_sensor_list
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
        name_placeholders: dict[str, str] = {}
        for i, sensor_id in enumerate(selected):
            state = self.hass.states.get(sensor_id)
            name = state.attributes.get("friendly_name", sensor_id) if state else sensor_id
            device_lines.append(f"{i + 1}. {name}")
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
            # Optional: a switch.* or climate.* (aircon) entity GridLens turns on/off to
            # actuate this load. Leave unset for forecast-only devices (e.g. an
            # ESS-managed port with no HA switch).
            schema_dict[vol.Optional(f"switch_{i}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "climate"])
            )
            # Only relevant if the control entity above is a climate.* one that doesn't
            # support climate.turn_on/turn_off (most do) — which hvac_mode to command for
            # "on". Ignored otherwise.
            schema_dict[vol.Optional(f"climate_on_mode_{i}", default="")] = selector.SelectSelector(
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
            )
            # Optional: a sensor.* entity reporting this device's own battery state of charge
            # (%) — most relevant for an EV charger (the vehicle's SOC), shown on the Power
            # Flow card the same way the home battery's SOC is. Leave unset for loads with no
            # battery of their own (pool pump, hot water, etc).
            schema_dict[vol.Optional(f"soc_{i}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            )
            # --- Modulating ("type 2") control — see CONF_DEFERRABLE_LOAD_SETPOINT in
            # const.py for the full rationale. Configuring a setpoint entity here turns
            # this device from on/off into continuously-throttled: GridLens commands a
            # charging-current/power limit on a 30 s tick instead of flipping the switch
            # above. Not OCPP-specific — any integration exposing a current-limit number
            # entity works (OCPP, Easee, Wallbox, Zaptec, go-e, openEVSE, Tesla and
            # Sigenergy AC chargers are all examples of the same shape). Leave blank to
            # keep this device on plain on/off control (via the switch above, or
            # forecast-only if that's blank too) — every field below is then unused.
            schema_dict[vol.Optional(f"setpoint_{i}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="number")
            )
            # What the setpoint entity's value means. "" = auto: read the entity's own
            # unit_of_measurement (A → current, W/kW → power) — right for every
            # integration listed above. Only needed for one that publishes no unit.
            schema_dict[vol.Optional(f"setpoint_unit_{i}", default="")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "", "label": "Auto (detect from the entity)"},
                        {"value": "a", "label": "Amps (A)"},
                        {"value": "w", "label": "Watts (W)"},
                        {"value": "kw", "label": "Kilowatts (kW)"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            # Phases the setpoint's amps figure applies across (W = amps × voltage ×
            # phases). 0 = auto-derive from this device's max power above and the
            # setpoint entity's own max value — right far more often than a guess, since
            # a 7.4 kW single-phase charger and a 22 kW three-phase one can both
            # advertise a 32 A ceiling; only max_kw tells them apart. Ignored for a
            # W/kW setpoint. Option values are strings ("0".."3") to match what the
            # frontend submits; coerced back to int on save.
            schema_dict[vol.Optional(f"phases_{i}", default="0")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "0", "label": "Auto-detect"},
                        {"value": "1", "label": "Single phase"},
                        {"value": "2", "label": "Two phase"},
                        {"value": "3", "label": "Three phase"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            # Nominal per-phase supply voltage for the amps↔watts conversion above. 0 =
            # use the 230 V IEC/AU default (DEFAULT_SUPPLY_VOLTAGE) — allowed as an
            # explicit value, not just a fallback, since most installs never need to
            # touch this. A 5% voltage error is only a 5% power error, well inside the
            # slack the min-current floor and write deadband already carry.
            schema_dict[vol.Optional(f"voltage_{i}", default=0.0)] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=500.0, step=1,
                    unit_of_measurement="V",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            # Minimum current this device can actually be given (A). An EV must not be
            # offered below ~6 A (IEC 61851 duty-cycle floor) — commanding less doesn't
            # charge slowly, it makes the car refuse or fault. 0 = use the 6 A default
            # (DEFAULT_MIN_CHARGE_CURRENT_A); set e.g. 0.1 for a genuinely continuous
            # load (a resistive heater on a dimmer) with no such floor.
            schema_dict[vol.Optional(f"min_current_{i}", default=0.0)] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=32.0, step=0.1,
                    unit_of_measurement="A",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            # Optional: a binary_sensor.* or sensor.* (e.g. OCPP's own ChargePointStatus
            # sensor) reporting whether something is actually plugged in / able to
            # accept charge. Leave unset if you don't have one — GridLens never
            # withholds charging just because it couldn't confirm a plug; this only
            # stops it commanding current into an empty socket.
            schema_dict[vol.Optional(f"plug_sensor_{i}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor", "sensor"])
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
            description_placeholders={"devices": "\n".join(device_lines), **name_placeholders},
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
                return await self.async_step_estimated_loads()

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

    async def async_step_estimated_loads(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Devices with a control entity (switch.*/climate.*) but no energy sensor at
        all and no way to add one — e.g. an IR-blaster-driven aircon. GridLens builds a
        synthetic energy sensor for these from aggregate-load inference (an optional
        auto-refine, load_estimation.py) seeded with a manual estimate. Fixed
        DEFERRABLE_LOAD_ESTIMATED_SLOTS slots, same static-schema reasoning as the
        declared-loads step above; a slot with a blank name is unused."""
        errors: dict[str, str] = {}
        if user_input is not None:
            est_names = [
                str(user_input.get(f"est_name_{i}", "") or "").strip()
                for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS)
            ]
            est_controls = [
                str(user_input.get(f"est_control_{i}", "") or "")
                for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS)
            ]
            # A slot with only one of {name, control} filled in silently does nothing
            # (_ensure_load_estimators skips on blank name; a blank control just warns
            # and skips too) — the exact bug this validation exists to catch upfront.
            if any(bool(n) != bool(c) for n, c in zip(est_names, est_controls)):
                errors["base"] = "estimated_load_name_control_mismatch"
            # Same device registered on both the previous (forecast-only) step and this
            # (controllable) one double-counts it — the LP sees it twice, once as an
            # always-on-schedule declared load and once as the real controllable device.
            # Caught for real on 2026-08-06: a Declared Loads leftover for "Daikin
            # Aircon" sat alongside a half-set-up Estimated Loads slot for the same unit.
            declared_names = {
                str(n).strip().lower()
                for n in self._sensor_data.get(CONF_DEFERRABLE_LOAD_DUMMY_NAMES, [])
                if n
            }
            if not errors and any(
                n.strip().lower() in declared_names for n in est_names if n
            ):
                errors["base"] = "estimated_load_duplicate_declared"
            if not errors:
                self._sensor_data[CONF_DEFERRABLE_LOAD_EST_NAMES] = est_names
                self._sensor_data[CONF_DEFERRABLE_LOAD_EST_CONTROL] = est_controls
                self._sensor_data[CONF_DEFERRABLE_LOAD_EST_KW] = [
                    float(user_input.get(f"est_kw_{i}", 1.0) or 1.0)
                    for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS)
                ]
                self._sensor_data[CONF_DEFERRABLE_LOAD_EST_AUTO] = [
                    bool(user_input.get(f"est_auto_{i}", False))
                    for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS)
                ]
                self._sensor_data[CONF_LOAD_POWER_SENSOR] = str(
                    user_input.get("load_power_sensor", "") or ""
                )
                return await self.async_step_current_plan()

        schema_dict = {}
        for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS):
            schema_dict[vol.Optional(f"est_name_{i}", default="")] = selector.TextSelector()
            # switch.* or climate.* — the device GridLens turns on/off. Required for a
            # slot to actually take effect (see __init__.py._ensure_load_estimators).
            schema_dict[vol.Optional(f"est_control_{i}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "climate"])
            )
            schema_dict[vol.Optional(f"est_kw_{i}", default=1.0)] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.05, max=20.0, step=0.05,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            # Opt-in: refine the seed above from real on/off transitions (needs
            # load_power_sensor below configured — otherwise this silently never fires).
            schema_dict[vol.Optional(f"est_auto_{i}", default=False)] = selector.BooleanSelector()
        # One field for the whole step, not per-slot: the whole-house load power sensor
        # auto-refine samples around a transition. The same kind of entity you'd use for
        # the Power Flow card's own "Load Power Entity" option — optional here too; if
        # left unset, auto-refine simply never fires and every slot above stays on its
        # manual kW seed.
        schema_dict[vol.Optional("load_power_sensor")] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )

        return self.async_show_form(
            step_id="estimated_loads",
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
            for key in _ENERGY_SCHEMA_KEYS:
                # Membership, not truthiness: a stored None means the user deliberately
                # cleared that sensor. Testing `entry_data.get(key)` would treat cleared
                # the same as never-set and let Energy-dashboard discovery put it back.
                if key in entry_data:
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
                self._sensor_data = {**entry_data, **user_input}
                # Every key in _energy_schema is user-editable, and a cleared
                # EntitySelector is absent from user_input rather than None. Spreading
                # entry_data first would inherit the old value for exactly those keys,
                # so re-assert each one explicitly: absent == cleared.
                for key in _ENERGY_SCHEMA_KEYS:
                    if key == CONF_ENERGY_SENSOR:
                        continue  # required field — never clearable
                    self._sensor_data[key] = user_input.get(key) or None
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
        existing_climate_modes = entry_data.get(CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE, [])
        existing_soc = entry_data.get(CONF_DEFERRABLE_LOAD_SOC_SENSORS, [])
        existing_cl = entry_data.get(CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD, [])
        existing_in_agg = entry_data.get(CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE, [])
        # Modulating-control fields — absent from every config entry saved before this
        # change, hence `.get(KEY, [])`: an old entry just yields empty lists here, and
        # every device below falls through to its "not modulating" default, unchanged
        # from what it did before this feature existed.
        existing_setpoint = entry_data.get(CONF_DEFERRABLE_LOAD_SETPOINT, [])
        existing_setpoint_unit = entry_data.get(CONF_DEFERRABLE_LOAD_SETPOINT_UNIT, [])
        existing_phases = entry_data.get(CONF_DEFERRABLE_LOAD_PHASES, [])
        existing_voltage = entry_data.get(CONF_DEFERRABLE_LOAD_VOLTAGE, [])
        existing_min_current = entry_data.get(CONF_DEFERRABLE_LOAD_MIN_CURRENT, [])
        existing_plug_sensor = entry_data.get(CONF_DEFERRABLE_LOAD_PLUG_SENSOR, [])
        # Existing lists are keyed by position in the previously saved sensor
        # list; map by sensor_id so reordering/removing devices keeps defaults.
        prev_sensors = entry_data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
        prev_kw = {s: existing_max_kw[i] for i, s in enumerate(prev_sensors) if i < len(existing_max_kw)}
        prev_switch = {
            s: existing_switches[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_switches) and existing_switches[i]
        }
        prev_climate_mode = {
            s: existing_climate_modes[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_climate_modes) and existing_climate_modes[i]
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
        prev_setpoint = {
            s: existing_setpoint[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_setpoint) and existing_setpoint[i]
        }
        prev_setpoint_unit = {
            s: existing_setpoint_unit[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_setpoint_unit)
        }
        prev_phases = {
            s: existing_phases[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_phases)
        }
        prev_voltage = {
            s: existing_voltage[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_voltage)
        }
        prev_min_current = {
            s: existing_min_current[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_min_current)
        }
        prev_plug_sensor = {
            s: existing_plug_sensor[i]
            for i, s in enumerate(prev_sensors)
            if i < len(existing_plug_sensor) and existing_plug_sensor[i]
        }

        if user_input is not None:
            max_kw_list = [float(user_input.get(f"max_kw_{i}", 3.5)) for i in range(len(selected))]
            switches_list = [
                str(user_input.get(f"switch_{i}", "") or "") for i in range(len(selected))
            ]
            climate_mode_list = [
                str(user_input.get(f"climate_on_mode_{i}", "") or "") for i in range(len(selected))
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
            # Modulating ("type 2") control, per device — same fields/coercion as the
            # setup-flow version of this step; see that copy's comment for the full
            # rationale. Unlike the entity-selector fields above, there's no
            # stale-vs-fresh ambiguity to worry about here: the form always re-asserts
            # a value (defaulting to "not modulating") on every save through this step.
            setpoint_list = [
                str(user_input.get(f"setpoint_{i}", "") or "") for i in range(len(selected))
            ]
            setpoint_unit_list = [
                str(user_input.get(f"setpoint_unit_{i}", "") or "") for i in range(len(selected))
            ]
            phases_list = [
                int(user_input.get(f"phases_{i}", 0) or 0) for i in range(len(selected))
            ]
            voltage_list = [
                float(user_input.get(f"voltage_{i}", 0.0) or 0.0) for i in range(len(selected))
            ]
            min_current_list = [
                float(user_input.get(f"min_current_{i}", 0.0) or 0.0) for i in range(len(selected))
            ]
            plug_sensor_list = [
                str(user_input.get(f"plug_sensor_{i}", "") or "") for i in range(len(selected))
            ]
            self._sensor_data[CONF_DEFERRABLE_LOAD_MAX_KW] = max_kw_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SWITCHES] = switches_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE] = climate_mode_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SOC_SENSORS] = soc_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD] = cl_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE] = in_agg_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SETPOINT] = setpoint_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_SETPOINT_UNIT] = setpoint_unit_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_PHASES] = phases_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_VOLTAGE] = voltage_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_MIN_CURRENT] = min_current_list
            self._sensor_data[CONF_DEFERRABLE_LOAD_PLUG_SENSOR] = plug_sensor_list
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
        name_placeholders: dict[str, str] = {}
        for i, sensor_id in enumerate(selected):
            state = self.hass.states.get(sensor_id)
            name = state.attributes.get("friendly_name", sensor_id) if state else sensor_id
            device_lines.append(f"{i + 1}. {name}")
            # Substituted into each field's own label below (see strings.json's
            # "{device_N_name} — ..." labels) so the form shows the actual device name
            # instead of "Device N".
            name_placeholders[f"device_{i}_name"] = name
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
                selector.EntitySelectorConfig(domain=["switch", "climate"])
            )
            # Only relevant if the control entity above is a climate.* one that doesn't
            # support climate.turn_on/turn_off (most do) — which hvac_mode to command for
            # "on". Ignored otherwise.
            schema_dict[vol.Optional(
                f"climate_on_mode_{i}", default=prev_climate_mode.get(sensor_id, "")
            )] = selector.SelectSelector(
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
            # --- Modulating ("type 2") control — see the setup-flow copy of this step
            # for the full rationale (same fields, same meaning). Pre-fill every value
            # from the previously saved config, sensor_id-keyed like switch/soc above,
            # so reordering or removing a device elsewhere in the wizard doesn't lose
            # a household's charger tuning.
            prev_setpoint_entity = prev_setpoint.get(sensor_id)
            setpoint_key = (
                vol.Optional(f"setpoint_{i}", default=prev_setpoint_entity)
                if prev_setpoint_entity else vol.Optional(f"setpoint_{i}")
            )
            schema_dict[setpoint_key] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="number")
            )
            schema_dict[vol.Optional(
                f"setpoint_unit_{i}", default=prev_setpoint_unit.get(sensor_id, ""),
            )] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "", "label": "Auto (detect from the entity)"},
                        {"value": "a", "label": "Amps (A)"},
                        {"value": "w", "label": "Watts (W)"},
                        {"value": "kw", "label": "Kilowatts (kW)"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            # Stored as int (see CONF_DEFERRABLE_LOAD_PHASES); the SelectSelector's own
            # option values are strings, so re-stringify the saved default to match.
            schema_dict[vol.Optional(
                f"phases_{i}", default=str(prev_phases.get(sensor_id, 0)),
            )] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "0", "label": "Auto-detect"},
                        {"value": "1", "label": "Single phase"},
                        {"value": "2", "label": "Two phase"},
                        {"value": "3", "label": "Three phase"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            schema_dict[vol.Optional(
                f"voltage_{i}", default=prev_voltage.get(sensor_id, 0.0),
            )] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=500.0, step=1,
                    unit_of_measurement="V",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[vol.Optional(
                f"min_current_{i}", default=prev_min_current.get(sensor_id, 0.0),
            )] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=32.0, step=0.1,
                    unit_of_measurement="A",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            prev_plug = prev_plug_sensor.get(sensor_id)
            plug_key = (
                vol.Optional(f"plug_sensor_{i}", default=prev_plug)
                if prev_plug else vol.Optional(f"plug_sensor_{i}")
            )
            schema_dict[plug_key] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor", "sensor"])
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
            description_placeholders={"devices": "\n".join(device_lines), **name_placeholders},
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
                return await self.async_step_estimated_loads()

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

    async def async_step_estimated_loads(self, user_input=None):
        """Devices with a control entity but no energy sensor at all — see the setup-flow
        docstring on this same step name for the full rationale."""
        entry_data = self._config_entry.data
        existing_names = entry_data.get(CONF_DEFERRABLE_LOAD_EST_NAMES, [])
        existing_control = entry_data.get(CONF_DEFERRABLE_LOAD_EST_CONTROL, [])
        existing_kw = entry_data.get(CONF_DEFERRABLE_LOAD_EST_KW, [])
        existing_auto = entry_data.get(CONF_DEFERRABLE_LOAD_EST_AUTO, [])
        existing_load_power = entry_data.get(CONF_LOAD_POWER_SENSOR, "")

        errors: dict[str, str] = {}
        if user_input is not None:
            est_names = [
                str(user_input.get(f"est_name_{i}", "") or "").strip()
                for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS)
            ]
            est_controls = [
                str(user_input.get(f"est_control_{i}", "") or "")
                for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS)
            ]
            # Same footgun as the setup-flow step: a slot with only a name or only a
            # control entity is silently inert (_ensure_load_estimators skips it). Caught
            # a real instance of this on 2026-08-06 — Daikin AC had control/kw/auto all
            # configured with a blank name, so the slot never activated.
            if any(bool(n) != bool(c) for n, c in zip(est_names, est_controls)):
                errors["base"] = "estimated_load_name_control_mismatch"
            # Same cross-step duplicate check as the setup flow — see its comment for the
            # real incident this catches.
            declared_names = {
                str(n).strip().lower()
                for n in self._sensor_data.get(CONF_DEFERRABLE_LOAD_DUMMY_NAMES, [])
                if n
            }
            if not errors and any(
                n.strip().lower() in declared_names for n in est_names if n
            ):
                errors["base"] = "estimated_load_duplicate_declared"
            if not errors:
                self._sensor_data[CONF_DEFERRABLE_LOAD_EST_NAMES] = est_names
                self._sensor_data[CONF_DEFERRABLE_LOAD_EST_CONTROL] = est_controls
                self._sensor_data[CONF_DEFERRABLE_LOAD_EST_KW] = [
                    float(user_input.get(f"est_kw_{i}", 1.0) or 1.0)
                    for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS)
                ]
                self._sensor_data[CONF_DEFERRABLE_LOAD_EST_AUTO] = [
                    bool(user_input.get(f"est_auto_{i}", False))
                    for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS)
                ]
                self._sensor_data[CONF_LOAD_POWER_SENSOR] = str(
                    user_input.get("load_power_sensor", "") or ""
                )
                return await self.async_step_current_plan()

        schema_dict = {}
        for i in range(DEFERRABLE_LOAD_ESTIMATED_SLOTS):
            schema_dict[vol.Optional(
                f"est_name_{i}", default=existing_names[i] if i < len(existing_names) else "",
            )] = selector.TextSelector()
            prev_control = existing_control[i] if i < len(existing_control) else ""
            control_key = (
                vol.Optional(f"est_control_{i}", default=prev_control)
                if prev_control else vol.Optional(f"est_control_{i}")
            )
            schema_dict[control_key] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "climate"])
            )
            schema_dict[vol.Optional(
                f"est_kw_{i}", default=existing_kw[i] if i < len(existing_kw) else 1.0,
            )] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.05, max=20.0, step=0.05,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[vol.Optional(
                f"est_auto_{i}", default=existing_auto[i] if i < len(existing_auto) else False,
            )] = selector.BooleanSelector()
        load_power_key = (
            vol.Optional("load_power_sensor", default=existing_load_power)
            if existing_load_power else vol.Optional("load_power_sensor")
        )
        schema_dict[load_power_key] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )

        return self.async_show_form(
            step_id="estimated_loads",
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
