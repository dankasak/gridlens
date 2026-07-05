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

# Deferrable loads
CONF_DEFERRABLE_LOAD_SENSORS = "deferrable_load_sensors"  # list of sensor IDs
CONF_DEFERRABLE_LOAD_MAX_KW = "deferrable_load_max_kw"    # list of max kW, parallel to sensors
CONF_DEFERRABLE_LOAD_HOURS = "deferrable_load_hours"      # list of hour specs, parallel to sensors


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
