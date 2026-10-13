"""Store-backed ad-hoc "don't discharge/export below X% until a datetime"
reserve for the main house battery.

One instance is created per config entry in __init__.py and shared (via
hass.data[DOMAIN][f"{entry_id}_export_floor"]) between the number.py /
datetime.py entities (writers — percent and deadline are two separate HA
entities that both write into the same store entry, see number.py's
GridLensExportFloorPercentNumber and datetime.py's
GridLensExportFloorUntilDateTime) and AdvisoryCoordinator (reader, in _run) —
sharing the instance means a dashboard write is visible on the coordinator's
very next tick, same reasoning as ChargeTargetStore.

Unlike ChargeTargetStore, there is only ever ONE floor per entry (the main
battery is singular), not one per sensor_id — so this store's data shape is
just {"percent": float, "until_iso": str} directly, no outer dict keyed by
device.

auto-clears once the deadline itself has passed — see export_floor.is_expired.
There is no "reached" condition (a floor has nothing to reach), so unlike a
charge target this only ever expires, never completes early.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import export_floor as ef
from .const import DOMAIN
from .reoptimize import request_reoptimize

STORE_VERSION = 1


def update_signal(entry_id: str) -> str:
    """Dispatcher signal fired whenever the floor changes for entry_id. There are
    TWO ways the underlying store entry can change without the number.py/
    datetime.py entity currently showing it being the one that wrote it: the
    grid_lens.set_export_floor/clear_export_floor services, and
    async_get_active's own auto-clear-on-expiry. Both entities connect to this
    in their async_added_to_hass so they stay in sync regardless of which path
    changed it."""
    return f"{DOMAIN}_export_floor_updated_{entry_id}"


class ExportFloorStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_export_floor_{entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {}
            self._loaded = True

    async def async_get_raw(self) -> dict:
        """The stored {"percent", "until_iso"} exactly as held, with no
        completeness or expiry check — what the number.py/datetime.py entities
        restore/merge their own state from, so a half-set floor (only one of
        the two entities configured so far) still shows correctly on each
        entity individually instead of appearing cleared. Never use this to
        decide whether a floor is actually live — that's async_get_active's
        job."""
        await self._ensure_loaded()
        return ef.read_raw(self._data)

    async def async_get_active(self) -> dict | None:
        """The floor if it is still live, else None — auto-clearing it first
        if its deadline has already passed. Uses read_target (not
        async_get_raw/read_raw) so a half-set floor — only one of the paired
        percent/datetime entities written so far — can never reach the
        optimizer."""
        await self._ensure_loaded()
        target = ef.read_target(self._data)
        if target is None:
            return None
        expired = False
        try:
            until_dt = dt_util.parse_datetime(target["until_iso"])
            expired = until_dt is not None and ef.is_expired(until_dt, dt_util.utcnow())
        except (TypeError, ValueError):
            expired = False
        if expired:
            await self.async_set(0.0, "")
            return None
        return target

    async def async_set(self, percent: float, until_iso: str) -> None:
        """Set (or clear, if percent <= 0 or until_iso is blank) the floor."""
        await self._ensure_loaded()
        self._data = ef.write_target(self._data, percent, until_iso)
        await self._store.async_save(self._data)
        request_reoptimize(self._hass, self._entry_id)
        async_dispatcher_send(self._hass, update_signal(self._entry_id))
