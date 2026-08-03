"""Pure decision logic for LoadEstimator (load_estimation.py), split out so it's testable
without the ``homeassistant`` package (unavailable in this dev container) — mirrors
override_expiry.py's split from deferrable_overrides.py. What this does NOT cover: the
HA-wired state-change listeners, timers, and Store I/O in load_estimation.py itself, which
need a running HA core — those are covered by live verification.

Sampling model: LoadEstimator watches an unmonitored device's control entity for an
off->on transition, takes a whole-house-load-power reading just before (``baseline_w``)
and a settled reading a few minutes after (``settled_w``, long enough for e.g. a
compressor to finish spinning up), and treats the delta as one observation of the
device's real draw. ``sample_delta_w``/``sample_is_plausible`` decide whether a given
observation is trustworthy enough to fold into the running estimate; ``ema_update`` folds
it in; ``integrate_kwh`` turns the current estimate into an accumulating energy total
while the device is on — together these back a synthetic "energy sensor" for a device
that has no real one.
"""
from __future__ import annotations

# Below this delta (W), a sample is indistinguishable from noise (another load in the
# house cycling, a sensor glitch) rather than this device switching on.
SAMPLE_FLOOR_W = 50.0

# EMA smoothing for the running power estimate. Seeded at the user's manual kW guess (see
# LoadEstimator) so early samples refine an already-reasonable number instead of climbing
# up from zero.
EMA_ALPHA = 0.3


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
