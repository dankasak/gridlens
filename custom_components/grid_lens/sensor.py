"""Sensor platform for electricity plan comparison."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, PLANS, METRICS, METRIC_INFO, PLAN_NAMES,
    CONF_DEFERRABLE_LOAD_SENSORS, CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_SWITCHES, CONF_DEFERRABLE_LOAD_SOC_SENSORS,
    CONF_DEFERRABLE_LOAD_SETPOINT, CONF_DEFERRABLE_LOAD_PHASES,
    CONF_DEFERRABLE_LOAD_VOLTAGE, CONF_DEFERRABLE_LOAD_MIN_CURRENT,
    CONF_DEFERRABLE_LOAD_PLUG_SENSOR, DEFAULT_SUPPLY_VOLTAGE,
    DEFAULT_MIN_CHARGE_CURRENT_A,
)
from .schedule_grid import week_from_hours
from .entity_lookup import resolve_power_sensor, resolve_device_name
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
        CurrentPlanCostSensor(coordinator, entry),
        BestAlternativePlanSensor(coordinator, entry),
        PotentialSavingsSensor(coordinator, entry),
    ]

    # Add plan metric sensors (36 sensors: 9 metrics × 4 plans)
    for plan_id in PLANS:
        for metric in METRICS:
            sensor = PlanMetricSensor(coordinator, entry, plan_id, metric)
            sensors.append(sensor)
    
    _LOGGER.warning(f"Setting up {len(sensors)} sensors (3 comparison + {len(PLANS) * len(METRICS)} plan metrics)")

    # Advisory-mode dispatch sensor (independent coordinator; read-only).
    advisory = hass.data[DOMAIN].get(f"{entry.entry_id}_advisory")
    if advisory is not None:
        try:
            from .advisory.dispatch_sensor import build_advisory_sensors
            sensors.extend(build_advisory_sensors(advisory, entry))
        except Exception as _adv_err:  # noqa: BLE001
            _LOGGER.warning("Advisory sensor setup skipped: %s", _adv_err)

    # Synthetic energy sensors for "estimated" deferrable loads (a device with a control
    # entity but no real energy feedback — see load_estimation.py). One LoadEstimator per
    # configured slot was already constructed in __init__.py._ensure_load_estimators;
    # this just wraps each in the entity that displays it.
    estimators = hass.data[DOMAIN].get(f"{entry.entry_id}_load_estimators", {})
    for estimator in estimators.values():
        sensors.append(GridLensEstimatedEnergySensor(estimator, entry))

    # Synthetic live-power sensors — one per device (estimated-load OR sensor-backed)
    # that ended up with no real power_entity resolvable by entity_lookup, so the Power
    # Flow card (which drops any node without one) can still show it. See
    # __init__.py._ensure_load_estimators's "step 2" docstring.
    power_estimators = hass.data[DOMAIN].get(f"{entry.entry_id}_power_estimators", {})
    seen_power_ids: set = set()
    for estimator in power_estimators.values():
        if id(estimator) in seen_power_ids:
            continue  # an estimated-load estimator also appears in `estimators` above
        seen_power_ids.add(id(estimator))
        sensors.append(GridLensEstimatedPowerSensor(estimator, entry))

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


class GridLensEstimatedEnergySensor(SensorEntity):
    """Synthetic ``total_increasing`` energy sensor for an "estimated" deferrable load —
    a device with a control entity but no real energy feedback path (e.g. an IR-blaster
    aircon). Displays LoadEstimator's own running kWh total; the estimator (not this
    entity) owns and persists the number via its own Store — same "manager persists,
    entity just displays it" split as the battery-control switch (see
    control/manager.py's ControlManager docstring for why RestoreEntity was rejected
    there: a reload's deadman timing would otherwise get restored as if it were genuine
    state). This entity's own entity_id is what gets spliced into
    CONF_DEFERRABLE_LOAD_SENSORS (__init__.py._ensure_load_estimators), so every
    other deferrable-load feature treats it exactly like a device with a real meter.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(self, estimator, entry: ConfigEntry) -> None:
        self._estimator = estimator
        self._entry = entry
        self._attr_name = f"{estimator.name} Estimated Consumption"
        self._attr_unique_id = estimator.unique_id

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Custom Integration",
            "model": "Plan Analyzer",
        }

    @property
    def native_value(self) -> float:
        return round(self._estimator.running_kwh, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._estimator.status()


class GridLensEstimatedPowerSensor(SensorEntity):
    """Synthetic live-power (W) sensor for a deferrable device that has no power_entity
    entity_lookup.resolve_power_sensor() can find — either an "estimated load" (no real
    sensor of any kind) or an ordinary sensor-backed device whose only telemetry is a
    cumulative energy counter (e.g. an ECHONET Lite aircon: device_class energy, not
    power). Without this, such a device is fully schedulable/controllable everywhere else
    in Grid Lens but invisible on the Power Flow card, which silently drops any node with
    no power_entity. Displays LoadEstimator.current_power_w — the estimator's own running
    estimate while the control entity reads "on", 0 otherwise; see
    __init__.py._ensure_load_estimators's "step 2" docstring for how a device qualifies.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:flash"

    def __init__(self, estimator, entry: ConfigEntry) -> None:
        self._estimator = estimator
        self._entry = entry
        self._attr_name = f"{estimator.name} Estimated Power"
        # Must exactly match the unique_id __init__.py._ensure_load_estimators already
        # registered via entity_registry.async_get_or_create() — that's how this entity
        # binds to the entity_id already reserved there (estimator.power_unique_id),
        # not something re-derived here.
        self._attr_unique_id = estimator.power_unique_id

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Custom Integration",
            "model": "Plan Analyzer",
        }

    @property
    def native_value(self) -> float:
        return round(self._estimator.current_power_w, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._estimator.status()


class CurrentPlanCostSensor(GridLensSensorBase):
    """Sensor showing the cost of the user's actual current plan."""

    _attr_name = "Current Plan Monthly Cost"
    _attr_native_unit_of_measurement = "$"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-usd"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._entry.entry_id}_current_plan_cost"

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self.coordinator.data or "current_plan_total" not in self.coordinator.data:
            return None
        return round(self.coordinator.data["current_plan_total"], 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if not self.coordinator.data:
            return {}

        attrs = {
            "plan_name": self.coordinator.data.get("current_plan_name"),
            "energy_cost": self.coordinator.data.get("current_plan_energy_cost", 0),
            "subscription_fee": self.coordinator.data.get("current_plan_monthly_fee", 25.00),
            "calculation_days": self.coordinator.data.get("usage_days", 0),
            "last_updated": self.coordinator.data.get("calculation_date"),
            # Expose sensor configuration for dashboard
            "energy_sensor": self._entry.data.get("energy_sensor"),
            "solar_sensor": self._entry.data.get("solar_sensor"),
            "export_sensor": self._entry.data.get("grid_export_sensor"),
            "import_price_sensor": self._entry.data.get("import_price_sensor"),
            "export_price_sensor": self._entry.data.get("export_price_sensor"),
            # Deferrable loads with their auto-discovered real-time power sensor — consumed by
            # the power-flow card so it shows each load's live consumption without hand-config.
            "deferrable_loads": self._build_deferrable_loads(),
        }

        # Add status message if waiting for data
        if self.coordinator.data.get("status") == "waiting_for_data":
            attrs["status"] = self.coordinator.data.get("message", "Waiting for data")

        return attrs

    def _build_deferrable_loads(self) -> list[dict[str, Any]]:
        """Each configured deferrable device + its auto-resolved power sensor.

        power_entity is discovered from the device behind the configured energy sensor (or the
        control switch) via entity_lookup.resolve_power_sensor — falling back to a
        LoadEstimator-backed synthetic power sensor (load_estimation.py,
        __init__.py._ensure_load_estimators's "step 2") when that finds nothing, e.g. a
        device whose only telemetry is a cumulative energy counter. Still None if neither
        is available. controllable = the device has SOME control mechanism configured — a
        switch/climate entity (type 1, on/off) OR a setpoint entity (type 2, modulating,
        MODULATING_CONTRACT.md) — since the point of this flag is "the Power Flow/Load
        Control cards may offer a control affordance for this row", true for either type.

        control_type distinguishes those two mechanisms for a card that needs to render
        them differently (a modulating device gets a live-current readout + a max-current
        ceiling instead of a plain on/off toggle): "modulating" when a setpoint entity is
        configured (CONF_DEFERRABLE_LOAD_SETPOINT[i]), "onoff" when only a switch/climate
        entity is, None for a forecast-only device with neither. A device can have BOTH a
        setpoint and a switch (the switch starts/stops delivery, the setpoint modulates
        it while running — the common non-OCPP case) — setpoint wins on control_type
        since that's the richer mechanism actually driving it (see
        control/modulating_controller.py).
        """
        data = self._entry.data
        sensors = data.get(CONF_DEFERRABLE_LOAD_SENSORS, []) or []
        max_kw = data.get(CONF_DEFERRABLE_LOAD_MAX_KW, []) or []
        switches = data.get(CONF_DEFERRABLE_LOAD_SWITCHES, []) or []
        soc_sensors = data.get(CONF_DEFERRABLE_LOAD_SOC_SENSORS, []) or []
        # Modulating ("type 2") config — every one of these is absent from any config
        # entry saved before this feature, so `.get(..., [])` + the index guards below
        # make an old entry behave identically to today (control_type stays "onoff"/None,
        # setpoint_entity/min_kw/plug_entity stay None).
        setpoints = data.get(CONF_DEFERRABLE_LOAD_SETPOINT, []) or []
        voltages = data.get(CONF_DEFERRABLE_LOAD_VOLTAGE, []) or []
        min_currents = data.get(CONF_DEFERRABLE_LOAD_MIN_CURRENT, []) or []
        phases_cfg = data.get(CONF_DEFERRABLE_LOAD_PHASES, []) or []
        plug_sensors = data.get(CONF_DEFERRABLE_LOAD_PLUG_SENSOR, []) or []
        power_estimators = self.hass.data.get(DOMAIN, {}).get(
            f"{self._entry.entry_id}_power_estimators", {}
        )
        sched_store = self.hass.data.get(DOMAIN, {}).get(
            f"{self._entry.entry_id}_deferrable_schedules"
        )
        # Every device defaults to fully-allowed (any hour) until the user paints a
        # weekly schedule on the dashboard card — that card is the only place an
        # availability window is set (see const.py's note on the retired static
        # deferrable_load_hours config field).
        default_week = week_from_hours(None)
        out: list[dict[str, Any]] = []
        for i, sensor_id in enumerate(sensors):
            sw = switches[i] if i < len(switches) else ""
            soc = soc_sensors[i] if i < len(soc_sensors) else ""
            setpoint_id = setpoints[i] if i < len(setpoints) else ""
            plug_id = plug_sensors[i] if i < len(plug_sensors) else ""
            try:
                # Include the setpoint entity as a last-resort anchor: a modulating
                # charger's number.* setpoint commonly lives on the same HA device as its
                # own power sensor (OCPP, Wallbox, Easee...), so this costs nothing for an
                # existing switch-only/forecast-only device (setpoint_id is "" there —
                # resolve_power_sensor skips falsy anchors) and gives a modulating-only
                # device (no switch) one more real shot at a live power_entity.
                power = resolve_power_sensor(self.hass, sensor_id, sw or None, setpoint_id or None)
            except Exception:  # noqa: BLE001 — discovery is best-effort, never break the sensor
                power = None
            if power is None:
                est = power_estimators.get(sensor_id)
                if est is not None:
                    power = est.power_sensor_entity_id
            # Weekly availability: the stored per-weekday grid if the user saved one on
            # the schedule card, else the fully-allowed default above — the schedule
            # card reads this to render/edit without a second discovery path.
            schedule = sched_store.cached(sensor_id) if sched_store is not None else None
            dashboard_names = getattr(self.coordinator, "energy_dashboard_names", None)

            if setpoint_id:
                control_type = "modulating"
                device_max_kw = float(max_kw[i]) if i < len(max_kw) else 0.0
                min_kw = self._modulating_min_kw(
                    setpoint_id,
                    device_max_kw,
                    voltage_cfg=float(voltages[i]) if i < len(voltages) else 0.0,
                    min_current_cfg=float(min_currents[i]) if i < len(min_currents) else 0.0,
                    phases_override=int(phases_cfg[i]) if i < len(phases_cfg) else 0,
                )
            elif sw:
                control_type = "onoff"
                min_kw = None
            else:
                control_type = None
                min_kw = None

            out.append({
                "name": resolve_device_name(
                    self.hass, sw or None, sensor_id, dashboard_names=dashboard_names
                ) or sensor_id,
                "energy_entity": sensor_id,
                "power_entity": power,
                "switch_entity": sw or None,
                # Battery/EV state-of-charge sensor, if the user configured one for this
                # device (most relevant for an EV charger) — read by the Power Flow card.
                "soc_entity": soc or None,
                "max_kw": float(max_kw[i]) if i < len(max_kw) else None,
                "controllable": bool(sw) or bool(setpoint_id),
                # --- modulating ("type 2") fields — see MODULATING_CONTRACT.md §7 ---
                "control_type": control_type,
                "setpoint_entity": setpoint_id or None,
                # Watts-floor equivalent of CONF_DEFERRABLE_LOAD_MIN_CURRENT, in kW, for a
                # card to shade the "off <-> min" no-man's-land on a modulating device's
                # live-current readout. None for a non-modulating device (the concept
                # doesn't apply).
                "min_kw": min_kw,
                "plug_entity": plug_id or None,
                # 7x24 grids (Monday first): the user-saved weekly schedule (null when
                # none saved) and the config-derived default it would revert to.
                "schedule": schedule,
                "default_schedule": default_week,
            })
        return out

    def _modulating_min_kw(
        self,
        setpoint_id: str,
        max_kw: float,
        *,
        voltage_cfg: float,
        min_current_cfg: float,
        phases_override: int,
    ) -> float:
        """CONF_DEFERRABLE_LOAD_MIN_CURRENT (A) converted to kW for display, using the
        same amps-to-watts geometry ModulatingLoadController resolves for actual control
        (control/modulating_controller.py, MODULATING_CONTRACT.md §3.2) — duplicated here
        (not imported) because this is a lightweight display-only conversion and the
        controller may not exist yet for this device (entitlement pending, setup still in
        progress) whereas this sensor must always render something.

        voltage_cfg/min_current_cfg/phases_override are the raw config values (0 = "use
        the default"), exactly like CONF_DEFERRABLE_LOAD_VOLTAGE/_MIN_CURRENT/_PHASES's
        own "0 = auto/default" contract in const.py.
        """
        voltage = voltage_cfg if voltage_cfg > 0 else DEFAULT_SUPPLY_VOLTAGE
        min_current_a = min_current_cfg if min_current_cfg > 0 else DEFAULT_MIN_CHARGE_CURRENT_A
        if phases_override:
            phases = max(1, min(3, phases_override))
        else:
            # Auto-derive from this device's own max_kw and the setpoint entity's native
            # max (A) — a 7.4 kW single-phase charger and a 22 kW three-phase one both
            # commonly advertise the same 32 A max, so only max_kw distinguishes them.
            # 32 A is the fallback native max when the setpoint entity can't be read (not
            # yet loaded, no `max`/`native_max_value` attribute) — the common OCPP/EVSE
            # single-circuit ceiling, same fallback MODULATING_CONTRACT.md §3.2 specifies.
            native_max_a = None
            state = self.hass.states.get(setpoint_id) if setpoint_id else None
            if state is not None:
                raw = state.attributes.get("max", state.attributes.get("native_max_value"))
                try:
                    if raw is not None and float(raw) > 0:
                        native_max_a = float(raw)
                except (TypeError, ValueError):
                    native_max_a = None
            if native_max_a is None:
                native_max_a = 32.0
            phases = 1
            if voltage > 0 and native_max_a > 0:
                phases = max(1, min(3, round(max_kw * 1000.0 / (native_max_a * voltage))))
        watts = min_current_a * voltage * phases
        return round(watts / 1000.0, 3)


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
        current_plan_total = self.coordinator.data.get("current_plan_total", 0)

        attributes = {}
        for plan_name, cost in plans.items():
            attributes[plan_name] = {
                "monthly_cost": round(cost, 2),
                "vs_current_plan": round(cost - current_plan_total, 2),
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
        """Return potential savings (negative means the current plan is cheaper)."""
        if not self.coordinator.data or "alternative_plans" not in self.coordinator.data:
            return None

        # If waiting for data, return 0
        if self.coordinator.data.get("status") == "waiting_for_data":
            return 0

        plans = self.coordinator.data["alternative_plans"]
        current_plan_total = self.coordinator.data.get("current_plan_total", 0)

        if not plans:
            return None

        # Find best alternative
        best_cost = min(plans.values())

        # Negative value means the current plan is cheaper
        savings = current_plan_total - best_cost
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
        current_plan_total = self.coordinator.data.get("current_plan_total", 0)
        current_plan_name = self.coordinator.data.get("current_plan_name") or "your current plan"

        if not plans:
            return {}

        best_plan = min(plans.items(), key=lambda x: x[1])

        return {
            "best_alternative": best_plan[0],
            "best_alternative_cost": round(best_plan[1], 2),
            "current_plan_cost": round(current_plan_total, 2),
            "recommendation": (
                f"Stay with {current_plan_name}" if current_plan_total <= best_plan[1]
                else f"Consider switching to {best_plan[0]}"
            ),
        }
