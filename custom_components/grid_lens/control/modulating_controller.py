"""ModulatingLoadController — actuates ONE *current-controlled* deferrable load (the
"type 2" load ``load_controller.py``'s docstring defers to): a device whose draw GridLens
sets continuously, via a ``number.*`` setpoint, rather than switching on and off.

The canonical case is an EV charger. Every HA charger integration worth supporting exposes
the same shape — a ``number.*`` carrying a charging-current limit — under a different name
(OCPP ``maximum_current``, Easee ``dynamic_charger_limit``, Wallbox
``maximum_charging_current``, Zaptec/go-e/openEVSE/Tesla/Sigenergy likewise). So this
controller is deliberately **not** an OCPP driver: it is told which number entity to write,
what its value means, and what the device's floor is. Any integration matching that shape
works with no code change here.

Why a separate controller rather than a mode on the on/off one:

* **The LP already solves for this.** ``def_i`` is a continuous 0..max_kw variable. The
  on/off controller quantises that away at a 50%-of-rated threshold, which for a 7 kW
  charger means "3.5 kW or nothing" — it throws away exactly the resolution that makes
  solar-following worth doing.
* **The plan tick is the wrong clock.** Following real PV means reacting to a cloud edge in
  tens of seconds, not on the 5-minute plan boundary. So there are two clocks: the inherited
  5-minute ``apply()`` still evaluates the plan and the greedy conditions (and owns all the
  observability that goes with them), while ``modulate()`` runs on the manager's 30-second
  fast loop and is the only thing that ever writes a setpoint. ``apply()`` in this subclass
  therefore *decides* and does not actuate.
* **The feasible set has a hole in it.** An EV must not be offered below ~6 A (the IEC 61851
  duty-cycle floor): commanding 3 A doesn't charge slowly, it makes the car refuse or fault.
  So the physically realisable set is ``{0} ∪ [min, max]``, not ``[0, max]``, and a
  sub-minimum LP allocation has to resolve to either "off" or "min" — never to itself. That
  snap is enforced *here* rather than as a semi-continuous MILP constraint in the optimiser:
  the LP would need a binary per device per slot (a real solve-time cost, on a model that
  already goes MILP only for conditional credits), and the controller has to own the
  decision anyway because it is the only layer that sees live surplus. ``min_kw`` is still
  plumbed through to the optimiser so that constraint can be switched on later.

Behaviours this file is careful about, in rough order of how expensive getting them wrong is:

* **Hysteresis around the floor.** Below the minimum, a load that is already running holds at
  the minimum until the target drops well clear of it (``_MIN_HOLD_FRACTION``), instead of
  dropping straight to 0. An EV that gets cut off can take 30+ seconds to re-handshake and
  some cars sulk far longer, so a session flapping off/on across a cloud edge costs far more
  charge than the few hundred watts of import that holding at 6 A might draw.
* **Write economy.** Every write here goes over the wire to real hardware — an OCPP
  ``SetChargingProfile``, or a cloud round-trip for Easee/Wallbox/Zaptec, some of which are
  rate-limited. So a change smaller than the deadband, or sooner than the minimum write
  interval, is skipped. Crossing the on/off boundary (or commanding 0) always writes
  immediately: that is a safety-relevant transition, not a trim.
* **Never raise, never force off.** Same two rules as the on/off controller. A failed write
  logs and returns; nothing in this file forces a load off on shutdown or a stale plan (the
  "leave as-is" deadman — see ``load_control_manager.py``).
* **Fail *open* on the plug sensor.** ``plugged_in()`` returns None, not False, whenever it
  can't confirm — an unconfigured or unavailable plug entity must never be the reason a car
  didn't charge overnight.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_MIN_CHARGE_CURRENT_A,
    DEFAULT_SUPPLY_VOLTAGE,
    MODULATING_UNPLUGGED_STATES,
)
from .load_controller import DeferrableLoadController

_LOGGER = logging.getLogger(__name__)

# While already delivering, hold at the minimum current until the target falls below this
# fraction of it. Sized so ordinary PV noise and the ±5% error in the assumed supply voltage
# can't produce a stop, while a genuine loss of surplus (dusk, a real cloud bank) still does.
_MIN_HOLD_FRACTION = 0.6

# Fallback quantisation step per setpoint unit, used when the number entity doesn't publish
# its own ``step``. Amps setpoints are integral on every charger integration surveyed; a
# 1 W / 0.001 kW step is effectively "don't quantise" for the power-setpoint case.
_DEFAULT_STEP = {"a": 1.0, "w": 1.0, "kw": 0.001}

# unit_of_measurement → our internal unit token. Anything unrecognised falls back to amps,
# which is what every charger integration surveyed publishes.
_UNIT_MAP = {"a": "a", "amp": "a", "amps": "a", "w": "w", "watt": "w", "kw": "kw"}


class ModulatingLoadController(DeferrableLoadController):
    """Continuous-power controller for one deferrable load with a ``number.*`` setpoint.

    Subclasses the on/off controller rather than sitting beside it so that manual override,
    the three Greedy Consumption toggles, the greedy observability fields and the debounce
    clock are inherited verbatim — and so ``LoadControlManager`` can hold both kinds in one
    ``controllers`` dict without branching on type at every call site.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        name: str,
        setpoint_entity_id: str,
        max_w: float,
        switch_entity_id: str = "",
        min_current_a: float = DEFAULT_MIN_CHARGE_CURRENT_A,
        phases: int = 0,
        voltage: float = 0.0,
        setpoint_unit: str = "",
        plug_entity_id: str = "",
        write_deadband_a: float = 0.5,
        min_write_interval_s: float = 20.0,
        **kwargs,
    ) -> None:
        # switch_entity_id is optional here (the common case is a setpoint alone — writing 0
        # amps stops delivery), but the parent's override/actuation bookkeeping is written
        # against it, so it is passed straight through and simply left empty when unused.
        super().__init__(
            hass, name=name, switch_entity_id=switch_entity_id, max_w=max_w, **kwargs
        )
        self.setpoint_entity_id = setpoint_entity_id
        self.plug_entity_id = plug_entity_id or ""

        # See DeferrableLoadController.join_key: a switchless charger (the common OCPP
        # shape) would otherwise publish an empty pairing key, and every such device on an
        # install would collide on it.
        self._join_key = switch_entity_id or setpoint_entity_id
        self.min_current_a = max(0.0, float(min_current_a or 0.0)) or DEFAULT_MIN_CHARGE_CURRENT_A
        self.voltage = float(voltage) if float(voltage or 0.0) > 0.0 else DEFAULT_SUPPLY_VOLTAGE
        self.write_deadband_a = max(0.0, float(write_deadband_a))
        self.min_write_interval_s = max(0.0, float(min_write_interval_s))

        # "" = infer from the entity's own unit_of_measurement the first time we can read it.
        # Resolution is deferred (not done here) because at construction time the charger
        # integration may not have published state yet — a startup-order race would otherwise
        # pin the amps fallback permanently on a watts-setpoint charger.
        self._configured_unit = (setpoint_unit or "").strip().lower()
        self._unit_cache: Optional[str] = self._configured_unit or None

        # 0 = auto-derive from max_w and the entity's own max (see _phase_count). Same
        # deferred-resolution reasoning as the unit above.
        self._configured_phases = int(phases or 0)
        self._phase_cache: Optional[int] = self._configured_phases or None
        self._phases_logged = False

        # User ceiling from number.*_max_current (see FEATURES.md §6). None = unrestricted;
        # the entity sets this on restore, so it is deliberately not defaulted to a value
        # here — "no entity yet" and "user chose the maximum" must stay distinguishable.
        self._current_cap_a: Optional[float] = None

        # Last value actually written, in SETPOINT units, plus when — the two inputs to the
        # write-economy check. None = nothing written yet, which forces the next write.
        self._last_setpoint: Optional[float] = None
        self._last_write_at: Optional[datetime] = None
        self._commanded_w = 0.0
        # Observability only (published by status()): which term produced the last commanded
        # figure. "plan" | "surplus" | "override" | "off".
        self._modulation_source = "off"
        # Last plan/greedy decision from the 5-minute apply(), kept so status() can explain a
        # commanded figure that the fast loop derived from live surplus rather than the plan.
        self._planned_w = 0.0
        self._want_on = False

    # ------------------------------------------------------------------ units & envelope
    def _native(self, attr: str) -> Optional[float]:
        """A numeric attribute (``min``/``max``/``step``) off the setpoint entity, or None.

        Everything that reads the entity's own envelope goes through here so that a charger
        integration which hasn't started yet degrades to "unknown" uniformly, rather than
        each caller inventing its own fallback."""
        st = self.hass.states.get(self.setpoint_entity_id)
        if st is None:
            return None
        try:
            val = st.attributes.get(attr)
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def _unit(self) -> str:
        """What the setpoint entity's value means: ``"a"`` | ``"w"`` | ``"kw"``.

        An explicit config value always wins. Otherwise the entity's own
        ``unit_of_measurement`` decides, and the answer is cached only once a real state has
        been read — an unreadable entity keeps returning the amps fallback *without* caching
        it, so the correct unit is still picked up when the integration comes up."""
        if self._unit_cache:
            return self._unit_cache
        st = self.hass.states.get(self.setpoint_entity_id)
        if st is None:
            return "a"
        raw = str(st.attributes.get("unit_of_measurement") or "").strip().lower()
        self._unit_cache = _UNIT_MAP.get(raw, "a")
        return self._unit_cache

    def _phase_count(self) -> int:
        """Phases the amps setpoint applies across (1..3).

        Auto-derivation exists because amps alone are ambiguous: a 7.4 kW single-phase
        charger and a 22 kW three-phase one both advertise 32 A, and only the device's
        configured max_kw tells them apart. Falls back to 1 (never 0 — that would make every
        conversion collapse to zero watts) and, like the unit, only caches once it has had a
        real entity read to work from."""
        if self._phase_cache:
            return self._phase_cache
        native_max = self._native("max")
        if not native_max or native_max <= 0 or self.max_w <= 0:
            return 1
        derived = int(round(self.max_w / (native_max * self.voltage)))
        derived = max(1, min(3, derived))
        self._phase_cache = derived
        if not self._phases_logged:
            self._phases_logged = True
            _LOGGER.info(
                "Modulating load %s: derived %d phase(s) from %.1f kW max over %.0f A @ %.0f V",
                self.name, derived, self.max_w / 1000.0, native_max, self.voltage,
            )
        return derived

    def _amps_to_w(self, amps: float) -> float:
        return max(0.0, float(amps)) * self.voltage * self._phase_count()

    def _w_to_amps(self, watts: float) -> float:
        denom = self.voltage * self._phase_count()
        return max(0.0, float(watts)) / denom if denom > 0 else 0.0

    def target_w_to_setpoint(self, target_w: float) -> float:
        """Convert watts to whatever the setpoint entity expects. Pure — no rounding, no
        clamping, no entity writes; ``_quantised_setpoint`` layers those on top."""
        unit = self._unit()
        if unit == "w":
            return max(0.0, float(target_w))
        if unit == "kw":
            return max(0.0, float(target_w)) / 1000.0
        return self._w_to_amps(target_w)

    def _setpoint_to_w(self, value: float) -> float:
        """Inverse of ``target_w_to_setpoint`` — used to read the hardware's own figure back."""
        unit = self._unit()
        if unit == "w":
            return max(0.0, float(value))
        if unit == "kw":
            return max(0.0, float(value)) * 1000.0
        return self._amps_to_w(value)

    @property
    def join_key(self) -> str:
        return self._join_key

    @property
    def min_w(self) -> float:
        """Lowest power this device can actually be given (the bottom of ``[min, max]``).

        For an amps setpoint that is the configured minimum current — the IEC 61851 floor by
        default. For a W/kW setpoint the device publishes its own floor as the number
        entity's ``min``, which is more authoritative than any amps figure we could infer;
        the amps-derived value is only the fallback for an entity that publishes no min."""
        unit = self._unit()
        if unit != "a":
            native_min = self._native("min")
            if native_min is not None and native_min > 0:
                return self._setpoint_to_w(native_min)
        return self._amps_to_w(self.min_current_a)

    @property
    def cap_w(self) -> float:
        """Highest power GridLens may command: the tightest of the device's configured
        max_kw, the user's max-current ceiling, and the setpoint entity's own native max.

        max_kw is only included when it is actually configured (>0) — an unset max_kw means
        "unknown", not "zero", and must not silently pin the device off; the entity's own
        max then carries the ceiling on its own."""
        caps: list[float] = []
        if self.max_w > 0:
            caps.append(self.max_w)
        if self._current_cap_a is not None:
            caps.append(self._amps_to_w(self._current_cap_a))
        native_max = self._native("max")
        if native_max is not None and native_max > 0:
            caps.append(self._setpoint_to_w(native_max))
        return min(caps) if caps else 0.0

    @property
    def native_max_a(self) -> float:
        """Hardware ceiling in amps — the upper bound for the user's max-current entity.

        Deliberately ignores ``_current_cap_a``: this is the bound *of* that control, so
        feeding it back in would let the ceiling ratchet itself downward."""
        native_max = self._native("max")
        if native_max is not None and native_max > 0:
            return native_max if self._unit() == "a" else self._w_to_amps(
                self._setpoint_to_w(native_max)
            )
        return self._w_to_amps(self.max_w)

    @property
    def current_cap_a(self) -> Optional[float]:
        return self._current_cap_a

    def set_current_cap_a(self, amps: Optional[float]) -> None:
        """User ceiling on the current GridLens may command (None = unrestricted).

        Takes effect on the next fast tick rather than writing immediately: the fast loop is
        at most ``MODULATION_INTERVAL_SECONDS`` away, and going through it keeps every
        setpoint write on one path with one set of write-economy rules."""
        self._current_cap_a = None if amps is None else max(0.0, float(amps))

    # ------------------------------------------------------------------ plug state
    def plugged_in(self) -> Optional[bool]:
        """True/False from the configured plug or charger-status entity, else None.

        None means "don't know" and every caller must treat it as "assume plugged" — GridLens
        never withholds charging because it couldn't confirm a plug. Note that HA's own
        ``unavailable`` is treated as unknown here even though it also appears in
        ``MODULATING_UNPLUGGED_STATES`` (where it stands for OCPP's *Unavailable*
        ChargePointStatus): the two are indistinguishable as strings, and reading an entity
        outage as "unplugged" would stop a charging session every time the charger
        integration blipped."""
        if not self.plug_entity_id:
            return None
        st = self.hass.states.get(self.plug_entity_id)
        if st is None:
            return None
        s = str(st.state).strip().lower()
        if s in ("", "none", "unknown", "unavailable"):
            return None
        return s not in MODULATING_UNPLUGGED_STATES

    # ------------------------------------------------------------------ state readback
    def _switch_state(self) -> Optional[bool]:
        """The optional companion on/off switch's state, or None if there isn't one / it's
        unreadable. Uses the parent's reader so a ``climate.*`` companion behaves the same
        here as it does for a type-1 load."""
        if not self.switch_entity_id:
            return None
        return super()._actual_state()

    def _actual_state(self) -> Optional[bool]:
        """Is the hardware currently delivering? Overrides the parent's switch read.

        "On" for a modulating load is a setpoint at or above the device's floor — a charger
        sitting at 0 A is off no matter what its relay says. A companion switch, when
        configured, can only veto (an off switch means off regardless of setpoint); an
        unreadable switch is ignored rather than treated as off, same fail-open discipline as
        the plug sensor."""
        st = self.hass.states.get(self.setpoint_entity_id)
        if st is None or str(st.state).lower() in ("unknown", "unavailable", "none", ""):
            return None
        try:
            value = float(st.state)
        except (TypeError, ValueError):
            return None
        if self._switch_state() is False:
            return False
        floor = self.min_w
        return value > 0.0 and (floor <= 0.0 or self._setpoint_to_w(value) + 1e-6 >= floor)

    # ------------------------------------------------------------------ greedy
    def _export_surplus_threshold_w(self) -> float:
        """Export (W) the house must already be spilling before greedy condition #2 fires.

        The parent's bar is the device's *full* draw, because a load that can only be fully
        on shouldn't switch on unless the whole draw is already covered. A modulating load
        has no such constraint — it can absorb any surplus down to its own floor — so the bar
        is the floor. This is the single hook that differs; ``_greedy_wants_on`` itself is
        inherited unchanged."""
        return self.min_w

    # ------------------------------------------------------------------ 5-minute tick
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
        battery_headroom_w: Optional[float] = None,
    ) -> None:
        """Evaluate the plan and the greedy conditions for this slot — and write nothing.

        The parent turns this decision straight into a switch write. Here it only records it:
        the actual setpoint is owned by ``modulate()`` on the 30-second loop, which blends
        this plan figure with live surplus. Splitting it this way keeps a single writer (so
        the deadband and min-write-interval are never bypassed) while leaving every greedy
        evaluation, and all of its observability, on the same clock and in the same code as
        the on/off controller's."""
        if self._override is not None:
            self._note = f"override_{'on' if self._override else 'off'}"
            # Mirror the parent exactly: greedy is not evaluated under an override, so its
            # published reason must be cleared rather than left stale on the UI.
            self._greedy_reason = None
            self._greedy_blocked = "override"
            self._greedy_free_kwh = None
            self._greedy_needed_kwh = None
            self._greedy_battery_headroom_w = None
            return

        greedy_on = self._greedy_wants_on(
            import_rate, export_rate, grid_power_w, schedule_allows,
            forecast_free_kwh, forecast_hours, battery_headroom_w,
        )
        self._planned_w = max(0.0, float(planned_w))
        self._want_on = greedy_on or self._planned_w > 0.0
        self._note = (
            f"plan_{self._planned_w:.0f}w{'_greedy' if greedy_on else ''}"
            if self._want_on else "plan_idle"
        )

    # ------------------------------------------------------------------ 30-second tick
    async def modulate(
        self, target_w: float, now: datetime, *, source: Optional[str] = None
    ) -> None:
        """Drive the setpoint toward ``target_w`` (watts at the appliance).

        ``source`` is observability only — the manager passes ``"surplus"`` when live export
        rather than the plan produced the figure, so the UI can distinguish "charging because
        the plan said so" from "charging because the roof is spilling". Optional, so a caller
        that only has a number to hand doesn't have to invent a label.
        """
        if self._override is not None:
            # A human has taken control; the one command that implements the override was
            # already issued by set_override(). Re-asserting it every 30 s would fight
            # whatever they do at the charger itself.
            return

        if self.plugged_in() is False:
            await self._write(0.0, now, source="off", reason="unplugged")
            return

        cap = self.cap_w
        floor = self.min_w
        want = max(0.0, float(target_w))
        if cap > 0.0:
            # cap_w == 0 means "no ceiling is knowable yet" (no max_kw configured and the
            # setpoint entity hasn't published its own max), NOT "zero watts allowed" —
            # clamping to it would silently pin the device off for as long as its integration
            # takes to come up. The entity's own min/max still bound the write itself.
            want = min(want, cap)

        if cap > 0.0 and floor > cap:
            # The user's ceiling (or the entity's own max) sits below the device's floor —
            # there is no feasible non-zero current. Command 0 rather than an amount the
            # hardware would refuse.
            commanded = 0.0
        elif want + 1e-6 < floor:
            # Below the floor: the feasible set is {0} ∪ [floor, cap], so this must resolve
            # one way or the other. Hold at the floor while already delivering (see the
            # module docstring on re-handshake cost); otherwise stay off.
            commanded = (
                floor
                if (self._commanded and floor > 0.0 and want >= floor * _MIN_HOLD_FRACTION)
                else 0.0
            )
        else:
            commanded = want

        if commanded <= 0.0:
            resolved = "off"
        else:
            resolved = source or "plan"
        await self._write(commanded, now, source=resolved)

    def _quantised_setpoint(self, commanded_w: float) -> float:
        """``commanded_w`` in setpoint units, snapped to the entity's ``step``.

        Rounds to nearest and then re-clamps into the feasible band, because rounding *down*
        at the floor would produce a value the device refuses (5 A on a 6 A minimum) and
        rounding *up* at the ceiling would exceed a limit the user or the hardware set."""
        if commanded_w <= 0.0:
            return 0.0
        unit = self._unit()
        step = self._native("step") or _DEFAULT_STEP.get(unit, 1.0)
        if step <= 0:
            step = _DEFAULT_STEP.get(unit, 1.0)
        value = round(self.target_w_to_setpoint(commanded_w) / step) * step
        lo = self.target_w_to_setpoint(self.min_w)
        cap = self.cap_w
        hi = self.target_w_to_setpoint(cap) if cap > 0 else value
        value = max(lo, min(hi, value)) if hi >= lo else lo
        # Trim binary float dust (0.30000000000000004 A) that a service call would otherwise
        # carry to the integration verbatim.
        return round(value, 6)

    def _deadband_in_setpoint_units(self) -> float:
        """``write_deadband_a`` expressed in whatever the setpoint speaks.

        The knob is defined in amps because that is the unit the user can reason about
        ("don't bother re-writing for less than half an amp"); for a W/kW setpoint the
        equivalent power delta is used so the same intent holds."""
        if self._unit() == "a":
            return self.write_deadband_a
        return self.target_w_to_setpoint(self._amps_to_w(self.write_deadband_a))

    async def _write(
        self, commanded_w: float, now: datetime, *, source: str, reason: str = ""
    ) -> None:
        """Apply write economy, then actuate. Never raises.

        A trim (same on/off state, small delta, or too soon since the last write) is skipped
        entirely. Crossing the on/off boundary — including the very first command, where
        ``_commanded`` is still None — always writes: those are the transitions that actually
        start or stop energy flowing, and delaying one to satisfy a rate limit is the wrong
        trade."""
        setpoint = self._quantised_setpoint(commanded_w)
        want_on = setpoint > 0.0
        crossing = self._commanded is None or want_on != bool(self._commanded)

        if not crossing:
            if (
                self._last_write_at is not None
                and (now - self._last_write_at).total_seconds() < self.min_write_interval_s
            ):
                self._note = f"hold_setpoint_rate_limit{'_' + reason if reason else ''}"
                return
            if (
                self._last_setpoint is not None
                and abs(setpoint - self._last_setpoint) < self._deadband_in_setpoint_units()
            ):
                self._note = f"hold_setpoint_deadband{'_' + reason if reason else ''}"
                return

        try:
            await self._write_setpoint(setpoint, want_on)
        except Exception as err:  # noqa: BLE001 — a failed write must never kill the loop
            _LOGGER.error(
                "Modulating load %s: setpoint write (%s = %s) failed: %s",
                self.name, self.setpoint_entity_id, setpoint, err,
            )
            self._note = f"setpoint_error:{err}"
            return

        if crossing:
            # Shares the parent's debounce clock so an inherited code path that reads
            # _changed_at (and the UI that publishes it) sees a real transition time.
            self._changed_at = now
            _LOGGER.info(
                "Modulating load %s → %s (%s = %s)%s",
                self.name, "on" if want_on else "off", self.setpoint_entity_id, setpoint,
                f" [{reason}]" if reason else f" [{source}]",
            )
        self._commanded = want_on
        self._commanded_w = commanded_w if want_on else 0.0
        self._last_setpoint = setpoint
        self._last_write_at = now
        self._modulation_source = source
        self._note = (
            f"setpoint_{setpoint:g}_{source}{'_' + reason if reason else ''}"
            if want_on else f"setpoint_off{'_' + reason if reason else ''}"
        )

    async def _write_setpoint(self, setpoint: float, want_on: bool) -> None:
        """The raw hardware write. Raises on failure — every caller wraps it.

        Ordering matters when a companion switch is configured: energise before ramping up,
        de-energise after commanding 0, so the device is never asked to deliver current
        through a relay that is still open (or left holding a stale non-zero limit after the
        relay opens)."""
        if want_on and self.switch_entity_id and self._switch_state() is not True:
            await self._switch(True)
        await self.hass.services.async_call(
            "number", "set_value",
            {"entity_id": self.setpoint_entity_id, "value": setpoint},
            blocking=True,
        )
        if not want_on and self.switch_entity_id and self._switch_state() is not False:
            await self._switch(False)

    async def _switch(self, on: bool) -> None:
        """Drive the optional companion on/off entity, reusing the parent's actuation so a
        ``climate.*`` companion goes through the same turn_on/set_hvac_mode fallback."""
        await super()._actuate(on)

    # ------------------------------------------------------------------ manual override
    def _force_on_target_w(self) -> float:
        """Power a Force On should command — "the maximum currently allowed", resolved
        against the same two edge cases ``modulate()`` handles and a bare ``cap_w`` read
        does not.

        * **Ceiling unknown** (``cap_w == 0``: no ``max_kw`` configured *and* the charger
          integration hasn't published its own max yet). Reading that as the literal ceiling
          makes Force On command 0 — a user pressing "On now" and getting nothing at all,
          which is the single worst outcome for a manual override. Fall back to the device's
          floor: charging at 6 A is a defensible answer where the hardware limit is unknown,
          and it is the one current every EV accepts.
        * **Ceiling below the floor** — a user cap of 3 A against a 6 A minimum. There is no
          feasible non-zero current, and clamping *up* to the floor would drive the charger
          past a limit the user explicitly set. Command 0, matching ``modulate()``.
        """
        cap = self.cap_w
        floor = max(0.0, self.min_w)
        if cap <= 0.0:
            return floor
        if floor > 0.0 and cap + 1e-6 < floor:
            _LOGGER.warning(
                "Force On for %s: ceiling %.0f W is below the %.0f W minimum charging "
                "current — no deliverable current, commanding off",
                self.name, cap, floor,
            )
            return 0.0
        return cap

    async def _actuate(self, want_on: bool) -> str:
        """Implements the parent's override path for a modulating device.

        Force On means "charge at the maximum currently allowed", not "close a relay" — the
        user's max-current ceiling and the hardware max still bind, they are the definition of
        "maximum allowed". Force Off commands 0. Routed through the parent's ``_command`` (via
        ``set_override``) so an override is logged, un-debounced and immediate, exactly like
        the on/off controller's."""
        target_w = self._force_on_target_w() if want_on else 0.0
        setpoint = self._quantised_setpoint(target_w)
        await self._write_setpoint(setpoint, setpoint > 0.0)
        self._commanded_w = target_w if setpoint > 0.0 else 0.0
        self._last_setpoint = setpoint
        self._last_write_at = dt_util.now()
        self._modulation_source = "override"
        return f"number.set_value({setpoint:g})"

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        st = super().status()
        st.update({
            "control_type": "modulating",
            "setpoint_entity": self.setpoint_entity_id,
            "setpoint_unit": self._unit(),
            "phases": self._phase_count(),
            "voltage": round(self.voltage, 1),
            "min_w": round(self.min_w, 1),
            "cap_w": round(self.cap_w, 1),
            "commanded_w": round(self._commanded_w, 1),
            "commanded_setpoint": self._last_setpoint,
            "max_current_a": (
                round(self._current_cap_a, 2) if self._current_cap_a is not None else None
            ),
            "plugged_in": self.plugged_in(),
            "last_write": self._last_write_at.isoformat() if self._last_write_at else None,
            "modulation_source": self._modulation_source,
            "planned_w": round(self._planned_w, 1),
        })
        return st
