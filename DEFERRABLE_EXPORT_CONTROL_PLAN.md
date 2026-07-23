# Export Price Floor, Per-Day Deferrable Overrides, and Deferrable Load Control — Plan

Status: **planning only, no code written yet.** Written 2026-07-23, grounded in the current
codebase (`custom_components/grid_lens/`) via direct file/line reads, not guesses. Read
`GRIDLENS_CHECKLIST.md` first for project state; this doc is the spec for three new features
requested 2026-07-23. **All four open product decisions below are now settled** (user, 2026-07-23)
— see "Product decisions" section; the rest of the doc reflects them.

1. A configurable **minimum export price** — below it, the optimizer stops treating export as
   valuable and prefers routing surplus into deferrable loads (motivating case: the optimizer
   sold at 2c/kWh instead of using that energy to charge the EV).
2. A **per-day override** of a deferrable load's target (kWh or hours) — e.g. "charge the EV
   more today, I'm driving far" — that beats the historical-average target for one calendar day
   only, then reverts automatically.
3. **Actual device control** for deferrable loads — today they're only modeled in the LP for
   forecasting; nothing turns a real EV charger or pool pump on/off. Add a configurable switch
   entity per deferrable load and drive it from the optimized schedule, mirroring the existing
   battery-control architecture (`control/manager.py`, `control/executor.py`).

Ordered by risk/complexity, cheapest and safest first: **Feature 1 → Feature 2 → Feature 3**.
Feature 3 is materially bigger and riskier (real hardware writes) than the other two combined —
size the implementation session(s) accordingly, and consider shipping it in its own advisory
(observability-only) sub-phase before wiring actual switch calls, exactly as the project already
did for battery control (`advisory/` shipped and was live-verified for ~2 weeks before
`control/` started writing to hardware).

---

## Feature 1 — Minimum export price floor

### Design

The LP's objective already prices export as `Σ r_exp[t]·exp[t]` with no floor
(`battery_optimizer.py:369`), and already has two precedents for exactly this kind of knob:
`export_penalty`/`soc_reward` (soft, additive nudges to `c_obj`) and `no_grid_charge` (hard
constraint row). **Use the soft pattern, not a hard constraint.** A hard `exp[t] ≤ 0` below the
floor risks infeasibility on days where nothing else can absorb the surplus (battery full,
deferrable daily targets already met) — the energy-balance equality
(`imp[t] + dis[t] − exp[t] − cha[t] − Σdef_i[t] = load[t] − solar[t]`, `battery_optimizer.py:445-458`)
has no curtailment variable, so `exp[t]` is already the sink of last resort. Flooring the
**price signal** to zero for slots below the threshold keeps export legal but valueless, so the
LP naturally prefers `def_i[t]` (deferrable) or `cha[t]` (battery) whenever they have headroom,
and still exports the physically-forced residual when they don't. This is a pure economic nudge
— no new infeasibility modes.

### Changes

- `const.py`: new `CONF_MIN_EXPORT_PRICE = "min_export_price"`, alongside `CONF_BATTERY_MIN_SOC`
  (`const.py:140-141`).
- `config_flow.py`: new `NumberSelector` in `_battery_schema` (`config_flow.py:529-557`),
  following the exact pattern at `:551-553` — recommend units of **c/kWh** to match how the user
  described "2c/kWh" (convert to $/kWh internally next to the existing rate math, which is in
  dollars). `min=0, max=~50, step=0.1`. Add to both the initial config flow and the options flow
  (same duplication pattern as `CONF_HAS_DEMAND_TARIFF`, `config_flow.py:448` / `:826-827`).
- `battery_optimizer.py`: new optional param `min_export_price: float | None = None` threaded
  `optimize_hourly_schedule` (`:82-100`) → `_lp_optimize` (`:224-228`) → `_lp_scipy`
  (`:275-279`), applied where `c_obj[X:X+T]` is built (`:369`):
  `effective_r_exp[t] = 0.0 if r_exp[t] < min_export_price else r_exp[t]`. Default `None` = today's
  byte-identical behavior everywhere else (matches how every other optional param in this file
  behaves). Add to the existing solver-path gate (`import_caps`/`export_caps`/`demand_active`,
  `battery_optimizer.py` gate site) so setting it forces the scipy path — consistent with every
  other "extra feature" in this file (HiGHS is broken anyway, per `GRIDLENS_CHECKLIST.md`'s
  Known Issues).
- `plan_calculator.py`: read `CONF_MIN_EXPORT_PRICE` off `entry.data` alongside
  `CONF_BATTERY_MIN_SOC`/`CONF_BATTERY_MAX_SOC` (`:82-83`), pass into the
  `optimize_hourly_schedule` call at `:2276`.
- `advisory/planner.py` / `advisory/coordinator.py`: same config read, passed into
  `AdvisoryPlanner.plan()`'s call to `optimize_hourly_schedule`.

Apply it in **both** the live control path and the plan-comparison path — it's a household
preference independent of which retailer plan is being evaluated, so "what would I save on plan
X" comparisons should reflect it too.

### Verification

Same pattern as every other LP change in this codebase (this dev container has no scipy): scp
`battery_optimizer.py` + a small test script to the LXC, run under
`docker run --rm python:3.12-slim` with `pip install scipy`, using a synthetic
high-solar/low-price scenario (mirrors `scratchpad/lp_degen_test.py` from the 2026-07-14
degeneracy fix). Confirm: (a) below-floor slots route surplus to `def_i` when the deferrable
daily target isn't met, (b) `min_export_price=None` reproduces the exact prior schedule
(regression check against an existing fixture), (c) no infeasibility when both battery and
deferrable are saturated on a big-solar day.

### Risk: low

Small, well-precedented diff; no hardware writes; default-off behavior is provably unchanged.

---

## Feature 2 — Per-day deferrable load override

### Design

Today a device's `daily_kwh` target is a straight 14-day historical average
(`PlanCalculator._get_deferrable_data`, `plan_calculator.py:1331-1425`, specifically the mean at
`:1398`), shared by both the plan-comparison path and the live advisory/control path
(`AdvisoryCoordinator._deferrable_device_params`, `advisory/coordinator.py:176-206`). There's no
existing entity or service for a user-set override — no `number.py`/`select.py` platform exists
in this component at all (only `sensor.py` and one global `switch.py`).

**Recommend a `number.py` platform**, one entity per configured deferrable device (e.g.
`number.grid_lens_ev_charger_today_boost_kwh`), over a service call — a service is cheaper to
build but the stated use case ("I'm driving far today") is exactly the kind of occasional,
dashboard-driven adjustment HA's `number` domain is designed for, and it composes for free with
existing dashboards/automations (e.g. a user could pre-set it via an automation the night
before a planned trip). Default `0` = no override, fall back to the historical average.
Unit is **kWh** (decided 2026-07-23) — a direct drop-in for the LP's existing `daily_kwh` field,
no hours→kWh conversion or rounding edge cases.

**Persistence**: use HA's `Store` helper (`homeassistant.helpers.storage.Store`), keyed by
device `sensor_id`, storing `{value_kwh, set_date}`. On every read (both by the entity on
`async_added_to_hass` and by `_get_deferrable_data`/`_deferrable_for_horizon`), compare
`set_date` to today's local date and treat as expired (revert to 0/historical) if it doesn't
match. This is simpler and more robust than a midnight `async_track_time_change` reset — no
timer to miss, survives restarts correctly, and self-heals if a tick is missed.

**Scope to the live control path only** (`AdvisoryCoordinator`), not plan-comparison — the
override is inherently a "right now" adjustment, not a historical what-if.

**Horizon simplification (flagged, not hidden):** the LP prorates `daily_kwh` per calendar day
across the whole horizon (`battery_optimizer.py:472-487`), applying one scalar target to every
day in the window. Giving the override true single-calendar-day granularity within a multi-day
horizon would mean extending that proration logic to accept a per-day-index target array —
real LP surgery, non-trivial. Given the advisory horizon is only 36h (mostly one full day), the
pragmatic v1 scope is: **the override scalar replaces the historical average for the entire
active horizon**, not just "today" precisely. Because the override auto-expires at local
midnight (via the `set_date` check above), by the time it's genuinely tomorrow the LP will
already be back to the historical average on its next 2-minute refresh — the only imprecision is
a few hours of slight over-application into the tail of "today" if the horizon's last day is a
partial day, which is a minor, documented, low-consequence edge case. Revisit true per-calendar-
day granularity only if this proves to matter in practice.

### Changes

- `helpers/storage.py` usage: new small module, e.g. `deferrable_overrides.py`, wrapping a
  `Store(hass, version=1, key=f"{DOMAIN}_deferrable_overrides")` with `get(sensor_id)` /
  `set(sensor_id, value_kwh)` returning `0.0` for missing/expired entries.
- `number.py` (new platform): `GridLensDeferrableOverrideNumber` per device in
  `entry.data[CONF_DEFERRABLE_LOAD_SENSORS]` (`const.py:148`); `native_min_value=0`,
  `native_max_value` derived from `max_kw * 24` for that device, `native_step=0.5`, unit kWh.
  `async_set_native_value` writes through the Store helper above.
- `advisory/coordinator.py`: `_deferrable_device_params` (`:176-206`) checks the Store after
  computing the historical `daily_kwh` and substitutes the override when present/unexpired.
- `strings.json` + `translations/en.json`: entity name strings (remember: both files, they've
  drifted before per the Known Issues in the checklist).

### Verification

Offline unit test for the Store expiry logic (pure Python, no HA/scipy needed — can run in this
container unlike the LP tests). Live test: set an override via Developer Tools → set a
`number.*` value, confirm the next advisory tick's `sensor...planned_dispatch` shows the boosted
`defer_N` energy for the device; wait past local midnight (or fake the date in a targeted test)
and confirm it reverts.

### Risk: low–medium

New entity platform is new surface area but low-consequence (worst case: a stale/wrong forecast
number, not a hardware fault) since nothing in this feature writes to hardware.

---

## Feature 3 — Deferrable load switch control

### Design

This is the only one of the three that writes to real hardware, so it should follow the
project's own established risk-reduction pattern: **advisory (observability, no writes) before
control (writes)** — exactly how battery control was staged (`advisory/` live-verified for
~2 weeks, per checklist, before `control/` started actuating).

**Phase 3a — observability only.** The LP already produces per-device on/off-ish signal
(`step["deferrable_per_device"]` in `battery_optimizer.py`, surfaced as `defer_0`/`defer_1`/...
in the trajectory, `advisory/planner.py:133-137`) and the advisory card already plots it
per-device. The only gap for "observability" is confirming the card clearly shows *recommended
on/off*, not just kWh — likely already sufficient; audit before building anything new.

**Phase 3b — actual control**, once 3a's data is trusted:

- **Config**: extend the existing `device_power` step (`config_flow.py:329-374` and its options-
  flow duplicate) with an optional per-device target-switch `EntitySelector` (filtered to the
  `switch` domain), parallel-list `CONF_DEFERRABLE_LOAD_SWITCHES` in `const.py` next to
  `CONF_DEFERRABLE_LOAD_HOURS` (`const.py:148-150`). Optional — a device with no switch
  configured stays forecast-only, exactly like today.
- **`DispatchInterval`** (`control/executor.py:32-78`) gains a `deferrable_w: list[float]`
  field, populated in `AdvisoryPlanner.plan()` (`advisory/planner.py:100-109`) from
  `step.get("deferrable_per_device")` — currently this data only reaches the trajectory (display),
  never the `DispatchInterval` list (control input). This is the one real code gap connecting
  "the LP already knows what it wants" to "the executor could act on it."
- **New `control/load_controller.py`**: a small `DeferrableLoadController` per device — *not* a
  reuse of `BatteryController` (that's SOC-guardrail-specific and inverter-HAL-coupled). Wraps a
  target `switch.*` entity_id, exposes `turn_on()/turn_off()` via
  `hass.services.async_call("switch", "turn_on"/"turn_off", {"entity_id": ...}, blocking=True)`
  wrapped in try/except that logs and returns `False` rather than raising — mirrors
  `inverters/sigenergy_mqtt.py`'s `_switch()` helper (`:255-258`) exactly, generalized to any
  `switch.*` entity rather than a Sigenergy-specific one. Add: a transition-economy no-op guard
  (skip redundant `turn_on`/`turn_off` calls when state is unchanged — mirrors
  `executor.py:172-178`). **No forced-state auto-expiry timer** — the battery's
  `_arm_expiry`/`_on_expiry` pattern (`battery_controller.py:226-244`) exists to auto-revert a
  missed-tick battery mode, but the "leave as-is on stale plan / HA stop" decision above means
  loads deliberately have no auto-off path anywhere; a missed tick just means the load stays in
  its last commanded state until the next successful tick.
- **New `control/load_control_manager.py`** (`LoadControlManager`), deliberately **decoupled
  from `ControlManager`/inverter HAL** (decided 2026-07-23 — kept separate) — deferrable-load
  control has zero brand-specific logic (any `switch.*` entity works the same way), and should
  work for households without a battery at all. Costs a second `async_track_time_change` timer
  running alongside the executor's, accepted as the price of not coupling load control to
  `has_battery`/inverter config. Holds a dict of `DeferrableLoadController`s, ticks on that
  cadence, reads `deferrable_w[i] > 0 → on` per device each tick from the current
  `DispatchInterval`.
- **Debounce**: unlike the battery (which can change mode every 5 min cheaply), physical loads
  like an EV charger or pool pump have real switching costs/wear. Add a minimum-run/minimum-off
  duration per device (config or a sane hardcoded default, e.g. 15 min) so a borderline LP signal
  doesn't chatter the switch.
- **Switch platform**: one `GridLensDeferrableLoadSwitch` per device with a configured target
  switch (`switch.py`), mirroring `GridLensBatteryControlSwitch`'s `RestoreEntity` structure
  (`switch.py:27-83`) but — flagged as an explicit product decision, not assumed — **default OFF**
  on a fresh install/no prior state, unlike the battery switch's default-ON. Physically switching
  an EV charger or pool pump has more direct real-world consequence than a battery mode change;
  starting opt-in is the safer default.

### Safety patterns to carry over (all already precedented in `control/`)

| Pattern | Existing precedent | Applies here as |
|---|---|---|
| HA-stop deadman | `manager.py:64,121-124` | **leave loads as-is** (decided 2026-07-23 — do not force off) on `EVENT_HOMEASSISTANT_STOP`; this is a deliberate divergence from the battery's `restore_normal` deadman, since forcing a real EV/pool-pump load off has more direct real-world consequence than reverting an inverter mode, and the user chose not to risk cutting off a session mid-way on an HA restart. Log the shutdown so it's visible, but don't act on the switch. |
| Stale-plan watchdog | `executor.py:212-215`, `_plan_is_stale` (30 min) | same threshold; degrade to "leave as-is, log warning" until a fresh plan arrives — consistent with the deadman choice above, no forced-off path anywhere in load control |
| Forced-mode auto-expiry | `battery_controller.py:226-244` | **not used** for loads (decided 2026-07-23) — see the "leave as-is" reasoning above; a missed tick leaves the load in its last state rather than auto-reverting |
| Fail-closed entitlement | `manager.py:53`, `_entitled=False` default | gates on the existing `battery_control` entitlement (decided 2026-07-23 — reused, not a new column); defaults closed the same way |
| Transition economy | `executor.py:172-178` | skip redundant `turn_on`/`turn_off` when unchanged |
| Never raise from a command write | `sigenergy_mqtt.py:269-271` | same try/except-log-return-False shape |

Note on reusing `battery_control` for entitlement: since `LoadControlManager` is deliberately
decoupled from `has_battery`/inverter config, a no-battery household with `battery_control`
granted would be entitled to load control despite having no battery — harmless today (both are
still developer-only, granted by hand via SQL, no Stripe pricing yet either way), but worth
revisiting if/when either feature gets its own price, since the column name will no longer match
what it actually gates.

### Verification

Offline: unit tests for `DeferrableLoadController` against a fake `hass.services.async_call`
(mirrors the existing `test_charge_source_split.py`/`test_advisory_load_dedup.py` style — pure
logic, runs in this container, no scipy needed). Live: a real smoke test on an actual switch
entity (e.g. a Meross plug controlling a genuinely low-stakes load first, not the EV charger or
pool pump on day one) — mirrors the 2026-07-14 battery smoke-test discipline in the checklist
(toggle on, confirm hardware state via the entity's own state, toggle off, confirm deadman).

### Risk: highest of the three

Real hardware writes to a second device class; new controller/manager code. Debounce duration
(the one remaining tunable, e.g. 15 min default) is a config detail, not a blocking decision —
pick a sane default and expose it if it needs tuning later.

---

## Product decisions — settled 2026-07-23

All four decisions previously open in this doc were resolved interactively with the user; every
section above already reflects them. Recorded here as a single reference:

1. **Deadman policy for loads** (HA shutdown / stale plan / missed tick): **leave as-is, never
   force off.** Deliberate divergence from the battery's `restore_normal` deadman — cutting off a
   real EV-charging session or pool-pump cycle has more direct real-world consequence than
   reverting an inverter mode, and the user chose not to risk that on an HA restart or a missed
   tick. Consequence: no forced-mode auto-expiry timer for loads either (see the safety-pattern
   table above) — a stuck/crashed loop just leaves the load where it was.
2. **Entitlement gating**: **reuse the existing `battery_control` `ApiKey` column**, not a new
   `deferrable_control` column. Simpler now (still developer-only, no Stripe wiring for either
   feature); accepted tradeoff is that the column name won't match what it gates once
   `LoadControlManager` is decoupled from `has_battery` (see the note under the safety-pattern
   table) — revisit if/when either feature gets its own price.
3. **`LoadControlManager` scope**: **kept fully separate** from `ControlManager` — no
   inverter/HAL coupling, works for households with no battery configured at all. Costs a second
   `async_track_time_change` timer alongside the executor's.
4. **Feature 2 override unit**: **kWh** — direct drop-in for the LP's existing `daily_kwh` field,
   no conversion step, no rounding edge case.

---

## Suggested build order

1. Feature 1 (export price floor) — small, safe, ships independently.
2. Feature 2 (per-day override) — small, safe, ships independently; can happen in parallel with
   1 since they touch almost disjoint files (LP objective vs. new entity platform + config
   reads).
3. Feature 3a (observability audit) — cheap, mostly confirms existing data is good enough.
4. Feature 3b (actual control) — biggest and riskiest; do this last, once 1 and 2 are live and
   the advisory data (3a) has been trusted for a while, same discipline as the original
   advisory→control staging for the battery.

---

## Model / cost strategy for implementation

Goal per the request: minimize tokens/cost without compromising correctness on the
safety-critical pieces.

- **Keep LP math and control-loop/safety code in the main interactive session** (this session's
  model), not delegated to background subagents. This mirrors how every LP change and every
  `control/` change in this project's history was actually done — direct, iterative, verified via
  LXC scipy runs (this container has no scipy) — because these changes need cross-file
  consistency checking (objective/constraint indices, sign conventions, solver-path gating) that
  benefits from one continuous reasoning thread, not fan-out. Concretely: `battery_optimizer.py`
  changes (Feature 1), `control/load_controller.py` + `executor.py`/`DispatchInterval` changes
  (Feature 3) — main session.
- **Delegate mechanical, well-templated boilerplate to a background `Agent` call** (general-
  purpose, default effort — no need for a bigger model) with a prompt that names the exact
  existing pattern to copy, so it doesn't need to re-derive conventions:
  - `const.py` constant additions, `config_flow.py` `NumberSelector`/`EntitySelector` schema
    blocks (Features 1 & 3), `strings.json` + `translations/en.json` sync.
  - The `number.py` platform for Feature 2 — the entity class is a near-verbatim adaptation of
    `switch.py`'s existing `RestoreEntity` pattern to `NumberEntity`; hand the agent the exact
    file:line references from this doc and the target Store-helper interface, and check its diff
    before wiring it in.
  - The `switch.py` per-device entity class for Feature 3 — same reasoning, template is
    `GridLensBatteryControlSwitch` (`switch.py:27-83`) verbatim modulo per-device wiring.
- **Use `Explore` (not full-file `Read`) for any further code-location questions** that come up
  mid-implementation — cheap, read-only, keeps the main session's context from filling with
  full-file dumps it doesn't need.
- **Do not use the `Workflow` multi-agent orchestration tool for this work** — no explicit
  opt-in from the user for that scale of spend, and the highest-risk piece (Feature 3's hardware
  writes) benefits more from one careful reasoning thread with live verification than from
  parallel fan-out.
- **Verification stays the dominant real cost**, same as every prior LP/control change in this
  project (LXC round-trips for scipy, live smoke tests on real hardware) — budget for that rather
  than trying to shave it; it's what actually catches bugs here (see: the 2026-07-14/07-16/07-21
  live-only bugs in the checklist, none of which static review alone would have caught).

Rough sizing, for planning purposes only: Feature 1 is a single small session. Feature 2 is a
single small-to-medium session (mostly the new Store/entity plumbing, LP side is a one-line
scalar swap). Feature 3 is the size of the original advisory+control build combined — expect it
to span several sessions the same way that did, with its own smoke-test and phased-rollout
discipline.
