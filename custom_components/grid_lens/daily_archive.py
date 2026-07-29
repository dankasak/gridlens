"""GridLens-owned archive of daily energy totals (PV / grid import / grid export).

Why this exists: the only recorder data HA keeps indefinitely is hourly long-term
statistics, and even those only reach back to when each sensor was created — and
they silently lose continuity when a user renames an entity or swaps inverter
integrations. Everything higher-resolution follows `purge_keep_days` (default 10).
Long-horizon features (seasonal plan comparison, per-calendar-month PV/load
climatology) need a daily-totals history that survives recorder purges, entity
renames, and DB migrations, so GridLens records it into its own HA Store from the
moment the integration is installed. The payload is tiny (~3 floats/day → a few
KB/year), so there is no retention cap.

On first run the archive backfills whatever daily history the recorder's
long-term statistics still hold; after that a listener shortly after local
midnight appends yesterday's totals (re-scanning a trailing window so days missed
across HA downtime self-heal).

Merge policy is fill-gaps-only: an already-recorded day is never overwritten,
so a later statistics rewrite (meter-reset artifacts, LTS repair tools) cannot
retroactively corrupt values captured close to the fact.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_SOLAR_SENSOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1

# How far back the initial backfill probes for long-term statistics.
BACKFILL_YEARS = 5
# Trailing window re-scanned on every daily tick, so gaps from downtime self-heal.
CATCHUP_DAYS = 14

# metric key in the store -> config key holding the sensor entity_id
_METRIC_SENSORS = {
    "pv": CONF_SOLAR_SENSOR,
    "imp": CONF_ENERGY_SENSOR,
    "exp": CONF_GRID_EXPORT_SENSOR,
}


class DailyEnergyArchive:
    """Append-only store of per-local-day kWh totals keyed by ISO date.

    Shape: {"days": {"2026-07-28": {"pv": 16.6, "imp": 3.2, "exp": 8.1}}}
    A metric is absent for a day when its sensor isn't configured, has no
    statistics, or that day's change was invalid (negative = meter reset).
    Household load isn't stored — consumers can estimate it as pv + imp - exp
    (battery flows roughly cancel over a full day).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_daily_archive_{entry.entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {"days": {}}
            self._data.setdefault("days", {})
            self._loaded = True

    # ---------------------------------------------------------------- queries

    async def async_get_days(self) -> dict[str, dict[str, float]]:
        """All recorded days ({iso_date: {metric: kwh}}). Returned dict is a copy."""
        await self._ensure_loaded()
        return {d: dict(v) for d, v in self._data["days"].items()}

    async def async_monthly_distribution(self, metric: str) -> dict[int, list[float]]:
        """Daily values of one metric bucketed by calendar month (1-12), sorted.

        This is the empirical per-month distribution long-horizon comparisons
        sample from ("percentage of days at a given PV output").
        """
        await self._ensure_loaded()
        buckets: dict[int, list[float]] = {}
        for day_iso, metrics in self._data["days"].items():
            if metric in metrics:
                buckets.setdefault(int(day_iso[5:7]), []).append(metrics[metric])
        return {m: sorted(v) for m, v in sorted(buckets.items())}

    # ---------------------------------------------------------------- writing

    def _sensors(self) -> dict[str, str]:
        """Configured metric -> entity_id map (skips unconfigured sensors)."""
        out = {}
        for metric, conf_key in _METRIC_SENSORS.items():
            sensor = self._entry.data.get(conf_key)
            if sensor:
                out[metric] = sensor
        return out

    async def _fetch_daily_changes(
        self, sensor: str, start: datetime, end: datetime
    ) -> dict[str, float]:
        """Per-local-day kWh change for one sensor from long-term statistics."""
        try:
            stats = await get_instance(self._hass).async_add_executor_job(
                statistics_during_period,
                self._hass,
                start,
                end,
                {sensor},
                "day",
                None,
                {"change"},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Daily archive: statistics fetch failed for %s: %s", sensor, err)
            return {}

        out: dict[str, float] = {}
        for record in stats.get(sensor, []):
            change = record.get("change")
            # Negative change = cumulative-meter reset artifact; that day's total
            # is unknowable from statistics, so skip it rather than store garbage.
            if change is None or float(change) < 0:
                continue
            ts = record["start"]
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts, tz=timezone.utc)
            elif ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            day_iso = dt_util.as_local(ts).date().isoformat()
            out[day_iso] = round(float(change), 3)
        return out

    async def async_record_range(self, start_day: date, end_day: date) -> int:
        """Fill archive gaps for complete local days in [start_day, end_day].

        Existing day/metric values are never overwritten. Returns the number of
        day-metric values added.
        """
        await self._ensure_loaded()
        today = dt_util.now().date()
        if end_day >= today:
            end_day = today - timedelta(days=1)  # only complete days
        if end_day < start_day:
            return 0

        tz = dt_util.get_default_time_zone()
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
        end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=tz)

        days = self._data["days"]
        end_iso = end_day.isoformat()
        added = 0
        for metric, sensor in self._sensors().items():
            changes = await self._fetch_daily_changes(sensor, start, end)
            for day_iso, kwh in changes.items():
                # The recorder can return the bucket starting exactly at the end
                # boundary (i.e. today, still incomplete) — never archive it, or
                # the fill-gaps-only merge would freeze the partial value forever.
                if day_iso > end_iso:
                    continue
                entry = days.setdefault(day_iso, {})
                if metric not in entry:
                    entry[metric] = kwh
                    added += 1
        if added:
            await self._store.async_save(self._data)
        return added

    async def async_backfill(self) -> None:
        """One-shot on setup: capture whatever the recorder's LTS still holds."""
        today = dt_util.now().date()
        added = await self.async_record_range(
            today - timedelta(days=BACKFILL_YEARS * 365), today - timedelta(days=1)
        )
        await self._ensure_loaded()
        n_days = len(self._data["days"])
        if added:
            _LOGGER.info(
                "Daily archive: backfill added %d values; archive now spans %d days",
                added,
                n_days,
            )
        else:
            _LOGGER.debug("Daily archive: backfill found nothing new (%d days stored)", n_days)

    async def async_daily_tick(self, _now=None) -> None:
        """Shortly-after-midnight listener: append yesterday (+ heal recent gaps)."""
        today = dt_util.now().date()
        added = await self.async_record_range(
            today - timedelta(days=CATCHUP_DAYS), today - timedelta(days=1)
        )
        if added:
            _LOGGER.debug("Daily archive: recorded %d values", added)
