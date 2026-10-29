"""GridLens-owned archive of raw sub-hourly import/export kWh, plan-agnostic.

Why this exists: `_compute_bill_items` attributes import/export kWh to a plan's declared
rate windows, preferring 5-minute short-term recorder statistics because a window with a
sub-hourly boundary (e.g. Flow Power's FiT window, 17:30-19:30) needs sub-hourly resolution
— an hourly bucket spanning 17:00-18:00 straddles the boundary, and the kWh inside it has to
be guessed at once only the hourly total survives (see docs/GRIDLENS_CHECKLIST.md's
2026-07-15 entry for the bug this was built to fix the first time). That 5-minute data is
purged after `purge_keep_days` (90 on this install, raised from HA's default 10 specifically
to widen this window — see the 2026-07-29 checklist retention-reality-check entry) — and a
bill checked later than that falls back to an even pro-rated split across the boundary hour,
which is only right if consumption/export happens to be spread uniformly within it. It
usually isn't: a battery-discharge automation timed to a FiT window (this install's
`activate_flow_power_export`/`end_of_flow_power_export`) front/back-loads right at the
window edges, so the even split can misattribute a large share of real peak export to
off-peak once the 5-minute shape is gone (re-confirmed 2026-10-27, re-checking the SAME Flow
Power bill the original fix was built against, 108 days after the billing period ended — 18
days past the 90-day extension).

**This module is a 2026-10-27 same-day redesign, not the original shape.** The first version
stored each day's kWh *already bucketed by the plan actually held that day's rate values*
(e.g. Flow Power's 45c/0c buckets). That throws away exactly the information a DIFFERENT
plan's pricing would need: Plan Comparison's whole point is ranking alternative plans
against real historical usage, and an alternative almost never shares the held plan's window
boundaries. A kWh already collapsed into "Flow Power's 17:30-19:30 bucket" cannot be
re-sliced into some other plan's different window — the time-of-day information is gone.
So this version stores raw energy per fixed 30-minute slot instead, with no plan coupling at
all: ANY plan's `get_import_rate_info`/`get_export_rate_info` can be re-run over the stored
slots at read time, whether that's the plan actually held that day, a later version of the
same plan, or a completely different candidate plan being ranked. 30 minutes is not an
arbitrary choice — no real AU retail tariff in this product's data splits a window finer
than the half-hour (see the pre-existing comment in `_compute_bill_items`'s hourly fallback:
"windows never split finer than :30").

This closes the gap the way `daily_archive.py` already closes it for daily PV/import/export
totals: capture the number the SAME day it's computed (while 5-minute data is still fresh),
and persist it forever in GridLens's own HA Store, so a bill check — or an alternative-plan
ranking — run any time later reads real sub-hourly shape with zero recorder-retention
dependency, for whichever plan is being priced.

Not backfillable beyond what 5-minute data still exists: unlike `daily_archive.py` (which
backfills 5 years from hourly long-term statistics, which HA never purges), this needs
sub-hourly data that is already gone for any day older than retention. `async_backfill`
tries a bounded window regardless (`BACKFILL_DAYS`) — a day beyond actual retention just
comes back with no 5-minute records and is skipped at no real cost.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1

# No real AU retail window splits finer than this — see module docstring.
SLOT_MINUTES = 30
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES

# Conservative bound for the one-shot setup backfill. Not read from the recorder's
# configured purge_keep_days (a YAML setting, not queryable without extra plumbing) —
# a day beyond whatever retention actually is just comes back empty and is skipped.
BACKFILL_DAYS = 95
# Trailing window re-scanned on every nightly tick, so gaps from downtime self-heal
# (mirrors daily_archive.py's CATCHUP_DAYS).
CATCHUP_DAYS = 14


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


class SubHourlyEnergyArchive:
    """Append-only store of per-local-day, per-30-minute-slot kWh, keyed by ISO date.

    Shape: {"days": {"2026-10-26": {"import": [<48 floats>], "export": [<48 floats>]}}}
    Slot i covers local [i*30, i*30+30) minutes past local midnight. A direction key is
    absent for a day when its sensor wasn't configured or had no 5-minute data at archive
    time (recorder gap, or — for backfill — already past retention).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_subhourly_energy_archive_{entry.entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {"days": {}}
            self._data.setdefault("days", {})
            self._loaded = True

    # ---------------------------------------------------------------- reading

    async def async_get_range(self, start_day: date, end_day: date) -> dict[str, dict]:
        """Archived days in [start_day, end_day] that actually have an entry."""
        await self._ensure_loaded()
        days_store = self._data["days"]
        return {
            iso: days_store[iso]
            for iso in (d.isoformat() for d in _date_range(start_day, end_day))
            if iso in days_store
        }

    # ---------------------------------------------------------------- writing

    @staticmethod
    def _rollup_slots(records: list[dict], day_start_local: datetime, tz) -> list[float]:
        """Fold 5-minute (timestamp, value) readings into SLOTS_PER_DAY half-hour totals."""
        slots = [0.0] * SLOTS_PER_DAY
        for rec in records:
            kwh = rec.get("value", 0.0)
            if kwh <= 0:
                continue
            local_dt = rec["timestamp"].astimezone(tz)
            minutes = (local_dt - day_start_local).total_seconds() / 60
            idx = int(minutes // SLOT_MINUTES)
            if 0 <= idx < SLOTS_PER_DAY:
                slots[idx] += kwh
        return [round(v, 4) for v in slots]

    async def async_record_range(self, start_day: date, end_day: date, calculator) -> int:
        """Fill archive gaps for complete local days in [start_day, end_day].

        Existing day entries are never overwritten (fill-gaps-only, same policy as
        daily_archive.py) — once a day is captured while 5-minute data was fresh, a
        later statistics rewrite or repair tool cannot retroactively corrupt it.
        Returns the number of days added. No plan is resolved or needed here — see
        module docstring for why this version deliberately stores raw energy only.
        """
        await self._ensure_loaded()
        today = dt_util.now().date()
        if end_day >= today:
            end_day = today - timedelta(days=1)  # only complete days
        if end_day < start_day:
            return 0

        days_store = self._data["days"]
        pending = [d for d in _date_range(start_day, end_day) if d.isoformat() not in days_store]
        if not pending:
            return 0

        tz = dt_util.get_default_time_zone()
        added = 0
        for day in pending:
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
            day_end = day_start + timedelta(days=1)
            entry: dict = {}
            if calculator.energy_sensor:
                recs = await calculator._get_usage_data(
                    day_start, day_end, calculator.energy_sensor, period="5minute"
                )
                if recs:
                    entry["import"] = self._rollup_slots(recs, day_start, tz)
            if calculator.grid_export_sensor:
                recs = await calculator._get_usage_data(
                    day_start, day_end, calculator.grid_export_sensor, period="5minute"
                )
                if recs:
                    entry["export"] = self._rollup_slots(recs, day_start, tz)
            if not entry:
                continue  # 5-minute data isn't (or no longer) available for this day

            days_store[day.isoformat()] = entry
            added += 1

        if added:
            await self._store.async_save(self._data)
        return added

    async def async_backfill(self, calculator) -> None:
        """One-shot on setup: capture whatever 5-minute data the recorder still holds."""
        today = dt_util.now().date()
        try:
            added = await self.async_record_range(
                today - timedelta(days=BACKFILL_DAYS), today - timedelta(days=1), calculator
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Sub-hourly energy archive: backfill failed: %s", exc)
            return
        await self._ensure_loaded()
        n_days = len(self._data["days"])
        if added:
            _LOGGER.info(
                "Sub-hourly energy archive: backfill captured %d day(s); archive now holds %d day(s)",
                added, n_days,
            )
        else:
            _LOGGER.debug("Sub-hourly energy archive: backfill found nothing new (%d day(s) stored)", n_days)

    async def async_daily_tick(self, calculator, _now=None) -> None:
        """Shortly-after-midnight listener: capture yesterday (+ heal recent gaps)."""
        today = dt_util.now().date()
        try:
            added = await self.async_record_range(
                today - timedelta(days=CATCHUP_DAYS), today - timedelta(days=1), calculator
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Sub-hourly energy archive: daily tick failed: %s", exc)
            return
        if added:
            _LOGGER.debug("Sub-hourly energy archive: captured %d day(s)", added)
