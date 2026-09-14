"""Store-backed ad-hoc "charge to X% by a datetime" target for one deferrable load.

One instance is created per config entry in __init__.py and shared (via
hass.data[DOMAIN][f"{entry_id}_charge_targets"]) between the number.py /
datetime.py entities (writers — percent and target time are two separate HA
entities that both write into the same store entry, see number.py's
GridLensChargeTargetPercentNumber and datetime.py's GridLensChargeTargetTimeDateTime)
and AdvisoryCoordinator (reader, in _deferrable_for_horizon) — sharing the
instance means a dashboard write is visible on the coordinator's very next
tick, same reasoning as DeferrableOverrideStore.

Unlike DeferrableOverrideStore's "Today Boost" (which persists until manually
cleared — a boost has no natural end condition), a dated target DOES have one:
async_get_active auto-clears it once the live SOC has reached the target
percent, or once the deadline itself has passed — see charge_target.is_reached /
is_expired. No carry-over notification exists here because there is nothing to
carry over silently: either it's still working toward a live deadline, or it
has already resolved one way or the other and cleared itself.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import charge_target as ct
from .const import DOMAIN

STORE_VERSION = 1


def update_signal(entry_id: str) -> str:
    """Dispatcher signal fired whenever a target changes for entry_id — sent with the
    affected sensor_id. There are TWO ways a target's underlying store entry can change
    without the number.py/datetime.py entity that's normally showing it being the one
    that wrote it: the grid_lens.set_charge_target/clear_charge_target services, and
    async_get_active's own auto-clear-on-reach-or-expiry. Both entities connect to this
    in their async_added_to_hass so they stay in sync with the store regardless of which
    path changed it, instead of only ever reflecting their own last write."""
    return f"{DOMAIN}_charge_target_updated_{entry_id}"


class ChargeTargetStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_charge_targets_{entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {}
            self._loaded = True

    async def async_get_raw(self, sensor_id: str) -> dict | None:
        """The stored {"percent", "target_iso"} for sensor_id with no reach/expiry
        check — what the number.py/datetime.py entities restore their own state
        from, so a half-set target (only one of the two entities configured so far)
        still shows correctly on each entity individually."""
        await self._ensure_loaded()
        return ct.read_target(self._data, sensor_id)

    async def async_get_active(self, sensor_id: str, current_percent: float | None) -> dict | None:
        """The target for sensor_id if it is still live, else None — auto-clearing it
        first if the live SOC has already reached it or its deadline has passed.
        `current_percent` is the device's live SOC reading right now (None if
        unavailable, in which case only the deadline can expire it)."""
        target = await self.async_get_raw(sensor_id)
        if target is None:
            return None
        reached = ct.is_reached(target["percent"], current_percent)
        expired = False
        try:
            target_dt = dt_util.parse_datetime(target["target_iso"])
            expired = target_dt is not None and ct.is_expired(target_dt, dt_util.utcnow())
        except (TypeError, ValueError):
            expired = False
        if reached or expired:
            await self.async_set(sensor_id, 0.0, "")
            return None
        return target

    async def async_set(self, sensor_id: str, percent: float, target_iso: str) -> None:
        """Set (or clear, if percent <= 0 or target_iso is blank) sensor_id's target."""
        await self._ensure_loaded()
        self._data = ct.write_target(self._data, sensor_id, percent, target_iso)
        await self._store.async_save(self._data)
        async_dispatcher_send(self._hass, update_signal(self._entry_id), sensor_id)
