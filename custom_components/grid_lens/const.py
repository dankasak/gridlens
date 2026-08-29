"""Constants for the Grid Lens integration."""

DOMAIN = "grid_lens"

# Plan identifiers
PLAN_AMBER = "amber"
PLAN_OVO = "ovo"
PLAN_EA = "ea"
PLAN_AGL = "agl"

PLANS = [PLAN_AMBER, PLAN_OVO, PLAN_EA, PLAN_AGL]

# Sensor metrics for each plan
METRIC_BATTERY_CHARGE = "battery_charge"
METRIC_BATTERY_DISCHARGE = "battery_discharge"
METRIC_SOLAR_PRODUCTION = "solar_production"
METRIC_GRID_IMPORT = "grid_import"
METRIC_GRID_EXPORT = "grid_export"
METRIC_BUY_PRICE = "buy_price"
METRIC_SELL_PRICE = "sell_price"
METRIC_HOURLY_COST = "hourly_cost"
METRIC_OPTIMIZATION_NOTES = "optimization_notes"

METRICS = [
    METRIC_BATTERY_CHARGE,
    METRIC_BATTERY_DISCHARGE,
    METRIC_SOLAR_PRODUCTION,
    METRIC_GRID_IMPORT,
    METRIC_GRID_EXPORT,
    METRIC_BUY_PRICE,
    METRIC_SELL_PRICE,
    METRIC_HOURLY_COST,
    METRIC_OPTIMIZATION_NOTES,
]

# Metric metadata
METRIC_INFO = {
    METRIC_BATTERY_CHARGE: {
        "name": "Battery Charge",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
    },
    METRIC_BATTERY_DISCHARGE: {
        "name": "Battery Discharge",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
    },
    METRIC_SOLAR_PRODUCTION: {
        "name": "Solar Production",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
    },
    METRIC_GRID_IMPORT: {
        "name": "Grid Import",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
    },
    METRIC_GRID_EXPORT: {
        "name": "Grid Export",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
    },
    METRIC_BUY_PRICE: {
        "name": "Buy Price",
        "unit": "$/kWh",
        "device_class": "monetary",
        "state_class": "measurement",
    },
    METRIC_SELL_PRICE: {
        "name": "Sell Price",
        "unit": "$/kWh",
        "device_class": "monetary",
        "state_class": "measurement",
    },
    METRIC_HOURLY_COST: {
        "name": "Hourly Cost",
        "unit": "$",
        "device_class": "monetary",
        "state_class": "measurement",
    },
    METRIC_OPTIMIZATION_NOTES: {
        "name": "Optimization Notes",
        "unit": None,
        "device_class": None,
        "state_class": None,
    },
}

# Plan display names
PLAN_NAMES = {
    PLAN_AMBER: "Amber Electric",
    PLAN_OVO: "OVO Energy",
    PLAN_EA: "EnergyAustralia",
    PLAN_AGL: "AGL",
}

# Maps plan_id constants to the "Retailer - Plan Name" keys used in plan_details
PLAN_ID_TO_KEY = {
    PLAN_AMBER: "Amber Electric - SmartShift",
    PLAN_OVO: "OVO Energy - The EV Plan",
    PLAN_EA: "EnergyAustralia - EV Night Boost",
    PLAN_AGL: "AGL - Night Saver EV",
}

# Config flow constants
CONF_ENERGY_SENSOR = "energy_sensor"
CONF_SOLAR_SENSOR = "solar_sensor"
CONF_GRID_EXPORT_SENSOR = "grid_export_sensor"

# Live signed grid power sensor (W). Positive = importing from the grid, negative =
# exporting. Used by the deferrable-load "Greedy Consumption" feature to detect real-time
# export surplus (see control/load_controller.py). Optional — the greedy feature's
# export-surplus condition is simply unavailable without it (import-price-zero still
# works). Not gated on has_battery: a battery-less household can use this too.
CONF_GRID_POWER_SENSOR = "grid_power_sensor"
CONF_IMPORT_PRICE_SENSOR = "import_price_sensor"
CONF_EXPORT_PRICE_SENSOR = "export_price_sensor"
CONF_DISTRIBUTOR = "distributor"
CONF_STATE = "state"
CONF_POSTCODE = "postcode"

# Whether the customer is on a network demand tariff (peak-kW charges).
# This is set by the DNSP based on the customer's NMI/meter, not by the retail
# plan, so we can't infer it — the user tells us. When True, plans that carry a
# demand charge (charges.demand_charge_per_kw_per_day) have it billed.
CONF_HAS_DEMAND_TARIFF = "has_demand_tariff"

# Default demand window when a plan defines a demand charge but no explicit
# window. NSW residential demand tariffs (e.g. Ausgrid) typically meter peak
# demand on weekday afternoons/evenings; 15:00–21:00 is the common band.
DEFAULT_DEMAND_WINDOW_HOURS = [15, 16, 17, 18, 19, 20]

# Whether the customer's meter has Controlled Load 1 / 2 (CL1/CL2) switched on for
# this connection. Same "DNSP/install-driven, user self-declares" pattern as
# CONF_HAS_DEMAND_TARIFF — this is a network/meter fact set by the DNSP, not
# something a retail plan or HA can infer, so the user tells us. Independently
# per-register since a network may switch on CL1 but not CL2 (or vice versa).
CONF_HAS_CONTROLLED_LOAD_1 = "has_controlled_load_1"
CONF_HAS_CONTROLLED_LOAD_2 = "has_controlled_load_2"

# Battery configuration
CONF_HAS_BATTERY = "has_battery"
CONF_BATTERY_CAPACITY = "battery_capacity"
CONF_BATTERY_MAX_CHARGE_RATE = "battery_max_charge_rate"
CONF_BATTERY_MAX_DISCHARGE_RATE = "battery_max_discharge_rate"
CONF_BATTERY_EFFICIENCY = "battery_efficiency"
CONF_BATTERY_SOC_SENSOR = "battery_soc_sensor"
CONF_BATTERY_CHARGE_POWER_SENSOR = "battery_charge_power_sensor"
CONF_BATTERY_DISCHARGE_POWER_SENSOR = "battery_discharge_power_sensor"
CONF_BATTERY_MIN_SOC = "battery_min_soc"
CONF_BATTERY_MAX_SOC = "battery_max_soc"

# Minimum export price floor, in cents/kWh (0 = disabled, unchanged behaviour).
# Below this price the optimizer stops treating grid export as valuable — it still
# exports if nothing else can absorb the surplus, but prefers routing it into a
# deferrable load or holding battery charge instead of selling cheap. Converted to
# $/kWh (÷100) before reaching BatteryOptimizer, to match import/export rate units.
CONF_MIN_EXPORT_PRICE = "min_export_price"

# Which inverter driver ControlManager dispatches battery commands to (inverters/__init__.py).
CONF_INVERTER_BRAND = "inverter_brand"
CONF_INVERTER_TRANSPORT = "inverter_transport"

# Deferrable loads
CONF_DEFERRABLE_LOAD_SENSORS = "deferrable_load_sensors"  # list of sensor IDs
CONF_DEFERRABLE_LOAD_MAX_KW = "deferrable_load_max_kw"    # list of max kW, parallel to sensors
# Availability windows for a sensor-backed device are set on the dashboard's Deferrable
# Loads weekly schedule card (deferrable_schedules.py), not in config — there used to be a
# parallel static "deferrable_load_hours" config-flow field (comma-separated hours) that
# seeded the LP's availability mask before the schedule card existed; removed 2026-08-02 as
# redundant with the card, which now fully owns this. A device with no stored weekly
# schedule yet is simply unrestricted (any hour) until the user paints one.
# Optional per-device control entity (switch.* OR climate.* entity), parallel to sensors.
# A device with one configured becomes a "type 1" controllable simple load: GridLens turns
# it on/off per the optimized schedule (gated by its own default-OFF master switch). Empty
# string = no control entity = forecast-only, exactly like before this feature. A climate.*
# entity (aircon) is turned on/off via climate.turn_on/turn_off (or climate.set_hvac_mode,
# see CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE below, for an entity that doesn't support those
# services) — GridLens only ever decides on/off timing, never hvac_mode/temperature.
# EV/OCPP chargers with richer control (charge-current setpoints) are a later, separate
# mechanism — not this list. Name kept as "switches" for config-data backward compatibility
# even though a climate.* id is equally valid here.
CONF_DEFERRABLE_LOAD_SWITCHES = "deferrable_load_switches"  # list of entity IDs ("" = none)
# Optional per-device hvac_mode to command when turning a climate.*-controlled device "on",
# parallel to CONF_DEFERRABLE_LOAD_SWITCHES. Only consulted for a climate.* control entity
# that doesn't declare ClimateEntityFeature.TURN_ON/TURN_OFF support (most do — see
# control/load_controller.py._actuate()); "" = auto-pick the entity's first non-"off"
# hvac_modes entry. Meaningless (ignored) for a switch.* control entity or a forecast-only
# device.
CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE = "deferrable_load_climate_on_mode"  # list of str ("" = auto)

# --- Modulating ("type 2") deferrable loads — EV chargers with a current/power setpoint ---
# The mechanism the CONF_DEFERRABLE_LOAD_SWITCHES comment above defers to. A device with a
# setpoint entity configured is driven by *how much* power it may draw, continuously, rather
# than on/off — which is what the LP actually solves for anyway (def_i is a continuous
# 0..max_kw variable; the on/off controller quantises it away at a 50% threshold).
#
# Deliberately NOT an OCPP-specific driver. Every HA charger integration worth supporting
# exposes the same shape — a number.* entity carrying a charging-current limit in amps:
#   OCPP (lbbrhzn/ocpp)  number.*_maximum_current          (A, min 0, step 1)
#   Easee                number.*_dynamic_charger_limit    (A)
#   Wallbox              number.*_maximum_charging_current (A)
#   Zaptec / go-e / openEVSE / Tesla / Sigenergy AC charger — same idea, different names.
# So the config is "point GridLens at that number entity", and any integration matching the
# shape works with no GridLens change. "" = not a modulating load (on/off or forecast-only).
CONF_DEFERRABLE_LOAD_SETPOINT = "deferrable_load_setpoint"  # list of number.* IDs ("" = none)
# What the setpoint entity's value MEANS. "" = auto: read the entity's own
# unit_of_measurement (A → current, W/kW → power), falling back to amps, which is what
# every charger integration listed above uses. Explicit values: "a" | "w" | "kw" — needed
# only for an integration that publishes no unit at all.
CONF_DEFERRABLE_LOAD_SETPOINT_UNIT = "deferrable_load_setpoint_unit"  # list of "" | "a" | "w" | "kw"
# Phases the setpoint's amps figure applies across, for the A ↔ W conversion
# (W = amps × voltage × phases). 0 = auto-derive from this device's configured max_kw and
# the setpoint entity's own max value, which is right far more often than a guess: a 7.4 kW
# single-phase charger advertises max 32 A, a 22 kW three-phase one also advertises 32 A —
# only max_kw distinguishes them. Ignored for a W/kW setpoint.
CONF_DEFERRABLE_LOAD_PHASES = "deferrable_load_phases"  # list of int (0 = auto, else 1/2/3)
# Nominal per-phase supply voltage for that conversion. 0 = DEFAULT_SUPPLY_VOLTAGE below.
# Only a scaling constant — a 5% voltage error is a 5% power error, well inside the slack
# the min-current floor and write deadband already carry.
CONF_DEFERRABLE_LOAD_VOLTAGE = "deferrable_load_voltage"  # list of float (0 = default)
# Minimum current the load can actually be given (A). An EV must not be offered below 6 A
# (IEC 61851 duty-cycle floor) — commanding 3 A doesn't charge slowly, it makes the car
# refuse or fault. So the physically feasible set is {0} ∪ [min, max], NOT [0, max], and
# a sub-minimum LP allocation has to resolve to either "off" or "min", never to itself.
# 0 = DEFAULT_MIN_CHARGE_CURRENT_A. Set 0.1 for a genuinely continuous load (a resistive
# heater on a dimmer) that has no such floor.
CONF_DEFERRABLE_LOAD_MIN_CURRENT = "deferrable_load_min_current"  # list of float A (0 = default)
# Optional entity reporting whether the EV is actually plugged in / the charger is able to
# deliver — a binary_sensor.*, or OCPP's own sensor.*_status (ChargePointStatus vocabulary).
# "" = unknown, and unknown means "assume available": GridLens never withholds charging
# because it couldn't confirm a plug. See MODULATING_PLUGGED_STATES / _UNPLUGGED_STATES.
CONF_DEFERRABLE_LOAD_PLUG_SENSOR = "deferrable_load_plug_sensor"  # list of entity IDs ("" = none)

# Default per-phase supply voltage for amps ↔ watts. 230 V is the IEC/EU/AU nominal
# (AU is nominally 230 V +10%/−6% since AS 60038, though real suburban supply often sits
# nearer 240 V). Only used when a device has no CONF_DEFERRABLE_LOAD_VOLTAGE override and
# no live voltage sensor was discovered.
DEFAULT_SUPPLY_VOLTAGE = 230.0
# IEC 61851 minimum EV charging current. Below this an EV will refuse to draw at all.
DEFAULT_MIN_CHARGE_CURRENT_A = 6.0
# How often the fast modulation loop re-evaluates a modulating load's current setpoint.
# The 5-minute plan tick is far too coarse to track a passing cloud — solar-following is
# the whole reason to modulate rather than switch. 30 s is fast enough to follow real PV
# and slow enough that a cloud-edge oscillation doesn't turn into a write storm (the
# controller's own deadband + min-write-interval carry the rest of that load).
MODULATION_INTERVAL_SECONDS = 30
# Charger status strings that mean "nothing is connected / can't deliver". Matched
# case-insensitively against a CONF_DEFERRABLE_LOAD_PLUG_SENSOR state. Everything else —
# including any state we don't recognise — counts as plugged, per the fail-open rule above.
# Vocabulary: OCPP 1.6 ChargePointStatus + the usual binary_sensor renderings.
MODULATING_UNPLUGGED_STATES = frozenset(
    {"available", "unavailable", "faulted", "reserved", "off", "false", "disconnected",
     "not_connected", "unplugged", "no_vehicle", "idle"}
)

# Optional per-device battery/EV state-of-charge sensor (sensor.* entity, %), parallel to
# sensors. Most relevant for an EV charger deferrable load (the vehicle's own SOC), but not
# restricted to that — any deferrable load with its own battery can use it. Empty string =
# not configured; the Power Flow card only shows a SOC figure + history link for devices
# that have one set, same as it already does for the home battery's soc_entity.
CONF_DEFERRABLE_LOAD_SOC_SENSORS = "deferrable_load_soc_sensors"  # list of sensor IDs ("" = none)
# Optional per-device SOC ceiling + capacity, parallel to sensors — lets the LIVE
# advisory/control optimizer stop scheduling further charge once a device's own SOC
# sensor (above) nears a configured maximum, freeing that energy for other deferrable
# loads or export rather than pushing the device past a limit its owner set on purpose
# (e.g. an EV charged to 90% for battery longevity — not GridLens's call to override).
# capacity_kwh converts the percentage ceiling into the kWh the LP reasons in; 0.0 (not
# provided) leaves the device on the plain daily_kwh mechanism, unchanged. Only
# advisory/coordinator.py's _deferrable_for_horizon wires in a live SOC reading and
# activates this — plan_calculator.py's plan-comparison backtest never does (there is
# no "current battery state" for a hypothetical past period), so it always falls
# through to today's behaviour regardless of these being set. See
# battery_optimizer.py's module docstring for the LP mechanics.
CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT = "deferrable_load_soc_max_percent"    # list of float (100 = no cap)
CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH = "deferrable_load_soc_capacity_kwh"  # list of float (0 = not provided)
# Optional per-device Controlled Load register wiring, parallel to sensors. "" = not
# wired to controlled load (default, same as today's behaviour). "controlled_load_1" /
# "controlled_load_2" means this device's energy is physically switched via that DNSP
# register rather than the general supply — only offered in the config flow when the
# matching CONF_HAS_CONTROLLED_LOAD_1/_2 flag is on. Bill-splitting: plan_calculator.py
# prices a CL-wired device's total kWh at the plan's controlled_load_rates entry (flat
# rate — the schema has no time-window structure for CL, so no LP timing variable is
# needed for pricing correctness) instead of the general import tariff.
CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD = "deferrable_load_controlled_load"  # list of register IDs ("" = none)
# Whether a CL-wired device's energy is CURRENTLY mixed into the household's main
# energy_sensor reading and needs subtracting before CL-pricing it, vs already on a
# genuinely separate register the main sensor never sees (the normal case — a real CL
# circuit is wired separately from whatever an inverter's CT clamp monitors, so it was
# never counted in the first place). Parallel to CONF_DEFERRABLE_LOAD_SENSORS; default
# False (already separate) matches the common real case. Only consulted when the
# matching device also has a CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD register set.
CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE = "deferrable_load_cl_in_aggregate"  # list of bool

# Declared/unmonitored deferrable loads — for a device with no HA-visible energy sensor
# at all. This is the common case for a genuine Controlled Load circuit (wired separately
# from whatever an inverter's CT clamp sees — a household would need to have deliberately
# added a Shelly or similar on that specific circuit to monitor it, which is rare), but
# also useful for any other unmonitored appliance. The user supplies an estimated average
# daily kWh instead of pointing at a sensor; the LP barely notices the difference since
# dispatch already runs off {daily_kwh, max_kw, allowed_hours}, not a raw sensor curve
# (see plan_calculator.py's _get_deferrable_data / _get_declared_loads).
# Fixed 2 slots — config-flow schemas are static (no native "add another" UX); covers the
# realistic CL1+CL2 case. Unused slots have an empty name.
DEFERRABLE_LOAD_DUMMY_SLOTS = 2
CONF_DEFERRABLE_LOAD_DUMMY_NAMES = "deferrable_load_dummy_names"              # list of str ("" = unused slot)
CONF_DEFERRABLE_LOAD_DUMMY_KWH = "deferrable_load_dummy_kwh"                  # list of float (estimated avg daily kWh)
CONF_DEFERRABLE_LOAD_DUMMY_MAX_KW = "deferrable_load_dummy_max_kw"            # list of float
CONF_DEFERRABLE_LOAD_DUMMY_HOURS = "deferrable_load_dummy_hours"              # list of hour specs
CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD = "deferrable_load_dummy_controlled_load"  # list of register IDs ("" = none)
# Same meaning as CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE above, parallel to the dummy-load
# lists instead of the sensor-backed ones.
CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE = "deferrable_load_dummy_cl_in_aggregate"  # list of bool

# Estimated (sensor-less, controllable) deferrable loads — a device with no HA-visible
# energy sensor AND no way to add one (no feedback path at all, e.g. an IR-blaster-driven
# aircon), but WHICH GridLens can still turn on/off via a switch.*/climate.* control
# entity. Distinct from CONF_DEFERRABLE_LOAD_DUMMY_* above: dummy/declared loads are
# forecast-only (no control entity, LP/plan-comparison modelling only — the Controlled
# Load nomination mechanism, see VPP_CONTROLLED_LOAD_HANDOFF.md) and are NOT extended by
# this feature. An estimated load gets a real synthetic energy sensor
# (GridLensEstimatedEnergySensor, sensor.py) that GridLens creates and maintains itself —
# integrating a running kWh total from an estimated power draw while the control entity
# reads "on" — then splices that sensor's entity_id into CONF_DEFERRABLE_LOAD_SENSORS (see
# __init__.py._ensure_load_estimators) so every existing sensor-backed code path
# (plan_calculator, LoadControlManager, advisory/coordinator, the schedule card, Today
# Boost, Greedy Consumption) treats it exactly like a device with a real meter — zero
# changes needed there. Fixed 3 slots, same static-config-flow-schema reasoning as
# DEFERRABLE_LOAD_DUMMY_SLOTS above.
DEFERRABLE_LOAD_ESTIMATED_SLOTS = 3
CONF_DEFERRABLE_LOAD_EST_NAMES = "deferrable_load_est_names"      # list of str ("" = unused slot)
CONF_DEFERRABLE_LOAD_EST_CONTROL = "deferrable_load_est_control"  # list of switch.*/climate.* IDs
CONF_DEFERRABLE_LOAD_EST_KW = "deferrable_load_est_kw"            # list of float (manual seed kW)
# Whether load_estimation.LoadEstimator should refine deferrable_load_est_kw's seed value
# from real on/off transitions of the control entity (see load_estimation.py). False =
# stay on the manual seed forever — a legitimate choice if the household has no whole-house
# load power sensor configured (CONF_LOAD_POWER_SENSOR) or doesn't trust the inference.
CONF_DEFERRABLE_LOAD_EST_AUTO = "deferrable_load_est_auto"        # list of bool

# Live whole-house load power sensor (W or kW; positive = consuming). Backend counterpart
# of the Power Flow card's own `load_power_entity` YAML option (grid-lens-powerflow-card.js)
# — same concept, same kind of entity a user would already have picked for that card —
# promoted to an integration-level config field because load_estimation.LoadEstimator runs
# server-side (a background HA listener/timer), not in the browser. Optional: only consulted
# when at least one CONF_DEFERRABLE_LOAD_EST_AUTO slot is True; unset = auto-refine simply
# never fires (fails open, logs once), same discipline as CONF_GRID_POWER_SENSOR.
CONF_LOAD_POWER_SENSOR = "load_power_sensor"

# VPP bolt-on program (e.g. AGL "Bring Your Own Battery") — a retailer-level credit
# program layered on top of whatever plan the household is already on, independent
# of CONF_CURRENT_PLAN. Nullable slug; "" / None means not enrolled. Fetched from
# GET /vpp-programs/list, mirroring how CONF_CURRENT_PLAN's dropdown is populated
# from /plans/list.
CONF_VPP_PROGRAM = "vpp_program"


def parse_hours_spec(spec: str | None) -> set[int] | None:
    """Parse a deferrable-load availability spec into a set of local hours (0-23).

    Returns None for "all"/blank, meaning the device can run at any hour.
    Accepts comma-separated hours and ranges; ranges are end-exclusive clock
    times and may wrap midnight: "18-08" → {18..23, 0..7}, "0-6,12" → {0..5, 12}.
    Raises ValueError on malformed input.
    """
    if spec is None:
        return None
    spec = spec.strip().lower()
    if spec in ("", "all"):
        return None
    hours: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            if not (0 <= start <= 23 and 0 <= end <= 24):
                raise ValueError(f"hour out of range in '{part}'")
            if start == end:
                raise ValueError(f"empty range '{part}'")
            h = start
            while h != end % 24:
                hours.add(h)
                h = (h + 1) % 24
        else:
            h = int(part)
            if not 0 <= h <= 23:
                raise ValueError(f"hour out of range in '{part}'")
            hours.add(h)
    return hours or None

# Current plan (user's active retail plan)
CONF_CURRENT_PLAN = "current_plan"

# GridLens API
CONF_GRIDLENS_EMAIL = "gridlens_email"
CONF_GRIDLENS_API_URL = "gridlens_api_url"
CONF_GRIDLENS_API_KEY = "gridlens_api_key"
GRIDLENS_DEFAULT_API_URL = "https://api.gridlens.au"

# Australian states
STATES = [
    "NSW",
    "VIC", 
    "QLD",
    "SA",
    "WA",
    "TAS",
    "NT",
    "ACT"
]

# Common NSW distributors
DISTRIBUTORS = {
    "NSW": ["Ausgrid", "Endeavour Energy", "Essential Energy"],
    "VIC": ["AusNet Services", "CitiPower", "Jemena", "Powercor", "United Energy"],
    "QLD": ["Energex", "Ergon Energy"],
    "SA": ["SA Power Networks"],
    "WA": ["Western Power"],
    "TAS": ["TasNetworks"],
    "NT": ["Power and Water Corporation"],
    "ACT": ["Evoenergy"]
}

# Popular EV plans (manually configured for now)
# These are examples - would be replaced with AER API data
POPULAR_EV_PLANS = [
    {
        "retailer": "EnergyAustralia",
        "plan_name": "EV Night Boost",
        "daily_supply_charge": 1.10,
        "rates": {
            "peak": 0.32,  # $/kWh
            "shoulder": 0.25,
            "off_peak": 0.07,  # 12am-6am
        },
        "time_periods": {
            "off_peak": [(0, 6)],  # midnight to 6am
            "shoulder": [(6, 14), (20, 24)],  # 6am-2pm, 8pm-midnight
            "peak": [(14, 20)],  # 2pm-8pm
        }
    },
    {
        "retailer": "AGL",
        "plan_name": "Night Saver EV",
        "daily_supply_charge": 1.15,
        "rates": {
            "peak": 0.35,
            "off_peak": 0.08,  # 12am-6am
        },
        "time_periods": {
            "off_peak": [(0, 6)],
            "peak": [(6, 24)],
        }
    },
    {
        "retailer": "OVO Energy",
        "plan_name": "The EV Plan",
        "daily_supply_charge": 1.05,
        "rates": {
            "peak": 0.30,
            "super_off_peak": 0.00,  # 11am-2pm free charging
            "off_peak": 0.08,  # 12am-6am
        },
        "time_periods": {
            "super_off_peak": [(11, 14)],  # 11am-2pm
            "off_peak": [(0, 6)],
            "peak": [(6, 11), (14, 24)],
        }
    }
]
