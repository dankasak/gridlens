"""Store-backed "charge timing preference" — Prefer early / No preference /
Prefer just-in-time — for each SOC-tracked deferrable load's floor-satisfying
charge (see charge_timing_preference.py for the maths and why this exists).

One instance is created per config entry in __init__.py and shared (via
hass.data[DOMAIN][f"{entry_id}_charge_timing_preferences"]) between the
select.py entity (writer — one GridLensChargeTimingPreferenceSelect per
SOC-tracked deferrable device) and AdvisoryCoordinator (reader, in
_deferrable_for_horizon) — sharing the instance means a dashboard change is
visible on the coordinator's very next tick, same reasoning as
ChargeTargetStore/DailyTargetStore.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .reoptimize import request_reoptimize
from .charge_timing_preference import read, write

STORE_VERSION = 1


def update_signal(entry_id: str) -> str:
    """Dispatcher signal fired whenever a device's preference changes — sent with
    the affected sensor_id. The entity connects to this in async_added_to_hass so
    it stays in sync regardless of which path changed it (today, only the entity
    itself writes; the signal exists so a future automation-facing service, same
    shape as grid_lens.set_charge_target, doesn't silently go stale — see
    charge_target_store.py's update_signal for the precedent)."""
    return f"{DOMAIN}_charge_timing_preference_updated_{entry_id}"


class ChargeTimingPreferenceStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = Store(
            hass, STORE_VERSION, f"{DOMAIN}_charge_timing_preferences_{entry_id}"
        )
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {}
            self._loaded = True

    async def async_get(self, sensor_id: str) -> str:
        """The stored preference for sensor_id, or the default (Prefer
        just-in-time) if this device has never had one set."""
        await self._ensure_loaded()
        return read(self._data, sensor_id)

    async def async_set(self, sensor_id: str, preference: str) -> None:
        await self._ensure_loaded()
        self._data = write(self._data, sensor_id, preference)
        await self._store.async_save(self._data)
        async_dispatcher_send(self._hass, update_signal(self._entry_id), sensor_id)
        request_reoptimize(self._hass, self._entry_id)
