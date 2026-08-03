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
    CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE,
    CONF_DEFERRABLE_LOAD_MAX_KW,
    CONF_DEFERRABLE_LOAD_MIN_CURRENT,
    CONF_DEFERRABLE_LOAD_PHASES,
    CONF_DEFERRABLE_LOAD_PLUG_SENSOR,
    CONF_DEFERRABLE_LOAD_SENSORS,
    CONF_DEFERRABLE_LOAD_SETPOINT,
    CONF_DEFERRABLE_LOAD_SETPOINT_UNIT,
    CONF_DEFERRABLE_LOAD_SWITCHES,
    CONF_DEFERRABLE_LOAD_VOLTAGE,
    CONF_GRID_POWER_SENSOR,
    DEFAULT_MIN_CHARGE_CURRENT_A,
    DOMAIN,
    MODULATION_INTERVAL_SECONDS,
)
from .executor import DispatchInterval
from .load_controller import DeferrableLoadController

_LOGGER = logging.getLogger(__name__)

# How far ahead Greedy Consumption's forecast-surplus condition looks for free energy the
# plan expects to waste (see DeferrableLoadController's module docstring). Long enough to
# see past the "battery is still soaking everything up" morning shoulder into the real
# afternoon spill, short enough that the forecast is still worth believing and that a
# device started now is plausibly still running when the surplus lands.
GREEDY_SURPLUS_LOOKAHEAD_HOURS = 4.0


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
        # Retained for Greedy Consumption's schedule lookup (_schedule_allows_now):
        # sensor_id is the schedule store's key for a device's stored weekly grid —
        # same source advisory/coordinator.py._deferrable_for_horizon already reads.
        self._sensor_ids = sensors
        # Live signed grid power sensor (W, +import/-export) for Greedy Consumption's
        # export-surplus condition. "" = not configured — that condition simply never
        # fires (the import-price-free condition still works without it).
        self._grid_power_sensor: str = d.get(CONF_GRID_POWER_SENSOR) or ""

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
        free_kwh, free_hours = (None, 0.0)
        if controller.greedy and controller.greedy_forecast_surplus:
            free_kwh, free_hours = self._forecast_free_kwh(index, now)
        try:
            await controller.apply(
                planned_w, now,
                import_rate=current.import_rate if current else None,
                export_rate=current.export_rate if current else None,
                grid_power_w=self._read_grid_power_w(),
                schedule_allows=schedule_allows,
                forecast_free_kwh=free_kwh,
                forecast_hours=free_hours,
            )
        except Exception as err:  # noqa: BLE001 — a bad device tick must not kill the timer
            _LOGGER.error("Load control tick failed for %s: %s", self.controllers[index].name, err)

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
        * **Free import window** (import price ≤ 0) — energy costs nothing, so take the
          device's whole envelope rather than metering it against export.

        Every input fails closed: an unknown rate, an unconfigured or unavailable grid sensor,
        or a device with no discoverable power sensor simply removes the surplus term, and the
        plan still drives. With no ``grid_power_sensor`` configured at all, surplus tracking
        never engages and this degrades to pure plan-following.
        """
        controller = self.controllers[index]
        current = self._current_interval(now)
        plan_w = max(0.0, self._device_power_now(index, current))
        import_rate = current.import_rate if current else None
        export_rate = current.export_rate if current else None

        surplus_w: Optional[float] = None
        if controller.greedy and not (
            controller.greedy_respects_schedule
            and not await self._schedule_allows_now(index, now)
        ):
            if export_rate is not None and export_rate <= 0.0 and self._grid_power_sensor:
                grid_w = self._read_grid_power_w()
                if grid_w is not None:
                    device_w = self._read_device_power_w(index)
                    surplus_w = max(0.0, -grid_w) + max(0.0, device_w or 0.0)
            if import_rate is not None and import_rate <= 0.0:
                cap_w = getattr(controller, "cap_w", 0.0)
                if cap_w > 0.0:
                    surplus_w = cap_w
            if controller.greedy_reason == "forecast_surplus":
                # Greedy condition #3 (see DeferrableLoadController's module docstring).
                # Unlike the two above it is forward-looking, so there is no live figure to
                # meter against — it is evaluated on the 5-minute tick by apply(), and
                # greedy_reason is the only record that it fired. Its bar is "even running
                # flat out for the whole look-ahead, the plan still throws free energy away",
                # so the correct response is the device's full envelope, exactly as an on/off
                # load's response is to switch fully on.
                #
                # Without this the condition would be silently inert for every modulating
                # device: apply() would set greedy_reason and light up the card's progress
                # bar while the target stayed at plan_w — a charger that reports it is
                # running on greedy and isn't.
                cap_w = getattr(controller, "cap_w", 0.0)
                if cap_w > 0.0:
                    surplus_w = max(surplus_w or 0.0, cap_w)
        target_w = max(plan_w, surplus_w or 0.0)
        if target_w <= 0.0:
            return 0.0, "off"
        return target_w, "surplus" if (surplus_w or 0.0) > plan_w else "plan"

    def _forecast_free_kwh(self, index: int, now: datetime) -> tuple[Optional[float], float]:
        """Free energy (kWh) the plan expects to leave UNCLAIMED over the next
        ``GREEDY_SURPLUS_LOOKAHEAD_HOURS``, plus the span (hours) actually covered.

        Two sources, per the user-facing definition of "free energy" (solar or free grid
        imports):

        * **Spilled export** — a slot whose export rate is $0 (or negative) and that the
          plan still exports into: that energy is being given away for nothing. Uses
          ``total_export_w`` (whole-house export, PV spill included), NOT ``export_w``
          (battery share only), and it's already net of every load the plan schedules in
          that slot — so nothing is subtracted from it here.
        * **Unused free-import window** — a slot whose import rate is $0: this device
          could run flat out for free then. Only the part the plan does NOT already have
          it running counts as unclaimed, hence the ``max_w - planned`` term.

        Returns ``(None, 0.0)`` when there isn't enough forecast left to judge — less
        than half the look-ahead window is covered by the plan. That's the same
        fail-closed discipline as the other greedy inputs: a sliver of horizon tail would
        otherwise shrink the controller's bar (which scales with the covered span) until
        a trivial surplus cleared it.
        """
        plan = self._plan
        controller = self.controllers.get(index)
        if not plan or controller is None:
            return None, 0.0
        window_end = now + timedelta(hours=GREEDY_SURPLUS_LOOKAHEAD_HOURS)
        total_kwh = 0.0
        covered_h = 0.0
        for pos, iv in enumerate(plan):
            slot_end = self._slot_end(plan, pos)
            o_start = max(iv.start, now)
            o_end = min(slot_end, window_end)
            if o_end <= o_start:
                continue
            hours = (o_end - o_start).total_seconds() / 3600.0
            free_w = 0.0
            if iv.export_rate is not None and iv.export_rate <= 0.0:
                free_w += max(0.0, iv.total_export_w)
            if iv.import_rate is not None and iv.import_rate <= 0.0:
                planned_w = self._device_power_now(index, iv)
                free_w += max(0.0, controller.max_w - max(0.0, planned_w))
            total_kwh += free_w * hours / 1000.0
            covered_h += hours
        if covered_h < GREEDY_SURPLUS_LOOKAHEAD_HOURS / 2.0:
            return None, 0.0
        return total_kwh, covered_h

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
