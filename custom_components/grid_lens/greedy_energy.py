"""GreedyEnergyTracker — per-device cumulative kWh consumed while Greedy Consumption (see
control/load_controller.py, FEATURES.md §7) was actually driving the device, rather than
the plan or a manual command.

One instance per deferrable-load index that both has a real (or synthetic — see
load_estimation.py) energy sensor AND a controller in LoadControlManager.controllers (i.e.
is actually controllable; forecast-only and declared/"dummy" loads never qualify, since
neither has a control entity Greedy Consumption could ever drive). Feeds
plan_calculator.py's "exclude greedy consumption" option: subtracting this tracker's
period total from a device's raw metered kWh keeps opportunistic, price-window-specific
consumption out of the daily_kwh target fed to the optimiser when scoring *alternative*
plans — see plan_calculator.py's _get_deferrable_data.

Applied uniformly to on/off (DeferrableLoadController) and modulating
(ModulatingLoadController) devices alike — both expose the same ``greedy_reason`` property,
and this tracker never special-cases by controller type (see greedy_energy_math.py's
docstring for why a modulating device's attribution is coarser than an on/off device's, and
why that's an accepted trade-off rather than a gap).

Persistence is a dedicated Store (mirrors load_estimation.EstimateStore) — the tracker, not
the sensor entity, owns the number, same "manager persists, entity just displays it" split
used throughout this integration.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .greedy_energy_math import accumulate

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1


class GreedyEnergyStore:
    """One Store per config entry, holding every tracked device's persisted state (keyed
    by the device's index as a string) — same one-store-many-keys shape as
    load_estimation.EstimateStore."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_greedy_energy_{entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {}
            self._loaded = True

    async def async_get(self, key: str) -> dict:
        await self._ensure_loaded()
        return dict(self._data.get(key) or {})

    async def async_set(self, key: str, value: dict) -> None:
        await self._ensure_loaded()
        self._data[key] = value
        await self._store.async_save(self._data)


class GreedyEnergyTracker:
    def __init__(
        self,
        hass: HomeAssistant,
        *,
        store: GreedyEnergyStore,
        index: int,
        name: str,
        source_sensor_id: str,
        controller,
    ) -> None:
        self.hass = hass
        self._store = store
        self._store_key = str(index)
        self.index = index
        self.name = name
        self.source_sensor_id = source_sensor_id
        # DeferrableLoadController or ModulatingLoadController — only .greedy_reason is
        # ever read from it, so either works with no type check.
        self._controller = controller
        self.unique_id = f"greedy_energy_{index}"
        # Metadata only (never read internally by this class) — set by the caller right
        # after construction once the entity_id has been reserved via the entity
        # registry (see __init__.py._ensure_greedy_trackers), so plan_calculator.py can
        # fetch this tracker's own recorder history without waiting for the sensor
        # platform to actually create the entity. Same "reserve first, read later" split
        # as LoadEstimator.power_sensor_entity_id.
        self.sensor_entity_id: Optional[str] = None

        self.running_kwh = 0.0
        self._last_value: Optional[float] = None
        self._cancel_listener: Optional[Callable] = None

    # ------------------------------------------------------------------ lifecycle
    async def async_load(self) -> None:
        """Restore persisted state. Call once, before start()."""
        data = await self._store.async_get(self._store_key)
        self.running_kwh = float(data.get("running_kwh", 0.0))
        last = data.get("last_value")
        if last is not None:
            self._last_value = float(last)
        else:
            # Nothing persisted (feature freshly enabled for this device) — seed the
            # baseline from the sensor's current reading rather than waiting for the next
            # state change to arrive before this tracker knows where to measure from.
            self._last_value = self._read_kwh(self.hass.states.get(self.source_sensor_id))

    def start(self) -> None:
        """Wire the live listener. Call after async_load()."""
        self._cancel_listener = async_track_state_change_event(
            self.hass, [self.source_sensor_id], self._on_source_change
        )

    def stop(self) -> None:
        if self._cancel_listener:
            self._cancel_listener()
        self._cancel_listener = None

    # ------------------------------------------------------------------ event handler
    def _read_kwh(self, state) -> Optional[float]:
        if state is None or state.state in ("unknown", "unavailable", None):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = (state.attributes or {}).get("unit_of_measurement")
        return value / 1000.0 if unit == "Wh" else value  # treat anything else as kWh

    async def _on_source_change(self, event) -> None:
        new_value = self._read_kwh(event.data.get("new_state"))
        if new_value is None:
            return
        was_greedy = bool(getattr(self._controller, "greedy_reason", None))
        added, counter_reset = accumulate(self._last_value, new_value, was_greedy)
        self._last_value = new_value
        if added <= 0.0 and not counter_reset:
            return
        if counter_reset:
            _LOGGER.debug(
                "Greedy energy tracker for %s: source sensor %s went backwards — "
                "resyncing baseline, not subtracting", self.name, self.source_sensor_id,
            )
        self.running_kwh += added
        await self._persist()

    async def _persist(self) -> None:
        await self._store.async_set(self._store_key, {
            "running_kwh": self.running_kwh,
            "last_value": self._last_value,
        })
