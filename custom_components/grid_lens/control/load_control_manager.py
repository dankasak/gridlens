"""LoadControlManager — actuation lifecycle for deferrable loads, on/off and modulating.

Deliberately **decoupled** from ``ControlManager``/the inverter HAL (product decision
2026-07-23): deferrable-load control has zero brand-specific logic (any ``switch.*``
behaves the same) and must work for households with no battery configured at all. The
cost is a second ``async_track_time_change`` timer alongside the battery executor's,
accepted to keep this independent of ``has_battery``/inverter config.

**Two clocks.** The 5-minute tick is the plan tick: it evaluates the LP's allocation and
the Greedy Consumption conditions for every enabled device, of either type. A device with a
``number.*`` setpoint (see ``modulating_controller.py``) additionally gets a 30-second fast
tick that blends that plan figure with live export surplus and writes the setpoint —
solar-following at the plan's resolution would be pointless, since a cloud edge is over
long before the next plan tick. The fast timer only exists while at least one *enabled*
device is modulating: a household with no modulating load must not silently acquire a
30-second timer it has no use for.

Per-device opt-in: each controllable load has its own default-OFF master switch (see
``switch.py``); ``enable(i)``/``disable(i)`` gate that device's actuation. Entitlement is
shared with battery control (the existing ``battery_control`` ApiKey column — product
decision 2026-07-23), fails **closed** (no writes until the API confirms), and revoking it
stops actuation immediately.

Deadman = **leave as-is**: on disable, HA stop, or a stale plan, this NEVER forces a load
off — it just stops driving it, leaving the last commanded hardware state in place. Cutting
a real appliance mid-cycle has more consequence than reverting an inverter mode.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CHARGE_POWER_SENSOR,
    CONF_BATTERY_DISCHARGE_POWER_SENSOR,
    CONF_BATTERY_MAX_DISCHARGE_RATE,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE,
    CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_MIN_CURRENT,
    CONF_DEFERRABLE_LOAD_PHASES,
    CONF_DEFERRABLE_LOAD_PLUG_SENSOR,
    CONF_DEFERRABLE_LOAD_START_BUTTON,
    CONF_DEFERRABLE_LOAD_STOP_BUTTON,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_SETPOINT,
    CONF_DEFERRABLE_LOAD_SETPOINT_UNIT,
    CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT,
    CONF_DEFERRABLE_LOAD_SOC_SENSORS,
    CONF_DEFERRABLE_LOAD_SWITCHES,
    CONF_DEFERRABLE_LOAD_VOLTAGE,
    CONF_GRID_POWER_SENSOR,
    CONF_LOAD_POWER_SENSOR,
    CONF_MAX_AC_OUTPUT_KW,
    CONF_MIN_EXPORT_PRICE,
    DEFAULT_MIN_CHARGE_CURRENT_A,
    DOMAIN,
    MODULATION_INTERVAL_SECONDS,
)
from ..inverters.base import BatteryAction
from .executor import DispatchInterval
from .load_controller import DeferrableLoadController

_LOGGER = logging.getLogger(__name__)

# How far ahead Greedy Consumption's forecast-surplus condition looks for energy the plan
# expects to waste (export at or below the Minimum Export Price — see
# DeferrableLoadController's module docstring). Spans most of a solar day so a mid-morning
# tick can already see the afternoon spill it should pre-empt. Since 2026-09-11 the
# condition is *proportional* — it runs the device at the average rate the plan wastes over
# the (reservation-clipped) window, not all-or-nothing against a flat-out bar — so widening
# this no longer raises a bar; it just lets a further-out spill be seen sooner. Kept below a
# full 24 h so the forecast is still worth believing and a device started now is plausibly
# still running when the spill lands.
GREEDY_SURPLUS_LOOKAHEAD_HOURS = 9.0

# The forecast-surplus budget window is clipped at the first slot in the look-ahead where
# the plan itself starts materially discharging the battery (BatteryAction.DISCHARGE at
# >= this power). Past that point the plan is spending the battery on something it values
# (evening peak export, covering a high import rate), so Greedy must not borrow charge
# across it — the reservation-window proxy for "don't discharge below the plan's own SOC
# trajectory" (DispatchInterval carries no per-slot SOC, so this is the available signal).
_RESERVED_DISCHARGE_MIN_W = 300.0

# Below this covered span the forecast-surplus budget is treated as unknowable (fail
# closed) — a sliver of window can't be averaged into a trustworthy rate. Deliberately a
# small absolute floor, not a fraction of the look-ahead: a near reservation legitimately
# shortens the window and must still be actionable.
_MIN_BUDGET_WINDOW_H = 0.5

# The live export-surplus term (see _modulation_target_w) deliberately undershoots true
# breakeven by this much, so ordinary noise lands on the export side more often than the
# import side — added 2026-09-11 on the household's own explicit instruction: a small
# mistaken *export* at a below-floor rate still earns something, a small mistaken *import*
# costs the (usually much higher) import rate, so the two aren't symmetric and shouldn't be
# aimed at with equal weight. Same reasoning as write_deadband_a's "cheaper to undershoot
# than chatter" — just applied to money instead of write frequency. Small relative to a
# modulating load's usual step (a 1 A quantisation step is ~230 W), so it nudges rather than
# meaningfully throttles.
_EXPORT_BIAS_W = 150.0

# The battery-priority correction (see _modulation_target_w) deliberately over-corrects by
# this much, for the same asymmetry reason as _EXPORT_BIAS_W above — added 2026-09-12 on the
# household's own explicit instruction after a fresh incident: a full hour of live stats
# (declining afternoon PV, the Wattpilot tracking it down but the battery still funding a
# residual ~0.25-0.5 kW of the gap the whole time, SOC 100% -> 98.5%) showed the *exact*
# cancellation below (`target_w - discharge_w`) converges to zero discharge only in the
# limit — every real tick lags the live reading it corrects against (30 s modulation
# ticks, amp-step quantisation, the write deadband/rate limit), so in practice it just
# stops the discharge from being made *worse* rather than driving it back to zero. Same
# household stance as the export-bias comment: a little mistaken *export* is cheap, a
# little battery cycling is the thing being avoided here (wear, per the household), so the
# correction should overshoot toward the safe side rather than track the discharge exactly.
_BATTERY_PRIORITY_BIAS_W = 150.0

# Stuck-setpoint-while-importing watchdog (see _check_stuck_import) — added 2026-09-13
# after the ac_output_cap headroom bug (GRIDLENS_CHECKLIST.md, same date) let a modulating
# device's setpoint freeze mid-overshoot with a live, unchanging import for 5+ minutes and
# nothing wrote it to the log; only a human noticing the household bill and asking "why is
# this happening" surfaced it. This check is deliberately bug-agnostic — it doesn't know or
# care WHICH clamp is stuck, only that one is, so it also catches whatever the *next* such
# bug turns out to be. Below this import figure is ordinary CT/meter noise, not a real
# overshoot worth a log line.
_STUCK_IMPORT_THRESHOLD_W = 100.0
# How long the setpoint must sit unchanged while importing above the threshold before this
# warns — long enough that a normal deadband hold (one or two 30s ticks) or the plan's own
# 5-minute-interval boundary never trips it, short enough that a genuinely stuck clamp is
# caught well inside the same charging session rather than discovered after the fact.
_STUCK_IMPORT_MIN_MINUTES = 3.0


class LoadControlManager:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        interval_minutes: int = 5,
        max_plan_age_minutes: float = 30.0,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.interval_minutes = max(1, int(interval_minutes))
        self.max_plan_age = timedelta(minutes=max_plan_age_minutes)

        d = entry.data
        sensors: list[str] = list(d.get(CONF_DEFERRABLE_LOAD_SENSORS, []) or [])
        max_kw: list = list(d.get(CONF_DEFERRABLE_LOAD_MAX_KW, []) or [])
        switches: list = list(d.get(CONF_DEFERRABLE_LOAD_SWITCHES, []) or [])
        climate_on_modes: list = list(d.get(CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE, []) or [])
        # Modulating ("type 2") wiring. Every one of these keys is absent from every config
        # entry saved before the feature existed, so each read is `.get(..., []) or []` plus
        # an index guard below — an old entry sees empty lists everywhere and takes exactly
        # the same on/off path it took before.
        setpoints: list = list(d.get(CONF_DEFERRABLE_LOAD_SETPOINT, []) or [])
        setpoint_units: list = list(d.get(CONF_DEFERRABLE_LOAD_SETPOINT_UNIT, []) or [])
        phases: list = list(d.get(CONF_DEFERRABLE_LOAD_PHASES, []) or [])
        voltages: list = list(d.get(CONF_DEFERRABLE_LOAD_VOLTAGE, []) or [])
        min_currents: list = list(d.get(CONF_DEFERRABLE_LOAD_MIN_CURRENT, []) or [])
        plug_sensors: list = list(d.get(CONF_DEFERRABLE_LOAD_PLUG_SENSOR, []) or [])
        # Momentary start/stop actuation, an alternative to a switch for a charger with
        # neither a stateful switch nor a setpoint that accepts 0 (see
        # ModulatingLoadController's module docstring — found on ha-wattpilot, 2026-09-11).
        start_buttons: list = list(d.get(CONF_DEFERRABLE_LOAD_START_BUTTON, []) or [])
        stop_buttons: list = list(d.get(CONF_DEFERRABLE_LOAD_STOP_BUTTON, []) or [])
        # Per-device hard SOC ceiling (see const.py's CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT
        # and _soc_cutoff_active below) — live-actuation enforcement, independent of and in
        # addition to the LP's own planning-side use of these same two lists
        # (advisory/coordinator.py._deferrable_for_horizon). "" sensor or a 100/unset
        # max_percent means disabled for that device, matching the LP side's convention.
        self._soc_sensors: list = list(d.get(CONF_DEFERRABLE_LOAD_SOC_SENSORS, []) or [])
        self._soc_max_percent: list = list(d.get(CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT, []) or [])
        # Retained for Greedy Consumption's schedule lookup (_schedule_allows_now):
        # sensor_id is the schedule store's key for a device's stored weekly grid —
        # same source advisory/coordinator.py._deferrable_for_horizon already reads.
        self._sensor_ids = sensors
        # Live signed grid power sensor (W, +import/-export) for Greedy Consumption's
        # export-surplus condition. "" = not configured — that condition simply never
        # fires (the import-price-free condition still works without it).
        self._grid_power_sensor: str = d.get(CONF_GRID_POWER_SENSOR) or ""
        # One-shot latch for the "greedy is armed but has no grid reading" warning below.
        # Logged once per manager rather than every 5-minute tick, per CONF_GRID_POWER_SENSOR's
        # own "fails open, logs once" discipline.
        self._warned_no_grid_power = False

        # Battery config for Greedy Consumption's forecast-surplus condition (see
        # DeferrableLoadController's module docstring and _battery_headroom_w below). Reused
        # verbatim from the LP optimiser's own battery config (plan_calculator.py) rather than
        # a control-specific duplicate — same numbers, same source of truth. Gated on the two
        # sensors being set, not a separate has_battery flag: an install with no battery has
        # neither sensor configured either, so checking them directly is enough and avoids a
        # second flag to keep in sync.
        self._battery_soc_sensor: str = d.get(CONF_BATTERY_SOC_SENSOR) or ""
        self._battery_charge_power_sensor: str = d.get(CONF_BATTERY_CHARGE_POWER_SENSOR) or ""
        # Optional second sensor for a battery whose charge-power reading is unipolar
        # (0 while discharging, e.g. Sigenergy's own "Battery Charging Power" — the
        # discharge magnitude lives on a *separate* "Battery Discharging Power" entity
        # instead of going negative on the same one). "" means the charge sensor above is
        # already signed (positive=charging, negative=discharging), the original
        # assumption — see _read_battery_net_power_w(). plan_calculator.py's historical
        # battery-behaviour backtest already reads the same two-sensor shape from this
        # same config key; this is that convention finally reaching the live control path
        # too (found 2026-09-11: with only the charge sensor read, a discharging Sigenergy
        # battery looked like "0 W, not charging" to both _battery_headroom_w() and the
        # live export-surplus term below, so a live battery discharge masked by a
        # near-zero grid reading (itself often the *inverter's own* self-consumption loop
        # holding grid flow near zero, not real solar surplus) was invisible — the surplus
        # term just re-authorised whatever the modulating load already drew, and the
        # forecast-surplus gate thought it had full headroom while the battery was
        # actually being drained to fund the load).
        self._battery_discharge_power_sensor: str = d.get(CONF_BATTERY_DISCHARGE_POWER_SENSOR) or ""
        self._battery_min_soc: float = float(d.get(CONF_BATTERY_MIN_SOC, 10.0))
        self._battery_max_discharge_rate_kw: float = float(d.get(CONF_BATTERY_MAX_DISCHARGE_RATE, 5.0))
        # Usable pack size (kWh). Backs the forecast-surplus condition's transient-dip check
        # (_battery_headroom_kwh): greedy may draw the battery down ahead of a forecast spill
        # only if the battery can absorb that dip without breaching min SOC. 0.0 / unset ->
        # the check can't be done, so the proportional forecast-surplus draw is disabled
        # (same fail-closed discipline as a missing SOC sensor).
        self._battery_capacity_kwh: float = float(d.get(CONF_BATTERY_CAPACITY, 0.0) or 0.0)

        # Inverter/plant AC output ceiling (see const.py's CONF_MAX_AC_OUTPUT_KW) — None
        # when unset (the common case, unchanged behaviour). Paired with the whole-house
        # load sensor already used for load estimation (CONF_LOAD_POWER_SENSOR): together
        # with the grid power sensor above, `load_w - grid_w` gives live combined AC output
        # without needing a vendor-specific "plant output" sensor.
        self._max_ac_output_w: Optional[float] = (
            float(d[CONF_MAX_AC_OUTPUT_KW]) * 1000.0
            if d.get(CONF_MAX_AC_OUTPUT_KW)
            else None
        )
        self._load_power_sensor: str = d.get(CONF_LOAD_POWER_SENSOR) or ""

        # One controller per device that has a control entity configured. Keyed by the
        # device's index in the deferrable lists, so DispatchInterval.deferrable_w[i] lines
        # up with controller i. Devices with neither a setpoint nor a switch stay
        # forecast-only (absent here). A setpoint wins over a switch when both are set: the
        # switch then becomes the modulating controller's start/stop companion rather than
        # the thing GridLens drives directly.
        self.controllers: dict[int, DeferrableLoadController] = {}
        # Indexes driven by ModulatingLoadController — the fast loop's work list, and the
        # test for whether the 30-second timer needs to exist at all.
        self._modulating: set[int] = set()
        # Per-modulating-device live power sensor, resolved once here rather than per tick:
        # the registry walk behind resolve_power_sensor is not free, and the answer can't
        # change without a config reload anyway. "" / missing = no sensor found, which makes
        # the surplus term fall back to "this device contributes nothing to the grid figure".
        self._device_power_sensors: dict[int, str] = {}
        # Per-device state for _check_stuck_import: when the current "importing with an
        # unchanging setpoint" streak began, the commanded_w it began at, and whether this
        # streak has already been warned about (so it logs once per incident, not once per
        # 30s tick for as long as the incident lasts).
        self._import_stuck: dict[int, dict] = {}
        for i, sensor_id in enumerate(sensors):
            sw = switches[i] if i < len(switches) else ""
            setpoint = setpoints[i] if i < len(setpoints) else ""
            if not sw and not setpoint:
                continue
            max_w = float(max_kw[i]) * 1000.0 if i < len(max_kw) else 0.0
            if setpoint:
                # Imported here, not at module scope, following this file's existing idiom
                # (see _device_name / _schedule_allows_now): the modulating path is dead
                # weight for the majority of installs that have no setpoint-controlled load,
                # and keeping it out of the import graph also keeps this module loadable by
                # the offline test harnesses, which stub HA module-by-module.
                from .modulating_controller import ModulatingLoadController

                self.controllers[i] = ModulatingLoadController(
                    hass,
                    name=self._device_name(setpoint or sw, sensor_id),
                    setpoint_entity_id=setpoint,
                    max_w=max_w,
                    switch_entity_id=sw,
                    min_current_a=(
                        float(min_currents[i]) if i < len(min_currents) and min_currents[i]
                        else DEFAULT_MIN_CHARGE_CURRENT_A
                    ),
                    phases=int(phases[i]) if i < len(phases) and phases[i] else 0,
                    voltage=float(voltages[i]) if i < len(voltages) and voltages[i] else 0.0,
                    setpoint_unit=setpoint_units[i] if i < len(setpoint_units) else "",
                    plug_entity_id=plug_sensors[i] if i < len(plug_sensors) else "",
                    climate_on_mode=climate_on_modes[i] if i < len(climate_on_modes) else "",
                    start_button_entity_id=(
                        start_buttons[i] if i < len(start_buttons) else ""
                    ),
                    stop_button_entity_id=(
                        stop_buttons[i] if i < len(stop_buttons) else ""
                    ),
                )
                self._modulating.add(i)
                self._device_power_sensors[i] = self._resolve_device_power(
                    sensor_id, setpoint, sw
                )
                continue
            self.controllers[i] = DeferrableLoadController(
                hass,
                name=self._device_name(sw, sensor_id),
                switch_entity_id=sw,
                max_w=max_w,
                climate_on_mode=climate_on_modes[i] if i < len(climate_on_modes) else "",
            )

        # Per-device state. _want_enabled = user/switch intent (persists across an
        # entitlement blip); _enabled = actually driving now (intent AND entitled).
        self._want_enabled: dict[int, bool] = {i: False for i in self.controllers}
        self._enabled: dict[int, bool] = {i: False for i in self.controllers}
        self._entitled = False  # fail closed until the API confirms

        self._plan: Optional[list[DispatchInterval]] = None
        self._plan_updated_at: Optional[datetime] = None
        self._degraded = False
        self._cancel_timer: Optional[Callable] = None
        # 30-second modulation loop (see the module docstring's "Two clocks"). Shares the
        # 5-minute timer's start/stop lifecycle, but is additionally conditional on at least
        # one enabled device actually being modulating.
        self._fast_cancel: Optional[Callable] = None
        # Per-device sync callbacks the switch entities register to refresh their state
        # when _enabled[i] changes from somewhere other than a direct toggle (entitlement).
        self._on_change: dict[int, Callable] = {}

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_hass_stop)

    def _device_name(self, switch_id: str, sensor_id: str) -> str:
        from ..entity_lookup import resolve_device_name
        return resolve_device_name(self.hass, switch_id, sensor_id) or sensor_id

    def _resolve_device_power(self, sensor_id: str, setpoint_id: str, switch_id: str) -> str:
        """Best live power (W) sensor for one modulating device, or "" if none is findable.

        The energy sensor is the first anchor because it is semantically closest to the
        appliance; the control entity is the fallback for a device configured without one.
        Best-effort by design — the surplus term degrades gracefully to "this device draws an
        unknown amount" rather than the whole feature refusing to start."""
        from ..entity_lookup import resolve_power_sensor
        try:
            return resolve_power_sensor(self.hass, sensor_id, setpoint_id or switch_id) or ""
        except Exception:  # noqa: BLE001 — a registry hiccup must not block setup
            return ""

    def has_controllable(self) -> bool:
        return bool(self.controllers)

    def is_modulating(self, index: int) -> bool:
        """True when device ``index`` is driven by a current/power setpoint rather than
        on/off. The entity platforms use this to decide whether to create the extra
        max-current entity for a device."""
        return index in self._modulating

    # ------------------------------------------------------------------ plan feed
    def set_plan(self, intervals: list[DispatchInterval], updated_at: Optional[datetime] = None) -> None:
        self._plan = sorted(intervals, key=lambda iv: iv.start)
        self._plan_updated_at = updated_at or dt_util.now()
        if self._degraded:
            _LOGGER.info("Load control: fresh plan received — clearing degraded state")
        self._degraded = False

    # ------------------------------------------------------------------ listeners
    def set_state_listener(self, index: int, callback: Optional[Callable]) -> None:
        if callback is None:
            self._on_change.pop(index, None)
        else:
            self._on_change[index] = callback

    def _notify(self, index: int) -> None:
        cb = self._on_change.get(index)
        if cb:
            cb()

    # ------------------------------------------------------------------ entitlement
    async def set_entitled(self, entitled: bool) -> None:
        was = self._entitled
        self._entitled = entitled
        if not entitled:
            # Revoked: stop driving every device (leave hardware as-is), keep intent so a
            # later re-grant auto-resumes without the user re-toggling each switch.
            if any(self._enabled.values()):
                _LOGGER.warning("Load control entitlement revoked — stopping actuation")
            for i in list(self._enabled):
                if self._enabled[i]:
                    self._enabled[i] = False
                    self.controllers[i]._commanded = None  # re-establish on resume
                    self._notify(i)
            self._stop_timer_if_idle()
        elif entitled and not was:
            # Granted: resume any device the user still wants enabled.
            for i in list(self._want_enabled):
                if self._want_enabled[i] and not self._enabled[i]:
                    await self.enable(i)

    # ------------------------------------------------------------------ per-device lifecycle
    async def enable(self, index: int) -> bool:
        if index not in self.controllers:
            return False
        self._want_enabled[index] = True
        if self._enabled[index]:
            return True
        if not self._entitled:
            _LOGGER.warning(
                "Load control for %s requested but account isn't entitled — refusing",
                self.controllers[index].name,
            )
            return False
        self._enabled[index] = True
        _LOGGER.warning("Load control ENABLED for %s", self.controllers[index].name)
        self._ensure_timer()
        self._notify(index)
        now = dt_util.now()
        await self._tick_device(index, now)
        if index in self._modulating:
            # _tick_device only records the plan decision for a modulating device; without
            # this the setpoint would sit untouched for up to MODULATION_INTERVAL_SECONDS
            # after the user flipped the master switch on, which reads as "nothing happened".
            await self._fast_tick_device(index, now)
        return True

    async def disable(self, index: int) -> None:
        if index not in self.controllers:
            return
        self._want_enabled[index] = False
        if self._enabled[index]:
            _LOGGER.warning(
                "Load control DISABLED for %s — leaving load as-is",
                self.controllers[index].name,
            )
        self._enabled[index] = False
        self.controllers[index]._commanded = None  # re-establish cleanly if re-enabled
        self._notify(index)
        self._stop_timer_if_idle()

    def is_enabled(self, index: int) -> bool:
        return bool(self._enabled.get(index, False))

    # ------------------------------------------------------------------ manual override
    def get_override(self, index: int):
        """'on' / 'off' while a manual override is active for the device, else None."""
        c = self.controllers.get(index)
        if c is None or c.override is None:
            return None
        return "on" if c.override else "off"

    async def set_override(self, index: int, mode, *, actuate: bool = True) -> bool:
        """Set (mode='on'/'off') or clear (mode=None) a device's manual override.

        Deliberately NOT gated on the device's enable switch or on entitlement: a
        forced on/off is a direct user command — morally identical to the user toggling
        the appliance's own switch entity, which they can always do — and it must work
        precisely when GridLens control is active so the controller stops fighting it
        (plan re-assert would otherwise flip the switch right back). Clearing returns
        the device to whatever the enable switch + plan dictate; if it's enabled, state
        re-establishes on an immediate tick rather than waiting up to 5 minutes.
        """
        c = self.controllers.get(index)
        if c is None:
            return False
        want = None if mode is None else (mode == "on")
        await c.set_override(want, dt_util.now(), actuate=actuate)
        self._notify(index)
        if want is None and actuate and self._enabled.get(index, False):
            now = dt_util.now()
            await self._tick_device(index, now)
            if index in self._modulating:
                # Returning to Auto must re-establish the setpoint now, not at the next fast
                # tick — the device is currently sitting at whatever the override commanded.
                await self._fast_tick_device(index, now)
        return True

    # ------------------------------------------------------------------ timer
    def _ensure_timer(self) -> None:
        if self._cancel_timer is None:
            minutes = list(range(0, 60, self.interval_minutes))
            self._cancel_timer = async_track_time_change(
                self.hass, self._tick, minute=minutes, second=0
            )
            _LOGGER.info("LoadControlManager timer started (interval=%dmin)", self.interval_minutes)
        self._ensure_fast_timer()

    def _ensure_fast_timer(self) -> None:
        """Start the modulation loop, but only once an enabled device actually needs it."""
        if self._fast_cancel is not None:
            return
        if not any(self._enabled.get(i, False) for i in self._modulating):
            return
        # Deferred for the same reason as the ModulatingLoadController import above: only a
        # modulating install ever reaches this line, and the offline harnesses stub
        # homeassistant.helpers.event with just the symbols the code they exercise uses.
        from homeassistant.helpers.event import async_track_time_interval

        self._fast_cancel = async_track_time_interval(
            self.hass, self._fast_tick, timedelta(seconds=MODULATION_INTERVAL_SECONDS)
        )
        _LOGGER.info(
            "LoadControlManager modulation loop started (every %ds)", MODULATION_INTERVAL_SECONDS
        )

    def _stop_timer_if_idle(self) -> None:
        if self._fast_cancel is not None and not any(
            self._enabled.get(i, False) for i in self._modulating
        ):
            self._fast_cancel()
            self._fast_cancel = None
            _LOGGER.info("LoadControlManager modulation loop stopped (no modulating devices active)")
        if self._cancel_timer is not None and not any(self._enabled.values()):
            self._cancel_timer()
            self._cancel_timer = None
            _LOGGER.info("LoadControlManager timer stopped (no devices active)")

    async def _on_hass_stop(self, _event) -> None:
        # Deadman = leave loads as-is. Stop the timer, but never force a switch off.
        if any(self._enabled.values()):
            _LOGGER.warning("HA stopping with load control active — leaving loads as-is (no forced off)")
        self.shutdown()

    def shutdown(self) -> None:
        """Stop ticking (config-entry unload). Deadman = leave loads as-is: never forces a
        switch off, and never winds a modulating load's setpoint back to 0 — an EV mid-charge
        is exactly the case where cutting delivery on a restart is worst. Both loops just
        stop; the hardware keeps whatever it was last given."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._fast_cancel is not None:
            self._fast_cancel()
            self._fast_cancel = None

    # ------------------------------------------------------------------ tick
    async def _tick(self, now: Optional[datetime] = None) -> None:
        now = now or dt_util.now()
        if not any(self._enabled.values()):
            return
        if self._plan is None:
            return  # nothing to act on yet — leave loads as-is
        if self._plan_is_stale(now):
            if not self._degraded:
                _LOGGER.warning("Load control: plan stale — leaving loads as-is until a fresh plan")
                self._degraded = True
            return
        for i in list(self._enabled):
            if self._enabled[i]:
                await self._tick_device(i, now)

    async def _tick_device(self, index: int, now: datetime) -> None:
        if self._plan is None or self._plan_is_stale(now):
            return  # leave as-is
        controller = self.controllers[index]
        current = self._current_interval(now)
        planned_w = self._device_power_now(index, current)
        schedule_allows = None
        if controller.greedy_respects_schedule:
            schedule_allows = await self._schedule_allows_now(index, now)
        spill_kwh, spill_hours = (None, 0.0)
        battery_headroom_w: Optional[float] = None
        battery_headroom_kwh: Optional[float] = None
        ac_output_headroom_w: Optional[float] = None
        if controller.greedy and controller.greedy_forecast_surplus:
            spill_kwh, spill_hours = self._forecast_surplus_budget(index, now)
            battery_headroom_w = self._battery_headroom_w()
            battery_headroom_kwh = self._battery_headroom_kwh()
            ac_output_headroom_w = self._ac_output_headroom_w()
        if controller.greedy and not self._grid_power_sensor and not self._warned_no_grid_power:
            # Greedy's export-surplus condition is the one that catches a house spilling
            # kilowatts at a $0 export price, and it is silently unavailable without this
            # sensor — which is optional, easy to leave blank, and NOT auto-discoverable
            # (the Energy dashboard stores energy statistics, never a live power entity).
            # warning, not debug: a debug line here would be invisible on a default install,
            # which is exactly the install this happens on.
            self._warned_no_grid_power = True
            _LOGGER.warning(
                "Greedy Consumption is enabled for %s but no Grid Power sensor is "
                "configured — the export-surplus condition can never fire, so the load "
                "will stay off through free export. Set the (optional) Grid Power sensor "
                "in Grid Lens > Reconfigure > Energy sensors to a live signed grid power "
                "entity (positive = importing, negative = exporting).",
                controller.name,
            )
        try:
            await controller.apply(
                planned_w, now,
                import_rate=current.import_rate if current else None,
                export_rate=current.export_rate if current else None,
                grid_power_w=self._read_grid_power_w(),
                schedule_allows=schedule_allows,
                forecast_spill_kwh=spill_kwh,
                forecast_hours=spill_hours,
                battery_headroom_w=battery_headroom_w,
                battery_headroom_kwh=battery_headroom_kwh,
                ac_output_headroom_w=ac_output_headroom_w,
                min_export_price=self._min_export_price(),
                soc_cutoff=self._soc_cutoff_active(index),
            )
        except Exception as err:  # noqa: BLE001 — a bad device tick must not kill the timer
            _LOGGER.error("Load control tick failed for %s: %s", self.controllers[index].name, err)
        # Push the fresh controller state (note, greedy_reason, greedy_blocked, the
        # forecast figures) to the control switch entity so the Load Control card is a
        # live view of what greedy decided this tick — not just whatever it read at
        # startup or the last time the user touched an override. HA suppresses the
        # state_changed event when nothing actually changed, so calling this every tick
        # is cheap.
        self._notify(index)

    def _current_interval(self, now: datetime) -> Optional[DispatchInterval]:
        current: Optional[DispatchInterval] = None
        for iv in self._plan or []:
            if iv.start <= now:
                current = iv
            else:
                break
        return current

    def _device_power_now(self, index: int, current: Optional[DispatchInterval]) -> float:
        """The planned power (W) for device ``index`` in interval ``current`` (or 0.0 if none)."""
        if current is None:
            return 0.0
        dw = current.deferrable_w
        return float(dw[index]) if index < len(dw) else 0.0

    def _min_export_price(self) -> float:
        """The user's Minimum Export Price ($/kWh) — the live dashboard number entity
        (number.py's GridLensMinExportPriceNumber), not a config-flow snapshot, so a
        change takes effect on the next tick with no reload. Stored as c/kWh; converted
        to $/kWh here to match DispatchInterval's rate units. 0.0 (the default) means the
        floor is disabled and Greedy Consumption's export-surplus bar stays at "≤ $0".

        Local import mirrors plan_calculator._get_min_export_price — same helper, same
        reason (the entity is authoritative once registered; the numeric fallback only
        covers the window before it is)."""
        from ..runtime_settings import get_live_number
        cents = get_live_number(
            self.hass, self.entry.entry_id, "min_export_price",
            self.entry.data.get(CONF_MIN_EXPORT_PRICE, 0.0),
        )
        return cents / 100.0

    # ------------------------------------------------------------------ modulation loop
    async def _fast_tick(self, now: Optional[datetime] = None) -> None:
        """30-second setpoint loop for modulating devices (see the module docstring).

        Same deadman as ``_tick``: no plan, or a stale one, means leave every load exactly
        where it is. Note this loop deliberately does NOT re-evaluate the greedy conditions
        or the plan — ``_tick_device`` owns those, on the 5-minute clock, so that a modulating
        device's observability (``greedy_reason``, the forecast figures) is produced by the
        same code and at the same cadence as an on/off device's."""
        now = now or dt_util.now()
        if self._plan is None or self._plan_is_stale(now):
            return
        for i in sorted(self._modulating):
            if self._enabled.get(i, False):
                await self._fast_tick_device(i, now)

    async def _fast_tick_device(self, index: int, now: datetime) -> None:
        controller = self.controllers.get(index)
        if controller is None or controller.override is not None:
            return  # a human has taken this device; set_override already actuated it
        if self._plan is None or self._plan_is_stale(now):
            # The deadman again, repeated here rather than only in _fast_tick: enable() and
            # a cleared override both call this directly for an immediate response, and
            # without this guard they would compute a target from a missing plan — which is
            # 0 W, i.e. actively stopping a charge the user may have started by hand. "Leave
            # as-is" has to mean writing nothing, not writing zero.
            return
        try:
            target_w, source = await self._modulation_target_w(index, now)
            await controller.modulate(target_w, now, source=source)
        except Exception as err:  # noqa: BLE001 — one bad device must not kill the timer
            _LOGGER.error("Modulation tick failed for %s: %s", controller.name, err)
        else:
            self._check_stuck_import(index, controller, now)
        self._notify(index)  # keep the card's live amps/kW and modulation_source current

    def _check_stuck_import(
        self, index: int, controller: "ModulatingLoadController", now: datetime
    ) -> None:
        """Warn once when this device's commanded setpoint sits unchanged for
        ``_STUCK_IMPORT_MIN_MINUTES`` while the household keeps importing more than
        ``_STUCK_IMPORT_THRESHOLD_W`` from the grid.

        This is the generic shape of the 2026-09-13 ac_output_cap bug
        (GRIDLENS_CHECKLIST.md): a control path that can hold an existing overshoot but
        never correct it produces exactly this pattern — a live import that persists tick
        after tick with zero further setpoint writes. Deliberately bug-agnostic: it reads
        only the setpoint and the grid reading, not any clamp's internal reasoning, so it
        also catches whatever the *next* stuck-clamp bug turns out to be, not just this
        one. A real, working correction (the setpoint actually moving) resets the streak
        every time, so an actively-converging control loop never trips this even while it
        temporarily imports.

        No grid power sensor configured, or momentarily unreadable, is silently skipped
        (not warned about) — that gap is already covered by ``_warned_no_grid_power`` and
        piling a second warning on top of it here would just be noise.
        """
        grid_w = self._read_grid_power_w()
        commanded_w = controller.status().get("commanded_w") or 0.0
        st = self._import_stuck.setdefault(
            index, {"since": None, "setpoint_w": None, "warned": False}
        )
        importing = grid_w is not None and grid_w > _STUCK_IMPORT_THRESHOLD_W
        setpoint_moved = (
            st["setpoint_w"] is None or abs(commanded_w - st["setpoint_w"]) > 1e-6
        )
        if not importing or commanded_w <= 0.0 or setpoint_moved:
            # Import cleared, device is off, or the setpoint just genuinely changed
            # (including the very first observation) — (re)start the streak clean rather
            # than warn on a control loop that IS moving.
            st["since"] = now if importing and commanded_w > 0.0 else None
            st["setpoint_w"] = commanded_w if importing and commanded_w > 0.0 else None
            st["warned"] = False
            return
        elapsed_min = (now - st["since"]).total_seconds() / 60.0
        if elapsed_min >= _STUCK_IMPORT_MIN_MINUTES and not st["warned"]:
            status = controller.status()
            _LOGGER.warning(
                "Load control: %s has held %.0fW for %.1f min while the household "
                "imports %.0fW from the grid — setpoint isn't correcting "
                "(modulation_source=%s, note=%s, greedy_blocked=%s). Investigate the "
                "active clamp; see GRIDLENS_CHECKLIST.md 2026-09-13 for the first known "
                "cause of this pattern.",
                controller.name, commanded_w, elapsed_min, grid_w,
                status.get("modulation_source"), status.get("note"),
                status.get("greedy_blocked"),
            )
            st["warned"] = True

    async def _modulation_target_w(self, index: int, now: datetime) -> tuple[float, str]:
        """Power (W) device ``index`` should be given right now, and which term set it.

        ``max(plan_w, surplus_w)``. The plan term is the LP's own allocation for the current
        interval and always applies. The surplus term is Greedy Consumption expressed
        continuously — where an on/off load can only ask "is the whole draw already covered",
        a modulating one can ask "how much is spare" and take exactly that:

        * **Live export surplus** (export price ≤ 0) — what the house is spilling right now
          PLUS what this device is already drawing, because the device's own draw is already
          netted off inside the grid reading. Without that second term the loop would
          converge to a fixed point at whatever it happened to start at: raise the setpoint,
          export falls by the same amount, and the surplus figure says there is no more room.
          The grid reading is corrected for live battery *discharge* first (fixed
          2026-09-11, see ``_read_battery_net_power_w``): a battery discharging to hold grid
          flow near zero — its own self-consumption loop, or GridLens's own SELF_USE battery
          action — makes "no import" look like free solar when it's actually funded by the
          battery. ``surplus_w = device_w - (grid_w + max(0, discharge_w))``: adding back
          what the battery is propping up recovers the grid flow *solar and consumption
          alone* would produce, which is what "surplus" is actually meant to measure.
          **Deliberately asymmetric** (household instruction, 2026-09-11): the battery
          *charging* is never netted the same way — a positive ``battery_w`` (absorbing
          spare solar) is dropped entirely, not added back as extra headroom for this
          device. The battery gets first claim on genuine surplus; this device only sees
          what's left after it, on top of a small deliberate ``_EXPORT_BIAS_W`` undershoot
          so ordinary noise lands on the (cheap) export side rather than the (expensive)
          import side. No battery configured (or unreadable) skips only the discharge
          correction (``device_w - grid_w - _EXPORT_BIAS_W``) — the export-bias margin
          applies to every install, battery or not.
        * **Free import window** (import price ≤ 0) — energy costs nothing, so take the
          device's whole envelope rather than metering it against export.

        Every input fails closed: an unknown rate, an unconfigured or unavailable grid sensor,
        or a device with no discoverable power sensor simply removes the surplus term, and the
        plan still drives. With no ``grid_power_sensor`` configured at all, surplus tracking
        never engages and this degrades to pure plan-following.

        **Inverter AC output ceiling, applied last, regardless of source** (found
        2026-09-12 — see ``_ac_output_headroom_w``). ``plan_w`` and ``fc_target_w`` both
        reason about PV/battery *capability*; neither knows the plant's own AC-side output
        can be capped well below that. Only relevant when ``CONF_MAX_AC_OUTPUT_KW`` is
        configured — unset (the default) leaves this a no-op.

        **Battery priority can pull the target below plan_w — the one exception to "plan is
        a floor"** (household instruction, 2026-09-11). Every term above only ever *adds* to
        plan_w; none of them can express "the plan turned out too optimistic, back off."
        Found live: plan_w assumed enough solar for both this device and the battery, real
        solar fell short, and with nothing able to reduce below plan_w the device kept its
        full planned draw regardless while the battery alone absorbed the entire shortfall.
        A live battery discharge (``discharge_w``, from ``_read_battery_net_power_w()``) is
        ground truth that *something* isn't matching the plan's assumptions right now — a
        too-optimistic forecast, self-use, or even a deliberate plan-driven evening
        discharge — and in every one of those cases the battery keeps first claim: this
        device is pulled back by the discharge amount plus a fixed ``_BATTERY_PRIORITY_BIAS_W``
        margin (found 2026-09-12: pulling back by *exactly* the live discharge reading only
        cancels it in the limit, since every real tick lags what it's correcting against —
        see that constant's comment), even below plan_w. Applied after
        ``max(plan_w, surplus_w)`` regardless of the greedy toggle (a priority correction,
        not an opportunistic add-on) and independent of ``grid_power_sensor`` (only needs
        the battery sensors, which already fail closed to 0 on their own).
        """
        controller = self.controllers[index]
        current = self._current_interval(now)
        plan_w = max(0.0, self._device_power_now(index, current))
        import_rate = current.import_rate if current else None
        export_rate = current.export_rate if current else None

        # Live battery discharge (W; 0 while charging/idle/unconfigured/unreadable) — read
        # once, shared by the live-surplus term below and the battery-priority correction
        # after the max() so both act on the same live reading within this tick.
        discharge_w = max(0.0, -(self._read_battery_net_power_w() or 0.0))

        surplus_w: Optional[float] = None
        if controller.greedy and not (
            controller.greedy_respects_schedule
            and not await self._schedule_allows_now(index, now)
        ):
            # "Export is being wasted" — mirrors DeferrableLoadController._greedy_wants_on:
            # $0 export historically, now also anything at or below the user's Minimum
            # Export Price. max(0.0, ...) keeps a 0 preference reproducing the old bar.
            export_waste_ceiling = max(0.0, self._min_export_price())
            if (export_rate is not None and export_rate <= export_waste_ceiling
                    and self._grid_power_sensor):
                grid_w = self._read_grid_power_w()
                if grid_w is not None:
                    device_w = self._read_device_power_w(index) or 0.0
                    # Net out battery *discharge* before treating "grid near zero" as real
                    # solar surplus (fixed 2026-09-11). A battery running its own — or
                    # GridLens's own SELF_USE — self-consumption loop holds grid flow near
                    # zero using stored charge, not spare solar; read on its own, that looks
                    # identical to genuine surplus.
                    #
                    # Asymmetric on purpose (household instruction, 2026-09-11): only
                    # discharge is added back — battery *charging* is dropped, not credited
                    # to this device, so the battery keeps first claim on genuine surplus
                    # rather than competing with this device for it. `_EXPORT_BIAS_W` then
                    # shaves a small, deliberate margin off the result so the loop settles a
                    # little short of true breakeven — cheap insurance against import on the
                    # noise, given import runs well above the export rate this household is
                    # diverting in the first place.
                    grid_w_ex_battery = grid_w + discharge_w
                    surplus_w = max(0.0, device_w - grid_w_ex_battery - _EXPORT_BIAS_W)
            if import_rate is not None and import_rate <= 0.0:
                cap_w = getattr(controller, "cap_w", 0.0)
                if cap_w > 0.0:
                    surplus_w = cap_w
            # Greedy condition #3 (see DeferrableLoadController's module docstring). It is
            # forward-looking — evaluated on the 5-minute tick by apply(), which stashes
            # the proportional power it wants in `_greedy_forecast_target_w` (already
            # clamped into this device's [min_w, cap_w] envelope by
            # ModulatingLoadController._forecast_surplus_snap_w). Without pulling it in
            # here the condition would be silently inert for every modulating device — the
            # card's badge would light up while the setpoint stayed at plan_w.
            fc_target_w = getattr(controller, "_greedy_forecast_target_w", 0.0) or 0.0
            if fc_target_w > 0.0:
                surplus_w = max(surplus_w or 0.0, fc_target_w)
        target_w = max(plan_w, surplus_w or 0.0)
        source = "surplus" if (surplus_w or 0.0) > plan_w else "plan"

        # Battery-priority correction — see the docstring above. The only place in this
        # function the target is allowed to drop below plan_w.
        if discharge_w > 0.0 and target_w > 0.0:
            relieved_w = max(0.0, target_w - discharge_w - _BATTERY_PRIORITY_BIAS_W)
            if relieved_w < target_w:
                target_w = relieved_w
                source = "battery_priority"

        # Inverter/plant AC output ceiling — a live, continuously-reevaluated hard cap,
        # applied regardless of which term above produced target_w. Every term so far
        # (plan_w, the live export-surplus term, and fc_target_w) reasons about PV and
        # battery *capability*, never about whether the plant can actually deliver that
        # much AC power at once; fc_target_w in particular is only refreshed on the
        # 5-minute apply() tick, so a stale forecast figure can keep winning this loop's
        # max() for minutes after live conditions no longer support it. Found 2026-09-12:
        # PV alone was already at the plant's ~10kW ceiling, so the battery's real ~20kW
        # of discharge headroom was moot, and the forecast-surplus condition — unaware of
        # any of this — sized the Wattpilot's target off PV+battery capability that could
        # never reach the car. See LoadControlManager._ac_output_headroom_w.
        ac_headroom_w = self._ac_output_headroom_w()
        if ac_headroom_w is not None:
            device_w = self._read_device_power_w(index) or 0.0
            allowed_w = max(0.0, device_w + ac_headroom_w)
            if target_w > allowed_w:
                target_w = allowed_w
                source = "ac_output_cap"

        if target_w <= 0.0:
            return 0.0, "off"
        return target_w, source

    def _forecast_surplus_budget(
        self, index: int, now: datetime
    ) -> tuple[Optional[float], float]:
        """``(spill_kwh, covered_h)`` — how much energy the plan expects to WASTE over the
        safe look-ahead window, and the span (hours) of that window.

        This is the numerator for Greedy Consumption's *proportional* forecast-surplus
        draw (``DeferrableLoadController._forecast_surplus_target_w``): the device is run
        at ``spill_kwh / covered_h`` (average rate the plan wastes), not all-or-nothing
        against a flat-out bar.

        Two waste sources, per the user-facing definition of "energy not worth selling":

        * **Spilled export** — a slot whose export rate is at or below the user's Minimum
          Export Price (``$0`` when that setting is disabled) and that the plan still
          exports into. Uses ``total_export_w`` (whole-house export, PV spill included),
          NOT ``export_w`` (battery share only), and it's already net of every load the
          plan schedules in that slot. Matches condition #2's instantaneous bar
          (``_greedy_wants_on``), so the forward-looking and live triggers agree on what
          counts as wasted.
        * **Unused free-import window** — a slot whose import rate is $0: this device
          could run flat out for free then. Only the part the plan does NOT already have
          it running counts (``max_w - planned``).

        **Reservation clip.** The window ends early at the first slot where the plan
        itself starts materially discharging the battery (``BatteryAction.DISCHARGE`` at
        >= ``_RESERVED_DISCHARGE_MIN_W``): past there the plan is spending the battery on
        something it values, and Greedy must not borrow charge across it. Since a
        ``DispatchInterval`` carries no per-slot SOC, this "stop at the first planned
        drawdown" rule is the available proxy for "don't discharge below the plan's own
        SOC trajectory".

        Returns ``(None, 0.0)`` when the safe window is shorter than
        ``_MIN_BUDGET_WINDOW_H`` (including: no plan, or the current slot is already a
        planned discharge) — fail closed rather than average a sliver into a rate.
        """
        plan = self._plan
        controller = self.controllers.get(index)
        if not plan or controller is None:
            return None, 0.0
        nominal_end = now + timedelta(hours=GREEDY_SURPLUS_LOOKAHEAD_HOURS)
        export_waste_ceiling = max(0.0, self._min_export_price())

        # First planned material battery drawdown inside the look-ahead -> the window ends
        # there (or at the nominal end, whichever is sooner).
        window_end = nominal_end
        for pos, iv in enumerate(plan):
            if iv.start >= nominal_end:
                break
            if (iv.action == BatteryAction.DISCHARGE
                    and iv.power_w >= _RESERVED_DISCHARGE_MIN_W):
                window_end = min(window_end, max(iv.start, now))
                break

        spill_kwh = 0.0
        covered_h = 0.0
        for pos, iv in enumerate(plan):
            slot_end = self._slot_end(plan, pos)
            o_start = max(iv.start, now)
            o_end = min(slot_end, window_end)
            if o_end <= o_start:
                continue
            hours = (o_end - o_start).total_seconds() / 3600.0
            free_w = 0.0
            if iv.export_rate is not None and iv.export_rate <= export_waste_ceiling:
                free_w += max(0.0, iv.total_export_w)
            if iv.import_rate is not None and iv.import_rate <= 0.0:
                planned_w = self._device_power_now(index, iv)
                free_w += max(0.0, controller.max_w - max(0.0, planned_w))
            spill_kwh += free_w * hours / 1000.0
            covered_h += hours
        if covered_h < _MIN_BUDGET_WINDOW_H:
            return None, 0.0
        return spill_kwh, covered_h

    @staticmethod
    def _slot_end(plan: list[DispatchInterval], pos: int) -> datetime:
        """End of plan slot ``pos``. ``DispatchInterval`` carries only a start, so a
        slot runs until the next one starts; the final slot reuses the previous gap (and
        falls back to 30 min for a degenerate single-slot plan)."""
        if pos + 1 < len(plan):
            return plan[pos + 1].start
        if pos > 0:
            return plan[pos].start + (plan[pos].start - plan[pos - 1].start)
        return plan[pos].start + timedelta(minutes=30)

    def _read_power_w(self, entity_id: str) -> Optional[float]:
        """A power entity's value in WATTS, or None if it can't be read.

        None always means "unknown" — never 0. Every caller here feeds a decision about how
        much power is spare, and a silent 0 would read as "no surplus" (or "this device draws
        nothing"), which is a guess dressed up as a measurement.

        kW is normalised to W because the sensors this reaches are user-picked or
        auto-discovered: HA's power device_class permits either unit and integrations
        genuinely differ, so a device reporting 3.2 kW must not be read as 3.2 W."""
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", None):
            return None
        try:
            value = float(st.state)
        except (TypeError, ValueError):
            return None
        unit = str((st.attributes or {}).get("unit_of_measurement") or "").strip().lower()
        return value * 1000.0 if unit == "kw" else value

    def _read_grid_power_w(self) -> Optional[float]:
        """Live signed grid power (W, +import/-export) for Greedy Consumption's
        export-surplus condition. None if unconfigured/unavailable/unparseable — the
        controller treats that as "unknown", never guessing a value."""
        return self._read_power_w(self._grid_power_sensor)

    def _read_battery_net_power_w(self) -> Optional[float]:
        """Live net battery power (W, +charging/-discharging), or None if unconfigured
        or unreadable.

        Two sensor shapes, matching plan_calculator.py's historical battery-behaviour
        backtest (same config keys, same convention — this is that logic finally
        reaching the live control path, see CONF_BATTERY_DISCHARGE_POWER_SENSOR's
        comment):

        * No discharge sensor configured — ``_battery_charge_power_sensor`` is assumed
          already signed (the original, still-common shape: Tesla Powerwall and most
          single-sensor integrations).
        * A discharge sensor IS configured — both sensors are unipolar (0 while the
          battery is doing the other thing), so net power is charge minus discharge.
          Sigenergy is the concrete case: "Battery Charging Power" reads 0 during a real
          discharge, so read alone it looks exactly like "not touching the battery at
          all" rather than "actively discharging".
        """
        if not self._battery_charge_power_sensor:
            return None
        charge_w = self._read_power_w(self._battery_charge_power_sensor)
        if charge_w is None:
            return None
        if not self._battery_discharge_power_sensor:
            return charge_w
        discharge_w = self._read_power_w(self._battery_discharge_power_sensor)
        if discharge_w is None:
            return None
        return charge_w - discharge_w

    def _read_percent(self, entity_id: str) -> Optional[float]:
        """A plain 0-100 sensor reading (SOC), or None if unconfigured/unavailable."""
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", None):
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    def _soc_cutoff_active(self, index: int) -> bool:
        """True when device ``index`` has a configured SOC sensor + ceiling
        (CONF_DEFERRABLE_LOAD_SOC_SENSORS / CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT) and the
        live reading is at/above that ceiling right now.

        This is the live-actuation half of that config pair — see const.py's comment on
        those two constants for the full incident this closes (2026-09-13): the LP's own
        planning use of the same fields (advisory/coordinator.py._deferrable_for_horizon)
        only ever shaped the *plan's* daily_kwh allocation, so Greedy Consumption's live
        export-surplus/forecast-surplus terms — which know nothing about a device's
        remaining SOC headroom — could and did keep commanding real current past the
        configured ceiling.

        Unlike that planning use, this does NOT need ``soc_capacity_kwh`` — a plain
        percent compare is all a live stop/no-stop decision needs. No sensor configured,
        no (or a 100%) ceiling configured, or an unreadable sensor all return False — same
        fail-open discipline as every other optional input in this manager: a missing or
        broken SOC sensor must never itself force a device off; it just means this
        particular safety net is unavailable, and plan/greedy continue to decide normally.
        """
        if index >= len(self._soc_sensors):
            return False
        sensor_id = self._soc_sensors[index]
        if not sensor_id:
            return False
        max_pct = self._soc_max_percent[index] if index < len(self._soc_max_percent) else 100.0
        if not max_pct or max_pct >= 100.0:
            return False
        soc = self._read_percent(sensor_id)
        if soc is None:
            return False
        return soc >= max_pct

    def _battery_headroom_w(self) -> Optional[float]:
        """Battery discharge headroom (W) available right now without dropping SOC below
        its configured minimum, or None if a battery isn't configured/readable.

        Backs Greedy Consumption's forecast-surplus condition (see
        ``DeferrableLoadController``'s module docstring): that condition pays for itself by
        drawing the battery down now and letting the forecast spill refill it later, so it
        must never be allowed to fire unless the battery can actually absorb the device's
        full draw right now — otherwise firing is just real, unbuffered grid import wearing
        a forecast's clothing.

        None (never 0) whenever the SOC sensor or the net battery-power reading is missing
        or unreadable — same fail-closed discipline as every other Greedy Consumption input;
        a household with no battery configured must never have this silently read as
        "unlimited headroom". 0.0 (a real, measured answer) once SOC is at or below the
        configured minimum: there is a battery, it just has nothing spare to give.

        Net battery power (``_read_battery_net_power_w()``, +charging/-discharging) —
        the discharging magnitude is netted off the rated max discharge rate to get what's
        actually still free, not just what the battery is rated for. Fixed 2026-09-11: this
        used to read ``_battery_charge_power_sensor`` directly, assuming it was always
        signed — wrong for a battery whose charge sensor reads 0 during a real discharge
        (Sigenergy: the discharge magnitude lives on a separate sensor). That made this
        method blind to an actual discharge, reporting the full rated discharge rate as
        "headroom" while the battery was being drawn down for real.
        """
        if not self._battery_soc_sensor or not self._battery_charge_power_sensor:
            return None
        soc = self._read_percent(self._battery_soc_sensor)
        if soc is None:
            return None
        if soc <= self._battery_min_soc:
            return 0.0
        charge_w = self._read_battery_net_power_w()
        if charge_w is None:
            return None
        discharging_w = max(0.0, -charge_w)
        return max(0.0, self._battery_max_discharge_rate_kw * 1000.0 - discharging_w)

    def _battery_headroom_kwh(self) -> Optional[float]:
        """Energy (kWh) the battery can lend before it hits its configured minimum SOC.

        This is the ceiling on the worst-case *transient* dip Greedy's proportional
        forecast-surplus draw is allowed to open up: it runs the device steadily at the
        rate the plan wastes, but the spill it's borrowing against may all land at the far
        end of the window — so mid-window the battery can be down by as much as the whole
        budget before the refill arrives. ``_forecast_surplus_target_w`` refuses to run if
        this is smaller than the budget.

        None (never 0, except a real measured floor) whenever pack capacity or the SOC
        reading is missing — same fail-closed discipline as ``_battery_headroom_w``. An
        install with ``has_battery`` but no capacity configured therefore can't use the
        proportional forecast-surplus draw at all, which is the safe default.
        """
        if not self._battery_capacity_kwh or not self._battery_soc_sensor:
            return None
        soc = self._read_percent(self._battery_soc_sensor)
        if soc is None:
            return None
        return max(0.0, (soc - self._battery_min_soc) / 100.0 * self._battery_capacity_kwh)

    def _ac_output_headroom_w(self) -> Optional[float]:
        """Headroom (W) below the configured inverter/plant AC output ceiling, or None if
        no ceiling is configured.

        Many all-in-one battery/PV inverters cap total AC output well below what PV and
        battery could otherwise deliver together — confirmed on this household's own
        Sigenergy plant, where 7 days of ``sensor.sigen_0_plant_active_power`` never
        exceeded ~10kW regardless of available PV or battery SOC (GRIDLENS_CHECKLIST.md,
        2026-09-12). Neither ``_battery_headroom_w`` above nor the LP's own forecast knows
        about this: both reason about PV/battery *capability*, not the inverter's AC-side
        rating, so on a day PV alone is already near the cap, "the battery has 20kWh free"
        is true and irrelevant — none of it can physically reach the loads. This is what
        sized the Wattpilot's charging current off headroom that didn't really exist
        (GRIDLENS_CHECKLIST.md, 2026-09-12).

        ``plant_output_w = load_w - grid_w`` — whole-house consumption minus whatever the
        grid is currently contributing (or absorbing, if negative) — is the live combined
        AC power the plant is delivering right now. Deliberately built from the two
        general-purpose sensors every install already has a config slot for
        (``CONF_LOAD_POWER_SENSOR``, ``CONF_GRID_POWER_SENSOR``) rather than a
        vendor-specific "total AC output" sensor, so this works on any inverter brand.

        **Currently-exported power is added back in, not treated as already spoken for**
        (fixed 2026-09-12, hours after the fix above shipped — found live: the household
        was exporting 3kW and this clamp throttled the Wattpilot DOWN anyway). Hitting the
        ceiling is not itself a problem — the plant is allowed to produce flat-out at
        ``max_ac_output_kw`` all day. What matters is whether MORE production would be
        needed, and redirecting power that's already being produced and already flowing
        out as export costs nothing: it doesn't add one extra watt to what the plant has to
        generate, it just changes where the existing output goes. Only genuinely NEW
        demand — beyond both the plant's spare production capacity and whatever's already
        being exported for free — can actually push total output past the ceiling. So
        headroom = ``(cap - plant_output)`` (spare production capacity, the original term)
        ``+ export_w`` (current export magnitude, 0 while importing) — the plain
        ``cap - plant_output`` alone conflates "the plant happens to be producing a lot
        right now" with "there's no room for more load", which is exactly backwards when
        most of that production is being wasted as export in the first place.

        None whenever no ceiling is configured (the overwhelmingly common case — most
        installs' PV + battery can't reach the inverter's rating anyway, so this feature
        is opt-in). Once a ceiling IS configured, 0.0 (not None) whenever the sensors it
        needs aren't configured or aren't currently readable — fails closed the same way
        ``_battery_headroom_w`` does: a household that has told GridLens about a real
        hardware limit gets that limit enforced, not silently ignored the moment a live
        reading blips.

        **Importing must be able to show up as *negative* headroom, not floor at zero**
        (found 2026-09-13 — GRIDLENS_CHECKLIST.md). The version of this method above
        computed ``plant_output_w = load_w - grid_w`` unconditionally, then floored
        ``cap - plant_output_w`` at 0.0 before adding ``export_w`` back. That floor is
        correct on the export side (see above) but wrong on the import side: subtracting
        a live import out of ``load_w`` credits the plant with output it isn't actually
        producing, so an import that exists *because* load already exceeds the cap gets
        netted straight back out and reported as ~0 headroom instead of the negative
        figure that would tell the caller to shed load. Downstream, ``allowed_w =
        device_w + ac_headroom_w`` can then never fall below the device's own current
        draw — the clamp becomes a one-way "don't increase further" ceiling that can hold
        an existing overshoot but never correct one, because the device's own (already
        excessive) draw is baked into ``load_w`` and handed straight back as the new
        floor. Live symptom: the Wattpilot's setpoint froze at 22 A with a steady
        ~350-450 W import for several minutes with zero further writes (``note`` stuck on
        ``hold_setpoint_deadband``) — not a slow control loop, a formula that structurally
        could not output a corrective figure while importing.

        Fix: while importing (``grid_w > 0``), compare the cap against ``load_w``
        directly — never net the live import back out of it first — so a load already
        over the cap shows up as negative headroom and pulls ``allowed_w`` back below the
        device's current draw. The exporting branch is untouched (it was already correct
        and stays covered by its own tests above).
        """
        if self._max_ac_output_w is None:
            return None
        if not self._load_power_sensor or not self._grid_power_sensor:
            return 0.0
        load_w = self._read_power_w(self._load_power_sensor)
        grid_w = self._read_grid_power_w()
        if load_w is None or grid_w is None:
            return 0.0
        if grid_w > 0.0:
            # Importing: crediting the plant with output it isn't producing (by netting
            # the import out of load_w first, as the export branch below does) is exactly
            # what hid the overshoot this method exists to correct. Compare the cap
            # against total demand instead, so exceeding it while backfilled by grid
            # import registers as negative headroom rather than a false-safe zero.
            return self._max_ac_output_w - load_w
        export_w = -grid_w
        plant_output_w = load_w + export_w
        return max(0.0, self._max_ac_output_w - plant_output_w) + export_w

    def _read_device_power_w(self, index: int) -> Optional[float]:
        """Live power (W) device ``index`` is drawing right now, for the surplus term's
        "add back what this device already takes" correction. None when no power sensor was
        discoverable for it, or it isn't readable — same fail-to-None discipline as the grid
        reading, and the caller treats it as a 0 contribution rather than aborting, since the
        surplus figure is still directionally right without it."""
        return self._read_power_w(self._device_power_sensors.get(index, ""))

    async def _schedule_allows_now(self, index: int, now: datetime) -> bool:
        """Does device ``index``'s stored weekly schedule (dashboard schedule editor)
        allow it to run right now? Reused verbatim from
        advisory/coordinator.py._deferrable_for_horizon's sourcing. No stored schedule
        yet = unrestricted (schedule_grid.slot_allowed's own contract fails OPEN on a
        malformed/missing grid) — a broken store must never silently pin a device off."""
        from ..schedule_grid import slot_allowed, week_from_hours

        store = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_deferrable_schedules")
        sensor_id = self._sensor_ids[index] if index < len(self._sensor_ids) else ""
        week = None
        if store is not None:
            try:
                week = await store.async_get(sensor_id)
            except Exception:  # noqa: BLE001 — a broken store must not block ticking
                week = None
        if week is None:
            week = week_from_hours(None)
        local = dt_util.as_local(now)
        return slot_allowed(week, local.weekday(), local.hour, local.minute)

    # ------------------------------------------------------------------ greedy consumption
    def is_greedy(self, index: int) -> bool:
        c = self.controllers.get(index)
        return bool(c.greedy) if c else False

    async def set_greedy(self, index: int, enabled: bool) -> bool:
        c = self.controllers.get(index)
        if c is None:
            return False
        c.set_greedy(enabled)
        return True

    def is_greedy_respects_schedule(self, index: int) -> bool:
        c = self.controllers.get(index)
        return bool(c.greedy_respects_schedule) if c else False

    async def set_greedy_respects_schedule(self, index: int, enabled: bool) -> bool:
        c = self.controllers.get(index)
        if c is None:
            return False
        c.set_greedy_respects_schedule(enabled)
        return True

    def is_greedy_forecast_surplus(self, index: int) -> bool:
        c = self.controllers.get(index)
        return bool(c.greedy_forecast_surplus) if c else False

    async def set_greedy_forecast_surplus(self, index: int, enabled: bool) -> bool:
        c = self.controllers.get(index)
        if c is None:
            return False
        c.set_greedy_forecast_surplus(enabled)
        return True

    # ------------------------------------------------------------------ max-current ceiling
    def get_current_cap_a(self, index: int) -> Optional[float]:
        """The user's max-current ceiling for a modulating device, or None if unset/N/A."""
        c = self.controllers.get(index)
        return getattr(c, "current_cap_a", None) if index in self._modulating else None

    async def set_current_cap_a(self, index: int, amps: Optional[float]) -> bool:
        """Set the user's max-current ceiling (None = unrestricted) for a modulating device.

        Deliberately does NOT force a write: the ceiling only bounds what the next fast tick
        may command, and that tick is at most MODULATION_INTERVAL_SECONDS away. Routing every
        setpoint write through the one loop keeps the write-economy rules unbypassable — a
        user dragging the slider would otherwise generate a write per pixel."""
        if index not in self._modulating:
            return False
        c = self.controllers.get(index)
        if c is None:
            return False
        c.set_current_cap_a(amps)
        self._notify(index)
        return True

    def current_limits_a(self, index: int) -> Optional[tuple[float, float]]:
        """``(min, max)`` amps for a modulating device's max-current entity, or None.

        The max is the *hardware* ceiling (the setpoint entity's own max, or the device's
        configured max_kw converted), never the user's current setting — that setting is what
        this bounds. Returns None when the device isn't modulating or the ceiling can't be
        established yet, so the entity can fall back rather than advertise a bogus range."""
        if index not in self._modulating:
            return None
        c = self.controllers.get(index)
        if c is None:
            return None
        top = float(getattr(c, "native_max_a", 0.0) or 0.0)
        if top <= 0:
            return None
        return float(getattr(c, "min_current_a", DEFAULT_MIN_CHARGE_CURRENT_A)), top

    def _plan_is_stale(self, now: datetime) -> bool:
        if self._plan_updated_at is None:
            return True
        return (now - self._plan_updated_at) > self.max_plan_age

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {
            "entitled": self._entitled,
            "degraded": self._degraded,
            "plan_updated_at": self._plan_updated_at.isoformat() if self._plan_updated_at else None,
            "devices": {
                i: {
                    "enabled": self._enabled.get(i, False),
                    "want_enabled": self._want_enabled.get(i, False),
                    **c.status(),
                }
                for i, c in self.controllers.items()
            },
        }
