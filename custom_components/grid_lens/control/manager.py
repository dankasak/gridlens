"""ControlManager — wires the Sigenergy driver → guardrail BatteryController → executor,
gated by the master enable switch AND a server-side entitlement check. Owns the
actuation lifecycle: enable() starts the loop, disable() (and HA shutdown) is the
deadman that restores native EMS.

Nothing here writes to the battery until enable() is called (master switch ON) AND the
account is entitled to battery control (checked periodically via the API, independent
of the plan-comparison subscription tier — see AdvisoryCoordinator._refresh_entitlement).
Entitlement defaults to False and stays False until the API says otherwise: a fresh
install (or a network blip before the first check completes) fails closed, not open.

User intent ("should control be running") is persisted here via a dedicated Store,
NOT via the switch entity's RestoreEntity state — see async_initialize()'s docstring
for why: a 2026-08-01 incident left control silently stuck off overnight because the
switch's restored "last state" landed on a transient off written by the reload deadman
below, not the user's actual last toggle.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import CONF_INVERTER_BRAND, CONF_INVERTER_TRANSPORT, DOMAIN
from ..inverters import get_inverter_controller
from .battery_controller import BatteryController, GuardrailConfig
from .executor import ScheduleExecutor

_LOGGER = logging.getLogger(__name__)

# Fallback for entries created before the inverter-selection config_flow step existed.
_DEFAULT_BRAND = "sigenergy"
_DEFAULT_TRANSPORT = "mqtt"

_INTENT_STORE_VERSION = 1


class ControlManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        d = entry.data

        brand = d.get(CONF_INVERTER_BRAND, _DEFAULT_BRAND)
        transport = d.get(CONF_INVERTER_TRANSPORT, _DEFAULT_TRANSPORT)
        driver = get_inverter_controller(hass, brand, transport)
        if driver is None:
            raise RuntimeError(f"No inverter driver for brand={brand!r} transport={transport!r}")
        cfg = GuardrailConfig(
            min_soc_pct=float(d.get("battery_min_soc", 10.0)),
            charge_cutoff_pct=float(d.get("battery_max_soc", 100.0)),
            max_charge_w=float(d.get("battery_max_charge_rate", 5.0)) * 1000.0,
            max_discharge_w=float(d.get("battery_max_discharge_rate", 5.0)) * 1000.0,
        )
        self.controller = BatteryController(hass, driver, cfg)
        self.executor = ScheduleExecutor(hass, self.controller, interval_minutes=5)
        self._enabled = False
        # Fail closed: no entitlement until the API confirms one. See module docstring.
        self._entitled = False
        # User/switch intent, independent of whether enable() actually succeeded — lets
        # set_entitled() auto-retry enable() once entitlement arrives without the switch
        # having to be toggled again. Seeded from _store by async_initialize(), not from
        # the switch entity's restored state (see module docstring).
        self._want_enabled = False
        # Optional sync callback the switch entity registers so it can refresh its
        # displayed state when `_enabled` changes from somewhere other than a direct
        # user toggle — e.g. set_entitled()'s auto-retry succeeding after a restart.
        self._on_change = None
        self._store = Store(hass, _INTENT_STORE_VERSION, f"{DOMAIN}_control_intent_{entry.entry_id}")

        # Timed override (see override_pause()/cancel_override()) — a temporary pause
        # that auto-resumes, distinct from _want_enabled/disable()'s indefinite off. Kept
        # as a separate field (not folded into _want_enabled) specifically so the
        # restart-safety story stays simple: _want_enabled alone still answers "should
        # control be running once nothing is overriding it", and _override_until alone
        # answers "is something temporarily suppressing that right now".
        self._override_until = None  # type: ignore[var-annotated]
        self._override_label: str | None = None
        self._override_cancel = None
        self._on_override_change = None
        # Default custom-duration knob for number.py's GridLensBatteryOverrideMinutesNumber
        # — a plain UI preference (restored via that entity's own RestoreEntity), not
        # safety-relevant, so it isn't persisted in this manager's Store.
        self.custom_override_minutes: float = 60.0

        # Deadman: restore native control if HA shuts down while control is active.
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_hass_stop)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def override_until(self):
        """The datetime the current pause auto-resumes at, or None if not paused."""
        return self._override_until

    @property
    def override_label(self) -> str | None:
        """The select option label that armed the current pause (e.g. "Pause 1 hour"),
        or None if not paused — see select.py's GridLensBatteryControlOverrideSelect."""
        return self._override_label

    async def async_initialize(self) -> None:
        """Restore user intent from the dedicated Store and (re-)enable if it was on.

        Deliberately NOT sourced from the switch entity's RestoreEntity state. That
        state is always transiently forced to "off" by async_prepare_for_reload()
        below on every single reload (the deadman must restore native EMS before
        teardown) — HA's RestoreStateData only dumps to disk periodically (or on a
        clean shutdown), so whether a *restart* reads back "on" or "off" for the
        switch depends on unrelated timing (did a periodic dump land during that
        transient off window?), not on what the user actually last chose. This bit
        production on 2026-08-01: two config-entry reloads landed close together
        during live testing, a dump happened to capture the transient "off", and
        control silently stayed off for ~6 hours with no error logged, until manually
        re-enabled. Persisting intent explicitly here, updated only by genuine calls
        to enable()/disable() (never by the reload deadman), removes the dependency
        on that timing entirely.

        Called once from async_setup_entry, before the switch platform is forwarded,
        so the switch's own initial state already reflects the outcome.
        """
        try:
            stored = await self._store.async_load()
        except Exception as exc:  # noqa: BLE001 — a corrupt store must not crash setup
            _LOGGER.warning("Could not load battery control intent (%s) — defaulting off", exc)
            return
        # No prior data (fresh install) defaults to wanting control on, matching the
        # switch's long-standing "default ON, an HA restart doesn't silently stop
        # optimising" behaviour. A genuinely stored False is honoured as-is.
        want_on = True if stored is None else bool(stored.get("want_enabled", True))
        self._want_enabled = want_on

        # A timed override (override_pause()) in progress when HA went down. Restore it
        # the same way as any other persisted intent — NOT by falling back to want_on
        # above, which would re-engage control early and defeat the whole point of the
        # pause. If the pause had already expired while HA was offline, fall through to
        # the normal want_on handling below instead of staying paused forever — the
        # exact failure mode this Store exists to avoid (see module docstring).
        until_iso = stored.get("override_until") if stored else None
        if until_iso:
            until = dt_util.parse_datetime(until_iso)
            now = dt_util.utcnow()
            if until is not None and until > now:
                self._override_until = until
                self._override_label = stored.get("override_label")
                self._arm_override_timer((until - now).total_seconds())
                _LOGGER.warning(
                    "GridLens battery control override active until %s — staying "
                    "paused across restart", until.isoformat(),
                )
                return
            # Expired (or unparsable) while HA was down — clear it before proceeding.
            await self._persist_intent()

        if want_on:
            await self.enable()

    def set_override_listener(self, callback_) -> None:
        """Registered by select.py's override entity; called (no args) whenever the
        override state changes (armed, cancelled, or auto-expired) from anywhere,
        same pattern as set_state_listener() for the master switch."""
        self._on_override_change = callback_

    async def override_pause(self, duration_minutes: float, label: str | None = None) -> bool:
        """Suspend actuation for `duration_minutes`, then resume automatically if the
        user's underlying intent (_want_enabled) is still on. Deliberately does NOT touch
        _want_enabled — see the _override_until field comment in __init__ for why that
        matters across a crash/restart. `label` (e.g. "Pause 1 hour") is persisted purely
        so the override select can redisplay the right option across a restart; it has no
        effect on the actual pause/resume behaviour."""
        duration_minutes = max(1.0, float(duration_minutes))
        self._cancel_override_timer()
        self._override_until = dt_util.utcnow() + timedelta(minutes=duration_minutes)
        self._override_label = label
        await self._persist_intent()
        await self._stop(restore_normal=True)
        self._arm_override_timer(duration_minutes * 60.0)
        _LOGGER.warning(
            "GridLens battery control paused until %s (user override)",
            self._override_until.isoformat(),
        )
        self._notify_override_change()
        return True

    async def cancel_override(self) -> bool:
        """Resume now — clears a pending pause and re-enables immediately if the user's
        underlying intent is still on. A no-op if nothing is currently paused."""
        if self._override_until is None:
            return False
        self._cancel_override_timer()
        self._override_until = None
        self._override_label = None
        await self._persist_intent()
        if self._want_enabled:
            await self.enable()
        self._notify_override_change()
        return True

    def _arm_override_timer(self, delay_s: float) -> None:
        self._override_cancel = async_call_later(self.hass, max(1.0, delay_s), self._on_override_expiry)

    def _cancel_override_timer(self) -> None:
        if self._override_cancel is not None:
            self._override_cancel()
            self._override_cancel = None

    @callback
    def _on_override_expiry(self, _now) -> None:
        self._override_cancel = None
        self._override_until = None
        self._override_label = None
        self.hass.async_create_task(self._resume_after_override())

    async def _resume_after_override(self) -> None:
        await self._persist_intent()
        if self._want_enabled:
            _LOGGER.warning("GridLens battery control override expired — resuming")
            await self.enable()
        self._notify_override_change()

    def _notify_override_change(self) -> None:
        if self._on_override_change:
            self._on_override_change()

    async def _persist_intent(self) -> None:
        await self._store.async_save({
            "want_enabled": self._want_enabled,
            "override_until": self._override_until.isoformat() if self._override_until else None,
            "override_label": self._override_label,
        })

    async def async_push_safety_floor(self) -> bool:
        """Best-effort push of the hardware discharge floor. Deliberately independent
        of entitlement and of whether control is enabled: this bounds how low ANY
        discharge can go — including the inverter's own native/VPP logic, not just a
        Grid Lens-issued force_discharge — so it's a pure safety configuration, not a
        monetized actuation, and should land for every has_battery account regardless
        of battery_control entitlement."""
        return await self.controller.async_push_safety_floor()

    def set_state_listener(self, callback) -> None:
        """Registered by the switch entity; called (no args) whenever `_enabled` changes."""
        self._on_change = callback

    def set_plan(self, plan) -> None:
        """Fresh advisory plan (list[DispatchInterval]) — the executor acts on it only
        while enabled, but we keep it current so enable() acts immediately."""
        self.executor.set_plan(plan)

    async def set_entitled(self, entitled: bool) -> None:
        """Called periodically (via the advisory coordinator) with this account's current
        battery-control entitlement from the API. Revoking it while active force-disables
        immediately (deadman); granting it while the user wants control on auto-enables —
        no need to re-toggle the switch once entitlement lands after a restart."""
        was_entitled = self._entitled
        self._entitled = entitled
        if not entitled and self._enabled:
            _LOGGER.warning("GridLens battery control entitlement revoked — disabling")
            await self._stop(restore_normal=True)
        elif entitled and not was_entitled and self._want_enabled and not self._enabled:
            await self.enable()

    async def enable(self) -> bool:
        self._want_enabled = True
        # An explicit enable (switch ON, or a pause expiring) always wins over a
        # pending timed pause — clear it rather than leaving a stale timer that would
        # otherwise fire later and find nothing to do.
        override_was_active = self._override_until is not None
        self._cancel_override_timer()
        self._override_until = None
        self._override_label = None
        await self._persist_intent()
        if override_was_active:
            self._notify_override_change()
        if self._enabled:
            return True
        if not self._entitled:
            _LOGGER.warning(
                "GridLens battery control requested but this account isn't entitled — refusing"
            )
            return False
        _LOGGER.warning("GridLens battery control ENABLED — actuating the battery per plan")
        self._enabled = True
        await self.executor.start()
        if self._on_change:
            self._on_change()
        return True

    async def disable(self) -> None:
        """Genuine user intent to stop control (switch turned off). Persists that
        intent — unlike async_prepare_for_reload() below, which must NOT persist
        anything since it fires on every reload regardless of user intent."""
        self._want_enabled = False
        # An explicit disable also cancels any pending timed pause — it's already
        # redundant (control is going off indefinitely either way), and leaving it
        # armed would otherwise fire later and silently flip control back on, undoing
        # the user's actual intent here.
        override_was_active = self._override_until is not None
        self._cancel_override_timer()
        self._override_until = None
        self._override_label = None
        await self._persist_intent()
        if override_was_active:
            self._notify_override_change()
        await self._stop(restore_normal=True)

    async def async_prepare_for_reload(self) -> None:
        """Deadman called from async_unload_entry before every config-entry reload —
        restores native EMS so the inverter isn't left mid-actuation while GridLens is
        offline. Deliberately bypasses disable(): this is a lifecycle safety action,
        not user intent, so it must NOT touch _want_enabled or the persisted Store (see
        async_initialize()'s docstring for the incident this caused when it did)."""
        await self._stop(restore_normal=True)

    async def _stop(self, restore_normal: bool) -> None:
        if not self._enabled:
            return
        _LOGGER.warning("GridLens battery control DISABLED — restoring native EMS")
        self._enabled = False
        await self.executor.stop(restore_normal=restore_normal)
        if self._on_change:
            self._on_change()

    async def _on_hass_stop(self, _event) -> None:
        if self._enabled:
            _LOGGER.warning("HA stopping with control active — deadman: restoring native EMS")
            await self.executor.stop(restore_normal=True)

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "entitled": self._entitled,
            "override_until": self._override_until.isoformat() if self._override_until else None,
            "override_label": self._override_label,
            **self.executor.status(),
            **self.controller.status(),
        }
