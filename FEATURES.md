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

The plan the user is currently on is **always shown regardless of the filter** (matched by
the `.current-plan` class, not its retailer) so there is always a baseline in view for the
filtered alternatives to be compared against, even when the active plan's retailer doesn't
match the search box. It still counts toward the "N of M" total.

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

**Postcode filtering (2026-09-22).** Same shape and same reasoning as network-tariff-code
matching just above, for a coarser problem: geographic scoping in Grid Lens otherwise stops
at state + DNSP network (see `config_flow.py`'s `_load_coverage`), so a household can still
see plans a retailer only offers in part of that network's footprint. A plan carries this as
`eligibility.included_postcodes` (comma-separated postcodes and/or inclusive ranges, e.g.
`"2000-2999,2610"`; `null`/absent = no restriction, the common case). The household enters
their own postcode via the "Postcode (optional)" field on the Current Plan step (initial
setup **and** Reconfigure) — this is the same `CONF_POSTCODE` key that used to sit on the
first setup screen and was read by nothing (see `config_flow.py`'s `async_step_user`
docstring); it's now wired to an actual filter. Blank/unset means "don't know", which
disables the filter entirely. Once set, `calculate_plan_costs` drops any candidate plan
whose included postcodes don't cover the household's, **except** the plan the household is
actually detected as being on, which always stays priceable. **Local-only**, same as the
tariff-code filter: the household's own postcode never leaves their HA instance. A plan's
own `included_postcodes` is public catalogue data, not customer data.

**Extraction from CDR PRD (2026-09-24).** `prd_sync.py`'s `author`/`restructure` commands now
extract `geography.includedPostcodes` automatically — but not verbatim. Investigated live
first: PRD restates the WHOLE distributor footprint on almost every plan (every GloBird
ZeroHero variant on Energex lists the same ~201 postcodes; every ENGIE plan on a given VIC
distributor lists the same count for that distributor), so a raw copy would be near-meaningless
noise on nearly every plan and actively risky — an incomplete PRD enumeration at a network's
edge would silently hide a genuinely available plan. `_prd_postcode_restriction` instead judges
a plan's list against its PEERS on the same distributor (`_network_postcode_footprint`, built
from the brand's already-fetched listing, no extra API calls): only a plan whose list is a
genuine proper subset of what its peers report gets `included_postcodes` set, with a reviewer
warning to verify against the fact sheet before publishing. Equal-to-peers (the routine case)
or no peers to compare against both leave it `NULL` rather than guess. `restructure` never
clears an existing hand-set restriction just because this run found no peer signal for it —
it's kept and flagged for manual reconfirmation instead (same "PRD wins, but silence isn't
evidence of absence" pattern as `monthly_subscription`). Existing plans are still all `NULL`
until re-authored/restructured, so this ships with zero behaviour change on its own.

**Files:** `plan_calculator.py` (`_plan_included_postcodes`), `retailer_plans.py`,
`const.py` (`CONF_POSTCODE`), `config_flow.py`, `strings.json`/`translations/en.json`;
API side: `gridlens-api/app/plan_models.py`, `plan_transform.py`, `plan_serialize.py`,
`plan_admin.py`, `main.py` (guarded `ALTER TABLE`); PRD extraction:
`gridlens-api/verification/prd_sync.py` (`_prd_postcode_restriction`,
`_network_postcode_footprint`, wired into `_derive_ir_from_prd` and `_merge_prd_structure`),
tested in `gridlens-api/tests/test_prd_postcode_extraction.py`; editor:
`gridlens-editor/main_window.py` (`PLAN_COLUMNS`), `main_window.ui`.

**Plan data** comes from the private `gridlens-api` (MySQL, temporally versioned —
`slug@date` rows). The HA side never sees another user's data and never sends usage data
out; the API only *delivers plan definitions*. See `PRIVACY_DATA_INVENTORY.md` in the API
repo.

**Rate structures modelled:** flat, TOU (multi-window, per-weekday), demand tariffs
(network-level *or* per-plan per-season — see below), controlled load, tiered/capped
rates (free-then-paid blocks), conditional daily credits (e.g. "stay under X kWh in
this window, get $1"), feed-in tariffs including wholesale-linked ones, supply charges.

**Per-season demand charges (`demand_periods`).** A plan may carry its own list of
demand-charge periods instead of relying on the shared network rate: each entry is one
`(season, window)` with its own `$/kW/day`, `days` and time window. A split high season
(e.g. Nov–Mar **and** Jun–Aug) is two entries with the same `season_label`; per-season
rate or window differences are separate entries. Sourced from the retailer's CDR PRD
`demandCharges` (`prd_sync.py` `prd_demand_periods()`), stored in the API's
`plan_demand_periods` table, served under the plan JSON's `demand_periods` key.
`retailer_plans.PlanFromData` parses it (`demand_period_covers`/`demand_rate_at`
helpers); a **non-empty** list overrides the network-level `demand_window` /
`demand_charge_per_kw_per_day` entirely, an **empty** one keeps the legacy single
network charge. A `demand_periods` plan's charge is **always priced** in the
comparison and fed to the optimiser — it's part of that plan's tariff structure,
so choosing the plan means being on it — *regardless* of the `has_demand_tariff`
config toggle (which is a fact about the customer's *current* DNSP meter and gates
only the legacy network-level charge). Without this, a demand-tariff plan variant
(e.g. Amber's "Smart Shift: Demand Tariff") would rank identically to its
non-demand sibling for anyone not currently on a demand meter. The bill breakdown (`grid-lens-card.js`) shows **one line per period**,
each with its own peak-kW, rate and in-season day count — matching how the retailer
itemises each season. `_compute_demand_charge_periods` in `plan_calculator.py` computes
the current-plan lines from actual metered usage per season; alternative plans get one
blended line off the LP dispatch (the LP schedule has no dates, so it can't be split by
season — an exact per-season LP form is deferred).

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

**A plan switch that falls inside the comparison window is split per-day, not
all-or-nothing** (`_plan_history_segments`/`_compute_multi_segment_bill_items` in
`plan_calculator.py`, 2026-09-15). "Current plan" for a custom date-range calculation
(Plan Comparison's date picker, via `PlanDataView`/`PlanStreamView`) used to be resolved
once from the plan-switch history log by comparing each entry's date against the window's
*start* — `entry.date <= window_start`. That's a single binary pick for the whole window:
if a switch date lands *after* the window start (even by one day — an off-by-one in the
logged switch date is enough), the newly-switched-to plan drops out of consideration
entirely and the *previous* plan silently prices the whole range, including days the
household was genuinely already on the new plan. Now each plan-history entry that falls
strictly inside `[start_date, end_date)` splits the window into segments, and each segment
is priced — actual usage, actual tariff — against whichever plan the log says was actually
held that day; the segment totals sum to the current-plan total. `current_plan_name` (the
label shown to the user) still reflects whichever plan is held as of the window's end (i.e.
today), not a segment's plan. The itemised `bill_items` for a multi-segment result carries
`is_multi_segment: true` and a `segments` array (each with its own full `bill_items`) rather
than merging two plans' rate lines into one table — a "Peak" line from two different
retailers would match neither plan's real bill. A PEA-eligible segment (Flow Power;
`aemo_price_sensor`) gets its own PEA credit sliced from the same whole-window AEMO price
series the main loop already fetches (`_segment_pea_result`) — confirmed live: a window
spanning a real Flow Power→GloBird switch now splits into two periods instead of falling
back, with the Flow Power segment showing its own correctly-computed PEA credit for just
that slice. **Known gap:** a segment whose plan is `is_market_linked` (sensor-priced
import/export, e.g. Amber SmartShift — a different mechanism PEA-slicing doesn't help with)
still isn't split-priced — the whole window falls back to the old single-plan pricing for
that case. See `docs/GRIDLENS_CHECKLIST.md`, 2026-09-15.

**The Plan Comparison card is organised around these bill-boundary periods, not around a
flat list of every plan** (`periods` field on `calculate_plan_costs`'s result, and
`_rank_plans_for_period`/`_price_plan_for_period` in `plan_calculator.py`, 2026-09-15). One
vertical section per period — however many that is; a window with no plan-history switch in
it is still exactly one period spanning the whole range, a window spanning three switches
renders four. Each section shows one cost-sorted grid: the plan actually held that period
(priced from that period's own real usage — the segment pricing above, always amber-banded
regardless of where it lands in the sort) sitting inline among that period's own ranked list
of every OTHER candidate plan, not pulled into its own row above them — 2026-09-15, per
owner feedback that a separate row made it harder to compare at a glance. Every OTHER
candidate plan is **re-priced against that same period's own usage slice**, not carried over
from a whole-window ranking: a short or unusual period can
genuinely favour a different plan than the window-wide comparison would suggest (a 1-day
period dominated by one unusually strong solar-export evening ranks very differently to a
5-day period dominated by weekday peak-import patterns). This is a real per-plan LP solve for
every candidate plan, once per period — accuracy over cheaper shortlisting, deliberately, at
the cost of `periods × plans` LP solves for a multi-period window (~240 extra solves for a
2-period, 124-plan comparison; full default-window request measured end-to-end at 55s —
fetch + whole-window loop + both periods — 2026-09-15) instead of `plans` for the common
single-period case, which is unaffected (reuses the whole-window loop's own results, zero
extra solves). Alternatives don't get an hourly chart — `_price_plan_for_period` deliberately
skips the hour-of-day-average machinery a period-sliced result has no meaningful version of;
they show the same itemised `_renderBillRows` breakdown as any other plan instead.

Two things make that cost bearable rather than a "stream failed" timeout (both 2026-09-15,
found live — see `docs/GRIDLENS_CHECKLIST.md`): the period-ranking loop reports progress
after every plan priced, via the same `'status'` SSE event the fetch phase already used, so
the stream keeps writing throughout instead of going silent for the whole periods phase; and
`GridLensCoordinator`'s unattended background refresh — which repeats on its own schedule
forever with nobody watching it, and was found competing with interactive requests for the
same executor thread pool — passes `skip_period_alternatives=True` so it only pays for the
cheap part (each period's actual-usage bill, no LP) and never the per-plan ranking. A third
form of the same contention (two interactive requests overlapping, e.g. a page reload
landing while the previous request is still running server-side) is closed by `_calc_lock()`
in `__init__.py` — one `asyncio.Lock` per config entry shared by every
`calculate_plan_costs()` call site, so a second caller waits (with its own "waiting" status
on the stream) rather than running concurrently and starving both.

**"Show best N" declutter filter** (`_topN`, default 5, `grid-lens-card.js`, 2026-09-15). A
`<select id="epc-topn">` next to the date controls, persisted like the date range. Purely a
client-side display filter over data the backend already fully ranked — changing it re-renders
instantly, no refetch. The plan actually held is always shown regardless of its rank (there
must always be a baseline to compare against); this only limits how many *alternatives* render,
per period in the multi-period view, or across the whole flat list in the single-period view.

**Spot pricing for market-linked *alternatives*** (`spot_pricing` block, 2026-09-04 —
`gridlens-api/docs/SPOT_PRICING_DESIGN.md`). A market-linked plan being *ranked* (not held)
used to be scored from its static `"(estimate)"` rate bands and a flat default FiT — which
carry no negative prices, no evening spikes, and none of the plan's spot export credit, so
Amber's fixed *Solar Sharer* standing offer would out-rank Amber *Smart Shift* on a
solar+battery install. When a plan carries a `spot_pricing` block (`region`, `import`/`export`
`adder_c_per_kwh` + `multiplier` + optional `cap`/`floor` — served as seven `spot_*` columns
on `plans`), `plan_calculator` fetches the real AEMO regional reference price once
(`_resolve_aemo_rrp_sensor` → `sensor.aemo_nem_<region>1_current_5min_period_price`, auto-
discovered; needs the `aemo_nem` integration), turns it into a per-clock-hour retail
import/export series (`_spot_retail_rates`: `rrp × multiplier + adder`, clamped per 5-min
interval), and prices that plan's LP path and no-battery path from it. Falls back to the
estimate bands for any hour with no RRP (pre-recorder-retention, ~90 days; or `aemo_nem`
absent).

The **bill breakdown** for a spot plan collapses to a single *"Spot import (period average)"*
usage line and a single *"Spot feed-in (period average)"* credit line — each the period
total at the c/kWh that actually resulted — rather than the dozens of one-off rate-bucketed
lines a per-hour-varying rate would otherwise produce (nothing on a real Amber bill looks
like that). A quiet `spot_note` says the plan is variable-rate. The per-hour rate *shape*
is on the new **"Average hourly price"** chart in the plan card (`renderRateChart` in
`grid-lens-card.js`): buy-rate and sell-rate polylines in c/kWh across the day, with a zero
line when the spot export price goes negative. Shown for any plan whose rate moves more
than 2c across the day (spot and TOU), sitting just below the existing "Average hourly cost"
(spend/income $) chart. The chart's rate values are the true per-interval rate the plan
priced against that hour of day (averaged across the whole comparison period), not
`cost/kwh` — the latter is 0 in any hour the LP didn't happen to import/export, which for a
battery-covered plan is roughly half the day and left one side of the chart full of gaps
(2026-09-05, found because the buy line looked broken next to a "working" sell line — same
derivation, just gappier on the plan being viewed).

**Spikes callout** (2026-09-05). The hourly charts above are hour-of-day *averages* across
the whole comparison period, so a single spike day is diluted into ~30 days' worth of the
same hour and becomes visually invisible — even though the ranked total and the LP's
dispatch already fully capture it (confirmed by inspecting the raw, un-averaged schedule:
the optimiser discharges to its max rate and drains SOC to floor specifically in the
highest-rate hours). A **"⚡ Spikes (>2× normal price)"** list under the price chart surfaces
these explicitly: real historical intervals where a spot plan's import or export rate
cleared 2x that direction's own period *median* (median, not mean, so the threshold isn't
dragged up by the very spikes it's meant to catch), detected on the raw LP schedule so the
date/rate/kWh/$ shown are what actually happened — not an average. Shows direction (buy /
sell / both), rate, the $ actually captured that hour (can be $0 — the battery only has so
much energy, so the optimiser may have held it for an even better hour instead, and the list
says so honestly rather than implying every spike was monetised), and when. Export spikes
dominate the list under a strict 2x rule: export tracks the wholesale price almost 1:1 (a
small subtracted fee), so a wholesale spike shows up near-proportionally, while import
carries a large fixed adder (network + environmental + fees + margin) on top that dilutes
the same spike's *ratio* relative to the higher baseline — a property of retail tariff
construction, not a detection bug. Only computed for spot-priced plans; capped at 25, top 6
shown with a count. `plan_calculator._calculate_plan_cost_with_battery_optimization`
("Spikes" comment) / `renderSpikes` in `grid-lens-card.js`.

**Bill breakdown mirrors a real retailer bill, on purpose.** The "our bill breakdown" card
(`grid-lens-card.js`) orders and labels its rows to match how an Australian electricity bill
actually reads — fixed charges (supply/subscription/demand/controlled load) first, usage
charges next, feed-in/export credits next (one line **per rate tier**, e.g. a capped
"top-up" rate separate from the base feed-in rate — via `_compute_bill_items`'s `fit.lines`,
built the same way as `energy_lines`), bonus/conditional credits last, then the total. The
point is letting a customer tick GloBird-ZEROHERO-style output off against their actual PDF
bill line by line to verify the product is pricing them correctly — don't reorder or
re-blend sections without checking against a real bill sample first (see CLAUDE.md).

⚠ **Gotcha — LP-path FiT tiers bucket by the free/over-cap split, not the blended rate**
(fixed 2026-09-04). For an alternative (LP-scored) plan with a daily-capped FiT (EA
BatteryEase: 8c first 10 kWh/day, 3c beyond), the solver reports a *blended* per-step
`export_rate` for whichever hour the day's cap boundary falls in, and that crossover lands
in a different hour with a different free/over ratio every day. Bucketing `fit.lines` by
`round(export_rate, 4)` therefore fragmented the FiT into a handful of one-off "Solar
Export" lines at rates printed on no real bill. `_compute_bill_items`'s LP branch now
buckets each step's `export_cap_free_kwh` / `export_cap_over_kwh` at their explicit
free/after-cap rates (exactly as the import `energy_lines` LP branch already did), and folds
`opt_result['cap_labels']` into the FiT label map so the post-cap line reads
"… (after N kWh/day)" instead of a bare "Solar Export". The two tranches still sum to the
solver's own per-step `export_credit`, so the total is unchanged. See the checklist entry.

⚠ **Gotcha — a structured cap plus a cap hint in the label text doubles up** (fixed
2026-09-04). Both label paths (`build_rate_caps` for the LP breakdown, `_split_capped_kwh`
for actual-usage) compose "`<label>` (first N kWh/day)" / "(after N kWh/day)" from the
row's `daily_cap_kwh`. When the stored `label` *also* carries a human-written hint —
"Solar Feed-in Tariff (first 10kWh/day)" — the result was "…(first 10kWh/day) (first 10
kWh/day)". `cap_label_base()` (in `retailer_plans.py`) now strips a trailing
"(… kWh …)" parenthetical from the base label before either path composes its tier labels;
it only runs where a cap is known present, so a TOU time-range like "Peak (3pm-9pm)" (no
"kWh") is left intact. This is a display-layer fix — the underlying plan data is untouched,
so a label like BatteryEase's is still worth tidying in the editor, but no longer has to be.

**Gotcha — capped-rate labels, now direction-scoped.** Label the free tier and the
after-cap tier explicitly; a rate-value-keyed label dict silently merges on collision if two
different tiers land on the same numeric rate — including *across* import and export (e.g.
GloBird's 0c import Free Window and 0c export No-Feed-in window). `_compute_bill_items` uses
separate `cap_labels` (import) and `export_cap_labels` (export) dicts for exactly this
reason — don't merge them back into one shared dict. See the checklist entry.

⚠ **Gotcha — a `VersionedPlan` bill spanning a retailer price change** (fixed 2026-09-04).
When the comparison window straddles a plan's `effective_from` boundary (two `/plans/history`
versions overlap the period), the date-aware LP correctly prices each hour at whichever
version was in force — so the schedule carries rate values from *both* versions. But
`_compute_bill_items` discovers rate→label mappings from `plan.get_display_breakdown()` /
`get_import_rate_defs()` / `get_export_rate_defs()`, and `VersionedPlan` used to return only
`self._latest` for all three. A rate value unique to the superseded version then matched no
label and rendered as a bare **"Energy"** / **"Solar Export"** usage line (real case:
`globird_solarplus`, Off-Peak 31.13c→28.05c on 2026-08-12 — the old 31.13c hours showed as
"Energy"). Those three methods now return every version's tiers, with a
`" (until <date>)"` / `" (from <date>)"` suffix on non-current ones (`_eff_suffix` /
`_all_rate_defs` in `retailer_plans.py`), and `get_display_breakdown` injects a zero-kWh
label anchor for any import tier only an old version had. Current-version labels still win on
a shared rate value. Consequence: a plan that changed rates mid-period now shows two lines
for the same tier at its two rates (e.g. "Off-Peak (until 12 Aug 2026)" 31.13c *and*
"Off-Peak" 28.05c) — which is how a real bill spanning the change reads. Other affected
plans: `energyaustralia_solar_max` (flat→3-rate TOU on 2026-08-17), `engie_solar_energy_plan`,
`arc_energy`, `flow_power_flow_home` (export label changed), `red_energy_living_energy_saver`.

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

**Plan-data fetch resilience (added 2026-09-03).** All three call sites that pull the
API's `/plans` payload — `GridLensCoordinator`, the `/plan_stream` SSE view and the
custom-range branch of `/plan_data` — go through `plan_cache.py::async_fetch_plans`. It
persists the last good payload per config entry (`.storage/grid_lens_plan_cache_<entry>`)
and, on a failed live fetch (a Cloudflare 502 while the API LXC redeploys is the usual
one), serves that cache for up to `CACHE_MAX_AGE` (14 days) instead of an empty list — a
slightly stale list still resolves the plan the user holds, which is what unblocks the
optimiser and advisory mode. A `402` (ended subscription) is never served from cache.
`GridLensCoordinator` also now sets a **dynamic `update_interval`** each run
(`_adjust_refresh_cadence`): a 12 h heartbeat when healthy so plan data self-heals even
though the `calculate_period` service is gone, and a 10 min retry while the configured
plan can't be resolved or only came from cache. If the configured plan slug is missing
from a *non-empty* list (a "mapped plan not served" problem, not an outage) it raises a
distinct persistent notification rather than retrying silently. `AdvisoryCoordinator`,
which depends on the main coordinator's `plan_data`, nudges it via
`async_request_refresh` (rate-limited to `MAIN_KICK_INTERVAL`, 5 min) whenever it can't
get a current plan, instead of spinning on `WAITING_INTERVAL` until an unrelated refresh
happens. Background: 2026-09-03, downstream of the gridlens-api boot-race incident, a
single startup 502 left `current_plan_name` `None` and advisory mode stuck "waiting" for
hours — see `GRIDLENS_CHECKLIST.md`.
**Files:** `plan_cache.py` (new), `__init__.py` (`GridLensCoordinator`), `advisory/coordinator.py`.

**"What if?" hypothetical battery/solar sizing (added 2026-09-23).** A collapsible panel
in the Plan Comparison toolbar (`grid-lens-card.js`, next to History) with two inputs —
battery size in kWh and solar production as a % of the household's real measured
production — plus "No solar or battery" (0/0) and "My current setup" presets. Answers
"what would my bill look like with a bigger/smaller/no system", including 0 kWh battery
and 0% solar to see the household's cost with neither.

**Solar is scaled, not re-simulated**: the real measured solar series is multiplied by
`solar_pct / 100` (0% = none, 100% = unchanged, 200% = double) — a production-scaling
approximation, not a panel-physics model, so it doesn't account for inverter clipping or a
differently-oriented array. **Battery is a fresh hypothetical `BatteryOptimizer`**: its
charge/discharge rate scales proportionally from the household's real configured rates
when a real battery exists, or defaults to a conservative 0.5C when it doesn't (e.g.
simulating a battery on a solar-only or no-battery-no-solar household).

⚠ **The one deliberate exception to "the current plan is never run through the LP"**
(the invariant documented earlier in this section). A hypothetical battery/solar size has
no real meter data to price against, so when either override is active, the plan the
household is actually on is priced through the exact same LP/simple path as every
alternative — the response's `whatif: {battery_kwh, solar_pct}` field (`null` when no
override is active) tells the frontend to render the hazard-striped "hypothetical, not
your real bill" banner instead of the normal actual-bill treatment. Multi-segment
plan-switch-history pricing (the `periods` construction) is bypassed for the same reason —
a what-if request always renders as a single flat comparison over the whole window.

**Files:** `plan_calculator.py` (`calculate_plan_costs`'s `whatif_battery_kwh`/
`whatif_solar_pct` params — see its docstring for the full mechanism, including how the
instance's own `battery_optimizer`/`has_battery` are temporarily swapped for the pricing
loop), `__init__.py` (`PlanDataView`/`PlanStreamView` — `?battery_kwh=`/`?solar_pct=`
query params on both `/plan_data` and `/plan_stream`; `battery_kwh`/`solar_pct` alone,
with no date range, still forces the on-the-fly recalculation branch rather than serving
stale cached real data mislabeled as a what-if result), `www/cards/grid-lens-card.js`.

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
- **Peak-demand shaving** — on a demand tariff the LP adds a peak-kW variable `P`,
  constrained `P ≥ grid import` in every in-window slot and priced in the objective, so it
  discharges the battery / shifts deferrable load out of the window to lower the peak. Two
  refinements for the **rolling** advisory horizon (a demand charge is billed on the single
  highest in-window demand over the whole *billing month*, not per horizon):
  `demand_peak_kw_month_to_date` (from the import sensor's hourly stats since the 1st)
  becomes a floor on `P` — no point shaving below a peak already locked in this month — and
  `demand_days_remaining` (calendar-month assumption) prices `P` at `rate × days-left`, the
  true marginal cost of setting a *new* peak. For a **legacy** (network-level) demand
  charge both default to 0, which is exactly right for plan comparison: that path solves
  one LP over the whole period, so the objective's `rate × n_days` fallback is already the
  exact bill. For a **`demand_periods`** plan the comparison path instead passes
  `demand_days_remaining` = the number of in-season days the horizon actually covers (so a
  charge that only applies ~5 months/yr isn't priced over all 365), and `demand_rate` = a
  day-weighted blend of the covering periods' rates; the LP's single `P` var is unchanged.
  The **rolling advisory** path (`advisory/coordinator.py` `_demand_inputs`) is
  `demand_periods`-aware too: the per-slot mask uses `PlanFromData.demand_period_covers`
  (season, window, day), `demand_rate` is the covering periods' blend over in-window
  horizon slots, and the peak var is priced over `min(days left in the billing month, days
  left in the active season)` (`advisory/demand.py` `days_to_season_end`). The result
  carries a `demand` summary.
- **Soft terminal SOC** — energy left in the battery at horizon end is valued at the
  horizon's mean export rate, instead of a hard "return to starting SOC" constraint. Kills
  the phantom end-of-horizon charge burst without enabling fake arbitrage.
- **SOC reward** (`0.0003`) — pure LP tie-breaking so degenerate optima resolve to the
  sensible plan (bank surplus solar rather than $0-export it). Calibrated: `0.001` distorts.
- **Minimum export price** (`number.*_minimum_export_price`, user-tunable — see §7a) — below
  this price, export earns nothing *in the objective*, so the LP prefers routing surplus into
  a deferrable load or the battery. It still exports if nothing else can absorb the surplus.
  The same setting also widens Greedy Consumption's real-time export bar (§7 condition 2).
- **No-grid-charge** option — the battery only ever charges from solar surplus; blocks
  buy-low/sell-high arbitrage for users who don't want it.
- **Deferrable devices stay in the energy balance** — `def_i` is priced via import/export
  like any other load. A past bug was double-counting them in *reporting*, not in the model;
  don't "fix" it by re-adding deferred energy to import.
- **Execution-realism filter (`dispatch_realism.py`, added 2026-09-18)** — the raw LP
  schedule assumes every scheduled kWh is actually drawn/sold; `control/executor.py`
  refuses to command a real grid force-charge or forced export below a materiality
  threshold (a real share of the slot AND above an absolute floor — see that module).
  `plan_calculator.py` now runs every alternative plan's LP schedule through the same
  filter before pricing it, so a plan's projected import/cost/savings can't include
  dispatch behaviour Grid Lens's live controller would never actually execute. Found
  comparing AGL Battery Rewards against Origin Battery Maximiser: a sub-threshold
  "top the battery up before the evening export window" sliver was being priced at
  face value. Current plan is unaffected (it's priced from actual usage, never the LP).

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

The `planned_dispatch` sensor also carries a `demand` attribute on a demand tariff:
`{planned_peak_kw, prior_peak_kw, rate_per_kw_per_day, days_remaining, window_slots}` —
`null` for plans without a demand charge.

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

## 3b. Immediate re-optimize on input change

**What it does.** Any user-facing write that changes what the LP plans against —
Minimum Export Price, Today Boost, an ad-hoc Charge Target, Daily Target (master or
per-device), a deferrable device's weekly schedule, or a Force On/Off/Auto override —
kicks `AdvisoryCoordinator.async_request_refresh()` immediately instead of leaving the
change to wait for the advisory's normal ~2 min tick. Both write paths (the dashboard
entity AND the equivalent `grid_lens.set_*`/`clear_*` service) trigger it, since the
hook lives in the shared store each one writes through, not in the entity/service layer
itself — see `reoptimize.py`'s docstring for the exact list of call sites
(`charge_target_store.py`, `daily_targets.py`, `deferrable_overrides.py`,
`deferrable_schedules.py`, `control/load_control_manager.py.set_override`, and
`number.py`'s `GridLensMinExportPriceNumber` directly, since it has no backing store).

`DataUpdateCoordinator.async_request_refresh()` already debounces/coalesces, so a value
changed while a run is already in flight just collapses into that run rather than
queuing a second one — callers never check "is it already running" themselves.
`AdvisoryCoordinator` passes a custom `request_refresh_debouncer` (`Debouncer(...,
cooldown=REOPTIMIZE_DEBOUNCE_COOLDOWN=1.5, immediate=True)`) rather than accepting HA's
own 10s default — at 10s, only the first of several changes made within 10s of each
other actually ran immediately; the rest silently collapsed into one trailing catch-up
run at the 10s mark (found live 2026-09-23: read as "the immediate re-optimize/optimizing
dot only works once", not a debounce cooldown). 1.5s keeps the "collapse a burst of rapid
changes into one solve" protection (a slider firing several onInput events per second)
while a normal, spaced-out change still feels immediate.

**Visual cue.** `AdvisoryCoordinator.is_optimizing` is true for the duration of an
in-flight run, pushed to listeners the moment the run *starts* (not just when it ends),
and exposed as the `is_optimizing` attribute on `sensor.*_planned_dispatch`. The Battery
Plan / status card (`grid-lens-advisory-card.js`, `compact` mode included) renders a
small pulsing dot (`.opt-dot` in `grid-lens-chart-common.js`'s shared `STYLE`) next to
the status badge while it's true, held visible for a minimum `OPT_DOT_MIN_MS` (900ms)
from whenever it last went true regardless of how fast the run actually finishes — on
this install the LP solve is sub-200ms, faster than a human can register a flash without
that forced minimum. Genuinely slower solves (larger horizons, a PuLP fallback) still
stay visible for their actual, longer duration on top of that floor.

**Files:** `reoptimize.py` (new), `advisory/coordinator.py`, `advisory/dispatch_sensor.py`,
`www/cards/grid-lens-advisory-card.js`, `www/cards/grid-lens-chart-common.js`.

**Deliberately excluded.** Toggles that don't change what the LP plans against — the
battery-control switch, a device's enable switch, the three Greedy Consumption switches,
and `GridLensModulatingMaxCurrentNumber`'s live current ceiling — don't trigger this. The
LP already plans independently of whether control is enabled (those switches only gate
*actuation* of an already-computed plan), and the modulating current ceiling narrows a
live 30s command, not an LP input.

---

## 3a. Shade correction (layer 2 input)

**What it does.** Learns a per-hour-of-day derate curve for a fixed, static solar
obstruction (trees, a neighbouring roofline) that a weather-based forecast provider has
no way to know about — it models panel geometry, weather and terrain, not a specific
household's shading. Compares the forecast provider's own live "power now" reading
against actual production (`sensor.*` configured as Solar Production in Sensors),
hour-by-hour over a trailing window, via HA recorder statistics — no external API calls.
Feeds the learned curve into the advisory layer's `ForecastProvider` (§3), so it corrects
what the battery optimiser plans against, not just a display number.

**Opt-in, off by default** — added 2026-09-23 as a general Grid Lens feature (works with
any forecast provider whose "power now" entity matches the shape below, not just
Solcast; needs both a forecast entity and an actual-production sensor configured and
trustworthy before it's safe to feed into dispatch decisions).

**Entities**
| Entity | What it holds |
|---|---|
| `sensor.*_solar_forecast_shade_corrected` | Raw forecast power × the current hour's learned factor. Attributes: `hourly_factors` (24-length, hour-of-day → multiplier), `hourly_samples` (matched-day count per hour), `raw_forecast_w`, `current_hour_factor`, `window_days`, `computed_at`, `forecast_entity_id`, `actual_entity_id`. |

**Config (Configure → Shade correction).** `shade_correction_enabled` (bool, default
off), `shade_forecast_power_sensor` (optional override), `shade_correction_window_days`
(7–90, default 30).

**Auto-discovery.** The forecast "power now" entity is found by shape, not name — any
`sensor` with `device_class: power`, `state_class: measurement`, and `estimate10`/
`estimate90` attributes (Solcast's convention; generic enough for another provider using
the same shape). Falls back to `sensor.solcast_pv_forecast_power_now` (documented
example default, only used if it actually exists) if shape-scanning finds no unique
match, then to the config override; skips setup entirely with a logged warning if
nothing resolves. The actual-production side reuses `CONF_SOLAR_SENSOR` — already
collected for every install from the HA Energy dashboard's "solar" source — rather than
inventing a second entity-resolution path.

**Learning.** Every 3h: pull `statistics_during_period` for both entities over
`window_days` (hourly buckets — `mean` for the forecast's power entity, `change` for the
actual production energy entity, since recorder's `sum` stat is a running cumulative
total, not a per-bucket delta). Bucket the ratio actual/forecast by **local hour-of-day
only** (not month×hour — this install's 90-day recorder retention isn't enough to fill a
12×24 grid with confidence); an hour needs ≥5 matched days before its factor moves off
1.0 (or its last learned value), and any single ratio is clamped to [0, 1.3] so one bad
statistics row can't dominate a median. Deliberately hour-of-day-only means precision
trades off against the recorder retention window — it re-learns as the trailing window
slides through the seasons rather than remembering last winter's curve.

**Files:** `shade_correction.py`, `entity_lookup.py` (`resolve_forecast_power_sensor`),
`advisory/forecast.py` (`ForecastProvider.shade_factors`), `advisory/coordinator.py`
(`_shade_factors`).

**Verification.** Live-tested 2026-09-23 on this install (real tree shading, factors
learned as low as 0.18 at 7am and 0.35 at 5pm from 28-30 matched days) — entity renders
correctly, `sensor.*_planned_dispatch`'s `sources.shade_correction_applied` confirms the
optimiser is consuming the corrected values. Not yet observed over multiple real dispatch
cycles / a season boundary.

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

**Bug fixed 2026-09-13: a device already over its ceiling made the WHOLE LP infeasible,
not just that device.** The day-0 floor row already clamped its own target to 0 via
`headroom = max(0, max_kwh - initial_kwh)` when the live reading sits above
`soc_max_percent` — but the SOC state variable's own upper bound (`ub[idx:idx+T]`) stayed
at `max_kwh`, unraised. The equality row that seeds day 0 (`ev_soc[i,0] = initial_kwh`,
unconditional) then pinned that variable ABOVE its own declared bound — not suboptimal,
a direct infeasible. And not a rare edge case: a device sitting a hair over its configured
ceiling (sensor lag, control-loop overshoot — precisely what the live SOC-cutoff interlock
above exists to catch) is the *normal* end state once it finishes charging, so this fired on
every single advisory tick once it started, taking `scipy` → `PuLP` → **every plan's entire
optimizer** down with it: the greedy fallback models no deferrable loads at all, so the Power
chart's "measured & forecast" forecast side (`defer_i`) went flat 0 for every device,
including ones with nothing wrong — misread live as the Power Flow hide-button work (added
the same day) over-hiding, since that was the most recent related change, when the two were
unconnected. Fix: `ub[idx:idx+T] = max(max_kwh, initial_kwh)` — the ceiling still stops the
LP charging any *further*, it just can't be lower than where the device already sits.
`battery_optimizer.py` also gained `_diagnose_infeasible()`: on any future infeasible solve,
it re-solves the horizon a handful of times with one feature group (deferrable loads as a
whole, then demand charge / caps / credits / hard terminal floor, then each individual
device) relaxed in turn and logs the first relaxation that restores feasibility — so
"LP optimisation failed... using greedy fallback" now names a likely cause instead of just
the bare scipy/HiGHS status code. See `GRIDLENS_CHECKLIST.md` 2026-09-13 for the full hunt,
including an inverted boolean in the diagnostic's first draft that initially blamed the six
innocent devices instead of the one real culprit.

**Observability (added 2026-09-07).** `ev_soc_status` on the LP result / `AdvisoryResult`
/ the `planned_dispatch` sensor — one dict per SOC-tracked device: `name`, `sensor_id`,
`capacity_kwh`, `initial_percent`, `max_percent`, `day0_final_percent`, `day0_charge_kwh`,
`target_kwh` (the un-clamped day-0 target — 14-day average or Today Boost), `target_percent`
(where SOC would have landed without the ceiling), `unmet_kwh` (`target_kwh − day0_charge_kwh`),
and `soc_limited` — `true` when the device reached its ceiling **and** more was wanted, i.e.
the ceiling is genuinely what shortened the cycle (not the availability window). The
`planned_dispatch` `trajectory` also carries a per-slot `defer_<i>_soc` (predicted SOC %,
day-0 slots only) for each SOC-tracked device, integrated from the post-consolidation
per-device energy so it lines up with the deferrable bars a card draws.

Two cards surface it:
- **`grid-lens-power-chart-card`** — a dashed per-device *planned* SOC curve on the existing
  0–100% right axis, in the device's colour, grouped with that device so isolating its
  legend entry keeps the curve. When `soc_limited`, a fainter flat line marks the ceiling
  the curve is flattening against, and the hover tooltip adds `… SOC plan 90% — capped at
  90%, N kWh held back by the ceiling`.
  **Measured per-device SOC (added 2026-09-09)** — a *solid* line on that same right axis,
  left of "now", for any deferrable load whose `soc_entity` (the `..._soc_sensors` config
  field) resolves, drawn exactly like the measured battery-SOC line. The dashed/solid pair
  reads the same way the standalone SOC card's does: dashed = planned, solid = measured.
  History is fetched by the base class (`_deferSocEntities()` hook → `_actualDeviceSoc`,
  parallel to `_deferSensorIds`); the sensor's live state is appended as a final point so
  the line reaches the divider. Fixed in the same change: the measured *battery* SOC line
  was silently missing its `actual: true` flag, so `clipForecastPastLine` clipped it to the
  forecast side (right of "now", where it has no points) and it never rendered.
- **`grid-lens-load-control-card`** — a "SOC-limited · `<got>` of ~`<target>` kWh" line on
  the device's row (next to the 14-day sparkline it appears to contradict), shown only
  while `soc_limited`; the tooltip explains the headroom maths and points at Max SOC % in
  Reconfigure.

A clamp caused by the ceiling (as opposed to the availability window) is also tagged
`reason: 'soc_ceiling'` in the existing `deferrable_clamped` notice and logged with its own
message so it doesn't read as "widen your weekly schedule" (the availability-window clamp's
advice, which wouldn't fix an SOC-ceiling clamp).

**Live-actuation enforcement (added 2026-09-13).** Everything above only ever shaped the
*plan's* `daily_kwh` allocation for day 0 — it had no way to stop Greedy Consumption's live
export-surplus / forecast-surplus terms (§6a) from adding real-time power on top of that
plan regardless of the device's remaining SOC headroom, because those terms are computed
from live grid/battery readings and know nothing about this config. Found live: a Wattpilot
install had `deferrable_load_soc_max_percent` configured at 85%, and the vehicle kept
charging straight through it to 86%+ because Greedy's export-surplus condition doesn't
consult this cap at all (GRIDLENS_CHECKLIST.md, 2026-09-13). Fixed by making the same two
fields (`..._soc_sensors` + `..._soc_max_percent`) a **hard interlock in the live tick**,
independent of the LP-side use above: `LoadControlManager._soc_cutoff_active(index)` reads
the live sensor every tick and, when it's at/above the configured ceiling, passes
`soc_cutoff=True` into `DeferrableLoadController.apply()` — which force-stops the device
immediately (no debounce, same urgency as a manual override) regardless of what the plan or
either greedy condition wants. `ModulatingLoadController` stashes the same decision from its
`apply()` override and enforces it a moment later in `modulate()` (the actual setpoint
writer, on the faster 30 s loop) — so a modulating device's cutoff holds even between
5-minute ticks. `soc_capacity_kwh` is NOT needed for this half (a plain percent compare is
enough to decide stop/no-stop); it stays purely an LP-planning input. A manual Force On
override still wins over the cutoff, same as it wins over the plan and both greedy
conditions — checked first in `apply()`, so the cutoff is never even evaluated once a human
has taken the device. Published on the Load Control card via `status()`'s new `soc_cutoff`
boolean and the existing `greedy_blocked: "soc_cutoff"` value. Covered end-to-end (including
a regression reproducing the exact incident — full export surplus, greedy + forecast-surplus
both on, SOC already past the ceiling) by `tests/test_modulating_load_control.py`'s
`soc_cutoff_*` / `manager_soc_cutoff_overrides_greedy_surplus` and
`tests/test_deferrable_load_control.py`'s `soc_cutoff_*`.

**Files:** `const.py` (`CONF_DEFERRABLE_LOAD_SOC_MAX_PERCENT` /
`CONF_DEFERRABLE_LOAD_SOC_CAPACITY_KWH`), `config_flow.py` (the wizard's `load_soc` step,
reached only when a load is marked as having its own battery — §12b), `plan_calculator.py` (`_get_deferrable_data` — static config passthrough only),
`advisory/coordinator.py` (`_deferrable_for_horizon` — the live reading for the LP),
`battery_optimizer.py` (`ev_soc_idx`/`ev_soc_specs` in `_lp_scipy`; `ev_day0_requested`,
per-slot `deferrable_soc_percent`, and the enriched `ev_soc_status` incl. `soc_limited`),
`advisory/planner.py` (`defer_<i>_soc` trajectory keys) + `advisory/models.py`
(`ev_soc_status` passthrough), `www/cards/grid-lens-chart-common.js` (`_evSocStatus`,
`multiLineChart` `pointsForecast` / `s.opacity`; `_deferSocEntities()` hook + `_actualDeviceSoc`
per-device measured-SOC fetch), `www/cards/grid-lens-power-chart-card.js`
(`_deviceSocSeries`/`_deferSocNote`; `_deferSocEntities()` reads `soc_entity` off the
`deferrable_loads` attribute), `www/cards/grid-lens-load-control-card.js`
(`_socCapFor`/`_socCapHtml`) — and, for the live-enforcement half,
`control/load_control_manager.py` (`_soc_cutoff_active`), `control/load_controller.py`
(`DeferrableLoadController.apply`'s `soc_cutoff` param + `status()`'s `soc_cutoff` field),
`control/modulating_controller.py` (`ModulatingLoadController.apply`/`modulate`).

**Ad-hoc dated extension (§9a).** The day-0-only scoping above is the *default* — a
one-off `grid_lens.set_charge_target` ("100% by 7am Saturday") extends a specific
device's own tracking/floor window out to its deadline slot, even past midnight,
without the multi-day infeasibility risk a *recurring* day-1+ ceiling would carry.
See §9a for the full feature.

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

**Card layout — every row the same shape.** Every row always renders the segmented Off
now/On now/Auto control *and* all three Greedy buttons for every device, even one with no
control switch configured at all — disabled and dimmed, with a tooltip explaining why,
rather than omitted. Rows for a controllable and a forecast-only device line up identically
instead of the row width jumping around depending on what's wired up. This row shape isn't
owned by one card — see §6b below for where it actually renders.

**Tooltips.** Every hint on this UI (disabled-control reasons, Greedy button explanations,
the boost ceiling note, sparkline bar dates) is a custom JS-delegated popup (`attachTooltip`/
`data-tip`, `grid-lens-chart-common.js`), not the native `title=""` attribute — shows in
~150 ms on hover instead of the browser's own ~1 s delay, and instantly on keyboard/touch
focus (every `[data-tip]` element carries `tabindex="0"` so touch and keyboard can reach it,
since native title tooltips are unreliable to trigger by tap).

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
case is a setpoint alone, since writing 0 A stops delivery. The wizard's `load_control`
step reflects this — the control entity is **optional** for a modulating load (and the
climate-only "on mode" question is dropped), so a setpoint-only charger such as a Fronius
Wattpilot completes setup without being forced to bind an unrelated switch. It was
required for every controllable load until 2026-09-10, contradicting this design.

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
`target = max(plan_w, surplus_w)`, where
`surplus_w = device_w - grid_w - max(0, discharge_w) - _EXPORT_BIAS_W` is the continuous
generalisation of Greedy Consumption — this device's own draw minus the grid flow *a battery
discharge isn't masking*, minus a small deliberate undershoot, i.e. how much it could pull
without creating new import. A free-import window targets the full cap. The surplus term is
gated on the same greedy toggles and schedule check as §7, and **fails closed**: no
`grid_power_sensor`, unknown rate, or unavailable entity and the term contributes nothing,
leaving pure plan-following.

**⚠ Net out battery discharge before trusting "grid near zero" (fixed 2026-09-11).** Found
live against the household's own Sigenergy + Wattpilot: a battery discharging to hold grid
flow near zero — its own onboard self-consumption loop, or GridLens's own SELF_USE battery
action — makes "no import" look exactly like free solar surplus. The formula used to be
`max(0,-grid_w) + device_w`, which (a) never subtracted a real import at all when
`grid_w > 0` — it just handed back the device's full current draw regardless, silently
overstating surplus by the *entire* shortfall — and (b) had no battery term, so it couldn't
tell "grid is 0 because solar exactly matches load" from "grid is 0 because the battery is
propping it up." `discharge_w` comes from `LoadControlManager._read_battery_net_power_w()`
(see §7's battery-headroom entry below for the two-sensor shape it also fixes); no battery
configured skips the correction (`device_w - grid_w - _EXPORT_BIAS_W`) — the export-bias
undershoot below still applies, that part isn't battery-specific.

**⚠ Battery charging is deliberately NOT symmetric with discharge (household instruction,
2026-09-11).** Only `max(0, discharge_w)` is added back — a battery actively *charging* from
spare solar contributes nothing to this device's claim, even though the same net-power
reading would suggest real headroom exists. The household's stated priority: the battery gets
first claim on genuine surplus; a modulating load only ever sees what's left over once the
battery's own charging is satisfied, never a bonus for what the battery is currently
absorbing. (The LP's own `plan_w` is unaffected by any of this — the live surplus term only
ever *adds* to what the plan already allocated, so a plan that deliberately schedules this
device from battery/grid for an unrelated economic reason, e.g. a cheap TOU window, still
works exactly as planned.)

**⚠ `_EXPORT_BIAS_W` (150 W default) — small deliberate export bias (household instruction,
2026-09-11).** The household's own reasoning: their currently-configured Minimum Export Price
floor treats even a positive, real export rate (3c/kWh) as "not worth selling" and routes it
to self-consumption instead — but import rates run well above that, so a small *mistaken*
import costs far more than a small *missed* export earns. Rather than aim the live-surplus
term at exact breakeven (where ordinary sensor noise lands on either side with equal
probability), it deliberately undershoots by a small fixed margin so noise is far more likely
to land on the (cheap) export side than the (expensive) import side. Small relative to a
typical quantisation step (~230 W for a 1 A step) — a nudge, not a meaningful throttle.

**⚠ Battery priority can pull the target below `plan_w` — found live the same day the
other two fixes shipped (household instruction, 2026-09-11).** Everything above this point
only ever *adds* to `plan_w`; none of it can express "the plan turned out too optimistic,
back off." That gap showed up immediately: the LP's plan for the live slot assumed enough
solar for *both* the EV and the battery (its own trajectory: `action: charge, power_w: 985,
grid_charge_w: 0` alongside `deferrable_kwh` for the EV in the same slot) — real solar fell
short, and with nothing able to reduce below `plan_w`, the EV kept its full planned draw
regardless while the battery alone absorbed the entire shortfall (climbing from 0 W to
560+ W discharge over several minutes, `modulation_source: "plan"` unmoving the whole time).
Fix: a live battery discharge is ground truth that *something* isn't matching the plan's
assumptions right now — a too-optimistic forecast, self-use, or even a deliberate
plan-driven evening discharge — and in every one of those cases the battery keeps first
claim. Applied **after** `target_w = max(plan_w, surplus_w)`: `target_w = max(0, target_w -
discharge_w - _BATTERY_PRIORITY_BIAS_W)`, regardless of the greedy toggle (a priority/safety
correction, not an opportunistic add-on) and independent of `grid_power_sensor` (only needs
the battery sensors). `modulation_source` reports `"battery_priority"` when this is what
actually reduced the figure, surfaced on the Load Control card's "why" line as "Reduced —
home battery has priority." No discharge (idle or charging) leaves the plan/surplus result
completely untouched — this never acts as a general-purpose override, only a live-discharge
response.

**⚠ `_BATTERY_PRIORITY_BIAS_W` (150 W default) — deliberate over-correction, added
2026-09-12 (household instruction, same reasoning as `_EXPORT_BIAS_W` above).** Cancelling
the live discharge *exactly* only drives it to zero in the limit: every real tick lags the
reading it's correcting against (30 s modulation ticks, amp-step quantisation, the write
deadband/rate limit), so in practice the plain `target_w - discharge_w` version just stops
discharge from getting *worse* rather than bringing it back to zero — confirmed live: a full
hour of declining afternoon PV showed the Wattpilot tracking the plan down while the battery
still funded a residual ~0.25–0.5 kW the whole time (SOC 100% → 98.5%). The household's
stance mirrors the export-bias one exactly: a little mistaken export is cheap, unnecessary
battery cycling (wear) is the thing being avoided, so the correction should overshoot toward
the safe side — pull back by the discharge amount *plus* a fixed margin — rather than track
the live reading exactly. Same 150 W default as `_EXPORT_BIAS_W`, same module
(`load_control_manager.py`), not user-configurable (a code constant, like its counterpart).

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

**⚠ Untested on real hardware** until 2026-09-11, when the household's own Fronius Wattpilot
arrived — the first modulating charger to actually meet this code, via `ha-wattpilot`
(`ruaan-deysel/ha-wattpilot`, local WebSocket, HACS). It immediately found a real gap:

**A charger with no stateful switch AND a setpoint that refuses 0 needs a third
mechanism.** ha-wattpilot's `max_charging_current` number has `native_min_value=6` — HA's
`number` platform rejects a `set_value` below that rather than clamping — and its only
start/stop control is two momentary `button.*` actions (the underlying `frc` force-state
property isn't exposed as a readable entity at all). Neither of the mechanisms above (write
0, or a switch) can express "off" here: writing 0 raised, and — before this was fixed — the
exception was caught *inside* `_write` **before** `self._commanded` was updated, so
GridLens believed it had turned the charger off while the hardware kept drawing at its last
setpoint. Config: `deferrable_load_start_button` / `..._stop_button` (both-or-neither,
enforced by the config flow), a `button.*` pair pressed by `ModulatingLoadController`
instead of a switch — start once on the off→on crossing, stop once on-off, never on a plain
amps ramp (a button has no readable state to gate on, so `_write()`'s own `crossing` flag is
what stops a repeat press every 30 s tick). When configured, turning off skips the setpoint
write entirely rather than attempting a value the entity would refuse. Still not an
OCPP-or-any-vendor driver — a second, optional actuation shape alongside the switch, for
exactly this "no switch, and 0 isn't valid either" case. See `control/modulating_controller.py`
`_write_setpoint` and `GRIDLENS_CHECKLIST.md` 2026-09-11 for the full incident.

**Known gap this introduces:** `_actual_state()` (is the hardware currently delivering?)
still reads the setpoint's raw value, which a button-actuated charger never zeroes on stop —
so it can read "on" for a while after a real stop-button press. Not on any decision path
today (only its own tests call it), documented in its docstring rather than silently wrong.

**Reassert on connect (2026-09-11, same day).** The Wattpilot's second surprise: it starts
charging on its own the instant a car is plugged in (its native `Default` mode has no local
PV-surplus/tariff signal to hold off with — `Eco` mode needs hardware this household doesn't
have and a tariff provider that doesn't serve Australia, see `GRIDLENS_CHECKLIST.md`). Write
economy is keyed off GridLens's *own* last commanded state, which quietly assumes the
hardware only ever moves because GridLens moved it — so with the plan already saying "off"
before and after the plug-in, nothing about GridLens's own decision changed, and the stop
was never re-sent. The device charged at full, un-costed grid rate for 28 minutes until a
human noticed and forced it off by hand via the override select. Fix, generic and not
Wattpilot-specific: `modulate()` now tracks `plugged_in()` and treats a confirmed
not-connected → connected edge as forcing one immediate re-actuation of whatever GridLens
currently wants, bypassing the deadband/rate-limit trim exactly like any other on/off
crossing — regardless of whether that decision differs from what was last commanded. Needs
only `deferrable_load_plug_sensor`, already optional on every modulating device regardless of
vendor; a device with no plug sensor configured gets no edge to trigger on (same fail-open
posture as plug detection itself), and a charger that doesn't free-run on its own just gets a
harmless redundant re-write. See `control/modulating_controller.py` `modulate()`/`_write()`
and `tests/test_modulating_load_control.py`'s `reconnect_*` checks.

**Brand-assisted setup (2026-09-11).** The Wattpilot's own setup needed a session's worth of
source-diving to find the right entities by hand (see the "First real modulating charger on
the rig" `GRIDLENS_CHECKLIST.md` entry that day) — nothing about the wizard surfaced any of
it. A new, purely optional `load_ev_brand` wizard step (`config_flow.py`, patterns in
`ev_charger_vendors.py`) now runs first for a modulating load: pick a recognised brand from a
dropdown and, if a matching entity is actually present on this Home Assistant instance, its
setpoint / plug-sensor / start-stop-button / switch fields (plus `min_current`) are pre-filled
as the very next two screens' defaults — still fully editable, nothing saved until the wizard
is walked through as normal. Leaving it on "Other" (the default) changes nothing, identical to
before this step existed. Only fills a currently-blank field, so re-running it on an
already-configured load never clobbers a deliberate manual override.

Per-vendor confidence is tracked explicitly (`confirmed` in `EV_CHARGER_VENDORS`), not
uniform: **Wattpilot** is live-confirmed (the entities above). **Sigenergy** AC-charger is
taken from this repo's own `custom_components/sigen` source (`ac_charger_output_current` /
`ac_charger_start_stop`) but not live-confirmed — no AC charger is wired to this dev rig's
plant, so this was actually the first time this codebase's own claimed vendor shape was
checked against its own other integration's source rather than assumed. OCPP, Easee and
Wallbox reuse this doc's/`strings.json`'s pre-existing (also unverified) claims; Zaptec, go-e
and OpenEVSE are new patterns from a 2026-09-11 web search — cited per-vendor as `source` in
`ev_charger_vendors.py` (Zaptec: github.com/ha-zaptec-community/ha-zaptec; go-eCharger:
github.com/cathiele/homeassistant-goecharger; OpenEVSE: home-assistant.io/integrations/openevse)
— also unverified against real hardware — each is labelled "(unverified pattern)" in the
dropdown so a wrong guess is never mistaken for a confirmed one. Getting one wrong is harmless by design: a false or missing
match just leaves that screen's field at its old blank/manual default. Tesla was deliberately
left out of the pattern table — no stable, install-independent entity-naming convention was
found, only per-install custom names — rather than guess one with nothing behind it.

Phase auto-derivation and each vendor's exact step/rounding semantics are still unverified
beyond the Wattpilot's own 1–32 A single number entity — every other vendor named above is
still stub-only.

**Vendor note surfacing (2026-09-16).** A vendor's `note` field in `EV_CHARGER_VENDORS`
(previously dev-only documentation, never shown to a user) is now carried by the options
flow (`self._load_vendor_note`, set in `async_step_load_ev_brand`) into the following
`load_modulating` screen's description, under a "Charger brand note" heading — empty when
no note applies (an unrecognised brand, or "Other"). Wattpilot's note now covers two things:
its start/stop buttons already act as the vendor's own pause (a crossing-only press of
ha-wattpilot's force-state register — car stays plugged in, only current delivery stops;
see `modulating_controller.py`'s `_write_setpoint`), so nothing extra was needed there; and
ha-wattpilot separately exposes a disabled-by-default `switch.*_charge_pause` entity that,
if enabled and left on, lets the charger's own firmware insert autonomous pauses outside
Grid Lens's control — the note tells the user to find and leave it off.

---

## 6b. Where load control renders (merged into the Power Flow header, 2026-09-24)

**What changed.** §6/§6a's per-device row (sparkline, Today Boost, the 3 Greedy toggles,
Off now/On now/Auto, the live status lines, the LoadEstimator debug panel) used to live only
on `grid-lens-load-control-card.js`, seeded onto the Settings view as "Deferrable Loads". It
now ALSO renders inline in `grid-lens-advisory-card.js`'s compact header — the "Optimiser &
Plan" bar at the top of the Power Flow page — behind the same chevron expander Daily Target
(§9b) already uses there, one row per device, alongside that row's %-of-average slider. This
removes having the same per-device deferrable-load block live on two separate pages, and
puts same-day actions (Today Boost, Greedy, On/Off) on the page a user actually looks at
first, instead of requiring a trip to Settings.

**Where it lives now.** `grid-lens-load-control-card.js` is NOT seeded onto Settings by
default any more (mirroring Daily Target's own 2026-09-22 relocation, §9b) — but the card
itself, and its Lovelace resource registration, are unchanged: it's still fully functional
and available for anyone who wants it as its own card on a different dashboard. Every
resolver (`controlSwitchFor`, `overrideSelectFor`, `greedySwitchFor`, `maxCurrentFor`,
`estimatorFor`, `socCapFor`, `windowHours`, `boostCeiling`, `resolveLoadControlRows`) and
every HTML-producing function (`greedyLine`, `modulationLine`, `currentReadoutHtml`,
`maxCurrentHtml`, `socCapHtml`, `sparklineHtml`, `estimatorToggleHtml`, `estimatorPanelHtml`,
`controlHtml`, `greedyButtonsHtml`, `boostInputHtml`, `attachTooltip`) moved into
`grid-lens-chart-common.js`'s "Load control helpers" section, imported by both cards, rather
than living only in `grid-lens-load-control-card.js` — the same fix already applied once to
Today Boost/charge-target entity resolution (§9b), now applied here for the same reason: two
cards' copies of this logic had already started to drift (the standalone card's own boost
resolver was missing exclusions the shared one already had — fixed as part of this move).

Every HTML-producing function takes an `opts.prefix` string so its own CSS class names don't
collide — `grid-lens-advisory-card.js` already uses the bare class `.row` for something
unrelated (the mode-transition timeline), so its call sites pass `{ prefix: 'dt-' }` and
`grid-lens-load-control-card.js`'s call sites pass none, keeping its class names unchanged.

**Same fix that touched the sparkline (see below) applies to `grid-lens-load-control-card.js`
itself too** — since `sparklineHtml()`/`estimatorToggleHtml()` are shared functions, the
misalignment fix reaches both cards from the one change.

**Files:** `grid-lens-chart-common.js` ("Load control helpers" section — everything above),
`grid-lens-load-control-card.js` (thin per-instance wrapper: constructor state, `hass`
signature diffing, `_paint()` calling into chart-common.js), `grid-lens-advisory-card.js`
(`_dt*` methods — `_dtPanelHtml()` calls the same chart-common.js functions with
`{ prefix: 'dt-' }`), `custom_components/grid_lens/__init__.py` (`_build_seed_views` — the
Settings seed entry removed, with the surrounding "Control" heading re-gated on SOC-tracking
so it isn't left orphaned with zero cards under it on an install with no SOC-tracked device
and no battery).

**Row-alignment bug fixed in the same change.** A screenshot showed the Today Boost box,
Greedy icons, and segmented control at inconsistent x-positions row to row. Root cause: the
sparkline's width scaled with how many days of recorder history a device actually had — down
to zero width outright on a failed/empty query — so every element after it in the row's flex
layout shifted left on a less-established device's row. `sparklineHtml()` now always renders
a fixed 14-bar-wide block, padding missing (older) days with invisible placeholder bars;
`estimatorToggleHtml()` gets the same same-size-invisible-placeholder treatment when a device
has no LoadEstimator, since that icon's presence/absence was a smaller second source of the
same shift (it sits between the Greedy icons and the segmented control in the row).

**Unverified.** This container has no browser. `node --check` and grep-based method/field
name collision checks (see §9b's own precedent for this class of check) both pass, and the
port from the standalone card's methods to shared functions preserves behaviour with no
signature changes to what any card renders — but the row alignment, the merged panel's
wrapping at real card widths, the ported tooltip positioning inside `grid-lens-advisory-
card.js`'s (previously tooltip-less) shadow root, and the new delegated event listeners have
not been exercised against live `hass` state. `getCardSize()`'s new formula for the expanded
compact header is a rough heuristic pending visual tuning. Needs `sync-to-ha.sh`, a dashboard
reload, and a click-through of both the Settings "Deferrable Loads" card and the new Power
Flow expander before this is "done" in the sense a curl-tested API change is done.

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
| 2 | **Export surplus** | Export price is **at or below the user's Minimum Export Price** (§7a — $0 when that setting is 0/disabled) **and** the house is currently exporting at least as much as this device draws — so running it can't create new import. | No new import; forgoes only below-floor export revenue the user has said they don't want. |
| 3 | **Forecast surplus** | Over a 9 h look-ahead (clipped at the plan's own next battery drawdown), the plan expects to waste energy at an average rate that clears this device's minimum-worthwhile draw — **and** the battery has both the discharge rate and the energy-to-min-SOC to actually supply it (see below). Runs the device **proportionally** to that rate, not all-or-nothing. | No new import; forgoes only below-floor export the plan would otherwise have made — see below |

**Condition 2's bar is lower for a modulating load** (§6a). An on/off load has to clear
`max_w` — all-or-nothing, so turning it on when only part of its draw is covered would create
real import. A modulating load can absorb *any* surplus, so its bar is `min_w` instead. That's
the single hook `_export_surplus_threshold_w()` exists for; the on/off behaviour is unchanged.

**Condition 2's *price* bar is the Minimum Export Price (§7a, added 2026-09-11).** It used to
be a hard `export_rate ≤ $0`. Now it's `export_rate ≤ min_export_price` — the same
user-tunable c/kWh setting the battery LP already uses (`number.*_minimum_export_price`).
Rationale: most of the day a typical FiT pays a few cents; a user who has set their floor to
5c is saying "I'd rather self-consume than sell below that". With the floor at its default 0
the test is byte-for-byte the old `≤ $0`. The *power* safety check is unchanged — the house
must already be exporting at least the device's draw — so this never creates new grid import,
it only redirects export the user has declared not worth selling. Applies to both the on/off
path (`load_controller.py::_greedy_wants_on`) and the modulating fast-tick surplus term
(`load_control_manager.py::_modulation_target_w`); `greedy_reason` stays `"export_surplus"`
for both the $0 and below-floor cases. **Condition 3's budget uses the same
`≤ min_export_price` bar** — so the forward-looking trigger and the live one agree on what
counts as wasted.

**Condition 2's two evaluations could disagree for a modulating load (fixed 2026-09-24).**
`LoadControlManager._modulation_target_w` (the 30s loop that actually writes a modulating
device's setpoint) nets the device's own already-flowing draw back into the grid reading
before judging surplus — once the device draws off real surplus, its own draw shrinks the
visible export, so without netting the loop would wrongly conclude "no more surplus" the
instant it started drawing. But `_greedy_wants_on` (the 5-minute tick that's the ONLY place
writing `greedy_reason`/`greedy_blocked` — everything the dashboard reads) compared the raw
grid reading with no such netting, so a device that was genuinely running on surplus could
show `greedy_blocked: "no_battery_headroom"` / `greedy_reason: null` right next to
`modulation_source: "surplus"` on the same card. Fix: `_greedy_wants_on` now nets `device_w`
and live battery discharge the same way `_modulation_target_w` does, both sourced from the
manager and threaded through `apply()`. See `docs/GRIDLENS_CHECKLIST.md`, 2026-09-24.

**Condition 3 is proportional, not all-or-nothing (2026-09-11 rewrite).** It used to fire
binary against a "could this device run flat out for the *whole* look-ahead and the plan
still spill more" bar — deliberately conservative, but it meant widening the look-ahead
(see the tuning-knob note below) raised the bar right along with it, and a modulating load
always jumped straight to `cap_w` with no proportionality. It now:

1. Computes the plan's forecast waste — `LoadControlManager._forecast_surplus_budget` — as
   `(spill_kwh, covered_h)` over a window that starts now and ends at **the earlier of** the
   nominal look-ahead or **the plan's own next material battery discharge**
   (`BatteryAction.DISCHARGE` at ≥`_RESERVED_DISCHARGE_MIN_W`, default 300 W). Past that
   point the plan is spending the battery on something it values (an evening export peak, a
   high import rate to cover) and Greedy must not borrow charge across it — since a
   `DispatchInterval` carries no per-slot SOC, "stop at the first planned drawdown" is the
   available proxy for "don't discharge below the plan's own SOC trajectory".
2. Runs the device at the average waste rate over that window, `spill_kwh / covered_h`,
   clamped to what the device can do: an on/off load only takes it if the rate clears its
   *full* draw (still all-or-nothing — it has no other setting) via
   `DeferrableLoadController._forecast_surplus_snap_w`; a modulating load takes the rate
   itself, capped at `cap_w`, via `ModulatingLoadController`'s override of the same hook. So
   a spill that would only justify a fraction of a charger's rate now ramps it in
   proportionally instead of demanding the full envelope or nothing.
3. Publishes the resulting draw as `forecast_target_w` (status()/the card), alongside the
   existing `forecast_free_kwh` (the budget) and `forecast_needed_kwh` (what the device would
   use flat-out over the same window — still the progress-bar denominator: the bar hits 100%
   exactly when an on/off device's rate clears its draw).

**Why #3 exists.** #1 and #2 are strictly instantaneous — they only fire once free energy is
already flowing. On a solar+battery house that fires late: mid-morning the battery soaks up
every spare watt, so live export is ~0 and neither fires, yet the plan already knows the
afternoon will spill far more than the device could eat. By the time export shows up, hours
of run-time are gone.

**Why #3 is safe.** Two battery gates, both backed by `LoadControlManager`'s battery config
(`battery_soc_sensor`, `battery_charge_power_sensor`, `battery_min_soc`,
`battery_max_discharge_rate`, and — new for the transient-dip check — `battery_capacity`, the
same fields the LP optimiser already uses; not a control-specific duplicate):

- **`battery_headroom_w`** (`_battery_headroom_w`) — free discharge rate right now (rated
  max minus whatever's already discharging) — caps the draw so it never asks for more than
  the battery can give this instant.
- **`battery_headroom_kwh`** (`_battery_headroom_kwh`) — energy to the configured minimum
  SOC (`(soc − min_soc)/100 × battery_capacity`) — caps the draw via
  `battery_headroom_kwh / forecast_hours`, the steady rate that uses no more than that
  energy over the *whole* window in the worst case a back-loaded spill (all the waste lands
  right before the window's reservation point) never shows up to repay it.

Both gates fold into one `min(rate_w, battery_headroom_w, battery_safe_rate_w)` clamp on the
rate — a **proportional** pin for a modulating device (it runs at whatever rate the battery
can safely fund, not the full ideal rate), reducing to pass/fail only for an on/off device
(no partial state to pin to). **Fixed 2026-09-20:** `battery_headroom_kwh` used to have to
cover the *entire* `forecast_spill_kwh` — the whole household's forecast waste, not what this
one device would actually draw — which a modest battery can never clear on a big-spill day
regardless of SOC or time of day (a 24 kWh battery, 10% min SOC, tops out at 21.6 kWh of
headroom, so it permanently failed against any spill bigger than that) and left condition #3
silently dead on exactly the days it exists for. See `docs/GRIDLENS_CHECKLIST.md`, 2026-09-20.

**The gate only ever saw the LIVE SOC snapshot, never the plan's own forecast (fixed
2026-09-24).** A battery mid-morning-charge (say 49% SOC) computed a small `battery_headroom_kwh`
against the fixed look-ahead denominator, capping the safe rate below even a modulating
device's floor and blocking condition #3 outright — even when the SAME plan's own trajectory
showed SOC climbing to 100% and holding there for hours before the day's next real discharge,
i.e. the live snapshot, taken mid-climb, understated the plan's own guarantee by more than
double. A naive per-slot "headroom/elapsed-time" rate was considered and rejected before
shipping — hand-traced against the 2026-09-21 regression incident's own numbers, it would have
computed ~2525 W and incorrectly unblocked the exact flat/non-recovering trajectory that fix
protects. Shipped fix: `LoadControlManager._plan_battery_headroom_kwh(now, window_end)` scans
the plan for its PEAK forecasted SOC anywhere in the window (new `DispatchInterval.forecast_soc_percent`
field, populated in `advisory/planner.py`) and widens `battery_headroom_kwh = max(live_snapshot,
plan_peak)` before the gate runs — never narrows it. Only the NUMERATOR changes; the existing
`battery_safe_window_h` denominator is untouched, so a flat trajectory's outcome is bit-for-bit
unchanged and only a genuine forecasted climb raises the rate. See `docs/GRIDLENS_CHECKLIST.md`,
2026-09-24.

Only when both gates clear does running the device draw the battery down instead of the grid,
with that hole refilled by the very spill being bet on. **No battery configured, no capacity
configured, or an unreadable sensor means no buffer exists — the condition fails closed and
never fires**, same discipline as conditions #1 and #2's missing-sensor handling. Recorded as
`greedy_blocked = "no_battery_headroom"` whenever the spill rate alone would have driven a
draw (see below) — distinguishing "the spill hasn't cleared the bar yet" from "it cleared,
but the battery can't safely supply it right now".

**⚠ `battery_charge_power_sensor` isn't signed on every inverter (fixed 2026-09-11).** The
original assumption — positive = charging, negative = discharging, one sensor — holds for
Tesla Powerwall and most single-sensor integrations, but Sigenergy (and presumably others)
splits charge and discharge across two always-*positive* sensors instead: "Battery Charging
Power" reads a flat 0 during a real discharge, so read alone it's indistinguishable from "not
touching the battery at all." `_battery_headroom_w()` (and `_modulation_target_w`'s live
surplus term above) now read `_read_battery_net_power_w()` instead, which optionally nets a
second `battery_discharge_power_sensor` (`CONF_BATTERY_DISCHARGE_POWER_SENSOR`) off the charge
reading when configured — the same two-sensor convention `plan_calculator.py`'s historical
battery-behaviour backtest already used for this exact config key, just not previously wired
into the live control path. Before this fix, a discharging Sigenergy battery reported the
*full* rated discharge rate as headroom (blind to the real, live draw), and the live-surplus
term above had no way to see the discharge was happening at all. No `battery_discharge_power_sensor`
configured reproduces the original signed-single-sensor behaviour exactly.

**⚠ A third, optional gate: the inverter's own AC output ceiling (found 2026-09-12).**
Both battery gates above reason about PV/battery *capability* — how much energy is there to
give — never about whether the plant can physically deliver it. Many all-in-one battery/PV
inverters cap total combined AC output well below what PV + battery could otherwise supply
together: confirmed on the household's own Sigenergy plant, where 7 days of
`sensor.sigen_0_plant_active_power` never exceeded ~10kW regardless of available PV or
battery SOC. On a day PV alone is already near that ceiling, "the battery has 20kWh free" is
true and irrelevant — none of it can reach the loads. This is what actually happened: Greedy
Consumption sized the household's Wattpilot's charging current off PV+battery headroom that
existed on paper but couldn't physically get through the inverter, and the car imported the
difference from the grid.

**`max_ac_output_kw` (Battery Configuration, options flow only — advanced/opt-in, 0 = unset,
the default)** backs `LoadControlManager._ac_output_headroom_w`: `plant_output_w = load_w −
grid_w` (whole-house consumption minus what the grid is contributing/absorbing) gives live
combined AC output from the two general-purpose sensors every install already has a config
slot for (`load_power_sensor`, `grid_power_sensor`) — no vendor-specific "total AC output"
sensor needed. None (not configured) is a pure no-op — unlike the battery gates, its absence
never blocks anything, since most installs' PV + battery genuinely can't reach their
inverter's rating. Once configured, an unreadable sensor fails closed to 0.0 headroom
(`greedy_blocked = "no_ac_output_headroom"` when that's what stopped condition #3 firing),
same discipline as the battery gates.

**Headroom credits back whatever's currently being exported (fixed same day, hours after
the gate above first shipped).** Naively, headroom = `max_ac_output_kw × 1000 − plant_output_w`
— but that alone conflates "the plant happens to be producing near its ceiling right now"
with "there's no room for more load", which is backwards whenever most of that production is
being wasted as export. Found live: household exporting ~3.4kW with PV near the plant's own
cap, and the gate throttled a legitimately surplus-soaking Wattpilot **down** anyway.
Redirecting power already flowing out as export to a load costs the plant nothing extra to
produce — only genuinely NEW demand, beyond both spare production capacity and current
export, can actually push total output past the ceiling. So the real headroom is
`max(0, max_ac_output_kw × 1000 − plant_output_w) + export_w` (`export_w = max(0, −grid_w)`,
0 while importing) — the export term is what makes hitting the ceiling harmless on a
sunny, mostly-exporting day, and only bites when the shortfall genuinely can't be produced.

Applied in two places: as a third clamp on condition #3's own target (`_forecast_surplus_target_w`,
alongside the two battery gates), and — because `fc_target_w` only refreshes on the 5-minute
`apply()` tick while the modulating fast loop runs every 30s — as a final live clamp in
`_modulation_target_w` (`source = "ac_output_cap"`), applied *after* plan/surplus/battery-priority
regardless of which term produced the pre-clamp target. The second one is the one that actually
matters day to day: it re-evaluates live every 30s, so it catches an uncontrolled coincidental
load (the household's own "Smart Load 01") or a stale forecast figure within one fast-tick,
not just at the next 5-minute plan tick.

**What counts as "energy the plan will waste"** (`LoadControlManager._forecast_surplus_budget`),
summed only up to the reservation point described above:
- **Spilled export** — a slot with `export_rate ≤ min_export_price` (i.e. `≤ 0` when the
  Minimum Export Price is disabled) that the plan still exports into. Uses
  `DispatchInterval.total_export_w` (whole-house export, PV spill included) — *not*
  `export_w`, which is only the battery's share of a discharge slot and is 0 on a pure
  solar-spill slot. Already net of every load the plan schedules, so nothing is subtracted.
- **Unused free-import window** — a slot with `import_rate ≤ 0`; only the part the plan does
  *not* already run this device counts (`max_w − planned_w`).

**Fail-closed everywhere.** Unknown rate, unavailable sensor, a safe window shorter than
`_MIN_BUDGET_WINDOW_H` (0.5 h — including when the plan is *already* discharging materially
this slot), or (condition #3 only) missing/unreadable battery SOC/charge/capacity → the
condition contributes nothing rather than guessing.

**Config:** the export-surplus condition needs `grid_power_sensor` — a **signed live power**
sensor, positive = importing, negative = exporting. Without it, condition #2 simply never
fires; #1 still works, and #3 works only if its own battery gates can be satisfied (see
above). Note this is a *power* sensor: the Energy-dashboard sensors (`energy_sensor`,
`solar_sensor`, `grid_export_sensor`) are cumulative kWh and cannot serve.

The forecast-surplus condition's battery gates need `battery_soc_sensor` (%),
`battery_charge_power_sensor` (**signed live power**, positive = charging, negative =
discharging), and `battery_capacity` (kWh) — the same battery config the LP optimiser
already uses (`plan_calculator.py`), not a control-specific duplicate. `battery_min_soc`
(default 10%) and `battery_max_discharge_rate` (kW, default 5.0) round it out. Without all
of these configured and readable, condition #3 never fires at all — there's nothing wrong
with running with it off, it just means the household hasn't given GridLens a way to confirm
the bet is safe.

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
- The Load Control card renders that case as *"Greedy: export is being wasted, but no grid
  power sensor is set"* instead of the misleading *"armed, waiting for free energy"*.
- `LoadControlManager` logs a one-shot **warning** (not debug — a debug line is invisible on
  the default install this happens on) naming the device and the fix.

**Observability** — see §11.

**Tuning knobs (`load_control_manager.py`):**
- `GREEDY_SURPLUS_LOOKAHEAD_HOURS = 9.0` (was 4.0 until 2026-09-11) — the nominal
  look-ahead, widened to span most of a solar day so a mid-morning tick can see the
  afternoon spill. Since the same-day proportional rewrite (below), widening this no
  longer raises a bar — the condition runs the device at whatever rate the budget works
  out to, not against a flat-out threshold — it just lets a further-out spill be seen
  sooner; the reservation clip is what actually bounds it.
- `_RESERVED_DISCHARGE_MIN_W = 300.0` — the planned-discharge power above which a slot
  counts as "the plan is spending the battery on something it values" and clips the
  budget window there. Lower it to make the clip trigger on smaller planned discharges
  (more conservative, shorter windows on average); raise it to let the budget window
  extend across small load-covering discharges.
- `_MIN_BUDGET_WINDOW_H = 0.5` — the shortest safe window the condition will act on.

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

**Disable button — hide a load from the Power Flow diagram (added 2026-09-11).** Every
node in the load fan carries a small "hide" button (bottom-left corner, `mdi:eye-off-outline`)
that turns off that device's own **"\<Name\> Show In Power Flow"** switch
(`switch.py::GridLensDeferrableVisibleSwitch`) — one per configured deferrable device,
default ON, created for ALL of them (including a forecast-only/declared load with no
control switch — it still draws a node, so it still needs the button). Turning it off is
purely cosmetic for this one card: the Load Control card, schedule card, greedy logic and
LP optimizer all keep reading `_build_deferrable_loads()` exactly as before and are
unaffected. `sensor.py::_build_deferrable_loads` resolves each switch's entity_id through
the entity registry by unique_id (`{entry_id}_deferrable_visible_{index}`) into a new
`visible_entity` field — there's no config-flow field or user-configured anchor to join on
for this one, unlike `switch_entity`/`soc_entity`; the switch itself is the thing being
referenced. The card's `_isLoadVisible()` fails open on a missing entity (an old config
entry mid-setup) rather than hiding every load. **No "show hidden loads" affordance on the
diagram itself** — a hidden load stays hidden until the switch is turned back on from its
own entity page (Settings → Devices → Grid Lens, entity category Config), the same
re-enable path the "Show Classic/Scene Power Flow" layout toggles already use.

**Same switch also hides the device from the Power chart and Plan Comparison (added
2026-09-13).** Still purely a display filter — daily_kwh keeps feeding the LP for every
alternative plan, and Load Control/schedule/greedy logic are still untouched; only where
a hidden device's own line/bar would otherwise be drawn disappears.
- **Power — measured & forecast chart** (`grid-lens-power-chart-card.js`): client-side,
  since this card already reads `hass.states` directly. `_isDeferVisible(i)` joins
  `_deferSensorIds[i]` (the trajectory's `deferrable_sensor_ids`, which can drop a
  zero-`daily_kwh` device and so isn't positionally aligned with the raw
  `deferrable_loads` attribute — see `advisory/coordinator.py`'s `_device_override`
  comment) against the `deferrable_loads` attribute's `visible_entity` field, same
  fail-open semantics as the Power Flow card. Gates the legend entry, the forecast +
  measured series, the greedy hatch band, the per-device SOC curve/ceiling line, and
  both tooltip rows (hourly-hover and pure-history) — a hidden device produces literally
  no visual output on this chart, not just a dimmed one.
- **Plan Comparison** (`grid-lens-card.js`): this card deliberately never stores the
  `hass` object (GC pressure — see its `set hass()` comment), so it cannot read switch
  state client-side. Filtering happens server-side instead, in
  `plan_calculator.py::calculate_plan_costs`, via the new `_deferrable_visibility()`
  helper — the same entity-registry lookup as `sensor.py::_build_deferrable_loads`,
  independently resolved (this class has no reference to that method's `ent_reg` call).
  A hidden device's entry is dropped from the `deferrable_devices` list sent to the
  frontend (both the streaming `plan` event and the final payload) and its slice of
  every hourly slot's `deferrable_per_device` array is dropped at the same position, for
  both the LP-schedule-based profile (alternative plans, `day_profile`) and the
  hour-of-day-average profile (current plan / non-battery-optimized plans, keyed off
  `deferrable_per_sensor_hod`) — two different index spaces (`deferrable_loads`'
  filtered-by-having-stats order vs `self.deferrable_load_sensors`' raw config order),
  each filtered independently by sensor_id/position rather than assumed to match.
  `deferrable_hod_avg`/`deferrable_kwh` (the combined-across-devices total) are
  deliberately left untouched, same rationale as "Exclude Greedy Consumption": that's
  real physical energy the household total must keep reflecting, only the per-device
  breakdown line disappears.

⚠ **Band width is capped at `MAX_GREEDY_STEP_MS` (5 min) — fixed 2026-09-02, was painting
whole plan-driven sessions as greedy.** The tracker sensor is edge-triggered: it only writes
a new recorded sample when its counter actually ticks up, never a periodic "still greedy"
heartbeat. `_fetchGreedyBands()` originally drew a band from the *previous* recorded sample
straight through to the one where an increase was detected — correct for a genuinely
continuous greedy run (which does emit dense ~60s samples that chain together), but wrong
whenever the gap between two real samples is large: user-reported and confirmed live on
2026-09-02 — a single 0.02 kWh bump at 14:30 after a flat overnight baseline (the day's only
other "sample" was the synthetic value-at-midnight row HA's history API synthesizes for the
query window's start) rendered as one band from midnight to 14:30, hatching an entire
plan-driven EV charging session (14.7 kWh, ~all of it plan-attributed per the 2026-08-31
fix) as if it were greedy. Each step's band now ends at the later sample but starts no
earlier than `t1 - MAX_GREEDY_STEP_MS`, matching `LoadControlManager`'s 5-minute tick (no
single step can represent more greedy duration than one tick's worth, since `greedy_reason`
is only re-derived that often). Genuinely continuous stretches are unaffected — their
samples are well under 5 minutes apart, so the cap never engages and the existing
touching-interval merge still chains them into one long band.

### 7a. Minimum Export Price

**What it is.** `number.*_minimum_export_price` (c/kWh, `NumberMode.BOX`, default 0 =
disabled, range 0–50, step 0.5). RestoreEntity — its state *is* the live setting, picked up
without a reload: by the battery LP on its next run (§7's "Minimum export price" objective
term — below this the LP values export at $0 and prefers a deferrable load or holding
charge) and by Greedy Consumption on its next 5-minute tick — it widens both the live
export-surplus bar (condition 2) and the forward-looking `_forecast_free_kwh` numerator
(condition 3) from `≤ $0` to `≤ this`. Set it aggressively (e.g. 5c when the plan shows a
day of 3c export) and a Greedy-enabled load with Forecast Surplus on will start early off
the battery rather than wait for the spill; set it to 0 and every bar reverts to `≤ $0`.

**When it's offered.** Created when the install has a battery **or** at least one deferrable
load (`number.py::async_setup_entry`) — so a battery-less house with a Greedy-enabled pool
pump still gets the knob. Config-flow no longer writes the key; `entry.data` only carries
the pre-entity fallback (0.0 in practice).

**Files:** `number.py::GridLensMinExportPriceNumber`, `runtime_settings.get_live_number`
(shared live-read), `plan_calculator.py::_get_min_export_price` (LP side),
`load_control_manager.py::_min_export_price` (Greedy side), `battery_optimizer.py`
(`r_exp` floor), `const.py::CONF_MIN_EXPORT_PRICE`.

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

## 9a. Ad-hoc charge target

**What it does.** A one-off "charge to X% by a datetime" target on an SOC-tracked
deferrable load — e.g. "100% by 7am Saturday" before a trip. Unlike Today Boost (a
kWh number with no deadline), this is a percent + a deadline: the optimizer still
picks the cheapest/solar-heavy slots between now and the deadline, only forcing
full-power charging in the run-up if it must, to guarantee the target is met in
time. Set via two paired entities per SOC-tracked device — `number.*_charge_target_percent`
and `datetime.*_charge_target_time`, shown together on `grid-lens-charge-target-card` (in
Settings → Charge Targets, seeded automatically for any install with at least one
SOC-tracked device) — or equivalently the `grid_lens.set_charge_target` /
`grid_lens.clear_charge_target` services (handy from an automation/script; the card stays
in sync with a service-set target live, via a dispatcher signal — no restart needed). Only
available on a device with an SOC sensor + capacity configured (the same
CONF_DEFERRABLE_LOAD_SOC_SENSORS/_CAPACITY_KWH fields the day-0 SOC ceiling uses —
see §5) — the target math needs a live SOC reading and a capacity to know how many
kWh are actually needed.

**Auto-clears itself** once the live SOC reaches the target percent, or once the
deadline itself passes — no manual clearing needed after use (unlike Today Boost,
which persists until zeroed; a dated target has a natural end condition Today Boost
doesn't). Setting the percent entity to 0 (or calling `clear_charge_target`) cancels
it early.

**A target above the standing SOC ceiling (`soc_max_percent`) temporarily raises it**
for the occasion — a everyday 80% longevity cap doesn't block an explicit "100% for
this trip" request.

**Files:** `number.py` (percent entity), `datetime.py` (deadline entity),
`www/cards/grid-lens-charge-target-card.js` (dashboard card, auto-discovers the entity
pairs via `charge_target_role`/`deferrable_sensor_id` state attributes),
`charge_target.py` (pure maths — slot rounding, reach/expiry, store read/write;
`tests/test_charge_target.py`), `charge_target_store.py` (shared Store; `update_signal` —
the dispatcher signal both entities and the card's underlying tiles refresh from when the
OTHER write path, e.g. the service, changes a target),
`services.py`/`services.yaml` (`set_charge_target`/`clear_charge_target`),
`advisory/coordinator.py` (`_charge_target`, wired into `_deferrable_for_horizon`),
`battery_optimizer.py` (`track_slots`/`floor_slot` — generalizes the day-0-only SOC
floor to bind at an arbitrary slot, even past midnight; see the `_lp_scipy` module
docstring for why a one-off dated floor is safe where a recurring one would not be).

**Horizon limit.** The deadline only binds once it falls within the current rolling
horizon (~24-48h) — set further out, it's simply not yet in view and the device
stays on its usual Today-Boost/historical-average floor until a later replan brings
the deadline into range. This is the existing rolling-horizon behaviour, not a bug
specific to this feature.

**Verified live (2026-09-14)** against the dev rig: a real target set on Wattpilot/XPENG
(95% by +3h, against its 85% standing ceiling) solved clean, raised the effective ceiling,
and matched hand-derived kWh figures exactly; a no-target tick reproduced the original
day-0-only behaviour unchanged. `battery_optimizer._lp_scipy` still can't be exercised in
this dev container itself (no scipy — confirmed again, see `tests/test_demand_charge.py`'s
header), so this was checked on the live instance, not here. See
`docs/GRIDLENS_CHECKLIST.md` 2026-09-14 for the full run (including a dispatcher/thread bug
in the `number`/`datetime` entities' live-sync found and fixed during this check).

**`grid-lens-charge-target-card` is unclicked** — added 2026-09-15, `node --check`'d clean
and the entities/attributes it auto-discovers off (`charge_target_role`,
`deferrable_sensor_id`) were confirmed live via the API, but this dev container has no
browser/display to actually render a Lovelace card in, so the card itself needs the owner
to look at it before this is "done" the way a curl-tested API change is done — same caveat
as `gridlens-editor` (docs/CLAUDE.md).

---

## 9b. Daily Target

**What it does.** Scales a deferrable device's daily-kWh target as a **percent of its
14-day average** — "the EV's battery is big enough it doesn't need a full charge every
day, dial it to 40%" or "skip hot water, it's going to rain, dial to 0%" — without
hand-computing an absolute kWh figure the way Today Boost (§9) requires. A **master**
target (`number.*_daily_target_master`) sets the default for every device with no pin of
its own; setting a **per-device** target (`number.*_<device>_daily_target`) **replaces**
the master's influence on that device (not multiplicative — pinning the EV to 100% while
the master sits at 60% for a cloudy day means the EV alone still gets its full usual
target). Values above 100% are allowed (e.g. 150% ahead of a run of cloudy days).

**Where it lives (relocated 2026-09-22).** The default, always-visible home is
`grid-lens-advisory-card`'s **compact header** — the "Optimiser & Plan" bar at the top of
the **Power Flow view** (the landing page): both solar forecast boxes and the master
slider sit right in that header, with a chevron expander next to the slider that drops
down the full per-device list on demand, so the collapsed bar stays a genuinely slim
status line. The original standalone `grid-lens-daily-target-card` (master slider + solar
header + every device, always expanded) still exists and is still registered as a
Lovelace resource, but is no longer seeded onto the default Settings view — it's there for
anyone who wants Daily Target as its own card on a different dashboard. Both cards are
built from the SAME exported functions in `grid-lens-chart-common.js` ("Daily Target
helpers" section) rather than keeping two copies that could drift — one slider per device
with its 14-day average and computed "≈X kWh at this rate" readout, a "Follow master"
reset button when a device is pinned, and **both today's remaining forecast solar and
tomorrow's** (with a placeholder weather icon each) for context while dialing.

**That same per-device expander is no longer Daily-Target-only (2026-09-24, see §6b).**
Each expanded row now ALSO carries the full load-control row — Today Boost, Greedy toggles,
Off now/On now/Auto, live status, the estimator debug panel — alongside this section's
slider, so a reader landing here should know the expander is a merged panel, not a
Daily-Target-exclusive one.

**Named "Daily Target", not "Tomorrow Planning" (its original name — renamed 2026-09-22,
same day it shipped).** The feature grew out of investigating a "battery charged off grid
for no reason" report (see the checklist's 2026-09-21 entry): the charge was actually
rational (a real next-evening VPP export window), but exposed that there was no way to
tell the optimizer "the EV doesn't need its usual full charge" to reduce that kind of
grid top-up. "Tomorrow Planning" was the first name, matching the evening-before-checking-
the-forecast mental model — but it's mechanically wrong: the LP applies the SAME scaled
`daily_kwh` figure to **every day-chunk in its rolling horizon**, and it takes effect from
the very next advisory tick (~2 min) regardless of what time of day you set it. Set it at
8am on a rainy morning and it caps what's left of TODAY too, not just tomorrow — "next 24
hours" would be *even less* accurate, since it implies a bounded window when the real
behaviour is "this daily figure until you change it back," persisting across every day in
the horizon, not just the next 24h. Renamed to a term that doesn't imply a specific day at
all.

**Day-boundary fix (2026-09-23).** For the feature's first day live, "day-chunk" secretly
meant a rolling 24h window counted from whenever the advisory solve last started, not a
real calendar day — found live when an EV charger scaled to 25% (~2.5kWh) on a cloudy day
had that entire target scheduled for the **following, sunnier morning** instead of that
day, defeating the whole point of a same-day reduction (see the checklist's 2026-09-23
entry). Fixed in `battery_optimizer.py` (`slot_day_index`/`_day_groups`, built per-horizon
from `retailer_plans.slot_calendar_day_index`): every day-chunk, including today's, is now
bound to a real local calendar date. A same-day target is satisfied within today's actual
remaining hours and can no longer be quietly fulfilled a day later just because that day
happens to be cheaper — the "next 24 hours" mental model above is now also literally
correct for what's left of today, not just close enough.

**The rainy-morning case, specifically — what it can and can't do.** Using it in the
morning for the current day works exactly like using it the evening before for the next
one: same store, same mechanism, no special-casing needed. But it only affects **energy
not yet drawn**. If a device (the EV, say) already finished its charge overnight before
you noticed the rain and dialled things down at 8am, the lower target won't claw that back
— it just caps further draw for the rest of today (and every day after, until reset). For
a device that hasn't run yet today (hot water heating in the afternoon), the morning
adjustment fully applies. This is why the card shows **both** today's remaining forecast
and tomorrow's, not just tomorrow's — a tomorrow-only header would be the wrong number to
look at on a rainy morning, actively misleading someone using the feature for today.

**Already-drawn accounting fix (2026-09-24).** "Caps further draw for the rest of today"
above was the *intent* from day one but wasn't actually true until this fix: today's day-0
target (both the flat per-device equality and the EV/SOC floor's no-active-charge-target
branch in `battery_optimizer.py`) was computed as `daily_kwh × (remaining slots today /
slots_per_day)` — a pure time-fraction proration with **no knowledge of energy already
metered today**. Found live: a Wattpilot's target was dialled down to 20% (~2.1 kWh)
mid-afternoon, well after Greedy Consumption had already charged ~8.9 kWh off real solar
surplus that day — the plan still demanded a further ~0.7 kWh that evening, off grid power,
because the proration re-derives a fresh fractional slice of the total regardless of what
already happened. Fixed with a new optional `consumed_today_kwh` field (per-device dict,
populated by `advisory/coordinator.py._consumed_today_kwh` from live statistics since local
midnight): when present, today's target becomes `max(0, daily_kwh - consumed_today_kwh)` —
the real remaining balance — via the new `battery_optimizer._day0_target_kwh` helper.
Absent (the default — `plan_calculator.py`'s plan-comparison backtest never sets it, same
reasoning as `soc_initial_percent`) reproduces the old proration byte-for-byte. See
`tests/test_day0_target_kwh.py` and the checklist's 2026-09-24 entry.

**Why 0% can't mean "clear" here (unlike Today Boost's 0 kWh).** Today Boost's 0 kWh is
meaningless as a boost, so it doubles as the "unset" sentinel. Daily Target's 0% is a
*meaningful, explicit* target (skip the device entirely), so it can't do double duty —
clearing a device's pin (falling back to the master) is its own action, either the card's
"Follow master" button or the `grid_lens.clear_daily_target` service. Writing any value,
including 0, via the number entity always pins.

**Persistence — deliberately NOT auto-reset at midnight**, even though that was the
initial instinct (mirroring what Today Boost *used* to do). `override_expiry.py`/
`daily_target_rules.py` document why: the advisory LP plans on a rolling horizon, so a
plan built before midnight can already be relying on a scaled target for a still-future
slot — auto-expiring it mid-plan would silently revert the plan out from under itself with
no notice, exactly the 2026-07-31 Today Boost incident this avoids repeating. A pinned
target instead persists until explicitly changed, with a once-per-day persistent
notification if it's still active past the day it was set (same UX as Today Boost's own
carry-over notice).

**Interaction with Today Boost (§9) and the ad-hoc charge target (§9a).** Ordering in
`advisory/coordinator.py._deferrable_device_params()`: Daily Target's percent-scale
(`_apply_daily_targets`) runs first, adjusting the historical `daily_kwh` baseline; Today
Boost's absolute override (`_apply_overrides`) then runs on top and **wins outright** for a
device where it's active — a same-day "I need X kWh today" is a more specific signal than a
percent-of-average. A device with an **active ad-hoc charge target** bypasses both: the LP
uses the target's SOC-gap floor instead of `daily_kwh` while the target is live
(`battery_optimizer.py`'s `ev_soc_idx` path, §9a) — the card annotates a device's row when
either of these is overriding its slider so the number displayed isn't mistaken for what
will actually happen.

**Weather icons are a placeholder.** There's no real weather-condition data behind them —
just a self-relative bucketing of forecast kWh against the best comparable day visible in
the same Solcast forecast array (never a hardcoded absolute kWh number, so it reads
sensibly on a 3kW system and a 15kW system alike). See the card's `weatherFor()`/
`_solarSummary()` — the intended swap point for nicer rendered/animated icons later. Any
AI-generated icon/animation art for that would live in `gridlens-api`, never in this public
repo, per the project's asset-location rule; plain mdi icons (as shipped) are fine here.

**Scope note.** Only devices with a real energy sensor (`CONF_DEFERRABLE_LOAD_SENSORS`) get
a Daily Target entity, same set as Today Boost. Declared/estimated ("dummy") devices
(`CONF_DEFERRABLE_LOAD_DUMMY_*`) are not included — they're not currently part of the live
advisory/dispatch plan at all (`_deferrable_device_params()` only calls
`calc._get_deferrable_data()`, which is sensor-history-only; declared loads are parsed by
a separate `_parse_declared_loads()` used only by the plan-comparison path), a pre-existing
gap this feature didn't create and doesn't fix.

**Files:** `daily_target_rules.py` (pure carry-over logic, no HA imports —
`tests/test_daily_targets.py`), `daily_targets.py` (shared `Store`-backed
`DailyTargetStore`), `number.py` (`GridLensMasterTargetPercentNumber`,
`GridLensDeferrableTargetPercentNumber` — join key `deferrable_sensor_id` +
`daily_target_scope` state attributes, disambiguating from Today Boost/charge-target's own
use of `deferrable_sensor_id`), `services.py`/`services.yaml`
(`set_daily_target`/`clear_daily_target`), `advisory/coordinator.py`
(`_apply_daily_targets`, called before `_apply_overrides` in `_deferrable_device_params`),
`www/cards/grid-lens-chart-common.js`'s "Daily Target helpers" section (resolvers,
`solarSummary`/`weatherFor`, `fetchDailyAverageKwh` — shared by both card files below),
`www/cards/grid-lens-advisory-card.js` (primary home, compact header — `_dt*` methods; the
expanded per-device panel these methods build now also carries the merged load-control row,
§6b, via that same file's "Load control helpers" section),
`www/cards/grid-lens-daily-target-card.js` (standalone card, unseeded but still available).

**Unverified** — added 2026-09-21, renamed + today/tomorrow forecast header added
2026-09-22, relocated into the advisory-card header (and the shared logic centralised into
`grid-lens-chart-common.js`) later the same day. `py_compile`/`node --check`'d clean at
every step, the pure carry-over logic unit-tested, and the backend round-trip (services →
entities → advisory log lines showing a scaled `daily_kwh`) was live-verified on the dev
rig repeatedly. The card's visual layout (including the weather-icon boxes and the new
expander) is **not yet clicked through by the owner** (no browser/display in this dev
container — same caveat as `grid-lens-charge-target-card` above and `gridlens-editor`,
docs/CLAUDE.md). Two real bugs were already caught and fixed via live verification before
the owner ever saw it working: (1) the number entities went stale when written through the
new services (a path other than the entity's own slider) until a
`charge_target_store.py`-style dispatcher signal was added — see
`feedback_store_backed_entity_dispatcher_sync` in the assistant's own memory notes for this
repo; (2) a method/property name collision (`_masterEid` used as both a class method and a
cached instance property) crashed the original standalone card on its second `hass`
update, surfaced only in the browser as "configuration error", not by `node --check` — see
`feedback_js_card_name_collision_check` for the grep-based check now run before shipping
any card change.

---

## 10. Cards & the default dashboard

All cards **auto-discover** their entities by attribute fingerprint — never a naming
convention, never a hardcoded entity id — so they work unmodified on any install.

| Card | Shows |
|---|---|
| `grid-lens-card` | Full plan comparison (the Plan Comparison view). |
| `grid-lens-powerflow-card` | **Gated** — live radial energy flow: solar / grid / battery / home + one node per deferrable load, animated flow balls, greedy badges. A **System line** (2026-09-15, moved under the Battery node 2026-09-16) shows the inverter's own self-consumption + conversion losses as a small grey subtext row beneath the Battery node's stats — computed brand-agnostically as the energy-balance residual `(solar + grid − battery) − load_power_entity` (using the signed grid/battery conventions: +import/−export, +charge/−discharge), clamped ≥0, and shown only above the 50 W idle threshold — so the diagram balances (battery discharge ≈ Home + System) instead of silently under-reporting household draw. It started as a separate cog node on the radial fan, but that node re-spread the fan every time the residual crossed the 50 W threshold, so it was folded into the battery's own label block. No vendor-specific "self-consumption" sensor is read, so it works on any inverter brand (this install's Sigenergy publishes one via MQTT as `sensor.sigen_0_self_consumed_power`, but most don't). Classic layout only — scene mode's photoreal anchors have no natural home for an inverter-loss term. The **Grid node carries a persistent "Buy 22.1c · Sell 3.0c" line** (current slot of the dispatch sensor's `trajectory`, via `price_source_entity` / auto-discovered) — both sides always shown, the side currently flowing bold (2026-09-11; was previously an in-play-only "@ Xc/kWh" that vanished while idle). Requires the Battery Control + Power Flow add-on; see §12. `load_power_entity`/`grid_power_entity`/`battery_power_entity`/`battery_discharge_power_entity` are auto-populated in the seeded dashboard straight from the same `load_power_sensor`/`grid_power_sensor`/`battery_charge_power_sensor`/`battery_discharge_power_sensor` config_flow already collects (Sensors/Battery setup steps) — no separate onboarding needed; `solar_power_entity` auto-discovers from HA's own Energy Dashboard prefs; `ev_power_entity`/`ev_active_entity` remain manual-only (no config_flow counterpart — only needed when the EV isn't already represented as a regular deferrable load). Each load node has its own **disable button** (bottom-left corner) to drop it from the diagram — see §5's "Disable button" entry. |
| `grid-lens-power-chart-card` | Measured & forecast power (kW) — solar, load, signed grid, signed battery, per-device deferrable, plus free-energy shading, **plus battery SOC on a right-hand 0–100% axis** (2026-08-28), **plus a per-device predicted SOC curve on that same axis for any deferrable load with an SOC model** (2026-09-07) — dashed, in the device's colour, grouped with the device; when the plan's SOC ceiling is shortening that device's charge (`soc_limited`) a faint flat line marks the ceiling and the tooltip names the kWh held back. **SOC on the historic (left-of-"now") side (2026-09-09):** the measured battery-SOC line — solid, right axis — now actually renders there (it was silently clipped to the forecast side by a missing `actual: true` flag), and a **measured per-device SOC** line (also solid, right axis, device colour) is drawn for any deferrable load whose `soc_entity` config field resolves; base class fetches it via the `_deferSocEntities()` hook into `_actualDeviceSoc`. Dashed = planned, solid = measured, matching the standalone SOC card. Click a legend name to isolate that series (forecast + measured pair, y-axis rescales to it); click it again to restore every series. SOC is exempt from isolation — it sits on its own axis, so keeping it costs the kW rescale nothing and it is context for whatever you isolated. **Left of "now" shows measured data only** (2026-09-08): each measured series carries the sensor's current live state as a final point so the line reaches the "now" divider (previously it stopped at the last recorded history row, up to a minute stale, leaving the forecast line as the only thing drawn there), and **every planned series is clipped at the divider** — the forecast flow lines, their area fills, the planned battery-SOC line and the per-device predicted-SOC curves/ceiling lines all stop at "now" (both axes), so nothing predicted renders in the past; set `show_forecast_history: true` to draw the forecast across the past again for plan-vs-actual comparison. (Still full-width: the free-energy time bands — the teal $0-import window is a known tariff fact, the orange spill band is plan-derived.) Downsampling (`ds()` in `grid-lens-chart-common.js`) also keeps each bucket's largest-magnitude sample for the measured kW series (`{peak:true}`), so a short real transient — a few-minute solar burst — survives instead of collapsing to whatever its bucket ended on. The crosshair (`continuousMeasuredPast` getter) reads the measured overlay at the exact hovered time for anything at or before "now" — including the elapsed part of the current 30-min slot — instead of snapping the read back to the last slot boundary; it only snaps to a slot in the genuine future. The other chart cards (price/cash/dispatch) are per-slot by nature and keep snapping. |
| `grid-lens-price-chart-card` | Buy/sell rate ($/kWh) for the current plan — the forward `trajectory` **plus** the elapsed-today half from the dispatch sensor's `past_rates` attribute (`_combinedTraj()`), so the line covers midnight→horizon-end with a hover crosshair either side of "now". Falls back to forward-only if `past_rates` is absent (restored/cached plan, older integration). On the Power Flow view it sits under the power chart. |

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
| `grid-lens-advisory-card` | Plan status header (plan name/solver/last-run time, status badge), control-mode timeline, deferrable-load recommendations, **plus Daily Target (§9b, relocated 2026-09-22): today/tomorrow solar forecast + master slider in the header, per-device sliders behind a chevron expander** — same header content in both compact and full layouts. **The expanded per-device panel also carries the full load-control row (§6b, merged 2026-09-24): sparkline, Today Boost, Greedy toggles, Off now/On now/Auto, live status, the estimator debug panel — alongside that row's slider.** `compact: true` config renders just the header (incl. the Daily Target/load-control block) — used as a slim "optimiser & plan" status bar at the top of the Power Flow view; `title` config overrides the header text in that mode. `show_current_rates: true` adds a one-line buy/sell readout ("Buy 22c/kWh · Sell 3c/kWh") under the plan-status line — the rate for the slot covering now, from the same `trajectory` attribute. Just the numbers; the rate *graph* is `grid-lens-price-chart-card`. Off by default and **not** used by the seed anymore — the Power Flow view shows the current rate on the `grid-lens-powerflow-card` Grid node instead (2026-09-11). Still available for a dashboard that has no Power Flow card. Works in the full card too. |
| `grid-lens-load-control-card` | One row per deferrable load: Today Boost, greedy toggles, Off now / On now / Auto, and live greedy status. **No longer seeded onto the default Settings view (2026-09-24)** — its default-visible home is now `grid-lens-advisory-card`'s per-device expander on the Power Flow view (§6b), same relocation Daily Target got in §9b. Still installed/registered for a dashboard that wants it as its own card. |
| `grid-lens-daily-target-card` | Standalone Daily Target card (§9b) — same content as `grid-lens-advisory-card`'s header block, always expanded, no chevron. No longer seeded onto the default Settings view (2026-09-22) since the advisory-card header is now the default home; still installed/registered for a dashboard that wants it as its own card. |
| `grid-lens-charge-target-card` | One row per SOC-tracked deferrable load: ad-hoc "charge to X% by a datetime" target (§9a) — a percent tile + a datetime tile, auto-paired via the `charge_target_role`/`deferrable_sensor_id` state attributes, plus a plain-text "Target: 95% by Sat, 2:22 am" / "No target set" status line. Empty state when no device has SOC tracking configured. |
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
`grid-lens-powerflow-card` diagram + `grid-lens-power-chart-card`, with a
`grid-lens-price-chart-card` (current plan's buy/sell rate, elapsed today + forecast) as a
full-width section under the power chart (added 2026-09-10, user request), and the compact
`grid-lens-advisory-card` status bar (see table above) pinned at the top so "when did the
optimiser last run" is visible without switching to the Battery Plan view. The current
buy/sell **rate number** shows on the `grid-lens-powerflow-card` Grid node ("Buy 22.1c ·
Sell 3.0c"); the status bar no longer carries `show_current_rates` (moved 2026-09-11, user
request — the number belongs next to the grid icon, the graph stays under the power chart).

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
| `switch.*_<device>_control` attributes | Commanded state, threshold, override, all three greedy toggles, **`greedy_reason`**, **`greedy_blocked`**, **`forecast_free_kwh` / `forecast_needed_kwh` / `forecast_target_w` / `forecast_battery_headroom_w` / `forecast_battery_headroom_kwh`**, note. `forecast_target_w` is the proportional power condition 3 is asking for right now (0 when it isn't firing); the `_kwh` headroom figure backs the transient-dip check. Modulating devices add `control_type`, `setpoint_entity`, `min_w`/`cap_w`, `commanded_w`/`commanded_setpoint`, `plugged_in`, `last_write`, `modulation_source`. **Refreshed every tick** (`_tick_device` / `_fast_tick_device` call `_notify`, added 2026-09-11) — before, these froze at whatever the last `enable`/`disable`/override action produced, so the card could show `note: not_started` for hours while greedy was in fact evaluating every 5 min. |
| **Load Control card** | Per row: control state, and a live greedy line — the firing reason (condition 3 shows the proportional draw, e.g. "soaking forecast surplus at ~840 W"), or why it's blocked (including **"export is being wasted, but no grid power sensor is set"**, §7 — the only blocked state that will *never* clear on its own, so it names the fix rather than reading as "not yet"), or the **forecast-surplus progress bar** (`6.2 / 8.0 kWh`, hover/focus tooltip explains it — the bar reaches 100% exactly when the rate would clear an on/off device's full draw). Shown both while armed and tracking toward the trigger, and after it's fired (condition 3 held it on) — the same bar, capped at 100%, rather than only appearing pre-trigger. For a modulating device (§6a): live amps + kW, the max-current ceiling input, and a one-line "why" — `modulation_source` (plan / surplus / override / off) and `plugged_in`. "Why is my car charging at 8 A right now?" must be answerable from the row. A device whose SOC ceiling is why today's scheduled charge falls short of its 14-day average (`ev_soc_status.soc_limited`) gets a **"SOC-limited · `<got>` of ~`<target>` kWh"** line next to that average, tooltip explaining the headroom maths and pointing at Max SOC % in Reconfigure (2026-09-07). |
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
| `load_ev_brand` | Charger brand (optional) — detects & pre-fills the next two screens' fields when a match is found on this HA instance | Control style is modulating |
| `load_control` | Control entity (+ climate on-mode for on/off) | Control style is on/off or modulating — **required** for on/off, **optional** for modulating (a setpoint-only charger needs no switch) |
| `load_modulating` | Setpoint, unit, phases, voltage, min current, plug sensor, start/stop button pair | Control style is modulating |
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
- **A modulating load no longer has to be given a control entity** (2026-09-10). The
  `load_control` step made `switch` `vol.Required` for every controllable load, so a
  setpoint-only charger (the common OCPP / Fronius Wattpilot shape — writing 0 A stops
  delivery) could not finish the wizard without binding some unrelated switch, even though
  `ModulatingLoadController` has always defaulted `switch_entity_id=""` and joined on the
  setpoint id. It is now `vol.Optional` when `load_modulating` is queued, and the
  climate-only "on mode" field is dropped for that case.

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

**NSW — Ausgrid** (172 plans). **NSW — Endeavour Energy** started 2026-09-18, its onboardable
candidate pool now exhausted as of 2026-09-19 (51 plans onboarded, including 5 resolved via
retailer fact-sheet research — CovaU/Diamond cap-period and day-coverage-gap fixes; remainder
needs a curated market-linked model, same as Ausgrid's own equivalent gap — see
`gridlens-api/docs/OPEN_ITEMS.md` "Endeavour Energy" for the live breakdown). **QLD — Energex**
onboarded 2026-09-19 (124 plans — 111 standard + 13 market-linked, the latter wired
2026-09-20 to the real AEMO QLD1 spot price via the existing `spot_pricing` model, same
mechanism §"Spot pricing for market-linked alternatives" above; adder unverified for Energex,
see `gridlens-api/docs/OPEN_ITEMS.md`). **QLD — Ergon** and NSW's third DNSP (Essential Energy) not
yet started. Expansion order and rationale
(real 2025 CER/CEC installed-base data, not a guess): finish NSW (Endeavour → Essential) → QLD
(Energex → Ergon) → VIC (blocked on a separate Victorian Energy Compare data pipeline — CDR
PRD doesn't cover VIC) → SA/ACT → WA (lowest priority — non-NEM WEM market, Synergy
near-monopoly retailer). VPP bolt-on programs and Controlled Load are designed and schema-live
in production, with real-data population partway through — see `VPP_CONTROLLED_LOAD_HANDOFF.md`
before touching.

---

## 14. File map

```
custom_components/grid_lens/
├── __init__.py              entry setup, _CARD_VERSION, _build_seed_views (dashboard seed)
├── const.py                 CONF_* keys, parse_hours_spec
├── config_flow.py           setup flow — §12a; options flow + per-load wizard — §12b
├── deferrable_loads.py      one-dict-per-load view over the parallel arrays the rest of
│                            the code reads; the seam the wizard edits through — §12b
├── ev_charger_vendors.py    per-brand EV-charger entity patterns for the optional
│                            load_ev_brand wizard step — §6a
├── plan_calculator.py       plan cost engine
├── retailer_plans.py        plan fetch/cache from the API
├── battery_optimizer.py     the LP/MILP
├── sensor.py                comparison sensors + deferrable_loads attribute
├── plan_sensors.py          per-plan metric sensors
├── switch.py                battery control + per-device control & greedy switches
├── select.py                Force On/Off/Auto override
├── number.py                Today Boost, Minimum Export Price, charge-target percent — §9a,
│                            Daily Target master/per-device percent — §9b
├── datetime.py              charge-target deadline entity — §9a
├── services.py/.yaml        set/clear schedule, calculate_period, set/clear charge_target,
│                            set/clear daily_target — §9b
├── schedule_grid.py         7x48 grid helpers (slot_allowed, week_from_hours)
├── deferrable_schedules.py  schedule Store
├── deferrable_overrides.py  boost Store
├── charge_target.py         ad-hoc charge-target pure maths (slot rounding, reach/expiry) — §9a
├── charge_target_store.py   ad-hoc charge-target Store — §9a
├── daily_target_rules.py    Daily Target pure carry-over logic — §9b
├── daily_targets.py         Daily Target Store (master + per-device) — §9b
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
