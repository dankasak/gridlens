"""DeferrableLoadController — actuates ONE simple on/off deferrable load (a ``switch.*``
entity) from the optimizer's planned per-device power.

Scope: "type 1" loads — a plain switchable appliance that draws roughly a fixed power
when on (pool pump, a smart-plug-fed EV cable, a resistive heater). GridLens decides
on/off per interval and toggles the switch. Loads with richer control (OCPP EV chargers
with charge-current setpoints) are a separate, later mechanism — not this controller.

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
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self.hass = hass
        self.name = name
        self.switch_entity_id = switch_entity_id
        self.max_w = max(0.0, float(max_w))
        self.min_on = float(min_on_seconds)
        self.min_off = float(min_off_seconds)
        self.on_fraction = float(on_fraction)
        self.on_floor_w = float(on_floor_w)

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
    ) -> bool:
        """True if Greedy Consumption says "on" right now, independent of the plan.

        Uses ``self.max_w`` (the device's real full configured draw) — NOT
        ``on_threshold_w()``'s 50%-of-max fractional floor, which is a different concept
        (mapping the LP's continuous per-slot allocation to a binary switch state). The
        greedy condition is specifically "would not create new grid import", which needs
        the device's actual full draw. Missing/unknown inputs (a sensor is unavailable,
        a rate is unknown) fail closed — greedy contributes nothing rather than guessing.
        """
        if not self._greedy_enabled:
            return False
        if self._greedy_respects_schedule and schedule_allows is False:
            return False
        if import_rate is not None and import_rate <= 0.0:
            return True
        if export_rate is not None and export_rate <= 0.0 and grid_power_w is not None:
            # Sign convention: positive = importing, negative = exporting (see
            # CONF_GRID_POWER_SENSOR). exporting_w is the magnitude of current export.
            exporting_w = max(0.0, -grid_power_w)
            if self.max_w > 0.0 and exporting_w >= self.max_w:
                return True
        return False

    def _actual_state(self) -> Optional[bool]:
        st = self.hass.states.get(self.switch_entity_id)
        if st is None:
            return None
        s = str(st.state).lower()
        if s == "on":
            return True
        if s == "off":
            return False
        return None  # unavailable / unknown

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
            return

        greedy_on = self._greedy_wants_on(import_rate, export_rate, grid_power_w, schedule_allows)
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
        service = "turn_on" if want_on else "turn_off"
        try:
            await self.hass.services.async_call(
                "switch", service, {"entity_id": self.switch_entity_id}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 — a failed write must never kill the loop
            _LOGGER.error(
                "Deferrable load %s: switch.%s(%s) failed: %s",
                self.name, service, self.switch_entity_id, err,
            )
            self._note = f"command_error:{err}"
            return False
        self._commanded = want_on
        if reset_timer:
            self._changed_at = now
        self._note = f"commanded_{'on' if want_on else 'off'}{tag}"
        _LOGGER.info(
            "Deferrable load %s → %s (%s)%s", self.name, service, self.switch_entity_id,
            " [greedy]" if tag else "",
        )
        return True

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

    def status(self) -> dict:
        return {
            "name": self.name,
            "switch": self.switch_entity_id,
            "max_w": round(self.max_w, 1),
            "on_threshold_w": round(self.on_threshold_w(), 1),
            "commanded": ("on" if self._commanded else "off") if self._commanded is not None else "unknown",
            "changed_at": self._changed_at.isoformat() if self._changed_at else None,
            "override": (
                ("on" if self._override else "off") if self._override is not None else "auto"
            ),
            "greedy": self._greedy_enabled,
            "greedy_respects_schedule": self._greedy_respects_schedule,
            "note": self._note,
        }
