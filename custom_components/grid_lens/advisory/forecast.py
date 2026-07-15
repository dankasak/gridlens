"""ForecastProvider — assembles hour-aligned solar/load/price arrays from HA sensors.

Grounded in the entities on this system:

* **Solar**  — Solcast ``sensor.solcast_pv_forecast_forecast_today`` / ``_tomorrow``,
  attribute ``detailedHourly`` = [{period_start, pv_estimate (kWh/h)}].
* **Import/Export** — a :class:`RateForecaster` (default ``PlanRateForecaster``) driven by
  our own GridLens plan definition — NOT a retailer integration's computed rate.
* **Load**   — pluggable :class:`LoadForecaster` (history-based model wired separately).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Protocol, Sequence

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .models import ForecastBundle
from .rates import RateForecaster

_LOGGER = logging.getLogger(__name__)


class LoadForecaster(Protocol):
    """Return per-slot load energy (kWh) for ``n_slots`` slots of ``slot_minutes``."""

    def series(self, start: datetime, n_slots: int, slot_minutes: int = 60) -> list[float]: ...


class FlatLoadForecaster:
    """Trivial constant-load fallback (``kwh_per_hour`` prorated to the slot)."""

    def __init__(self, kwh_per_hour: float = 0.5) -> None:
        self.kwh = kwh_per_hour

    def series(self, start: datetime, n_slots: int, slot_minutes: int = 60) -> list[float]:
        return [self.kwh * (slot_minutes / 60.0)] * n_slots


class HourOfDayLoadForecaster:
    """Load = average consumption for that hour-of-day (24-length kWh vector),
    prorated to the slot length. Populate ``by_hour`` from HA statistics; this class
    stays pure so it is unit-testable.
    """

    def __init__(self, by_hour: Sequence[float]) -> None:
        if len(by_hour) != 24:
            raise ValueError("by_hour must have 24 entries")
        self.by_hour = list(by_hour)

    def series(self, start: datetime, n_slots: int, slot_minutes: int = 60) -> list[float]:
        dt = slot_minutes / 60.0
        out = []
        for i in range(n_slots):
            slot = dt_util.as_local(start + timedelta(minutes=i * slot_minutes))
            out.append(max(0.0, self.by_hour[slot.hour]) * dt)
        return out


_DEFAULTS = {
    "solar_today": "sensor.solcast_pv_forecast_forecast_today",
    "solar_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
}


class ForecastProvider:
    def __init__(
        self,
        hass: HomeAssistant,
        rate_forecaster: RateForecaster,
        load_forecaster: Optional[LoadForecaster] = None,
        *,
        entities: Optional[dict[str, str]] = None,
        slot_minutes: int = 60,
    ) -> None:
        self.hass = hass
        self.rate_forecaster = rate_forecaster
        self.load_forecaster = load_forecaster or FlatLoadForecaster()
        self.e = {**_DEFAULTS, **(entities or {})}
        self.slot_minutes = int(slot_minutes)

    def build(self, n_slots: int = 24) -> Optional[ForecastBundle]:
        slot = self.slot_minutes
        dt = slot / 60.0
        start = self._floor_slot(dt_util.now(), slot)

        # Solcast: 30-min detail (detailedForecast) for sub-hourly, else hourly.
        # pv_estimate is average kW over the period → per-slot energy = kW × dt.
        attr = "detailedForecast" if slot < 60 else "detailedHourly"
        solar_map = self._solar_by_slot(attr, slot)
        solar = [
            max(0.0, solar_map.get(start + timedelta(minutes=i * slot), 0.0)) * dt
            for i in range(n_slots)
        ]

        imp, exp = self.rate_forecaster.rates(start, n_slots, slot)
        if not imp or not exp:
            _LOGGER.warning("ForecastProvider: rate forecaster returned no rates")
            return None

        load = self.load_forecaster.series(start, n_slots, slot)

        return ForecastBundle(
            start=start,
            solar_kwh=solar,
            load_kwh=load,
            import_rate=imp,
            export_rate=exp,
            slot_minutes=slot,
            sources={
                "solar_slots": sum(
                    1 for i in range(n_slots)
                    if (start + timedelta(minutes=i * slot)) in solar_map
                ),
                "rate_model": type(self.rate_forecaster).__name__,
                "load_model": type(self.load_forecaster).__name__,
                "slot_minutes": slot,
            },
        )

    # ------------------------------------------------------------------ sources
    def _solar_by_slot(self, attr: str, slot_minutes: int) -> dict[datetime, float]:
        out: dict[datetime, float] = {}
        for eid in (self.e["solar_today"], self.e["solar_tomorrow"]):
            st = self.hass.states.get(eid)
            if st is None:
                continue
            for row in st.attributes.get(attr, []) or []:
                dt = self._parse(row.get("period_start"))
                if dt is None:
                    continue
                out[self._floor_slot(dt, slot_minutes)] = float(row.get("pv_estimate", 0.0))
        return out

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _floor_slot(dt: datetime, slot_minutes: int) -> datetime:
        d = dt_util.as_local(dt)
        return d.replace(minute=(d.minute // slot_minutes) * slot_minutes,
                         second=0, microsecond=0)

    @staticmethod
    def _parse(value) -> Optional[datetime]:
        if not value:
            return None
        # Solcast stores period_start as a datetime object, not a string.
        if isinstance(value, datetime):
            return dt_util.as_local(value)
        if not isinstance(value, str):
            return None
        dt = dt_util.parse_datetime(value)
        if dt is None:
            # Flow Power uses "YYYY-MM-DD HH:MM:SS+1000" (no colon in offset).
            try:
                dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S%z")
            except (ValueError, TypeError):
                return None
        return dt_util.as_local(dt)
