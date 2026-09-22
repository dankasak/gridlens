"""DeferrableLoadController — actuates ONE simple on/off deferrable load (a ``switch.*``
OR ``climate.*`` entity) from the optimizer's planned per-device power.

Scope: "type 1" loads — a plain switchable appliance that draws roughly a fixed power
when on (pool pump, a smart-plug-fed EV cable, a resistive heater, an aircon unit).
GridLens decides on/off per interval and toggles the entity. Loads with richer control
(OCPP EV chargers with charge-current setpoints) are a separate, later mechanism — not
this controller.

**Climate entities** (aircon): "on"/"off" is the hvac_mode, not a native switch state —
``_actual_state()`` treats any state other than "off" as on. Actuation prefers
``climate.turn_on``/``climate.turn_off`` (both this integration's two shipped device
families — ECHONET Lite and SmartIR/Broadlink — support these), falling back to
``climate.set_hvac_mode`` for a climate integration that doesn't declare that support
(see ``_actuate()``). GridLens deliberately never touches hvac_mode or target temperature
beyond deciding on/off — comfort settings (mode, setpoint) stay under the user's own
control (or e.g. the ``climate_scheduler`` integration's), same "type 1" philosophy as a
plain switch. Note: if something else (a schedule, the user) also drives on/off on the
same climate entity, GridLens's plan and that other driver can fight — no arbitration is
attempted here, same as two humans fighting over one switch.

Design mirrors the battery side's discipline without reusing ``BatteryController`` (which
is SOC-guardrail- and inverter-HAL-specific):

* **On/off threshold** — the LP's per-device power is a continuous 0..max variable, so a
  fractional value has no direct meaning for a physically-binary device. "On" = the plan
  allocated at least ~half the device's rated power to it this slot (matching the advisory
  card's 3a ``_deferMode`` reading), with an absolute floor so tiny LP noise never counts.
* **Transition economy** — only issue a service call when the state actually needs to
  change; a re-assert fires only if the hardware has drifted from what we commanded.
* **Debounce** — a minimum on-time and off-time so a borderline, flip-flopping LP signal
  doesn't chatter a physical relay (real switching wear, unlike a cheap battery mode change).
* **Never raise from a command write** — a failed ``switch.turn_*`` logs and returns False,
  never propagates (mirrors ``inverters/sigenergy_mqtt.py._switch``).
* **No forced-off path** — deliberately. The deadman policy for loads is "leave as-is"
  (product decision 2026-07-23): cutting a real appliance mid-cycle on an HA restart or a
  missed tick has more real-world consequence than reverting an inverter mode, so nothing
  here ever forces a load off on shutdown/stale-plan; a stopped loop just leaves the last
  commanded state in place.
* **Greedy Consumption** (opt-in per device, off by default) — a real-time safety-net on
  top of the LP's plan: turn the device on any time energy is genuinely free right now,
  regardless of what the plan scheduled for this slot. Two conditions (either is enough):
  the current import price is free (a plan's $0 window), or the household is currently
  exporting at least as much power as this device draws while the export price is at or
  below the user's **Minimum Export Price** (``min_export_price``, a c/kWh preference;
  0 disables the floor, so the bar is then just "export price ≤ $0"). Running the device
  can't create new grid import — the house is already exporting more than its draw — it
  only redirects export the user has said isn't worth selling into self-consumption.
  Folds into the same ``want_on`` computed each tick, so it's subject to the same
  debounce/transition-economy machinery as a plan-driven flip — no separate code path,
  no separate chatter risk. Optionally gated to the device's own configured availability
  window/weekly schedule (``greedy_respects_schedule``); off by default, since greedy is
  meant to be opportunistic ("don't leave free energy on the table"). Like everything
  else here, greedy is completely suppressed while a manual override is active — a human
  at the physical switch always wins.
* **Forecast surplus** (``greedy_forecast_surplus``, a third opt-in on top of greedy, off
  by default) — a *forward-looking*, *proportional* third greedy condition. The two above
  are strictly instantaneous: they only fire once wasted energy is already flowing. On a
  solar+battery house that systematically fires late — mid-morning the battery soaks up
  every spare watt, so live export is ~0 and neither condition is true, yet the plan
  already knows this afternoon will spill far more than the retailer's feed-in is worth.
  By the time export actually shows up, hours of run-time have been wasted.

  So: ``LoadControlManager._forecast_surplus_budget`` sums how much energy the plan will
  waste (export at or below the Minimum Export Price, plus unused free-import headroom)
  over a look-ahead window, and the device is run *now* at that average rate —
  ``spill_kwh / covered_h`` — clamped to the device's own envelope. Not all-or-nothing
  against a "could it run flat out the whole window" bar: greedy activates exactly to the
  extent the plan would otherwise waste energy.

  Opt-in on top of the master switch because it fires ahead of any live signal, purely off
  the plan's forecast. Two things keep the bet safe, so running now draws the *battery*
  down rather than the grid and the hole is refilled by the forecast spill:

  1. **Reservation clip.** The budget window ends at the first slot where the plan itself
     starts materially discharging the battery — past there the plan is spending the
     battery on something it values (evening peak export, a high import rate), and greedy
     must not borrow charge across it. A ``DispatchInterval`` carries no per-slot SOC, so
     "stop at the first planned drawdown" is the proxy for "don't discharge below the
     plan's own SOC trajectory".
  2. **Battery headroom, rate and energy.** ``battery_headroom_w`` (free discharge rate
     right now) caps the draw; ``battery_headroom_kwh`` (energy to the configured minimum
     SOC) caps it too, via the steady rate that would use no more than that energy over the
     *whole* window in the worst case a back-loaded spill never shows up to repay it — a
     proportional clamp on the rate, not an all-or-nothing bar against the household's
     entire forecast waste (fixed 2026-09-20 — see ``_forecast_surplus_target_w``'s
     docstring). Both come from ``LoadControlManager``; either missing (no battery, no
     capacity, unreadable sensor) fails the condition closed — same discipline as every
     other Greedy Consumption input.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# ClimateEntityFeature bit flags this module needs. Mirrored here (not imported from
# homeassistant.components.climate) to keep this module light and importable in the
# offline test harness, which doesn't stub that component — must stay in sync with HA
# core's ClimateEntityFeature.TURN_ON / TURN_OFF.
_CLIMATE_FEATURE_TURN_ON = 256
_CLIMATE_FEATURE_TURN_OFF = 128


class DeferrableLoadController:
    def __init__(
        self,
        hass: HomeAssistant,
        *,
        name: str,
        switch_entity_id: str,
        max_w: float,
        min_on_seconds: float = 900.0,
        min_off_seconds: float = 900.0,
        on_fraction: float = 0.5,
        on_floor_w: float = 50.0,
        climate_on_mode: str = "",
    ) -> None:
        self.hass = hass
        self.name = name
        self.switch_entity_id = switch_entity_id
        self.max_w = max(0.0, float(max_w))
        self.min_on = float(min_on_seconds)
        self.min_off = float(min_off_seconds)
        self.on_fraction = float(on_fraction)
        self.on_floor_w = float(on_floor_w)
        # Only consulted for a climate.* entity that doesn't support climate.turn_on/off
        # (see _actuate()) — the hvac_mode to command for "on". "" = auto-pick the
        # entity's first non-"off" hvac_modes entry at actuation time.
        self.climate_on_mode = climate_on_mode or ""

        # Last state WE commanded. None until the first tick — the real switch state at
        # startup is unknown / could be user-set, so the first tick always issues a command
        # to establish a known state.
        self._commanded: Optional[bool] = None
        self._changed_at: Optional[datetime] = None
        self._note = "not_started"

        # Manual override: None = auto (plan-driven), True = forced on, False = forced
        # off. While set, apply() does nothing at all — no plan-driven flips AND no
        # drift re-assert: an override is "GridLens, hands off; set it to X and leave
        # it", so a human at the physical switch always wins afterwards.
        self._override: Optional[bool] = None

        # Greedy Consumption: opt-in real-time override of the plan (see module
        # docstring). Both default OFF — greedy never activates on a fresh install/entity
        # restore, and "respects schedule" defaults to the more opportunistic behaviour
        # (ignore the schedule) unless the user asks for the stricter one.
        self._greedy_enabled = False
        self._greedy_respects_schedule = False
        # Forecast-surplus greedy condition (see module docstring). Also default OFF, and
        # additionally gated behind _greedy_enabled — it's a third greedy condition, not a
        # parallel feature, so the master greedy switch still turns everything off.
        self._greedy_forecast_surplus = False

        # Hard SOC interlock (see const.py's CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT and
        # LoadControlManager._soc_cutoff_active): True while this device's own configured
        # SOC sensor reads at/above its configured ceiling. Set by apply() every tick,
        # read by ModulatingLoadController.modulate() on the faster 30s loop (the on/off
        # controller has no second loop — apply() already actuates directly). Overrides
        # every other term (plan, both greedy conditions, battery priority, AC output cap)
        # — the one thing that can't ever raise it back is a manual override, which is
        # checked first and stands down all of this, same as every other decision here.
        self._soc_cutoff = False

        # Observability only — never read by any decision, only published by status()
        # (and from there onto the control switch's attributes, the Load Control card and
        # the Power Flow card). Which greedy condition fired on the last evaluated tick,
        # why greedy was blocked if it was, and the forecast-surplus figures behind the
        # third condition so the UI can show progress toward the bar rather than just a
        # boolean that flips with no warning.
        self._greedy_reason: Optional[str] = None
        self._greedy_blocked: Optional[str] = None
        self._greedy_free_kwh: Optional[float] = None
        self._greedy_needed_kwh: Optional[float] = None
        self._greedy_battery_headroom_w: Optional[float] = None
        self._greedy_battery_headroom_kwh: Optional[float] = None
        self._greedy_ac_output_headroom_w: Optional[float] = None
        # Power (W) the proportional forecast-surplus condition wants this device to draw
        # right now (0.0 when it isn't firing). Read by
        # LoadControlManager._modulation_target_w for a modulating device and published by
        # status(); for an on/off device it's simply max_w when firing, else 0.
        self._greedy_forecast_target_w: float = 0.0

    # ------------------------------------------------------------------ identity
    @property
    def join_key(self) -> str:
        """The entity id cards and auxiliary entities fingerprint to pair themselves with
        this device.

        Every GridLens entity that hangs off a deferrable device — the greedy switches, the
        override select, and the device's row on the Load Control card — finds its device by
        matching a published ``switch`` attribute rather than by any naming convention
        (FEATURES.md §10). For an on/off load that key is simply the control switch.

        It exists as a property rather than a bare attribute read because a *modulating*
        load may legitimately have no on/off entity at all (an OCPP charger stops by being
        told 0 A), and an empty string is not a usable key: every switchless charger on an
        install would collide on it, silently pairing one device's row with another's
        entities. The subclass overrides this to fall back to the setpoint entity.
        """
        return self.switch_entity_id

    @property
    def greedy_reason(self) -> Optional[str]:
        """Which greedy condition fired on the last evaluated tick, or None.

        Published in ``status()`` for the UI, but also *read* by
        ``LoadControlManager._modulation_target_w``: a modulating load has to turn the
        forecast-surplus condition into an actual power target, and this is the only record
        that it fired (the condition is expensive to evaluate and is computed once per
        5-minute tick, not per 30-second modulation tick)."""
        return self._greedy_reason

    # ------------------------------------------------------------------ policy
    def on_threshold_w(self) -> float:
        """Planned power (W) at/above which this slot counts as 'device on'."""
        return max(self.on_floor_w, self.on_fraction * self.max_w)

    def desired_on(self, planned_w: float) -> bool:
        return planned_w >= self.on_threshold_w()

    def _greedy_wants_on(
        self,
        import_rate: Optional[float],
        export_rate: Optional[float],
        grid_power_w: Optional[float],
        schedule_allows: Optional[bool],
        forecast_spill_kwh: Optional[float] = None,
        forecast_hours: Optional[float] = None,
        battery_headroom_w: Optional[float] = None,
        battery_headroom_kwh: Optional[float] = None,
        ac_output_headroom_w: Optional[float] = None,
        min_export_price: float = 0.0,
    ) -> bool:
        """True if Greedy Consumption says "on" right now, independent of the plan.

        ``min_export_price`` ($/kWh) is the user's Minimum Export Price preference: the
        export-surplus condition treats any export priced at or below it as not worth
        selling, so greedy soaks it locally instead. 0.0 disables the floor and restores
        the original "export price ≤ $0" bar exactly.

        ``forecast_spill_kwh`` / ``forecast_hours`` / ``battery_headroom_w`` /
        ``battery_headroom_kwh`` / ``ac_output_headroom_w`` drive the proportional
        forecast-surplus condition (see ``_forecast_surplus_target_w``). It also sets
        ``self._greedy_forecast_target_w`` — the power that condition wants — which the
        manager reads for a modulating device.

        Uses ``self.max_w`` (the device's real full configured draw) — NOT
        ``on_threshold_w()``'s 50%-of-max fractional floor, which is a different concept
        (mapping the LP's continuous per-slot allocation to a binary switch state). The
        greedy condition is specifically "would not create new grid import", which needs
        the device's actual full draw. Missing/unknown inputs (a sensor is unavailable,
        a rate is unknown) fail closed — greedy contributes nothing rather than guessing.

        Side effect, deliberately: records WHICH condition fired in ``_greedy_reason``
        (and the forecast figures behind the third one) for ``status()`` to publish. A
        greedy "on" is otherwise indistinguishable from a plan-driven one in the UI —
        "why is my pool pump running?" is the whole observability question here.
        """
        self._greedy_free_kwh = forecast_spill_kwh
        self._greedy_needed_kwh = (
            self.forecast_surplus_needed_kwh(forecast_hours) if forecast_hours else None
        )
        self._greedy_battery_headroom_w = battery_headroom_w
        self._greedy_battery_headroom_kwh = battery_headroom_kwh
        self._greedy_ac_output_headroom_w = ac_output_headroom_w
        self._greedy_forecast_target_w = 0.0
        self._greedy_reason = None
        self._greedy_blocked = None
        if not self._greedy_enabled:
            return False
        if self._greedy_respects_schedule and schedule_allows is False:
            self._greedy_blocked = "schedule"
            return False
        if import_rate is not None and import_rate <= 0.0:
            self._greedy_reason = "import_free"
            return True
        # "Export is being wasted" — historically export_rate <= $0, now also any rate at
        # or below the user's Minimum Export Price (a rate they've said isn't worth
        # selling). max(0.0, ...) keeps a negative/zero preference from ever RAISING the
        # bar below $0, and a 0.0 preference reproduces the old test byte-for-byte.
        export_waste_ceiling = max(0.0, min_export_price)
        if export_rate is not None and export_rate <= export_waste_ceiling:
            if grid_power_w is None:
                # The export price is $0 — the one situation this condition exists for —
                # but there is no live grid reading to measure the spill against, so it
                # cannot fire. Record that, because it is otherwise INVISIBLE: the device
                # sits off through hours of free export and every published field says
                # "armed, waiting for free energy", which reads as "no surplus yet" rather
                # than "structurally unable to see one". Almost always means no
                # CONF_GRID_POWER_SENSOR is configured (it is optional, and unlike the
                # energy sensors it cannot be auto-discovered from the Energy dashboard,
                # which stores only energy statistics — never a live power entity); it
                # also covers a configured sensor that is unavailable right now.
                self._greedy_blocked = "no_grid_power"
            else:
                # Sign convention: positive = importing, negative = exporting (see
                # CONF_GRID_POWER_SENSOR). exporting_w is the magnitude of current export.
                exporting_w = max(0.0, -grid_power_w)
                if self.max_w > 0.0 and exporting_w >= self._export_surplus_threshold_w():
                    self._greedy_reason = "export_surplus"
                    self._greedy_blocked = None
                    return True
        target_w = self._forecast_surplus_target_w(
            forecast_spill_kwh, forecast_hours, battery_headroom_w, battery_headroom_kwh,
            ac_output_headroom_w,
        )
        if target_w > 0.0:
            self._greedy_forecast_target_w = target_w
            self._greedy_reason = "forecast_surplus"
            # A later condition firing supersedes the block recorded above — greedy is on,
            # so publishing a "blocked" reason alongside it would just be noise.
            self._greedy_blocked = None
            return True
        return False

    def _export_surplus_threshold_w(self) -> float:
        """Export (W) the house must already be spilling before greedy condition #2 fires.

        For a binary load this is the device's full draw: it can only be all-on, so turning
        it on with anything less already covered would create new priced grid import, which
        is exactly what this condition promises never to do. Factored out as a hook purely so
        a load that CAN modulate (``ModulatingLoadController``) can lower the bar to its own
        minimum without duplicating ``_greedy_wants_on``."""
        return self.max_w

    def forecast_surplus_needed_kwh(self, hours: float) -> float:
        """Reference figure for the observability progress bar: what this device would
        consume running flat out for ``hours``. NOT the fire threshold any more (the
        forecast-surplus condition is proportional since 2026-09-11) — it's the
        denominator the card divides ``forecast_free_kwh`` by to show "how close is the
        forecast spill to keeping this device fully fed"."""
        return max(0.0, self.max_w) / 1000.0 * max(0.0, hours)

    def _forecast_surplus_snap_w(self, target_w: float) -> float:
        """Clamp the proportional forecast-surplus rate to what this device can physically
        do. The caller has already checked ``target_w`` clears
        ``_export_surplus_threshold_w()`` (``max_w`` for an on/off load), so an on/off load
        just runs fully on. ``ModulatingLoadController`` overrides this to clamp into its
        own ``[min_w, cap_w]`` envelope instead."""
        return self.max_w

    def _forecast_surplus_target_w(
        self,
        forecast_spill_kwh: Optional[float],
        forecast_hours: Optional[float],
        battery_headroom_w: Optional[float] = None,
        battery_headroom_kwh: Optional[float] = None,
        ac_output_headroom_w: Optional[float] = None,
    ) -> float:
        """Power (W) the *proportional* forecast-surplus condition wants this device to
        draw right now — 0.0 when it isn't firing (see module docstring).

        ``forecast_spill_kwh`` / ``forecast_hours`` come from
        ``LoadControlManager._forecast_surplus_budget``: how much energy the plan will
        waste over the reservation-clipped look-ahead, and that window's span. The device
        runs at the average waste rate ``forecast_spill_kwh / forecast_hours``, clamped to
        what it can physically do (``_forecast_surplus_snap_w``).

        ``_export_surplus_threshold_w()`` is the smallest draw worth a write — ``max_w``
        for an on/off load (all-or-nothing), ``min_w`` for a modulating one. If the spill
        rate doesn't even reach that, the condition simply doesn't fire and records
        nothing (there's just no surplus to chase).

        Once the spill *is* big enough, two battery gates apply — both fail-closed on a
        missing/None input (no battery, no capacity, unreadable sensor) and both recording
        ``"no_battery_headroom"`` in ``_greedy_blocked`` so the "armed but can't act" state
        is visible rather than silently inert:

        * ``battery_headroom_w`` — free discharge rate right now — caps the draw; if that
          cap drops it back below the minimum-worthwhile draw, the condition is blocked.
        * ``battery_headroom_kwh`` — energy to the configured minimum SOC — bounds the
          *rate*, not the pass/fail: ``battery_headroom_kwh / forecast_hours`` is the
          steady draw that, sustained for the whole window, uses no more than that energy
          in the worst case the spill never shows up to repay it (a back-loaded spill can
          leave the battery down by the full amount drawn before the refill lands). Fixed
          2026-09-20: this used to require the *whole* ``forecast_spill_kwh`` — the entire
          household's forecast waste, not this device's own draw — to fit in headroom, an
          all-or-nothing bar a modest battery can essentially never clear on a day with a
          large spill (a 24 kWh battery can never have >21.6 kWh of headroom to a 10% min
          SOC, so it permanently failed against a 30+ kWh spill regardless of time of day
          or actual SOC — see GRIDLENS_CHECKLIST.md, 2026-09-20). ``min(rate_w,
          battery_headroom_w, battery_safe_rate_w)`` folds this in as one more proportional
          clamp on the rate a modulating load can safely be pinned to, same as the other
          two; an on/off load still can't do partial, so this only reduces to a pass/fail
          for it — but now against what *this device* would draw over the window, not the
          whole house's spill.

        A third, optional gate — ``ac_output_headroom_w``
        (``LoadControlManager._ac_output_headroom_w``) — clamps the draw again when the
        household has configured an inverter AC output ceiling (``CONF_MAX_AC_OUTPUT_KW``):
        the plan's own forecast reasons about PV/battery *capability*, never about whether
        the plant can physically deliver that much combined AC power, so a day PV alone is
        already near the ceiling would otherwise size this device's target off battery
        headroom that can't actually reach it (found 2026-09-12 — GRIDLENS_CHECKLIST.md).
        None (the default, when no ceiling is configured) leaves this a no-op — unlike the
        battery gates, its absence is NOT itself a block, since the feature is opt-in and
        most installs have no such ceiling to model.
        """
        if not self._greedy_forecast_surplus:
            return 0.0
        if forecast_spill_kwh is None or not forecast_hours or forecast_hours <= 0.0:
            return 0.0
        if forecast_spill_kwh <= 0.0:
            return 0.0
        rate_w = forecast_spill_kwh * 1000.0 / forecast_hours
        min_draw = self._export_surplus_threshold_w()
        if rate_w + 1e-6 < min_draw:
            return 0.0  # the spill itself isn't enough to justify even a minimal run
        if battery_headroom_w is None or battery_headroom_kwh is None:
            self._greedy_blocked = "no_battery_headroom"
            return 0.0
        if battery_headroom_w <= 0.0:
            self._greedy_blocked = "no_battery_headroom"
            return 0.0
        battery_safe_rate_w = battery_headroom_kwh * 1000.0 / forecast_hours
        target_w = min(rate_w, battery_headroom_w, battery_safe_rate_w)
        if target_w + 1e-6 < min_draw:
            self._greedy_blocked = "no_battery_headroom"
            return 0.0
        if ac_output_headroom_w is not None:
            target_w = min(target_w, ac_output_headroom_w)
            if target_w + 1e-6 < min_draw:
                self._greedy_blocked = "no_ac_output_headroom"
                return 0.0
        return self._forecast_surplus_snap_w(target_w)

    def _actual_state(self) -> Optional[bool]:
        st = self.hass.states.get(self.switch_entity_id)
        if st is None:
            return None
        s = str(st.state).lower()
        if s in ("unknown", "unavailable"):
            return None
        if self.switch_entity_id.startswith("climate."):
            # A climate entity's state IS its hvac_mode — anything other than "off"
            # counts as "on" (cool/heat/dry/fan_only/heat_cool/auto/...), unlike a
            # switch's strict on/off vocabulary.
            return s != "off"
        if s == "on":
            return True
        if s == "off":
            return False
        return None  # unrecognized state string

    # ------------------------------------------------------------------ tick
    async def apply(
        self,
        planned_w: float,
        now: datetime,
        *,
        import_rate: Optional[float] = None,
        export_rate: Optional[float] = None,
        grid_power_w: Optional[float] = None,
        schedule_allows: Optional[bool] = None,
        forecast_spill_kwh: Optional[float] = None,
        forecast_hours: Optional[float] = None,
        battery_headroom_w: Optional[float] = None,
        battery_headroom_kwh: Optional[float] = None,
        ac_output_headroom_w: Optional[float] = None,
        min_export_price: float = 0.0,
        soc_cutoff: bool = False,
    ) -> None:
        """Reconcile the switch toward the plan (plus Greedy Consumption, if enabled)
        for this tick.

        Debounce applies to any genuine flip (want != commanded) — plan-driven or
        greedy-triggered alike, both go through the same ``want_on`` below, so a
        greedy "on" is exactly as chatter-protected as a plan-driven one. A drift
        re-assert (want == commanded but the hardware has moved) is NOT debounced — it
        restores the state we already intend, so there's no chatter risk.

        ``soc_cutoff`` (``LoadControlManager._soc_cutoff_active``) is a hard interlock:
        this device's own configured SOC sensor is at/above its configured ceiling.
        Checked after the manual override (a human's explicit Force On still wins — see
        the module docstring's override discipline) but before the plan/greedy decision,
        and forces an immediate, debounce-free off — same urgency as an override, because
        the whole point is to stop drawing current *now*, not after up to ``min_off``
        seconds of hold.
        """
        if self._override is not None:
            self._note = f"override_{'on' if self._override else 'off'}"
            # Greedy isn't evaluated at all under an override — clear the published
            # reason so the UI can't keep showing a stale "running because X" for a
            # device a human has since taken manual control of.
            self._greedy_reason = None
            self._greedy_blocked = "override"
            self._greedy_free_kwh = None
            self._greedy_needed_kwh = None
            self._greedy_battery_headroom_w = None
            self._greedy_battery_headroom_kwh = None
            self._greedy_ac_output_headroom_w = None
            self._greedy_forecast_target_w = 0.0
            return

        if soc_cutoff != self._soc_cutoff:
            self._soc_cutoff = soc_cutoff
            if soc_cutoff:
                _LOGGER.warning(
                    "Load control: %s reached its configured SOC cutoff — stopping "
                    "(see CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT)", self.name,
                )
        if soc_cutoff:
            self._note = "soc_cutoff"
            self._greedy_reason = None
            self._greedy_blocked = "soc_cutoff"
            self._greedy_free_kwh = None
            self._greedy_needed_kwh = None
            self._greedy_battery_headroom_w = None
            self._greedy_battery_headroom_kwh = None
            self._greedy_ac_output_headroom_w = None
            self._greedy_forecast_target_w = 0.0
            if self._commanded is not False:
                await self._command(False, now)
            return

        greedy_on = self._greedy_wants_on(
            import_rate, export_rate, grid_power_w, schedule_allows,
            forecast_spill_kwh, forecast_hours, battery_headroom_w,
            battery_headroom_kwh, ac_output_headroom_w, min_export_price,
        )
        plan_on = self.desired_on(planned_w)
        if plan_on and self._greedy_reason is not None:
            # The plan alone already wanted this device on this slot — greedy also
            # matched, but it isn't WHY the device is running (the scheduler would have
            # triggered this regardless), so don't attribute the run, or the energy it
            # consumes, to greedy. Keeps the invariant status()/greedy_reason document:
            # None = "greedy isn't the reason it's on; the plan is, or it's off."
            self._greedy_reason = None
        want_on = greedy_on or plan_on
        tag = "_greedy" if self._greedy_reason else ""

        # First tick: establish a known state regardless of debounce.
        if self._commanded is None:
            await self._command(want_on, now, tag=tag)
            return

        if want_on != self._commanded:
            held = (now - self._changed_at).total_seconds() if self._changed_at else 1e9
            min_hold = self.min_on if self._commanded else self.min_off
            if held < min_hold:
                self._note = f"hold_{'on' if self._commanded else 'off'}_debounce"
                return
            await self._command(want_on, now, tag=tag)
            return

        # want_on == commanded: re-assert only if the hardware drifted from it (e.g. a
        # transport blip dropped the write, or something else toggled it).
        actual = self._actual_state()
        if actual is not None and actual != self._commanded:
            _LOGGER.warning(
                "Deferrable load %s drifted (hardware=%s, commanded=%s) — re-issuing",
                self.name, "on" if actual else "off", "on" if self._commanded else "off",
            )
            await self._command(want_on, now, reset_timer=False, tag=tag)
        else:
            self._note = f"holding_{'on' if self._commanded else 'off'}{tag}"

    # ------------------------------------------------------------------ manual override
    @property
    def override(self) -> Optional[bool]:
        return self._override

    async def set_override(
        self, mode: Optional[bool], now: datetime, *, actuate: bool = True
    ) -> None:
        """Set (or clear) the manual override.

        ``mode`` True/False = force on/off: issue ONE immediate command (no debounce — a
        direct user action, not a chattering plan signal), then stop driving the load
        until the override is cleared. ``mode`` None = restore GridLens control: the next
        plan-driven ``apply`` re-establishes state immediately (the first-tick path, which
        also skips debounce — "restore control" means act on the plan now).

        ``actuate=False`` restores a persisted override across an HA restart without
        touching the hardware (the leave-as-is deadman discipline).
        """
        self._override = mode
        if mode is None:
            # Force a clean re-establish on the next apply() — debounce-free by design.
            self._commanded = None
            self._note = "override_cleared"
            return
        if actuate:
            _LOGGER.warning(
                "Manual override for %s: forcing %s (%s)",
                self.name, "on" if mode else "off", self.switch_entity_id,
            )
            await self._command(mode, now)
        else:
            self._note = f"override_{'on' if mode else 'off'}_restored"

    async def _command(
        self, want_on: bool, now: datetime, *, reset_timer: bool = True, tag: str = ""
    ) -> bool:
        try:
            label = await self._actuate(want_on)
        except Exception as err:  # noqa: BLE001 — a failed write must never kill the loop
            _LOGGER.error(
                "Deferrable load %s: command(%s, want_on=%s) failed: %s",
                self.name, self.switch_entity_id, want_on, err,
            )
            self._note = f"command_error:{err}"
            return False
        self._commanded = want_on
        if reset_timer:
            self._changed_at = now
        self._note = f"commanded_{'on' if want_on else 'off'}{tag}"
        _LOGGER.info(
            "Deferrable load %s → %s (%s)%s", self.name, label, self.switch_entity_id,
            " [greedy]" if tag else "",
        )
        return True

    async def _actuate(self, want_on: bool) -> str:
        """Issue the HA service call for ``want_on``. Returns a short label for the log
        line. Raises on failure — ``_command`` (the only caller) turns that into a safe,
        logged no-op; never propagates further."""
        if not self.switch_entity_id.startswith("climate."):
            service = "turn_on" if want_on else "turn_off"
            await self.hass.services.async_call(
                "switch", service, {"entity_id": self.switch_entity_id}, blocking=True
            )
            return f"switch.{service}"

        if self._climate_supports_turn_on_off():
            service = "turn_on" if want_on else "turn_off"
            await self.hass.services.async_call(
                "climate", service, {"entity_id": self.switch_entity_id}, blocking=True
            )
            return f"climate.{service}"

        # Fallback for a climate integration that doesn't declare TURN_ON/TURN_OFF
        # support (not every one does) — drive hvac_mode directly instead.
        hvac_mode = "off" if not want_on else (self.climate_on_mode or self._default_on_mode())
        await self.hass.services.async_call(
            "climate", "set_hvac_mode",
            {"entity_id": self.switch_entity_id, "hvac_mode": hvac_mode}, blocking=True,
        )
        return f"climate.set_hvac_mode({hvac_mode})"

    def _climate_supports_turn_on_off(self) -> bool:
        st = self.hass.states.get(self.switch_entity_id)
        features = (st.attributes.get("supported_features", 0) or 0) if st else 0
        return bool(features & _CLIMATE_FEATURE_TURN_ON) and bool(features & _CLIMATE_FEATURE_TURN_OFF)

    def _default_on_mode(self) -> str:
        """Best-guess "on" hvac_mode when neither turn_on support nor an explicit
        ``climate_on_mode`` is available: the entity's own first non-"off" advertised
        mode (HA convention lists the primary mode(s) before "off")."""
        st = self.hass.states.get(self.switch_entity_id)
        modes = (st.attributes.get("hvac_modes") if st else None) or []
        for mode in modes:
            if mode != "off":
                return mode
        return "heat_cool"

    # ------------------------------------------------------------------ greedy consumption
    @property
    def greedy(self) -> bool:
        return self._greedy_enabled

    @property
    def greedy_respects_schedule(self) -> bool:
        return self._greedy_respects_schedule

    def set_greedy(self, enabled: bool) -> None:
        self._greedy_enabled = bool(enabled)

    def set_greedy_respects_schedule(self, enabled: bool) -> None:
        self._greedy_respects_schedule = bool(enabled)

    @property
    def greedy_forecast_surplus(self) -> bool:
        return self._greedy_forecast_surplus

    def set_greedy_forecast_surplus(self, enabled: bool) -> None:
        self._greedy_forecast_surplus = bool(enabled)

    def status(self) -> dict:
        return {
            "name": self.name,
            # How this device is driven, for consumers that must branch on it (the Load
            # Control card, sensor.py's deferrable_loads attribute). Set here rather than
            # only on the modulating subclass so the key is always present and a consumer
            # never has to treat "absent" as a third case.
            "control_type": "onoff",
            "switch": self.join_key,
            "max_w": round(self.max_w, 1),
            "on_threshold_w": round(self.on_threshold_w(), 1),
            "commanded": ("on" if self._commanded else "off") if self._commanded is not None else "unknown",
            "changed_at": self._changed_at.isoformat() if self._changed_at else None,
            "override": (
                ("on" if self._override else "off") if self._override is not None else "auto"
            ),
            "greedy": self._greedy_enabled,
            "greedy_respects_schedule": self._greedy_respects_schedule,
            "greedy_forecast_surplus": self._greedy_forecast_surplus,
            # Hard SOC interlock (see CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT). True means
            # this device is being force-stopped regardless of plan/greedy, because its
            # own configured SOC sensor is at/above its configured ceiling right now.
            "soc_cutoff": self._soc_cutoff,
            # --- greedy observability (see the attributes' comment in __init__) ---
            # Which condition is holding the device on right now (None = greedy isn't the
            # reason it's on; the plan is, or it's off).
            "greedy_reason": self._greedy_reason,
            # Why greedy couldn't fire, when it couldn't: "schedule" (outside the device's
            # availability window with Respects Schedule on), "override" (a human has
            # Force On/Off set), "no_grid_power" (the export price is $0 but there is no
            # readable grid power sensor, so the export-surplus condition can't be judged),
            # "no_battery_headroom" (the forecast-surplus bar cleared, but the battery
            # has no configured/readable SOC-and-charge-sensor headroom to safely draw on,
            # so firing would be real, unbuffered grid import), or "no_ac_output_headroom"
            # (a configured inverter AC output ceiling — CONF_MAX_AC_OUTPUT_KW — leaves no
            # room once current plant output is accounted for). None = greedy was free to
            # fire and simply didn't match.
            "greedy_blocked": self._greedy_blocked,
            # Forecast-surplus figures: energy the plan expects to waste over the
            # reservation-clipped look-ahead (`forecast_free_kwh`), the flat-out reference
            # the card's progress bar divides it by (`forecast_needed_kwh`), and the
            # proportional power the condition is actually asking for right now
            # (`forecast_target_w`, 0 when it isn't firing). All None/0 unless the
            # forecast-surplus toggle is on — the manager only computes the inputs then.
            "forecast_free_kwh": (
                round(self._greedy_free_kwh, 2) if self._greedy_free_kwh is not None else None
            ),
            "forecast_needed_kwh": (
                round(self._greedy_needed_kwh, 2) if self._greedy_needed_kwh is not None else None
            ),
            "forecast_target_w": round(self._greedy_forecast_target_w, 1),
            # Battery headroom backing the forecast-surplus gate: free discharge rate now
            # (W) and energy to the configured minimum SOC (kWh). None = no battery / no
            # capacity configured, or a sensor couldn't be read.
            "forecast_battery_headroom_w": (
                round(self._greedy_battery_headroom_w, 1)
                if self._greedy_battery_headroom_w is not None else None
            ),
            "forecast_battery_headroom_kwh": (
                round(self._greedy_battery_headroom_kwh, 2)
                if self._greedy_battery_headroom_kwh is not None else None
            ),
            # Inverter AC output headroom (W) backing the forecast-surplus gate, when
            # CONF_MAX_AC_OUTPUT_KW is configured. None = no ceiling configured (the
            # common case — this is a no-op then, not a block).
            "forecast_ac_output_headroom_w": (
                round(self._greedy_ac_output_headroom_w, 1)
                if self._greedy_ac_output_headroom_w is not None else None
            ),
            "note": self._note,
        }
