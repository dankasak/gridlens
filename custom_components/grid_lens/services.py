"""Service handler for calculating plan data."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .plan_calculator import PlanCalculator

_LOGGER = logging.getLogger(__name__)

SERVICE_CALCULATE_PERIOD = "calculate_period"
SERVICE_SET_DEFERRABLE_SCHEDULE = "set_deferrable_schedule"
SERVICE_CLEAR_DEFERRABLE_SCHEDULE = "clear_deferrable_schedule"
SERVICE_SET_CHARGE_TARGET = "set_charge_target"
SERVICE_CLEAR_CHARGE_TARGET = "clear_charge_target"


async def async_setup_services(hass: HomeAssistant, entry) -> None:
    """Set up services for the integration."""
    
    async def handle_calculate_period(call: ServiceCall) -> None:
        """Handle the calculate_period service call."""
        start_date_str = call.data.get("start_date")
        end_date_str = call.data.get("end_date")
        
        if not start_date_str or not end_date_str:
            raise HomeAssistantError("start_date and end_date are required")
        
        # Parse dates
        try:
            start_date = dt_util.parse_datetime(start_date_str)
            end_date = dt_util.parse_datetime(end_date_str)
            
            if not start_date or not end_date:
                raise ValueError("Invalid date format")
            
            # Make timezone aware
            if start_date.tzinfo is None:
                start_date = dt_util.as_local(start_date)
            if end_date.tzinfo is None:
                end_date = dt_util.as_local(end_date)
                
        except Exception as e:
            raise HomeAssistantError(f"Invalid date format: {e}")
        
        if start_date >= end_date:
            raise HomeAssistantError("start_date must be before end_date")
        
        _LOGGER.warning(f"Calculating plan data from {start_date} to {end_date}")
        
        # Get coordinator from the stored entry
        coordinator = hass.data[DOMAIN][entry.entry_id]
        
        # Get or create calculator
        if not coordinator.calculator:
            from .plan_calculator import PlanCalculator
            _LOGGER.warning("Creating new calculator instance for service call")
            coordinator.calculator = PlanCalculator(hass, entry)
        
        calculator = coordinator.calculator
        
        # Calculate for each plan
        await _calculate_and_populate_sensors(
            hass, calculator, start_date, end_date
        )
        
        _LOGGER.warning("Plan calculation completed successfully")
    
    async def _write_schedule(sensor_id: str | None, days) -> None:
        """Shared validate-and-persist for the two schedule services. `sensor_id` is
        the device's configured energy sensor (the canonical device key, same as the
        boost-override store); `days` is 7 rows (Monday first) of 24 hour values,
        1 = allowed to run, or None to clear back to the static config spec."""
        store = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_deferrable_schedules")
        if store is None:
            raise HomeAssistantError("Grid Lens schedule store is not available")
        if not sensor_id:
            raise HomeAssistantError("sensor_id is required")
        configured = entry.data.get("deferrable_load_sensors", []) or []
        if sensor_id not in configured:
            raise HomeAssistantError(
                f"{sensor_id} is not a configured deferrable load "
                f"(configured: {', '.join(configured) or 'none'})"
            )
        try:
            await store.async_set(sensor_id, days)
        except ValueError as err:
            raise HomeAssistantError(f"Invalid schedule grid: {err}")
        _LOGGER.warning(
            "Deferrable weekly schedule %s for %s",
            "saved" if days is not None else "cleared", sensor_id,
        )
        # Rewrite the coordinator entities' state now (no recalculation) so the cost
        # sensor's `deferrable_loads` attribute — which the schedule card reads —
        # reflects this save immediately. Without this the attribute snapshot stays
        # stale until the next (manual) coordinator refresh and the card would appear
        # to lose the save on its next repaint.
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator is not None:
            coordinator.async_update_listeners()

    async def handle_set_deferrable_schedule(call: ServiceCall) -> None:
        """Persist a deferrable device's weekly (7x24 per-weekday) availability grid —
        called by the schedule editor card."""
        await _write_schedule(call.data.get("sensor_id"), call.data.get("days"))

    async def handle_clear_deferrable_schedule(call: ServiceCall) -> None:
        """Remove a device's stored weekly schedule — it reverts to the static
        availability-hours spec from the integration config."""
        await _write_schedule(call.data.get("sensor_id"), None)

    async def _write_charge_target(sensor_id: str | None, percent: float, target_iso: str) -> None:
        """Shared validate-and-persist for the two charge-target services. `sensor_id`
        is the device's configured energy sensor (same canonical key as the schedule/
        boost stores); mirrors number.py/datetime.py's own writes through the same
        store — this is just an automation-friendly alternative to setting those two
        entities by hand. `percent<=0` or a blank `target_iso` clears the target
        (see charge_target.write_target)."""
        store = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_charge_targets")
        if store is None:
            raise HomeAssistantError("Grid Lens charge-target store is not available")
        if not sensor_id:
            raise HomeAssistantError("sensor_id is required")
        configured = entry.data.get("deferrable_load_sensors", []) or []
        if sensor_id not in configured:
            raise HomeAssistantError(
                f"{sensor_id} is not a configured deferrable load "
                f"(configured: {', '.join(configured) or 'none'})"
            )
        soc_sensors = entry.data.get("deferrable_load_soc_sensors", []) or []
        soc_capacities = entry.data.get("deferrable_load_soc_capacity_kwh", []) or []
        idx = configured.index(sensor_id)
        has_soc = (
            idx < len(soc_sensors) and soc_sensors[idx]
            and idx < len(soc_capacities) and soc_capacities[idx]
        )
        if percent > 0 and not has_soc:
            raise HomeAssistantError(
                f"{sensor_id} has no SOC sensor/capacity configured — a charge target "
                "needs live SOC tracking to know how much energy is actually needed "
                "(see the device's reconfigure options)"
            )
        await store.async_set(sensor_id, percent, target_iso)
        _LOGGER.warning(
            "Charge target %s for %s%s",
            "saved" if percent > 0 and target_iso else "cleared", sensor_id,
            f": {percent:g}% by {target_iso}" if percent > 0 and target_iso else "",
        )

    async def handle_set_charge_target(call: ServiceCall) -> None:
        """Set an ad-hoc one-off charge target — e.g. 100% by 7am Saturday before a
        trip. The equivalent of setting the device's charge-target percent + datetime
        entities by hand; auto-clears once reached or once the deadline passes."""
        target_dt = call.data.get("target_datetime")
        try:
            parsed = dt_util.parse_datetime(target_dt) if target_dt else None
        except Exception as e:
            raise HomeAssistantError(f"Invalid target_datetime: {e}")
        if parsed is None:
            raise HomeAssistantError("target_datetime is required and must be a valid datetime")
        if parsed.tzinfo is None:
            parsed = dt_util.as_local(parsed)
        percent = call.data.get("target_percent")
        if percent is None:
            raise HomeAssistantError("target_percent is required")
        await _write_charge_target(call.data.get("sensor_id"), float(percent), dt_util.as_utc(parsed).isoformat())

    async def handle_clear_charge_target(call: ServiceCall) -> None:
        """Cancel a device's ad-hoc charge target, if one is set."""
        await _write_charge_target(call.data.get("sensor_id"), 0.0, "")

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_CALCULATE_PERIOD,
        handle_calculate_period,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_DEFERRABLE_SCHEDULE, handle_set_deferrable_schedule
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_DEFERRABLE_SCHEDULE, handle_clear_deferrable_schedule
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CHARGE_TARGET, handle_set_charge_target
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_CHARGE_TARGET, handle_clear_charge_target
    )

    _LOGGER.info(f"Registered service: {DOMAIN}.{SERVICE_CALCULATE_PERIOD}")


async def _calculate_and_populate_sensors(
    hass: HomeAssistant,
    calculator: PlanCalculator,
    start_date: datetime,
    end_date: datetime,
) -> None:
    """Calculate and populate all plan sensors for the given period."""
    
    # Get base data (usage, solar, export)
    usage_data = await calculator._get_usage_data(start_date, end_date)
    solar_data = await calculator._get_usage_data(start_date, end_date, calculator.solar_sensor) if calculator.solar_sensor else []
    export_data = await calculator._get_usage_data(start_date, end_date, calculator.grid_export_sensor) if calculator.grid_export_sensor else []
    
    if not usage_data:
        raise HomeAssistantError("No usage data found for the specified period")
    
    _LOGGER.warning(f"Loaded {len(usage_data)} usage records, {len(solar_data)} solar records, {len(export_data)} export records")
    
    _LOGGER.warning("calculate_period service is deprecated — use the Grid Lens dashboard instead")
    raise HomeAssistantError("calculate_period service is deprecated. Use the Grid Lens dashboard for plan comparisons.")
