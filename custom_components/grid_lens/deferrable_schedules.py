"""Store-backed weekly (per-weekday, per-hour) availability schedule for deferrable loads.

One instance is created per config entry in __init__.py and shared (via
hass.data[DOMAIN][f"{entry_id}_deferrable_schedules"]) between the writer (the
grid_lens.set_deferrable_schedule service, called by the schedule editor card) and the
readers (AdvisoryCoordinator's horizon mask builder, plan_calculator's LP mask builder,
and sensor.py's deferrable_loads attribute) — sharing the instance means a schedule
saved from the dashboard is visible on the coordinator's very next tick without a second
Store object re-reading a possibly-stale copy from disk.

A stored weekly grid replaces the device's static deferrable_load_hours config spec;
devices with no stored grid keep the config-spec behaviour unchanged. Grid semantics and
validation live in schedule_grid.py (zero HA imports, offline-testable).
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .schedule_grid import read_week, write_week

STORE_VERSION = 1


class DeferrableScheduleStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_deferrable_schedules_{entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def async_load(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {}
            self._loaded = True

    async def async_get(self, sensor_id: str):
        """The 7x24 weekly grid for sensor_id, or None if unset."""
        await self.async_load()
        return read_week(self._data, sensor_id)

    def cached(self, sensor_id: str):
        """Sync read of the in-memory cache (for sensor attributes, where an async store
        load isn't possible). __init__.py preloads the store at setup, so this is only
        empty in the brief window before that completes."""
        return read_week(self._data, sensor_id)

    async def async_set(self, sensor_id: str, week) -> None:
        """Set (or clear, if week is None) sensor_id's weekly grid.
        Raises ValueError on a malformed grid."""
        await self.async_load()
        self._data = write_week(
            self._data, sensor_id, week, dt_util.now().date().isoformat()
        )
        await self._store.async_save(self._data)
