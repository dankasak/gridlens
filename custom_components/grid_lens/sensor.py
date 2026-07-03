"""Sensor platform for electricity plan comparison."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PLANS, METRICS, METRIC_INFO, PLAN_NAMES
from .plan_sensors import PlanMetricSensor

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the electricity plan comparison sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Original comparison sensors
    sensors = [
        AmberCostSensor(coordinator, entry),
        BestAlternativePlanSensor(coordinator, entry),
        PotentialSavingsSensor(coordinator, entry),
    ]

    # Add plan metric sensors (36 sensors: 9 metrics × 4 plans)
    for plan_id in PLANS:
        for metric in METRICS:
            sensor = PlanMetricSensor(coordinator, entry, plan_id, metric)
            sensors.append(sensor)
    
    _LOGGER.warning(f"Setting up {len(sensors)} sensors (3 comparison + {len(PLANS) * len(METRICS)} plan metrics)")

    async_add_entities(sensors)


class GridLensSensorBase(CoordinatorEntity, SensorEntity):
    """Base class for electricity plan sensors."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_has_entity_name = True

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Custom Integration",
            "model": "Plan Analyzer",
        }


class AmberCostSensor(GridLensSensorBase):
    """Sensor showing current Amber Electric cost."""

    _attr_name = "Amber Monthly Cost"
    _attr_native_unit_of_measurement = "$"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-usd"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._entry.entry_id}_amber_cost"

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self.coordinator.data or "amber_total" not in self.coordinator.data:
            return None
        return round(self.coordinator.data["amber_total"], 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if not self.coordinator.data:
            return {}
        
        attrs = {
            "energy_cost": self.coordinator.data.get("amber_actual_cost", 0),
            "subscription_fee": self.coordinator.data.get("amber_monthly_fee", 25.00),
            "calculation_days": self.coordinator.data.get("usage_days", 0),
            "last_updated": self.coordinator.data.get("calculation_date"),
            # Expose sensor configuration for dashboard
            "energy_sensor": self._entry.data.get("energy_sensor"),
            "solar_sensor": self._entry.data.get("solar_sensor"),
            "export_sensor": self._entry.data.get("grid_export_sensor"),
            "import_price_sensor": self._entry.data.get("import_price_sensor"),
            "export_price_sensor": self._entry.data.get("export_price_sensor"),
        }
        
        # Add status message if waiting for data
        if self.coordinator.data.get("status") == "waiting_for_data":
            attrs["status"] = self.coordinator.data.get("message", "Waiting for data")
        
        return attrs


class BestAlternativePlanSensor(GridLensSensorBase):
    """Sensor showing the best alternative plan."""

    _attr_name = "Best Alternative Plan"
    _attr_icon = "mdi:lightning-bolt"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._entry.entry_id}_best_plan"

    @property
    def native_value(self) -> str | None:
        """Return the best alternative plan name."""
        if not self.coordinator.data or "alternative_plans" not in self.coordinator.data:
            return None
        
        # Check if waiting for data
        if self.coordinator.data.get("status") == "waiting_for_data":
            return "Waiting for data"
        
        plans = self.coordinator.data["alternative_plans"]
        if not plans:
            return None
        
        # Find cheapest plan
        best_plan = min(plans.items(), key=lambda x: x[1])
        return best_plan[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all plan costs for comparison."""
        if not self.coordinator.data or "alternative_plans" not in self.coordinator.data:
            return {}
        
        # If waiting for data, show helpful message
        if self.coordinator.data.get("status") == "waiting_for_data":
            return {
                "status": self.coordinator.data.get("message", "Waiting for data"),
                "info": "The integration needs at least 24 hours of energy usage data to perform calculations."
            }
        
        plans = self.coordinator.data["alternative_plans"]
        amber_total = self.coordinator.data.get("amber_total", 0)
        
        attributes = {}
        for plan_name, cost in plans.items():
            attributes[plan_name] = {
                "monthly_cost": round(cost, 2),
                "vs_amber": round(cost - amber_total, 2),
            }
        
        return attributes


class PotentialSavingsSensor(GridLensSensorBase):
    """Sensor showing potential monthly savings."""

    _attr_name = "Potential Monthly Savings"
    _attr_native_unit_of_measurement = "$"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:piggy-bank"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._entry.entry_id}_savings"

    @property
    def native_value(self) -> float | None:
        """Return potential savings (negative means Amber is cheaper)."""
        if not self.coordinator.data or "alternative_plans" not in self.coordinator.data:
            return None
        
        # If waiting for data, return 0
        if self.coordinator.data.get("status") == "waiting_for_data":
            return 0
        
        plans = self.coordinator.data["alternative_plans"]
        amber_total = self.coordinator.data.get("amber_total", 0)
        
        if not plans:
            return None
        
        # Find best alternative
        best_cost = min(plans.values())
        
        # Negative value means Amber is cheaper
        savings = amber_total - best_cost
        return round(savings, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional information."""
        if not self.coordinator.data:
            return {}
        
        # If waiting for data, show helpful message
        if self.coordinator.data.get("status") == "waiting_for_data":
            return {
                "status": self.coordinator.data.get("message", "Waiting for data"),
                "recommendation": "Integration is collecting usage data. Check back in 24-48 hours."
            }
        
        plans = self.coordinator.data.get("alternative_plans", {})
        amber_total = self.coordinator.data.get("amber_total", 0)
        
        if not plans:
            return {}
        
        best_plan = min(plans.items(), key=lambda x: x[1])
        
        return {
            "best_alternative": best_plan[0],
            "best_alternative_cost": round(best_plan[1], 2),
            "current_amber_cost": round(amber_total, 2),
            "recommendation": (
                "Stay with Amber" if amber_total <= best_plan[1]
                else f"Consider switching to {best_plan[0]}"
            ),
        }
