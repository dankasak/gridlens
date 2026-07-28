"""Entity-registry helpers for discovering the sensors that belong with a deferrable load.

When a user picks a control switch (or just configures a deferrable device's energy sensor),
the *real-time power* sensor that measures that same appliance is almost always a sibling
entity on the same HA device — a smart plug exposes on/off + current-power + energy together.
`resolve_power_sensor` finds it so the power-flow card can show live consumption without the
user hand-picking yet another entity.

The tricky case is a big multi-function device (e.g. a Sigenergy plant) where dozens of power
sensors share ONE device — a naive "first power sibling" would grab the wrong one. So with
several candidates we pick the one whose entity_id shares the longest prefix with the anchor
(`…smart_load_1_total_consumption` → `…smart_load_1_power`), and refuse to guess (return None)
when nothing meaningfully matches. A device with a single power sensor (the smart-plug case)
is returned directly.
"""
from __future__ import annotations

from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

# Below this shared-prefix length (chars) a match on a multi-power-sensor device is treated
# as coincidental (e.g. only the integration prefix "sigen_plant_" overlaps) — better to
# return nothing than to surface an unrelated power reading.
_MIN_PREFIX_MATCH = 6


async def async_get_energy_dashboard_names(hass: HomeAssistant) -> dict[str, str]:
    """Map each Energy Dashboard "individual device" consumption sensor to its user-set label.

    This is the only place a rename like "Hot Water" actually lives — the Energy Dashboard
    stores it against the tracked sensor, not on the entity itself. Best-effort: an empty dict
    just means callers fall back to the entity's own name."""
    try:
        from homeassistant.components.energy import data as energy_data

        manager = await energy_data.async_get_manager(hass)
        if not manager.data:
            return {}
        return {
            dev["stat_consumption"]: dev["name"]
            for dev in manager.data.get("device_consumption", [])
            if dev.get("stat_consumption") and dev.get("name")
        }
    except Exception:  # noqa: BLE001 — naming is best-effort, never break the sensor
        return {}


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def _power_sensors_on_device(reg, device_id: str) -> list[str]:
    out = []
    for ent in er.async_entries_for_device(reg, device_id, include_disabled_entities=False):
        if not ent.entity_id.startswith("sensor."):
            continue  # only measurement sensors, never a number.* setpoint
        dc = ent.device_class or ent.original_device_class
        if dc == "power":
            out.append(ent.entity_id)
    return out


_NAME_SUFFIXES = (" Total Consumption", " Consumption", " Energy", " Power")


def resolve_device_name(
    hass: HomeAssistant,
    *anchors: Optional[str],
    dashboard_names: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """A clean display name for a deferrable load, resolved reliably at setup time.

    Tries each anchor (e.g. the control switch first — it names the appliance — then the
    energy sensor), in priority order: the entity registry's explicit ``name`` override, then
    the Energy Dashboard's per-device label (``dashboard_names``, if the caller has one — this
    is where a user-facing rename like "Hot Water" actually lives, since it's set on the Energy
    Dashboard's individual-device tracker, not on the entity itself), then the registry's
    ``original_name`` (populated from disk at startup, before the owning integration has
    published state, so it beats reading a live ``friendly_name`` that may not exist yet), then
    the live ``friendly_name``, then a humanized object_id. Trailing metering words are trimmed
    so a "… Energy" sensor doesn't make the load read "Foo Energy"."""
    reg = er.async_get(hass)
    dashboard_names = dashboard_names or {}
    for anchor in anchors:
        if not anchor:
            continue
        name = None
        ent = reg.async_get(anchor)
        if ent is not None:
            name = ent.name
        if not name:
            name = dashboard_names.get(anchor)
        if not name and ent is not None:
            name = ent.original_name
        if not name:
            st = hass.states.get(anchor)
            if st is not None:
                name = st.attributes.get("friendly_name")
        if not name:
            continue
        for suffix in _NAME_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return name
    # Last resort: humanize the first anchor's object_id.
    for anchor in anchors:
        if anchor:
            return anchor.split(".", 1)[-1].replace("_", " ").title()
    return None


def resolve_power_sensor(hass: HomeAssistant, *anchors: Optional[str]) -> Optional[str]:
    """Best real-time power (W/kW) sensor for the device behind any of ``anchors``.

    Anchors are tried in order (e.g. the configured energy sensor first — semantically closest
    to the appliance's power — then the control switch). Returns the sensor's entity_id, or
    ``None`` if no device/power sensor can be confidently matched.
    """
    reg = er.async_get(hass)
    for anchor in anchors:
        if not anchor:
            continue
        entry = reg.async_get(anchor)
        if entry is None or not entry.device_id:
            continue
        cands = _power_sensors_on_device(reg, entry.device_id)
        if not cands:
            continue
        if len(cands) == 1:
            return cands[0]
        anchor_obj = anchor.split(".", 1)[-1]
        best = max(cands, key=lambda c: _common_prefix_len(anchor_obj, c.split(".", 1)[-1]))
        if _common_prefix_len(anchor_obj, best.split(".", 1)[-1]) >= _MIN_PREFIX_MATCH:
            return best
        # Ambiguous multi-power-sensor device with no good name match — don't guess.
    return None
