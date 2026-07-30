# Tomorrow's Deferrable Load Boost — Design Doc

Status: **planning only, no code written yet.** Written 2026-07-29, grounded in the current
`advisory/coordinator.py` and `battery_optimizer.py` via direct file/line reads. Extends the
already-shipped "today boost" (Feature 2 in `DEFERRABLE_EXPORT_CONTROL_PLAN.md`, live in
`number.py`/`deferrable_overrides.py`) with a second, independent boost for **tomorrow**
specifically — e.g. "I'm driving further tomorrow, charge the EV more tomorrow" without
inflating today's target. Read `GRIDLENS_CHECKLIST.md` first for overall project state.

## Why this isn't just "add a second number entity"

Today's boost is deceptively simple-looking, but it works because of two facts that stop being
true the moment "tomorrow" enters the picture:

**1. The override store only ever means "today."** `override_expiry.read_value()` compares a
stored `set_date` against `today` and returns 0 the moment they don't match
(`override_expiry.py:10-18`). There's no notion of "set for a different, future date" at all.

**2. The LP has no per-calendar-day distinction to begin with.** `_apply_overrides()`
(`advisory/coordinator.py:217-238`) substitutes a single scalar `dev['daily_kwh']`, and that
scalar is applied identically to **every day in the entire rolling horizon** by
`battery_optimizer.py`'s per-device day loop (`:691-710`):

```python
for d in range(n_days):
    t0 = d * slots_per_day
    t1 = min(t0 + slots_per_day, T)
    target = dev['daily_kwh'] * (t1 - t0) / slots_per_day   # <- same value, every day
```

So "today's boost" doesn't actually target *today* at the LP level — it currently boosts the
whole horizon uniformly, and only reads as "today only" because the override **store** expires
it at local midnight before the next advisory tick can pick it up. There is no existing
machinery anywhere in this codebase that gives one calendar day a different target from
another. Adding "tomorrow" requires building that, not just adding a UI control.

**3. Those `n_days` chunks aren't calendar days anyway.** The comment at `battery_optimizer.py:679-690`
is explicit: chunks are anchored to **horizon start** ("now"), not local midnight — a deliberate
fix for a different bug (2026-07-24, GRIDLENS_CHECKLIST.md: forcing a truncated last chunk's full
target manufactured a fake "must run overnight" recommendation). So "day 0" in that loop today
means "the next 24h from whenever the advisory coordinator happens to be ticking," not "today."
If "now" is 6pm, day-0 already contains 6 hours of what a human calls "tomorrow."

Conditional credits (GloBird ZEROHERO's $1/day bonus) hit this exact problem already and solved
it: `build_conditional_credits()` (`retailer_plans.py:562-588`) builds a per-slot `day_index`
from real calendar dates (`dt.toordinal()` in the plan's local timezone), not `t // slots_per_day`,
specifically so a credit window spanning a chunk boundary doesn't get split across two binaries.
The deferrable per-day loop has no equivalent — it's the one piece of day-chunking in this file
that's still horizon-anchored.

## Design

### 1. Override store: key by absolute calendar date, not "today"

Generalize `override_expiry.py` from "was this set today" to "is there a stored value for this
specific date":

```python
def read_value(data: dict, sensor_id: str, target_date: str) -> float:
    """target_date: ISO date string (e.g. "2026-07-30"). 0.0 if unset for that date."""
    entry = (data.get(sensor_id) or {}).get(target_date)
    ...

def write_value(data: dict, sensor_id: str, value_kwh: float, target_date: str) -> dict:
    # per-sensor dict of {iso_date: value_kwh}; 0/negative clears that date's entry
    ...
```

This is a strict generalization, not a breaking change in spirit: "today boost" becomes the
special case of writing/reading with `target_date = today`. The nice side effect: **midnight
rollover needs zero special-case code.** A "tomorrow boost" written today for date `2026-07-30`
is simply whatever gets returned when date `2026-07-30` *becomes* today — no explicit "roll the
override forward" step, no timer, nothing to miss. This is the same self-healing property
`deferrable_overrides.py`'s module docstring already calls out for the existing date-comparison
design, just extended to more than one date.

Add a light prune-on-load (drop entries whose date is in the past) so the stored dict doesn't
grow one entry per device per day forever — cheap, and mirrors nothing dangerous (a dropped
past-date entry was already unreadable/irrelevant).

### 2. Give the LP an actual per-day target — the real decision point

Two ways to get "tomorrow" a distinct target from "today." **Recommended: Option B**, but B is
a materially bigger change than A, so it's written up honestly as a choice, not a foregone
conclusion — same spirit as the export-price-floor doc's soft-vs-hard-constraint call.

**Option A — cheap, approximate: boost by horizon chunk index, not calendar date.**
Extend each `dev` dict with a sparse `daily_kwh_by_chunk: {0: today_val, 1: tomorrow_val}`,
consulted instead of the flat scalar at `battery_optimizer.py:701`. Reuses today's exact chunk
boundaries and truncation logic untouched — smallest possible diff. **The catch:** chunk 0/1
boundaries are horizon-anchored ("now" to "now+24h", "now+24h" to "now+48h"), not
midnight-anchored, so "chunk 1" only approximates "tomorrow." Set a Tomorrow Boost at 6pm and
"chunk 1" actually covers 6pm-tomorrow through 6pm-day-after — it includes 6 hours of the real
day-after-tomorrow and excludes the 6 hours between now and midnight tonight (which sit in
chunk 0, alongside today's own target). The later in the day you set it, the fuzzier "chunk 1 ≈
tomorrow" gets.

**Option B — correct, bigger lift: calendar-anchored day chunking, mirroring conditional
credits.** Build a per-slot `day_index` for the deferrable loop the same way
`build_conditional_credits` already does (`retailer_plans.py:573-578`: real local-timezone date
ordinal per slot), thread it into `optimize_hourly_schedule`/`_lp_scipy` alongside the existing
`conditional_credits`' own `day_index`, and group the per-day equality constraint by that instead
of `d * slots_per_day` (`battery_optimizer.py:693-710`). This makes "tomorrow" mean exactly what
a human means by it, independent of what time the advisory coordinator happens to be ticking.

The real cost of Option B: **the truncated-chunk relaxation becomes two-sided.**
`truncated_days` today only ever contains the *last* chunk, because chunks are horizon-anchored
and only the tail can be short (`battery_optimizer.py:472-475`, "only the LAST chunk can ever be
short"). Under calendar anchoring, the **first** calendar day is also almost always partial (the
horizon starts at "now," partway through today), so the ≤-cap-instead-of-=-equality treatment
that today only guards the last chunk needs to apply to the first one too — otherwise a solve
that starts at, say, 9pm would force-equal "today's" already-mostly-elapsed remainder to the
full daily target, which is exactly the kind of manufactured-nonsense recommendation the
truncated-day fix was written to prevent in the first place. This needs care, but it's a
generalization of logic that already exists and is already tested, not new territory.

**Recommendation: Option B.** Option A's approximation gets worse specifically in the evening —
which, based on the EV-charging motivating case, is exactly when a user is most likely to be
setting a "charge more tomorrow" boost after finishing today's drive. A control whose meaning
visibly drifts depending on what time you happen to set it is a bad UX trap. Size the
implementation session for B accordingly (bigger than Option A, comparable to the original
Feature 2 lift).

### 3. Horizon length: "tomorrow" isn't always fully visible

`HORIZON_HOURS = 36` (`advisory/coordinator.py:47`). A 36-hour rolling window from "now" only
fully contains tomorrow's calendar day when "now" is roughly before mid-morning; ticking in the
evening, the horizon runs out partway through tomorrow (e.g. "now" = 18:00 → horizon ends 06:00
the day after tomorrow, i.e. tomorrow is only ~¾ visible). Whatever fraction of tomorrow falls
outside the horizon simply can't be planned for yet — the LP can't allocate a target to slots it
can't see.

**Proposed mitigation:** when any device has a non-zero tomorrow-override active, temporarily
extend this solve's horizon (e.g. to 48h) so tomorrow is always fully in view — cheap to do
conditionally (only pay the extra solve size on the days someone's actually using the feature)
and avoids silently under-delivering a boost the user explicitly asked for. Needs to thread
through `bundle`/forecast-provider sizing (`n_slots = int(HORIZON_HOURS * 60 / SLOT_MINUTES)`,
`:441`) as a per-tick override rather than a constant.

### 4. Coordinator wiring

`_apply_overrides()` (`:217-238`) currently does one `store.async_get()` per device and
substitutes a flat scalar. Replace with a per-day lookup keyed by the same calendar dates the
new `day_index` (from §2 Option B) uses — e.g. build `dev['daily_kwh_by_date'] = {iso_date:
override_kwh}` for whichever of today/tomorrow have an active override, leaving other days to
fall through to the historical `daily_kwh` average exactly as today. `_deferrable_for_horizon()`
(`:257-283`) passes this dict through into the dict it hands to `optimize_hourly_schedule`
alongside `hour_mask`.

Scope stays advisory-only, matching Feature 2's existing precedent — `plan_calculator.py` (the
plan-comparison path) never reads the override store today and shouldn't start here either; that
path is deliberately a 14-day historical average for *comparing plans*, not a live-control
target.

### 5. UI: a second boost entity per device, plus an existing staleness gap worth closing here

`number.py`'s `GridLensDeferrableOverrideNumber` becomes parametrized by which date it targets
(today / tomorrow), each computing its target date fresh (`dt_util.now().date() + timedelta(days=offset)`)
rather than baking in a fixed date at entity construction — so, like the store, the entity
automatically tracks "tomorrow" forward as calendar days roll over.

**Related pre-existing gap, worth fixing in the same change:** today's boost entity's displayed
value is set once in `async_added_to_hass` (`number.py:152-155`) and never refreshed after that
— the LP-facing value is always correct (the coordinator re-reads the store fresh every tick,
`_apply_overrides:230`), but the **dashboard tile** could keep showing yesterday's now-expired
number until the entity happens to be rewritten or HA restarts. This was low-stakes with a
single "today" entity (worst case: a stale display of a value that's already functionally 0).
It becomes more visible with two entities whose target dates both shift at midnight — the
"Today" tile should visibly pick up what was "Tomorrow" a moment ago. Fix: each entity schedules
its own `async_track_time_change(hour=0, minute=0, second=5)` (small offset past midnight to
avoid racing the date rollover) to re-read from the store and `async_write_ha_state()`,
unsubscribed in `async_will_remove_from_hass` — self-contained per entity, no new shared
dispatcher needed.

`grid-lens-boost-tuning-card.js`'s auto-discovery fingerprint (`deferrable_sensor_id` state
attribute) already generalizes cleanly to two entities per device — no card-side discovery
change needed. It does need a small display change: group the two tiles per device (or label
them "EV Mobile Charger — Today" / "EV Mobile Charger — Tomorrow") so the card doesn't just show
four same-named tiles with no way to tell them apart. Cheapest fix: add a
`boost_horizon: "today" | "tomorrow"` state attribute alongside `deferrable_sensor_id` and have
the card append it to the resolved device name.

## Open questions

1. **Option A vs B** — confirm B (calendar-correct) is worth the bigger lift, given the doc's
   recommendation, before implementation sizing.
2. **Horizon auto-extend (§3)** — worth the added solve cost on boost-active days, or is a
   "your tomorrow boost may only partially apply late in the day" caveat in the tile's
   description acceptable instead?
3. **Does the horizon even need to go past "tomorrow"?** At 36-48h, "the day after tomorrow" is
   barely or not at all in view — this doc scopes to exactly two controls (today/tomorrow), not
   an N-days-ahead generalization. Confirm that's sufficient (seems very likely, given the
   motivating case).
4. **Max value / cap** — today's boost caps at `max_kw * 24` (`number.py:144`). Same ceiling for
   tomorrow, presumably — flagging only because it's worth confirming rather than assuming.

## Changes (once Option A/B is settled)

- `override_expiry.py`: date-keyed store schema (§1) — `tests/test_deferrable_override.py`
  needs matching updates (pure-Python, no HA import, per its own docstring).
- `deferrable_overrides.py`: `async_get`/`async_set` take a `target_date` param; add prune-on-load.
- `retailer_plans.py` or a new small shared helper: factor the per-slot calendar-date `day_index`
  builder out of `build_conditional_credits` (`:562-588`) so the deferrable path can reuse it
  without duplicating the timezone/ordinal logic.
- `battery_optimizer.py`: thread a deferrable-specific `day_index` alongside the existing
  credit-block one; rework `truncated_days` (`:472-475`) and the per-day equality/ub split
  (`:691-710`) to be two-sided under calendar anchoring (Option B).
- `advisory/coordinator.py`: `_apply_overrides` (`:217-238`) and `_deferrable_for_horizon`
  (`:257-283`) per §4; optional horizon auto-extend per §3.
- `number.py`: parametrize `GridLensDeferrableOverrideNumber` by target-date offset (§5); add
  the midnight self-refresh listener.
- `www/cards/grid-lens-boost-tuning-card.js`: per-device grouping/labeling for two tiles (§5).
- Bump `_CARD_VERSION`, `sync-to-ha.sh`, per the usual workflow.

## Verification

Unlike the animated-icons card work, this one **does** need the scipy/LXC round-trip (LP
changes) — same pattern as every other `battery_optimizer.py` change in this repo: scp the file
plus a synthetic-scenario test script to the LXC, run under `docker run --rm python:3.12-slim`
with `pip install scipy`.

1. Synthetic two-day horizon, distinct today/tomorrow overrides on one device: confirm each
   day's `Σ def_i[t]` matches its own target, not a blended/uniform value.
2. Confirm the *first* calendar-day chunk gets the same ≤-cap truncation treatment as the last
   currently does, when the solve starts late in the day (Option B) — this is the new edge case
   that doesn't exist today.
3. Live: set a Tomorrow Boost in the evening, confirm the dashboard's "Today" tile picks it up
   automatically after local midnight without a restart (§5 self-refresh).
4. Confirm setting *only* a tomorrow override leaves today's schedule byte-identical to no
   override at all (no accidental cross-day leakage).
