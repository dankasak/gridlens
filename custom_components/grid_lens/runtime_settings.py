"""Config values that are live-adjustable via a dashboard entity rather than
buried in the config-flow options — see number.py's
GridLensMinExportPriceNumber. The entity is the authoritative live value once
it exists (RestoreEntity persists it across restarts); the numeric `default`
passed in here only covers the brief window before the entity has registered
(e.g. immediately after first setup).
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN


def get_live_number(hass: HomeAssistant, entry_id: str, unique_id_suffix: str, default: float) -> float:
    """Read a `number.*` entity's current value by its unique_id.

    Falls back to `default` if the entity hasn't registered yet, or its state
    isn't a parseable number (e.g. still "unknown" in the brief window right
    after startup before RestoreEntity resolves).
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("number", DOMAIN, f"{entry_id}_{unique_id_suffix}")
    if entity_id:
        state = hass.states.get(entity_id)
        if state is not None and state.state not in ("unknown", "unavailable"):
            try:
                return float(state.state)
            except ValueError:
                pass
    return default
