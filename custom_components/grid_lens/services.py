"""Service handler for calculating plan data."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    PLANS,
    PLAN_AMBER,
    PLAN_OVO,
    PLAN_EA,
    PLAN_AGL,
    METRICS,
    METRIC_BATTERY_CHARGE,
    METRIC_BATTERY_DISCHARGE,
    METRIC_SOLAR_PRODUCTION,
    METRIC_GRID_IMPORT,
    METRIC_GRID_EXPORT,
    METRIC_BUY_PRICE,
    METRIC_SELL_PRICE,
    METRIC_HOURLY_COST,
    METRIC_OPTIMIZATION_NOTES,
)
from .plan_calculator import PlanCalculator

_LOGGER = logging.getLogger(__name__)

SERVICE_CALCULATE_PERIOD = "calculate_period"


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
    
    # Register service
    hass.services.async_register(
        DOMAIN,
        SERVICE_CALCULATE_PERIOD,
        handle_calculate_period,
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


async def _populate_current_plan_actual(
    hass: HomeAssistant,
    calculator: PlanCalculator,
    plan_id: str,
    start_date: datetime,
    end_date: datetime,
    usage_data: list,
    solar_data: list,
    export_data: list,
) -> None:
    """Populate current-plan sensors with actual behavior."""
    
    # Group data by hour
    hourly_data = {}
    
    # Grid import
    for record in usage_data:
        hour = record["timestamp"].replace(minute=0, second=0, microsecond=0)
        if hour not in hourly_data:
            hourly_data[hour] = {
                "timestamp": hour,
                "grid_import": 0,
                "solar": 0,
                "grid_export": 0,
                "battery_charge": 0,
                "battery_discharge": 0,
                "buy_price": 0,
                "sell_price": 0,
            }
        hourly_data[hour]["grid_import"] += record["value"]
    
    # Solar
    for record in solar_data:
        hour = record["timestamp"].replace(minute=0, second=0, microsecond=0)
        if hour in hourly_data:
            hourly_data[hour]["solar"] += record["value"]
    
    # Export
    for record in export_data:
        hour = record["timestamp"].replace(minute=0, second=0, microsecond=0)
        if hour in hourly_data:
            hourly_data[hour]["grid_export"] += record["value"]
    
    # Battery (actual behavior)
    if calculator.has_battery and calculator.battery_power_sensor:
        battery_data = await calculator._get_battery_behavior(start_date, end_date)
        for record in battery_data:
            hour = record["timestamp"].replace(minute=0, second=0, microsecond=0)
            if hour in hourly_data:
                hourly_data[hour]["battery_charge"] += record["charge_kwh"]
                hourly_data[hour]["battery_discharge"] += record["discharge_kwh"]
    
    # Legacy: estimate prices based on time of day
    from datetime import datetime as _dt
    
    # Update sensors for each hour
    for hour, data in sorted(hourly_data.items()):
        # Estimate prices based on time of day
        buy_price = current_plan.get_import_rate(hour)
        sell_price = current_plan.get_export_rate(hour)

        data["buy_price"] = buy_price
        data["sell_price"] = sell_price

        hourly_cost = (data["grid_import"] * buy_price) - (data["grid_export"] * sell_price)

        await _update_plan_sensors(hass, plan_id, hour, data, hourly_cost, "Actual current-plan behavior")


async def _populate_plan_optimized(
    hass: HomeAssistant,
    calculator: PlanCalculator,
    plan_id: str,
    plan,
    start_date: datetime,
    end_date: datetime,
    usage_data: list,
    solar_data: list,
    export_data: list,
) -> None:
    """Populate plan sensors with optimized behavior."""
    
    # Run optimization
    from .battery_optimizer import BatteryOptimizer
    
    # Build hourly profiles
    hourly_profiles = []
    current = start_date
    
    while current < end_date:
        next_hour = current + timedelta(hours=1)
        
        # Sum usage/solar/export for this hour
        hour_usage = sum(r["value"] for r in usage_data if current <= r["timestamp"] < next_hour)
        hour_solar = sum(r["value"] for r in solar_data if current <= r["timestamp"] < next_hour)
        hour_export = sum(r["value"] for r in export_data if current <= r["timestamp"] < next_hour)
        
        # Get rates from plan
        import_rate = plan.get_import_rate(current)
        export_rate = plan.get_export_rate(current)
        
        hourly_profiles.append({
            "timestamp": current,
            "usage_kwh": hour_usage,
            "solar_kwh": hour_solar,
            "export_kwh": hour_export,
            "import_rate": import_rate,
            "export_rate": export_rate,
        })
        
        current = next_hour
    
    # Optimize battery if available
    if calculator.has_battery:
        optimizer = BatteryOptimizer(
            capacity_kwh=calculator.battery_capacity,
            max_charge_rate_kw=calculator.battery_max_charge_rate,
            max_discharge_rate_kw=calculator.battery_max_discharge_rate,
            efficiency_percent=calculator.battery_efficiency,
            min_soc_percent=calculator.battery_min_soc,
            max_soc_percent=calculator.battery_max_soc,
        )

        optimization_result = optimizer.optimize_hourly_schedule(
            solar_profile=[p["solar_kwh"] for p in hourly_profiles],
            load_profile=[p["usage_kwh"] for p in hourly_profiles],
            import_rates=[p["import_rate"] for p in hourly_profiles],
            export_rates=[p["export_rate"] for p in hourly_profiles],
        )
        schedule = optimization_result.get("schedule", [])
    else:
        schedule = []
        for profile in hourly_profiles:
            schedule.append({
                "timestamp": profile["timestamp"],
                "import_kwh": profile["usage_kwh"] - profile["solar_kwh"] + profile["export_kwh"],
                "export_kwh": profile["export_kwh"],
                "charge_kwh": 0,
                "discharge_kwh": 0,
                "import_cost": (profile["usage_kwh"] - profile["solar_kwh"] + profile["export_kwh"]) * profile["import_rate"],
                "export_credit": profile["export_kwh"] * profile["export_rate"],
            })
    
    # Update sensors for each hour
    strategy = plan.describe_strategy()
    for hour_data in schedule:
        timestamp = hour_data["timestamp"]
        
        data = {
            "grid_import": hour_data.get("import_kwh", 0),
            "grid_export": hour_data.get("export_kwh", 0),
            "battery_charge": hour_data.get("charge_kwh", 0),
            "battery_discharge": hour_data.get("discharge_kwh", 0),
            "solar": next((p["solar_kwh"] for p in hourly_profiles if p["timestamp"] == timestamp), 0),
            "buy_price": next((p["import_rate"] for p in hourly_profiles if p["timestamp"] == timestamp), 0),
            "sell_price": next((p["export_rate"] for p in hourly_profiles if p["timestamp"] == timestamp), 0),
        }
        
        hourly_cost = hour_data.get("import_cost", 0) - hour_data.get("export_credit", 0)
        
        await _update_plan_sensors(hass, plan_id, timestamp, data, hourly_cost, strategy)


async def _update_plan_sensors(
    hass: HomeAssistant,
    plan_id: str,
    timestamp: datetime,
    data: dict,
    hourly_cost: float,
    notes: str,
) -> None:
    """Update all sensors for a plan at a specific timestamp."""
    
    sensor_updates = {
        METRIC_BATTERY_CHARGE: data.get("battery_charge", 0),
        METRIC_BATTERY_DISCHARGE: data.get("battery_discharge", 0),
        METRIC_SOLAR_PRODUCTION: data.get("solar", 0),
        METRIC_GRID_IMPORT: data.get("grid_import", 0),
        METRIC_GRID_EXPORT: data.get("grid_export", 0),
        METRIC_BUY_PRICE: data.get("buy_price", 0),
        METRIC_SELL_PRICE: data.get("sell_price", 0),
        METRIC_HOURLY_COST: hourly_cost,
        METRIC_OPTIMIZATION_NOTES: notes,
    }
    
    for metric, value in sensor_updates.items():
        entity_id = f"sensor.{plan_id}_{metric}"
        
        # For now, just update state
        # TODO: Inject into recorder for historical data
        hass.states.async_set(
            entity_id,
            value,
            {
                "timestamp": timestamp.isoformat(),
                "hour_of_day": timestamp.hour,
                "day_of_week": timestamp.strftime("%A"),
                "friendly_name": f"{plan_id.upper()} {metric.replace('_', ' ').title()}",
            }
        )
