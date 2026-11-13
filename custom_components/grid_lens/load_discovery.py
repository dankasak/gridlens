"""Device-first discovery of every entity one deferrable load needs.

The config flow used to ask for each entity in turn — energy sensor, then control
entity, then setpoint, then SOC sensor — each from a global picker listing every
entity in the house. That shape cannot be made safe: the pickers show *friendly
names*, so an entity whose name was edited to say "Julian's Aircon switch" is
indistinguishable from the real thing, and on 2026-11-12 exactly that happened —
a deferrable load ended up metering and controlling the kitchen dishwasher's plug
while its energy counter pointed at an air conditioner on the other side of the
house (`docs/GRIDLENS_CHECKLIST.md`, 2026-11-13).

This module inverts it. The user picks **one appliance** — an HA *device*, which
is a physical thing an integration vouches for — and everything else is read off
that device's own entities. A wrong appliance is then visible ("Dishwasher") rather
than hiding behind a name someone typed, and entities belonging to two different
appliances can no longer be combined into one load by accident.

Every rule here is a property of the HA data model, never a name match:

* **`entity_category`** separates an appliance's real control from its settings.
  A TP-Link plug exposes `switch.<plug>` alongside `switch.…_led`,
  `switch.…_auto_off_enabled`, `switch.…_auto_update_enabled`; only the first has
  `entity_category is None`. Across the 13 appliance-shaped devices on the
  development rig this resolves the control entity to exactly one candidate on
  every single device — including preferring `climate.<aircon>` over the ECHONET
  `switch.…_power_saving_operation_setting` on all three heat pumps.
* **`device_class` + `state_class`** identify the energy counter
  (`energy` + `total`/`total_increasing`), the live power sensor
  (`power` + `measurement`) and a device battery (`battery`).
* **The Energy Dashboard's own device list** breaks ties. When a device hosts
  several energy counters — one Sigenergy plant hosts fourteen, one per smart load
  — the counters the user put on their Energy Dashboard are the ones that name
  real appliances, so those win, and if several are tracked the caller is asked to
  choose between *appliances*, not between raw entities.

Nothing is guessed. `discover_wiring` reports what it could not resolve in
`unresolved`/`notes` so the flow can ask one targeted question instead of falling
back to asking everything.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .deferrable_loads import DECLARED, ESTIMATED, MONITORED
from .entity_lookup import resolve_device_name, resolve_power_sensor

# Energy counters: a cumulative total, not a "power right now" reading.
_ENERGY_STATE_CLASSES = frozenset({"total", "total_increasing"})

# At or above this many energy counters on one device, treat it as a multi-circuit
# device rather than a single appliance (see discover_wiring). Two is ordinary — a
# smart plug exposing today's and this month's totals — so the threshold sits above
# that; the Sigenergy plant on the development rig has fourteen.
_MULTI_APPLIANCE_ENERGY_CANDS = 4

# A modulating device's setpoint is a number.* in amps or watts (an EV charger's
# max charging current, a battery's charge-power limit). The unit decides how the
# flow stores it (CONF_DEFERRABLE_LOAD_SETPOINT_UNIT) — mA/MW aren't used by any
# charger we've seen and are deliberately not guessed at.
_SETPOINT_UNITS = {"A": "a", "W": "w", "kW": "kw"}


def _state_class(hass: HomeAssistant, ent) -> Optional[str]:
    """`state_class`, from the registry's capabilities if present, else live state.

    The registry copy is populated from disk at startup and survives an integration
    that hasn't published state yet; the live read covers an entity whose platform
    sets state_class dynamically.
    """
    caps = getattr(ent, "capabilities", None) or {}
    sc = caps.get("state_class")
    if sc:
        return str(sc)
    st = hass.states.get(ent.entity_id)
    if st is None:
        return None
    sc = st.attributes.get("state_class")
    return str(sc) if sc else None


def _device_class(hass: HomeAssistant, ent) -> Optional[str]:
    dc = ent.device_class or ent.original_device_class
    if dc:
        return str(dc)
    st = hass.states.get(ent.entity_id)
    if st is None:
        return None
    dc = st.attributes.get("device_class")
    return str(dc) if dc else None


def _unit(hass: HomeAssistant, ent) -> Optional[str]:
    unit = getattr(ent, "unit_of_measurement", None)
    if unit:
        return str(unit)
    st = hass.states.get(ent.entity_id)
    if st is None:
        return None
    unit = st.attributes.get("unit_of_measurement")
    return str(unit) if unit else None


def _is_visible(ent) -> bool:
    """Not hidden and not disabled — the bar for a *measurement* to be usable."""
    return not getattr(ent, "hidden_by", None) and not getattr(ent, "disabled_by", None)


def _is_actuator(ent) -> bool:
    """True for an entity that actuates the appliance itself.

    `entity_category` is HA's own marker for "this entity configures or diagnoses
    the device rather than being it" (`config`/`diagnostic`), set by the
    integration, not by the user — which is what makes it trustworthy here.

    Applied to **controls and setpoints only, never to sensors.** TP-Link's plugs
    categorise their own energy counters as `diagnostic`
    (`sensor.<plug>_today_s_consumption`, `…_this_month_s_consumption`), so demanding
    an uncategorised energy sensor would report every TP-Link-metered appliance as
    having no meter at all and quietly downgrade it from monitored to estimated
    (caught on the development rig, 2026-11-13). A measurement only has to be
    visible; what it measures is already established by its device_class.
    """
    return _is_visible(ent) and getattr(ent, "entity_category", None) is None


# Attributes through which an entity declares that it acts on OTHER entities: HA
# climate/switch groups (`entity_id`), this install's climate_scheduler schedule
# switches (`entities`), and the aggregator shape the Power Flow card already
# excludes from its Aircon node (`member_entities`). Matched by shape — a list of
# entity-id-looking strings — not by attribute name alone, since `entities` is a
# common enough key that a real appliance might use it for something else.
_WRAPPER_ATTRS = ("member_entities", "entities", "entity_id")


def _is_group(hass: HomeAssistant, ent) -> bool:
    """A wrapper around other entities rather than a real appliance.

    Generic on purpose: any HA group is excluded the same way, not just the
    `climate_scheduler` entities that made this necessary here. `_resolveAirconUnits`
    in the Power Flow card excludes its Aircon node's members by the same test. The
    point is that actuating a wrapper means Grid Lens fights whatever the wrapper is
    itself actuating — picking `switch.schedule_dans_aircon` would toggle a schedule,
    not an air conditioner, and the load would look configured while controlling
    nothing.
    """
    st = hass.states.get(ent.entity_id)
    if st is None:
        return False
    for attr in _WRAPPER_ATTRS:
        value = st.attributes.get(attr)
        if isinstance(value, (list, tuple)) and value and all(
            isinstance(v, str) and "." in v for v in value
        ):
            return True
    return False


def _same_appliance(a: str, b: str) -> bool:
    """Whether two labels plausibly name one appliance.

    Compared on letters and digits alone, so spelling and spacing tidy-ups
    ("WestinghouseFridge" vs "Westinghouse Fridge") and one name containing the other
    ("Julian's Aircon" vs "Julian's Aircon switch") count as the same thing, while
    "Julian's Aircon switch" vs "Dishwasher" does not.
    """
    na = "".join(ch for ch in a.lower() if ch.isalnum())
    nb = "".join(ch for ch in b.lower() if ch.isalnum())
    if not na or not nb:
        return True  # nothing to disagree about
    return na in nb or nb in na


def _entities_for_device(hass: HomeAssistant, device_id: str) -> list[Any]:
    reg = er.async_get(hass)
    return list(
        er.async_entries_for_device(reg, device_id, include_disabled_entities=False)
    )


def _domain(ent) -> str:
    return ent.entity_id.split(".", 1)[0]


async def async_dashboard_stats(hass: HomeAssistant) -> dict[str, str]:
    """Energy Dashboard individual-device trackers: `{energy sensor: label}`.

    Re-exported from entity_lookup so a caller needs only this module. The labels
    are what the user typed on the dashboard ("Hot Water"), which is also the best
    appliance name available for a counter whose own entity name is a bare noun.
    """
    from .entity_lookup import async_get_energy_dashboard_names

    return await async_get_energy_dashboard_names(hass)


def _pick_energy(
    hass: HomeAssistant, ents: Iterable[Any], dashboard: dict[str, str]
) -> tuple[Optional[str], list[str]]:
    """The device's energy counter, plus every other counter that could have been it.

    A single counter is returned outright. With several, the ones on the Energy
    Dashboard win — a multi-appliance device (one inverter hosting several
    monitored circuits) has one tracker per real appliance, so those are the
    meaningful choices and the rest are totals/sub-readings. Returns `(None, cands)`
    when the choice is genuinely the user's, so the flow asks about appliances
    rather than silently taking the first.
    """
    cands = [
        e.entity_id
        for e in ents
        if _domain(e) == "sensor"
        and _is_visible(e)
        and _device_class(hass, e) == "energy"
        and (_state_class(hass, e) in _ENERGY_STATE_CLASSES)
    ]
    if len(cands) <= 1:
        return (cands[0] if cands else None), cands
    tracked = [c for c in cands if c in dashboard]
    if len(tracked) == 1:
        return tracked[0], cands
    # Several tracked counters (or none): the user picks, from the tracked subset
    # when there is one — those at least name appliances.
    return None, (tracked or cands)


def _pick_control(
    hass: HomeAssistant, ents: Iterable[Any]
) -> tuple[Optional[str], list[str]]:
    """The entity Grid Lens turns on and off, plus the other primary candidates.

    `climate.*` beats `switch.*`: on a heat pump the climate entity is the appliance
    (and is what the Power Flow card's aggregated Aircon node groups by — a load
    controlled through a plug instead gets its own node *and* is still counted
    inside the aircon aggregate, i.e. drawn twice). A device with one primary
    switch and no climate entity resolves to that switch.
    """
    primary = [
        e for e in ents
        if _domain(e) in ("switch", "climate") and _is_actuator(e) and not _is_group(hass, e)
    ]
    climate = [e.entity_id for e in primary if _domain(e) == "climate"]
    if len(climate) == 1:
        return climate[0], [e.entity_id for e in primary]
    if climate:
        return None, climate
    ids = [e.entity_id for e in primary]
    if len(ids) == 1:
        return ids[0], ids
    return None, ids


def _pick_setpoint(hass: HomeAssistant, ents: Iterable[Any]) -> tuple[Optional[str], str]:
    """A `number.*` in A/W/kW — what makes a load modulating rather than on/off."""
    for e in ents:
        if _domain(e) != "number" or not _is_actuator(e):
            continue
        unit = _unit(hass, e)
        if unit in _SETPOINT_UNITS:
            return e.entity_id, _SETPOINT_UNITS[unit]
    return None, ""


def _pick_soc(hass: HomeAssistant, ents: Iterable[Any]) -> Optional[str]:
    for e in ents:
        if (
            _domain(e) == "sensor"
            and _is_visible(e)
            and _device_class(hass, e) == "battery"
        ):
            return e.entity_id
    return None


def discover_wiring(
    hass: HomeAssistant,
    *,
    device_id: Optional[str] = None,
    control_entity: Optional[str] = None,
    dashboard: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Everything one appliance needs, read off its HA device.

    Pass `device_id` for a real device, or `control_entity` alone for a device-less
    entity (a SmartIR climate entity, a template switch — these have no siblings to
    discover, so only the name and kind come back).

    The returned `kind` is **derived, never asked**: an energy counter makes a load
    monitored, a controllable with no counter makes it estimated, and neither makes
    it declared (forecast-only). `unresolved` names each slot the caller must ask
    about, with the candidates to offer; `notes` carries anything a reviewer should
    see before committing.
    """
    dashboard = dashboard or {}
    out: dict[str, Any] = {
        "device_id": device_id,
        "energy": None,
        "power": None,
        "control": None,
        "control_domain": None,
        "setpoint": None,
        "setpoint_unit": "",
        "soc": None,
        "unresolved": {},
        "notes": [],
    }

    if device_id:
        ents = _entities_for_device(hass, device_id)
        energy, energy_cands = _pick_energy(hass, ents, dashboard)
        control, control_cands = _pick_control(hass, ents)
        setpoint, setpoint_unit = _pick_setpoint(hass, ents)
        # A device hosting MANY energy counters is not one appliance — it is an
        # inverter, a plant, a multi-circuit meter. Its single "primary" switch then
        # belongs to the device as a whole, not to the circuit being configured: the
        # Sigenergy plant handed a hot-water smart load `switch.…_plant_remote_ems`
        # and `number.…_plant_max_charging_limit`, i.e. the battery's EMS switch and
        # charge-power limit, which Grid Lens would have driven believing it was
        # heating water. Prefix matching cannot separate them (every entity shares the
        # integration's own long prefix), so nothing is assumed: the control is asked
        # about, and "nothing — forecast only" is a legitimate answer, which is exactly
        # how this install has had that load configured all along.
        if len(energy_cands) >= _MULTI_APPLIANCE_ENERGY_CANDS:
            if control or control_cands:
                out["unresolved"]["control"] = control_cands or ([control] if control else [])
                out["notes"].append(
                    f"“{_device_name(hass, device_id) or 'This device'}” meters "
                    f"{len(energy_cands)} separate circuits, so what switches *this* one "
                    "cannot be worked out from the device — pick it, or leave it as "
                    "forecast-only."
                )
            control, setpoint, setpoint_unit = None, None, ""
        # Same reasoning for the SOC sensor: on a multi-circuit device it is the
        # *device's* battery, not this circuit's. The Sigenergy plant offered a hot
        # water load `sensor.…_plant_battery_soc`, which the optimiser would have read
        # as that load's own state of charge and capped its run time against.
        soc = (
            None if len(energy_cands) >= _MULTI_APPLIANCE_ENERGY_CANDS
            else _pick_soc(hass, ents)
        )
        out.update(
            energy=energy, control=control, setpoint=setpoint,
            setpoint_unit=setpoint_unit, soc=soc,
        )
        if energy is None and len(energy_cands) > 1:
            out["unresolved"]["energy"] = energy_cands
            out["notes"].append(
                f"{len(energy_cands)} energy counters on this device — pick the one "
                "that measures this appliance."
            )
        if control is None and len(control_cands) > 1:
            out["unresolved"]["control"] = control_cands
            out["notes"].append(
                f"{len(control_cands)} things here can be switched — pick the one that "
                "runs the appliance."
            )
    else:
        out["control"] = control_entity

    control = out["control"]
    if control:
        out["control_domain"] = control.split(".", 1)[0]

    # Live power: the same resolver the sensor platform uses at runtime, anchored on
    # entities of THIS device only, so it cannot reach across to another appliance's
    # meter the way a free-entity pick could.
    if out["energy"] or control:
        try:
            out["power"] = resolve_power_sensor(hass, out["energy"], control, out["setpoint"])
        except Exception:  # noqa: BLE001 — discovery is best-effort, never break the flow
            out["power"] = None

    out["device_name"] = _device_name(hass, device_id) if device_id else None
    out["name"] = (
        resolve_device_name(hass, control, out["energy"], dashboard_names=dashboard)
        or out["device_name"]
        or "Unnamed load"
    )
    # A *renamed control entity* that disagrees with its own device is the trap that
    # started all this: `switch.<plug>` on the device "Dishwasher", renamed to
    # "Julian's Aircon switch", was picked out of a global entity list and spent a day
    # configured as an air conditioner's control (2026-11-12). The device name is the
    # one label no amount of entity renaming can quietly change, so it is the thing to
    # show. Deliberately NOT fired for a name that merely differs — an Energy Dashboard
    # label ("Carport Lights" tracked as "EV Charger") or a tidied-up spelling
    # ("WestinghouseFridge" → "Westinghouse Fridge") is normal and warning about those
    # cried wolf on 5 of 18 appliances on this rig, which is how a real warning gets
    # ignored.
    if control and out["device_name"]:
        ent = er.async_get(hass).async_get(control)
        renamed = (getattr(ent, "name", None) or "").strip() if ent else ""
        if renamed and not _same_appliance(renamed, out["device_name"]):
            out["notes"].append(
                f"Check this one: the control entity has been renamed “{renamed}”, but it "
                f"belongs to the device “{out['device_name']}”. Those should be the same "
                "appliance — if they are not, this is the wrong device."
            )

    if out["energy"]:
        out["kind"] = MONITORED
    elif control or out["setpoint"]:
        out["kind"] = ESTIMATED
    else:
        out["kind"] = DECLARED
        out["notes"].append(
            "Nothing here to measure or switch — Grid Lens will plan around this "
            "appliance from a declared daily usage, but never actuate it."
        )

    if out["setpoint"] and out["control_domain"] == "switch":
        out["notes"].append(
            "This looks like a modulating device (it has an adjustable "
            f"{out['setpoint_unit'].upper() or 'power'} setpoint), so Grid Lens will ramp "
            "the setpoint rather than only switching it."
        )
    if out["control_domain"] == "climate":
        out["notes"].append(
            "Controlled through its climate entity, so it joins the Aircon group on the "
            "Power Flow card instead of getting a node of its own."
        )
    return out


def _device_name(hass: HomeAssistant, device_id: Optional[str]) -> Optional[str]:
    if not device_id:
        return None
    dev = dr.async_get(hass).async_get(device_id)
    if dev is None:
        return None
    return dev.name_by_user or dev.name or None


def discover_appliances(
    hass: HomeAssistant,
    *,
    exclude_entry_id: Optional[str] = None,
    taken: Iterable[str] = (),
    dashboard: Optional[dict[str, str]] = None,
) -> list[dict[str, str]]:
    """Every appliance a deferrable load could be built from, best candidates first.

    Three tiers, in order:

    1. devices holding an Energy Dashboard tracker — the user curated these, so they
       are real appliances by definition, whether or not anything can switch them
       (a hot water circuit metered by an inverter has no switch and still belongs);
    2. other devices with something to actuate;
    3. device-less controllables (SmartIR climate entities, template switches), which
       have no device to discover from and are labelled as entities so that is plain.

    Tier 2 deliberately requires an *actuator* rather than "any energy or power
    sensor". Measurement alone is not an appliance: on the development rig that
    looser rule listed the AEMO price feed, the Solcast forecast, six SigenStor PV
    strings and a Garmin account — 35 rows, most of them nonsense. A device with
    sensors but nothing to switch is only useful as a forecast-only load, and tier 1
    already covers that case for anything the user cares enough about to have put on
    their Energy Dashboard.

    Grid Lens's own entities are excluded via `exclude_entry_id` — its synthetic
    estimated-consumption sensors are not appliances, and listing them is how the
    old flow let a load be built on top of another load. The exclusion is matched on
    each **entity's** `config_entry_id`, not on the device registry's
    `config_entries`, which is unpopulated in this HA version's stored registry.
    """
    dashboard = dashboard or {}
    taken_ids = set(taken)
    reg = er.async_get(hass)

    # HA's own marker for a registry device that isn't a physical thing — a cloud
    # account, a forecast provider, an integration's service device. None of them is
    # an appliance that can be deferred.
    dev_reg = dr.async_get(hass)
    service_devices = {
        d.id for d in dev_reg.devices.values()
        if str(getattr(d, "entry_type", "") or "") == "service"
    }

    by_device: dict[str, list[Any]] = {}
    orphan_controls: list[Any] = []
    for ent in reg.entities.values():
        if getattr(ent, "disabled_by", None):
            continue
        if exclude_entry_id and getattr(ent, "config_entry_id", None) == exclude_entry_id:
            continue
        if ent.device_id:
            if ent.device_id in service_devices:
                continue
            by_device.setdefault(ent.device_id, []).append(ent)
        elif (
            _domain(ent) in ("switch", "climate")
            and _is_actuator(ent)
            and not _is_group(hass, ent)
        ):
            orphan_controls.append(ent)

    tier1: list[dict[str, str]] = []
    tier2: list[dict[str, str]] = []
    for device_id, ents in by_device.items():
        # An appliance already configured as a load is not offered again. Matched on
        # the device's entities rather than on the device itself, since that is how a
        # load identifies itself (by its energy sensor or control entity).
        if any(e.entity_id in taken_ids for e in ents):
            continue
        controls = [
            e for e in ents
            if _domain(e) in ("switch", "climate")
            and _is_actuator(e)
            and not _is_group(hass, e)
        ]
        tracked = [e.entity_id for e in ents if e.entity_id in dashboard]
        if not (tracked or controls):
            continue
        # An unmetered device whose control can't be resolved is not offerable: there is
        # nothing to measure and no way to tell which of its switches runs it. This is
        # what keeps "Sigenergy Plant Grid Code" — six grid-protection toggles, no meter
        # — out of a list of household appliances.
        if not tracked and len(controls) > 1 and not any(
            _domain(c) == "climate" for c in controls
        ):
            continue
        name = _device_name(hass, device_id) or "Unnamed device"
        bits = []
        # The Energy Dashboard label is worth showing only when it says something the
        # device name doesn't — "Carport Lights" tracked as "EV Charger" is exactly
        # the mismatch a reviewer needs to see before committing.
        label_from_dash = dashboard.get(tracked[0]) if tracked else None
        if label_from_dash and label_from_dash.strip().lower() != name.strip().lower():
            bits.append(f"metered as “{label_from_dash}”")
        elif tracked:
            bits.append("metered")
        if controls:
            bits.append(
                "climate" if any(_domain(c) == "climate" for c in controls)
                else "switchable"
            )
        else:
            bits.append("no on/off control")
        label = f"{name} — {', '.join(bits)}" if bits else name
        row = {"value": f"device:{device_id}", "label": label}
        (tier1 if tracked else tier2).append(row)

    # One appliance can expose both a climate entity and a power switch with no device
    # to tie them together (SmartIR + a template switch, which is this rig's shape for
    # two aircons). Offering both is the choose-X-then-Y confusion this module exists to
    # remove, so they are collapsed by resolved name with climate winning — it carries
    # mode and setpoint, and it is what the Aircon group node aggregates on.
    tier3: list[dict[str, str]] = []
    seen_names: dict[str, int] = {}
    for e in sorted(orphan_controls, key=lambda e: 0 if _domain(e) == "climate" else 1):
        if e.entity_id in taken_ids:
            continue
        name = resolve_device_name(hass, e.entity_id) or e.entity_id
        key = name.strip().lower()
        if key in seen_names:
            continue
        seen_names[key] = 1
        tier3.append({
            "value": f"entity:{e.entity_id}",
            "label": f"{name} — {_domain(e)} entity, no device",
        })

    for tier in (tier1, tier2, tier3):
        tier.sort(key=lambda r: r["label"].lower())
    return tier1 + tier2 + tier3


async def async_seed_max_kw(hass: HomeAssistant, power_entity: Optional[str]) -> Optional[float]:
    """Highest power this appliance has actually drawn, from recorder statistics.

    Seeds the "maximum power draw" field with something true instead of a typed
    guess — the figure caps what the optimiser may assign the load in any half
    hour, so a guess that is too low silently starves it. Returns None (caller keeps
    its own default) when there is no power sensor or no history yet; never raises,
    because a missing recorder must not block adding a load.
    """
    if not power_entity:
        return None
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        end = dt_util.utcnow()
        start = end - timedelta(days=30)
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, start, end,
            {power_entity}, "day", None, {"max"},
        )
        peaks = [r["max"] for r in rows.get(power_entity, []) if r.get("max") is not None]
        if not peaks:
            return None
        watts = max(peaks)
        st = hass.states.get(power_entity)
        unit = (st.attributes.get("unit_of_measurement") if st else None) or "W"
        kw = float(watts) if unit.lower() == "kw" else float(watts) / 1000.0
        # Round up to the next 0.1 kW and keep a little headroom over the observed
        # peak, which is a sampled maximum rather than the appliance's rating.
        return max(0.1, round(kw * 1.1 + 0.049, 1))
    except Exception:  # noqa: BLE001 — a seed is a convenience, never a requirement
        return None
