"""Plan calculator for comparing electricity costs."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period

from .battery_optimizer import BatteryOptimizer
from .retailer_plans import (
    plans_from_api_data, versioned_plans_from_history, build_rate_caps,
    build_conditional_credits, RetailerPlan, PlanFromData,
)
from .const import (
    DOMAIN,
    CONF_ENERGY_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_IMPORT_PRICE_SENSOR,
    CONF_EXPORT_PRICE_SENSOR,
    CONF_HAS_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_MAX_CHARGE_RATE,
    CONF_BATTERY_MAX_DISCHARGE_RATE,
    CONF_BATTERY_EFFICIENCY,
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_CHARGE_POWER_SENSOR,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_MAX_SOC,
    CONF_MIN_EXPORT_PRICE,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_SWITCHES,
    CONF_DEFERRABLE_LOAD_SETPOINT,
    CONF_DEFERRABLE_LOAD_MIN_CURRENT,
    CONF_DEFERRABLE_LOAD_PHASES,
    CONF_DEFERRABLE_LOAD_VOLTAGE,
    CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD,
    CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE,
    CONF_DEFERRABLE_LOAD_DUMMY_NAMES,
    CONF_DEFERRABLE_LOAD_DUMMY_KWH,
    CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD,
    CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE,
    CONF_HAS_DEMAND_TARIFF,
    DEFAULT_DEMAND_WINDOW_HOURS,
    DEFAULT_MIN_CHARGE_CURRENT_A,
    DEFAULT_SUPPLY_VOLTAGE,
    POPULAR_EV_PLANS,
)
from .entity_lookup import resolve_device_name, async_get_energy_dashboard_names

_LOGGER = logging.getLogger(__name__)


class PlanCalculator:
    """Calculate and compare electricity plan costs."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the calculator."""
        self.hass = hass
        self.entry = entry
        self.energy_sensor = entry.data.get(CONF_ENERGY_SENSOR)
        self.solar_sensor = entry.data.get(CONF_SOLAR_SENSOR)
        self.grid_export_sensor = entry.data.get(CONF_GRID_EXPORT_SENSOR)
        self.import_price_sensor = entry.data.get(CONF_IMPORT_PRICE_SENSOR)
        self.export_price_sensor = entry.data.get(CONF_EXPORT_PRICE_SENSOR)
        # Whether the customer is on a network demand tariff. Only when True do
        # plans carrying a demand charge have it billed (see _compute_demand_charge).
        self.has_demand_tariff = entry.data.get(CONF_HAS_DEMAND_TARIFF, False)

        # Plan data fetched from API (plan_id → plan_data dict).
        # Set by the SSE handler before calling calculate_plan_costs.
        self.plan_data: dict | None = None

        # Plan version history for the analysis period, fetched from
        # /plans/history at the start of calculate_plan_costs. When present,
        # _get_plans() returns VersionedPlan wrappers so past intervals are
        # priced with the plan version in force at the time.
        self.plan_history: dict | None = None
        self._history_period: tuple | None = None  # (start_dt, end_dt)

        # Network operator definitions fetched from API (operator_key → operator_data dict).
        # Set by the SSE handler before calling calculate_plan_costs.
        self.network_operators: dict = {}

        # Battery configuration
        self.has_battery = entry.data.get(CONF_HAS_BATTERY, False)
        self.battery_capacity = entry.data.get(CONF_BATTERY_CAPACITY, 13.5)
        self.battery_max_charge_rate = entry.data.get(CONF_BATTERY_MAX_CHARGE_RATE, 5.0)
        self.battery_max_discharge_rate = entry.data.get(CONF_BATTERY_MAX_DISCHARGE_RATE, 5.0)
        self.battery_efficiency = entry.data.get(CONF_BATTERY_EFFICIENCY, 95.0)
        self.battery_min_soc = entry.data.get(CONF_BATTERY_MIN_SOC, 10.0)
        self.battery_max_soc = entry.data.get(CONF_BATTERY_MAX_SOC, 90.0)
        self.battery_soc_sensor = entry.data.get(CONF_BATTERY_SOC_SENSOR)
        self.battery_power_sensor = entry.data.get(CONF_BATTERY_CHARGE_POWER_SENSOR)  # Signed sensor

        self.deferrable_load_sensors: list[str] = entry.data.get(CONF_DEFERRABLE_LOAD_SENSORS, [])
        self.deferrable_load_max_kw: list[float] = entry.data.get(CONF_DEFERRABLE_LOAD_MAX_KW, [])
        self.deferrable_load_switches: list[str] = entry.data.get(CONF_DEFERRABLE_LOAD_SWITCHES, [])
        # Modulating-load wiring, read only to derive each device's min_kw floor (below).
        # Absent from every config entry saved before that feature, hence the `or []`.
        self.deferrable_load_setpoint: list[str] = entry.data.get(
            CONF_DEFERRABLE_LOAD_SETPOINT, []) or []
        self.deferrable_load_min_current: list = entry.data.get(
            CONF_DEFERRABLE_LOAD_MIN_CURRENT, []) or []
        self.deferrable_load_phases: list = entry.data.get(CONF_DEFERRABLE_LOAD_PHASES, []) or []
        self.deferrable_load_voltage: list = entry.data.get(CONF_DEFERRABLE_LOAD_VOLTAGE, []) or []
        # Controlled Load register wiring, parallel to deferrable_load_sensors ("" = not
        # CL-wired) — see _build_cl_devices for how this turns into a flat bill line.
        self.deferrable_load_controlled_load: list[str] = entry.data.get(
            CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD, [])
        self.deferrable_load_cl_in_aggregate: list[bool] = entry.data.get(
            CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE, [])
        # Declared loads with no HA sensor at all (the normal case for a genuine
        # Controlled Load circuit — see const.py's docstring on the CONF_*_DUMMY_*
        # keys). Parsed once here; _build_cl_devices merges the CL-wired ones in
        # with the sensor-backed devices above.
        self.declared_loads: list[dict] = self._parse_declared_loads(entry)
        self.current_plan_override: str | None = entry.data.get("current_plan")
        
        # Initialize battery optimizer if battery is configured
        self.battery_optimizer = None
        if self.has_battery:
            self.battery_optimizer = BatteryOptimizer(
                capacity_kwh=self.battery_capacity,
                max_charge_rate_kw=self.battery_max_charge_rate,
                max_discharge_rate_kw=self.battery_max_discharge_rate,
                efficiency_percent=self.battery_efficiency,
                min_soc_percent=self.battery_min_soc,
                max_soc_percent=self.battery_max_soc,
            )
            _LOGGER.info(f"Battery optimizer initialized: {self.battery_capacity}kWh battery")

    def _get_min_export_price(self) -> float:
        """Live value from the dashboard number entity (see number.py), not a
        cached config-flow snapshot — read fresh on every LP call so a change
        the user makes takes effect on the next comparison, no reload needed.
        Stored as c/kWh; converted here to $/kWh to match the optimizer's rate
        units."""
        from .runtime_settings import get_live_number
        cents = get_live_number(
            self.hass, self.entry.entry_id, "min_export_price",
            self.entry.data.get(CONF_MIN_EXPORT_PRICE, 0.0),
        )
        return cents / 100.0

    def _get_plans(self) -> list[RetailerPlan]:
        """Return plan objects from API data. Tier filtering is enforced by the API.

        When version history for the analysis period has been loaded
        (see _fetch_plan_history), plans that changed during the period come
        back as VersionedPlan wrappers; otherwise current-version plans.
        """
        if not self.plan_data:
            _LOGGER.warning("No plan data loaded from API; calculation will have no plans.")
            return []
        if self.plan_history and self._history_period:
            return versioned_plans_from_history(
                self.plan_data, self.plan_history, self.network_operators,
                self._history_period[0], self._history_period[1])
        return plans_from_api_data(self.plan_data, self.network_operators)

    def _parse_declared_loads(self, entry: ConfigEntry) -> list[dict]:
        """Parse the fixed-slot declared/unmonitored-load config into a list of
        dicts, skipping any slot with a blank name (the "unused slot" convention —
        see const.py's CONF_DEFERRABLE_LOAD_DUMMY_* docstring)."""
        names = entry.data.get(CONF_DEFERRABLE_LOAD_DUMMY_NAMES, [])
        kwh = entry.data.get(CONF_DEFERRABLE_LOAD_DUMMY_KWH, [])
        cl = entry.data.get(CONF_DEFERRABLE_LOAD_DUMMY_CONTROLLED_LOAD, [])
        in_agg = entry.data.get(CONF_DEFERRABLE_LOAD_DUMMY_CL_IN_AGGREGATE, [])
        loads = []
        for i, name in enumerate(names):
            if not name:
                continue
            loads.append({
                'name': name,
                'daily_kwh': float(kwh[i]) if i < len(kwh) else 0.0,
                'controlled_load': cl[i] if i < len(cl) else "",
                'in_aggregate': bool(in_agg[i]) if i < len(in_agg) else False,
            })
        return loads

    def _build_cl_devices(self, deferrable_loads: list[dict]) -> list[dict]:
        """Combine sensor-backed and declared devices that are wired to a Controlled
        Load register into one list, each with the total daily kWh _compute_bill_items
        needs to price them (uniform whether the figure came from real sensor history
        or a user estimate — the LP already treats both the same way; see
        CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD's docstring for why no LP timing variable
        is needed for CL pricing correctness).

        Returns [{'name', 'register', 'daily_kwh', 'in_aggregate'}, ...].
        """
        by_sensor = {d['sensor_id']: d['daily_kwh'] for d in deferrable_loads}
        devices = []
        for i, sensor_id in enumerate(self.deferrable_load_sensors):
            register = (
                self.deferrable_load_controlled_load[i]
                if i < len(self.deferrable_load_controlled_load) else ""
            )
            if not register:
                continue
            devices.append({
                'name': resolve_device_name(self.hass, None, sensor_id) or sensor_id,
                'register': register,
                'daily_kwh': by_sensor.get(sensor_id, 0.0),
                'in_aggregate': (
                    bool(self.deferrable_load_cl_in_aggregate[i])
                    if i < len(self.deferrable_load_cl_in_aggregate) else False
                ),
            })
        for d in self.declared_loads:
            if not d['controlled_load']:
                continue
            devices.append({
                'name': d['name'],
                'register': d['controlled_load'],
                'daily_kwh': d['daily_kwh'],
                'in_aggregate': d['in_aggregate'],
            })
        return devices

    async def _fetch_plan_history(self, start_date: datetime, end_date: datetime) -> None:
        """Fetch /plans/history for the analysis period. Best-effort: any
        failure leaves plan_history unset and calculation proceeds on current
        rates (the pre-versioning behaviour)."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        import aiohttp as _aiohttp
        from .const import (
            CONF_GRIDLENS_API_KEY, CONF_GRIDLENS_API_URL, CONF_STATE,
            CONF_DISTRIBUTOR,
        )
        self.plan_history = None
        self._history_period = None
        api_key = self.entry.data.get(CONF_GRIDLENS_API_KEY, "")
        api_url = self.entry.data.get(CONF_GRIDLENS_API_URL, "https://api.gridlens.au")
        if not api_key:
            return
        params = {
            "state": self.entry.data.get(CONF_STATE, "NSW"),
            "from": start_date.date().isoformat(),
            "to": end_date.date().isoformat(),
        }
        network = self.entry.data.get(CONF_DISTRIBUTOR, "")
        if network:
            params["network"] = network
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{api_url}/plans/history", params=params,
                headers={"X-API-Key": api_key,
                         "User-Agent": "GridLens-HA-Integration/1.0"},
                timeout=_aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("plans/history returned %s; using current rates",
                                    resp.status)
                    return
                payload = await resp.json()
        except Exception as exc:
            _LOGGER.warning("plans/history fetch failed (%s); using current rates", exc)
            return
        history = payload.get("plans") or {}
        self.plan_history = history
        self._history_period = (start_date, end_date)
        n_versioned = sum(1 for v in history.values() if len(v) > 1)
        if n_versioned:
            _LOGGER.info("Plan history loaded: %d plan(s) changed during the "
                         "analysis period; old intervals will use the rates in "
                         "force at the time", n_versioned)

    async def calculate_plan_costs(
        self,
        start_date: datetime = None,
        end_date: datetime = None,
        on_plan_ready=None,  # async callable(plan_key, detail, meta) — called after each plan
        on_progress=None,    # async callable(message, step, total) — called after each data fetch
    ) -> dict[str, Any]:
        """Calculate costs for all plans based on historical usage.
        
        With battery:
        - Current plan: Uses ACTUAL battery behavior from sensors
        - Alternative plans: Uses OPTIMIZED battery strategy
        
        Args:
            start_date: Start of analysis period (defaults to 30 days ago)
            end_date: End of analysis period (defaults to now)
        """
        # Default to last 30 days if not specified (UTC-aware)
        if end_date is None:
            end_date = datetime.now(timezone.utc)
        if start_date is None:
            start_date = end_date - timedelta(days=30)
        
        # Calculate actual days in period. Use round() so that a period ending at
        # 23:59:59 (total_seconds just under N×86400) still counts as N days.
        actual_days = round((end_date - start_date).total_seconds() / 86400)

        # Load plan version history so past intervals are priced with the plan
        # version in force at the time (falls back to current rates on failure).
        await self._fetch_plan_history(start_date, end_date)

        # Build the fetch-phase step count up front so the SSE stream can report
        # real progress instead of one opaque "Fetching…" message for the whole
        # phase. Each condition below mirrors an `if <sensor>:` fetch further down.
        _fetch_total = 1  # usage_data is always fetched
        if self.solar_sensor:
            _fetch_total += 1
        if self.grid_export_sensor:
            _fetch_total += 1
        if self.battery_power_sensor:
            _fetch_total += 1
        if self.deferrable_load_sensors:
            _fetch_total += 1
        if self.battery_soc_sensor:
            _fetch_total += 1
        if any(getattr(p, 'aemo_price_sensor', None) for p in self._get_plans()):
            _fetch_total += 1
        if self.import_price_sensor:
            _fetch_total += 1
        if self.export_price_sensor:
            _fetch_total += 1
        _fetch_done = 0

        async def _progress(message: str) -> None:
            nonlocal _fetch_done
            _fetch_done += 1
            if on_progress:
                await on_progress(message, _fetch_done, _fetch_total)

        usage_data = await self._get_usage_data(start_date, end_date)
        await _progress("Fetched grid import / load history")

        if not usage_data:
            _LOGGER.info("No usage data available yet")
            return {
                "current_plan_energy_cost": 0,
                "current_plan_monthly_fee": 25.00,
                "current_plan_total": 25.00,
                "alternative_plans": {},
                "usage_days": 0,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "calculation_date": datetime.now().isoformat(),
                "status": "waiting_for_data",
                "message": "Waiting for energy usage data. Check back in 24 hours.",
            }

        # Get solar data (used for battery optimisation modelling, not for deriving import/export)
        solar_data = []
        if self.solar_sensor:
            solar_data = await self._get_usage_data(start_date, end_date, self.solar_sensor)
            await _progress("Fetched solar production history")

        # Determine grid import and export.
        # When dedicated import/export sensors are configured, use them directly —
        # energy_sensor IS already the net grid import, so never subtract solar from it.
        # Only fall back to load-minus-solar if there is no export sensor (i.e. the
        # energy_sensor is a total-load sensor, not a grid-import sensor).
        grid_export_data = []
        export_fine_data = []
        if self.grid_export_sensor:
            grid_export_data = await self._get_usage_data(start_date, end_date, self.grid_export_sensor)
            _LOGGER.warning(
                f"Using direct sensors — import: {sum(d['value'] for d in usage_data):.2f} kWh, "
                f"export: {sum(d['value'] for d in grid_export_data):.2f} kWh"
            )
            # 5-minute export series for FiT-window attribution in bill items.
            # Hourly buckets cannot resolve sub-hourly FiT windows (e.g. Flow Power's
            # 17:30-19:30: the 17:00 bucket's start timestamp falls outside the window,
            # but most of its export can be inside it). Short-term statistics may only
            # cover part of the period — _compute_bill_items falls back to pro-rated
            # hourly buckets for uncovered hours.
            export_fine_data = await self._get_usage_data(
                start_date, end_date, self.grid_export_sensor, period="5minute"
            )
            await _progress("Fetched grid export history")
        elif self.solar_sensor and solar_data:
            # No dedicated export sensor: derive import/export from (total load) − solar
            usage_data, grid_export_data = self._calculate_grid_import(usage_data, solar_data)
            _LOGGER.warning(
                f"Derived from load−solar — import: {sum(d['value'] for d in usage_data):.2f} kWh, "
                f"export: {sum(d['value'] for d in grid_export_data):.2f} kWh"
            )

        # Fetch actual battery charge/discharge history once — this is the most
        # expensive query in the whole calculation (raw state_changes over the
        # full period for a high-frequency power sensor, then integrated in
        # Python), so it must not be fetched more than once per calculation.
        # Used for:
        #   1. Feeding the current-plan cost calculation (actual behavior)
        #   2. Computing true house load for LP (strips grid-to-battery charging)
        #   3. Populating battery chart columns on market-linked plan profiles
        battery_data: list[dict] = []
        battery_hod_avg: dict = {}
        if self.battery_power_sensor:
            battery_data = await self._get_battery_behavior(start_date, end_date)
            if battery_data:
                battery_hod_avg = self._aggregate_battery_by_hod(battery_data)
            await _progress("Fetched battery charge/discharge history")

        # Calculate current plan's actual cost with ACTUAL battery behavior
        current_plan_energy_cost = await self._calculate_current_plan_cost_with_battery(
            usage_data,
            grid_export_data,
            solar_data,
            start_date,
            end_date,
            battery_data=battery_data,
        )

        # True house load = solar + grid_import - grid_export + discharge - charge.
        # When EMHASS/HEMS grid-charges the battery, that shows up as grid import
        # but is NOT household demand.  Leaving it in would force the LP for
        # alternative plans to satisfy that artificial load, producing nonsense.
        true_load_data = self._compute_true_load_data(
            solar_data, usage_data, grid_export_data, battery_data
        )

        # Deferrable loads: fetch per-device data, build LP load list, and combined time series.
        # Each device gets its own LP variable with its own max kW and daily kWh target —
        # this is critical because e.g. a 1.8 kW EV charger needs many more hours than a
        # 4.7 kW hot water system to deliver the same energy; combining them loses this.
        deferrable_data: list[dict] = []
        deferrable_hod_avg: dict = {}
        deferrable_loads: list[dict] = []
        deferrable_per_sensor_hod: list[dict] = []
        if self.deferrable_load_sensors:
            deferrable_data, deferrable_loads, deferrable_per_sensor_hod = await self._get_deferrable_data(start_date, end_date)
            await _progress("Fetched deferrable load history")
            if deferrable_data:
                deferrable_hod_avg = self._aggregate_kwh_by_hod(deferrable_data)
                for load in deferrable_loads:
                    _LOGGER.warning(
                        "Deferrable load %s: %.2f kWh/day, max %.1f kW (min %.1f h/day to complete)",
                        load['sensor_id'], load['daily_kwh'], load['max_kw'],
                        load['daily_kwh'] / load['max_kw'] if load['max_kw'] > 0 else 0,
                    )

        # Controlled-Load-wired devices (sensor-backed or declared) — priced separately
        # in _compute_bill_items, not part of the LP's normal deferrable dispatch (the
        # LP's own timing choice doesn't affect a CL rate's dollar value, since it has
        # no time-of-day structure; see _build_cl_devices).
        cl_devices = self._build_cl_devices(deferrable_loads)
        if cl_devices:
            _LOGGER.warning(
                "Controlled Load devices: %s",
                ", ".join(f"{d['name']} ({d['register']}, {d['daily_kwh']:.2f} kWh/day)" for d in cl_devices),
            )

        # Base load = true household demand minus deferrable loads.
        # The LP will re-optimise when to deliver the same total kWh per day.
        base_load_data = self._subtract_ev_from_load(true_load_data, deferrable_data)

        # SOC by hour-of-day for chart display (uses HA statistics mean, fast).
        soc_hod_avg: dict = {}
        if self.battery_soc_sensor:
            soc_hod_avg = await self._get_avg_stat_by_hod(
                self.battery_soc_sensor, start_date, end_date, stat="mean"
            )
            await _progress("Fetched battery SOC history")

        # Average home load and solar by hour-of-day for chart display.
        home_load_hod_avg = self._aggregate_kwh_by_hod(base_load_data)
        solar_hod_avg = self._aggregate_kwh_by_hod(solar_data) if solar_data else {}
        total_deferrable_daily_kwh = sum(d['daily_kwh'] for d in deferrable_loads)
        _LOGGER.warning(
            "Profile data ready: %d hod SOC entries, %.1f kWh/day base load avg, %.2f kWh/day deferrable (%d devices)",
            len(soc_hod_avg),
            sum(home_load_hod_avg.values()),
            total_deferrable_daily_kwh,
            len(deferrable_loads),
        )

        # Identify the current plan (the one the user is actually on).
        # Only this plan uses real sensor data; all other plans are LP-optimised.
        _, current_plan_name = self._detect_current_plan(actual_days)
        _LOGGER.warning("Current plan detected: %s", current_plan_name or "(none)")

        # PEA calculation for Flow Power (and any future plan with aemo_price_sensor).
        # Fetch AEMO dispatch prices once; compute PEA from actual grid import vs market prices.
        pea_results: dict = {}  # plan_key → pea_result dict
        _aemo_price_cache: dict[str, list[dict]] = {}  # aemo_sensor → price series
        _pea_plans_with_sensor = [
            p for p in self._get_plans() if getattr(p, 'aemo_price_sensor', None)
        ]
        for _pea_plan in _pea_plans_with_sensor:
            aemo_sensor = _pea_plan.aemo_price_sensor
            bpea = getattr(_pea_plan, 'bpea', 0.017)
            _pea_key = f"{_pea_plan.retailer} - {_pea_plan.plan_name}"
            # Multiple plans (e.g. several Flow Power variants) share the same
            # household AEMO sensor — fetch its raw 5-min history only once.
            if aemo_sensor not in _aemo_price_cache:
                _aemo_price_cache[aemo_sensor] = await self._fetch_5min_prices(
                    aemo_sensor, start_date, end_date
                )
            price_series = _aemo_price_cache[aemo_sensor]
            if price_series:
                result = self._compute_pea_credit(usage_data, price_series, bpea)
                if result:
                    pea_results[_pea_key] = result
                    _LOGGER.warning(
                        "PEA for %s: credit=$%.2f (LWAP=%.2fc TWAP=%.2fc PEA=%.3fc/kWh)",
                        _pea_key, result['pea_credit'],
                        result['lwap_c'], result['twap_c'], result['pea_c'],
                    )
            else:
                _LOGGER.warning(
                    "No AEMO price data for PEA calculation (%s); check %s has statistics",
                    _pea_key, aemo_sensor,
                )
        if _pea_plans_with_sensor:
            await _progress("Fetched AEMO spot price history")

        # Pre-fetch everything needed for per-plan hourly profiles and bill items
        # so these can be computed inside the plan loop (enabling streaming callbacks).
        avg_import_prices = {}
        avg_export_prices = {}
        if self.import_price_sensor:
            avg_import_prices = await self._get_avg_price_by_hour(
                self.import_price_sensor, start_date, end_date
            )
            await _progress("Fetched import price history")
        if self.export_price_sensor:
            avg_export_prices = await self._get_avg_price_by_hour(
                self.export_price_sensor, start_date, end_date
            )
            await _progress("Fetched export price history")
        hourly_day_profile = self._compute_hourly_day_profile(usage_data, grid_export_data)
        energy_flows = await self._prepare_energy_flow_data(
            usage_data,
            solar_data if solar_data else [],
            grid_export_data,
            start_date,
            end_date,
            precomputed_battery_data=battery_data,
        )

        # Calculate costs for all plans.
        # Sort so the current plan runs first — it skips the LP and returns immediately,
        # allowing the streaming callback to render it before alternatives finish.
        plan_costs = {}
        plan_optimization_results = {}
        current_plan_total = None

        _LOGGER.warning(f"Battery check: has_battery={self.has_battery}, optimizer={bool(self.battery_optimizer)}, solar_data={len(solar_data) if solar_data else 0} records")

        all_plans_ordered = sorted(
            self._get_plans(),
            key=lambda p: 0 if f"{p.retailer} - {p.plan_name}" == current_plan_name else 1,
        )

        for plan in all_plans_ordered:
            plan_key = f"{plan.retailer} - {plan.plan_name}"
            is_current = (plan_key == current_plan_name)
            opt_result = None

            # ── Cost + optimisation ──────────────────────────────────────────────────
            if self.has_battery and self.battery_optimizer and solar_data:
                _LOGGER.warning(f"Using OPTIMISED battery calculation for {plan_key}")
                cost, opt_result = await self._calculate_plan_cost_with_battery_optimization(
                    plan,
                    solar_data,
                    base_load_data,
                    grid_export_data,
                    deferrable_loads=deferrable_loads,
                )

                # The current plan keeps its LP-optimised `cost` here. This used to be
                # overwritten with price-sensor-derived energy, which priced the held
                # plan off a feed that need not correspond to it at all (e.g. a Flow
                # Power wholesale sensor left configured after switching to a TOU plan).
                fixed_credit = getattr(plan, 'fixed_daily_credit', 0.0) * actual_days
                plan_costs[plan_key] = cost - fixed_credit
                breakdown = plan.get_display_breakdown(opt_result)
                plan_optimization_results[plan_key] = {
                    'optimization': opt_result,
                    'breakdown': breakdown,
                    'strategy': plan.describe_strategy(),
                    'plan_info': plan.get_plan_info(),
                }

            # (No price-sensor branch for the current plan: without a battery optimiser
            # it falls through to the same simple per-plan calculation as every
            # alternative, rather than being costed from an unrelated price feed.)
            else:
                _LOGGER.warning(f"Using SIMPLE calculation for {plan_key}")
                cost = self._calculate_plan_cost_simple(usage_data, plan)
                fixed_credit = getattr(plan, 'fixed_daily_credit', 0.0) * actual_days
                plan_costs[plan_key] = cost - fixed_credit
                plan_optimization_results[plan_key] = {
                    'breakdown': {'total': cost, 'note': 'No battery optimisation available'},
                    'strategy': plan.describe_strategy(),
                    'plan_info': plan.get_plan_info(),
                }

            # ── Hourly profile ───────────────────────────────────────────────────────
            lp_day_profile = opt_result.get('day_profile') if opt_result else None
            if lp_day_profile and not is_current:
                for slot in lp_day_profile:
                    h = slot['hour']
                    slot['home_load_kwh'] = round(home_load_hod_avg.get(h, 0.0), 4)
                    slot['solar_kwh']     = round(solar_hod_avg.get(h, 0.0), 4)
                plan_optimization_results[plan_key]['hourly_profile'] = lp_day_profile
            else:
                profile = self._build_plan_hourly_profile(
                    hourly_day_profile, plan, avg_import_prices, avg_export_prices, start_date
                )
                for slot in profile:
                    h = slot['hour']
                    batt = battery_hod_avg.get(h, {})
                    slot['charge_kwh']     = round(batt.get('charge_kwh', 0.0), 4)
                    slot['discharge_kwh']  = round(batt.get('discharge_kwh', 0.0), 4)
                    slot['home_load_kwh']  = round(home_load_hod_avg.get(h, 0.0), 4)
                    slot['solar_kwh']      = round(solar_hod_avg.get(h, 0.0), 4)
                    slot['deferrable_kwh'] = round(deferrable_hod_avg.get(h, 0.0), 4)
                    slot['deferrable_per_device'] = [
                        round(deferrable_per_sensor_hod[ii].get(h, 0.0), 4)
                        for ii in range(len(deferrable_per_sensor_hod))
                    ]
                    slot['soc_percent'] = round(soc_hod_avg.get(h, 0.0), 1)
                plan_optimization_results[plan_key]['hourly_profile'] = profile

                # Overlay actual historical battery behaviour onto the LP schedule,
                # in place. This used to *replace* the schedule with battery-only slots;
                # harmless while the current plan's bill items came from a separate
                # actual-usage path, but _compute_bill_items now reads this same
                # schedule for every plan, and slots stripped of their import/export
                # and rate fields zero out every line quantity while leaving the
                # total correct — an itemised bill of all-zero kWh.
                if battery_hod_avg and opt_result is not None:
                    for slot in opt_result.get('schedule') or []:
                        h = slot.get('hour')
                        batt = battery_hod_avg.get(h, {})
                        slot['charge_kwh']    = round(batt.get('charge_kwh', 0.0), 3)
                        slot['discharge_kwh'] = round(batt.get('discharge_kwh', 0.0), 3)
                        slot['soc_percent']   = round(soc_hod_avg.get(h, 0.0), 1)

            # ── Bill items ───────────────────────────────────────────────────────────
            plan_optimization_results[plan_key]['breakdown']['bill_items'] = \
                self._compute_bill_items(
                    plan,
                    usage_data,
                    grid_export_data,
                    actual_days,
                    # No current-plan special case (both default to None). Passing the
                    # metered actuals here scored the current plan on a *substituted*
                    # price — the configured import_price_sensor, or the 15c/kWh
                    # fallback in _calculate_cost_breakdown when none is set — while
                    # every alternative was priced by its own tariff off the LP
                    # dispatch. That made the headline "current vs best alternative"
                    # apples-to-oranges, and mispriced outright whenever the price
                    # sensor didn't correspond to the plan actually held. The plan's
                    # own rate structure is authoritative for every plan.
                    comparison_total=plan_costs.get(plan_key),
                    opt_result=opt_result,
                    pea_result=pea_results.get(plan_key),
                    export_fine_data=export_fine_data,
                    cl_devices=cl_devices,
                )

            # The current plan's headline cost is its LP-optimised total, exactly like
            # every alternative — bill_items no longer overrides it. Overriding used to
            # push the substituted-price total (see the _compute_bill_items call above)
            # into the ranking, so the one plan the user was actually on was the one
            # scored on a different basis from everything it was compared against.
            if is_current:
                current_plan_total = plan_costs[plan_key]
                # Take the energy component from the plan's own breakdown too. It is
                # surfaced as the `energy_cost` attribute on the current-plan cost
                # sensor, and would otherwise still report the estimate from
                # _calculate_current_plan_cost_with_battery — which falls back to a
                # flat 15c/kWh when no price sensor is configured.
                _current_bd = plan_optimization_results[plan_key].get('breakdown') or {}
                if 'total_energy_cost' in _current_bd:
                    current_plan_energy_cost = _current_bd['total_energy_cost']

            # ── Streaming callback ───────────────────────────────────────────────────
            if on_plan_ready:
                await on_plan_ready(plan_key, plan_optimization_results[plan_key], {
                    'current_plan_name': current_plan_name,
                    'alternative_plans': dict(plan_costs),
                    'usage_days': actual_days,
                    'start_date': start_date.isoformat(),
                    'end_date': end_date.isoformat(),
                    'current_plan_total': current_plan_total or 0,
                    'energy_flows': energy_flows,
                    'deferrable_devices': [
                        {"name": d["name"], "sensor_id": d["sensor_id"]}
                        for d in deferrable_loads
                    ],
                })

        # Final current-plan total (fallback if current plan not in plan_costs).
        current_supply = (
            next((p.daily_supply_charge for p in self._get_plans()
                  if f"{p.retailer} - {p.plan_name}" == current_plan_name), 1.342)
            * actual_days
        )
        if current_plan_total is None:
            current_plan_total = (
                plan_costs[current_plan_name]
                if current_plan_name and current_plan_name in plan_costs
                else current_plan_energy_cost + current_supply
            )

        # Calculate potential savings vs current plan
        savings = {}
        for plan_name, cost in plan_costs.items():
            savings[f"{plan_name}_vs_current"] = cost - current_plan_total

        return {
            "current_plan_energy_cost": current_plan_energy_cost,
            "current_plan_monthly_fee": next(
                (p.monthly_subscription_fee for p in self._get_plans()
                 if f"{p.retailer} - {p.plan_name}" == current_plan_name),
                0.0,
            ),
            "current_plan_total": current_plan_total,
            "current_plan_name": current_plan_name,
            "alternative_plans": plan_costs,
            "plan_details": plan_optimization_results,  # New: detailed results for dashboard
            "energy_flows": energy_flows,  # New: for energy flow visualization
            "deferrable_devices": [
                {"name": d["name"], "sensor_id": d["sensor_id"]}
                for d in deferrable_loads
            ],
            "usage_days": actual_days,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "calculation_date": datetime.now().isoformat(),
            **savings,
        }

    async def _prepare_energy_flow_data(
        self,
        usage_data: list[dict],
        solar_data: list[dict],
        export_data: list[dict],
        start_time: datetime,
        end_time: datetime,
        precomputed_battery_data: list[dict] | None = None,
    ) -> dict:
        """Prepare energy flow data for dashboard visualization.
        
        Returns hourly data for first 24 hours in format for chart.
        """
        #Group all data by hour
        flows_by_hour = {}
        
        # Add grid import
        for d in usage_data:
            hour = d["timestamp"].replace(minute=0, second=0, microsecond=0)
            if hour not in flows_by_hour:
                flows_by_hour[hour] = {
                    "timestamp": hour.isoformat(),
                    "grid_import": 0,
                    "solar": 0,
                    "grid_export": 0,
                    "battery_charge": 0,
                    "battery_discharge": 0,
                }
            flows_by_hour[hour]["grid_import"] += d["value"]
        
        # Add solar
        for d in solar_data:
            hour = d["timestamp"].replace(minute=0, second=0, microsecond=0)
            if hour in flows_by_hour:
                flows_by_hour[hour]["solar"] += d["value"]
        
        # Add export
        for d in export_data:
            hour = d["timestamp"].replace(minute=0, second=0, microsecond=0)
            if hour in flows_by_hour:
                flows_by_hour[hour]["grid_export"] += d["value"]
        
        # Add battery data if available (use pre-fetched data to avoid a second DB query)
        if self.has_battery and self.battery_power_sensor:
            try:
                battery_data = precomputed_battery_data if precomputed_battery_data is not None \
                    else await self._get_battery_behavior(start_time, end_time)
                for d in battery_data:
                    hour = d["timestamp"].replace(minute=0, second=0, microsecond=0)
                    if hour in flows_by_hour:
                        # Accumulate battery data (don't replace)
                        flows_by_hour[hour]["battery_charge"] += d["charge_kwh"]
                        flows_by_hour[hour]["battery_discharge"] += d["discharge_kwh"]
            except Exception as e:
                _LOGGER.warning(f"Could not get battery data for flows: {e}")
        
        # Convert to list sorted by timestamp, take first 24 hours
        all_flows = sorted(flows_by_hour.values(), key=lambda x: x["timestamp"])
        hourly_flows = all_flows[:24]
        
        # Calculate summary from the HOURLY data (not cumulative sensors)
        summary = {
            "total_solar": round(sum(d["solar"] for d in hourly_flows), 1),
            "total_import": round(sum(d["grid_import"] for d in hourly_flows), 1),
            "total_export": round(sum(d["grid_export"] for d in hourly_flows), 1),
            "total_battery_charge": round(sum(d["battery_charge"] for d in hourly_flows), 1),
            "total_battery_discharge": round(sum(d["battery_discharge"] for d in hourly_flows), 1),
        }
        
        _LOGGER.warning(f"Energy flow summary (24h): {summary}")

        return {
            "hourly": hourly_flows,
            "summary": summary,
        }

    async def _calculate_cost_breakdown(
        self, usage_data: list[dict], export_data: list[dict]
    ) -> tuple[float, float]:
        """Return (import_cost, export_credit) as separate components for bill display."""
        total_import_kwh = sum(d["value"] for d in usage_data) if usage_data else 0.0
        total_export_kwh = sum(d["value"] for d in export_data) if export_data else 0.0

        if self.import_price_sensor and usage_data:
            import_cost = await self._calculate_cost_with_prices(
                usage_data, self.import_price_sensor, "import"
            )
        else:
            import_cost = total_import_kwh * 0.15

        export_credit = 0.0
        if self.export_price_sensor and export_data:
            export_credit = await self._calculate_cost_with_prices(
                export_data, self.export_price_sensor, "export"
            )
        elif export_data:
            export_credit = total_export_kwh * 0.05

        return import_cost, export_credit

    def _compute_demand_charge(
        self,
        plan,
        usage_data: list[dict],
        opt_result: dict | None,
        actual_days: int,
        tz,
        prefer_actual: bool = False,
    ) -> dict | None:
        """Return the demand-charge bill line, or None if it doesn't apply.

        Demand charges are billed on peak *kW*, not kWh: the highest average
        demand within the network's demand window over the billing period,
        charged at $/kW/day × days. Grid Lens works with hourly energy data, so
        peak kW is approximated as the maximum hourly grid-import kWh in the
        window (1 kWh over 1 h = 1 kW average). Sub-hourly spikes are averaged
        out, so this is a lower bound on the true metered demand.

        Only applies when the customer is on a demand tariff (config toggle) and
        the plan actually carries a demand charge.
        """
        if not self.has_demand_tariff:
            return None
        rate = getattr(plan, 'demand_charge_per_kw_per_day', 0.0) or 0.0
        if rate <= 0 or not getattr(plan, 'demand_charge_active', False):
            return None

        window = getattr(plan, 'demand_window', None) or {}
        hours = window.get('hours', DEFAULT_DEMAND_WINDOW_HOURS)
        if hours == 'all':
            def hour_ok(_h):
                return True
        else:
            hset = set(hours)

            def hour_ok(h):
                return h in hset

        days_spec = window.get('days', 'weekdays')

        def day_ok(weekday: int) -> bool:  # 0=Mon .. 6=Sun
            if days_spec == 'all':
                return True
            if days_spec == 'weekends':
                return weekday >= 5
            return weekday < 5  # 'weekdays' (default)

        # Peak kW within the window. For optimised alternatives the LP dispatch
        # already reflects battery peak-shaving; for the current plan we use the
        # actual metered import. LP schedule slots have no weekday, so the day
        # filter is applied only on the actual-usage path.
        lp_schedule = opt_result.get('schedule', []) if opt_result else []
        opt_peak = opt_result.get('demand_peak_kw') if opt_result else None
        peak_kw = 0.0
        source = 'usage'
        if not prefer_actual and opt_peak is not None:
            # The LP solved the peak directly (weekday-aware window), so bill the
            # exact value it optimised against rather than re-scanning the schedule.
            peak_kw = opt_peak
            source = 'optimised-lp'
        elif not prefer_actual and lp_schedule:
            source = 'optimised'
            for step in lp_schedule:
                h = step.get('hour', 0) % 24
                if hour_ok(h):
                    peak_kw = max(peak_kw, step.get('import_kwh', 0.0))
        else:
            for d in (usage_data or []):
                local_dt = d['timestamp'].astimezone(tz)
                if hour_ok(local_dt.hour) and day_ok(local_dt.weekday()):
                    peak_kw = max(peak_kw, d['value'])

        # Always emit the line for a demand-charge plan the user qualifies for —
        # even at a $0 peak — so a battery that fully shaves the peak is visibly
        # doing its job rather than silently dropping the whole line.
        return {
            'label': window.get('label') or 'Demand charge',
            'peak_kw': round(peak_kw, 3),
            'rate_per_kw_per_day': round(rate, 5),
            'days': actual_days,
            'amount': round(peak_kw * rate * actual_days, 2),
            'window_hours': hours,
            'source': source,
            'approximate': True,
        }

    def _split_capped_kwh(
        self, plan, direction: str, local_dt, kwh: float,
        daily_used: dict, cap_labels: dict,
    ) -> list:
        """Split ``kwh`` at ``local_dt`` across a capped rate's free portion and
        its post-cap rate once ``daily_cap_kwh`` is exceeded for that calendar
        day (e.g. GloBird ZEROHERO's free-window 50 kWh/day import cap, or its
        15 kWh/day Super Export cap).

        ``daily_used`` accumulates free-tier kWh already consumed today per
        (direction, date, rate label) — callers share one dict across a whole
        bill calculation so the running total is correct, and it naturally
        resets per calendar day since the key includes the date.
        ``cap_labels`` collects a display label for any post-cap rate
        encountered, keyed by rounded rate, for callers that build energy
        line items from rate value alone.

        Returns ``[(rate, kwh_at_rate), ...]`` — a single-element list when
        the matched rate has no cap (the common case).
        """
        get_info = plan.get_import_rate_info if direction == "import" else plan.get_export_rate_info
        info = get_info(local_dt)
        rate = info["rate"]
        cap = info.get("daily_cap_kwh")
        after_rate = info.get("rate_after_cap")
        if kwh <= 0 or not cap or after_rate is None:
            return [(rate, kwh)]

        label = info.get("label") or "Energy"
        key = (direction, local_dt.date(), label)
        used = daily_used.get(key, 0.0)
        remaining = max(0.0, cap - used)
        free_kwh = min(kwh, remaining)
        over_kwh = kwh - free_kwh
        daily_used[key] = used + free_kwh

        parts = []
        if free_kwh > 1e-9:
            parts.append((rate, free_kwh))
            cap_labels.setdefault(round(rate, 4), f"{label} (first {cap:g} kWh/day)")
        if over_kwh > 1e-9:
            parts.append((after_rate, over_kwh))
            cap_labels.setdefault(round(after_rate, 4), f"{label} (after {cap:g} kWh/day)")
        return parts

    def _calculate_actual_conditional_credits(
        self, plan, usage_data: list[dict], actual_days: int,
    ) -> tuple[float, dict]:
        """Evaluate a plan's conditional day-credits (e.g. GloBird ZEROHERO's
        $1/day for <=0.03 kWh/hour import, 6-9pm) against ACTUAL historical
        import rather than the LP's hypothetical dispatch. Used only for the
        plan the household is actually on: real behaviour, not an optimizer's
        plan, determines whether the credit was earned each day.

        Returns (total_amount, per_credit_detail) — detail mirrors
        RetailerPlan.get_display_breakdown()'s 'conditional_credits' shape
        (amount/days_earned/days_total per label) so both code paths render
        identically on the dashboard.
        """
        from collections import defaultdict
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")

        credit_defs = plan.get_conditional_credits()
        if not credit_defs or not usage_data:
            return 0.0, {}

        total = 0.0
        detail: dict = {}
        for credit in credit_defs:
            window = credit.get("window") or {}
            threshold = float(credit.get("threshold_kwh") or 0.0)
            amount_per_day = float(credit.get("amount_per_day") or 0.0)
            label = credit.get("label", "Conditional Credit")

            hourly_kwh: dict = defaultdict(float)  # (date_ordinal, hour) -> kWh
            for d in usage_data:
                local_dt = d["timestamp"].astimezone(tz)
                if PlanFromData._in_window(window, local_dt):
                    hourly_kwh[(local_dt.date().toordinal(), local_dt.hour)] += d["value"]

            hours_by_day: dict = defaultdict(set)
            for (day_ord, hour) in hourly_kwh:
                hours_by_day[day_ord].add(hour)

            days_earned = 0
            for day_ord, hours_present in hours_by_day.items():
                if all(hourly_kwh[(day_ord, h)] <= threshold for h in hours_present):
                    days_earned += 1
            days_total = len(hours_by_day)

            if days_total:
                amount = round(days_earned * amount_per_day, 2)
                total += amount
                detail[label] = {
                    "amount": amount,
                    "days_earned": days_earned,
                    "days_total": days_total,
                }
        return round(total, 2), detail

    def _compute_bill_items(
        self,
        plan,
        usage_data: list[dict],
        export_data: list[dict],
        actual_days: int,
        import_cost_actual: float = None,
        export_credit_actual: float = None,
        comparison_total: float = None,
        opt_result: dict = None,
        pea_result: dict = None,
        export_fine_data: list[dict] = None,
        cl_devices: list[dict] = None,
    ) -> dict:
        """Return itemised bill breakdown matching Australian electricity bill format.

        All amounts are inc-GST (Australian advertised rates include GST).
        The 'gst_included' line shows the GST component of the total (total / 11).

        For LP-optimised plans, opt_result is used so the quantities reflect the
        solver's dispatch (not historical data).
        """
        from collections import defaultdict
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")

        # Shared across the import and export sections below so a plan with a
        # capped rate on both directions (e.g. GloBird ZEROHERO) tracks each
        # independently; keyed by (direction, date, label) so the free
        # allowance naturally resets each calendar day.
        daily_used: dict = {}
        cap_labels: dict = {}

        supply_amount = round(plan.daily_supply_charge * actual_days, 2)
        subscription_fee = getattr(plan, 'monthly_subscription_fee', 0.0)
        subscription_amount = round(subscription_fee * actual_days / 30.44, 2) if subscription_fee else 0.0

        # lp_schedule is always populated from opt_result when available.
        # import_cost_actual (set only for the current plan) takes priority over LP for bill items.
        lp_schedule = opt_result.get('schedule', []) if opt_result else []

        if import_cost_actual is not None:
            # Current plan: build tier breakdown from actual usage + plan's rate structure.
            # import_cost_actual is the sensor-verified authoritative total cost.
            total_import_kwh = 0.0
            total_export_kwh = sum(d["value"] for d in export_data) if export_data else 0.0
            tier_data: dict = defaultdict(lambda: {'kwh': 0.0, 'cost': 0.0})
            for d in (usage_data or []):
                kwh = d['value']
                total_import_kwh += kwh
                if kwh > 1e-6:
                    local_dt = d['timestamp'].astimezone(tz)
                    for rate, part_kwh in self._split_capped_kwh(
                            plan, "import", local_dt, kwh, daily_used, cap_labels):
                        rk = round(rate, 4)
                        tier_data[rk]['kwh'] += part_kwh
                        tier_data[rk]['cost'] += part_kwh * rk
            dummy_slots = [{'hour': h, 'import_kwh': 0.0, 'import_cost': 0.0,
                            'export_kwh': 0.0, 'export_credit': 0.0} for h in range(48)]
            dummy_sections = plan.get_display_breakdown({'schedule': dummy_slots}).get('sections', [])
            # cap_labels wins on collision — it explicitly names both the free/
            # under-cap and after-cap portions of a capped rate (e.g. "Free Window
            # (first 50 kWh/day)" vs "Free Window (after 50 kWh/day)") so the two
            # never render as the same indistinguishable label; the generic
            # dummy_sections probe only fills in tiers cap_labels doesn't cover.
            rate_to_label: dict = {**{round(s['rate'], 4): s['title']
                                   for s in dummy_sections if s.get('rate', 0) >= 0},
                                   **cap_labels}
            all_rates = sorted(set(tier_data.keys()) | set(rate_to_label.keys()), reverse=True)
            if all_rates:
                energy_lines = [{'label': rate_to_label.get(rk, 'Energy'),
                                 'rate_c': round(rk * 100, 2),
                                 'kwh': round(tier_data[rk]['kwh'], 2),
                                 'amount': round(tier_data[rk]['cost'], 2)}
                                for rk in all_rates]
                # Deliberately NO reconciliation plug here. This used to force
                # energy_lines[0] to absorb any gap between the summed rate×kWh and
                # import_cost_actual, which silently made one line's amount stop
                # equalling its own kwh × rate_c (and could even render it negative).
                # It also concealed the misprice it was papering over: the lines always
                # summed to the total, so the bill looked internally consistent. If a
                # caller ever reintroduces import_cost_actual, a divergence here means
                # the substituted price disagrees with the plan's own tariff — that
                # should surface, not get absorbed.
            else:
                energy_lines = [{'label': 'Energy', 'rate_c': 0,
                                 'kwh': round(total_import_kwh, 2),
                                 'amount': round(import_cost_actual, 2)}]

        elif lp_schedule:
            # LP-optimised plan: build energy lines from the LP schedule's per-slot
            # import_rate (which correctly applies weekday/weekend rates).  To also
            # show tiers where the optimizer achieved 0 grid import, we probe the
            # plan's full rate structure using a zero-kWh dummy schedule.
            #
            # Capped rates (e.g. GloBird ZEROHERO's 50 kWh/day free-window import cap):
            # the battery-dispatch solver (BatteryOptimizer, wired via import_caps/
            # export_caps built by retailer_plans.build_rate_caps) already tracks the
            # daily free-tier budget when deciding dispatch, and each schedule step
            # carries an explicit free/over-cap kWh split (import_cap_free_kwh /
            # import_cap_over_kwh) instead of one blended import_rate — bucket those
            # directly so a day that crosses the cap mid-hour doesn't fragment into an
            # odd one-off blended-rate tier.
            total_import_kwh = 0.0
            total_export_kwh = opt_result.get('total_export_kwh', 0.0)
            tier_data: dict = defaultdict(lambda: {'kwh': 0.0, 'cost': 0.0})
            for step in lp_schedule:
                imp = step.get('import_kwh', 0.0)
                total_import_kwh += imp
                free = step.get('import_cap_free_kwh', 0.0)
                over = step.get('import_cap_over_kwh', 0.0)
                if free > 1e-9:
                    rk = round(step.get('import_cap_free_rate', 0.0), 4)
                    tier_data[rk]['kwh']  += free
                    tier_data[rk]['cost'] += free * rk
                if over > 1e-9:
                    rk = round(step.get('import_cap_over_rate', 0.0), 4)
                    tier_data[rk]['kwh']  += over
                    tier_data[rk]['cost'] += over * rk
                uncapped = imp - free - over
                if uncapped > 1e-6:
                    rk = round(step.get('import_rate', 0.0), 4)
                    tier_data[rk]['kwh']  += uncapped
                    tier_data[rk]['cost'] += uncapped * rk

            # Discover plan-defined rate tiers and their labels via a dummy schedule
            # (all-zero kWh, 48 h so days=2 avoids division-by-zero in some plans).
            dummy_slots = [{'hour': h, 'import_kwh': 0.0, 'import_cost': 0.0,
                            'export_kwh': 0.0, 'export_credit': 0.0} for h in range(48)]
            dummy_sections = plan.get_display_breakdown({'schedule': dummy_slots}).get('sections', [])
            # cap_labels (from build_rate_caps, carried on opt_result) wins on collision —
            # same precedence, and same reasoning, as the actual-usage branch above: it
            # explicitly names both the free/under-cap and after-cap portions so they
            # never render as the same indistinguishable label.
            rate_to_label: dict = {
                **{
                    round(s['rate'], 4): s['title']
                    for s in dummy_sections
                    if s.get('cost', 0) >= 0 and s.get('rate', 0) >= 0
                },
                **(opt_result.get('cap_labels', {}) if opt_result else {}),
            }

            all_rates = sorted(set(tier_data.keys()) | set(rate_to_label.keys()), reverse=True)
            if all_rates:
                energy_lines = [
                    {
                        'label': rate_to_label.get(rk, 'Energy'),
                        'rate_c': round(rk * 100, 2),
                        'kwh': round(tier_data[rk]['kwh'], 2),
                        'amount': round(tier_data[rk]['cost'], 2),
                    }
                    for rk in all_rates
                ]
            else:
                energy_lines = [{'label': 'Energy (grid)', 'rate_c': 0, 'kwh': 0.0, 'amount': 0.0}]

        else:
            # Historical fallback (no LP result available)
            total_import_kwh = sum(d["value"] for d in usage_data) if usage_data else 0.0
            total_export_kwh = sum(d["value"] for d in export_data) if export_data else 0.0
            tier_data = defaultdict(lambda: {'kwh': 0.0, 'cost': 0.0})
            for d in usage_data:
                local_dt = d['timestamp'].astimezone(tz)
                for rate, part_kwh in self._split_capped_kwh(
                        plan, "import", local_dt, d['value'], daily_used, cap_labels):
                    rk = round(rate, 4)
                    tier_data[rk]['kwh'] += part_kwh
                    tier_data[rk]['cost'] += part_kwh * rate

            sorted_rates = sorted(tier_data.keys())
            n = len(sorted_rates)
            label_map = {}
            if n == 0:
                energy_lines = [{'label': 'Energy (grid)', 'rate_c': 0, 'kwh': 0.0, 'amount': 0.0}]
            elif n == 1:
                label_map[sorted_rates[0]] = 'Energy'
            elif n == 2:
                label_map[sorted_rates[0]] = 'Off-peak energy'
                label_map[sorted_rates[1]] = 'Peak energy'
            else:
                label_map[sorted_rates[0]] = 'Off-peak energy'
                label_map[sorted_rates[-1]] = 'Peak energy'
                for r in sorted_rates[1:-1]:
                    label_map[r] = 'Shoulder energy'
            # Prefer the real "(over cap)" label discovered while splitting,
            # over the generic off-peak/peak/shoulder positional heuristic.
            label_map.update({rk: lbl for rk, lbl in cap_labels.items() if rk in tier_data})

            if n > 0:
                energy_lines = sorted([
                    {
                        'label': label_map.get(rk, 'Energy'),
                        'rate_c': round(rk * 100, 2),
                        'kwh': round(data['kwh'], 2),
                        'amount': round(data['cost'], 2),
                    }
                    for rk, data in tier_data.items()
                ], key=lambda x: x['rate_c'], reverse=True)

        # Controlled Load: move any CL-wired device's energy out of general consumption
        # and price it separately at this plan's flat CL rate. CL rates have no
        # time-window structure (see ControlledLoadRateIR/get_controlled_load_rate) —
        # unlike the tiered energy_lines above, WHEN the device drew that energy is
        # irrelevant to its cost, so a flat total-kWh price is exact, not an
        # approximation, regardless of the LP's own dispatch timing.
        #
        # The general-tier reduction below IS an approximation: a device's energy is
        # only removed from usage_data/lp_schedule to begin with when it's genuinely on
        # a separate register the main sensor never saw (the normal real case), so most
        # of the time there's nothing to reduce here. The "in_aggregate" case (a
        # hypothetical load currently mixed into the general reading, being evaluated
        # for a move to CL) has no real per-hour shape to subtract precisely — so it's
        # approximated as a uniform proportional reduction across every tier, rather
        # than interval-precise.
        cl_amount = 0.0
        cl_kwh_in_aggregate = 0.0
        cl_lines: list = []
        for device in (cl_devices or []):
            rate_info = plan.get_controlled_load_rate(device['register'])
            if rate_info is None:
                continue  # this plan doesn't itemise this register — leave it in general consumption
            dev_kwh = device['daily_kwh'] * actual_days
            dev_cost = dev_kwh * float(rate_info['rate'])
            dev_supply = float(rate_info.get('daily_supply_charge') or 0.0) * actual_days
            cl_lines.append({
                'label': f"Controlled Load — {device['name']}",
                'register': device['register'],
                'rate_c': round(float(rate_info['rate']) * 100, 2),
                'kwh': round(dev_kwh, 2),
                'amount': round(dev_cost + dev_supply, 2),
            })
            cl_amount += dev_cost + dev_supply
            if device.get('in_aggregate'):
                cl_kwh_in_aggregate += dev_kwh
        cl_amount = round(cl_amount, 2)

        if cl_kwh_in_aggregate > 1e-9 and total_import_kwh > 1e-9:
            keep_ratio = max(0.0, (total_import_kwh - cl_kwh_in_aggregate) / total_import_kwh)
            for line in energy_lines:
                line['kwh'] = round(line['kwh'] * keep_ratio, 2)
                line['amount'] = round(line['amount'] * keep_ratio, 2)
            total_import_kwh *= keep_ratio

        # FiT: priority order:
        #   1. Current plan with spot export (Amber-as-current): use actual sensor credit
        #   2. LP-optimised non-current plan: use solver's per-step export credit
        #   3. Current plan with fixed FiT: apply plan.get_export_rate() to actual export_data
        fit_credit = 0.0
        fit_eligible_kwh = 0.0
        if export_credit_actual is not None and getattr(plan, 'spot_export_pricing', False):
            # Spot-priced export for the current plan (e.g. Amber): use sensor's actual credit.
            fit_credit = export_credit_actual
            fit_eligible_kwh = total_export_kwh
        elif lp_schedule and export_credit_actual is None:
            # LP-optimised non-current plan: use per-step export credit from the solver.
            for step in lp_schedule:
                exp = step.get('export_kwh', 0.0)
                cred = step.get('export_credit', 0.0)
                if exp > 1e-6 and step.get('export_rate', 0.0) > 0:
                    fit_credit += cred
                    fit_eligible_kwh += exp
        else:
            # Current plan with fixed FiT (e.g. Flow Power): apply plan rate to actual export_data.
            #
            # Prefer 5-minute short-term statistics: FiT windows sit on half-hour
            # boundaries (Flow Power 17:30-19:30), and matching an hourly bucket's
            # start timestamp against such a window misattributes up to an hour of
            # export at each edge (real case: 121 kWh of 17:30-18:00 export dropped
            # because the 17:00 bucket "starts" outside the window). 5-minute buckets
            # attribute export to the window it actually occurred in.
            fine = export_fine_data or []
            covered_hours = set()
            for d in fine:
                local_dt = d['timestamp'].astimezone(tz)
                covered_hours.add(local_dt.replace(minute=0, second=0, microsecond=0))
                for rate, part_kwh in self._split_capped_kwh(
                        plan, "export", local_dt, d['value'], daily_used, cap_labels):
                    if rate > 0:
                        fit_credit += part_kwh * rate
                        fit_eligible_kwh += part_kwh
            # Hourly fallback for hours outside short-term retention: pro-rate each
            # bucket across its two half-hours (windows never split finer than :30),
            # assuming export is spread evenly within the bucket. Not cap-aware
            # (only fires for data beyond 5-minute short-term retention, i.e. old
            # historical gaps) — a capped export window here is priced at its free
            # rate regardless of how much was already exported that day.
            for d in export_data:
                local_dt = d['timestamp'].astimezone(tz)
                if local_dt.replace(minute=0, second=0, microsecond=0) in covered_hours:
                    continue
                r0 = plan.get_export_rate(local_dt)
                r30 = plan.get_export_rate(local_dt + timedelta(minutes=30))
                halves = [r for r in (r0, r30) if r > 0]
                if halves:
                    fit_credit += d['value'] / 2 * sum(halves)
                    fit_eligible_kwh += d['value'] / 2 * len(halves)
        fit_credit = round(fit_credit, 2)
        fit_eligible_kwh = round(fit_eligible_kwh, 2)
        fit_rate_c = round(fit_credit / fit_eligible_kwh * 100, 2) if fit_eligible_kwh > 0 else 0.0

        # Demand charge (peak-kW), only when the customer is on a demand tariff and
        # the plan carries one. Uses actual metered usage for the current plan and
        # the LP dispatch (battery peak-shaving) for optimised alternatives.
        demand = self._compute_demand_charge(
            plan, usage_data, opt_result, actual_days, tz,
            prefer_actual=import_cost_actual is not None,
        )
        demand_amount = demand['amount'] if demand else 0.0

        energy_charges = round(sum(line['amount'] for line in energy_lines), 2)
        gross_charges = round(
            energy_charges + supply_amount + subscription_amount + demand_amount + cl_amount, 2)

        # Price Efficiency Adjustment (Flow Power).
        # Use computed PEA from AEMO spot prices when available; no fallback estimate.
        pea_credit = 0.0
        pea_breakdown = None
        if pea_result:
            pea_credit = pea_result['pea_credit']
            pea_breakdown = pea_result

        # VPP participation credit (e.g. EA BatteryEase $15/month, AGL $80/yr, ENGIE $240/yr).
        vpp_daily = getattr(plan, 'fixed_daily_credit', 0.0)
        vpp_credit = round(vpp_daily * actual_days, 2) if vpp_daily else 0.0

        # Conditional day-credits (e.g. GloBird ZEROHERO's $1/day for <=0.03 kWh/hour
        # import, 6-9pm). For the current plan, evaluate against ACTUAL historical
        # import (real behaviour decides what was earned) rather than the LP's
        # hypothetical opt_result — otherwise this total silently omitted the credit
        # entirely, since it's a distinct mechanism from the priced energy_lines above.
        # For alternative/LP-optimised plans, reuse the LP's own earned-day count
        # (already correctly reflected in their ranking cost) so this itemised total
        # matches that ranking instead of double-guessing dispatch that never ran.
        if import_cost_actual is not None:
            conditional_amount, conditional_detail = self._calculate_actual_conditional_credits(
                plan, usage_data, actual_days,
            )
        elif opt_result and opt_result.get('conditional_credits'):
            conditional = opt_result['conditional_credits']
            conditional_amount = round(sum(c.get('amount', 0.0) for c in conditional.values()), 2)
            conditional_detail = {
                label: {
                    'amount': round(c.get('amount', 0.0), 2),
                    'days_earned': c.get('days_earned', 0),
                    'days_total': c.get('days_total', 0),
                }
                for label, c in conditional.items()
            }
        else:
            conditional_amount, conditional_detail = 0.0, {}

        net_total = round(
            gross_charges - fit_credit - pea_credit - vpp_credit - conditional_amount, 2)
        gst_included = round(net_total / 11, 2)

        result: dict = {
            'energy_lines': energy_lines,
            'supply': {
                'rate_per_day': round(plan.daily_supply_charge, 4),
                'days': actual_days,
                'amount': supply_amount,
            },
            'subscription': {
                'rate_per_month': subscription_fee,
                'months': round(actual_days / 30.44, 2),
                'amount': subscription_amount,
            } if subscription_amount else None,
            'demand': demand,
            'controlled_load': {'lines': cl_lines, 'amount': cl_amount} if cl_lines else None,
            'fit': {
                'rate_c': fit_rate_c,
                'kwh': fit_eligible_kwh,
                'total_export_kwh': round(total_export_kwh, 2),
                'credit': fit_credit,
            },
            'vpp_credit': round(vpp_credit, 2) if vpp_credit else None,
            'pea_credit': round(pea_credit, 2) if pea_breakdown is not None else None,
            'pea_breakdown': pea_breakdown,
            'conditional_credits': conditional_detail or None,
            'gross_charges': gross_charges,
            'gst_included': gst_included,
            'total': net_total,
        }

        if comparison_total is not None and abs(comparison_total - net_total) > 0.50:
            saving = round(net_total - comparison_total, 2)
            if saving > 0:
                result['optimisation_note'] = (
                    f'Battery optimisation saves ${saving:.2f}'
                    f' (optimised total: ${comparison_total:.2f})'
                )

        return result

    def _compute_hourly_day_profile(
        self, usage_data: list[dict], export_data: list[dict]
    ) -> list[dict]:
        """Return average import/export kWh per hour-of-day (0-23) in Sydney timezone."""
        from collections import defaultdict
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")
        imp_by_h: dict = defaultdict(list)
        exp_by_h: dict = defaultdict(list)
        for d in usage_data:
            imp_by_h[d["timestamp"].astimezone(tz).hour].append(d["value"])
        for d in export_data:
            exp_by_h[d["timestamp"].astimezone(tz).hour].append(d["value"])
        return [
            {
                "hour": h,
                "import_kwh": sum(imp_by_h[h]) / max(len(imp_by_h[h]), 1),
                "export_kwh": sum(exp_by_h[h]) / max(len(exp_by_h[h]), 1),
            }
            for h in range(24)
        ]

    async def _get_avg_price_by_hour(
        self, price_sensor: str, start_time: datetime, end_time: datetime
    ) -> dict:
        """Return {hour: avg_price} from price sensor state history over the period."""
        from collections import defaultdict
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")
        try:
            from homeassistant.components.recorder import history as recorder_history
            states = await get_instance(self.hass).async_add_executor_job(
                recorder_history.state_changes_during_period,
                self.hass, start_time, end_time, price_sensor,
            )
            if not states or price_sensor not in states:
                return {}
            by_hour: dict = defaultdict(list)
            for state in states[price_sensor]:
                try:
                    val = float(state.state)
                except (ValueError, TypeError):
                    continue
                by_hour[state.last_changed.astimezone(tz).hour].append(val)
            return {h: sum(v) / len(v) for h, v in by_hour.items() if v}
        except Exception as exc:
            _LOGGER.warning("Could not build price-by-hour profile for %s: %s", price_sensor, exc)
            return {}

    def _aggregate_kwh_by_hod(self, data: list[dict]) -> dict:
        """Average kWh by hour-of-day (Sydney time). Returns {0-23: avg_kwh}."""
        from collections import defaultdict
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")
        by_hod: dict = defaultdict(list)
        for d in data:
            by_hod[d['timestamp'].astimezone(tz).hour].append(d['value'])
        return {h: sum(v) / len(v) for h, v in by_hod.items() if v}

    def _deferrable_min_kw(self, index: int) -> float:
        """Lowest power device ``index`` can physically be given, kW. 0 = no floor.

        Only a modulating load has one: an EV must not be offered below ~6 A (IEC 61851), so
        its feasible set is ``{0} ∪ [min_kw, max_kw]`` rather than ``[0, max_kw]``.

        **Reserved for a later semi-continuous constraint.** The LP still uses a plain
        continuous ``0..max_kw`` variable and ignores this value entirely — enforcing the hole
        in the feasible set would need a MILP binary per device per slot, which is a real
        solve-time cost on a model that currently goes MILP only for conditional credits.
        Today the floor is enforced downstream, in ``control/modulating_controller.py``, which
        has to own the decision anyway (it is the only layer that sees live surplus). This is
        plumbed through now so the constraint can be switched on without re-threading config
        through three modules.

        Uses the *configured* phase count only (0 → 1). The controller can do better — it
        auto-derives phases from the setpoint entity's own max — but that needs a live entity
        read, and understating a floor nothing yet reads is harmless.
        """
        setpoint = (
            self.deferrable_load_setpoint[index]
            if index < len(self.deferrable_load_setpoint) else ""
        )
        if not setpoint:
            return 0.0
        amps = (
            float(self.deferrable_load_min_current[index])
            if index < len(self.deferrable_load_min_current)
            and self.deferrable_load_min_current[index]
            else DEFAULT_MIN_CHARGE_CURRENT_A
        )
        volts = (
            float(self.deferrable_load_voltage[index])
            if index < len(self.deferrable_load_voltage) and self.deferrable_load_voltage[index]
            else DEFAULT_SUPPLY_VOLTAGE
        )
        phases = (
            int(self.deferrable_load_phases[index])
            if index < len(self.deferrable_load_phases) and self.deferrable_load_phases[index]
            else 1
        )
        return max(0.0, amps * volts * phases) / 1000.0

    async def _get_deferrable_data(
        self, start_time: datetime, end_time: datetime
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Fetch all deferrable load sensors.

        Returns:
            (combined_list, deferrable_loads, per_sensor_hod_avgs) where:
            - combined_list: summed hourly time series (for base-load subtraction)
            - deferrable_loads: per-device LP parameters with 'sensor_id', 'name',
              'daily_kwh', 'max_kw'
            - per_sensor_hod_avgs: list of {hour: avg_kwh} dicts, one per sensor,
              for per-device chart display on market-linked plan profiles
        """
        from collections import defaultdict
        combined: dict = defaultdict(float)
        deferrable_loads: list[dict] = []
        per_sensor_hod_avgs: list[dict] = []
        days = max(1, round((end_time - start_time).total_seconds() / 86400))
        # Same name resolution as sensor.py's _build_deferrable_loads (control switch
        # anchor first, then the Energy Dashboard's per-device label, e.g. "Hot Water")
        # so the power chart's legend/tooltip names match the power-flow card's — a raw
        # friendly_name here would instead surface the underlying sensor's own name
        # (e.g. a Sigenergy smart port) whenever the two diverge.
        dashboard_names = await async_get_energy_dashboard_names(self.hass)

        for i, sensor_id in enumerate(self.deferrable_load_sensors):
            raw = await self._get_usage_data(start_time, end_time, sensor_id)
            if not raw:
                _LOGGER.warning("No statistics data for deferrable sensor %s", sensor_id)
                per_sensor_hod_avgs.append({})
                continue

            divisor = 1.0
            state_obj = self.hass.states.get(sensor_id)
            sw = self.deferrable_load_switches[i] if i < len(self.deferrable_load_switches) else ""
            name = resolve_device_name(
                self.hass, sw or None, sensor_id, dashboard_names=dashboard_names
            ) or sensor_id
            if state_obj:
                unit = state_obj.attributes.get("unit_of_measurement", "")
                if unit == "Wh":
                    divisor = 1000.0
                    _LOGGER.warning("Deferrable sensor %s reports in Wh — dividing by 1000", sensor_id)

            sensor_records: list[dict] = []
            sensor_total = 0.0
            for d in raw:
                kwh = d['value'] / divisor
                combined[d['timestamp']] += kwh
                sensor_total += kwh
                sensor_records.append({'timestamp': d['timestamp'], 'value': kwh})

            per_sensor_hod_avgs.append(self._aggregate_kwh_by_hod(sensor_records))

            max_kw = (
                self.deferrable_load_max_kw[i]
                if i < len(self.deferrable_load_max_kw)
                else 3.5
            )

            # Availability window: the device's stored weekly schedule (7x24
            # per-weekday grid from the dashboard schedule card) when the user has
            # painted one, else unrestricted (any hour) — the schedule card is the
            # only place this is set now (see const.py's note on the retired static
            # deferrable_load_hours config field).
            week = None
            sched_store = self.hass.data.get(DOMAIN, {}).get(
                f"{self.entry.entry_id}_deferrable_schedules"
            )
            if sched_store is not None:
                try:
                    week = await sched_store.async_get(sensor_id)
                except Exception:  # noqa: BLE001 — a broken store must not kill the calc
                    week = None

            daily_kwh = sensor_total / days
            if week is not None:
                from .schedule_grid import max_daily_hours
                window_capacity = max_daily_hours(week) * max_kw
            else:
                window_capacity = 24 * max_kw
            if daily_kwh > window_capacity:
                _LOGGER.warning(
                    "Deferrable %s needs %.1f kWh/day but its availability window "
                    "can only deliver %.1f kWh/day — the optimizer will schedule "
                    "the maximum the window allows",
                    sensor_id, daily_kwh, window_capacity,
                )

            deferrable_loads.append({
                'sensor_id': sensor_id,
                'name': name,
                'daily_kwh': daily_kwh,
                'max_kw': max_kw,
                'min_kw': self._deferrable_min_kw(i),
                'week': week,
            })
            _LOGGER.warning(
                "Deferrable sensor %s (%s): %.2f kWh/day, max %.1f kW, hours %s",
                sensor_id, name, daily_kwh, max_kw,
                "weekly schedule" if week is not None else "all",
            )

        combined_list = [
            {'timestamp': ts, 'value': val}
            for ts, val in sorted(combined.items())
        ]
        return combined_list, deferrable_loads, per_sensor_hod_avgs

    def _subtract_ev_from_load(
        self, load_data: list[dict], ev_data: list[dict]
    ) -> list[dict]:
        """Return base load = load - EV charging, clamped to ≥ 0."""
        if not ev_data:
            return load_data
        ev_map = {
            d['timestamp'].replace(minute=0, second=0, microsecond=0): d['value']
            for d in ev_data
        }
        result = []
        for d in load_data:
            ts = d['timestamp'].replace(minute=0, second=0, microsecond=0)
            ev_kwh = ev_map.get(ts, 0.0)
            result.append({'timestamp': d['timestamp'], 'value': max(0.0, d['value'] - ev_kwh)})
        return result

    async def _get_avg_stat_by_hod(
        self, sensor_id: str, start_time: datetime, end_time: datetime, stat: str = "mean"
    ) -> dict:
        """Return {hour: avg_value} using HA long-term statistics (mean/sum/etc).

        Uses the pre-aggregated hourly statistics rather than raw state changes,
        so it is fast even for high-frequency sensors like battery SOC.
        """
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass, start_time, end_time,
                {sensor_id}, "hour", None, {stat},
            )
            if not stats or sensor_id not in stats:
                return {}
            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo
            tz = ZoneInfo("Australia/Sydney")
            from collections import defaultdict
            by_hod: dict = defaultdict(list)
            for rec in stats[sensor_id]:
                val = rec.get(stat)
                if val is None:
                    continue
                ts = rec["start"]
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts, tz=timezone.utc)
                elif ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                by_hod[ts.astimezone(tz).hour].append(float(val))
            return {h: sum(v) / len(v) for h, v in by_hod.items() if v}
        except Exception as exc:
            _LOGGER.warning("Could not get %s stat for %s: %s", stat, sensor_id, exc)
            return {}

    async def _fetch_5min_prices(
        self, sensor_id: str, start_time: datetime, end_time: datetime
    ) -> list[dict]:
        """Fetch raw 5-minute AEMO price state changes for PEA calculation.

        Returns a list of {timestamp (UTC-aware), value ($/kWh)} records, one per
        state change.  Using raw 5-min samples rather than hourly averages lets
        _compute_pea_credit compute TWAP as the true mean of every dispatch
        interval, and LWAP using the actual per-interval prices within each hour.
        """
        try:
            from homeassistant.components.recorder import history as recorder_history
            states = await get_instance(self.hass).async_add_executor_job(
                recorder_history.state_changes_during_period,
                self.hass, start_time, end_time, sensor_id,
            )
            if not states or sensor_id not in states:
                _LOGGER.warning("No state history for price sensor %s", sensor_id)
                return []
            result = []
            for state in states[sensor_id]:
                try:
                    val = float(state.state)
                except (ValueError, TypeError):
                    continue
                ts = state.last_changed
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                result.append({"timestamp": ts, "value": val})
            _LOGGER.warning(
                "Fetched %d raw 5-min price records for %s", len(result), sensor_id
            )
            return result
        except Exception as exc:
            _LOGGER.warning("Could not fetch 5-min prices for %s: %s", sensor_id, exc)
            return []

    @staticmethod
    def _compute_pea_credit(
        usage_data: list[dict],        # [{timestamp (UTC-aware), value (kWh)}]
        price_series_5min: list[dict], # [{timestamp (UTC-aware), value ($/kWh)}] — raw 5-min readings
        bpea: float = 0.017,           # $/kWh benchmark (~1.7c, adjusted annually by Flow Power)
    ) -> dict:
        """Compute Flow Power Price Efficiency Adjustment using raw 5-minute prices.

        Formula (from flowpower.com.au/residential/pricing/our-pricing/pea-calculated/):
          TWAP = mean of every 5-min dispatch price in the billing period
          LWAP = Σ(avg_price_in_hour × import_kwh) / Σ(import_kwh)
          CPEA = LWAP - TWAP
          PEA  = CPEA - BPEA
          credit = -PEA × total_import_kwh  (negative PEA → credit, positive → surcharge)

        Using raw 5-min samples for TWAP avoids averaging-of-averages bias that
        occurs when some hours have fewer than 12 dispatch intervals (data gaps).
        """
        from collections import defaultdict

        # Build hour_utc → [5-min prices] map for LWAP matching.
        hour_prices: dict = defaultdict(list)
        for d in price_series_5min:
            h = d["timestamp"].replace(minute=0, second=0, microsecond=0)
            hour_prices[h].append(d["value"])

        if not hour_prices:
            return None

        # TWAP: simple average of every 5-min price reading in the billing window.
        all_prices = [p for prices in hour_prices.values() for p in prices]
        twap = sum(all_prices) / len(all_prices)

        # LWAP: consumption-weighted average.  Each hour's kWh is weighted by the
        # mean of the actual 5-min prices recorded within that hour.
        total_kwh = 0.0
        weighted_sum = 0.0
        matched = 0
        for rec in usage_data:
            h = rec["timestamp"].replace(minute=0, second=0, microsecond=0)
            kwh = rec["value"]
            prices_in_hour = hour_prices.get(h)
            if not prices_in_hour:
                continue
            avg_hour_price = sum(prices_in_hour) / len(prices_in_hour)
            matched += 1
            total_kwh += kwh
            weighted_sum += avg_hour_price * kwh

        if total_kwh < 0.01 or matched == 0:
            return None

        lwap = weighted_sum / total_kwh
        cpea = lwap - twap              # $/kWh; negative = shifted to cheap hours
        pea  = cpea - bpea             # $/kWh; subtract benchmark
        pea_credit = -pea * total_kwh  # $; positive = credit to customer

        _LOGGER.warning(
            "PEA: LWAP=%.4f TWAP=%.4f CPEA=%.4f BPEA=%.4f PEA=%.4f "
            "credit=$%.2f on %.1f kWh (%d matched hours, %d 5-min intervals)",
            lwap, twap, cpea, bpea, pea, pea_credit, total_kwh, matched, len(all_prices),
        )
        return {
            "lwap_c":        round(lwap  * 100, 3),
            "twap_c":        round(twap  * 100, 3),
            "cpea_c":        round(cpea  * 100, 3),
            "bpea_c":        round(bpea  * 100, 3),
            "pea_c":         round(pea   * 100, 3),
            "total_kwh":     round(total_kwh,   2),
            "pea_credit":    round(pea_credit,  2),
            "matched_hours": matched,
        }

    def _aggregate_battery_by_hod(self, battery_data: list[dict]) -> dict:
        """Average battery charge/discharge by hour-of-day (Sydney time).

        Returns {hour_of_day: {'charge_kwh': float, 'discharge_kwh': float}}.
        """
        from collections import defaultdict
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")
        sums: dict = defaultdict(lambda: {'charge_kwh': 0.0, 'discharge_kwh': 0.0, 'count': 0})
        for d in battery_data:
            hod = d['timestamp'].astimezone(tz).hour
            sums[hod]['charge_kwh'] += d['charge_kwh']
            sums[hod]['discharge_kwh'] += d['discharge_kwh']
            sums[hod]['count'] += 1
        return {
            h: {
                'charge_kwh':    v['charge_kwh']    / v['count'],
                'discharge_kwh': v['discharge_kwh'] / v['count'],
            }
            for h, v in sums.items()
        }

    def _compute_true_load_data(
        self,
        solar_data: list[dict],
        grid_import_data: list[dict],
        grid_export_data: list[dict],
        battery_data: list[dict],
    ) -> list[dict]:
        """Reconstruct true household load from energy balance.

        true_load = solar + grid_import - grid_export + battery_discharge - battery_charge

        Grid-to-battery charging inflates grid_import without increasing household
        demand; this formula cancels it out so the LP for alternative plans models
        actual appliance demand rather than HEMS-driven grid charging.

        Falls back to grid_import_data when battery data is unavailable.
        """
        if not battery_data:
            return grid_import_data

        solar_map = {
            d['timestamp'].replace(minute=0, second=0, microsecond=0): d['value']
            for d in solar_data
        }
        export_map = {
            d['timestamp'].replace(minute=0, second=0, microsecond=0): d['value']
            for d in grid_export_data
        }
        battery_map = {
            d['timestamp'].replace(minute=0, second=0, microsecond=0): d
            for d in battery_data
        }

        result = []
        for d in grid_import_data:
            ts = d['timestamp'].replace(minute=0, second=0, microsecond=0)
            solar_kwh  = solar_map.get(ts, 0.0)
            export_kwh = export_map.get(ts, 0.0)
            batt       = battery_map.get(ts, {})
            discharge  = batt.get('discharge_kwh', 0.0)
            charge     = batt.get('charge_kwh', 0.0)
            true_load  = solar_kwh + d['value'] - export_kwh + discharge - charge
            result.append({'timestamp': d['timestamp'], 'value': max(0.0, true_load)})
        return result

    def _build_plan_hourly_profile(
        self,
        day_profile: list[dict],
        plan,
        avg_import_prices: dict,
        avg_export_prices: dict,
        start_date: datetime,
    ) -> list[dict]:
        """Apply plan rates to the hourly day profile, returning per-hour cost/income."""
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")
        # Use mid-period date for DST-aware rate lookup
        sample_date = start_date + timedelta(days=15)
        result = []
        for slot in day_profile:
            h = slot["hour"]
            dt = datetime(sample_date.year, sample_date.month, sample_date.day, h, 0, tzinfo=tz)
            if plan.is_market_linked and avg_import_prices:
                imp_rate = avg_import_prices.get(h, 0.15)
                # Fall back to plan.get_export_rate (time-aware) rather than the flat
                # feed_in_tariff=0 so hours where the price sensor has no state change
                # (e.g. Flow Power's FiT sensor only fires at 17:30 and 19:30) still
                # get the correct rate instead of zero.
                plan_exp_rate = plan.get_export_rate(dt)
                exp_rate = avg_export_prices.get(h, plan_exp_rate) if avg_export_prices else plan_exp_rate
            else:
                imp_rate = plan.get_import_rate(dt)
                exp_rate = plan.get_export_rate(dt)
            result.append({
                "hour": h,
                "import_kwh": round(slot["import_kwh"], 4),
                "export_kwh": round(slot["export_kwh"], 4),
                "import_cost": round(slot["import_kwh"] * imp_rate, 4),
                "export_income": round(slot["export_kwh"] * exp_rate, 4),
                "import_rate": round(imp_rate, 4),
                "export_rate": round(exp_rate, 4),
            })
        return result

    def _detect_current_plan(self, days: int) -> tuple:
        """Return (supply_charge, plan_key) for the current plan."""
        plans = self._get_plans()

        # User-configured plan takes priority over auto-detection.
        # current_plan_override may be a slug ID or a "Retailer - Plan Name" string.
        if self.current_plan_override:
            for plan in plans:
                plan_key = f"{plan.retailer} - {plan.plan_name}"
                if plan_key == self.current_plan_override or getattr(plan, 'plan_id', None) == self.current_plan_override:
                    supply = plan.daily_supply_charge * days
                    _LOGGER.info("Current plan (configured): %s (supply $%.2f)", plan_key, supply)
                    return supply, plan_key

        # Fall back to guessing from the price sensor entity name.
        if not self.import_price_sensor:
            return 25.00, None

        sensor = self.import_price_sensor.lower()
        for plan in plans:
            retailer_slug = plan.retailer.lower().replace(" ", "_")
            if retailer_slug in sensor or plan.retailer.lower().split()[0] in sensor:
                supply = plan.daily_supply_charge * days
                plan_key = f"{plan.retailer} - {plan.plan_name}"
                _LOGGER.info("Current plan (auto-detected): %s (supply $%.2f)", plan_key, supply)
                return supply, plan_key

        return 25.00, None  # Fallback

    async def _get_usage_data(
        self, start_time: datetime, end_time: datetime, sensor_id: str = None,
        period: str = "hour",
    ) -> list[dict]:
        """Get historical usage data from HA statistics (change values per period).

        period="hour" reads long-term statistics; period="5minute" reads short-term
        statistics, which only exist within the recorder's retention window — callers
        using 5minute must tolerate partial or empty coverage.
        """
        sensor = sensor_id or self.energy_sensor
        if not sensor:
            return []

        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_time,
                end_time,
                {sensor},
                period,
                None,
                {"change"},
            )

            if not stats or sensor not in stats:
                if period == "hour":
                    _LOGGER.warning(f"No long-term statistics for {sensor}")
                else:
                    _LOGGER.info(f"No {period} short-term statistics for {sensor}")
                return []

            usage_data = []
            for record in stats[sensor]:
                change = record.get("change")
                if change is None:
                    continue
                kwh = max(0.0, float(change))
                ts = record["start"]
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts, tz=timezone.utc)
                elif ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                usage_data.append({
                    "timestamp": ts,
                    "hour": ts.hour,
                    "value": kwh,
                    "hourly_rate": kwh,
                })

            total = sum(d["value"] for d in usage_data)
            _LOGGER.info(f"Statistics: {len(usage_data)} hourly records for {sensor}, total {total:.2f} kWh")
            return usage_data

        except Exception as e:
            _LOGGER.error(f"Error fetching statistics for {sensor}: {e}", exc_info=True)
            return []

    def _calculate_grid_import(
        self, load_data: list[dict], solar_data: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """Calculate grid import and export by comparing solar with load.
        
        Grid Import = Total Load - Solar Production (when load > solar)
        Grid Export = Solar Production - Total Load (when solar > load)
        
        Returns: (grid_import_data, grid_export_data)
        
        Note: This is approximate as timestamps may not align perfectly.
        """
        # Create a dict of solar production by timestamp (rounded to nearest hour)
        solar_by_hour = {}
        for solar in solar_data:
            # Round timestamp to nearest hour for matching
            hour_key = solar["timestamp"].replace(minute=0, second=0, microsecond=0)
            if hour_key not in solar_by_hour:
                solar_by_hour[hour_key] = 0
            solar_by_hour[hour_key] += solar["value"]
        
        # Calculate import/export for each period
        grid_import_data = []
        grid_export_data = []
        
        for load in load_data:
            hour_key = load["timestamp"].replace(minute=0, second=0, microsecond=0)
            solar_kwh = solar_by_hour.get(hour_key, 0)
            
            if solar_kwh > load["value"]:
                # Exporting to grid
                export_kwh = solar_kwh - load["value"]
                grid_export_data.append({
                    "timestamp": load["timestamp"],
                    "hour": load["hour"],
                    "value": export_kwh,
                    "hourly_rate": load.get("hourly_rate", 0),
                })
            elif load["value"] > solar_kwh:
                # Importing from grid
                import_kwh = load["value"] - solar_kwh
                grid_import_data.append({
                    "timestamp": load["timestamp"],
                    "hour": load["hour"],
                    "value": import_kwh,
                    "hourly_rate": load.get("hourly_rate", 0),
                })
        
        total_load = sum(d["value"] for d in load_data)
        total_solar = sum(solar_by_hour.values())
        total_import = sum(d["value"] for d in grid_import_data)
        total_export = sum(d["value"] for d in grid_export_data)
        
        _LOGGER.info(f"Grid flow calculation: {total_load:.2f} kWh load, {total_solar:.2f} kWh solar → {total_import:.2f} kWh import, {total_export:.2f} kWh export")
        
        return grid_import_data, grid_export_data

    async def _get_battery_behavior(
        self, start_time: datetime, end_time: datetime
    ) -> List[dict]:
        """Get actual battery charge/discharge behavior from signed power sensor.
        
        Args:
            start_time: Start of period
            end_time: End of period
            
        Returns:
            List of dicts with hourly battery behavior:
            {
                'timestamp': datetime,
                'hour': int (0-23),
                'charge_kwh': float (positive),
                'discharge_kwh': float (positive),
            }
        """
        if not self.battery_power_sensor:
            _LOGGER.warning("No battery power sensor configured")
            return []

        def _unit_divisor(entity_id: str) -> float:
            """Return 1000.0 if sensor reports in W, 1.0 if already kW."""
            state_obj = self.hass.states.get(entity_id)
            if state_obj:
                unit = state_obj.attributes.get("unit_of_measurement", "")
                if unit == "W":
                    return 1000.0
            return 1.0

        charge_divisor = _unit_divisor(self.battery_power_sensor)
        discharge_sensor = self.entry.data.get("battery_discharge_power_sensor")
        discharge_divisor = _unit_divisor(discharge_sensor) if discharge_sensor else 1.0

        _LOGGER.warning(
            "Battery sensors: charge=%s (÷%.0f), discharge=%s (÷%.0f)",
            self.battery_power_sensor, charge_divisor,
            discharge_sensor or "none", discharge_divisor,
        )

        try:
            from homeassistant.components.recorder import history as recorder_history

            sensors_to_fetch = [self.battery_power_sensor]
            if discharge_sensor:
                sensors_to_fetch.append(discharge_sensor)

            all_states = {}
            for sensor_id in sensors_to_fetch:
                fetched = await get_instance(self.hass).async_add_executor_job(
                    recorder_history.state_changes_during_period,
                    self.hass,
                    start_time,
                    end_time,
                    sensor_id,
                )
                if fetched and sensor_id in fetched:
                    all_states[sensor_id] = fetched[sensor_id]

            if not all_states:
                _LOGGER.warning("No historical data for battery sensors")
                return []

            hourly_data: dict = {}

            def _integrate_states(sensor_id: str, divisor: float, sign: float = 1.0) -> None:
                """Integrate power states into hourly_data. sign=1 for charge, -1 for discharge."""
                sensor_states = all_states.get(sensor_id, [])
                prev_ts = prev_pw = None
                for state in sensor_states:
                    try:
                        power_kw = float(state.state) / divisor
                        ts = state.last_changed
                        if prev_ts is not None and prev_pw is not None:
                            dt_h = (ts - prev_ts).total_seconds() / 3600
                            if 0 < dt_h < 1:
                                energy_kwh = prev_pw * dt_h * sign
                                hk = prev_ts.replace(minute=0, second=0, microsecond=0)
                                if hk not in hourly_data:
                                    hourly_data[hk] = {
                                        'timestamp': hk, 'hour': hk.hour,
                                        'charge_kwh': 0.0, 'discharge_kwh': 0.0,
                                    }
                                if energy_kwh > 0:
                                    hourly_data[hk]['charge_kwh'] += energy_kwh
                                else:
                                    hourly_data[hk]['discharge_kwh'] += abs(energy_kwh)
                        prev_ts, prev_pw = ts, power_kw
                    except (ValueError, TypeError):
                        continue

            if discharge_sensor:
                # Two separate unipolar sensors (charge-only + discharge-only, both positive)
                _integrate_states(self.battery_power_sensor, charge_divisor, sign=1.0)
                _integrate_states(discharge_sensor, discharge_divisor, sign=-1.0)
            else:
                # Single signed sensor (positive = charging, negative = discharging)
                _integrate_states(self.battery_power_sensor, charge_divisor, sign=1.0)

            battery_data = sorted(hourly_data.values(), key=lambda x: x['timestamp'])

            total_charge    = sum(d['charge_kwh']    for d in battery_data)
            total_discharge = sum(d['discharge_kwh'] for d in battery_data)
            _LOGGER.warning(
                "Battery behavior: %d hours, %.1f kWh charged, %.1f kWh discharged",
                len(battery_data), total_charge, total_discharge,
            )

            return battery_data
            
        except Exception as e:
            _LOGGER.error(f"Error fetching battery behavior: {e}")
            return []

    async def _calculate_current_plan_cost(self, usage_data: list[dict], export_data: list[dict]) -> float:
        """Calculate the current plan's actual cost from usage and price data.

        Cost = (Import kWh × Purchase Price) - (Export kWh × Feed-in Price)

        Note: Can be NEGATIVE if export credits exceed import costs!
        """
        import_kwh = sum(d["value"] for d in usage_data) if usage_data else 0
        export_kwh = sum(d["value"] for d in export_data) if export_data else 0

        _LOGGER.warning(f"Current plan calculation: import_kwh={import_kwh:.2f}, export_kwh={export_kwh:.2f}")
        _LOGGER.warning(f"Has price sensor: {bool(self.import_price_sensor)}, Has feedin sensor: {bool(self.export_price_sensor)}")

        if not self.import_price_sensor:
            # Estimate without price sensor
            import_cost = import_kwh * 0.15  # ~15c/kWh average
            export_credit = export_kwh * 0.05  # ~5c/kWh average feed-in
            net_cost = import_cost - export_credit
            _LOGGER.warning(f"Current plan cost (estimated): ${import_cost:.2f} import - ${export_credit:.2f} export = ${net_cost:.2f}")
            return net_cost  # Can be negative!

        # Use ACTUAL prices from the configured price sensors
        import_cost = await self._calculate_cost_with_prices(
            usage_data,
            self.import_price_sensor,
            "import"
        )

        export_credit = 0
        if self.export_price_sensor and export_data:
            export_credit = await self._calculate_cost_with_prices(
                export_data,
                self.export_price_sensor,
                "export"
            )

        net_cost = import_cost - export_credit
        _LOGGER.warning(f"Current plan cost (ACTUAL prices): ${import_cost:.2f} import - ${export_credit:.2f} export = ${net_cost:.2f}")

        return net_cost  # Can be negative - you made money!

    async def _calculate_cost_with_prices(
        self,
        usage_data: list[dict],
        price_sensor: str,
        flow_type: str = "import"
    ) -> float:
        """Calculate cost using actual price data from sensor.
        
        Args:
            usage_data: List of usage data with timestamps and values
            price_sensor: Entity ID of price sensor
            flow_type: "import" or "export"
            
        Returns:
            Total cost for the period
        """
        if not usage_data:
            return 0.0
        
        # Get price history for the same period
        start_time = min(d["timestamp"] for d in usage_data)
        end_time = max(d["timestamp"] for d in usage_data)
        
        try:
            from homeassistant.components.recorder import history as recorder_history
            price_states = await get_instance(self.hass).async_add_executor_job(
                recorder_history.state_changes_during_period,
                self.hass,
                start_time,
                end_time,
                price_sensor,
            )
            
            if not price_states or price_sensor not in price_states:
                _LOGGER.warning(f"No price history for {price_sensor} - using estimates")
                avg_price = 0.15 if flow_type == "import" else 0.05
                total_kwh = sum(d["value"] for d in usage_data)
                return total_kwh * avg_price
            
            # Build price lookup by timestamp
            price_by_time = {}
            for state in price_states[price_sensor]:
                try:
                    price = float(state.state)
                    timestamp = state.last_changed
                    # Round to hour for matching
                    hour_key = timestamp.replace(minute=0, second=0, microsecond=0)
                    price_by_time[hour_key] = price
                except (ValueError, TypeError):
                    continue
            
            # Calculate cost by matching usage to prices
            total_cost = 0.0
            matched_kwh = 0
            unmatched_kwh = 0
            
            for usage in usage_data:
                kwh = usage["value"]
                timestamp = usage["timestamp"]
                hour_key = timestamp.replace(minute=0, second=0, microsecond=0)
                
                # Try to find price for this hour
                price = price_by_time.get(hour_key)
                
                if price is not None:
                    # Price sensor reports in $/kWh (e.g. 0.33 = 33c/kWh)
                    total_cost += kwh * price
                    matched_kwh += kwh
                else:
                    # Use average price for unmatched hours (already in $/kWh)
                    avg_price = sum(price_by_time.values()) / len(price_by_time) if price_by_time else (0.15 if flow_type == "import" else 0.05)
                    total_cost += kwh * avg_price
                    unmatched_kwh += kwh
            
            _LOGGER.info(
                f"Calculated {flow_type} cost: ${total_cost:.2f} "
                f"(matched: {matched_kwh:.1f}kWh, estimated: {unmatched_kwh:.1f}kWh)"
            )
            
            return total_cost
            
        except Exception as e:
            _LOGGER.error(f"Error calculating cost with prices: {e}")
            # Fallback to estimates
            avg_price = 0.15 if flow_type == "import" else 0.05
            total_kwh = sum(d["value"] for d in usage_data)
            return total_kwh * avg_price

    async def _calculate_current_plan_cost_with_battery(
        self,
        usage_data: list[dict],
        export_data: list[dict],
        solar_data: list[dict],
        start_time: datetime,
        end_time: datetime,
        battery_data: list[dict] | None = None,
    ) -> float:
        """Calculate the current plan's cost accounting for actual battery behavior.

        If battery is configured:
        - Reads actual battery charge/discharge from sensors
        - This represents what you ACTUALLY did (via EMHASS or other system)
        - Calculates costs based on actual behavior

        If no battery:
        - Falls back to standard calculation

        Args:
            battery_data: pre-fetched battery behavior, when the caller already
                fetched it, to avoid re-running the expensive raw-history query.
        """
        if not self.has_battery or not self.battery_power_sensor:
            # No battery - use standard calculation
            return await self._calculate_current_plan_cost(usage_data, export_data)

        # Get actual battery behavior (fetch only if the caller didn't already)
        if battery_data is None:
            battery_data = await self._get_battery_behavior(start_time, end_time)

        if not battery_data:
            _LOGGER.warning("No battery behavior data - using standard calculation")
            return await self._calculate_current_plan_cost(usage_data, export_data)

        # With battery, we need to account for what was actually imported/exported
        # Battery data shows charge/discharge, which affects grid import/export
        import_kwh = sum(d["value"] for d in usage_data) if usage_data else 0
        export_kwh = sum(d["value"] for d in export_data) if export_data else 0

        # Log actual battery usage
        total_charge = sum(d['charge_kwh'] for d in battery_data)
        total_discharge = sum(d['discharge_kwh'] for d in battery_data)

        _LOGGER.warning(
            f"Current plan with ACTUAL battery: import={import_kwh:.1f}kWh, export={export_kwh:.1f}kWh, "
            f"battery_charge={total_charge:.1f}kWh, battery_discharge={total_discharge:.1f}kWh"
        )

        # Calculate costs using standard method (which already accounts for import/export)
        return await self._calculate_current_plan_cost(usage_data, export_data)

    async def _calculate_plan_cost_with_battery_optimization(
        self,
        plan: RetailerPlan,
        solar_data: list[dict],
        load_data: list[dict],
        export_data: list[dict],
        deferrable_loads: list[dict] = None,
    ) -> Tuple[float, Dict]:
        """Calculate plan cost with OPTIMIZED battery usage.
        
        This shows what the cost WOULD BE if you optimally used your battery
        for this particular plan's rate structure.
        
        Returns:
            Tuple of (total_cost, optimization_result)
        """
        if not self.battery_optimizer:
            _LOGGER.warning("No battery optimizer - falling back to standard calculation")
            return self._calculate_plan_cost_simple(load_data, plan), {}

        # Use historical grid_import as load and solar as generation.
        # This answers: "given the same net grid exchange pattern, what is the optimal
        # battery dispatch under each plan?"  The LP then decides WHEN to import/export
        # rather than trying to re-derive the full household consumption (which is
        # entangled with the existing battery behaviour under the current plan).

        # Build load profile first — it spans the full window including nighttime.
        hourly_load = self._build_hourly_profile(load_data)

        # Build solar aligned to load's timestamp range, defaulting to 0 for missing
        # hours (nighttime). Solar statistics have no records during dark hours, so a
        # naive min(len(solar), len(load)) would truncate the LP to daytime only,
        # preventing the optimizer from scheduling any loads to overnight windows.
        load_hour_keys = sorted(set(
            d['timestamp'].replace(minute=0, second=0, microsecond=0)
            for d in load_data
        ))
        solar_by_hour: dict = {}
        for d in solar_data:
            hk = d['timestamp'].replace(minute=0, second=0, microsecond=0)
            solar_by_hour[hk] = solar_by_hour.get(hk, 0.0) + d['value']
        hourly_solar = [solar_by_hour.get(hk, 0.0) for hk in load_hour_keys]

        T = min(len(hourly_solar), len(hourly_load))
        hourly_solar = hourly_solar[:T]
        hourly_load  = hourly_load[:T]

        _LOGGER.warning(
            "Built profiles: solar=%d h (%.1f kWh), grid_import=%d h (%.1f kWh)",
            len(hourly_solar), sum(hourly_solar),
            len(hourly_load),  sum(hourly_load),
        )

        # Derive start_time from load data so the rate array covers the full window.
        if load_data:
            start_time = min(d['timestamp'] for d in load_data)
        elif solar_data:
            start_time = min(d['timestamp'] for d in solar_data)
        else:
            start_time = datetime.now(timezone.utc) - timedelta(days=2)

        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")

        # Demand-charge peak-shaving inputs (only when the user is on a demand
        # tariff and this plan carries one). Build a per-LP-hour window mask so
        # the optimiser lowers the peak grid import inside the metered window.
        demand_rate = 0.0
        demand_predicate = None
        if (self.has_demand_tariff
                and getattr(plan, 'demand_charge_active', False)
                and getattr(plan, 'demand_charge_per_kw_per_day', 0.0) > 0):
            demand_rate = plan.demand_charge_per_kw_per_day
            window = getattr(plan, 'demand_window', None) or {}
            whours = window.get('hours', DEFAULT_DEMAND_WINDOW_HOURS)
            days_spec = window.get('days', 'weekdays')

            def demand_predicate(local_dt):
                h_ok = True if whours == 'all' else (local_dt.hour in whours)
                wd = local_dt.weekday()
                if days_spec == 'all':
                    d_ok = True
                elif days_spec == 'weekends':
                    d_ok = wd >= 5
                else:
                    d_ok = wd < 5  # weekdays (default)
                return h_ok and d_ok

        hourly_import_rates = []
        hourly_export_rates = []
        local_hods = []  # local hour-of-day per LP hour, for availability masks
        demand_window_mask = [] if demand_predicate else None
        local_dows: list[int] = []  # weekday per LP hour (0=Mon), for weekly schedules
        for hour_idx in range(T):
            local_dt = (start_time + timedelta(hours=hour_idx)).astimezone(tz)
            hourly_import_rates.append(plan.get_import_rate(local_dt))
            hourly_export_rates.append(plan.get_export_rate(local_dt))
            local_hods.append(local_dt.hour)
            local_dows.append(local_dt.weekday())
            if demand_predicate:
                demand_window_mask.append(1 if demand_predicate(local_dt) else 0)

        # Capped rate windows (e.g. GloBird ZEROHERO's 50 kWh/day free-import window,
        # or a capped Super Export credit) — without this the LP would treat the free
        # tier as unlimited and dump/pull arbitrary kWh through it.
        import_caps, export_caps, cap_labels = build_rate_caps(plan, start_time, T)
        # Conditional day-credits (e.g. GloBird ZEROHERO's "$1/day when imports
        # are 0.03 kWh/hour or less, 6pm-9pm") — without this the comparison
        # would understate a plan carrying one by up to its full annual value,
        # since it's a distinct mechanism from build_rate_caps's continuous
        # price tranches. A no-op ([]) for a plan without one.
        conditional_credits = build_conditional_credits(plan, start_time, T)

        # Translate each device's stored weekly schedule (per-weekday half-hour grid,
        # painted on the dashboard schedule card) into a per-LP-hour mask so the
        # optimizer only schedules it when it is actually available (e.g. an EV that
        # is plugged in overnight cannot soak up midday solar). At this LP's hourly
        # resolution a half-allowed hour becomes a fractional mask (0.5), capping the
        # device at half its hourly energy. No stored schedule = no mask = unrestricted.
        from .schedule_grid import hour_fraction
        lp_deferrable_loads = []
        for dev in (deferrable_loads or []):
            week = dev.get('week')
            lp_dev = dict(dev)
            lp_dev['hour_mask'] = (
                [hour_fraction(week, local_dows[t], local_hods[t]) for t in range(T)]
                if week is not None else None
            )
            lp_deferrable_loads.append(lp_dev)

        # Run LP optimiser in a thread pool so the event loop stays responsive.
        import functools
        result = await self.hass.async_add_executor_job(
            functools.partial(
                self.battery_optimizer.optimize_hourly_schedule,
                solar_profile=hourly_solar,
                load_profile=hourly_load,
                import_rates=hourly_import_rates,
                export_rates=hourly_export_rates,
                deferrable_loads=lp_deferrable_loads,
                demand_rate=demand_rate,
                demand_window_mask=demand_window_mask,
                import_caps=import_caps,
                export_caps=export_caps,
                conditional_credits=conditional_credits,
                min_export_price=self._get_min_export_price(),
            )
        )
        # Carried through to _compute_bill_items so capped-rate tiers in the cost
        # breakdown get a real label (e.g. "Free Window... (over cap)") instead of
        # falling into the generic "Energy" bucket.
        result['cap_labels'] = cap_labels
        _LOGGER.warning(
            "Optimiser solver=%s  import=%.1f kWh ($%.2f)  export=%.1f kWh ($%.2f)  net=$%.2f",
            result.get('solver', '?'),
            result['total_import_kwh'], result['total_import_cost'],
            result['total_export_kwh'], result['total_export_credit'],
            result['net_cost'],
        )
        
        # Build a 24-h average day profile from the LP schedule so the dashboard
        # charts show plan-specific import/export patterns rather than historical data.
        N = len(deferrable_loads or [])
        start_local_hour = start_time.astimezone(tz).hour
        hour_sums: dict = {h: {'import_kwh': 0.0, 'export_kwh': 0.0,
                                'import_cost': 0.0, 'export_credit': 0.0,
                                'charge_kwh': 0.0, 'discharge_kwh': 0.0,
                                'soc_percent': 0.0, 'deferrable_kwh': 0.0,
                                'solar_kwh': 0.0,
                                'deferrable_per_device': [0.0] * N,
                                'count': 0}
                           for h in range(24)}
        for step in result.get('schedule', []):
            t = step['hour']
            hod = (start_local_hour + t) % 24
            s = hour_sums[hod]
            s['import_kwh']      += step.get('import_kwh', 0)
            s['export_kwh']      += step.get('export_kwh', 0)
            s['import_cost']     += step.get('import_cost', 0)
            s['export_credit']   += step.get('export_credit', 0)
            s['charge_kwh']      += step.get('charge_kwh', 0)
            s['discharge_kwh']   += step.get('discharge_kwh', 0)
            s['soc_percent']     += step.get('soc_percent', 0)
            s['deferrable_kwh']  += step.get('deferrable_kwh', 0)
            s['solar_kwh']       += step.get('solar_kwh', 0)
            per_dev = step.get('deferrable_per_device', [])
            for ii in range(min(N, len(per_dev))):
                s['deferrable_per_device'][ii] += per_dev[ii]
            s['count']           += 1
        day_profile = []
        for h in range(24):
            s = hour_sums[h]
            n = s['count'] or 1
            imp_kwh  = s['import_kwh']  / n
            exp_kwh  = s['export_kwh']  / n
            imp_cost = s['import_cost'] / n
            exp_cred = s['export_credit'] / n
            day_profile.append({
                'hour':                  h,
                'import_kwh':            round(imp_kwh,  4),
                'export_kwh':            round(exp_kwh,  4),
                'import_cost':           round(imp_cost, 4),
                'export_income':         round(exp_cred, 4),
                'import_rate':           round(imp_cost / imp_kwh, 4) if imp_kwh > 0 else 0,
                'export_rate':           round(exp_cred / exp_kwh, 4) if exp_kwh > 0 else 0,
                'charge_kwh':            round(s['charge_kwh']     / n, 4),
                'discharge_kwh':         round(s['discharge_kwh']  / n, 4),
                'soc_percent':           round(s['soc_percent']    / n, 1),
                'deferrable_kwh':        round(s['deferrable_kwh'] / n, 4),
                'solar_kwh':             round(s['solar_kwh']      / n, 4),
                'deferrable_per_device': [round(s['deferrable_per_device'][ii] / n, 4) for ii in range(N)],
            })
        result['day_profile'] = day_profile

        # Add daily supply charges
        days = len(hourly_solar) / 24
        supply_cost = plan.daily_supply_charge * days

        conditional_total = sum(
            c.get('amount', 0.0) for c in (result.get('conditional_credits') or {}).values()
        )
        total_cost = result['net_cost'] + supply_cost - conditional_total

        _LOGGER.info(
            f"Plan {plan.retailer} - {plan.plan_name} with OPTIMIZED battery: "
            f"import={result['total_import_kwh']:.1f}kWh (${result['total_import_cost']:.2f}), "
            f"export={result['total_export_kwh']:.1f}kWh (${result['total_export_credit']:.2f}), "
            f"supply=${supply_cost:.2f}, conditional_credits=${conditional_total:.2f}, "
            f"total=${total_cost:.2f}"
        )

        return total_cost, result

    def _calculate_plan_cost_simple(
        self, usage_data: list[dict], plan: RetailerPlan
    ) -> float:
        """Calculate plan cost without battery optimization.
        
        Args:
            usage_data: Historical usage data
            plan: RetailerPlan instance
            
        Returns:
            Total cost for the plan
        """
        if not usage_data:
            return 0.0
        
        total_cost = 0.0
        
        # Calculate days in period
        first_timestamp = min(d["timestamp"] for d in usage_data)
        last_timestamp = max(d["timestamp"] for d in usage_data)
        days = (last_timestamp - first_timestamp).days + 1
        
        # Add daily supply charges
        total_cost += plan.daily_supply_charge * days
        
        # Calculate usage costs using plan's rate structure (cap-aware: splits
        # kWh across a capped rate's free portion and its post-cap rate once
        # daily_cap_kwh is exceeded for that calendar day).
        total_kwh = 0
        daily_used: dict = {}
        cap_labels: dict = {}
        for usage in usage_data:
            timestamp = usage["timestamp"]
            kwh = usage["value"]
            total_kwh += kwh

            for rate, part_kwh in self._split_capped_kwh(
                    plan, "import", timestamp, kwh, daily_used, cap_labels):
                total_cost += part_kwh * rate
        
        _LOGGER.debug(
            f"Plan {plan.retailer} - {plan.plan_name}: {total_kwh:.2f} kWh, "
            f"supply: ${plan.daily_supply_charge * days:.2f}, total: ${total_cost:.2f}"
        )

        return total_cost


    def _build_hourly_profile(self, data: list[dict]) -> list[float]:
        """Convert usage data to hourly profile (kWh per hour).
        
        Args:
            data: List of dicts with 'timestamp', 'hour', 'value'
            
        Returns:
            List of hourly kWh values
        """
        if not data:
            return []
        
        # Group by hour and sum values
        hourly_values = {}
        for d in data:
            timestamp = d['timestamp']
            hour_key = timestamp.replace(minute=0, second=0, microsecond=0)
            
            if hour_key not in hourly_values:
                hourly_values[hour_key] = 0
            hourly_values[hour_key] += d['value']
        
        # Sort by timestamp and return as list
        sorted_hours = sorted(hourly_values.items())
        return [value for _, value in sorted_hours]

