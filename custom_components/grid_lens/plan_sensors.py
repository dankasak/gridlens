"""Plan metric sensors for electricity plan comparison."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    PLANS,
    METRICS,
    METRIC_INFO,
    PLAN_NAMES,
)

_LOGGER = logging.getLogger(__name__)


class PlanMetricSensor(CoordinatorEntity, SensorEntity):
    """Sensor for a specific metric of a plan, backed by the coordinator."""

    def __init__(self, coordinator, entry: ConfigEntry, plan_id: str, metric: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._plan_id = plan_id
        self._metric = metric

        metric_info = METRIC_INFO[metric]
        plan_name = PLAN_NAMES[plan_id]

        self._attr_name = f"{plan_name} {metric_info['name']}"
        self._attr_unique_id = f"{entry.entry_id}_{plan_id}_{metric}"
        self._attr_native_unit_of_measurement = metric_info["unit"]

        if metric_info["device_class"]:
            self._attr_device_class = SensorDeviceClass(metric_info["device_class"])
        if metric_info["state_class"]:
            self._attr_state_class = SensorStateClass(metric_info["state_class"])

        self._attr_icon = self._get_icon(metric)

    def _get_icon(self, metric: str) -> str:
        """Get icon for metric."""
        icons = {
            "battery_charge": "mdi:battery-charging",
            "battery_discharge": "mdi:battery-minus",
            "solar_production": "mdi:solar-power",
            "grid_import": "mdi:transmission-tower-import",
            "grid_export": "mdi:transmission-tower-export",
            "buy_price": "mdi:cash-minus",
            "sell_price": "mdi:cash-plus",
            "hourly_cost": "mdi:currency-usd",
            "optimization_notes": "mdi:text-box",
        }
        return icons.get(metric, "mdi:chart-line")

    @property
    def entity_id(self) -> str:
        """Return the entity ID."""
        return f"sensor.{self._plan_id}_{self._metric}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value from coordinator data."""
        if not self.coordinator.data:
            return None
        plan_metrics = self.coordinator.data.get("plan_metrics", {})
        return plan_metrics.get(self._plan_id, {}).get(self._metric)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if not self.coordinator.data:
            return {}
        return {
            "plan_id": self._plan_id,
            "metric": self._metric,
            "usage_days": self.coordinator.data.get("usage_days", 0),
            "calculation_date": self.coordinator.data.get("calculation_date", ""),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up plan metric sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    sensors = []
    for plan_id in PLANS:
        for metric in METRICS:
            sensor = PlanMetricSensor(coordinator, entry, plan_id, metric)
            sensors.append(sensor)

    async_add_entities(sensors, True)
    _LOGGER.info(f"Created {len(sensors)} plan metric sensors")
