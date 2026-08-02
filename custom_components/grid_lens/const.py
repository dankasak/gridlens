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
# Optional per-device control switch (switch.* entity), parallel to sensors. A device with
# a switch configured becomes a "type 1" controllable simple load: GridLens turns it on/off
# per the optimized schedule (gated by its own default-OFF master switch). Empty string = no
# switch = forecast-only, exactly like before this feature. EV/OCPP chargers with richer
# control (charge-current setpoints) are a later, separate mechanism — not this switch list.
CONF_DEFERRABLE_LOAD_SWITCHES = "deferrable_load_switches"  # list of switch IDs ("" = none)
# Optional per-device battery/EV state-of-charge sensor (sensor.* entity, %), parallel to
# sensors. Most relevant for an EV charger deferrable load (the vehicle's own SOC), but not
# restricted to that — any deferrable load with its own battery can use it. Empty string =
# not configured; the Power Flow card only shows a SOC figure + history link for devices
# that have one set, same as it already does for the home battery's soc_entity.
CONF_DEFERRABLE_LOAD_SOC_SENSORS = "deferrable_load_soc_sensors"  # list of sensor IDs ("" = none)
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
