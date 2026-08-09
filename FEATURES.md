# Grid Lens — Feature Reference

**What this doc is:** the *current state* map of everything Grid Lens does — one entry per
user-facing feature, with the entities it creates, the config it needs, the files that
implement it, and the gotchas. Read this to understand **how the product works**.

**What it is not:** a history. `GRIDLENS_CHECKLIST.md` is the append-only record of *what
happened when* and *why a decision was made* — read that for rationale, incidents, and
work-in-progress. This doc is the answer to "what does it do today, and where is it?".

> **Keep this in sync.** Any change that adds, removes, or materially alters a user-facing
> feature — a new entity, a new config option, a new card, a changed default — updates this
> doc **in the same change**. The public docs (`docs/docs.html`, `docs/index.html`) and the
> video plan (`gridlens-api/MARKETING_VIDEO_PLAN.md` — private repo, moved 2026-08-02) are
> both downstream of it.

---

## 0. The shape of the product

Grid Lens is three layers stacked on the same model. Most competitors stop at layer 1.

| Layer | Question it answers | Needs |
|---|---|---|
| **1. Compare** | "Which retail plan is cheapest *for my actual house*?" | Energy sensors + history |
| **2. Plan** | "Given tomorrow's prices and solar forecast, what *should* my battery and loads do?" | Layer 1 + forecast + battery config |
| **3. Control** | "Do it." | Layer 2 + an inverter driver / load switches + entitlement |

Everything below belongs to one of those layers. The optimiser is the same LP/MILP in all
three — comparison scores a plan by *optimally* operating the house under it, which is why
the comparison is fair between a flat tariff and a wholesale-linked one.

---

## 1. Plan comparison (layer 1)

**What it does.** Models every plan available for the user's network against their real
metered history, and reports what each would have cost over a chosen period. Each plan is
scored by running the full optimiser under that plan's rate structure, so a plan with a
great overnight window is credited for the load-shifting it would actually enable.

**Entities**
| Entity | What it holds |
|---|---|
| `sensor.*_current_plan_monthly_cost` | Cost under the user's current plan. Carries `deferrable_loads` (the canonical per-device list every card auto-discovers from). |
| `sensor.*_best_alternative_plan` | Name of the cheapest modelled alternative. |
| `sensor.*_potential_monthly_savings` | Difference between the two. |
| `sensor.*` per-plan metric sensors | `plan_sensors.py` — one set per modelled plan. |

**Date-range re-run:** `GET /api/grid_lens/plan_data?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
recalculates the whole comparison on the fly and returns it as JSON (this is what the
dashboard's period picker calls). The old `grid_lens.calculate_period` service is
**deprecated and raises** — it is still registered, so it looks callable, but
`_calculate_and_populate_sensors` throws `HomeAssistantError` before returning anything.

**Files:** `plan_calculator.py`, `retailer_plans.py`, `sensor.py`, `plan_sensors.py`.

**Plan data** comes from the private `gridlens-api` (MySQL, temporally versioned —
`slug@date` rows). The HA side never sees another user's data and never sends usage data
out; the API only *delivers plan definitions*. See `PRIVACY_DATA_INVENTORY.md` in the API
repo.

**Rate structures modelled:** flat, TOU (multi-window, per-weekday), demand tariffs,
controlled load, tiered/capped rates (free-then-paid daily blocks), conditional daily
credits (e.g. "stay under X kWh in this window, get $1"), feed-in tariffs including
wholesale-linked ones, supply charges.

**Gotcha — capped-rate labels.** Label the free tier and the after-cap tier explicitly;
rate-value-keyed dicts silently merge on collision. See the checklist entry.

**The plan the user is actually on is never run through the LP.** Alternatives are scored by
what the optimiser could *achieve* under each tariff — a legitimate "what if you switched"
question. The current plan isn't hypothetical, so `calculate_plan_costs` (`plan_calculator.py`)
gives it a dedicated path instead: actual metered import/export, priced against *its own*
published tariff (`_compute_bill_items`'s actual-usage branch — cap-aware tiers, real
conditional-credit evaluation from real per-day behaviour, real FiT windows), not the LP's
optimal-dispatch fantasy. The two can diverge wildly — e.g. the LP assumes the battery
fully free-cycles every single day inside a plan's zero-rate window, which real dispatch may
never do — and conflating them once produced a "your bill breakdown" card showing $0.99 for
a period GloBird actually billed $21.04 (`GRIDLENS_CHECKLIST.md`, 2026-08-04).
`is_market_linked` plans (Amber SmartShift, real dynamic import) are the one exception: their
own published rate structure is a nominal reference, not the real price, so actual usage is
priced from the configured `import_price_sensor` / `export_price_sensor` instead — genuinely
actual data, just sourced from a live feed rather than a static tariff. That sensor-priced
path doesn't yet itemise per-tier (no per-interval FiT/energy_lines split for it) — a known
gap, not a silent wrong number: it reports one clearly-labelled total instead of guessing.

**Bill breakdown mirrors a real retailer bill, on purpose.** The "our bill breakdown" card
(`grid-lens-card.js`) orders and labels its rows to match how an Australian electricity bill
actually reads — fixed charges (supply/subscription/demand/controlled load) first, usage
charges next, feed-in/export credits next (one line **per rate tier**, e.g. a capped
"top-up" rate separate from the base feed-in rate — via `_compute_bill_items`'s `fit.lines`,
built the same way as `energy_lines`), bonus/conditional credits last, then the total. The
point is letting a customer tick GloBird-ZEROHERO-style output off against their actual PDF
bill line by line to verify the product is pricing them correctly — don't reorder or
re-blend sections without checking against a real bill sample first (see CLAUDE.md).

**Gotcha — capped-rate labels, now direction-scoped.** Label the free tier and the
after-cap tier explicitly; a rate-value-keyed label dict silently merges on collision if two
different tiers land on the same numeric rate — including *across* import and export (e.g.
GloBird's 0c import Free Window and 0c export No-Feed-in window). `_compute_bill_items` uses
separate `cap_labels` (import) and `export_cap_labels` (export) dicts for exactly this
reason — don't merge them back into one shared dict. See the checklist entry.

---

## 2. The optimiser (layer 2 core)

**What it does.** A linear/mixed-integer program over a 72-hour horizon at 30-minute
resolution that decides, for every slot: battery charge/discharge, grid import/export, and
how much of each deferrable device's daily energy budget to run. Objective: minimise net
cost (import cost − export credit − conditional credits), subject to battery capacity/rate/
SOC limits, per-device availability masks, and daily energy requirements.

**Files:** `battery_optimizer.py` (the LP itself), `advisory/planner.py` (wraps it, turns a
solved schedule into a dispatch plan + trajectory), `advisory/forecast.py`,
`advisory/rates.py`, `advisory/load_history.py`.

**Solver chain:** HiGHS (`_lp_highspy`) → scipy `linprog` MILP → PuLP.
**⚠ Known:** the HiGHS path is broken (`'Highs' object has no attribute
`changeColsCostByRange'`) and *always* falls back to scipy. Not caused by any recent work;
every plan silently solves on scipy MILP. Logs say `lp/scipy-milp solved 72 hours`.

**Notable modelling decisions** (each has a checklist entry with the reasoning):
- **Soft terminal SOC** — energy left in the battery at horizon end is valued at the
  horizon's mean export rate, instead of a hard "return to starting SOC" constraint. Kills
  the phantom end-of-horizon charge burst without enabling fake arbitrage.
- **SOC reward** (`0.0003`) — pure LP tie-breaking so degenerate optima resolve to the
  sensible plan (bank surplus solar rather than $0-export it). Calibrated: `0.001` distorts.
- **Minimum export price** (`number.*_minimum_export_price`, user-tunable) — below this
  price, export earns nothing *in the objective*, so the LP prefers routing surplus into a
  deferrable load or the battery. It still exports if nothing else can absorb the surplus.
- **No-grid-charge** option — the battery only ever charges from solar surplus; blocks
  buy-low/sell-high arbitrage for users who don't want it.
- **Deferrable devices stay in the energy balance** — `def_i` is priced via import/export
  like any other load. A past bug was double-counting them in *reporting*, not in the model;
  don't "fix" it by re-adding deferred energy to import.

---

## 3. Advisory / dispatch plan (layer 2 output)

**What it does.** Publishes the solved plan so the dashboard (and the control layer) can
consume it.

**Entities**
| Entity | What it holds |
|---|---|
| `sensor.*_planned_dispatch` | **The canonical plan.** `trajectory` attribute = array of 30-min slots, each with `import_rate`/`export_rate` ($/kWh), `solar_kwh`, `load_kwh`, `buy_kwh`, `sell_kwh`, `battery_kwh`, `soc_percent`, `action`, `defer_N` per device. |
| `sensor.*_next_action` | charge / discharge / self_use. |
| `sensor.*_soc_now`, `sensor.*_planned_end_soc` | SOC tiles. |
| `sensor.*_plan_net_cost` | Net $ over the horizon under the plan. |

**Files:** `advisory/coordinator.py`, `advisory/dispatch_sensor.py`, `advisory/models.py`.

**⚠ Canonical price source.** For "what's the current buy/sell rate" in *any* Grid Lens UI,
read the dispatch sensor's `trajectory` — **not** a raw retailer price sensor. The
trajectory is what the optimiser actually priced against (conditional credits, plan-specific
rate logic, caps). Auto-discover it by scanning for a sensor whose `trajectory[0]` has
`import_rate` (`_resolvePriceEntityId()` in `grid-lens-powerflow-card.js`) — never hardcode
the install-specific slug.

**Startup behaviour.** The plan needs a battery SOC reading, which can take ~9.5 min to
arrive after a restart. The coordinator uses a dynamic `update_interval` plus a persisted
plan restore so the Battery Plan card isn't blank for 10 minutes.

---

## 4. Battery control (layer 3)

**What it does.** Actuates the battery to follow the plan, through a brand-agnostic
inverter HAL.

**Entity:** `switch.*_battery_control` — master on/off.

**Files:** `control/manager.py` (lifecycle, entitlement, persistence), `control/executor.py`
(the 5-minute tick + `DispatchInterval`), `control/battery_controller.py` (SOC guardrails),
`inverters/base.py` (the HAL contract), `inverters/sigenergy_mqtt.py` (the one shipped
driver: battery control + export curtailment).

**How a slot becomes a command** (`executor.py` — the subtlety that took several bugs to get
right):
- `power_w` is the *total* planned battery flow; `grid_charge_w` and `export_w` are the
  portions the plan intends to source from / sell to the **grid**. The executor branches on
  those, not on `power_w`:
  - **Solar-only charge** → Maximum Self-consumption. Force-charging at `power_w` here makes
    the inverter import whenever instantaneous PV < `power_w` (the 10 kW import-spike bug);
    force-charging at a *tiny* `grid_charge_w` instead throttles the solar charge and dumps
    surplus to a $0 export.
  - **Material grid charge** → `force_charge(grid_charge_w)`.
  - **Free-import window** (`import_rate ≈ 0`) → `force_charge(power_w)`, the full ceiling —
    there's no cost risk in over-committing when import is free.
  - **Load-covering discharge** → self-consumption (forcing "battery first" at $0 FiT would
    spill real-time load dips into a worthless export).
  - **Discharge with a material export component** → forced battery-first at the planned rate.
- Entering max self-consumption **must reset the charge cap to the hardware max**, or tiny
  LP grid nibbles force-charge the battery low and export free solar.

**Deadman:** on disable / HA stop / stale plan, the battery is handed back to its native EMS.

**Gotchas**
- `switch.*_battery_control` can sit **off for a full day with no log or error trail**.
  Check its state and history *first* when exports aren't happening.
- **Never restore control intent from an entity a deadman forces off on every reload.** The
  manager persists intent in its own `Store` and restores itself *before* the switch entity
  is created; the switch just displays it. (RestoreEntity was tried and dropped — 2026-08-01.)
- Brief PV spikes above the inverter's own `available_max_charging_power` export to grid.
  That's a hardware limit, not the charge-cap software bug.

---

## 5. Deferrable loads — modelling

**What it is.** A load whose *timing* is flexible but whose daily energy is roughly fixed:
EV charger, hot water, pool pump. The optimiser is told each device's average daily kWh, its
max kW, and when it's *allowed* to run; it decides *when*.

**Config** (per device, parallel lists): `deferrable_load_sensors` (energy sensor — the join
key), `..._max_kw`, `..._switches` (control entity, `""` = forecast-only — see below),
`..._soc_sensors` (e.g. EV SOC). Availability windows are **not** set here — see §8; a device
is fully unrestricted (any hour) until the user paints a schedule on the dashboard card. (A
static per-device `deferrable_load_hours` config-flow field used to seed this before the
schedule card existed — removed 2026-08-02 as redundant with it.)

**Control entity can be `switch.*` OR `climate.*`** (added 2026-08-02, for aircon). Despite
the config key's name, `..._switches` accepts either domain — `control/load_controller.py`
picks the actuation mechanism from the entity_id's own domain, so every other consumer
(`switch.py`'s per-device Control/Greedy switches, `select.py`'s override, the schedule card)
stays domain-agnostic. See §6 for how a climate entity is actually driven.

**Or a `number.*` current setpoint** — a *modulating* device (added 2026-08-03, for EV
chargers). Config: `deferrable_load_setpoint` plus `..._setpoint_unit`, `..._phases`,
`..._voltage`, `..._min_current`, `..._plug_sensor`. A device with a setpoint entity is
driven by *how much* power it may draw rather than on/off. See §6a.

**Estimated (unmonitored, controllable) loads** — a third device category, for a control
entity with no energy feedback path at all and no way to add one (the canonical case: an
IR-blaster-driven aircon, `climate.daikin_ac` on this install — no ECHONET/Modbus/etc
telemetry, just IR commands out). Config: `deferrable_load_est_names` /
`..._est_control` / `..._est_kw` (manual seed) / `..._est_auto` (opt-in to refine the seed
from real usage), fixed 3 slots, plus one entry-wide `load_power_sensor` (whole-house load
power — the backend counterpart of the Power Flow card's own `load_power_entity` card
option). **A slot needs both a name and a control entity** — `_ensure_load_estimators`
skips it silently (no warning) if either is blank, so a slot with the control entity/kW/
auto-refine filled in but no name looks fully configured yet does nothing (hit for real,
`GRIDLENS_CHECKLIST.md`, 2026-08-06); the config-flow step now rejects that combination
instead of accepting it. A brand-new slot also won't be scheduled by the optimiser until its
synthetic sensor has real usage history (`daily_kwh` starts at 0 — see §3's `daily_kwh`
note) — use Today Boost to seed a target immediately instead of waiting ~14 days. GridLens
builds a real synthetic energy sensor for each configured slot
(`GridLensEstimatedEnergySensor`, `sensor.py`) and splices its entity_id straight into
`deferrable_load_sensors` at setup (`__init__.py._ensure_load_estimators`, before anything
else reads `entry.data`) — so every other deferrable-load feature (LP dispatch, control, the
schedule card, Today Boost, Greedy Consumption) treats it exactly like a device with a real
meter, no separate code path. The number itself comes from `load_estimation.py`'s
`LoadEstimator`: it watches the control entity for an off→on transition, samples the house
load power sensor just before and ~3 minutes after (long enough for a compressor to spin up),
and folds a plausible delta into an EMA-smoothed power estimate — discarding the sample if
another configured deferrable device changed state during the window (can't attribute the
delta) or if the delta is outside a sane floor/ceiling. Learns from *any* on/off transition
(manual, climate_scheduler, Force On), not just ones GridLens itself commanded, so it
bootstraps from ordinary use. Distinct from "declared/dummy" loads below — those stay
forecast-only by design (the Controlled Load nomination mechanism) and were **not** extended
by this feature.

**Power-only inference for the Power Flow card** (added 2026-08-02) — fully automatic, no
config beyond `load_power_sensor` (same field as above). The Power Flow card drops any
deferrable-load node with no live `power_entity`, and `entity_lookup.resolve_power_sensor()`
can only find one if a `device_class: power` sibling sensor exists on the device — which
ECHONET Lite aircons never have (only a cumulative `device_class: energy` counter). So at
setup, `_ensure_load_estimators`'s second pass scans every deferrable device with a control
entity and no resolvable `power_entity` (sensor-backed **or** an estimated load from the
first pass) and builds it a power-only `LoadEstimator` (`track_energy=False` — the real
accumulation, where one exists, already comes from the device's own sensor; this instance
exists purely to back `GridLensEstimatedPowerSensor`, a live-W reading = the estimate while
"on", 0 while "off"). `sensor.py._build_deferrable_loads()` falls back to it as `power_entity`
whenever the real lookup finds nothing. A device with a real power sensor already, or with no
control entity at all (forecast-only), is left untouched.

**Calibration source** (own-meter preferred, added 2026-08-04): the device's cumulative
energy sensor is passed in as `energy_sensor=sensor_id` — that sensor is *why* the device
reached step 2 in the first place, since it's the only telemetry it has. When set,
`LoadEstimator` calibrates by reading that sensor's value at the start and end of one full
on-period and dividing the rise by the elapsed time (`load_estimate_math.energy_sample_avg_w`,
discards a sample if the on-period was too short to trust or the counter went backwards —
a device reboot resetting its counter, not a real near-zero reading), **never** the
whole-house `load_power_sensor` — that fallback path is only still used for step 1's true
"no sensor at all" estimated loads. Own-meter calibration is immune to any *other* load's
power draw during the same window, unlike house-load sampling, which attributes the whole
house-power delta at an off→on transition to the one device turning on — wrong whenever a
second load is drawing variable power at the same time (e.g. two aircon units running
together, one ECHONET-metered and one not: the metered one's own counter is unaffected by
the other's compressor cycling, but the house-load delta isn't).

**Daily kWh** comes from a 14-day historical average of the device's own energy sensor,
overridable per-day (see Today Boost).

**Declared / "dummy" loads.** A device with *no* sensor can be declared by name + estimated
average daily kWh (`deferrable_load_dummy_*`). The LP needs almost no change — it already
runs off `daily_kwh`, not raw curves. This is the foundation for Controlled Load nomination,
where the circuit is genuinely unmonitored (separate circuit from the inverter CT) **and**
uncontrollable (DNSP-switched) — so that feature must be designed around declared/estimated
loads, never sensor wiring.

**Files:** `plan_calculator.py`, `advisory/coordinator.py::_deferrable_for_horizon`,
`entity_lookup.py` (name/power-sensor auto-discovery).

**Naming.** Display names resolve via `resolve_device_name()`: entity-registry name → Energy
Dashboard per-device label → trimmed entity id. The Energy Dashboard label is where a rename
like "Hot Water" actually lives, so it wins over the raw registry name.

---

## 6. Deferrable load control (layer 3)

**What it does.** Switches a simple on/off appliance ("type 1" load — draws roughly a fixed
power when on) to follow the plan. Includes aircon: a `climate.*` entity is a valid control
entity, not just `switch.*` (added 2026-08-02).

**Climate entities (aircon).** "On"/"off" is the hvac_mode, not a native switch state —
anything other than `off` counts as on. Actuation prefers `climate.turn_on`/`climate.turn_off`
(both device families this integration ships against — ECHONET Lite and SmartIR/Broadlink —
support these), falling back to `climate.set_hvac_mode` for a climate integration that doesn't
declare `TURN_ON`/`TURN_OFF` support, using either a per-device configured hvac mode
(`deferrable_load_climate_on_mode`) or the entity's own first non-"off" `hvac_modes` entry.
GridLens deliberately never touches hvac_mode or target temperature beyond deciding on/off —
comfort settings stay under the user's own control (or e.g. `climate_scheduler`'s). **Gotcha:**
if something else also drives on/off on the same climate entity (a schedule, the user),
GridLens's plan and that other driver can fight — no arbitration is attempted, same as two
people fighting over one switch.

**Entities per controllable device**
| Entity | Default | Purpose |
|---|---|---|
| `switch.*_<device>_control` | **OFF** | Master: GridLens drives this load. |
| `select.*_<device>_override` | Auto | **Force On / Force Off / Auto** — a direct human command. |
| `switch.*_<device>_greedy_consumption` | **OFF** | See §7. |
| `switch.*_<device>_greedy_respects_schedule` | **OFF** | See §7. |
| `switch.*_<device>_greedy_forecast_surplus` | **OFF** | See §7. |
| `number.*_<device>_today_boost` | 0 | See §9. |

**Files:** `control/load_control_manager.py` (lifecycle, tick, entitlement),
`control/load_controller.py` (per-device decision + actuation).

**Design**
- **On/off threshold** — the LP's per-device power is continuous, so "on" = the plan
  allocated ≥ 50% of the device's rated power to this slot, with an absolute floor so LP
  noise never counts.
- **Debounce** — minimum on-time and off-time (default 15 min each) so a borderline signal
  doesn't chatter a physical relay. A *drift re-assert* (hardware moved away from what we
  commanded) is deliberately **not** debounced — it restores existing intent.
- **Deadman = leave as-is.** On disable, HA stop, or a stale plan, a load is **never** forced
  off — GridLens just stops driving it. Cutting a real appliance mid-cycle has more
  real-world consequence than reverting an inverter mode.
- **A manual override wins over everything**, including greedy and the drift re-assert.
  "Hands off; set it to X and leave it."
- Deliberately **decoupled** from the battery `ControlManager`: load control has zero
  brand-specific logic and must work on a house with no battery at all.

**Card layout — every row the same shape.** `grid-lens-load-control-card.js` always
renders the segmented Off now/On now/Auto control *and* all three Greedy buttons for
every device, even one with no control switch configured at all — disabled and dimmed,
with a tooltip explaining why, rather than omitted. Rows for a controllable and a
forecast-only device line up identically instead of the row width jumping around
depending on what's wired up.

**Tooltips.** Every hint on this card (disabled-control reasons, Greedy button
explanations, the boost ceiling note, sparkline bar dates) is a custom JS-delegated
popup (`_initTooltip`/`data-tip` in `grid-lens-load-control-card.js`), not the native
`title=""` attribute — shows in ~150 ms on hover instead of the browser's own ~1 s
delay, and instantly on keyboard/touch focus (every `[data-tip]` element carries
`tabindex="0"` so touch and keyboard can reach it, since native title tooltips are
unreliable to trigger by tap).

---

## 6a. Modulating load control — EV chargers (layer 3)

**What it does.** Drives a load that accepts a *continuous* power/current setpoint — an EV
charger — by deciding how many amps it may draw, re-evaluated every **30 seconds**. This is
"type 2" control, alongside §6's type-1 on/off.

**Why it's a better fit than on/off.** The LP already solves `def_i` as a continuous
`0..max_kw` variable. The on/off controller throws that resolution away at a 50% threshold
(`desired_on()`); a modulating controller consumes the optimiser's actual answer.

**The abstraction is a `number.*` setpoint, not an OCPP driver.** Every charger integration
worth supporting exposes the same shape — a number entity carrying a charging-current limit
in amps: OCPP (`lbbrhzn/ocpp`) `number.*_maximum_current` (A, min 0, step 1), Easee's dynamic
charger limit, Wallbox's max charging current, Zaptec, go-e, openEVSE, Tesla's charging amps,
Sigenergy's AC-charger output current. Config is "point GridLens at that number entity", so
any integration matching the shape works with **no GridLens change**. A `switch.*` may be
configured alongside it (turned on before ramping up, off after commanding 0); the common
case is a setpoint alone, since writing 0 A stops delivery.

**Entities:** all of §6's, plus `number.*_<device>_max_current` — a user ceiling on the
current GridLens may command. Defaults to the hardware max (unrestricted out of the box);
`RestoreEntity`, because it's durable user intent with no deadman that clears it.

**Files:** `control/modulating_controller.py` (`ModulatingLoadController`, a subclass of
`DeferrableLoadController` so override/greedy/debounce behaviour is shared, not forked),
`control/load_control_manager.py` (the fast loop).

**Two loops, deliberately.** The existing 5-minute tick still runs `apply()` — it evaluates
the greedy conditions and sets intent. A second `async_track_time_interval` at
`MODULATION_INTERVAL_SECONDS` (30 s) calls `modulate()`. It starts **only when an enabled
device is actually modulating** — a household with no charger never gains a 30-second timer.
5 minutes is far too coarse to track a passing cloud, and solar-following is the whole reason
to modulate rather than switch.

**Target power** (`LoadControlManager._modulation_target_w`):
`target = max(plan_w, surplus_w)`, where `surplus_w` is the continuous generalisation of
Greedy Consumption — `current export + what this device is already drawing`, i.e. how much it
could pull without creating new import. A free-import window targets the full cap. The
surplus term is gated on the same greedy toggles and schedule check as §7, and **fails closed**:
no `grid_power_sensor`, unknown rate, or unavailable entity and the term contributes nothing,
leaving pure plan-following.

**⚠ The 6 A floor is the subtle part.** An EV's feasible set is `{0} ∪ [min, max]`, **not**
`[0, max]` — IEC 61851 forbids offering below 6 A, and commanding 3 A doesn't charge slowly,
it makes the car refuse or fault. So a sub-minimum allocation must resolve to *either* off or
min, never to itself. The controller snaps, with **hysteresis**: below `min_w` while off stays
off; below `min_w` while already charging holds at `min_w` down to `0.6 × min_w` before
dropping to 0. A cut-off EV can take 30+ s to re-handshake, so an unnecessary stop at a cloud
edge is expensive.

**The LP is deliberately *not* told this** (2026-08-03 decision). It keeps its continuous
variable; `min_kw` is plumbed through `plan_calculator._deferrable_min_kw` → the per-device
dicts → `battery_optimizer`, where it is **reserved and currently ignored**. Modelling
`{0} ∪ [min, max]` properly needs a binary per slot per device (~144 per charger over a 72 h
horizon) on a solve that already always falls back to scipy because the HiGHS path is broken.
The plumbing is there so it can be switched on behind a flag once solve time is measured. The
practical cost of the gap is small: a linear objective already prefers running flat out in the
cheapest slots.

**Write economy is a safety property, not tidiness.** These setpoints go over the wire as
OCPP `SetChargingProfile` calls or cloud API writes to Easee/Wallbox/Zaptec. A 30-second loop
with no throttle is a write storm against someone's charger. Hence a `write_deadband_a`
(0.5 A, converted to the setpoint's own unit) and a `min_write_interval_s` (20 s) — both
bypassed when the target is 0 or crosses the on/off boundary, which always writes immediately.

**Unit handling.** `watts = amps × voltage × phases`. The unit is inferred from the setpoint
entity's own `unit_of_measurement` (resolved lazily — at construction the charger integration
may not have published state yet), overridable per device. Phases auto-derive from
`max_kw ÷ (native_max_a × voltage)`, clamped 1–3: a 7.4 kW single-phase charger and a 22 kW
three-phase one both advertise 32 A, and only `max_kw` distinguishes them.

**Plug detection fails OPEN.** `deferrable_load_plug_sensor` is optional; a state in
`MODULATING_UNPLUGGED_STATES` (OCPP `ChargePointStatus` vocabulary plus the usual
binary_sensor renderings) commands 0. Unconfigured, unavailable, or unrecognised means
"assume plugged" — GridLens must never withhold charging because it couldn't confirm a plug.

**Gotcha — the join key.** A switchless charger has `switch_entity_id == ""`. Every auxiliary
entity (greedy switches, override select) and the Load Control card pair themselves to a
device by matching a published `switch` attribute, so an empty string would make every
switchless charger on an install collide. `DeferrableLoadController.join_key` exists for
exactly this: the subclass falls back to the setpoint entity id. Use it, never
`switch_entity_id`, for anything user-facing.

**⚠ Untested on real hardware.** The dev rig has no modulating charger — only an on/off smart
plug — so everything above is verified against stubs only (`tests/test_modulating_load_control.py`).
Phase auto-derivation, companion-switch ordering, and each vendor's step/rounding semantics
have never met a real charger.

---

## 7. Greedy Consumption

**What it does.** A real-time safety net *on top of* the plan: turn a load on whenever
energy is genuinely free, regardless of what the plan scheduled for this slot. Three
conditions, any one is enough. All fold into the same `want_on` the plan produces, so a
greedy "on" gets the identical debounce and transition-economy treatment — no separate code
path, no separate chatter risk. All are suppressed entirely under a manual override.

| # | Condition | Fires when | Can it cost money? |
|---|---|---|---|
| 1 | **Free import** | This slot's import rate is $0 (a plan's free window). | No |
| 2 | **Export surplus** | Export price is $0 **and** the house is currently exporting at least as much as this device draws — so running it can't create new import. | No |
| 3 | **Forecast surplus** | Over a 4 h look-ahead, the plan expects to waste **more free energy than this device could consume running flat out for that whole window**. | **Yes** |

**Condition 2's bar is lower for a modulating load** (§6a). An on/off load has to clear
`max_w` — all-or-nothing, so turning it on when only part of its draw is covered would create
real import. A modulating load can absorb *any* surplus, so its bar is `min_w` instead. That's
the single hook `_export_surplus_threshold_w()` exists for; the on/off behaviour is unchanged.

**Condition 3 on a modulating load targets the full envelope** (`cap_w`), not a metered
surplus — it's forward-looking, so there's no live figure to meter against, and its bar is
already "even running flat out for the whole window the plan still spills". `greedy_reason`
is the only record that it fired (it's evaluated on the 5-min tick, not the 30-s one), which
is why that property is public and read by `_modulation_target_w`.

**Why #3 exists.** #1 and #2 are strictly instantaneous — they only fire once free energy is
already flowing. On a solar+battery house that fires late: mid-morning the battery soaks up
every spare watt, so live export is ~0 and neither fires, yet the plan already knows the
afternoon will spill far more than the device could eat. By the time export shows up, hours
of run-time are gone.

**Why #3 is safe enough to ship.** It has its own opt-in switch (and requires the master
greedy switch), and the bar is the *full* window — "even with this load running continuously
from now to the end of the window, the plan still throws free energy away". What makes the
bet pay is the battery: running now draws it down, and that hole is refilled by surplus that
would otherwise have been exported for nothing.

**What counts as "free energy the plan will waste"** (`LoadControlManager._forecast_free_kwh`):
- **Spilled export** — a slot with `export_rate ≤ 0` that the plan still exports into. Uses
  `DispatchInterval.total_export_w` (whole-house export, PV spill included) — *not*
  `export_w`, which is only the battery's share of a discharge slot and is 0 on a pure
  solar-spill slot. Already net of every load the plan schedules, so nothing is subtracted.
- **Unused free-import window** — a slot with `import_rate ≤ 0`; only the part the plan does
  *not* already run this device counts (`max_w − planned_w`).

**Fail-closed everywhere.** Unknown rate, unavailable sensor, or a plan covering less than
half the look-ahead → the condition contributes nothing rather than guessing. (The bar
scales with the covered span, so a sliver of horizon tail would otherwise shrink it until a
trivial surplus cleared it.)

**Config:** the export-surplus condition needs `grid_power_sensor` — a **signed live power**
sensor, positive = importing, negative = exporting. Without it, condition #2 simply never
fires; #1 and #3 still work. Note this is a *power* sensor: the Energy-dashboard sensors
(`energy_sensor`, `solar_sensor`, `grid_export_sensor`) are cumulative kWh and cannot serve.

**Observability** — see §11.

**Tuning knob:** `GREEDY_SURPLUS_LOOKAHEAD_HOURS = 4.0` in `load_control_manager.py`.

---

## 8. Weekly schedules (allowed run times)

**What it does.** A 7 × 48 (per-weekday, half-hourly) grid of when each deferrable device is
*allowed* to run, painted on a dashboard card. Feeds the LP as a per-slot availability mask,
and optionally gates Greedy Consumption.

**Services:** `grid_lens.set_deferrable_schedule`, `grid_lens.clear_deferrable_schedule`.
**Card:** `grid-lens-defer-schedule-card.js`. **Store:** `deferrable_schedules.py`.
**Helpers:** `schedule_grid.py` (`slot_allowed`, `week_from_hours`, `rolling_window_hours`).

**Fallback chain:** stored weekly grid → all-allowed. This is now the *only* place a
sensor-backed device's availability window is set — the config-flow's old static
`deferrable_load_hours` field (a comma-separated-hours text box per device, from before this
card existed) was removed 2026-08-02 as redundant with it. Both layers **fail OPEN** — a
malformed/missing store must never silently pin a device off, it just means unrestricted.

**Gotcha:** the optimiser's first day-chunk is anchored to *now*, not local midnight, so it
spans two weekdays. "Hours available in the next 24 h" (`rolling_window_hours`) is the right
bound for anything user-facing, not "allowed hours per day".

---

## 9. Today Boost

**What it does.** Overrides a device's daily kWh target for today only — "the EV needs 25 kWh
tonight, not its usual 13". `number.*_<device>_today_boost`, 0 = use the 14-day historical
average.

**Files:** `number.py`, `deferrable_overrides.py`, `override_expiry.py`.

**History sparkline.** The Load Control card shows a 14-day daily-kWh bar sparkline next to
each device's Today Boost input (including today, partial) plus the average of the completed
days — the same 14-day window `load_history.py` averages for the optimizer's own default, so
the number the sparkline centers on is the number Today Boost is overriding. Fetched
client-side via the recorder's `recorder/statistics_during_period` WS call
(`period: 'day', types: ['change']`), cached per device for 15 minutes
(`grid-lens-load-control-card.js::_fetchHistory`/`_pollHistory`) — no new backend entity or
config. A device with no recorder statistics yet (freshly added sensor) simply shows no
sparkline rather than an error.

**Behaviour**
- Carryover is deliberate and bounded: a boost persists across the post-midnight slots the
  plan is already relying on it for, then expires — it can't silently inflate `daily_kwh`
  every day forever.
- A target above what the availability window can physically deliver is silently clamped by
  the LP, which reads as "my boost did nothing" — so the **card shows the ceiling**
  (`max_kw × rolling window hours`) at the input.

---

## 10. Cards & the default dashboard

All cards **auto-discover** their entities by attribute fingerprint — never a naming
convention, never a hardcoded entity id — so they work unmodified on any install.

| Card | Shows |
|---|---|
| `grid-lens-card` | Full plan comparison (the Plan Comparison view). |
| `grid-lens-powerflow-card` | **Gated** — live radial energy flow: solar / grid / battery / home + one node per deferrable load, animated flow balls, live buy/sell price, greedy badges. Requires the Battery Control + Power Flow add-on; see §12. |
| `grid-lens-power-chart-card` | Measured & forecast power (kW) — solar, load, signed grid, signed battery, per-device deferrable, plus free-energy shading. |
| `grid-lens-price-chart-card` | Import/export rate trajectory. |
| `grid-lens-soc-chart-card` | Battery SOC curve. |
| `grid-lens-cash-chart-card` | Cumulative cost/credit. |
| `grid-lens-dispatch-chart-card` | Planned EMS mode timeline. |
| `grid-lens-advisory-card` | Plan status tiles. |
| `grid-lens-load-control-card` | One row per deferrable load: Today Boost, greedy toggles, Off now / On now / Auto, and live greedy status. |
| `grid-lens-defer-schedule-card` | The 7 × 48 allowed-run-times editor. |
| `grid-lens-flex-row-card` | Layout helper. |

**Aggregated Aircon node** (Power Flow card, added 2026-08-02, wattage+estimate cue added
2026-08-05). Every `climate.*` entity that isn't a group/aggregator wrapper (identified by the
*absence* of a `member_entities` attribute — not by name or integration, so any HA climate
group is excluded the same way) is folded into one "dragon" node instead of drawing its own —
heat/cool/neutral/idle art picked from the busiest state across all units, with a corner badge
showing the active count and a tooltip breakdown ("1 heating · 2 off"). **Per-unit detail panel**
(added 2026-08-05, tap-to-toggle 2026-08-05): tapping/clicking the dragon icon itself (not just
the corner badge) opens a panel below the diagram with the same summary line plus one line per
`climate.*` unit — its resolved state (Heating/Cooling/Off/etc) and, wherever the entity reports
`current_temperature`/`temperature` (or `target_temp_low`/`target_temp_high` for range-mode
units), its current and target temperature. Units that report neither temperature attribute just
show their state with no temp suffix. Deliberately **not** a native SVG `<title>` (tried first,
reverted same day): the card fully rebuilds `shadowRoot.innerHTML` on every re-render — which
fires on any watched solar/grid/battery/load power change, i.e. every few seconds — so a native
title's DOM node kept getting torn down before the browser's hover-and-wait timer could fire, and
it doesn't work on touch at all. The panel's open/closed state instead lives on the component
instance (`_openTooltipId`), survives the `innerHTML` rebuild, and its content re-reads live each
render so it stays open and up to date rather than vanishing; tap the panel's close button or
anywhere else on the card to dismiss it. Built generically off `_pnode`'s `nodeTooltip` field, so
any other node could opt into the same panel just by setting it. A `climate.*` entity
that's ALSO a deferrable load's `switch_entity` (e.g. an ECHONET Lite aircon under load control)
is represented here instead of getting its own individual node — full aggregation, no per-unit
carve-out. **Wattage**: summed across whichever units resolve a `power_entity` via that same
deferrable-loads lookup — partial coverage is fine (an install with one metered unit and two
unmonitored ones still shows a number for the one it can see); no wattage line at all when zero
units resolve one, rather than fabricating a figure. Prefixed with **"~"** and called out in the
tooltip as "(estimated)" whenever any summed component is a `LoadEstimator`-backed synthetic
reading rather than a real meter — detected generically by the presence of an `auto_refine`
attribute (a shape unique to `GridLensEstimatedPowerSensor`), not by entity name. This is also
why a bad estimate here (a device on a flaky integration flapping `unavailable` mid-run — see
`load_estimation.py`'s `_confirmed_on` handling) can make the **Home** node read low or 0:
`Home = max(0, whole_home_load − Σ deferrable_loads)`, and every deferrable load's power
(including an over-estimated aircon) is subtracted out of it.

**Seeded dashboard.** New installs get a "Grid Lens" sidebar dashboard built by
`_build_seed_views()` in `__init__.py`, written **once** into `.storage/lovelace.grid_lens`.
Views: Plan Comparison, Battery Plan, Settings.

**⚠ Two rules that bite:**
1. **Card JS changes reach every dashboard automatically** (the seed just instantiates the
   card). **Dashboard structure/config changes do not** — adding/removing a card, changing a
   card's YAML options, view layout — those must be mirrored in `_build_seed_views()` **in
   the same change**.
2. **Card cache-busting is driven by `_CARD_VERSION` in `__init__.py`** — the single source
   of truth, force-rewritten into every Lovelace resource URL on every HA startup. Editing
   `.storage/lovelace_resources` directly or via websocket *appears* to work and silently
   reverts on the next restart. To ship a card change: bump `_CARD_VERSION`, run
   `sync-to-ha.sh`. If `grid-lens-chart-common.js` itself changed, **also bump its `?v=`
   sub-import string in every card that imports it** — ES modules cache by exact URL.
3. **`grid-lens-powerflow-card.js` doesn't live here.** Its source of truth is
   `gridlens-api/app/cards/grid-lens-powerflow-card.js` (private repo) — `sync-to-ha.sh`
   never touches it. To ship a change to it: edit it in `gridlens-api`, push to `main` (the
   self-hosted runner rebuilds/redeploys the API container automatically), then bump
   `_CARD_VERSION` in the public repo and `sync-to-ha.sh` as usual so browsers fetch the new
   version through the proxy. See §12 for why it's served this way.

---

## 11. Observability

Because layers 2 and 3 take actions the user didn't ask for slot-by-slot, "why did it do
that?" has to be answerable from the dashboard alone.

| Surface | Answers |
|---|---|
| `switch.*_battery_control` attributes | Applied action/power, last tick, plan age, degraded state, note. |
| `switch.*_<device>_control` attributes | Commanded state, threshold, override, all three greedy toggles, **`greedy_reason`**, **`greedy_blocked`**, **`forecast_free_kwh` / `forecast_needed_kwh`**, note. Modulating devices add `control_type`, `setpoint_entity`, `min_w`/`cap_w`, `commanded_w`/`commanded_setpoint`, `plugged_in`, `last_write`, `modulation_source`. |
| **Load Control card** | Per row: control state, and a live greedy line — the firing reason, or why it's blocked, or a **progress bar toward the forecast-surplus bar** (`6.2 / 8.0 kWh`) while it's armed and tracking. For a modulating device (§6a): live amps + kW, the max-current ceiling input, and a one-line "why" — `modulation_source` (plan / surplus / override / off) and `plugged_in`. "Why is my car charging at 8 A right now?" must be answerable from the row. |
| **Power Flow card** | A badge on a load node while *greedy*, not the plan, is holding it on — leaf for the two instantaneous reasons, sun-alert for forecast surplus, with the kWh figures in the tooltip. |
| **Power Chart card** | Free-energy time bands: **orange = free energy being wasted** (plan exports into a ≤$0 export price), **teal = free import window**. Legend appears only when a band is in view; the crosshair tooltip names the band. |
| `ha core logs` | Every optimiser run logs horizon, device count, solver status, credits, caps, export floor. |

**⚠ Read live control state from the control SWITCH, not the `deferrable_loads` sensor
attribute.** That sensor is a `CoordinatorEntity` tied to the plan-comparison run, so its
attributes only refresh when a comparison lands — while control state flips on the 5-minute
tick. The switches are plain polled entities and are the only surface that tracks live.

---

## 12. Account, tiers, entitlement

- **Free** — model your own current plan. No API key needed; the integration registers the
  installation automatically.
- **Pro ($1/month)** — all plans for your state.
- **Control entitlement** — battery control and deferrable-load control share one
  entitlement column. It **fails closed**: no actuation until the API confirms. Revoking it
  stops actuation immediately but *keeps user intent*, so a re-grant auto-resumes without
  the user re-toggling every switch.
- **Battery Control + Power Flow add-on** (`ApiKey.battery_control` / `ApiKey.powerflow_card`,
  granted/revoked together by one Stripe Price — `gridlens-api/app/billing.py`) — an optional
  paid add-on on top of either the free or Pro plan-comparison tier; see `subscribe.html`.
  **The Power Flow card itself is the gate, not just its data**: unlike every other card
  (shipped free, source in the public repo), `grid-lens-powerflow-card.js`'s source of truth
  lives in `gridlens-api/app/cards/`, served only via `GET /cards/powerflow`
  (`gridlens-api/app/cards.py`), gated on `powerflow_card` — 402 if not entitled. The public
  integration never ships this card's code at all. `custom_components/grid_lens/__init__.py`'s
  `PowerflowCardView` proxies it: fetches server-to-server with the install's own API key
  (the browser never sees the key), caches per config entry (5 min if entitled, 60 s if not,
  so an upgrade is reflected reasonably promptly), and on a network failure **prefers
  re-serving a stale-but-real cached copy over the paywall** — an API outage must never nag a
  paying customer. Not entitled (or nothing ever fetched) serves
  `powerflow_locked.LOCKED_CARD_JS`, a self-contained upsell stub registered under the *same*
  custom-element tag (`grid-lens-powerflow-card`) so existing dashboard configs, including the
  seeded one, don't need to know which variant they're getting.

**⚠ Every asset exclusive to a gated feature must be gated the same way as its JS — not
just the code.** Found 2026-08-05: all 29 of the Power Flow card's node icons (battery,
solar, grid, EV, water-heater, aircon) had been sitting in the *public* `gridlens` repo's
`custom_components/grid_lens/www/icons/` the whole time, served as plain static files —
predating this card being gated at all (`git log --follow` traces the oldest one back to a
pre-gating "power-flow POC" commit) and never moved when gating was added. No other (free)
card referenced any of them, so the exposure bought nothing and just quietly undermined the
entitlement boundary this section otherwise describes carefully. Fixed by mirroring
`PowerflowCardView` exactly for binary assets: `PowerflowIconView`
(`/api/grid_lens/icons/{filename}`) proxies `GET /cards/powerflow/icons/{filename}`
(`gridlens-api/app/cards.py`, same `require_api_key` + `powerflow_card` check, filename
checked against a strict allowlist regex before any filesystem access), and the icon files
themselves now live in `gridlens-api/app/cards/icons/`, not the public repo. **The lesson for
any future gated feature**: adding a new node icon, image, or other binary asset that only
that feature uses means adding it to the *private* repo's icon directory and, if it's a new
top-level asset *type* (not just a new file under an existing served path), extending
`PowerflowIconView`/`cards.py` — never drop it straight into the public repo's `www/`
tree just because that's the path already being edited for something else nearby.

**HA → API calls must** use `async_get_clientsession(hass)` and send
`User-Agent: GridLens-HA-Integration/1.0`. A raw `aiohttp.ClientSession()` gets 403 from
Cloudflare's bot protection.

**What leaves the house:** email, HA installation UUID, state, plan ID, network slug. Never
energy usage. Any change here updates `PRIVACY_DATA_INVENTORY.md` **and** `docs/privacy.html`
in the same change.

---

## 13. Coverage

Proof-of-concept: **NSW — Ausgrid**. Endeavour, Essential, and other states in progress.
VPP bolt-on programs and Controlled Load are designed and schema-live in production, with
real-data population partway through — see `VPP_CONTROLLED_LOAD_HANDOFF.md` before touching.

---

## 14. File map

```
custom_components/grid_lens/
├── __init__.py              entry setup, _CARD_VERSION, _build_seed_views (dashboard seed)
├── const.py                 CONF_* keys, parse_hours_spec
├── config_flow.py           setup + options (reconfigure) flows
├── plan_calculator.py       plan cost engine
├── retailer_plans.py        plan fetch/cache from the API
├── battery_optimizer.py     the LP/MILP
├── sensor.py                comparison sensors + deferrable_loads attribute
├── plan_sensors.py          per-plan metric sensors
├── switch.py                battery control + per-device control & greedy switches
├── select.py                Force On/Off/Auto override
├── number.py                Today Boost, Minimum Export Price
├── services.py/.yaml        set/clear schedule, calculate_period
├── schedule_grid.py         7x48 grid helpers (slot_allowed, week_from_hours)
├── deferrable_schedules.py  schedule Store
├── deferrable_overrides.py  boost Store
├── load_estimation.py       LoadEstimator + EstimateStore — synthetic energy sensor for an
│                            unmonitored controllable load (aircon w/ no feedback), §5
├── load_estimate_math.py    pure sample-accept/EMA/integration logic behind LoadEstimator
├── entity_lookup.py         device name / power sensor auto-discovery
├── advisory/                forecast → LP → dispatch plan + sensors
├── control/                 executor, battery + load control managers, controllers
│   └── modulating_controller.py  type-2 EV-charger current setpoint control, §6a
├── inverters/               HAL: base.py contract, sigenergy_mqtt.py driver
├── tests/                   offline suites (no HA/scipy needed — `python3 tests/<f>.py`)
└── www/cards/               all Lovelace cards + grid-lens-chart-common.js
```

**Tests.** Every suite runs offline with stubbed HA: `python3 tests/test_*.py`. Run them all
before shipping — they're the only automated safety net in this repo.
