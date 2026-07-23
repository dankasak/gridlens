"""Store-backed per-day override of a deferrable load's daily kWh target.

One instance is created per config entry in __init__.py and shared (via
hass.data[DOMAIN][f"{entry_id}_deferrable_overrides"]) between the number.py
entities (writer, one per configured deferrable device) and AdvisoryCoordinator
(reader, in _deferrable_device_params) — sharing the instance means both sides
see the same in-memory cache, so a write from the dashboard is visible on the
coordinator's very next tick without a second Store object re-reading a
possibly-stale copy from disk.

Expiry is date-based, not a timer: every read compares the override's stored
set_date to today's local date and treats a mismatch as "no override" (see
override_expiry.read_value). This is simpler than an async_track_time_change
midnight reset — no timer to miss, and it self-heals across restarts.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .override_expiry import read_value, write_value

STORE_VERSION = 1


def _today_local() -> str:
    return dt_util.now().date().isoformat()


class DeferrableOverrideStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_deferrable_overrides_{entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {}
            self._loaded = True

    async def async_get(self, sensor_id: str) -> float:
        """Today's override kWh for sensor_id, or 0.0 if unset/expired."""
        await self._ensure_loaded()
        return read_value(self._data, sensor_id, _today_local())

    async def async_set(self, sensor_id: str, value_kwh: float) -> None:
        """Set (or clear, if value_kwh <= 0) today's override for sensor_id."""
        await self._ensure_loaded()
        self._data = write_value(self._data, sensor_id, value_kwh, _today_local())
        await self._store.async_save(self._data)
