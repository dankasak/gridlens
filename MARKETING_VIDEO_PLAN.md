# Grid Lens — Video Program

Plan for the whole video output: **tutorials** (one per major feature, for docs + organic
YouTube) and **ads** (short, for paid push). Includes the exact capture list for the human
holding the screen recorder, and the ComfyUI pipeline for everything that isn't a screen
recording.

Read `FEATURES.md` first — every script below is downstream of it. If a feature changes,
the script changes.

---

## 0. Division of labour

There are three kinds of footage and they're produced completely differently. **Do not try
to make ComfyUI generate a dashboard** — it will produce a plausible-looking fake UI, which
is both useless and, on an ad, dishonest.

| Kind | Source | Who/what makes it |
|---|---|---|
| **Product footage** | Real Home Assistant, real data | **You**, screen recording / screenshots |
| **Motion graphics** | Titles, callouts, arrows, numbers | Rendered from HTML/CSS (see §5) |
| **B-roll / atmosphere** | House, roof, sunrise, EV, meter box | **ComfyUI** (Wan 2.2 i2v / t2v), or stock |

**Rule: every claim in an ad must be visible in real product footage.** ComfyUI b-roll sets
mood between claims; it never *is* the claim.

---

## 1. Capture guide — what I need you to record

This is the part only you can do. Do this **once, well**, and every video below can be cut
from it. Total: about 40 minutes of your time.

### 1.1 Setup before you record

- **Browser**: Chrome/Edge, a fresh profile with no extensions, no bookmarks bar.
- **Window size**: exactly **1920×1080**. Not maximised-on-a-4K — an actual 1920×1080
  viewport, so text is crisp at 1080p and nothing needs rescaling.
- **HA theme**: record **both** light and dark for the hero shots (§1.2 items 1–3). Dark
  reads better in ads; light reads better in docs.
- **Zoom**: browser at 100%. If the dashboard looks sparse, use HA's own column settings —
  don't zoom.
- **Hide personal data**: your email in the Grid Lens config, your address if any card shows
  it, and the sidebar's other dashboards if they name family members. Easiest: make a clean
  admin user with only the Grid Lens dashboard in its sidebar.
- **Recorder**: OBS, 1920×1080, 60 fps, no webcam, no mic (VO is added later). Save as MP4.
- **Cursor**: turn ON cursor highlight/click-ring in OBS. It matters enormously for tutorials.

### 1.2 The shot list

Record each as a **separate file**, named as given. Don't worry about mistakes — pause, redo
the action, keep rolling; it gets cut.

**Hero / establishing (used in almost every video)**
1. `hero-dashboard-scroll.mp4` — the Grid Lens dashboard, slow scroll top to bottom, ~15 s.
   Do it twice: light and dark.
2. `hero-powerflow-idle.mp4` — the Power Flow card **full screen**, 30 s, untouched, while
   energy is actually flowing (solar generating + battery doing something). The animated flow
   balls are the single most watchable thing this product has. Get this at midday.
3. `hero-powerchart.mp4` — the Power Chart card full screen, 20 s. Then slowly drag the
   crosshair across a full day so tooltips pop. **Do this on a day with a visible orange
   free-energy band** if you can — that's the money shot for the Greedy video.

**Plan comparison**
4. `compare-table.mp4` — the Plan Comparison view, showing the ranked list of plans with
   costs. Slow scroll. Let the savings figure sit on screen for 3 s.
5. `compare-daterange.mp4` — change the comparison date range and let it recalculate.

**Setup / onboarding (for the "getting started" tutorial)**
6. `setup-hacs.mp4` — HACS → custom repository → add → install Grid Lens.
7. `setup-configflow.mp4` — Settings → Devices & Services → Add Integration → Grid Lens →
   through every step of the wizard: state/network, sensors, current plan. Go **slowly**;
   pause a beat on each screen before clicking Next.
8. `setup-dashboard-appears.mp4` — the sidebar after restart, clicking into the new Grid Lens
   dashboard for the first time.

**Battery**
9. `battery-plan-view.mp4` — the Battery Plan view: SOC curve + dispatch timeline + tiles.
10. `battery-control-toggle.mp4` — toggling `Battery Control` on, then opening the entity's
    attributes (more-info → Attributes) so the applied action / plan age are visible.

**Deferrable loads & control**
11. `loadcontrol-card.mp4` — the Deferrable Loads card, 15 s static, then hover each control
    so tooltips appear.
12. `loadcontrol-override.mp4` — click **On now**, wait for the appliance's own switch entity
    to flip on screen, then **Auto**. If you can get a real appliance visibly reacting
    (a smart plug's state, a pool pump's power sensor rising), that's gold.
13. `schedule-card-paint.mp4` — the Allowed Run Times card: paint a block of hours with the
    mouse, save. This is very satisfying to watch — take your time, make it look deliberate.
14. `boost-set.mp4` — type a Today Boost value (e.g. 25) and let the plan re-solve; ideally
    cut back to the Power Chart afterward showing the device's planned block get bigger.

**Greedy Consumption (the new feature)**
15. `greedy-toggles.mp4` — the three greedy pills on a load row, toggled one at a time,
    hovering each to show its tooltip.
16. `greedy-progress.mp4` — **the important one.** The Load Control card while
    `Greedy Forecast Surplus` is armed and the progress line is showing
    `X / Y kWh` with a partly-filled bar. Hold for 10 s.
17. `greedy-fires.mp4` — if you can catch it: the moment the load turns on and the badge
    appears on the Power Flow card + the greedy line goes green. **This may take patience** —
    a good midday with a full battery and a $0 export price is the setup. If you can't catch
    it naturally, use **Force On** for the visual and *say so in the VO* — never imply a
    staged moment was organic.
18. `greedy-band-hover.mp4` — Power Chart, hovering inside an orange band so the tooltip
    reads "Spilling X kW at $0 — free energy wasted".

**Stills (screenshots, PNG, same 1920×1080)**
19. `still-powerflow.png`, `still-powerchart.png`, `still-compare.png`,
    `still-loadcontrol.png`, `still-schedule.png` — clean, no cursor. These feed the docs
    site, the thumbnails, and the ComfyUI i2v pipeline (§4).

### 1.3 What makes these usable vs. not

- **Don't narrate while recording.** VO is added later against a script; live narration
  locks the pacing and can't be re-cut.
- **Move the mouse deliberately and slowly.** Fast mouse movement is unreadable at 30 fps
  after compression and is the #1 reason tutorial footage gets rebuilt.
- **Pause 2 s before and after every click.** Gives the editor room to cut and the viewer
  room to follow.
- **Real numbers only.** Never edit a savings figure. If your real savings are unimpressive,
  we say so honestly and sell the mechanism instead — see §3's honesty rules.

---

## 2. Tutorial series (organic YouTube + embedded in docs)

House style: **screen recording + voiceover, no talking head, 60–90 s each**, one job per
video, ends by pointing at the next one. Title format: `Grid Lens — <verb phrase>`.

Each entry below: **length · footage · script**. Script lines are VO; `[bracketed]` is
on-screen action.

---

### T1 — "What Grid Lens actually does" (90 s, the pillar video)

*Footage: 1, 2, 4, 9.*

> Most electricity comparison sites ask you three questions and guess the rest.
> [1: dashboard scroll]
> Grid Lens does something different. It already knows your house — because it lives inside
> your Home Assistant, and it's been watching your real meter, your solar, and your battery.
> [2: powerflow, let it breathe 4 s]
> So when it compares plans, it isn't comparing averages. It's simulating *your* house on
> *every* plan — including how a smart battery and a shiftable load would behave under each
> one.
> [4: comparison table]
> That's the difference between "this plan has a cheaper off-peak rate" and "this plan saves
> *you* eleven dollars a month, because your car charges at 2am anyway."
> [9: battery plan view]
> And once it knows the cheapest way to run your house — it can just run it.
> Your data never leaves your network. There are no referral links and no retailer deals.
> Free to model your own plan. A dollar a month to compare them all.
> [end card]

---

### T2 — "Install and set up in five minutes" (90 s)

*Footage: 6, 7, 8.*

> [6] Grid Lens installs through HACS as a custom repository. Add the repo, install, restart.
> [7, step by step] Then add the integration. First, where you are — your state, and your
> network distributor. That's the company that owns the poles and wires, not your retailer;
> it's on your bill.
> Next, your sensors. If your Home Assistant Energy dashboard already works, these are
> already here — Grid Lens reads the same list.
> Then your current plan. This is your baseline, and modelling it is free forever.
> [8] Restart, and Grid Lens adds its own dashboard to your sidebar. That's it — no cards to
> configure, no YAML.
> Give it a day of history before you judge the numbers. The optimiser wants at least
> twenty-four hours to work with.

---

### T3 — "Reading your Power Flow and Power Chart" (75 s)

*Footage: 2, 3, 18.*

> [2] This is your house, right now. Solar in, grid in or out, battery charging or
> discharging, and every deferrable load you've configured as its own node.
> The moving balls are real power — and they're all on one scale, so a four-kilowatt flow
> looks the same size whether it's your battery or your hot water.
> The price under Grid is what the optimiser is actually pricing against right now — not a
> raw retailer feed, the plan it's working from.
> [3] The Power Chart is the same house over time. Solid lines are the forecast, thin lines
> are what actually happened, so you can see how well the plan is tracking reality.
> [18: hover an orange band] And this shading is Grid Lens telling you something useful:
> orange means the plan expects to spill energy it can't sell — free energy about to go to
> waste. Teal means a window where importing costs you nothing.
> Remember those colours. They're the whole idea behind the next video.

---

### T4 — "Comparing every plan honestly" (75 s)

*Footage: 4, 5.*

> [4] Every plan available on your network, ranked by what it would have cost *you*.
> Not a headline rate — a full simulation, over your real usage, for the period you choose.
> [5: change date range] Change the window and it re-solves. A plan that wins over summer
> can lose over winter, and you should be able to see that.
> Here's the part that's hard to do honestly: a plan with a great overnight rate is only
> worth something if you can *move load into it*. So Grid Lens scores each plan by working
> out the best way to run your house under that plan — battery, EV charger, hot water and
> all — and then compares the bottom lines.
> We take no commission and have no retailer relationships. If your current plan wins, it
> says so.

---

### T5 — "Let Grid Lens run your battery" (80 s)

*Footage: 9, 10, 2.*

> [9] Once Grid Lens knows the cheapest way to run your house, it can do it.
> This is the plan: charge here, hold here, discharge into the expensive window there.
> [10: toggle Battery Control] One switch hands the battery over. From then on, every five
> minutes, Grid Lens reconciles your inverter against the plan.
> It is deliberately careful. It respects your SOC limits. It won't grid-charge if you've
> told it not to. And if Home Assistant restarts, or the plan goes stale, it hands the
> battery straight back to its own built-in EMS rather than leaving it in a forced mode.
> [10: attributes] Everything it's doing is visible — the action it applied, when, and how
> old the plan is.
> Battery control is brand-agnostic by design. Sigenergy ships today; the driver interface
> is small on purpose.

---

### T6 — "Deferrable loads: shift what you can" (85 s)

*Footage: 11, 13, 14, 3.*

> [11] A deferrable load is anything where you care that it happens, but not exactly when.
> Hot water. Pool pump. The car.
> Grid Lens learns roughly how much energy each one needs per day from its own sensor, then
> decides *when* to run it.
> [13: paint the schedule] You decide when it's *allowed* to. Paint the hours — per weekday,
> half-hour resolution. The pool pump can be daytime-only; the car can be anytime.
> [14: set a boost] And when today isn't typical, override it. Big drive tomorrow? Tell it
> twenty-five kilowatt-hours instead of the usual thirteen, and the plan re-solves around it.
> If you ask for more than the allowed window can physically deliver, it tells you the
> ceiling instead of quietly ignoring you.
> [3] Then you can watch the plan move the load into the cheap window.

---

### T7 — "Greedy Consumption: never waste free energy" (90 s) ⭐

*Footage: 3/18, 15, 16, 17, 2.*

> [18: orange band] Here's a thing that happens on every solar house. Your battery fills up.
> Your panels keep producing. And the surplus goes out to the grid for a feed-in tariff of
> basically nothing.
> That's free energy, and you're giving it away.
> [15] Greedy Consumption is Grid Lens's answer. Turn it on for a load, and that load will
> opportunistically run any time energy is genuinely free — regardless of what the plan
> scheduled.
> There are three triggers. The first two are simple and can never cost you a cent: import
> is free this window, or you're exporting more than this appliance draws at a feed-in of
> zero — so running it can't create new import.
> [16: the progress bar] The third one is smarter, and it's opt-in on its own. Instead of
> waiting for free energy to show up, it looks *ahead*. If the plan says more energy is
> about to be spilled than this load could possibly use — it starts the load early.
> Because waiting is the expensive option. By the time your export actually shows up, you've
> already lost hours of run-time.
> This bar is it counting down to that decision.
> [17: it fires; badge appears] And when it acts, it tells you why.
> Every trigger respects your schedule if you ask it to, and a manual override always wins.
> The human at the switch is always right.

---

### T8 — "When you want to take over" (45 s)

*Footage: 12, 11.*

> [12] Grid Lens is not trying to win an argument with you.
> Every controllable load has On now, Off now, and Auto. Hit On now and the appliance turns
> on and *stays* on — Grid Lens stops driving it entirely, including its own safety re-asserts.
> Auto hands it back.
> And the failure behaviour is deliberately boring: if Grid Lens loses its plan, or Home
> Assistant restarts, it never yanks a running appliance off. It just stops driving it and
> leaves it exactly as it is.

---

## 3. Ads (paid push: YouTube pre-roll, Reels/Shorts)

Three lengths. All must survive **muted playback** — burn in captions, and make the visual
carry the claim.

### Honesty rules (non-negotiable)
- Real footage, real numbers, real dashboard. No mocked-up UI, no invented savings figure.
- Never state a specific dollar saving as typical. Savings depend entirely on the house.
  Say **"see what you'd save"**, not "save $X".
- Don't name or disparage a retailer.
- "Your data stays home" is true and is the strongest differentiator — lead with it.

---

### A1 — 15 s pre-roll (skippable; the first 5 s do all the work)

| t | Visual | Caption / VO |
|---|---|---|
| 0–3 s | `hero-powerflow` full screen, balls moving | **"Your solar is giving energy away."** |
| 3–7 s | Power Chart, orange band pulses | "Every day your battery fills — and the rest goes out for nothing." |
| 7–12 s | Load Control card, greedy line goes green; load node badge appears | "Grid Lens catches it. And uses it." |
| 12–15 s | Logo + URL | **"Grid Lens. Free for Home Assistant."** `gridlens.au` |

### A2 — 30 s (the main paid unit)

| t | Visual | Caption / VO |
|---|---|---|
| 0–4 s | `hero-powerflow` | "This is a house that already knows everything about its own energy." |
| 4–9 s | `compare-table` | "So why compare electricity plans with a form and a guess?" |
| 9–16 s | Comparison ranking scrolls, savings figure lands | "Grid Lens simulates your actual house on every plan in your network. Solar, battery, EV and all." |
| 16–23 s | `battery-plan-view` → `battery-control-toggle` | "Then it runs it. Battery, hot water, the car — on the cheapest schedule it can find." |
| 23–27 s | Text over powerflow | **"No referral links. No retailer deals. Your usage data never leaves your house."** |
| 27–30 s | Logo + URL | "Free to start. One dollar a month for everything." |

### A3 — 60 s (YouTube in-stream, retargeting)

Structure: **A2's arc, but earn it.** 0–10 s the problem (spilling free energy, and not
knowing if your plan is right); 10–25 s comparison shown properly with a date-range change;
25–45 s control — battery plan, load schedule painting, greedy firing; 45–55 s the trust
block (local, neutral, open about what leaves the house); 55–60 s CTA.

### A4 — 20 s vertical (Shorts / Reels)

Crop to the **Power Flow card alone** — it's radial and survives a 9:16 crop better than any
other card. Single claim: *"Your battery fills. Your solar keeps going. Grid Lens spends the
difference instead of giving it away."*

---

## 4. ComfyUI pipeline for b-roll and motion

GPU box: `ssh gpu` (192.168.1.128). ComfyUI `http://192.168.1.128:8188`,
`~/src/comfyui/`. See `ANIMATED_ICON_GENERATION.md` for the environment details — same
install, same rules (**avoid `api_` nodes**; this install runs `--disable-api-nodes` so they
fail loudly rather than billing).

### 4.1 What to generate

Atmosphere only, 3–5 s each, cut between product shots:

| Clip | Prompt direction |
|---|---|
| `broll-roof-dawn` | Australian suburban roof with solar panels, first light, slow drift, warm |
| `broll-meterbox` | Domestic switchboard/meter box, shallow depth of field, subtle light |
| `broll-ev-charging` | EV charging cable plugged in at night, garage, soft ambient glow |
| `broll-hotwater` | Hot water cylinder in a laundry, morning light through a window |
| `broll-suburb-dusk` | Wide suburban street at dusk, lights coming on, very slow push |

### 4.2 How

**Preferred: I2V (Wan 2.2)** — generate or source a still, then animate it. Far more
controllable than t2v, and it's how you keep a consistent look across clips.
Workflow: `temp/comfyui-jobs/10_moon_i2v_EDITABLE_UI_FORMAT.json` — swap the LoadImage and
prompt. Put source stills in `~/src/comfyui/basedir/input/`.

**Stills:** shoot them on a phone (best — real, local, and free of AI artefacts) or generate
with the Flux.2 Klein workflow
(`temp/comfyui-jobs/12_flux2_klein_edit_EDITABLE_UI_FORMAT.json`), which is also the tool for
instruction-based fixes ("make the sky less orange", "remove the car").

**T2V** (`11_moon_t2v_alpha_EDITABLE_UI_FORMAT.json`) is the alpha-channel pipeline — use it
for **transparent overlay elements** (a glowing spark, an animated arrow, a particle wipe)
that need to composite over product footage, not for scene b-roll.

Renders land in `~/src/comfyui/basedir/output/`. Quality settings ≈10–13 min per clip on the
4080 Super; the 4-step turbo LoRA is much faster if you're iterating on composition.

### 4.3 Hard limits — read before generating

- **Never generate a screen, dashboard, chart, or number.** Diffusion models produce
  convincing garbage UI. Any screen in any video is a real recording.
- **No people's faces.** Not worth the uncanny-valley risk or the licensing ambiguity.
- **No recognisable brands** — no inverter logos, no retailer signage, no number plates.
- Keep b-roll **under 20% of total runtime** in tutorials and **under 40%** in ads. It's
  seasoning; the product is the meal.

### 4.4 Consistency

Pick one look and hold it across every clip: **warm morning light, Australian suburban,
slightly desaturated, shallow depth of field, no people.** Put that phrase in every prompt.
Generate all b-roll in one session so the model state and seed neighbourhood match.

---

## 5. Motion graphics (titles, callouts, end cards)

Don't use ComfyUI for these, and don't hand-animate them. Build them as **HTML/CSS pages and
screen-record them**, reusing `docs/style.css` so the type and colour match the website
exactly. It's faster than After Effects, it's version-controlled, and it can't drift from the
brand.

- **Title cards**: full-bleed, one line, 2 s.
- **Callouts**: a semi-transparent rounded box + arrow, positioned over a paused product
  frame; record the CSS transition.
- **End card** (same on every video): logo, `gridlens.au`, "Free for Home Assistant", and
  the subscribe/next-video link.

---

## 6. Production order

Do it in this order — each step de-risks the next.

1. **Capture §1.2.** Everything else is blocked on this.
2. **Cut T7 (Greedy) first.** It's the newest feature, the most visually distinctive, and the
   best test of whether the footage is good enough. If T7 works, the rest will.
3. **Then T1 (pillar) and T2 (setup).** These are what a new visitor watches.
4. **Then A1/A2** — cut from T1 and T7's best frames. Ads are an edit of tutorial footage,
   not a separate shoot.
5. **T3–T6, T8** as capacity allows.
6. **B-roll last.** You'll know exactly which 3-second gaps need filling.

**Publishing**: embed T1 on `docs/index.html`, T2 on `docs/docs.html` under Install, T5–T8
alongside their feature sections. Every video description links to `gridlens.au` and the
GitHub repo.

---

## 7. Open decisions

- **Voice**: real recorded VO vs. TTS. Real is better and this is a trust-led product; a
  synthetic voice on a "we're honest about your data" ad is a bad look. Recommend recording
  it yourself — an Australian accent is an asset for an Australian product.
- **Music**: needs a license. Epidemic Sound / Artlist. Budget this before cutting A2.
- **Channel**: create the YouTube channel and reserve the handle before publishing anything.
- **Spend**: don't push paid until T1 and T2 are live — pre-roll sends traffic to a site that
  should already have something to watch.
