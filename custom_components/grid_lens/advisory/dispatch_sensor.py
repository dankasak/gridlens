"""Advisory dispatch sensor — publishes the planned action + SOC trajectory (read-only)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
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


class _AdvisoryTileSensorBase(CoordinatorEntity, SensorEntity):
    """Shared plumbing for the small single-value sensors that back the dashboard's
    native tile cards — split out from AdvisoryDispatchSensor's attribute bag so each
    value can bind to its own `type: tile` card instead of a Jinja template."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AdvisoryCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_advisory_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Grid Lens",
        }

    @property
    def _attributes(self) -> dict:
        return (self.coordinator.data or {}).get("attributes") or {}


class AdvisoryNextActionSensor(_AdvisoryTileSensorBase):
    """Same state as AdvisoryDispatchSensor, exposed separately so it can back a
    tile card without pulling the full trajectory payload along for the ride."""

    _attr_name = "Next Action"
    _attr_icon = "mdi:battery-arrow-up-outline"

    def __init__(self, coordinator: AdvisoryCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "next_action")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        if data.get("status") != "ok":
            return data.get("status", "unknown")
        return data.get("next_action", "unknown")

    @property
    def extra_state_attributes(self) -> dict:
        power_w = (self.coordinator.data or {}).get("next_power_w")
        return {"next_power_w": power_w} if power_w is not None else {}


class AdvisorySocNowSensor(_AdvisoryTileSensorBase):
    _attr_name = "SOC Now"
    _attr_icon = "mdi:battery-50"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: AdvisoryCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "soc_now")

    @property
    def native_value(self) -> float | None:
        if (self.coordinator.data or {}).get("status") != "ok":
            return None
        return self._attributes.get("initial_soc_percent")


class AdvisoryPlannedEndSocSensor(_AdvisoryTileSensorBase):
    _attr_name = "Planned End SOC"
    _attr_icon = "mdi:battery-charging-70"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: AdvisoryCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "planned_end_soc")

    @property
    def native_value(self) -> float | None:
        if (self.coordinator.data or {}).get("status") != "ok":
            return None
        return self._attributes.get("final_soc_percent")


class AdvisoryNetCostSensor(_AdvisoryTileSensorBase):
    _attr_name = "Plan Net Cost"
    _attr_icon = "mdi:cash"
    _attr_native_unit_of_measurement = "$"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: AdvisoryCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "net_cost")

    @property
    def native_value(self) -> float | None:
        if (self.coordinator.data or {}).get("status") != "ok":
            return None
        return self._attributes.get("net_cost")


def build_advisory_sensors(coordinator: AdvisoryCoordinator, entry: ConfigEntry) -> list:
    return [
        AdvisoryDispatchSensor(coordinator, entry),
        AdvisoryNextActionSensor(coordinator, entry),
        AdvisorySocNowSensor(coordinator, entry),
        AdvisoryPlannedEndSocSensor(coordinator, entry),
        AdvisoryNetCostSensor(coordinator, entry),
    ]
