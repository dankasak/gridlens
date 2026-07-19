"""AdvisoryCoordinator — periodic forecast + optimize, read-only (no battery writes).

Runs independently of the main plan-comparison coordinator on its own timer. It reuses
the main coordinator's already-detected current plan for rates, so it never re-runs the
heavy comparison. Every failure is caught and surfaced as a status string — advisory mode
must never disturb the rest of the integration.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from ..battery_optimizer import BatteryOptimizer
from ..const import DOMAIN
from .forecast import FlatLoadForecaster, ForecastProvider, HourOfDayLoadForecaster
from .load_history import build_hour_of_day_load
from .planner import AdvisoryPlanner
from .rates import PlanRateForecaster
from ..retailer_plans import build_rate_caps

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=2)
# While we can't yet produce a plan (SOC/forecast/plan not up after a restart), poll
# fast so the card recovers within seconds of its dependencies arriving instead of
# waiting for the next slow tick. The Sigen SOC sensor in particular can take several
# minutes to appear after a HAOS restart.
WAITING_INTERVAL = timedelta(seconds=20)
STORE_VERSION = 1
# Only restore a persisted plan this fresh — beyond it the forward horizon is stale
# enough that a blank card until the first live run is preferable.
CACHE_MAX_AGE = timedelta(hours=12)
META_REFRESH = timedelta(minutes=2)  # how often to refresh plan + load history
# 36h so the horizon always contains the NEXT export window (tomorrow's 17:30-19:30),
# giving the optimizer a reason to STORE tomorrow-morning's solar instead of dumping it
# to the grid at $0. A 24h horizon ends ~1h before that peak and produces nonsensical
# $0 exports of surplus solar. (Terminal-SOC end-of-horizon artifact still applies at the
# far tail — see TODO; mitigated by re-optimising every 10 min.)
HORIZON_HOURS = 36
SLOT_MINUTES = 30  # 30-min resolution captures Flow Power's 17:30-19:30 boundaries

# Sensible defaults for this deployment (overridable via entry.data later).
DEFAULT_SOC_SENSOR = "sensor.sigen_plant_ess_soc"
DEFAULT_LOAD_SENSOR = "sensor.sigen_plant_accumulated_consumed_energy"


class AdvisoryCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_advisory", update_interval=UPDATE_INTERVAL
        )
        self.entry = entry
        self._plan = None
        self._load_forecaster = FlatLoadForecaster()
        self._meta_refreshed = None
        # Persists the last successful plan so the card can render it immediately after a
        # restart (see async_load_cached / _persist), rather than sitting blank until the
        # first live optimisation completes.
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_advisory_{entry.entry_id}")
        self._deferrable_params = []  # [{name, daily_kwh, max_kw}] from history
        # Combined deferrable energy by hour-of-day (avg kWh/hour, 24-length). The
        # whole-home load sensor already meters these devices, so this is subtracted from
        # the base-load vector before optimising to avoid double-counting them (see
        # _subtract_deferrable_from_load).
        self._deferrable_load_hod = [0.0] * 24

    # ------------------------------------------------------------------ config
    def _cfg(self, key: str, default):
        return self.entry.data.get(key, default)

    def _soc_sensor(self) -> str:
        return self._cfg("battery_soc_sensor", "") or DEFAULT_SOC_SENSOR

    def _read_soc(self) -> float | None:
        st = self.hass.states.get(self._soc_sensor())
        if st is None or st.state in ("unknown", "unavailable", None):
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ plan
    def _current_plan(self):
        """Build the CONFIGURED current plan directly from API data (deterministic).

        Keyed by the ``current_plan`` id, so we never silently fall back to the wrong
        plan while the main coordinator's async current-plan detection is still running.
        Returns None (→ retry) rather than guessing.
        """
        main = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        calc = getattr(main, "calculator", None)
        plan_data = getattr(calc, "plan_data", None) if calc else None
        if not plan_data:
            return None

        from ..retailer_plans import PlanFromData

        plan_id = self.entry.data.get("current_plan")
        if plan_id and plan_id in plan_data:
            return PlanFromData(plan_data[plan_id])

        # Secondary: match the main coordinator's detected name. Never guess plans[0].
        current_name = (main.data or {}).get("current_plan_name") if main and main.data else None
        if current_name:
            try:
                for p in calc._get_plans():
                    if f"{p.retailer} - {p.plan_name}" == current_name:
                        return p
            except Exception:  # noqa: BLE001
                pass
        return None

    async def _refresh_meta(self) -> None:
        """Refresh the current plan + load history (infrequent)."""
        self._plan = self._current_plan()
        # Deferrable params (and their hour-of-day energy) MUST be fetched before the base
        # load so we can subtract the double-counted deferrable energy out of it below.
        self._deferrable_params = await self._deferrable_device_params()
        vector = await build_hour_of_day_load(
            self.hass, self._cfg("load_sensor", "") or DEFAULT_LOAD_SENSOR
        )
        if vector:
            # Dedup: the whole-home load sensor already includes the EV + Sigen smart-load,
            # so subtract each deferrable device's historical energy before this becomes the
            # base-load forecaster. Otherwise that demand is counted twice — once in the base
            # load AND again as the LP deferrable variables — inflating on-site demand by
            # ~15 kWh/day and forcing phantom grid-charging. Mirrors the main engine's
            # plan_calculator._subtract_ev_from_load (plan_calculator.py:219).
            vector = self._subtract_deferrable_from_load(vector)
            self._load_forecaster = HourOfDayLoadForecaster(vector)
        self._meta_refreshed = dt_util.utcnow()

    async def _deferrable_device_params(self) -> list:
        """Per-device deferrable params (name, daily_kwh, max_kw) from history — reuses the
        main calculator's logic so advisory schedules the same EV/pool loads.

        Also populates ``self._deferrable_load_hod`` (combined deferrable energy by
        hour-of-day, avg kWh/hour) from the same statistics, so the base-load vector can be
        de-duplicated against it (the whole-home sensor already meters these devices)."""
        self._deferrable_load_hod = [0.0] * 24
        if not self._cfg("deferrable_load_sensors", []):
            return []
        main = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        calc = getattr(main, "calculator", None)
        if calc is None:
            return []
        try:
            end = dt_util.utcnow()
            combined, defs, _ = await calc._get_deferrable_data(
                end - timedelta(days=14), end
            )
            # combined = summed hourly deferrable time series; aggregate to a 24-length
            # avg-kWh-per-hour-of-day vector, matching build_hour_of_day_load's units so the
            # subtraction below is per-hour and unit-consistent. Reuses the calculator's own
            # aggregator (same tz/rounding as the base-load HOD it will be subtracted from).
            if combined:
                hod = calc._aggregate_kwh_by_hod(combined)
                self._deferrable_load_hod = [float(hod.get(h, 0.0)) for h in range(24)]
            return defs or []
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Advisory: deferrable device params unavailable: %s", err)
            self._deferrable_load_hod = [0.0] * 24
            return []

    def _subtract_deferrable_from_load(self, load_hod: list[float]) -> list[float]:
        """Base load = whole-home load minus deferrable devices, per hour-of-day, floored 0.

        The whole-home consumption sensor already meters the EV + Sigen smart-load, so their
        energy must be removed before it becomes the base-load forecaster — otherwise demand
        is double-counted (base load AND the LP's deferrable variables). This mirrors
        plan_calculator._subtract_ev_from_load, adapted to the advisory's 24-length
        hour-of-day vector: both operands are average kWh consumed per hour-of-day, so the
        subtraction is per-hour, unit-consistent, and clamped at 0."""
        defer = getattr(self, "_deferrable_load_hod", None)
        if not defer:
            return load_hod
        return [
            max(0.0, load_hod[h] - (defer[h] if h < len(defer) else 0.0))
            for h in range(len(load_hod))
        ]

    def _deferrable_for_horizon(self, bundle) -> list:
        """Build the optimizer's per-device deferrable dicts for THIS horizon: device
        daily_kwh/max_kw + a per-slot availability mask from the hours config."""
        from ..const import parse_hours_spec

        hours_cfg = self._cfg("deferrable_load_hours", []) or []
        out = []
        for i, dev in enumerate(self._deferrable_params or []):
            daily, maxkw = dev.get("daily_kwh", 0.0), dev.get("max_kw", 0.0)
            if daily <= 0 or maxkw <= 0:
                continue
            spec = hours_cfg[i] if i < len(hours_cfg) else "all"
            try:
                allowed = parse_hours_spec(spec)
            except Exception:  # noqa: BLE001
                allowed = None
            mask = None
            if allowed is not None:
                mask = [
                    1 if dt_util.as_local(
                        bundle.start + timedelta(minutes=j * bundle.slot_minutes)
                    ).hour in allowed else 0
                    for j in range(bundle.slots)
                ]
            out.append({"daily_kwh": daily, "max_kw": maxkw, "hour_mask": mask,
                        "name": dev.get("name")})
        return out

    # ------------------------------------------------------------------ cache
    async def async_load_cached(self) -> None:
        """Seed ``self.data`` from the last persisted plan so the card renders the
        previous plan immediately on restart, before live SOC/forecast are ready.

        Call once, before the sensor platform is set up. The plan is replaced by a live
        one within WAITING_INTERVAL of its dependencies arriving; its ``generated_at``
        timestamp (shown on the card) makes the staleness visible in the meantime.
        """
        try:
            cached = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 — a bad cache must never block setup
            _LOGGER.debug("Advisory cache load failed: %s", err)
            return
        if not cached or cached.get("status") != "ok":
            return
        gen = dt_util.parse_datetime((cached.get("attributes") or {}).get("generated_at") or "")
        if gen is None or (dt_util.utcnow() - dt_util.as_utc(gen)) > CACHE_MAX_AGE:
            return
        cached["restored"] = True  # lets the sensor/card flag it as a pre-restart plan
        self.data = cached
        # Poll fast until the first live plan lands, so the restored one is short-lived.
        self.update_interval = WAITING_INTERVAL

    def _persist(self, result: dict) -> None:
        # delay_save batches rapid writes; loss of the very latest plan on a hard crash
        # is harmless since it's regenerated every 10 min.
        self._store.async_delay_save(lambda: result, 5)

    # ------------------------------------------------------------------ update
    async def _async_update_data(self) -> dict:
        try:
            result = await self._run()
        except Exception as err:  # noqa: BLE001 — never propagate to break the platform
            _LOGGER.exception("Advisory update failed: %s", err)
            result = {"status": "error", "reason": str(err)}
        # Changing update_interval here is honoured when the coordinator reschedules its
        # next refresh — far more robust than a manually-chained async_call_later, which
        # could silently stop retrying (as it did in practice, leaving the card blank until
        # the next slow tick).
        if result.get("status") == "ok":
            self.update_interval = UPDATE_INTERVAL  # steady cadence once we have a plan
            self._persist(result)
            return result

        # Not "ok" — deps not up yet (post-restart) or a transient failure. Keep showing
        # the last good plan (restored from disk or a previous live run) instead of blanking
        # the card, and poll fast until a fresh plan lands. Only surface the raw
        # waiting/error state when we have nothing better to show (e.g. a fresh install).
        self.update_interval = WAITING_INTERVAL
        prev = self.data if isinstance(self.data, dict) and self.data.get("status") == "ok" else None
        if prev is not None:
            merged = dict(prev)
            merged["restored"] = True
            if result.get("reason"):
                merged["pending_reason"] = result["reason"]  # why the live plan is pending
            return merged
        return result

    async def _run(self) -> dict:
        if not self._cfg("has_battery", False):
            return {"status": "disabled", "reason": "no battery configured"}

        now = dt_util.utcnow()
        # Refresh plan+load if stale OR if we still have no plan (retry each tick until the
        # main coordinator has detected one — don't get stuck "waiting" for 6h).
        if (
            self._plan is None
            or self._meta_refreshed is None
            or (now - self._meta_refreshed) > META_REFRESH
        ):
            await self._refresh_meta()

        if self._plan is None:
            return {"status": "waiting", "reason": "current plan not available yet"}

        soc = self._read_soc()
        if soc is None:
            return {"status": "waiting", "reason": f"SOC unavailable ({self._soc_sensor()})"}

        provider = ForecastProvider(
            self.hass,
            PlanRateForecaster(self._plan),
            self._load_forecaster,
            slot_minutes=SLOT_MINUTES,
        )
        n_slots = int(HORIZON_HOURS * 60 / SLOT_MINUTES)
        bundle = provider.build(n_slots)
        if bundle is None or bundle.hours == 0:
            return {"status": "waiting", "reason": "forecast unavailable"}

        # The native EMS often charges the battery above our preferred max (e.g. to 100%).
        # Don't let the LP force an immediate $0 discharge just to hit the cap — raise the
        # upper bound to the current SOC so it *holds* that energy for a profitable window.
        # (The cap still applies whenever the battery starts at/below it.)
        cfg_max_soc = float(self._cfg("battery_max_soc", 90.0))
        opt_max_soc = max(cfg_max_soc, soc)
        optimizer = BatteryOptimizer(
            capacity_kwh=float(self._cfg("battery_capacity", 13.5)),
            max_charge_rate_kw=float(self._cfg("battery_max_charge_rate", 5.0)),
            max_discharge_rate_kw=float(self._cfg("battery_max_discharge_rate", 5.0)),
            efficiency_percent=float(self._cfg("battery_efficiency", 95.0)),
            min_soc_percent=float(self._cfg("battery_min_soc", 10.0)),
            max_soc_percent=opt_max_soc,
        )
        # Capped rate windows (e.g. GloBird ZEROHERO's 50 kWh/day free-import window)
        # on the user's actual current plan — without this the live dispatch would
        # treat the free tier as unlimited. A no-op ([], [], {}) for the common case
        # of a plan with no capped rates.
        import_caps, export_caps, _cap_labels = build_rate_caps(
            self._plan, bundle.start, bundle.slots, bundle.slot_minutes
        )
        result = AdvisoryPlanner(optimizer).plan(
            bundle, initial_soc_percent=soc,
            deferrable_loads=self._deferrable_for_horizon(bundle),
            import_caps=import_caps,
            export_caps=export_caps,
        )

        # Feed the fresh plan to the control manager (it acts on it only while the master
        # switch is on; keeping it current means enabling control acts immediately).
        mgr = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_control")
        if mgr is not None:
            mgr.set_plan(result.plan)

        next_action = result.plan[0].action.value if result.plan else "self_use"
        next_power = result.plan[0].power_w if result.plan else 0.0
        return {
            "status": "ok",
            "next_action": next_action,
            "next_power_w": round(next_power, 1),
            "attributes": result.to_attributes(),
            "sources": bundle.sources,
            "plan_name": f"{self._plan.retailer} - {self._plan.plan_name}",
        }
