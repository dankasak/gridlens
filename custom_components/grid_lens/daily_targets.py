"""Store-backed "Daily Target" percent-of-average target, per deferrable device
plus one master that applies to every device without its own explicit override.

Named "Daily Target", not "Tomorrow Planning" (its original name, changed
2026-09-22 — see daily_target_rules.py's docstring): it applies to every day in
the advisory LP's rolling horizon, not one calendar date, and takes effect from
the next advisory tick whatever time of day you set it.

One instance is created per config entry in __init__.py and shared (via
hass.data[DOMAIN][f"{entry_id}_daily_targets"]) between the number.py
entities (writer — one GridLensDeferrableTargetPercentNumber per configured
deferrable device, plus one GridLensMasterTargetPercentNumber) and
AdvisoryCoordinator (reader, in _apply_daily_targets) — sharing the instance
means a dashboard write is visible on the coordinator's very next tick, same
reasoning as DeferrableOverrideStore.

See daily_target_rules.py's docstring for why this is NOT a copy of
DeferrableOverrideStore: 0% is a meaningful, explicit target here ("skip this
device"), so it can't double as the "clear back to default" sentinel the way
0 kWh does for Today Boost — clearing is its own action (async_clear), and a
set percent persists (with a once-a-day carry-over notice) rather than
auto-expiring at midnight, for the same rolling-horizon reason Today Boost
itself moved away from midnight expiry.
"""
from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity_lookup import resolve_device_name
from .reoptimize import request_reoptimize
from .daily_target_rules import (
    MASTER_KEY,
    clear_percent,
    mark_notified,
    read_percent,
    should_notify_carryover,
    write_percent,
)

STORE_VERSION = 1
DEFAULT_MASTER_PERCENT = 100.0


def _today_local() -> str:
    return dt_util.now().date().isoformat()


def update_signal(entry_id: str) -> str:
    """Dispatcher signal fired whenever ANY key in the store changes (a device pin,
    a clear, or the master) — sent with the affected key (a sensor_id, or MASTER_KEY).
    Needed because there are TWO ways a store entry can change without the number.py
    entity that's normally showing it being the one that wrote it: the
    grid_lens.set_daily_target/clear_daily_target services, and — for a
    per-device entity specifically — a master-percent change (a device with no pin of
    its own displays the master's value, so it must repaint when master moves even
    though its OWN key in the store never changed). Every entity connects to this in
    its own async_added_to_hass so it stays in sync regardless of which path changed
    it, the same reasoning as charge_target_store.py's update_signal."""
    return f"{DOMAIN}_daily_target_updated_{entry_id}"


class DailyTargetStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_daily_targets_{entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {}
            self._loaded = True

    async def _get_raw(self, key: str) -> float | None:
        await self._ensure_loaded()
        today = _today_local()
        if should_notify_carryover(self._data, key, today):
            self._notify_carryover(key)
            self._data = mark_notified(self._data, key, today)
            await self._store.async_save(self._data)
        return read_percent(self._data, key)

    def _notify_carryover(self, key: str) -> None:
        if key == MASTER_KEY:
            label = "The Daily Target **master** percent"
        else:
            name = resolve_device_name(self._hass, key) or key
            label = f"The Daily Target for **{name}**"
        value = read_percent(self._data, key)
        persistent_notification.async_create(
            self._hass,
            f"{label} ({value:g}%) is still set from a previous day — it doesn't "
            "clear itself automatically. Reset it on the Grid Lens dashboard if you "
            "don't want it applied.",
            title="Grid Lens: Daily Target carried over",
            notification_id=f"grid_lens_daily_target_carryover_{key}",
        )

    async def async_get_master(self) -> float:
        """The master percent, or the 100% default if never set."""
        value = await self._get_raw(MASTER_KEY)
        return value if value is not None else DEFAULT_MASTER_PERCENT

    async def async_set_master(self, percent: float) -> None:
        await self._ensure_loaded()
        self._data = write_percent(self._data, MASTER_KEY, percent, _today_local())
        await self._store.async_save(self._data)
        async_dispatcher_send(self._hass, update_signal(self._entry_id), MASTER_KEY)
        request_reoptimize(self._hass, self._entry_id)

    async def async_get_effective(self, sensor_id: str) -> float:
        """sensor_id's own explicit override if set, else the current master percent."""
        device_value = await self._get_raw(sensor_id)
        if device_value is not None:
            return device_value
        return await self.async_get_master()

    async def async_is_override(self, sensor_id: str) -> bool:
        return (await self._get_raw(sensor_id)) is not None

    async def async_set(self, sensor_id: str, percent: float) -> None:
        """Pin sensor_id to an explicit percent, independent of the master."""
        await self._ensure_loaded()
        self._data = write_percent(self._data, sensor_id, percent, _today_local())
        await self._store.async_save(self._data)
        async_dispatcher_send(self._hass, update_signal(self._entry_id), sensor_id)
        request_reoptimize(self._hass, self._entry_id)

    async def async_clear(self, sensor_id: str) -> None:
        """Unpin sensor_id so it goes back to following the master percent."""
        await self._ensure_loaded()
        self._data = clear_percent(self._data, sensor_id)
        await self._store.async_save(self._data)
        async_dispatcher_send(self._hass, update_signal(self._entry_id), sensor_id)
        request_reoptimize(self._hass, self._entry_id)
