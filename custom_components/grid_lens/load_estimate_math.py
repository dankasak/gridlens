"""Pure decision logic for LoadEstimator (load_estimation.py), split out so it's testable
without the ``homeassistant`` package (unavailable in this dev container) — mirrors
override_expiry.py's split from deferrable_overrides.py. What this does NOT cover: the
HA-wired state-change listeners, timers, and Store I/O in load_estimation.py itself, which
need a running HA core — those are covered by live verification.

Two independent sampling models feed the same accept/EMA pipeline:

1. **House-load sampling** (the original model): watches an unmonitored device's control
   entity for an off->on transition, takes a whole-house-load-power reading just before
   (``baseline_w``) and a settled reading a few minutes after (``settled_w``, long enough
   for e.g. a compressor to finish spinning up), and treats the delta as one observation
   of the device's real draw. Vulnerable to contamination from any *other* load's power
   swinging during the same window — a second HVAC unit cycling its own compressor, in
   particular, since that never shows up as a discrete state change on a control entity
   the contamination guard could watch.
2. **Own-meter sampling** (``energy_sample_avg_w``): for a device that has a real but
   coarse/non-real-time cumulative energy sensor (e.g. an ECHONET Lite aircon's
   ``measured_cumulative_power_consumption`` — updates infrequently, but it's the
   device's *own* meter), reads that sensor's value at the on->off boundary of one full
   on-period and divides the rise by the elapsed time. Immune to any other device's power
   draw by construction — it never touches the whole-house sensor at all — so it's
   preferred over house-load sampling whenever a real energy sensor is configured.

``sample_delta_w``/``sample_is_plausible`` decide whether a given house-load observation
is trustworthy enough to fold into the running estimate; ``energy_sample_avg_w`` does the
equivalent job for an own-meter observation; ``ema_update`` folds an accepted sample of
either kind into the estimate; ``integrate_kwh`` turns the current estimate into an
accumulating energy total while the device is on — together these back a synthetic
"energy sensor" for a device that has no real one (or a live "power" reading for a device
whose only real sensor is a cumulative, not instantaneous, one).
"""
from __future__ import annotations

from typing import Optional

# Below this delta (W), a sample is indistinguishable from noise (another load in the
# house cycling, a sensor glitch) rather than this device switching on.
SAMPLE_FLOOR_W = 50.0

# EMA smoothing for the running power estimate. Seeded at the user's manual kW guess (see
# LoadEstimator) so early samples refine an already-reasonable number instead of climbing
# up from zero.
EMA_ALPHA = 0.3

# Minimum on-duration (hours) before an own-meter sample is trusted — mirrors
# LoadEstimator.SETTLE_SECONDS (180s). Below this, a coarse cumulative counter's reporting
# lag/resolution can make "rise / elapsed" meaningless rather than just noisy (e.g. a
# quantization rounding a single Wh tick into an implausible instantaneous rate).
MIN_ENERGY_SAMPLE_HOURS = 180.0 / 3600.0


def sample_delta_w(baseline_w: float, settled_w: float) -> float:
    """The step change attributed to the device switching on."""
    return settled_w - baseline_w


def sample_ceiling_w(seed_kw: float) -> float:
    """Upper plausibility bound for a sample delta: a generous multiple (4x) of the
    user's own seed estimate, floored at 6000W so a low/zero seed doesn't reject every
    real sample outright before any data has refined it."""
    return max(max(0.0, seed_kw) * 1000.0 * 4.0, 6000.0)


def sample_is_plausible(delta_w: float, seed_kw: float, floor_w: float = SAMPLE_FLOOR_W) -> bool:
    """Whether a sample delta is trustworthy enough to fold into the estimate — guards
    against attributing unrelated household noise (or a sensor glitch) to this device."""
    return floor_w <= delta_w <= max(sample_ceiling_w(seed_kw), floor_w)


def energy_sample_reject_reason(delta_kwh: float, elapsed_hours: float) -> Optional[str]:
    """Why `energy_sample_avg_w` would refuse to trust this observation, distinguishing
    the two very different causes a caller (LoadEstimator) wants to log/record
    separately: an on-period that just hasn't run long enough yet to trust the coarse
    counter's resolution, versus the counter itself having gone backwards (a device
    reboot resetting it, or a rollover) — the same delta/elapsed inputs, but one is
    "wait longer" and the other is "the meter lied". None if the sample is trustworthy."""
    if delta_kwh < 0:
        return "counter_reset"
    if elapsed_hours < MIN_ENERGY_SAMPLE_HOURS:
        return "too_short"
    return None


def energy_sample_avg_w(delta_kwh: float, elapsed_hours: float) -> Optional[float]:
    """Average power implied by a cumulative energy sensor's rise over one full on-period.
    None (rather than 0) for a duration too short to trust, or a negative delta — a
    counter reset/rollover on the device itself, not a real reading of near-zero draw."""
    if elapsed_hours < MIN_ENERGY_SAMPLE_HOURS or delta_kwh < 0:
        return None
    return (delta_kwh * 1000.0) / elapsed_hours


def ema_update(current_w: float, sample_w: float, alpha: float = EMA_ALPHA) -> float:
    """Fold one accepted sample into the running power estimate."""
    return (1.0 - alpha) * current_w + alpha * sample_w


def integrate_kwh(running_kwh: float, power_w: float, elapsed_hours: float) -> float:
    """Add one tick's worth of energy to the running total, using whatever the current
    best power estimate is (manual seed until enough samples exist, refined after).
    Never subtracts (a negative elapsed span, e.g. a clock going backwards, is a no-op)."""
    if elapsed_hours <= 0.0 or power_w <= 0.0:
        return running_kwh
    return running_kwh + (power_w / 1000.0) * elapsed_hours
