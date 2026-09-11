"""Best-effort brand detection for the modulating-load wizard's EV-charger step.

The wizard (`config_flow.py`'s `async_step_load_ev_brand`) is a convenience layer, not
a requirement — §6a's whole design already works against *any* charger integration that
exposes a charging-current `number` entity, with every field filled in by hand. This
module exists only to save that hunt when the charger happens to be one we recognise:
pick a brand, and if a matching entity is actually present on this Home Assistant
instance, pre-fill the setpoint / plug-sensor / button / switch fields the next two wizard
screens ask for. Every field it fills stays a normal, editable default — nothing here
writes a load's config directly, and a wrong or missing match just leaves the field
blank for manual entry, exactly like today.

Confidence varies per vendor and is tracked in `confirmed`:
  * `True`  — live entity IDs actually observed on a real device (currently: Wattpilot,
    confirmed 2026-09-11 against the household's own `ruaan-deysel/ha-wattpilot`
    install; see GRIDLENS_CHECKLIST.md that date).
  * `False` — a plausible pattern from the vendor's own integration source or published
    docs, never confirmed against real hardware. Getting one of these wrong is harmless
    (the form just shows a blank/wrong default the user overrides before saving) but is
    not the same claim as a live-confirmed match — don't upgrade one to `True` without
    an actual device to check it against, the same discipline this project applies to
    the OCPP flash-wear caution (OPEN_ITEMS.md) and to the hallucinated-repo-name lesson
    from earlier the same day (GRIDLENS_CHECKLIST.md, 2026-09-11, "Native Wattpilot vs
    OCPP" entry) — a plausible-looking claim about a piece of hardware is not the same
    as one anyone here has actually verified.

Deliberately free of Home Assistant imports at module scope (only `detect()` touches
`hass.states`, defensively) so the vendor table itself can be exercised offline.
"""
from __future__ import annotations

import re
from typing import Any

# Each vendor's `patterns` maps a wizard field name to a regex matched against the full
# entity_id (`domain.object_id`), anchored so an unrelated entity that merely contains
# the same word doesn't false-match. Field names line up 1:1 with what
# `async_step_load_control` / `async_step_load_modulating` already store on a load dict
# (`switch`, `setpoint`, `plug_sensor`, `start_button`, `stop_button`) — this module only
# ever *proposes* values for those same keys, never new ones.
EV_CHARGER_VENDORS: list[dict[str, Any]] = [
    {
        "id": "wattpilot",
        "label": "Fronius Wattpilot (ha-wattpilot)",
        "confirmed": True,
        "note": (
            "Live-confirmed 2026-09-11. No stateful switch — this charger's setpoint "
            "floor is 6 A, so wire the Start/Stop button pair on the next screen rather "
            "than a Control Entity switch."
        ),
        "patterns": {
            "setpoint": r"^number\..*_max_charging_current$",
            "plug_sensor": r"^sensor\..*_car_connected$",
            "start_button": r"^button\..*_start_charging$",
            "stop_button": r"^button\..*_stop_charging$",
        },
        "defaults": {"min_current": 6.0},
    },
    {
        "id": "sigenergy",
        "label": "Sigenergy AC Charger",
        "confirmed": False,
        "note": (
            "Pattern taken from this repo's own custom_components/sigen source "
            "(register key ac_charger_output_current, min [6, X]) — not live-confirmed, "
            "no AC charger is wired to this dev rig's plant."
        ),
        "patterns": {
            "setpoint": r"^number\..*ac_charger_output_current$",
            "switch": r"^switch\..*ac_charger_start_stop$",
        },
        "defaults": {"min_current": 6.0},
    },
    {
        "id": "ocpp",
        "label": "OCPP (lbbrhzn/ocpp)",
        "confirmed": False,
        "note": "Pattern per docs.html's existing claim; unverified against real hardware.",
        "patterns": {"setpoint": r"^number\..*_maximum_current$"},
        "defaults": {"min_current": 6.0},
    },
    {
        "id": "easee",
        "label": "Easee",
        "confirmed": False,
        "note": "Pattern per strings.json's existing claim; unverified against real hardware.",
        "patterns": {"setpoint": r"^number\..*dynamic_charger_limit$"},
        "defaults": {"min_current": 6.0},
    },
    {
        "id": "wallbox",
        "label": "Wallbox",
        "confirmed": False,
        "note": "Pattern per strings.json's existing claim; unverified against real hardware.",
        "patterns": {"setpoint": r"^number\..*maximum_charging_current$"},
        "defaults": {"min_current": 6.0},
    },
    {
        "id": "zaptec",
        "label": "Zaptec",
        "confirmed": False,
        "note": (
            "Pattern from ha-zaptec-community/ha-zaptec's published docs (2026-09-11 web "
            "search); unverified against real hardware."
        ),
        "patterns": {"setpoint": r"^number\..*available_current$"},
        "defaults": {"min_current": 6.0},
    },
    {
        "id": "goe",
        "label": "go-eCharger",
        "confirmed": False,
        "note": (
            "Several independent go-eCharger integrations exist (MQTT, cloud v2, custom "
            "component) with likely-differing entity IDs; this pattern is a best-effort "
            "common suffix from 2026-09-11 web search, unverified against real hardware."
        ),
        "patterns": {"setpoint": r"^number\..*max_current$"},
        "defaults": {"min_current": 6.0},
    },
    {
        "id": "openevse",
        "label": "OpenEVSE",
        "confirmed": False,
        "note": (
            "Pattern from the core openevse integration's published \"Charge rate\" "
            "number entity (2026-09-11 web search); unverified against real hardware."
        ),
        "patterns": {"setpoint": r"^number\..*charge_rate$"},
        "defaults": {"min_current": 6.0},
    },
]

# No reliable, generic entity-naming convention was found for Tesla's integrations
# (examples seen were per-install custom names, not a stable suffix) — it stays a
# documented "known-good shape" in docs.html but isn't offered here, rather than
# guessing a pattern with nothing behind it. Add it once a real pattern is confirmed.

_OTHER = {"id": "other", "label": "Other / not listed — I'll pick the entities myself"}


def _entity_ids(hass) -> list[str]:
    """All known entity_ids, or [] if `hass.states` doesn't support the lookup (e.g. the
    offline test stubs) — detection is a convenience, never a hard requirement."""
    try:
        return list(hass.states.async_entity_ids())
    except Exception:
        return []


def detect(hass, vendor_id: str) -> dict[str, str]:
    """First matching entity_id per field for `vendor_id`, or {} if unknown/no match."""
    vendor = next((v for v in EV_CHARGER_VENDORS if v["id"] == vendor_id), None)
    if not vendor:
        return {}
    entity_ids = _entity_ids(hass)
    if not entity_ids:
        return {}
    found: dict[str, str] = {}
    for field, pattern in vendor["patterns"].items():
        rx = re.compile(pattern)
        match = next((eid for eid in entity_ids if rx.match(eid)), None)
        if match:
            found[field] = match
    return found


def vendor_options(hass) -> list[dict[str, str]]:
    """SelectSelector options, each flagged when a live match was actually found on this
    Home Assistant instance and when the underlying pattern is itself unverified."""
    options = [dict(_OTHER)]
    for vendor in EV_CHARGER_VENDORS:
        label = vendor["label"]
        if detect(hass, vendor["id"]).get("setpoint"):
            label += " — detected on this system"
        elif not vendor["confirmed"]:
            label += " (unverified pattern)"
        options.append({"value": vendor["id"], "label": label})
    return options


def vendor_by_id(vendor_id: str) -> dict[str, Any] | None:
    return next((v for v in EV_CHARGER_VENDORS if v["id"] == vendor_id), None)
