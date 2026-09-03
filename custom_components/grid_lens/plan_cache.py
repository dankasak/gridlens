"""Resilient fetch + on-disk cache for the API ``/plans`` payload.

Three call sites need the same payload: ``GridLensCoordinator`` (the plan-comparison
engine), the ``/api/grid_lens/plan_stream`` SSE view and the custom-date-range branch of
``/api/grid_lens/plan_data``. Each used to fetch independently, and a transient non-200
left whichever caller hit it with an empty plan list. Because the main coordinator has no
periodic refresh (the ``calculate_period`` service is deprecated and raises), a single
blip at startup wedged current-plan resolution — and every sensor plus advisory mode that
depends on it — until the next HA restart. Observed 2026-09-03: a Cloudflare 502 while the
gridlens-api LXC was recovering from a boot-race left ``current_plan_name`` ``None`` for
hours.

``async_fetch_plans`` fetches once, persists the last good payload per config entry, and
returns the cached copy (up to ``CACHE_MAX_AGE`` old) whenever a live fetch fails. Plan
reference data changes slowly, so a days-old list is far better than nothing — the user's
*current* plan still resolves, which is what unblocks the optimiser and advisory mode.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_GRIDLENS_API_KEY,
    CONF_GRIDLENS_API_URL,
    CONF_STATE,
    CONF_DISTRIBUTOR,
)

_LOGGER = logging.getLogger(__name__)

# A live fetch that fails falls back to a cached payload no older than this. Two weeks is
# a deliberate trade: plan rates rarely move week-to-week, and resolving the plan the user
# actually holds from a slightly stale list beats a blank dashboard. A genuinely ended
# subscription (402) is never served from cache — see below.
CACHE_MAX_AGE = timedelta(days=14)

_STORE_VERSION = 1
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
_SUBSCRIPTION_NOTIFICATION_ID = "grid_lens_subscription_ended"


@dataclass(slots=True)
class PlanFetch:
    """Outcome of a plan-data fetch."""

    plans: dict = field(default_factory=dict)
    network_operators: dict = field(default_factory=dict)
    tier: str | None = None
    # "live"  — fresh from the API
    # "cache" — the API failed; serving the last good payload from disk
    # "empty" — the API failed and there is no usable cache (or a 402 / ended subscription)
    source: str = "empty"
    status: int | None = None  # HTTP status of the live attempt, if one completed
    age: timedelta | None = None  # age of the payload when source == "cache"

    @property
    def ok(self) -> bool:
        """True when we have plans to work with, live or cached."""
        return bool(self.plans)


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(hass, _STORE_VERSION, f"{DOMAIN}_plan_cache_{entry.entry_id}")


async def async_fetch_plans(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    extra_params: dict | None = None,
    notify_on_402: bool = True,
) -> PlanFetch:
    """Fetch ``/plans`` for this entry, falling back to the on-disk cache on failure.

    ``extra_params`` is merged into the query string (the SSE/date-range views pass none
    today, but the seam is there). ``notify_on_402`` lets a background caller raise the
    "subscription ended" notification while an on-demand view stays silent.
    """
    api_url = entry.data.get(CONF_GRIDLENS_API_URL, "https://api.gridlens.au")
    params: dict = {
        "state": entry.data.get(CONF_STATE, "NSW"),
        "current_plan": entry.data.get("current_plan", ""),
        "network": entry.data.get(CONF_DISTRIBUTOR, ""),
    }
    if extra_params:
        params.update(extra_params)

    status: int | None = None
    try:
        session = async_get_clientsession(hass)
        async with session.get(
            f"{api_url}/plans",
            params=params,
            headers={
                "X-API-Key": entry.data.get(CONF_GRIDLENS_API_KEY, ""),
                "User-Agent": "GridLens-HA-Integration/1.0",
            },
            timeout=_HTTP_TIMEOUT,
        ) as resp:
            status = resp.status
            if resp.status == 200:
                payload = await resp.json()
                plans = payload.get("plans", {}) or {}
                network_operators = payload.get("network_operators", {}) or {}
                tier = payload.get("tier")
                if plans:
                    await _store(hass, entry).async_save(
                        {
                            "saved_at": dt_util.utcnow().isoformat(),
                            "plans": plans,
                            "network_operators": network_operators,
                            "tier": tier,
                        }
                    )
                    persistent_notification.async_dismiss(
                        hass, _SUBSCRIPTION_NOTIFICATION_ID
                    )
                    _LOGGER.debug(
                        "Fetched %d plan(s) from API (tier=%s)", len(plans), tier
                    )
                    return PlanFetch(plans, network_operators, tier, "live", status)
                _LOGGER.warning("API /plans returned 200 but no plans; trying cache")
            elif resp.status == 402:
                body = (await resp.text())[:200]
                _LOGGER.warning(
                    "API /plans returned 402 (subscription ended): %s", body
                )
                if notify_on_402:
                    persistent_notification.async_create(
                        hass,
                        "Your Grid Lens subscription has ended and the dashboard cannot "
                        "show plan data. Please resubscribe at **gridlens.au/pricing** or "
                        "reconfigure the integration to restore your original plan.",
                        title="Grid Lens: Subscription Ended",
                        notification_id=_SUBSCRIPTION_NOTIFICATION_ID,
                    )
                # An ended subscription is a real state, not a blip — don't paper over it
                # with a stale cache.
                return PlanFetch(status=status, source="empty")
            else:
                body = (await resp.text())[:200]
                _LOGGER.warning("API /plans returned %s: %s", resp.status, body)
    except Exception as exc:  # noqa: BLE001 — any failure falls through to the cache
        _LOGGER.warning("Could not fetch plan data from API: %s", exc)

    # Live fetch failed (non-200 or exception) — serve the last good payload if fresh.
    cached = await _store(hass, entry).async_load()
    if cached and cached.get("plans"):
        saved_at = dt_util.parse_datetime(cached.get("saved_at") or "")
        age = dt_util.utcnow() - saved_at if saved_at else None
        if age is None or age <= CACHE_MAX_AGE:
            _LOGGER.warning(
                "Serving %d cached plan(s) after a failed /plans fetch (cache age: %s)",
                len(cached["plans"]),
                _fmt_age(age),
            )
            return PlanFetch(
                cached["plans"],
                cached.get("network_operators", {}) or {},
                cached.get("tier"),
                "cache",
                status,
                age,
            )
        _LOGGER.warning(
            "Plan cache is stale (%s old, max %s) — not using it",
            _fmt_age(age),
            CACHE_MAX_AGE,
        )

    return PlanFetch(status=status, source="empty")


def _fmt_age(age: timedelta | None) -> str:
    if age is None:
        return "unknown"
    hours = age.total_seconds() / 3600
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"
