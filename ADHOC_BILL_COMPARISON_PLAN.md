# Ad-hoc Paper Bill Comparison — Design Doc

Status: **planning only, no code written yet.** Written 2026-08-09, grounded in direct reads
of `plan_calculator.py`, `retailer_plans.py`, `__init__.py` (view registration + `PlanDataView`),
and `config_flow.py` on this install. Read `docs/GRIDLENS_CHECKLIST.md` and `FEATURES.md` §1 (Plan
comparison) first for how the *existing* comparison engine works — this doc only covers what's
new.

## What this is

Let a user type in numbers off a **paper/PDF bill for a property that isn't wired into this HA
instance at all** (a rental, a family member's place, a second premises with no GridLens sensors)
and get a ranked list of "what would this have cost on plan X" — without creating any entity,
writing to any Store, or touching any of the `sensor.*_current_plan_monthly_cost` /
`sensor.*_best_alternative_plan` figures that describe *this* household. Two different problems
worth separating up front:

1. **Isolation from the user's own figures** — easy, and covered in full below.
2. **Comparison fidelity from paper-bill-only data** — inherently coarser than the sensor-driven
   comparison, not an implementation gap. Scope the feature to what a paper bill can actually
   support rather than trying to fake the rest.

## Why isolation is basically free here

The existing comparison isn't one monolithic thing — it's two layers, and only the top one is
HA-specific:

- **`retailer_plans.py`'s `RetailerPlan` / `PlanFromData` classes are pure rate-structure
  objects.** `get_import_rate(dt)`, `get_export_rate(dt)`, `get_import_rate_info(dt)` take a
  datetime and return a rate — nothing in this file touches `hass`, the recorder, or any entity.
- **`plan_calculator.py` is the HA-specific half**: it pulls `usage_data`/`export_data` as
  `list[{"timestamp": datetime, "value": kwh}]` out of the recorder (`statistics_during_period`,
  `state_changes_during_period`) for *this* install's configured sensors, then feeds those lists
  through `_compute_bill_items()` — which, notably, doesn't care where `usage_data` came from.

So an ad-hoc path doesn't need to touch the recorder, the coordinator, or any Store at all: it
only needs to *synthesize* a `usage_data`/`export_data` list from manually-typed totals instead
of pulling one from history, then run it through the same rate-structure code. Nothing about that
requires reading or writing anything that belongs to the user's own account.

**Concrete isolation guarantees for the new code path:**
- New HTTP view is **stateless per-request** — no `hass.data[DOMAIN]` mutation, no
  `Store.async_save()`, no entity creation. It reads the config entry only for the API key
  (to call `gridlens-api`'s `/plans`, same as `PlanDataView` already does) — never for the user's
  own state/network/sensors.
- State + network for the ad-hoc property are **request parameters**, not read from the config
  entry — so this also transparently handles "another service" meaning a different state/DNSP,
  not just a different account at the same address.
- Nothing persists across requests by default (see "Open questions" for the optional save-a-
  scenario extension, which is out of scope for v1).

## The data ceiling: what a paper bill can and can't support

`_split_capped_kwh()` and `get_import_rate_info()` key everything off a **datetime** so
time-of-use windows, weekday/weekend rate differences, and daily caps resolve correctly against
real interval data. A paper bill gives none of that — at best a peak/shoulder/off-peak *kWh
total* for the whole period (if the meter is TOU-metered and the retailer itemises it that way),
at worst a single total kWh figure. There's no way to recover which half-hour any of that energy
was actually used in, so plan types whose cost genuinely depends on shape, not just volume, can't
be modelled honestly from paper-bill input:

| Plan feature | Needs | Paper-bill verdict |
|---|---|---|
| Flat rate | total kWh | ✅ exact |
| Simple TOU (peak/shoulder/off-peak $/kWh) | kWh per window | ✅ exact, if the bill itemises the split (most TOU bills do); otherwise the user must estimate a peak/off-peak split, which the form should flag as an estimate |
| Daily supply charge | days in period | ✅ exact |
| Flat FiT | export kWh total | ✅ exact |
| Capped/tiered rate (free-then-paid daily block) | which day each kWh fell on | ⚠️ approximate — requires assuming usage is spread evenly across days in the period, since a paper bill's total doesn't say which days blew the cap |
| Demand tariff (peak kW) | interval peak demand | ❌ not derivable from a bill total — exclude these plans from ad-hoc candidates, don't guess |
| Conditional day-credits (e.g. GloBird ZEROHERO's $1/day) | per-day, per-hour behaviour | ❌ not derivable — exclude, or show the plan's base rate only with the credit called out as "not evaluated, needs interval data" |
| Market-linked / wholesale FiT | real dispatch prices | ❌ exclude — no sensible way to model without a live price feed |

This isn't a corner someone cut — it's the actual information content of a paper bill. The
right move is to **filter the candidate plan list to types the input actually supports**, and
label excluded plans as "needs your smart meter's interval data — not available from a bill
total" rather than silently mispricing them (see `CLAUDE.md`'s bill-breakdown accuracy rule —
same principle applies to *not* producing a number that looks precise but isn't grounded).

## Design

### 1. Extract the tier-splitting logic to be instance-independent

`PlanCalculator._split_capped_kwh()` (`plan_calculator.py:970`) is already a pure function in
every way that matters — it takes `(plan, direction, local_dt, kwh, daily_used, cap_labels)` and
never touches `self`. Move it to a module-level function in `retailer_plans.py` (or a new
`rate_math.py` shared by both), and have `PlanCalculator` call the module-level version. This is
a no-behavior-change refactor that lets the new ad-hoc path reuse the exact same capped-rate
logic instead of forking a second copy that can drift.

### 2. New module: `adhoc_bill_calculator.py`

Pure-ish module, HA-aware only for the aiohttp session:

```python
async def compare_adhoc_bill(
    hass, api_key: str, api_url: str,
    state: str, network: str,
    period_days: int,
    usage_kwh: dict,   # {"peak": .., "shoulder": .., "off_peak": ..} or {"total": ..}
    export_kwh: float = 0.0,
    weekday_weekend_split: dict | None = None,  # optional finer input, v2
) -> dict:
```

Steps:
1. **Synthesize `usage_data`/`export_data`.** For each category the user entered (e.g. "peak"),
   walk every day in the period, find every half-hour slot on that day whose
   `plan.get_import_rate_info(dt)["label"]` would fall in that category *for a representative
   plan* — actually simpler and plan-agnostic: allocate each category's total evenly across all
   calendar slots that a **generic TOU calendar** (weekday/weekend × time-of-day, using the same
   day-type logic `PlanFromData` already uses) assigns to that category name, so the synthesized
   list carries realistic timestamps rather than being time-blind. If the user only gives a
   single total (no TOU split), spread evenly across all slots — mathematically equivalent to a
   flat allocation, which is the right answer when there's no better information.
2. **Fetch candidate plans** for `state`/`network` via the same call `PlanDataView` already makes
   (`GET {api_url}/plans?state=..&network=..`, `X-API-Key` header, `async_get_clientsession(hass)`
   — see `feedback_ha_aiohttp` — no `current_plan` param needed here since there's no "current
   plan" for a premises this install doesn't meter).
3. **Filter** candidates to plan types the input supports (table above) — check
   `plan.demand_charge_per_kw_per_day`, `plan.is_market_linked`, `plan.get_conditional_credits()`,
   and drop or flag rather than modelling them blind.
4. **Cost each surviving plan** via the extracted tier-splitting function + `plan.daily_supply_charge
   * period_days` + flat export credit — this is a trimmed-down version of
   `_compute_bill_items`'s "historical fallback" branch (`plan_calculator.py:~1240`), not the
   LP-optimised branch (no LP here — there's no battery/solar shape to optimise against).
5. Return a ranked list: `[{plan_id, retailer, plan_name, estimated_total, breakdown, caveats}]`,
   `caveats` naming anything approximated (even peak/off-peak split, capped-rate day-spread
   assumption) so the UI can show it inline rather than presenting a bare number.

### 3. New stateless endpoint

`POST /api/grid_lens/adhoc_bill_compare` in `__init__.py`, following the existing
`HomeAssistantView` pattern (`PlanDataView` at `__init__.py:424` is the closest analogue).
Request body: state, network, period start/end (or days), usage categories + kWh, export kWh.
Response: the ranked list from step 5 above. `requires_auth` should follow whatever the rest of
the `/api/grid_lens/*` views do today (`PlanDataView` currently sets `requires_auth = False`,
relying on being same-origin-only from the HA frontend — match that for consistency, revisit if
this ever gets exposed outside the local dashboard).

### 4. New frontend card: `grid-lens-adhoc-bill-card.js`

Form fields: state dropdown, network dropdown (populate from the `/plans` response's
`network_operators`, same as `PlanDataView` already returns — no need to duplicate
`const.py`'s `DISTRIBUTORS` map client-side), billing period (start/end date or a day count),
usage input (start with a single total-kWh field plus optional peak/shoulder/off-peak fields for
users whose bill itemises TOU), export kWh (optional). Result: a ranked plan list styled like the
existing best-alternative card, each row showing the estimated total and any caveats from step 5.
Explicitly label the whole card "estimate from bill totals" in the UI copy so it's never confused
with the sensor-driven comparison's precision.

**Placement:** this doesn't belong on the main energy dashboard (`_build_seed_views()`) — it's a
one-off tool, not an ongoing status view. A separate optional dashboard view/tab, or a card the
user manually adds where they like, fits better than seeding it everywhere by default.

## Open questions (need your call before implementation)

1. **Free or gated?** Given `feedback_marketing_asset_gating` — this is a good acquisition tool
   (works for a non-customer's property, no GridLens sensors required) but costs API calls to
   `gridlens-api`. Recommend: free, unlimited-ish but rate-limited per API key, since its whole
   value is "try before you commit your own house's data."
2. **Persistence.** v1 above is fully stateless (nothing saved). If you want to revisit a saved
   ad-hoc scenario later (e.g. compare a rental property's bill across a few months), that needs
   a small new `Store` keyed separately from every existing one — deliberately deferred out of v1
   so isolation stays trivially obviously true rather than "isolated because I was careful."
3. **TOU category granularity.** Start with peak/shoulder/off-peak (matches how most AU TOU bills
   itemise) — flag if you want per-season or per-weekday-vs-weekend input too; that's more form
   complexity for a fidelity gain a paper bill mostly can't back up anyway.

## Effort estimate

- Extract `_split_capped_kwh` to module scope: ~30 min, mechanical.
- `adhoc_bill_calculator.py` (synthesis + fetch + filter + cost): ~half a day, the synthesis-timestamp
  logic (step 1) is the fiddliest part.
- New view: ~30 min, directly mirrors `PlanDataView`'s fetch pattern.
- New card: ~half a day (form + result rendering, no new visual language needed).
- **Total: roughly one focused day**, plus `FEATURES.md`/`docs.html` updates per `CLAUDE.md`'s
  documentation-is-part-of-the-work rule once it ships.
