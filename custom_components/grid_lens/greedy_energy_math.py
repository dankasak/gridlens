"""Pure accumulation logic for GreedyEnergyTracker (greedy_energy.py), split out so it's
testable without the ``homeassistant`` package — mirrors load_estimate_math.py's split
from load_estimation.py.

The tracker watches a deferrable device's own energy sensor and, on every state change,
attributes the reading's rise since the last change to "greedy" energy whenever the
device's controller currently reports a non-None ``greedy_reason`` (Greedy Consumption
firing — see control/load_controller.py). This is a coarse, event-driven approximation:
attribution is all-or-nothing per delta, decided by whatever `greedy_reason` reads at the
moment the new reading arrives, not integrated continuously against exactly when greedy
started/stopped within the interval. Good enough for the ~5-minute granularity Greedy
Consumption itself operates at (see load_control_manager.py's tick), and applied uniformly
to on/off and modulating controllers alike — a modulating device's surplus-boosted current
is over-attributed as fully "greedy" rather than split from its plan-driven baseline, a
deliberate simplification rather than an attempt at a precise blend.
"""
from __future__ import annotations

from typing import Optional


def accumulate(
    last_value: Optional[float], new_value: float, was_greedy: bool
) -> tuple[float, bool]:
    """Returns (kwh_to_add, counter_reset).

    ``last_value`` is the source sensor's previous reading (kWh), or None on the very
    first observation — that call only establishes a baseline and contributes nothing,
    since there is no prior reading to diff against.

    A negative delta means the source counter went backwards (a device reboot resetting
    its own energy sensor, the same failure mode load_estimate_math.energy_sample_avg_w
    guards against) — never subtracted from the running total; the caller resyncs its
    baseline to ``new_value`` regardless so the next call measures forward from here.
    """
    if last_value is None:
        return 0.0, False
    delta = new_value - last_value
    if delta < 0:
        return 0.0, True
    return (delta if was_greedy else 0.0), False
