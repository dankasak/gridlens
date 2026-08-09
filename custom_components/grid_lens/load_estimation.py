"""LoadEstimator — builds a synthetic energy sensor for a deferrable load with no real
energy feedback path at all (e.g. an IR-blaster-driven aircon), by watching a whole-house
load power sensor around the device's own off->on transitions. Also backs a live power
reading for a device whose only real telemetry is a cumulative (non-real-time) energy
counter — for THAT case, when ``energy_sensor`` is set, calibration instead comes from the
device's own meter across one full on-period, never the whole-house sensor (see
_on_control_state_change) — immune to another load's power swinging during the same
window, which the whole-house path is not.

One instance per "estimated load" config slot (see const.py's CONF_DEFERRABLE_LOAD_EST_*),
or per power-only-inference device (__init__.py._ensure_load_estimators's "step 2").
Learns from ANY off->on transition of the device's control entity — not just ones GridLens
itself commanded — so it bootstraps from ordinary use (manual toggles, climate_scheduler,
Force On) as well as GridLens's own dispatch. See load_estimate_math.py for the pure
accept/reject/EMA/integration logic this wraps with live state.

Persistence is a dedicated Store (mirrors deferrable_overrides.DeferrableOverrideStore),
not RestoreEntity — the estimator (not the sensor entity) owns the number, same "manager
persists, entity just displays it" split used throughout control/ (see
control/manager.py's ControlManager docstring for why RestoreEntity was rejected there).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .load_estimate_math import (
    ema_update,
    energy_sample_avg_w,
    integrate_kwh,
    sample_delta_w,
    sample_is_plausible,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1

# How long after an off->on transition to take the "settled" reading — long enough for a
# compressor or similar to finish spinning up, per the aircon use case this was built for.
SETTLE_SECONDS = 180.0
# How often the running kWh total is advanced while the device reads "on".
INTEGRATION_INTERVAL_SECONDS = 60


class EstimateStore:
    """One Store per config entry, holding every estimated-load slot's persisted state
    (keyed by a per-slot string key) — same one-store-many-keys shape as
    DeferrableOverrideStore."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}_load_estimates_{entry_id}")
        self._data: dict = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._data = await self._store.async_load() or {}
            self._loaded = True

    async def async_get(self, key: str) -> dict:
        await self._ensure_loaded()
        return dict(self._data.get(key) or {})

    async def async_set(self, key: str, value: dict) -> None:
        await self._ensure_loaded()
        self._data[key] = value
        await self._store.async_save(self._data)


class LoadEstimator:
    def __init__(
        self,
        hass: HomeAssistant,
        *,
        store: EstimateStore,
        store_key: str,
        name: str,
        control_entity_id: str,
        seed_kw: float,
        auto_refine: bool,
        load_power_sensor: str,
        other_control_entity_ids: list[str],
        track_energy: bool = True,
        energy_sensor: str = "",
    ) -> None:
        self.hass = hass
        self._store = store
        self._store_key = store_key
        # Same string as the HA unique_id the caller reserved for this slot's synthetic
        # sensor entity (see __init__.py._ensure_load_estimators) — exposed so sensor.py
        # can use it directly rather than re-deriving it. `power_unique_id`/
        # `power_sensor_entity_id` are set by the caller as plain attributes after
        # construction (not every estimator gets a power sensor reserved at construction
        # time — see _ensure_load_estimators) — never read internally here, purely
        # metadata for sensor.py/_build_deferrable_loads.
        self.unique_id = store_key
        self.power_unique_id: Optional[str] = None
        self.power_sensor_entity_id: Optional[str] = None
        self.name = name
        self.control_entity_id = control_entity_id
        self.seed_kw = max(0.0, float(seed_kw))
        self.auto_refine = bool(auto_refine)
        self.load_power_sensor = load_power_sensor or ""
        self._other_control_entity_ids = [e for e in other_control_entity_ids if e]
        # False for a device that already has its own real cumulative energy sensor
        # (the "power-only inference" case, __init__.py's step 2) — this estimator exists
        # purely to back a live-power reading, so running_kwh must stay unused rather than
        # accumulating a second, redundant (and uncalibrated) energy total alongside the
        # device's real one.
        self.track_energy = bool(track_energy)
        # A real (if coarse/non-real-time) cumulative energy sensor on the device itself —
        # e.g. an ECHONET Lite aircon's measured_cumulative_power_consumption. When set,
        # this is the ONLY calibration source used (see _on_control_state_change): it's
        # immune to any other load's power swinging during the sample window by
        # construction, unlike house-load sampling below, so there's no reason to also run
        # the contamination-prone path once a clean own-meter reading is available.
        self.energy_sensor = energy_sensor or ""

        self.estimated_w = self.seed_kw * 1000.0
        self.sample_count = 0
        self.running_kwh = 0.0
        self.last_sample_delta_w: Optional[float] = None
        self.last_sample_at: Optional[datetime] = None
        self._last_integrated_at: Optional[datetime] = None

        self._pending_baseline_w: Optional[float] = None
        self._pending_since: Optional[datetime] = None
        self._contaminated = False
        self._warned_no_load_sensor = False

        self._energy_on_start_kwh: Optional[float] = None
        self._energy_on_start_at: Optional[datetime] = None

        # Last *confirmed* True/False on/off reading, surviving through intervening
        # unavailable/unknown blips (see _on_control_state_change) — distinct from a raw
        # old_state._is_on() read, which would collapse a blip to None and get treated as
        # a real transition.
        self._confirmed_on: Optional[bool] = None

        self._cancel_control_listener: Optional[Callable] = None
        self._cancel_other_listener: Optional[Callable] = None
        self._cancel_integration_timer: Optional[Callable] = None
        self._cancel_settle: Optional[Callable] = None

    # ------------------------------------------------------------------ lifecycle
    async def async_load(self) -> None:
        """Restore persisted state. Call once, before start()."""
        data = await self._store.async_get(self._store_key)
        self.estimated_w = float(data.get("estimated_w", self.estimated_w))
        self.sample_count = int(data.get("sample_count", 0))
        self.running_kwh = float(data.get("running_kwh", 0.0))
        last_at = data.get("last_integrated_at")
        self._last_integrated_at = dt_util.parse_datetime(last_at) if last_at else None

    def start(self) -> None:
        """Wire the live listeners. Call after async_load()."""
        self._cancel_control_listener = async_track_state_change_event(
            self.hass, [self.control_entity_id], self._on_control_state_change
        )
        if self._other_control_entity_ids:
            self._cancel_other_listener = async_track_state_change_event(
                self.hass, self._other_control_entity_ids, self._on_other_state_change
            )
        if self.track_energy:
            self._cancel_integration_timer = async_track_time_interval(
                self.hass, self._on_integration_tick,
                timedelta(seconds=INTEGRATION_INTERVAL_SECONDS),
            )

    def stop(self) -> None:
        for cancel in (self._cancel_control_listener, self._cancel_other_listener,
                       self._cancel_integration_timer, self._cancel_settle):
            if cancel:
                cancel()
        self._cancel_control_listener = None
        self._cancel_other_listener = None
        self._cancel_integration_timer = None
        self._cancel_settle = None

    # ------------------------------------------------------------------ on/off state
    def _is_on(self, state) -> Optional[bool]:
        if state is None:
            return None
        s = str(state.state).lower()
        if s in ("unknown", "unavailable"):
            return None
        if self.control_entity_id.startswith("climate."):
            return s != "off"
        if s == "on":
            return True
        if s == "off":
            return False
        return None

    @property
    def current_power_w(self) -> float:
        """Best-guess live power draw right now: the current estimate while the control
        entity reads "on", 0.0 otherwise (off, or state unknown/unavailable — never guess
        a positive draw from an uncertain state). Backs GridLensEstimatedPowerSensor for
        any device the Power Flow card would otherwise have no live wattage for."""
        return self.estimated_w if self._is_on(self.hass.states.get(self.control_entity_id)) is True else 0.0

    def _read_load_w(self) -> Optional[float]:
        if not self.load_power_sensor:
            if not self._warned_no_load_sensor:
                _LOGGER.warning(
                    "Load estimator for %s: auto-refine is on but no house load power "
                    "sensor is configured — refinement will never run, staying on the "
                    "manual %.2f kW seed.", self.name, self.seed_kw,
                )
                self._warned_no_load_sensor = True
            return None
        st = self.hass.states.get(self.load_power_sensor)
        if st is None or st.state in ("unknown", "unavailable", None):
            return None
        try:
            value = float(st.state)
        except (TypeError, ValueError):
            return None
        unit = (st.attributes or {}).get("unit_of_measurement")
        return value if unit == "W" else value * 1000.0  # treat anything else as kW

    def _read_energy_kwh(self) -> Optional[float]:
        if not self.energy_sensor:
            return None
        st = self.hass.states.get(self.energy_sensor)
        if st is None or st.state in ("unknown", "unavailable", None):
            return None
        try:
            value = float(st.state)
        except (TypeError, ValueError):
            return None
        unit = (st.attributes or {}).get("unit_of_measurement")
        return value / 1000.0 if unit == "Wh" else value  # treat anything else as kWh

    # ------------------------------------------------------------------ event handlers
    async def _on_control_state_change(self, event) -> None:
        new_state = event.data.get("new_state")
        is_on = self._is_on(new_state)
        now = dt_util.now()
        if is_on is None:
            # Transient unavailable/unknown (e.g. a flaky integration reconnecting) is not
            # a real device transition — ignore it entirely rather than treating it as
            # "off". Previously this fell through to the was-on/is-on comparisons below,
            # which read unavailable the same as a genuine off: a control entity that
            # blips unavailable while genuinely running would spuriously *complete* the
            # in-flight energy sample early (as if it just switched off), then *arm* a
            # brand-new one on reconnect (as if it just switched back on) — chopping one
            # continuous on-period into short, arbitrary fragments. Own-meter calibration
            # reads a coarse, laggy cumulative counter (e.g. ECHONET Lite, updating in
            # ~0.1kWh steps every several minutes); dividing a lagged tick by one of those
            # short fragment durations produced wildly inflated W estimates (observed:
            # 2987W for a unit whose own meter showed ~1kWh/hour = ~1000W sustained).
            return
        was_on = self._confirmed_on
        self._confirmed_on = is_on
        if self.energy_sensor:
            # Own-meter sampling: one observation per full on-period, immune to any other
            # device's power draw — never falls through to house-load sampling below.
            if was_on is not True and is_on is True:
                self._arm_energy_sample(now)
            elif is_on is False and self._energy_on_start_at is not None:
                await self._complete_energy_sample(now)
        elif was_on is not True and is_on is True:
            await self._arm_sample(now)
        elif is_on is False and self._pending_since is not None:
            # Turned off again before the settle window elapsed — the sample window is
            # ambiguous (spin-up interrupted), discard rather than guess.
            self._cancel_pending()
        self._last_integrated_at = now

    def _on_other_state_change(self, event) -> None:
        # Any other configured deferrable device changing state during our pending
        # sample window means the load-power delta can't be cleanly attributed to THIS
        # device — mark the in-flight sample contaminated; _on_settle discards it.
        if self._pending_since is not None:
            self._contaminated = True

    async def _arm_sample(self, now: datetime) -> None:
        if not self.auto_refine:
            return
        baseline = self._read_load_w()
        if baseline is None:
            return
        self._pending_baseline_w = baseline
        self._pending_since = now
        self._contaminated = False
        if self._cancel_settle:
            self._cancel_settle()
        self._cancel_settle = async_call_later(self.hass, SETTLE_SECONDS, self._on_settle)

    def _cancel_pending(self) -> None:
        self._pending_baseline_w = None
        self._pending_since = None
        self._contaminated = False
        if self._cancel_settle:
            self._cancel_settle()
            self._cancel_settle = None

    async def _on_settle(self, _now) -> None:
        self._cancel_settle = None
        baseline = self._pending_baseline_w
        contaminated = self._contaminated
        self._pending_baseline_w = None
        self._pending_since = None
        self._contaminated = False
        if baseline is None:
            return
        if self._is_on(self.hass.states.get(self.control_entity_id)) is not True:
            return  # turned off again right at the boundary — discard
        if contaminated:
            _LOGGER.debug(
                "Load estimator for %s: discarding sample — another deferrable device "
                "changed state during the settle window", self.name,
            )
            return
        settled = self._read_load_w()
        if settled is None:
            return
        delta_w = sample_delta_w(baseline, settled)
        if not sample_is_plausible(delta_w, self.seed_kw):
            _LOGGER.debug(
                "Load estimator for %s: discarding implausible sample delta=%.0fW",
                self.name, delta_w,
            )
            return
        self.estimated_w = ema_update(self.estimated_w, delta_w)
        self.sample_count += 1
        self.last_sample_delta_w = delta_w
        self.last_sample_at = dt_util.now()
        _LOGGER.info(
            "Load estimator for %s: accepted sample delta=%.0fW (n=%d) -> estimate=%.0fW",
            self.name, delta_w, self.sample_count, self.estimated_w,
        )
        await self._persist()

    def _arm_energy_sample(self, now: datetime) -> None:
        kwh = self._read_energy_kwh()
        if kwh is None:
            return
        self._energy_on_start_kwh = kwh
        self._energy_on_start_at = now

    async def _complete_energy_sample(self, now: datetime) -> None:
        start_kwh = self._energy_on_start_kwh
        start_at = self._energy_on_start_at
        self._energy_on_start_kwh = None
        self._energy_on_start_at = None
        if start_kwh is None or start_at is None:
            return
        kwh_now = self._read_energy_kwh()
        if kwh_now is None:
            return
        elapsed_h = max(0.0, (now - start_at).total_seconds()) / 3600.0
        avg_w = energy_sample_avg_w(kwh_now - start_kwh, elapsed_h)
        if avg_w is None:
            _LOGGER.debug(
                "Load estimator for %s: discarding own-meter sample — on-period too "
                "short or energy counter went backwards (reset/rollover)", self.name,
            )
            return
        if not sample_is_plausible(avg_w, self.seed_kw):
            _LOGGER.debug(
                "Load estimator for %s: discarding implausible own-meter sample "
                "avg=%.0fW", self.name, avg_w,
            )
            return
        self.estimated_w = ema_update(self.estimated_w, avg_w)
        self.sample_count += 1
        self.last_sample_delta_w = avg_w
        self.last_sample_at = dt_util.now()
        _LOGGER.info(
            "Load estimator for %s: accepted own-meter sample avg=%.0fW (n=%d) -> "
            "estimate=%.0fW", self.name, avg_w, self.sample_count, self.estimated_w,
        )
        await self._persist()

    async def _on_integration_tick(self, now) -> None:
        is_on = self._is_on(self.hass.states.get(self.control_entity_id))
        last = self._last_integrated_at or now
        elapsed_h = max(0.0, (now - last).total_seconds()) / 3600.0
        self._last_integrated_at = now
        if is_on is True and elapsed_h > 0.0:
            self.running_kwh = integrate_kwh(self.running_kwh, self.estimated_w, elapsed_h)
            await self._persist()

    async def _persist(self) -> None:
        await self._store.async_set(self._store_key, {
            "estimated_w": self.estimated_w,
            "sample_count": self.sample_count,
            "running_kwh": self.running_kwh,
            "last_integrated_at": (self._last_integrated_at or dt_util.now()).isoformat(),
        })

    # ------------------------------------------------------------------ observability
    def status(self) -> dict:
        return {
            "estimated_kw": round(self.estimated_w / 1000.0, 3),
            "current_power_w": round(self.current_power_w, 1),
            "manual_kw": round(self.seed_kw, 3),
            "sample_count": self.sample_count,
            "last_sample_delta_w": (
                round(self.last_sample_delta_w, 1) if self.last_sample_delta_w is not None else None
            ),
            "last_sample_at": self.last_sample_at.isoformat() if self.last_sample_at else None,
            "auto_refine": self.auto_refine,
            "load_power_sensor": self.load_power_sensor or None,
            "energy_sensor": self.energy_sensor or None,
            "track_energy": self.track_energy,
        }
