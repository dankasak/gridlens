"""Store-backed override of a deferrable load's daily kWh target.

One instance is created per config entry in __init__.py and shared (via
hass.data[DOMAIN][f"{entry_id}_deferrable_overrides"]) between the number.py
entities (writer, one per configured deferrable device) and AdvisoryCoordinator
(reader, in _deferrable_device_params) — sharing the instance means both sides
see the same in-memory cache, so a write from the dashboard is visible on the
coordinator's very next tick without a second Store object re-reading a
possibly-stale copy from disk.

The override does NOT auto-expire at local midnight — it persists until the
user explicitly clears it (sets it back to 0). It used to expire on a plain
date-string comparison, which seemed simpler than a midnight timer, but had a
real failure mode: the advisory LP plans on a 36h rolling horizon, so a plan
built before midnight could already be relying on a boosted target for a
still-future (post-midnight) slot — e.g. reserving battery SOC overnight for
an early-morning EV charge instead of exporting it. The override would then
vanish out from under that plan within minutes of the date ticking over (the
advisory re-reads it every 2-min tick), silently reverting daily_kwh back to
the historical average mid-plan and undoing the reservation. See the
2026-07-31 incident this replaced. Instead, `async_get` raises a persistent
notification once per calendar day an active override carries over, so a
forgotten boost is visible rather than either silently reverting (old
behaviour) or silently running forever (the risk of just removing expiry
outright).
"""
from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity_lookup import resolve_device_name
from .override_expiry import mark_notified, read_value, should_notify_carryover, write_value

STORE_VERSION = 1


def _today_local() -> str:
    return dt_util.now().date().isoformat()


class DeferrableOverrideStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_deferrable_overrides_{entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {}
            self._loaded = True

    async def async_get(self, sensor_id: str) -> float:
        """sensor_id's current override kWh, or 0.0 if unset. Never expires on
        its own; the first read of each new calendar day for an override that
        carried over from an earlier day raises a persistent notification."""
        await self._ensure_loaded()
        today = _today_local()
        if should_notify_carryover(self._data, sensor_id, today):
            self._notify_carryover(sensor_id)
            self._data = mark_notified(self._data, sensor_id, today)
            await self._store.async_save(self._data)
        return read_value(self._data, sensor_id)

    def _notify_carryover(self, sensor_id: str) -> None:
        name = resolve_device_name(self._hass, sensor_id) or sensor_id
        value = read_value(self._data, sensor_id)
        persistent_notification.async_create(
            self._hass,
            f"The 'Today Boost' for **{name}** ({value:g} kWh) is still set from a "
            "previous day — it no longer clears itself automatically at midnight. "
            "Set it back to 0 on the Grid Lens dashboard if you don't want it applied today.",
            title="Grid Lens: boost carried over",
            notification_id=f"grid_lens_boost_carryover_{sensor_id}",
        )

    async def async_set(self, sensor_id: str, value_kwh: float) -> None:
        """Set (or clear, if value_kwh <= 0) sensor_id's override."""
        await self._ensure_loaded()
        self._data = write_value(self._data, sensor_id, value_kwh, _today_local())
        await self._store.async_save(self._data)
