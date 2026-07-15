"""History-based load model — average consumption per hour-of-day from HA statistics."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


async def build_hour_of_day_load(
    hass: HomeAssistant, sensor_id: str, days: int = 14
) -> list[float] | None:
    """Return a 24-length vector of average kWh consumed per hour-of-day.

    Reads hourly ``change`` statistics for a cumulative energy sensor
    (``total_increasing``) over the last ``days`` and averages by local hour-of-day.
    Returns None if statistics are unavailable so the caller can fall back.
    """
    if not sensor_id:
        return None
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        end = dt_util.utcnow()
        start = end - timedelta(days=days)
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {sensor_id},
            "hour",
            None,
            {"change"},
        )
    except Exception as err:  # noqa: BLE001 — recorder is best-effort
        _LOGGER.warning("Load history: statistics query failed for %s: %s", sensor_id, err)
        return None

    rows = (stats or {}).get(sensor_id, [])
    if not rows:
        _LOGGER.warning("Load history: no statistics for %s", sensor_id)
        return None

    sums = [0.0] * 24
    counts = [0] * 24
    for row in rows:
        change = row.get("change")
        if change is None:
            continue
        start_val = row.get("start")
        # `start` may be an epoch (float) or datetime depending on HA version.
        when = (
            dt_util.utc_from_timestamp(start_val)
            if isinstance(start_val, (int, float))
            else start_val
        )
        if when is None:
            continue
        hod = dt_util.as_local(when).hour
        sums[hod] += max(0.0, float(change))
        counts[hod] += 1

    if not any(counts):
        return None
    # Average per hour-of-day; fall back to the daily-mean for any empty hour.
    total = sum(sums)
    total_obs = sum(counts)
    daily_mean_hour = (total / total_obs) if total_obs else 0.5
    return [
        (sums[h] / counts[h]) if counts[h] else daily_mean_hour for h in range(24)
    ]
