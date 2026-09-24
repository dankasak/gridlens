"""Kick the advisory optimizer (AdvisoryCoordinator) to re-run immediately after a user
changes a value it plans against, instead of waiting for its normal ~2 min tick.

Call this from every write path that feeds the LP: the min-export-price entity, and the
shared stores behind charge targets, Daily Target, Today Boost, deferrable schedules and
Force On/Off overrides (see each store's own async_set/async_clear for why the entity AND
service write paths both land there, so hooking the store catches both automatically).

``DataUpdateCoordinator.async_request_refresh()`` already debounces: a call made while a
refresh is in flight collapses into that run rather than queuing a second one, so callers
never need to check "is it already running" themselves.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import DOMAIN


def request_reoptimize(hass: HomeAssistant, entry_id: str) -> None:
    # Never let this fire-and-forget nudge break the caller's own write (same "never raise
    # on a side-effect" discipline as every actuation path in this integration) — a `hass`
    # missing `.data` (e.g. a lightweight test double, or a setup-ordering edge case) just
    # means no reoptimize happens, not that the store write or entity state push fails.
    advisory = getattr(hass, "data", {}).get(DOMAIN, {}).get(f"{entry_id}_advisory")
    if advisory is None or not hasattr(advisory, "async_request_refresh"):
        return
    # Daily Target / Today Boost are only re-read from their stores inside the
    # coordinator's own _refresh_meta(), which is normally throttled to once per
    # META_REFRESH (~2 min) — invisible under the old periodic-only tick (same cadence
    # as the throttle), but without this an immediate re-run right after one of those
    # changes would solve against stale deferrable params and look like nothing
    # happened. See AdvisoryCoordinator.invalidate_meta()'s docstring.
    invalidate_meta = getattr(advisory, "invalidate_meta", None)
    if invalidate_meta is not None:
        invalidate_meta()
    entry = getattr(advisory, "entry", None)
    coro = advisory.async_request_refresh()
    if entry is not None and hasattr(entry, "async_create_background_task"):
        entry.async_create_background_task(hass, coro, name="grid_lens_reoptimize_now")
    else:  # pragma: no cover — defensive fallback, entry is always set in practice
        hass.async_create_task(coro)
