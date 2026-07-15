"""Advisory dispatch sensor — publishes the planned action + SOC trajectory (read-only)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from .coordinator import AdvisoryCoordinator


class AdvisoryDispatchSensor(CoordinatorEntity, SensorEntity):
    """State = next planned battery action; attributes carry the full SOC trajectory."""

    _attr_has_entity_name = True
    _attr_name = "Planned Dispatch"
    _attr_icon = "mdi:battery-clock"

    def __init__(self, coordinator: AdvisoryCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_planned_dispatch"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        if data.get("status") != "ok":
            return data.get("status", "unknown")
        return data.get("next_action", "unknown")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        attrs: dict = {"status": data.get("status")}
        if data.get("reason"):
            attrs["reason"] = data["reason"]
        if data.get("status") == "ok":
            attrs["next_power_w"] = data.get("next_power_w")
            attrs["plan_name"] = data.get("plan_name")
            attrs["sources"] = data.get("sources")
            if data.get("restored"):
                attrs["restored"] = True  # last good plan, shown until a live one lands
            if data.get("pending_reason"):
                attrs["pending_reason"] = data["pending_reason"]  # why live plan is pending
            attrs.update(data.get("attributes", {}))  # generated_at, trajectory, soc, cost…
        return attrs


def build_advisory_sensors(coordinator: AdvisoryCoordinator, entry: ConfigEntry) -> list:
    return [AdvisoryDispatchSensor(coordinator, entry)]
