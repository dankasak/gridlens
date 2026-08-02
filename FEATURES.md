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
> video plan (`MARKETING_VIDEO_PLAN.md`) are both downstream of it.

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

**Service:** `grid_lens.calculate_period` — re-run the comparison over an explicit date range.

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
key), `..._max_kw`, `..._switches` (control switch, `""` = forecast-only), `..._soc_sensors`
(e.g. EV SOC). Availability windows are **not** set here — see §8; a device is fully
unrestricted (any hour) until the user paints a schedule on the dashboard card. (A static
per-device `deferrable_load_hours` config-flow field used to seed this before the schedule
card existed — removed 2026-08-02 as redundant with it.)

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
power when on) to follow the plan.

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
| `grid-lens-powerflow-card` | Live radial energy flow: solar / grid / battery / home + one node per deferrable load, animated flow balls, live buy/sell price, greedy badges. |
| `grid-lens-power-chart-card` | Measured & forecast power (kW) — solar, load, signed grid, signed battery, per-device deferrable, plus free-energy shading. |
| `grid-lens-price-chart-card` | Import/export rate trajectory. |
| `grid-lens-soc-chart-card` | Battery SOC curve. |
| `grid-lens-cash-chart-card` | Cumulative cost/credit. |
| `grid-lens-dispatch-chart-card` | Planned EMS mode timeline. |
| `grid-lens-advisory-card` | Plan status tiles. |
| `grid-lens-load-control-card` | One row per deferrable load: Today Boost, greedy toggles, Off now / On now / Auto, and live greedy status. |
| `grid-lens-defer-schedule-card` | The 7 × 48 allowed-run-times editor. |
| `grid-lens-flex-row-card` | Layout helper. |

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

---

## 11. Observability

Because layers 2 and 3 take actions the user didn't ask for slot-by-slot, "why did it do
that?" has to be answerable from the dashboard alone.

| Surface | Answers |
|---|---|
| `switch.*_battery_control` attributes | Applied action/power, last tick, plan age, degraded state, note. |
| `switch.*_<device>_control` attributes | Commanded state, threshold, override, all three greedy toggles, **`greedy_reason`**, **`greedy_blocked`**, **`forecast_free_kwh` / `forecast_needed_kwh`**, note. |
| **Load Control card** | Per row: control state, and a live greedy line — the firing reason, or why it's blocked, or a **progress bar toward the forecast-surplus bar** (`6.2 / 8.0 kWh`) while it's armed and tracking. |
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
├── entity_lookup.py         device name / power sensor auto-discovery
├── advisory/                forecast → LP → dispatch plan + sensors
├── control/                 executor, battery + load control managers, controllers
├── inverters/               HAL: base.py contract, sigenergy_mqtt.py driver
├── tests/                   offline suites (no HA/scipy needed — `python3 tests/<f>.py`)
└── www/cards/               all Lovelace cards + grid-lens-chart-common.js
```

**Tests.** Every suite runs offline with stubbed HA: `python3 tests/test_*.py`. Run them all
before shipping — they're the only automated safety net in this repo.
