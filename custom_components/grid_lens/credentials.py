"""Local mirror of the Grid Lens API credentials, so a reinstall can recover silently.

Deliberately its own module rather than living in `__init__.py`: the config flow needs
these helpers, and importing the package root from inside the flow drags in the whole
integration (coordinator, optimiser, platform setup) at form-submit time. That import
raises in any environment where the package root isn't already loaded, and the flow's
broad `except Exception` turns it into a misleading `cannot_connect` on the last screen.

Why a local mirror is enough to close the reinstall gap: `/register` is keyed on the HA
installation UUID and the API stores only a hash, so it cannot hand the key back. But a
409 can only happen when `.storage` survived — HA keeps the installation UUID in
`.storage/core.uuid`, so wiping `.storage` regenerates it and `/register` just returns
200. Same UUID implies same `.storage`, which implies this store is still present.

No new secret exposure: the key already sits in plaintext in `.storage/core.config_entries`.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

# NOT suffixed with the config entry id (unlike grid_lens_advisory_* and friends): the
# point is to be readable by the config flow, before any entry exists, after the previous
# entry was removed. Nothing deletes it — the integration defines no async_remove_entry.
STORAGE_KEY = "grid_lens_credentials"
STORAGE_VERSION = 1


async def async_save_credentials(
    hass: HomeAssistant,
    *,
    ha_uuid: str | None,
    api_key: str | None,
    email: str | None,
    api_url: str | None,
) -> None:
    """Mirror the API credentials. Never raises — mirroring must not break setup."""
    if not ha_uuid or not api_key:
        return
    try:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        await store.async_save({
            "ha_installation_id": ha_uuid,
            "api_key": api_key,
            "email": email,
            "api_url": api_url,
        })
    except Exception:
        _LOGGER.debug("Could not write the credential recovery store", exc_info=True)


async def async_load_credentials(hass: HomeAssistant, ha_uuid: str | None) -> dict | None:
    """Return mirrored credentials, but only if they belong to *this* installation.

    The UUID check stops a backup restored onto a different machine (which gets a fresh
    installation UUID) from presenting the original machine's key as its own.
    """
    try:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        data = await store.async_load()
    except Exception:
        _LOGGER.debug("Could not read the credential recovery store", exc_info=True)
        return None
    if not data or not data.get("api_key"):
        return None
    if ha_uuid and data.get("ha_installation_id") not in (None, ha_uuid):
        return None
    return data
