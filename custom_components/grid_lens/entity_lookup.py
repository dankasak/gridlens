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

import re
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


def _norm(s: str) -> str:
    """Letters and digits only, lowercased — for comparing two spellings of one name."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


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

# Entity names that describe the *entity's role on a device* rather than the appliance.
# Integrations that set `has_entity_name = True` (Meross, Shelly, Tasmota, TP-Link, most
# modern smart-plug/switch platforms) put the appliance identity in the DEVICE name and
# give each entity a bare noun — device "Pool" + entity "Outlet", rendered by HA as
# "Pool Outlet". Taking `original_name` alone there yields "Outlet", which names nothing.
#
# The metering words are here as well as in _NAME_SUFFIXES because that tuple only strips
# them as a *suffix on a longer name* ("Dishwasher Energy" → "Dishwasher"); a `has_entity_name`
# sensor's whole name is often the bare word ("Energy"), which no suffix match touches. A
# deferrable load configured with only an energy sensor and no control switch — the ordinary
# "I added the plug to the Energy Dashboard" shape — hits exactly that.
_GENERIC_ENTITY_NAMES = frozenset(
    {
        "outlet",
        "socket",
        "plug",
        "switch",
        "relay",
        "load",
        "output",
        "channel",
        "state",
        "status",
        "on/off",
        "toggle",
        "total",
        # Bare metering names (see above — not reachable via _NAME_SUFFIXES).
        "energy",
        "power",
        "consumption",
        "total consumption",
        "energy total",
        "power consumption",
    }
)

# A trailing channel index on a role noun: "Outlet 2", "Switch 0", "Power1", "Relay_3".
# Kept separate from the noun so the noun can be recognised as generic while the index is
# preserved — two channels of one power strip must not both resolve to the strip's name.
_INDEX_RE = re.compile(r"^(.*?)[\s_-]*(\d+)$")


def _trim_suffixes(name: str) -> str:
    for suffix in _NAME_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _role_noun_index(name: str) -> Optional[str]:
    """`""` if `name` is a bare role noun, the channel index if it's an indexed one, else None.

    `None` means "this name carries the appliance, use it as-is". The empty string and an
    index are distinguished because they want different fallbacks — see _registry_name.
    """
    stripped = name.strip()
    if stripped.lower() in _GENERIC_ENTITY_NAMES:
        return ""
    m = _INDEX_RE.match(stripped)
    if m and m.group(1).strip().lower() in _GENERIC_ENTITY_NAMES:
        return m.group(2)
    return None


def _device_name(hass: HomeAssistant, ent) -> Optional[str]:
    """The appliance name from the device registry (user rename first), if any."""
    if ent is None or not ent.device_id:
        return None
    from homeassistant.helpers import device_registry as dr

    dev = dr.async_get(hass).async_get(ent.device_id)
    if dev is None:
        return None
    return dev.name_by_user or dev.name or None


def _registry_name(hass: HomeAssistant, anchor: str, ent) -> Optional[str]:
    """``original_name``, promoted to the device name when it's only a role noun.

    ``original_name`` is populated from disk at startup, before the owning integration has
    published state, so it beats reading a live ``friendly_name`` that may not exist yet.
    But on a `has_entity_name` entity it is only the *entity's* half of the displayed name
    — see _GENERIC_ENTITY_NAMES. Three outcomes:

    * A name that carries the appliance ("Smart Load 01 Total Consumption") is used as-is, so
      a multi-appliance device (one Sigenergy plant hosting several smart loads) names each
      load distinctly instead of collapsing them all onto the plant's name.
    * A bare role noun ("Outlet", "Energy") or an absent name becomes the **device** name
      ("Pool") — not HA's full concatenation ("Pool Outlet"), which reads like a wiring
      diagram rather than an appliance.
    * An *indexed* role noun ("Outlet 2", "Switch 0") keeps HA's concatenation ("Power Strip
      Outlet 2"). Here the index is the only thing distinguishing two loads on one device, so
      dropping to the bare device name would give a multi-channel power strip two deferrable
      loads with the same name.

    There is a fourth outcome before any of those: a **meter whose own name already starts
    with its device's name** is just that device's measurement, however the integration
    chose to word the rest, so the device name is the whole answer. ECHONET writes
    "Julian's Aircon Measured cumulative power consumption"; _NAME_SUFFIXES can only strip
    phrases it has been told about, and the point of this product is not to accumulate a
    dictionary of every integration's wording. The structural test — energy/power
    device_class, plus `original_name` starting with the device name and continuing — says
    the remainder describes the reading without needing to know what it says. A name that
    does *not* start with the device name ("Smart Load 01 Total Consumption" on a
    "Sigenergy Plant") is identifying one appliance among several and is left alone.
    """
    if ent is None:
        return None
    if (ent.device_class or ent.original_device_class) in ("energy", "power"):
        device = _device_name(hass, ent)
        orig = _norm(ent.original_name or "")
        if device and orig.startswith(_norm(device)) and orig != _norm(device):
            return device
    trimmed = _trim_suffixes(ent.original_name or "")
    index = _role_noun_index(trimmed) if trimmed else ""
    if index is None:
        return trimmed
    device = _device_name(hass, ent)
    if not device:
        return trimmed or (ent.original_name or None)
    return f"{device} {trimmed}".strip() if index else device


def resolve_device_name(
    hass: HomeAssistant,
    *anchors: Optional[str],
    dashboard_names: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """A clean display name for a deferrable load, resolved reliably at setup time.

    Anchors are the entities that identify the load (e.g. the control switch first — it
    names the appliance — then the energy sensor). Resolution is **priority-major, not
    anchor-major**: each naming source is tried across *every* anchor before falling back to
    the next source, in this order:

    1. an explicit ``name`` override on a **control** anchor — the user renamed the thing
       that actuates the appliance, which is them naming the appliance;
    2. the Energy Dashboard's per-device label (``dashboard_names``, if the caller has one —
       this is where a user-facing rename like "Hot Water" actually lives, since it's set on
       the Energy Dashboard's individual-device tracker, not on the entity itself);
    2b. an explicit ``name`` override on a **sensor** anchor (see below — demoted, because a
       renamed meter usually describes the reading, not the appliance);
    3. the entity/device registry name (see _registry_name);
    4. the live ``friendly_name``;
    5. a humanized object_id.

    The per-source sweep matters: looping anchor-major instead let a *lower*-priority source
    on an earlier anchor beat a higher-priority one later, so a control switch whose registry
    name was the generic "Outlet" short-circuited the "Pool" the user had typed on the Energy
    Dashboard against the same load's energy sensor (2026-10-23). Trailing metering words are
    trimmed so a "… Energy" sensor doesn't make the load read "Foo Energy".

    Two things changed on 2026-11-13, after this rig's ECHONET heat pump — whose energy
    counter carries the override "Measured cumulative power consumption" — became the whole
    load's identity (display name, greedy sensor, generated estimator entity_id) the moment
    its control entity stopped carrying a name of its own:

    * **An override that is only the tail of ``original_name`` is not a rename** — see
      _override. This is the part that actually fixes it, because it holds whether or not
      the install has an Energy Dashboard. Demoting the sensor override (below) does not:
      it only worked here because this rig happens to track that sensor on the Energy
      Dashboard under the right label, and left three of the four call-site anchor shapes
      still resolving to the meter's name.
    * **A rename on a sensor anchor is demoted below the dashboard label.** A rename on the
      control entity is the user naming the appliance and still wins outright, but a renamed
      *meter* usually describes the reading, and an explicit Energy Dashboard label is
      appliance-level by construction. Kept as defence in depth for a meter rename that is
      genuinely novel text yet still describes a reading.

    Both keep the 2026-10-23 "Outlet" case and the deliberate rename-beats-dashboard case
    working, and an install with no Energy Dashboard behaves exactly as before."""
    reg = er.async_get(hass)
    dashboard_names = dashboard_names or {}
    anchors = tuple(a for a in anchors if a)
    ents = {a: reg.async_get(a) for a in anchors}

    def _live(anchor: str) -> Optional[str]:
        st = hass.states.get(anchor)
        return st.attributes.get("friendly_name") if st is not None else None

    def _override(anchor: str) -> Optional[str]:
        """``ent.name``, unless the override only repeats the tail of ``original_name``.

        HA's rename dialog prefills the *entity's own* half of the displayed name, so
        opening it on a `has_entity_name` entity and saving stores an override that is
        byte-identical to `original_name` with the device-name prefix removed. Nobody
        named anything: the dialog was opened. Treating that as a deliberate rename is
        what turned this rig's ECHONET heat pump into "Measured cumulative power
        consumption" — `original_name` was "Julian's Aircon Measured cumulative power
        consumption", and the stored override was its last four words.

        The test is deliberately narrow: the override must be a **suffix** of
        `original_name` (normalised), which is the one shape a dropped prefix can
        produce. Stripping a prefix can only remove appliance identity, never add it, so
        there is nothing to lose by falling through to the registry name. A rename that
        is *not* that tail — "Filter Pump" over "Outlet", "Dishwasher" over "Smart Plug 4
        Energy" — is the user genuinely naming the appliance and still wins outright.
        """
        ent = ents[anchor]
        if ent is None:
            return None
        name = (ent.name or "").strip()
        if not name:
            return None
        orig = _norm(ent.original_name or "")
        return None if orig and orig.endswith(_norm(name)) else name

    def _control_override(anchor: str) -> Optional[str]:
        """A rename on anything that actuates (switch/climate/number), never a meter."""
        return None if anchor.startswith("sensor.") else _override(anchor)

    def _sensor_override(anchor: str) -> Optional[str]:
        return _override(anchor) if anchor.startswith("sensor.") else None

    for source in (
        _control_override,
        lambda a: dashboard_names.get(a),
        _sensor_override,
        lambda a: _registry_name(hass, a, ents[a]),
        _live,
    ):
        for anchor in anchors:
            name = source(anchor)
            if name:
                return _trim_suffixes(name)
    # Last resort: humanize the first anchor's object_id.
    for anchor in anchors:
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


def resolve_forecast_power_sensor(hass: HomeAssistant) -> Optional[str]:
    """Find the solar forecast provider's live "power right now" entity by shape, not name.

    Matches the pattern shading_correction.py needs: a continuous power measurement of
    what the forecast provider currently predicts for THIS instant (as opposed to a
    day-ahead total) — recorded by the recorder alongside actual production, so the two
    can be compared hour-by-hour. Solcast's `solcast_solar` integration is the only
    provider on this install, but nothing here assumes that: any forecast sensor with a
    `device_class: power`, `state_class: measurement` and a P10/P90 confidence-interval
    pair (`estimate10`/`estimate90`, Solcast's attribute names, but generic enough that
    another provider using the same convention would also match) qualifies. Returns None
    — never guesses — when there's not exactly one match, so the caller falls back to
    a configured override or a documented default entity_id.
    """
    matches = []
    for state in hass.states.async_all("sensor"):
        attrs = state.attributes
        if attrs.get("device_class") != "power" or attrs.get("state_class") != "measurement":
            continue
        if "estimate10" in attrs and "estimate90" in attrs:
            matches.append(state.entity_id)
    if len(matches) == 1:
        return matches[0]
    return None
