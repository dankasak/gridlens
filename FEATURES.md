# Grid Lens — Feature Reference

**What this doc is:** the *current state* map of everything Grid Lens does — one entry per
user-facing feature, with the entities it creates, the config it needs, the files that
implement it, and the gotchas. Read this to understand **how the product works**.

**What it is not:** a history. `docs/GRIDLENS_CHECKLIST.md` is the append-only record of *what
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

**Retailer filter (`grid-lens-card.js`, 2026-08-28).** A type-to-filter search box in the
comparison toolbar, with a `<datalist>` of the retailers actually present so it suggests
"EnergyAustralia" rather than making the user guess the spelling, plus an "N of M" count
while a filter is active. Escape or the native ✕ clears it. Added because the NSW catalogue
reached 93 plans and the page became unreadable.

It **hides DOM nodes rather than re-rendering** — re-rendering on each keystroke would
replace the `<input>` being typed into and lose focus and caret position every character,
and the streaming `plan` events already re-render the card once per plan priced (`render()`
restores focus if the box had it). Filtering is purely presentational: nothing is removed
from `this._data`, so chart scaling stays global across all plans and the History panel
still sees every one. Skeleton placeholders hide while a filter is active — their retailer
is not known yet, so showing them would claim a match that cannot be supported.

⚠ **The retailer is the segment before the FIRST `" - "`.** Plan names contain the
separator too (`Alinta Energy - Standing Offer - Time of Use`), so splitting on the last
one, or on every one, gets it wrong. Verified against all 93 live keys: 18 retailers
parsed, matching the API's own 18 exactly, 0 malformed.

**Plan keys must be unique** — `plan_costs` and `plan_details` are dicts keyed on
`"{retailer} - {plan_name}"`, so two plans sharing one display name means the second
overwrites the first. `_duplicate_plan_keys()` / `_plan_key()` in `plan_calculator.py`
suffix the slug onto any contested key as a structural guard; the plan data itself carries
the tariff variant (`Residential Netflix Plan (Single Rate)`) so the guard stays dormant.

**Network-tariff-code matching (2026-09-01).** Some plans are only valid on a specific DNSP
network tariff — e.g. ENGIE's "VPP Advantage" exists as four separate stored plans
(`engie_vpp_advantage`, `_ea111`, `_ea011`, `_ea025`), each restricted to one specific Ausgrid
network tariff per its own fact sheet, with identical VPP/FiT terms but different supply
charge, TOU rates, and demand-charge status. A plan carries this as
`eligibility.required_network_tariff_codes` (comma-separated, e.g. `"EA116"`; `null`/absent
= no restriction). The household enters their own code(s) — read off their bill, the same
way this install's own network tariff was confirmed — via the "Network tariff code
(optional)" field on the Current Plan step (initial setup **and** Reconfigure). Blank/unset
means "don't know", which disables the filter entirely: every plan (gated or not) stays in
the ranking. Once set, `calculate_plan_costs` drops any candidate plan whose required
code(s) don't intersect the household's, **except** the plan the household is actually
detected as being on (`_detect_current_plan`), which always stays priceable regardless of a
tariff-code mismatch. **Local-only**: the household's own code never leaves their HA
instance — it's not part of the `/register` payload or any other API call. A plan's own
required code is public catalogue data, not customer data.

**Files:** `plan_calculator.py`, `retailer_plans.py`, `sensor.py`, `plan_sensors.py`,
`const.py` (`CONF_NETWORK_TARIFF_CODES`, `parse_network_tariff_codes`), `config_flow.py`,
`www/cards/grid-lens-card.js`.

**Plan data** comes from the private `gridlens-api` (MySQL, temporally versioned —
`slug@date` rows). The HA side never sees another user's data and never sends usage data
out; the API only *delivers plan definitions*. See `PRIVACY_DATA_INVENTORY.md` in the API
repo.

**Rate structures modelled:** flat, TOU (multi-window, per-weekday), demand tariffs,
controlled load, tiered/capped rates (free-then-paid blocks), conditional daily
credits (e.g. "stay under X kWh in this window, get $1"), feed-in tariffs including
wholesale-linked ones, supply charges.

**Cap semantics — `cap_period` + `cap_application`** (`plan_rates`, added 2026-08-26).
`daily_cap_kwh` gives a cap's size; these two say what it means:
- `cap_period` — what the allowance is quoted per (`day` … `billing_period`). Defaults to
  `day`, which is what every plan was before.
- `cap_application` — `strict` is a hard limit inside each period. `pooled` means the
  allowance accrues across the billing period (allowance × days), so unused headroom banks.

Both exist in the market, on the *same* regulated product: AGL applies Solar Sharer's
24 kWh as "the first 24 kWh… **each day**", EnergyAustralia as "an **average** of 24 kWh
per day across your billing period". GloBird's step rates pool likewise. **Pricing a pooled
cap as strict understates the plan**, so this is money, not labelling.
The bill calculation pools exactly (`cap × days_in_period`). ⚠ The optimiser can only pool
across its 24–48h **horizon**, not a real billing month — strictly better than treating it
as strict, but it cannot bank allowance from last week. Bill line items follow suit,
reading "first 24 kWh/day avg" for a pooled cap.

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
a period GloBird actually billed $21.04 (`docs/GRIDLENS_CHECKLIST.md`, 2026-08-04).
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

**"Exclude Greedy Consumption" checkbox (added 2026-08-30).** In the Plan Comparison
toolbar (`grid-lens-card.js`, next to the retailer filter), **unchecked by default**. When
checked, each deferrable device's `daily_kwh` target — the number fed to the LP when
scoring *alternative* plans, `sensor_total / days` in `plan_calculator._get_deferrable_data`
— has its tracked Greedy Consumption energy (§7) subtracted first, via
`calculate_plan_costs(..., exclude_greedy=True)` → `?exclude_greedy=true` on
`/api/grid_lens/plan_stream` and `/plan_data`. Rationale: Greedy Consumption (§7)
opportunistically runs a device whenever the *current* plan makes energy momentarily free;
left in, that inflated average gets re-asked of every alternative plan as if it were
unconditional need, silently favouring whatever plan created the free windows the device
exploited. **Deliberately does not touch**: the current plan's own actual-bill total (always
real metered usage — see the previous entry), or the `combined`/base-load series used for
the non-deferrable "other load" hour-of-day average (that must keep reflecting real physical
energy flow regardless of *why* a device drew power). Has no effect on a period predating
the tracker (§7's "no retroactive data" caveat) — expected, not a bug.

**"Excludes Greedy Consumption" hazard stripe (added 2026-08-30).** While the checkbox
above is checked, every *alternative* plan's card (never the current plan) shows a
diagonal-stripe banner under its hourly charts (`grid-lens-card.js::_greedyStripeHtml`),
flagging that its profile/total are the adjusted estimate described above rather than this
household's literal usage on that plan. Purely presentational — no new data or config.

**Chart colour palette (re-picked 2026-08-30).** Every chart-series colour in
`grid-lens-card.js` (household/solar/buying/selling/spend/income/SOC + the per-device
`DEVICE_COLORS` rotation) and in `grid-lens-chart-common.js`/`grid-lens-power-chart-card.js`
(the Power Flow view's `--defer1-4`/`--free-spill`/`--free-import`) was re-validated with
the dataviz skill's `validate_palette.js` (OKLab CVD deltaE, `--pairs all`) after a user
report of near-duplicate colours on both pages — see `GRIDLENS_CHECKLIST.md` 2026-08-30 for
the specific clashes found (a literal hex duplicate, a red-vs-green pair only 1.2 ΔE apart
under deuteranopia) and the handful of pre-existing, lower-severity gaps left as documented,
accepted limitations rather than cascading the redesign into cross-card-shared anchors.

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

**Solver chain:** scipy `linprog`/`milp` → PuLP/CBC → greedy.

**scipy IS HiGHS.** `linprog(method="highs")` and `optimize.milp` both use it, with no
external binary. A second hand-rolled `highspy` path (`_lp_highspy`) existed until
2026-08-22 and was **removed, not repaired**: it was a parallel route to the same solver,
carrying its own copy of the model that had drifted from the real one, and it had been
raising `AttributeError` on every call for months. Don't reintroduce one —
`tests/test_import_bound.py` guards against it.

**Only the scipy path is complete.** `_lp_pulp` is battery-only: no deferrable loads,
demand charges, capped rates, conditional credits, min-export price or soft terminal SOC,
despite accepting a `deferrable_loads` argument and ignoring it. `_lp_optimize` gates on
every one of those and refuses PuLP outright for a deferrable horizon, so a fallback can
never quietly answer a different question than the one asked. **Do not relax that gate.**

**The greedy fallback also models no deferrable loads** (`_greedy_optimize` takes no such
argument). Any solver failure therefore doesn't just cost optimality, it changes the
question being answered — which is why a solve reaching greedy is worth investigating, not
tolerating. Check `solver=` in the log: `lp/scipy` or `lp/scipy-milp` is healthy.

**Grid-import bound (`_import_bound`).** The per-slot import ceiling, doubling as the
conditional-credit big-M. Must be sized from real demand — peak net load + all deferrable
draw + battery charging + 50% — because the energy-balance row is an equality and a bound
below genuine need makes the model **infeasible**, not merely suboptimal. Until 2026-08-21
it was derived from the battery's power rating alone, so a 5 kW/5 kW battery capped import
at 20 kWh/h and any bigger hour made every plan unsolvable. Kept finite: an unbounded import
lets a plan with FiT above its import rate farm unlimited arbitrage.

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
  - **Load-covering discharge in a free-import window** (`import_rate ≈ 0`) → **IDLE**, not
    self-consumption. Self-consumption is price-blind, so on a day where actual solar
    undershoots the forecast (cloud) it would drain the battery to cover load that
    equally-free grid import could have served for nothing. IDLE holds SOC and lets the
    shortfall fall through to import — the discharge-side mirror of the free-import charge
    branch above.
  - **Discharge with a material export component** → forced battery-first at the planned rate,
    even when import happens to be free (export earns money regardless of the import side).
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
`..._soc_sensors` (e.g. EV SOC). The parallel-list shape is what every consumer reads, but
nothing edits it directly any more: `deferrable_loads.py` presents it as one dict per load
and the config flow works through that (§12b). Availability windows are **not** set here — see §8; a device
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
`docs/GRIDLENS_CHECKLIST.md`, 2026-08-06); the config-flow step now rejects that combination
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

**Estimator observability** (added 2026-08-17) — every measured observation (accepted *or*
rejected, up to the last 40) is persisted as `sample_history` on the estimator
(`load_estimation.py::_record_sample`, restored across restarts) and exposed via `status()`,
not just the single most-recent sample kept before. The Load Control card (§10/§11) reads it:
any row backed by an estimator (either an unmonitored load above, or the power-only case
below) gets a small toggle that expands a per-device panel — current estimate/seed/sample
count/calibration source, a convergence chart of the estimate over time, and a list of the
most recent accept/reject decisions with why (`implausible`, `contaminated`, own-meter
`too_short`/`counter_reset`). This exists because a rejected or corrupted sample previously
had no surface at all beyond a DEBUG log line — the 2026-08-05 flap-corruption bug (a
spurious 2987W reading from an unavailable/unknown control-entity blip mistaken for a real
off→on transition) would have been visible immediately on this panel instead of only in
retrospect via the log.

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

**SOC ceiling (added 2026-08-29)** — for a device with the `..._soc_sensors` field above
also set, two more per-device fields become meaningful: `deferrable_load_soc_max_percent`
(default 100 = no cap) and `deferrable_load_soc_capacity_kwh` (default 0 = not provided,
same as no cap). Together they let the **live advisory/control path only** (never the
plan-comparison backtest, which has no "now") stop scheduling further charge once the
device is close enough to the configured ceiling, freeing that energy for other deferrable
loads or export — e.g. an EV normally charged to 90% for battery longevity no longer gets
pushed to 100% just because its 14-day average says it usually needs that much.

Mechanically: `advisory/coordinator.py::_deferrable_for_horizon` reads the sensor's LIVE
state each tick and, if both fields are set and the reading is valid, adds
`soc_capacity_kwh` / `soc_initial_percent` / `soc_max_percent` to that device's dict passed
to the LP. `battery_optimizer.py` then gives that device a real SOC state variable — but
**only across today's slots (day 0 of the horizon)**, not the whole multi-day horizon: this
model has no forecast of the device's own energy consumption between charges (an EV's
driving, specifically), so a naive "reach the ceiling every day" constraint would go
infeasible the moment a later day started already full. Day 1+ stays on the plain flat
`daily_kwh` mechanism, unchanged — acceptable because this is a rolling-horizon optimiser
that re-solves every ~2 minutes, so a future day's schedule is always recomputed with fresh
SOC data before it's ever acted on. Day 0's target is still a floor (charge at least the
usual daily amount) clamped by real headroom under the ceiling, not just a hard cap — so a
device far from its ceiling keeps its normal behaviour.

Exposed for observability (no dashboard card built for this yet — a natural follow-up):
`ev_soc_status` on the LP result / `AdvisoryResult` — one dict per SOC-tracked device
(`name`, `sensor_id`, `initial_percent`, `max_percent`, `day0_final_percent`,
`day0_charge_kwh`). A clamp caused by the ceiling (as opposed to the availability window)
is tagged `reason: 'soc_ceiling'` in the existing `deferrable_clamped` notice and logged
with its own message so it doesn't read as "widen your weekly schedule" (the availability-
window clamp's advice, which wouldn't fix an SOC-ceiling clamp).

**Files:** `const.py` (`CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT` /
`CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH`), `config_flow.py` (the wizard's `load_soc` step,
reached only when a load is marked as having its own battery — §12b), `plan_calculator.py` (`_get_deferrable_data` — static config passthrough only),
`advisory/coordinator.py` (`_deferrable_for_horizon` — the live reading),
`battery_optimizer.py` (`ev_soc_idx`/`ev_soc_specs` in `_lp_scipy`), `advisory/planner.py` +
`advisory/models.py` (`ev_soc_status` passthrough).

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
- **The advisory forecast respects the override too** (`advisory/coordinator.py`'s
  `_deferrable_for_horizon`) — Force Off zeroes the device's per-slot availability mask
  for the whole horizon (it genuinely cannot run until Auto is reselected), Force On
  opens every slot regardless of the painted weekly schedule. Without this the LP kept
  planning the device's normal schedule while it sat physically forced off, and the
  Power Chart card's forecast line (drawn straight from `sensor.*_planned_dispatch`'s
  `trajectory`) showed a charge that was never going to happen — reported live
  2026-08-12 (EV charger left on Force Off overnight after a manual "drive it now").
  Scoped to the live advisory/dispatch plan only, not plan-comparison's LP (a
  temporary override on the device you actually own shouldn't bias how an alternative
  retailer plan is ranked).
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
| 3 | **Forecast surplus** | Over a 4 h look-ahead, the plan expects to waste **more free energy than this device could consume running flat out for that whole window** — **and** the battery currently has enough SOC/discharge headroom to actually supply the device (see below). | No — see below |

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

**Why #3 is safe (added 2026-08-31: battery-headroom gate).** #3 used to be a genuine bet —
it could fire purely off the forecast, with no live check, and turning a device fully on when
the battery is flat and there's no live solar is real, unbuffered grid import. It is now
additionally gated on live battery headroom
(`LoadControlManager._battery_headroom_w`, backed by the same `battery_soc_sensor` /
`battery_charge_power_sensor` / `battery_min_soc` / `battery_max_discharge_rate` config the LP
optimiser already uses): the forecast bar clearing is necessary but no longer sufficient — the
battery's current SOC must also be above its configured minimum, with enough free discharge
rate (rated max minus whatever it's already discharging) to cover this device's full draw
right now. Only then does running the device draw the battery down instead of the grid, with
that hole refilled later by the very spill the forecast is betting on. **No battery configured,
or an unreadable SOC/charge sensor, means no buffer exists — the condition fails closed and
never fires**, same discipline as conditions #1 and #2's missing-sensor handling. Recorded as
`greedy_blocked = "no_battery_headroom"` when the forecast alone would have fired (see below) —
distinguishing "the forecast hasn't cleared yet" from "the forecast cleared but the battery
can't safely supply it right now".

**What counts as "free energy the plan will waste"** (`LoadControlManager._forecast_free_kwh`):
- **Spilled export** — a slot with `export_rate ≤ 0` that the plan still exports into. Uses
  `DispatchInterval.total_export_w` (whole-house export, PV spill included) — *not*
  `export_w`, which is only the battery's share of a discharge slot and is 0 on a pure
  solar-spill slot. Already net of every load the plan schedules, so nothing is subtracted.
- **Unused free-import window** — a slot with `import_rate ≤ 0`; only the part the plan does
  *not* already run this device counts (`max_w − planned_w`).

**Fail-closed everywhere.** Unknown rate, unavailable sensor, a plan covering less than half
the look-ahead, or (condition #3 only) missing/unreadable battery SOC or charge-power sensors
→ the condition contributes nothing rather than guessing. (The forecast bar scales with the
covered span, so a sliver of horizon tail would otherwise shrink it until a trivial surplus
cleared it.)

**Config:** the export-surplus condition needs `grid_power_sensor` — a **signed live power**
sensor, positive = importing, negative = exporting. Without it, condition #2 simply never
fires; #1 still works, and #3 works only if its own battery-headroom gate can be satisfied
(see above). Note this is a *power* sensor: the Energy-dashboard sensors (`energy_sensor`,
`solar_sensor`, `grid_export_sensor`) are cumulative kWh and cannot serve.

The forecast-surplus condition's battery-headroom gate needs `battery_soc_sensor` (%) and
`battery_charge_power_sensor` (**signed live power**, positive = charging, negative =
discharging) — the same battery config the LP optimiser already uses (`plan_calculator.py`),
not a control-specific duplicate. `battery_min_soc` (default 10%) and
`battery_max_discharge_rate` (kW, default 5.0) round it out. Without both sensors configured
and readable, condition #3 never fires at all — there's nothing wrong with running with it
off, it just means the household hasn't given GridLens a way to confirm the bet is safe.

⚠ **`grid_power_sensor` could be silently DESTROYED by a reconfigure, and the loss was
invisible.** Found 2026-08-28: ~5 kW exported for two hours at $0 with the 1.9 kW EV charger
sitting off. The field had been set correctly and was wiped by the reconfigure wizard.

*The data-loss mechanism* (`config_flow.py::GridLensOptionsFlow.async_step_sensors`): every
key in `_ENERGY_SCHEMA_KEYS` was re-asserted as `user_input.get(key) or None`, because a
cleared `EntitySelector` submits *absent* rather than `None`. But an `EntitySelector` seeded
via `suggested_value` with an entity id that doesn't currently resolve **renders empty** — and
an untouched empty picker also submits absent. The two are indistinguishable, so a transient
condition (the inverter integration hadn't finished loading when the wizard was opened, an
entity was renamed) became a permanent deletion of a setting the user never touched, on a step
they only walked through to reach something else. Now: absent + a seeded value the picker
*could* render == cleared (honoured); absent + a seeded value that doesn't resolve == not
answered, keep what is stored (and log a warning).

*Why nothing caught it.* `grid_power_sensor` had **zero** config-flow test coverage, and the
options flow had no test coverage at all. `tests/test_config_flow.py` now covers both halves
of the clear rule plus discovery, and the preserve test was confirmed to fail against the old
code before being kept.

**It is now auto-discovered** (`_discover_grid_power_sensor`) from the install's own Power Flow
card `grid_power_entity` — the same fact, the same sign convention, already answered by the
same person. It is *not* guessed from entity names: "a power sensor with 'grid' in the name"
would happily match an unsigned import-only register, and greedy would then read a positive
import as "not exporting" forever. Silently wrong beats visibly absent. Note the HA Energy
dashboard cannot supply this field — it stores cumulative energy statistics only, never a live
power entity — which is why it is the one energy field that starts blank.

Three further changes make an empty field visible rather than silent:
- `greedy_blocked = "no_grid_power"` is recorded whenever the export price is ≤ 0 and the
  grid reading is missing/unavailable, so the state is published, not inferred.
- `greedy_blocked = "no_battery_headroom"` is recorded whenever condition #3's forecast bar
  has cleared but the battery-headroom gate above blocks it — same "publish it, don't let it
  read as silently inert" reasoning.
- The Load Control card renders that case as *"Greedy: export is free, but no grid power
  sensor is set"* instead of the misleading *"armed, waiting for free energy"*.
- `LoadControlManager` logs a one-shot **warning** (not debug — a debug line is invisible on
  the default install this happens on) naming the device and the fix.

**Observability** — see §11.

**Tuning knob:** `GREEDY_SURPLUS_LOOKAHEAD_HOURS = 4.0` in `load_control_manager.py`.

**Greedy energy tracking (added 2026-08-30).** A per-device `sensor.*_<device>_greedy_consumption`
entity — cumulative kWh the device drew while any of the three conditions above were
actually driving it, as opposed to the plan or a manual command. Feeds §1's "exclude Greedy
Consumption" plan-comparison option: without this, the `daily_kwh` figure fed to the LP for
every *alternative* plan is inflated by whatever a device opportunistically ran only because
the *current* plan happened to offer a free window — biasing the comparison toward the plan
that created the free energy in the first place.

**Files:** `greedy_energy_math.py` (pure accumulate/counter-reset logic, mirrors
`load_estimate_math.py`'s split), `greedy_energy.py` (`GreedyEnergyStore` +
`GreedyEnergyTracker` — persists via its own Store, same "manager persists, entity just
displays it" split as `load_estimation.LoadEstimator`), `sensor.py`'s
`GridLensGreedyEnergySensor`, `__init__.py::_ensure_greedy_trackers` (wiring — runs *after*
`LoadControlManager` is built, unlike `_ensure_load_estimators`, since eligibility and the
live `greedy_reason` read both come from `LoadControlManager.controllers`).

One tracker per device index that has both a real/synthetic energy sensor
(`deferrable_load_sensors[i]`) **and** a controller in `LoadControlManager.controllers` —
forecast-only and declared/"dummy" loads never qualify, since nothing decides on/off for
them so Greedy Consumption could never have driven them. Applied uniformly to on/off and
modulating (§6a) controllers alike: on every change of the device's own energy sensor, the
tracker attributes the *entire* delta since the last reading to "greedy" whenever
`controller.greedy_reason` is truthy at that moment. This is a deliberate, coarse
approximation — a modulating device's current can be a live blend of plan-driven and
surplus-boosted power, and this tracker doesn't attempt to split that blend, only to decide
whether *any* greedy influence was present. Same counter-reset guard as `LoadEstimator`'s
own-meter sampling (a device reboot resetting its energy counter is discarded, not
subtracted).

⚠ **`greedy_reason` must mean greedy was the actual reason — on/off devices only so far
(fixed 2026-08-31).** Before this fix, `DeferrableLoadController.apply()` set
`greedy_reason` purely from whether a greedy condition matched this tick, with no check for
whether the plan *itself* already wanted the device on. A switch-controlled EV charger
(§6, `switch.*`/`climate.*` devices) sitting inside its scheduled plan window — one the LP
would have turned on at the same slot with or without any spare solar — still got 100% of
its consumption tagged greedy the moment forecast surplus (or either of the other two
conditions) also happened to be true, silently inflating what §1's "exclude Greedy
Consumption" checkbox subtracts and biasing the alternative-plan comparison toward whichever
plan created that coincidence. Now `apply()` clears `greedy_reason` whenever
`desired_on(planned_w)` — the plan alone — already wanted the device on this slot, so the
device's own invariant holds: None means the plan is why it's on, greedy or not.

Deliberately **not** applied the same way to `ModulatingLoadController` (§6a, OCPP-style
setpoint devices). Its `greedy_reason` is read raw by
`LoadControlManager._modulation_target_w` to decide the forecast-surplus boost — clearing it
whenever the plan wants *any* nonzero power would starve that decision (a device charging at
a modest plan-driven rate for most of its window would never see the boost even when the
forecast genuinely clears its bar), and a `_modulation_source`-gated property override was
tried and reverted: apply() and modulate() run on separate clocks (5 min vs 30 s), so
`_modulation_source` isn't populated at the moment `status()`/tests read `greedy_reason`
straight after an `apply()` tick, breaking the existing "export surplus bar is min for
modulating" test's expectation of an immediate read. The pre-existing over-attribution this
leaves in place for modulating devices — a device already drawing plan-driven power that
greedy also tops up within the same tick still gets the *whole* delta tagged greedy, not
just the topped-up portion — remains a known, documented simplification (see above), now
joined by this one: a modulating device already fully covered by the plan can still show a
`greedy_reason` it isn't the actual cause of. Splitting that correctly needs threading the
manager's `plan_w`/`surplus_w` split (already computed in `_modulation_target_w`) down into
the controller or tracker, which is a larger change than this fix.

⚠ **Tracked going forward only — no retroactive data.** The tracker starts at 0 kWh
whenever a device first qualifies; a plan comparison over a period that predates this
feature (or predates the device being configured) has nothing to exclude, and the §1
checkbox will silently produce the same result as unchecked for that period. This is
expected, not a bug — there is no way to know retroactively which past energy was greedy.

**Power chart hatch (added 2026-08-30).** The Power Flow view's power chart
(`grid-lens-power-chart-card.js`) shades the *measured* portion of a deferrable device's
line/area with diagonal stripes wherever this tracker's sensor shows it was actually
greedy-driven, using the tracker's own colour so it never gets confused with the plan's
free-energy bands above. Sourced from `sensor.py::_build_deferrable_loads`'s new
`greedy_energy_entity` field (joined the same way `power_entity` already is) — history is
fetched separately from the base class's power/SOC fetch (`_fetchGreedyBands()`, riding
its same throttled cadence) since a cumulative counter needs delta-between-samples logic,
not the "read as an instantaneous reading" treatment every other actual series gets. A
device with no controller (forecast-only/declared) has no tracker and so is never hatched
— consistent with the tracker itself never existing for it (see above). Hovering a hatched
stretch appends "(greedy)" to that device's tooltip row; the legend only advertises the
hatch when one is actually in view (same pattern as the free-energy bands' own legend
entries).

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

**Unpainted-aircon nudge** (added 2026-08-11). "Fully unrestricted until painted" is a
reasonable default for a pool pump or EV charger, but a worse trap for a `climate.*`-
controlled device (aircon, added §5/§6, 2026-08-02): comfort, not price, decides when it
needs to run, so an unpainted one lets the LP assume it can shift a whole day's runtime to
3am — in plan comparison *and* the real dispatch/control plan, since it's the same LP
(§0/§2). `_notify_unpainted_climate_schedules` (`__init__.py`, runs once per setup/reload
right after the schedule store loads) fires a persistent notification naming every
`climate.*`-backed deferrable device with no stored weekly grid, pointing at the Deferrable
Loads dashboard card; it self-clears once every such device has a schedule painted. This
only prompts — it doesn't change LP behaviour or force a default schedule, and a
`switch.*`-controlled device (pool pump, EV charger) is never flagged.

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
| `grid-lens-powerflow-card` | **Gated** — live radial energy flow: solar / grid / battery / home + one node per deferrable load, animated flow balls, live buy/sell price, greedy badges. Requires the Battery Control + Power Flow add-on; see §12. `load_power_entity`/`grid_power_entity`/`battery_power_entity`/`battery_discharge_power_entity` are auto-populated in the seeded dashboard straight from the same `load_power_sensor`/`grid_power_sensor`/`battery_charge_power_sensor`/`battery_discharge_power_sensor` config_flow already collects (Sensors/Battery setup steps) — no separate onboarding needed; `solar_power_entity` auto-discovers from HA's own Energy Dashboard prefs; `ev_power_entity`/`ev_active_entity` remain manual-only (no config_flow counterpart — only needed when the EV isn't already represented as a regular deferrable load). |
| `grid-lens-power-chart-card` | Measured & forecast power (kW) — solar, load, signed grid, signed battery, per-device deferrable, plus free-energy shading, **plus battery SOC on a right-hand 0–100% axis** (2026-08-28). Click a legend name to isolate that series (forecast + measured pair, y-axis rescales to it); click it again to restore every series. SOC is exempt from isolation — it sits on its own axis, so keeping it costs the kW rescale nothing and it is context for whatever you isolated. |
| `grid-lens-price-chart-card` | Import/export rate trajectory. |

**Secondary axis (`multiLineChart`, `opts.rightAxis` + `series[].axis: 'right'`).** Added so
SOC could share the Power Flow chart. A right-axis series is excluded from the left axis'
min/max — otherwise a 0–100 percentage stretches a kW axis to +100 and flattens every real
flow — and never draws an area fill, because the fill baseline is the *left* axis' zero and
means nothing on a percentage scale. Right-axis series draw last, above every wash.

⚠ **The hazard of a secondary axis is reading a value off the wrong scale**, and the power
chart is `symmetric: true` (0 kW at the vertical centre, since grid and battery are signed),
so 50% SOC sits exactly on the 0 kW line. Mitigated by making SOC unmistakably its own
thing: a dedicated `--soc` hue used for the curves *and* the right-hand ticks, axis line and
`SOC` caption; no area fill when everything else has one; the heaviest stroke on the chart;
and no right-hand gridlines, so the horizontal rules keep meaning the left axis only.
Planned is dashed and measured solid — the same shape language the standalone SOC card uses
— rather than two new hues on a chart already carrying ten.

Callers passing no `rightAxis` are byte-for-byte unchanged (verified against the previous
`multiLineChart` across five series shapes with the clock frozen).

| `grid-lens-soc-chart-card` | Battery SOC curve, planned vs measured, full height. Kept alongside the Power Flow chart's SOC overlay on purpose: the overlay is at-a-glance context next to dispatch, this is the divergence diagnostic for whether control is actually tracking the plan. |
| `grid-lens-cash-chart-card` | Cumulative cost/credit. |
| `grid-lens-dispatch-chart-card` | Planned EMS mode timeline. |
| `grid-lens-advisory-card` | Plan status header (plan name/solver/last-run time, status badge), control-mode timeline, deferrable-load recommendations. `compact: true` config renders just the header — used as a slim "optimiser & plan" status bar at the top of the Power Flow view; `title` config overrides the header text in that mode. |
| `grid-lens-load-control-card` | One row per deferrable load: Today Boost, greedy toggles, Off now / On now / Auto, and live greedy status. |
| `grid-lens-defer-schedule-card` | The 7 × 48 allowed-run-times editor. |
| `grid-lens-flex-row-card` | Layout helper — per-child `flex` control, stacks below a breakpoint, and collapses children that hide themselves (native `conditional` cards) out of the row. |

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
Views, in order: **Power Flow, Battery Plan, Settings, Plan Comparison**. Power Flow is
first deliberately — HA opens a dashboard on its first view, so that's the landing page;
Plan Comparison sits last as the occasionally-revisited "should I switch retailer?"
screen (reordered 2026-08-20, user request).

**Power Flow view** (split out of Battery Plan 2026-08-20, user request) — the
`grid-lens-powerflow-card` diagram + `grid-lens-power-chart-card`, with the compact
`grid-lens-advisory-card` status bar (see table above) pinned at the top so "when did
the optimiser last run" is visible without switching to the Battery Plan view.

**Power Flow layout toggles** (added 2026-08-20, user request). Two entities —
`switch.*_show_scene_power_flow` and `switch.*_show_classic_power_flow`
(`EntityCategory.CONFIG`, `RestoreEntity`-persisted, defined by `_POWERFLOW_LAYOUTS` in
`switch.py`) — independently show/hide a `scene` and a `classic` instance of the Power
Flow diagram. **Both can be on at once**, rendering side by side with scene on the left.
Defaults: classic ON, scene OFF (classic is the low-CPU option — a fresh install
shouldn't decode scene video without opting in). Ordinary switches, so an automation can
drive them too (e.g. scene only on the wall tablet in the evening).
- The seed wraps each diagram in a native `conditional` card keyed to its switch, inside
  the `flex-row-card`. **`flex` goes on the conditional wrapper, not the diagram** — the
  flex-row card lays out its direct children, which are the wrappers.
- `grid-lens-flex-row-card` collapses a child that hides itself (`el.hidden`) out of the
  row entirely — needed because it sets an inline `display:block` on every child, which
  otherwise beats `[hidden]{display:none}` and leaves a blank gap holding its flex basis.
  Uses a `MutationObserver` on `hidden` as well as syncing on `set hass`, since a
  conditional card flips `hidden` when *it* receives hass, which can land after ours.
- **With both layouts on, the power chart moves to a full-width line underneath them**
  rather than squeezing three across. Driven by the flex-row card's
  `own_line_when_siblings: N` per-child option (seed sets `2` on the chart), evaluated
  against *live* visibility — so it reflows as the toggles change, not baked into the
  seed. Implemented by nesting the non-own-line children in a nowrap `.group` and
  switching only the own-line child's basis to 100%; the main row deliberately still
  doesn't use `flex-wrap`, which this card abandoned early on because wrapping keys off
  flex-*basis* rather than post-shrink size and broke rows far too eagerly.
- The toggle chips are rendered by `grid-lens-advisory-card`'s `layout_toggles` config
  (a generic list of `{entity, label}` — nothing Power-Flow-specific about it; works in
  the full card too, not just `compact`).
- **Sizing differs per layout on purpose** — don't copy one's numbers onto the other.
  Classic renders square, so `max_width` (550) is what sets its size and `max_height`
  (780) is just a non-clipping ceiling; the §10 "move all three together" gotcha is about
  not distorting that square. Scene is pinned to its background's aspect ratio instead
  (shipped v9 cabin is 1360×752 → ~900×498). The scene instance also sets
  `show_labels: false` — its photoreal elements are themselves the indicators, so
  overlaid name/value text just clutters the artwork; classic keeps its labels since it
  has no other way to identify a node. Nodes stay tappable either way.
- `show_ev` is **derived, not hardcoded**: the dedicated EV satellite node is suppressed
  when any deferrable load has an SOC sensor configured (that field exists for an EV
  charger — §5), since the vehicle is then already drawn as a load node and would appear
  twice. An install whose EV isn't a deferrable load still gets the satellite.

**Battery Plan view** — the status tiles (Now/SOC now/Planned end/Plan net cost), the
full (non-compact) `grid-lens-advisory-card`, and the SOC/dispatch/price/cash forecast
charts. No longer includes the Power Flow diagram (moved to its own view above).

**⚠ Seed the dashboard THROUGH its live `LovelaceStorage`, not a bare `Store`.**
`_register_dashboard` runs on `EVENT_HOMEASSISTANT_STARTED`, by which point the lovelace
component has already built a `LovelaceStorage` for each registered dashboard — and that
object caches its config in memory on first load. A dashboard whose store file didn't exist
at startup (the first-run case) has already cached "no config", so writing the seed file via
a separate `Store` leaves the stale cache serving an **empty dashboard for the rest of the
session**: HA renders the title plus an untitled "New section" placeholder, with **no log
line and no browser-console error**. Fixed 2026-08-20 by calling the live dashboard's own
`async_save()` (which updates the cache and notifies listeners) when one exists, falling back
to the raw store only when there's no live object yet. Every new install previously got a
blank Grid Lens dashboard until its next HA restart. Symptom is indistinguishable from a
corrupt/rejected config — if a seeded dashboard ever looks blank again, check whether the
on-disk file is correct *and* whether anything wrote it behind the live object's back, before
suspecting the seed content.

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
| `switch.*_<device>_control` attributes | Commanded state, threshold, override, all three greedy toggles, **`greedy_reason`**, **`greedy_blocked`**, **`forecast_free_kwh` / `forecast_needed_kwh` / `forecast_battery_headroom_w`**, note. Modulating devices add `control_type`, `setpoint_entity`, `min_w`/`cap_w`, `commanded_w`/`commanded_setpoint`, `plugged_in`, `last_write`, `modulation_source`. |
| **Load Control card** | Per row: control state, and a live greedy line — the firing reason, or why it's blocked (including **"export is free, but no grid power sensor is set"**, §7 — the only blocked state that will *never* clear on its own, so it names the fix rather than reading as "not yet"), or the **forecast-surplus progress bar** (`6.2 / 8.0 kWh`, hover/focus tooltip explains it). Shown both while armed and tracking toward the trigger, and after it's fired (condition 3 held it on) — the same bar, capped at 100%, rather than only appearing pre-trigger. For a modulating device (§6a): live amps + kW, the max-current ceiling input, and a one-line "why" — `modulation_source` (plan / surplus / override / off) and `plugged_in`. "Why is my car charging at 8 A right now?" must be answerable from the row. |
| **Load Control card → Estimator panel** | Per-device toggle (rows backed by a `LoadEstimator`, §5, only) expanding: current estimate/seed kW/sample count/calibration source, a convergence chart of the estimate over time, and the last 8 accept/reject decisions with why (`implausible`, `contaminated`, own-meter `too_short`/`counter_reset`). "Why does this estimate look wrong?" must be answerable without `ha core logs`. |
| **Power Flow card** | A badge on a load node while *greedy*, not the plan, is holding it on — leaf for the two instantaneous reasons, sun-alert for forecast surplus, with the kWh figures in the tooltip. |
| **Power Chart card** | Free-energy time bands: **orange = free energy being wasted** (plan exports into a ≤$0 export price), **teal = free import window**. Legend appears only when a band is in view; the crosshair tooltip names the band. |
| `ha core logs` | Every optimiser run logs horizon, device count, solver status, credits, caps, export floor. |

**⚠ Read live control state from the control SWITCH, not the `deferrable_loads` sensor
attribute.** That sensor is a `CoordinatorEntity` tied to the plan-comparison run, so its
attributes only refresh when a comparison lands — while control state flips on the 5-minute
tick. The switches are plain polled entities and are the only surface that tracks live.

---

## 12. Account, tiers, entitlement

⚠ **Withdrawing a file from `www/` does not remove it from existing installs, and
`/grid_lens` serves that whole tree.** An in-place update — HACS, or `sync-to-ha.sh`'s
`cp -r` — copies files in and never deletes. `grid-lens-powerflow-card.js` was moved to
`gridlens-api` behind `PowerflowIconView`'s entitlement check on 2026-08-02, but every
install that updated across that commit kept the pre-gating copy at
`/grid_lens/cards/grid-lens-powerflow-card.js`, **outside the gate and reachable by anyone
with the URL**. Found on the dev rig 2026-08-28, still dated Aug 2. The Lovelace resource
had correctly pointed at the gated `/api/` path the whole time — nothing referenced the old
file, it was simply still on disk being served.

`_WITHDRAWN_WWW_FILES` in `__init__.py` lists exact relative filenames the integration once
shipped and must never serve again; `_prune_withdrawn_www_files()` deletes them **before**
the static path is registered, so there is no window in which one is reachable, and logs at
`warning` — the only signal an install was ever exposed. Exact filenames only, never globs
or paths, with an `is_relative_to()` guard, because this unlinks from the user's filesystem.

**Gating a card is therefore a two-part change:** move it to `gridlens-api/app/cards/` *and*
add its old `www/` filename to `_WITHDRAWN_WWW_FILES`. Doing only the first leaves every
existing install serving it ungated. Covered by `tests/test_withdrawn_www_prune.py`.


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

## 12a. Setup (the config flow)

**What it does.** Gets a new install to its first plan comparison in as few answers as
possible, and pushes everything with a sane default into the options flow instead.

**Screens, in order** (`config_flow.py`, `GridLensConfigFlow`):

| Step | Asks | Shown when |
|---|---|---|
| `user` | State, email | Always |
| `distributor` | Network | Only if >1 network in that state has plan data |
| `sensors` | Grid import (required); solar, export, grid power, import/export price (optional) | Always — pre-filled from HA's Energy dashboard |
| `battery` | Has battery, capacity, max charge/discharge | Always; checkbox pre-ticked if the Energy dashboard has a battery source |
| `devices` | Which Energy-dashboard appliances are deferrable | Only if the Energy dashboard lists device_consumption entries |
| `device_power` | Max power + optional control entity, per device | Only if a device was selected |
| `current_plan` | Current plan, demand tariff, VPP program | Always — then registers with the API |

A minimal install (no battery, no dashboard devices) is **four screens**. The most complex
is seven.

**Coverage gate.** `_load_coverage()` runs on the first submit: one `/plans/list` call per
candidate network in the chosen state, concurrently. It decides three things at once —
whether to abort (`state_not_supported`, no plan data anywhere in that state), whether to
skip the distributor screen (exactly one covered network), and what the final step's plan
dropdown contains. Because the plan list is prefetched here, `current_plan` does no plan
I/O and its dropdown can never be empty. **Before 2026-08-21 there was no gate**: a user
outside NSW/Ausgrid filled in every screen and hit a required dropdown with zero options,
no error and no way forward.

**What setup deliberately does *not* ask** — all of it lives in the options flow
(`GridLensOptionsFlow`; deferrable-load detail in the per-load wizard, §12b, everything else
under **Reconfigure everything**) and all of it has a default that is right for most
installs:

- Controlled Load 1/2 (defaults false) — a DNSP/meter fact most people can't answer offhand,
  and it only gates a dropdown on the advanced load steps.
- Inverter brand/transport — auto-detected via `detect_inverter_brand()` when a battery is
  declared, otherwise left unset. Battery control is a separately-entitled add-on that ships
  default-off (§4), so the honest time to ask is when it's switched on.
- Battery round-trip efficiency, min SOC, max SOC (`_BATTERY_ADVANCED_DEFAULTS` = 95%/10%/90%,
  written explicitly into the entry because the optimiser and guardrails read those keys).
- Per-device: climate on-mode, SOC sensor, controlled-load register, in-aggregate flag, and
  the whole modulating-control set (setpoint, unit, phases, voltage, min current, plug
  sensor) — §6a. Stored as aligned blank lists so downstream `zip`s stay index-safe.
- Declared and estimated loads (§5) — both advanced. All three load kinds are now set up
  in the per-load wizard, §12b.
- API URL — now on the options flow's **API key & connection** step, where self-hosting
  belongs.

**One entry per install.** `single_config_entry` in `manifest.json` — a second entry means a
second coordinator against the same inverter, which is a real hazard once a battery is
declared. A second Add Integration now aborts on `single_instance_allowed`. This also rules
out a genuine multi-state install; nobody has asked for one, and the safety case wins until
somebody does.

**Upgrade pitch.** Setup no longer ends on a blocking `async_external_step` redirect to
gridlens.au/subscribe (which also silently did nothing on installs with no external/internal
URL). `async_step_finalize` creates a persistent notification (`{DOMAIN}_upgrade`) instead.
**Note:** the `/api/grid_lens/subscribe_callback` view and its `pending_subscriptions` dict
in `__init__.py` are now unreferenced by the config flow.

**Reinstall (409) — solved locally.** `/register` is keyed on HA installation UUID, so
removing and re-adding the integration 409s. The integration mirrors its credentials into a
**global** `Store` (`grid_lens_credentials` — deliberately *not* entry-id-suffixed, so the
config flow can read it before any entry exists), written by `async_save_credentials()` on
every successful setup and backfilled on every `async_setup_entry` so installs predating the
feature are covered from their next restart. On 409 the flow calls
`_async_recover_api_key()`, revalidates the mirrored key against `/plans/meta`, and
continues silently — no re-entry, no support ticket.

A purely local fix suffices because **a 409 can only occur when `.storage` survived**: HA
keeps the installation UUID in `.storage/core.uuid`, so wiping `.storage` regenerates it and
`/register` simply returns 200. Same UUID ⟹ same `.storage` ⟹ the mirror is still there. No
`async_remove_entry` is defined, so nothing deletes it on removal.

The pitch in `async_step_finalize` is **tier-aware**: it is shown only to free accounts.
A fresh `/register` always mints a free key so the pitch is right there, but a recovered key
is frequently already paid, and telling a subscriber their "free account is locked to that
one plan" reads as *"you have no API key"* while every paid feature keeps working. The tier
comes from the `/plans/meta` response already being made to validate the recovered key, so
it costs no extra request. `manual_key` never showed this notification, which is why the
problem only appeared once recovery started routing through `finalize`.

`manual_key` remains as the fallback for the residual cases (mirror deleted by hand, key
revoked server-side, partially-restored backup) and still explains that only a hash is
stored and points at `support@gridlens.au`. Recovery is fail-safe: any unexpected condition
returns `None` and falls back to asking, because writing a stale key would produce an
install that looks configured and then 401s on every refresh.

**A user who lost their key still cannot self-serve, and support cannot serve them with
tooling.** Two things are missing, both deliberately left for a product decision:

- **No email infrastructure exists in the API at all** (no smtp/sendgrid/mailgun anywhere in
  `gridlens-api/app/`), so there is no "email me my key" path to build against.
- **No admin key-reissue endpoint exists** — every `/admin/*` route is plans/VPP only.
  Honouring the promise the config flow and `docs/docs.html` now both make ("email us for a
  replacement") currently means **hand-editing the `api_keys` table in MySQL**. Fine at
  current scale, but it is manual, undocumented as a runbook, and will not survive volume.
- The obvious no-email alternative — let a matching `ha_installation_id` + email rotate the
  key — is **not** a safe default: anyone knowing both could hijack a *paid* key. That's a
  security trade-off for the owner to make, not an implementation detail.

Raised with the user 2026-08-21; see `docs/GRIDLENS_CHECKLIST.md` for that day's entry.

**Gotchas.**
- **`translations/en.json` is what HA actually loads for a custom component — never
  `strings.json`.** A label present only in `strings.json` renders as its raw key
  (`gridlens_email`) in the UI. They drifted and did exactly that on setup's first screen;
  `sync-to-ha.sh` now regenerates `en.json` from `strings.json` on every sync.
- `_validate_energy_sensors()` covers import, solar **and** export. Only import used to be
  checked, so a watts sensor in the solar or export slot was accepted and quietly mispriced
  every comparison.
- `control/manager.py`'s `_DEFAULT_BRAND` is still `"sigenergy"` — a hardcoded fallback that
  predates this work and is contrary to §0's generic-design rule. Not changed here (live
  installs may lean on it); see `docs/GRIDLENS_CHECKLIST.md` 2026-08-21.

**Tests.** `tests/test_config_flow.py` — 22 offline tests driving the flow end to end with
stubbed HA + voluptuous (neither importable in this container).

---

## 12b. Deferrable-load wizard (the options flow)

**What it does.** Configures deferrable loads one load at a time, asking only what that
load's kind and control style actually use. Reached from **Settings → Devices & Services →
Grid Lens → Configure → Deferrable loads**, or as part of **Reconfigure everything**.

**Why it was rebuilt (2026-08-30).** The previous options flow put every field for every
load on two enormous forms. `device_power` rendered **14 fields per selected device** —
max kW, control entity, climate on-mode, SOC sensor, SOC ceiling, SOC capacity, setpoint,
setpoint unit, phases, voltage, min current, plug sensor, CL register, in-aggregate — for a
dishwasher as readily as for a modulating EV charger. Four loads made a 56-field screen, and
`translations/en.json` carried **140 pre-baked labels** (ten slots × 14) each prefixed with
`{device_N_name} —` because there was no other way to say which device a field belonged to.
`declared_loads` and `estimated_loads` then drew a fixed 2 and 3 slots whether used or not.
A forecast-only pool pump now answers **three** questions.

**Screens** (`config_flow.py`, `GridLensOptionsFlow`):

| Step | Asks | Shown when |
|---|---|---|
| `loads` | Hub — pick a load to edit, add one, or save | Always |
| `load_kind` | Metered / controllable-but-unmetered / neither | Adding a load |
| `load_add_monitored` | Which Energy-dashboard appliance | Adding a metered load |
| `load_detail_monitored` | Max kW, control style, has-own-battery, [on CL] | Editing a metered load |
| `load_detail_declared` | Name, daily kWh, max kW, hours, [on CL] | Editing a declared load |
| `load_detail_estimated` | Name, control entity, est. kW, auto-refine | Editing an estimated load |
| `load_control` | Control entity + climate on-mode | Control style is on/off or modulating |
| `load_modulating` | Setpoint, unit, phases, voltage, min current, plug sensor | Control style is modulating |
| `load_soc` | SOC sensor, charge ceiling, capacity | "Has its own battery" ticked |
| `load_cl` | CL register, already-in-aggregate | "On a Controlled Load circuit" ticked |
| `load_power` | Whole-house load power sensor | Offered when an estimated load exists |

**Three load kinds, one namespace.** `monitored` (has an energy sensor), `estimated`
(controllable but unmetered — `LoadEstimator` infers its draw), `declared` (neither; an
estimated daily kWh the optimiser plans around but never actuates). The kind is chosen once,
up front, from what HA can already see and do — which is the question that decides every
field afterwards. Declared and estimated loads share a name namespace and a duplicate is now
rejected **at the point of entry** rather than by a cross-step check several screens later.

**Storage is unchanged.** `deferrable_loads.py` is the seam: `read_loads()` turns the 22
parallel arrays into one dict per load, `write_loads()` turns them back index-aligned and
equal-length. Nothing downstream (`plan_calculator.py`, `__init__.py`,
`control/load_control_manager.py`, `sensor.py`, `number.py` — ~280 references) sees a
difference, and no config-entry migration is needed. `control_style()` derives
forecast-only / on-off / modulating from *which entities are set* rather than storing a new
field, so entries written before the wizard classify correctly with no migration.

**Things it fixes as a side effect:**

- **Changing a load's control style now clears the fields the old style owned**
  (`apply_control_style`). Previously, switching a modulating charger back to plain on/off
  left its setpoint entity in the config, and `LoadControlManager` kept treating it as
  modulating.
- **An estimated load can no longer be saved half-configured.** Both name and control entity
  are required; the old fixed-slot form let a name-without-control slot save silently inert,
  which cost a real misconfiguration (Daikin AC, 2026-08-06).
- **The 2-declared / 3-estimated slot caps are gone.** They were only ever a rendering
  artifact of the fixed-slot forms — every consumer iterates whatever length it is handed.
- **`Configure` no longer means walking the whole wizard.** The menu gained a direct
  **Deferrable loads** entry; editing one appliance no longer means re-answering energy
  sensors, battery specs and the plan picker. That path saves by merging over the entry's own
  data, so untouched settings survive.
- **Controlled-Load flags are seeded from the entry** in `GridLensOptionsFlow.__init__`
  rather than defaulting to `False`. The direct path never runs `async_step_controlled_load`,
  and `False` there would have silently hidden every CL question from a household that has a
  CL register.

**Known gaps.** The hub is a select-and-submit list, not one-click-per-load — HA menus
require a static `async_step_*` per option, and a dynamic load list can't provide that. CL
registers are still not filtered by the device types the network confirms for that register
(`NetworkIR.controlled_load_eligible_devices`); no live eligible-device lookup is wired into
the flow yet.

**Files:** `deferrable_loads.py` (new — the accessor seam), `config_flow.py`
(`GridLensOptionsFlow`, the `loads`/`load_*` steps), `strings.json` +
`translations/en.json`.

**Tests.** `tests/test_deferrable_wizard.py` — 21 offline tests: array round-tripping and
index alignment, per-kind field sets, step chaining, style downgrades clearing stale fields,
duplicate/half-configured rejection, the menu save path, and a pre-wizard entry with short
arrays reading back on defaults.

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
├── config_flow.py           setup flow — §12a; options flow + per-load wizard — §12b
├── deferrable_loads.py      one-dict-per-load view over the parallel arrays the rest of
│                            the code reads; the seam the wizard edits through — §12b
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
