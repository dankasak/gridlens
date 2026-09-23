"""Shade correction — learns a per-hour-of-day derate curve for a fixed, static solar
obstruction (trees, a neighbouring roofline) that a generic weather-based forecast
provider has no way to know about, and applies it wherever Grid Lens consumes a solar
forecast.

Why this exists: Solcast (and providers like it) model panel geometry, weather and
terrain, but not "there's a tree 15m west of this array that shades it every afternoon".
That shows up as a forecast that's roughly right in the morning and consistently too
high every afternoon, day after day, regardless of cloud cover — a *time-of-day* bias,
not a weather-forecasting error. Solcast's own fix for this (site-measurement tuning,
see the Solcast API Python SDK's "Rooftop PV Tuning" notebook) only fits capacity/
azimuth/tilt against your history — three scalars that can't represent "clear except
2-4pm", so it can only ever average the shading away rather than track its actual shape.

The approach here instead: compare the forecast provider's own live "power right now"
reading against actual production, hour by hour, over a trailing window, and learn a
ratio per local hour-of-day (0-23). Both series come from HA's recorder — no external
API calls, no separate telemetry upload, and it works with any forecast provider whose
"power now" entity has the shape ``resolve_forecast_power_sensor`` looks for (see
entity_lookup.py), not just Solcast.

Deliberately NOT modelling by (month, hour): this install's recorder only retains 90
days (see CLAUDE.md's TimescaleDB note), which isn't enough history to fill a 12x24
grid with confidence — a rolling hour-of-day-only window trades seasonal precision for
having *any* usable sample count, and re-learns as the season (and the sun's angle
through the shading obstruction) drifts across the trailing window.

Opt-in (CONF_SHADE_CORRECTION_ENABLED, default off): it needs both a forecast power
entity and CONF_SOLAR_SENSOR (actual production) configured and trustworthy, and it
feeds into ForecastProvider — which the battery optimizer plans against — so a bad
learn (e.g. actual production sensor briefly reporting zero) should never silently
degrade dispatch decisions for a household that never asked for this.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN, DEFAULT_SOLCAST_POWER_NOW_ENTITY
from .entity_lookup import resolve_forecast_power_sensor

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(hours=3)
# While the forecast entity can't yet be resolved (most commonly: grid_lens's
# async_setup_entry ran before the forecast provider's own integration — Solcast is
# cloud_polling — finished loading after a restart), poll fast so the sensor recovers
# within seconds instead of waiting up to a full UPDATE_INTERVAL. Same pattern as
# advisory/coordinator.py's WAITING_INTERVAL for the same class of startup race.
WAITING_INTERVAL = timedelta(seconds=20)
# Below this, a hover-near-zero forecast (dawn/dusk) turns tiny sensor noise into wild
# ratios that would dominate the median for no good reason.
MIN_FORECAST_KWH_PER_HOUR = 0.05
# Fewer samples than this for a given hour-of-day and the learned factor is kept at
# whatever it last was (or 1.0 = no correction, on the very first run) rather than
# committing to a ratio estimated from a handful of days.
MIN_SAMPLES_PER_HOUR = 5
# Shading only ever reduces production below a clean-sky model, but a modest upper
# clamp guards against a single bad statistics row (a spike) getting enshrined as "this
# hour normally produces more than forecast" for the next 3h until the next learn.
MIN_FACTOR = 0.0
MAX_FACTOR = 1.3

_ENERGY_UNIT_TO_KWH = {"kwh": 1.0, "wh": 1.0 / 1000.0, "mwh": 1000.0}
_POWER_UNIT_TO_KW = {"w": 1.0 / 1000.0, "kw": 1.0}


def _default_factors() -> list[float]:
    return [1.0] * 24


def _default_samples() -> list[int]:
    return [0] * 24


class ShadeCorrectionCoordinator(DataUpdateCoordinator):
    """Recomputes the 24-hour derate curve from recorder statistics on a slow timer.

    Cheap to recompute from scratch every tick (two `statistics_during_period` queries
    over `window_days`) — recorder statistics are durable, so unlike most GridLens
    coordinators there's nothing to persist across restarts; the first refresh after
    a restart reconstructs exactly the same curve as before it, modulo whatever new
    data arrived in between.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        forecast_override: Optional[str],
        actual_entity: str,
        window_days: int,
    ) -> None:
        # Starts on WAITING_INTERVAL — _resolve_forecast_entity below switches to the
        # slow UPDATE_INTERVAL once (and only once) a forecast entity actually resolves.
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_shade_correction", update_interval=WAITING_INTERVAL)
        self.entry = entry
        self.forecast_override = forecast_override
        self.forecast_entity: Optional[str] = None
        self.actual_entity = actual_entity
        self.window_days = max(1, int(window_days))
        self._factors = _default_factors()
        self._samples = _default_samples()
        self._resolve_forecast_entity()

    def _resolve_forecast_entity(self) -> None:
        """Re-attempted on every tick until it succeeds (see WAITING_INTERVAL above),
        then never again — once resolved, stays resolved for the life of this
        coordinator, so a later ambiguous-match tick can't make it flap between two
        candidate sensors."""
        if self.forecast_entity:
            return
        eid = (
            self.forecast_override
            or resolve_forecast_power_sensor(self.hass)
            or (
                DEFAULT_SOLCAST_POWER_NOW_ENTITY
                if self.hass.states.get(DEFAULT_SOLCAST_POWER_NOW_ENTITY) is not None
                else None
            )
        )
        if eid:
            self.forecast_entity = eid
            self.update_interval = UPDATE_INTERVAL
            _LOGGER.info("Shade correction: resolved forecast entity %s", eid)

    async def _async_update_data(self) -> dict[str, Any]:
        self._resolve_forecast_entity()
        if not self.forecast_entity:
            return self._result(error="waiting for a forecast power sensor to become available")
        try:
            return await self._learn()
        except Exception as exc:  # noqa: BLE001 — never break the optimizer's forecast path
            _LOGGER.warning("Shade correction: learn failed, keeping previous curve: %s", exc)
            return self._result(error=str(exc))

    async def _learn(self) -> dict[str, Any]:
        forecast_unit = self._unit(self.forecast_entity)
        actual_unit = self._unit(self.actual_entity)
        power_scale = _POWER_UNIT_TO_KW.get((forecast_unit or "").lower())
        energy_scale = _ENERGY_UNIT_TO_KWH.get((actual_unit or "").lower())
        if power_scale is None or energy_scale is None:
            return self._result(
                error=(
                    f"unrecognised units (forecast={forecast_unit!r} on {self.forecast_entity}, "
                    f"actual={actual_unit!r} on {self.actual_entity})"
                )
            )

        end = dt_util.utcnow()
        start = end - timedelta(days=self.window_days)
        instance = get_instance(self.hass)

        forecast_rows = await instance.async_add_executor_job(
            statistics_during_period, self.hass, start, end,
            {self.forecast_entity}, "hour", None, {"mean"},
        )
        # "change" (not "sum") — for a total_increasing energy sensor, recorder's "sum"
        # stat is a running reset-adjusted cumulative total, not a per-bucket delta;
        # "change" is recorder's own already-differenced per-period value, matching how
        # daily_archive.py reads the same kind of sensor.
        actual_rows = await instance.async_add_executor_job(
            statistics_during_period, self.hass, start, end,
            {self.actual_entity}, "hour", None, {"change"},
        )
        forecast_by_start = {
            self._bucket_key(row["start"]): row["mean"] * power_scale
            for row in forecast_rows.get(self.forecast_entity, [])
            if row.get("mean") is not None
        }
        actual_by_start = {
            self._bucket_key(row["start"]): row["change"] * energy_scale
            for row in actual_rows.get(self.actual_entity, [])
            # Negative change = cumulative-meter reset artifact (see daily_archive.py) —
            # that hour's real production is unknowable from statistics, so skip it.
            if row.get("change") is not None and float(row["change"]) >= 0
        }

        ratios_by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
        for ts, forecast_kwh in forecast_by_start.items():
            actual_kwh = actual_by_start.get(ts)
            if actual_kwh is None or forecast_kwh < MIN_FORECAST_KWH_PER_HOUR:
                continue
            local_hour = dt_util.as_local(ts).hour
            ratio = max(MIN_FACTOR, min(MAX_FACTOR, actual_kwh / forecast_kwh))
            ratios_by_hour[local_hour].append(ratio)

        factors = list(self._factors)
        samples = [0] * 24
        for hour, ratios in ratios_by_hour.items():
            samples[hour] = len(ratios)
            if len(ratios) >= MIN_SAMPLES_PER_HOUR:
                factors[hour] = round(statistics.median(ratios), 3)

        self._factors = factors
        self._samples = samples
        return self._result()

    def _result(self, error: Optional[str] = None) -> dict[str, Any]:
        return {
            "factors": list(self._factors),
            "samples": list(self._samples),
            "computed_at": dt_util.utcnow().isoformat(),
            "forecast_entity": self.forecast_entity,
            "actual_entity": self.actual_entity,
            "window_days": self.window_days,
            "error": error,
        }

    @staticmethod
    def _bucket_key(start: Any) -> datetime:
        """Normalise a statistics row's ``start`` to a tz-aware UTC datetime — HA has
        returned this as either an epoch float or a datetime depending on version/call
        path (see daily_archive.py's ``_fetch_daily_changes``, which defends the same
        way), and the two independent queries here must key-match regardless."""
        if isinstance(start, (int, float)):
            return datetime.fromtimestamp(start, tz=timezone.utc)
        if start.tzinfo is None:
            return start.replace(tzinfo=timezone.utc)
        return start

    def _unit(self, entity_id: str) -> Optional[str]:
        state = self.hass.states.get(entity_id)
        return state.attributes.get("unit_of_measurement") if state else None

    @property
    def factors(self) -> list[float]:
        """24-length list, hour-of-day (local) -> multiplier. Safe to read any time —
        defaults to all-1.0 (no correction) before the first successful learn."""
        return list(self._factors)


class ShadeCorrectedForecastPowerSensor(CoordinatorEntity, SensorEntity):
    """The forecast provider's own live "power now" reading, multiplied by the learned
    hour-of-day factor. Recomputes on every raw-forecast update (every ~10 min, not
    just the coordinator's 3h learn tick) so it tracks the underlying forecast as
    closely as the uncorrected sensor does — only the derate curve itself is slow-moving.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:weather-partly-cloudy"

    def __init__(self, coordinator: ShadeCorrectionCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_name = "Solar Forecast (Shade Corrected)"
        self._attr_unique_id = f"{entry.entry_id}_shade_corrected_forecast_power"
        # Which entity the raw-forecast tracker below is currently wired to — None until
        # the coordinator resolves one (see ShadeCorrectionCoordinator's startup-race
        # handling). Re-synced on every coordinator update so a resolution that lands
        # AFTER this entity was added still gets tracked, not just one present at add-time.
        self._tracked_forecast_entity: Optional[str] = None
        self._unsub_forecast_tracker = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Grid Lens",
            "manufacturer": "Custom Integration",
            "model": "Plan Analyzer",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._sync_forecast_tracker()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_forecast_tracker is not None:
            self._unsub_forecast_tracker()
            self._unsub_forecast_tracker = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_forecast_tracker()
        super()._handle_coordinator_update()

    @callback
    def _sync_forecast_tracker(self) -> None:
        entity_id = self.coordinator.forecast_entity
        if entity_id == self._tracked_forecast_entity:
            return
        if self._unsub_forecast_tracker is not None:
            self._unsub_forecast_tracker()
            self._unsub_forecast_tracker = None
        self._tracked_forecast_entity = entity_id
        if entity_id is None:
            return

        @callback
        def _on_raw_forecast_change(event) -> None:
            self.async_write_ha_state()

        self._unsub_forecast_tracker = async_track_state_change_event(
            self.hass, [entity_id], _on_raw_forecast_change
        )

    @property
    def available(self) -> bool:
        return self._raw_state() is not None

    def _raw_state(self):
        entity_id = self.coordinator.forecast_entity
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @property
    def native_value(self) -> Optional[float]:
        raw = self._raw_state()
        if raw is None:
            return None
        factors = (self.coordinator.data or {}).get("factors") or _default_factors()
        hour = dt_util.now().hour
        return round(raw * factors[hour], 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        raw = self._raw_state()
        return {
            "raw_forecast_w": raw,
            "current_hour_factor": (data.get("factors") or _default_factors())[dt_util.now().hour],
            "hourly_factors": data.get("factors"),
            "hourly_samples": data.get("samples"),
            "window_days": data.get("window_days"),
            "computed_at": data.get("computed_at"),
            "forecast_entity_id": self.coordinator.forecast_entity,
            "actual_entity_id": self.coordinator.actual_entity,
            "error": data.get("error"),
        }
