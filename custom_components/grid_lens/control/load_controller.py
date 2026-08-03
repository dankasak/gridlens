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
  exporting at least as much power as this device draws while the export price is $0 (so
  running it can't create new grid import — it only claims otherwise-worthless export).
  Folds into the same ``want_on`` computed each tick, so it's subject to the same
  debounce/transition-economy machinery as a plan-driven flip — no separate code path,
  no separate chatter risk. Optionally gated to the device's own configured availability
  window/weekly schedule (``greedy_respects_schedule``); off by default, since greedy is
  meant to be opportunistic ("don't leave free energy on the table"). Like everything
  else here, greedy is completely suppressed while a manual override is active — a human
  at the physical switch always wins.
* **Forecast surplus** (``greedy_forecast_surplus``, a third opt-in on top of greedy, off
  by default) — a *forward-looking* third greedy condition. The two conditions above are
  strictly instantaneous: they only fire once free energy is already flowing. On a
  solar+battery house that systematically fires late — mid-morning the battery soaks up
  every spare watt, so live export is ~0 and neither condition is true, yet the plan
  already knows that this afternoon far more energy will spill to a $0 export than this
  device could ever eat. By the time export actually shows up, hours of run-time have
  been wasted. So: if the plan forecasts that, over the look-ahead window, *more* free
  energy will be thrown away than this device could consume running flat out for that
  whole window, run it now.

  This one is a genuine bet and is therefore separately opt-in: unlike the other two it
  CAN create real, priced grid import in the moment, in exchange for capturing a much
  larger forecast spill. The mechanism that makes it pay is the battery — running now
  draws the battery down (or leaves it lower), and that hole is refilled by surplus that
  would otherwise have been exported for nothing. The "could run flat out for the whole
  window and still spill" threshold is what keeps it honest: a marginal, uncertain
  surplus never trips it.
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
        forecast_free_kwh: Optional[float] = None,
        forecast_hours: Optional[float] = None,
    ) -> bool:
        """True if Greedy Consumption says "on" right now, independent of the plan.

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
        self._greedy_free_kwh = forecast_free_kwh
        self._greedy_needed_kwh = (
            self.forecast_surplus_needed_kwh(forecast_hours) if forecast_hours else None
        )
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
        if export_rate is not None and export_rate <= 0.0 and grid_power_w is not None:
            # Sign convention: positive = importing, negative = exporting (see
            # CONF_GRID_POWER_SENSOR). exporting_w is the magnitude of current export.
            exporting_w = max(0.0, -grid_power_w)
            if self.max_w > 0.0 and exporting_w >= self._export_surplus_threshold_w():
                self._greedy_reason = "export_surplus"
                return True
        if self._forecast_surplus_wants_on(forecast_free_kwh, forecast_hours):
            self._greedy_reason = "forecast_surplus"
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
        """Free energy (kWh) that must be forecast wasted over ``hours`` before the
        forecast-surplus condition fires: exactly what this device would consume running
        flat out for that whole window.

        Deliberately the *full* window rather than some fraction of it. The condition is
        allowed to spend real money right now, so the bar is "even with this device
        running continuously from now to the end of the window, the plan still throws
        free energy away" — not "there's a bit of spare solar around".
        """
        return max(0.0, self.max_w) / 1000.0 * max(0.0, hours)

    def _forecast_surplus_wants_on(
        self, forecast_free_kwh: Optional[float], forecast_hours: Optional[float]
    ) -> bool:
        """Third greedy condition: the plan forecasts more free energy going to waste
        over the look-ahead than this device could possibly absorb (see module docstring).

        ``forecast_free_kwh`` is computed by ``LoadControlManager`` from the live plan
        (free energy the plan itself does NOT already allocate to this device);
        ``forecast_hours`` is the span it actually covered, which can be shorter than the
        nominal look-ahead near the end of the plan horizon — so the bar scales down with
        it rather than becoming unreachable. Fails closed on missing inputs, like the
        other two conditions.
        """
        if not self._greedy_forecast_surplus:
            return False
        if forecast_free_kwh is None or not forecast_hours or forecast_hours <= 0.0:
            return False
        needed = self.forecast_surplus_needed_kwh(forecast_hours)
        return needed > 0.0 and forecast_free_kwh >= needed

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
        forecast_free_kwh: Optional[float] = None,
        forecast_hours: Optional[float] = None,
    ) -> None:
        """Reconcile the switch toward the plan (plus Greedy Consumption, if enabled)
        for this tick.

        Debounce applies to any genuine flip (want != commanded) — plan-driven or
        greedy-triggered alike, both go through the same ``want_on`` below, so a
        greedy "on" is exactly as chatter-protected as a plan-driven one. A drift
        re-assert (want == commanded but the hardware has moved) is NOT debounced — it
        restores the state we already intend, so there's no chatter risk.
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
            return

        greedy_on = self._greedy_wants_on(
            import_rate, export_rate, grid_power_w, schedule_allows,
            forecast_free_kwh, forecast_hours,
        )
        want_on = greedy_on or self.desired_on(planned_w)
        tag = "_greedy" if (want_on and greedy_on) else ""

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
            # --- greedy observability (see the attributes' comment in __init__) ---
            # Which condition is holding the device on right now (None = greedy isn't the
            # reason it's on; the plan is, or it's off).
            "greedy_reason": self._greedy_reason,
            # Why greedy couldn't fire, when it couldn't: "schedule" (outside the device's
            # availability window with Respects Schedule on) or "override" (a human has
            # Force On/Off set). None = greedy was free to fire and simply didn't match.
            "greedy_blocked": self._greedy_blocked,
            # Forecast-surplus progress: free energy the plan expects to waste over the
            # look-ahead vs the bar it has to clear. Both None unless the forecast-surplus
            # toggle is on (the manager only computes it then).
            "forecast_free_kwh": (
                round(self._greedy_free_kwh, 2) if self._greedy_free_kwh is not None else None
            ),
            "forecast_needed_kwh": (
                round(self._greedy_needed_kwh, 2) if self._greedy_needed_kwh is not None else None
            ),
            "note": self._note,
        }
