# Animated Icon Generation — Process Doc

Companion to `ANIMATED_LOAD_ICONS_PLAN.md` (the card-side mechanism: `icons_active` /
`icons_idle_active`, APNG-with-alpha, active/idle swap). This doc covers the *other* half —
how an animated `.apng` asset actually gets produced, from a ComfyUI render to a deployed
file the card can load.

GPU server: `ssh gpu` (192.168.1.128, `tesla.duckdns.org`). ComfyUI at
`http://192.168.1.128:8188`, service `comfyui.service`, files under `~/src/comfyui/`.
Reachable directly via HTTP/curl from the HA add-on container — no tunnel needed
(`curl http://192.168.1.128:8188/system_stats` to sanity-check reachability).

## 0. Efficiency notes — read this before starting a new icon (saves real tokens)

- **This container has no image-generation tool of its own and no PIL/pip** (musl,
  read-only lib dir — `pip3 install pillow` fails with `EACCES` on `ld-musl-x86_64.so.1`).
  Don't try either; go straight to the GPU box (`ssh gpu`) for both rendering and any
  PNG/JPG/frame-extraction work — Python+PIL is already set up there.
- **Prefer the ComfyUI HTTP API over drag-and-drop** for anything beyond a single one-off
  render: `POST /prompt` with a job JSON, poll `GET /history/{id}`, fetch bytes via
  `GET /view`. Don't hand-build a job JSON from scratch — copy the closest existing file in
  `temp/comfyui-jobs/` (see §1/§1b for which template fits which case) and edit only the
  prompt/seed/input-image fields. This is the difference between a few curl calls per asset
  and a UI screenshot-and-click loop per asset — matters a lot once you're generating a
  matrix of variants (§1c), not just one icon.
- **Reuse one seed across a family of stills that must share geometry.** If you need N
  variants of the *same object* that differ in only one described attribute (fill level,
  color, pose), keep every other prompt word and the seed identical across all N T2I jobs.
  Confirmed this produces near-identical object geometry (jar/cork/glass shape held constant
  across 5 fluid-level stills) — much cheaper than generating one, reviewing, discarding, and
  redescribing per variant.
- **Decide the full state space before generating anything.** If a node needs to represent
  more than one independent dimension (e.g. SOC level × charge/discharge direction), work out
  every combination the card actually needs to render *first* (§1c), then batch the stills and
  animations for that whole matrix in one pass. Scoping this mid-stream (starting with one
  variant, then being asked to add levels, then directions) costs an extra round trip through
  every step below per follow-up instead of once.
- **When negative/"don't do X" prompt language doesn't work in an I2V prompt, don't retry
  with a stronger negative — restructure the sentence around a known-good one instead.** Wan
  2.2 (and video diffusion generally) reads the *positive* description of what's in the
  frame, not exclusions inside it. If an existing prompt for a *different* material
  reliably produces a good effect (e.g. frost/mist breath), copy its sentence structure
  verbatim and swap only the material words, rather than describing the new target state
  from first principles. See `AIRCON_DRAGON_ICON_PLAN.md`'s "neutral breath" note for the
  concrete case (2 failed negation-based attempts vs. 1 successful structure-reuse attempt).
- **Verify a deploy without `gh` CLI.** This environment has no `gh auth` configured, so
  `gh run list` / workflow-status checks fail. For the private `gridlens-api` repo (which
  auto-deploys to the LXC via a GitHub Actions self-hosted runner on push to `main`), confirm
  a deploy landed by SSHing straight to the LXC and grepping the running container instead:
  `ssh lxc "docker exec gridlens-api-1 grep -o 'ICON_V = .*' /app/app/cards/grid-lens-powerflow-card.js"`
  (swap the grep target for whatever string changed). For the public `gridlens` repo, curl the
  HA-proxied URL directly (§4).
- **Card-JS changes and icon-file changes use two *different* cache-busting mechanisms —
  don't assume one covers the other.** Adding a brand-new icon *file* under an existing key
  needs no cache-bust at all (§4). But teaching the card *logic* about a new icon key (a new
  `imgKey`, a new bucket, a new default URL in `setConfig()`) is a card-JS change, and needs
  **both** halves bumped: `ICON_V` (inline JS constant in
  `gridlens-api/app/cards/grid-lens-powerflow-card.js`, cache-busts the icon URLs themselves)
  **and** `_CARD_VERSION` (Python constant in the public repo's
  `custom_components/grid_lens/__init__.py`, cache-busts the *card JS file's own* URL) — plus
  an HA restart to clear `PowerflowCardView`'s in-memory proxy cache (public repo). Missing
  either half means the browser (or the proxy) keeps serving stale JS after a correct deploy.

## 1. Generate in ComfyUI

Two pipelines, pick based on whether you're starting from an existing image or from scratch:

- **T2V (text-to-video), Wan 2.1 + Alpha LoRA** — generates from a text prompt only, with a
  real alpha channel baked in by the model itself (dual VAE: one decodes RGB, one decodes a
  grayscale mask, `JoinImageWithAlpha` combines them). Output: `SaveAnimatedWEBP` node, an
  animated `.webp` with genuine per-pixel transparency already correct.
  Editable workflow: `temp/comfyui-jobs/11_moon_t2v_alpha_EDITABLE_UI_FORMAT.json`.
- **I2V (image-to-video), Wan 2.2** — continues an existing still image (e.g. an existing
  static icon, or a fresh Flux.2-generated still) into motion. No native alpha — the source
  image's background (plain white, matching this project's icon convention) stays in every
  frame, needs chroma-keying afterward (§2b). Output: `SaveVideo` node, an `.mp4`.
  Editable workflow: `temp/comfyui-jobs/10_moon_i2v_EDITABLE_UI_FORMAT.json`.

Both load into ComfyUI via drag-and-drop onto the canvas, or Workflow → Open. Edit prompt
text / sampler settings / resolution directly on the nodes, hit Queue.

**Third pipeline — T2I still, Flux.2 Klein 9B (fp8).** When there's no existing image to
edit or animate-from and the icon needs a brand-new character/object designed from a text
description (e.g. a new mascot, a new prop), generate the base still with Flux.2 Klein first,
then feed it into I2V (above) for motion. Node graph: `UNETLoader` → `CLIPLoader` →
`VAELoader` → `CLIPTextEncode` → `EmptyFlux2LatentImage` → `Flux2Scheduler` → `BasicGuider` +
`RandomNoise` → `KSamplerSelect` → `SamplerCustomAdvanced` → `VAEDecode` → `SaveImage`.
Template: `temp/comfyui-jobs/18_kettle_base_flux2.json` (or `07_moon_base_flux2.json`) — copy
one of these rather than building the graph by hand. Keep the icon set's white-background
convention in the prompt text (matches what the I2V/chroma-key step downstream expects).

**Submitting jobs via the API instead of the UI** (much faster once you're doing more than
one render): copy a template JSON, edit its `inputs` fields (prompt text, seed,
`image` for I2V's `LoadImage` node), then:
```bash
# submit
curl -s -X POST http://192.168.1.128:8188/prompt \
  -H 'Content-Type: application/json' \
  -d @job.json   # {"prompt": {...node graph...}}
# → {"prompt_id": "..."}

# poll until it has an "outputs" key
curl -s http://192.168.1.128:8188/history/<prompt_id>

# download a specific output image/video once history shows it
curl -s "http://192.168.1.128:8188/view?filename=<name>&subfolder=&type=output" -o out.png
```
To animate an I2V job from a *just-generated* still (not a pre-existing file), upload it
first: `curl -s -X POST http://192.168.1.128:8188/upload/image -F "image=@still.png"` — the
returned filename is what goes in the I2V job's `LoadImage.inputs.image`.

### 1c. Generating a state matrix (e.g. level × direction) instead of a single icon

Some nodes need more than one idle/active pair — the battery icon's SOC-level ×
charge/discharge matrix and the aircon dragon's heat/cool/neutral breath states
(`AIRCON_DRAGON_ICON_PLAN.md`) are the two precedents. Both follow the same shape:

1. **Enumerate every combination the card needs to render** up front (see the card-side
   pattern below) — don't discover new combinations mid-pipeline.
2. **Generate one T2I still per *idle-visible* dimension** (e.g. one per SOC level) reusing
   the same seed across all of them (§0) so they read as "the same object" — only the
   described attribute (fill level, count, etc.) changes per prompt.
3. **Generate one I2V animation per *active* combination** (e.g. level × direction), each
   continuing from its matching still — so the active loop's start frame visually matches the
   idle frame it swaps from/in the card (no "pop" between idle and active for the same level).
4. Finalize every animation through the same chroma-key script (§2b) and deploy the full set
   together (§4) — a partial matrix (some levels animated, others not) is worse than shipping
   the static-only version, since the card would render an inconsistent pop between states.

Card-side, this is a **compound `imgKey`**, not a new rendering path — computed once per
render in whichever method builds the node's actor descriptor (e.g. `_bottomActors()`),
following the exact `icons`/`icons_active` swap `_pnode()` already does for every other node.
Concretely (battery precedent, `grid-lens-powerflow-card.js`):
```js
_batterySocBucket(pct) {
  if (isNaN(pct)) return 'mid';
  if (pct < 15) return 'empty';
  if (pct < 35) return 'low';
  if (pct < 65) return 'mid';
  if (pct < 85) return 'high';
  return 'full';
}
// in _bottomActors():
const socBucket = this._batterySocBucket(f.socPct);
const imgKey = conn.battery.active
  ? `battery_${socBucket}_${conn.battery.reverse ? 'charge' : 'discharge'}`
  : `battery_${socBucket}`;
```
`icons`/`icons_active` in `setConfig()`'s defaults then just need one URL per key in the
matrix (`battery_empty`, `battery_low`, … / `battery_empty_charge`, `battery_empty_discharge`,
…) — `_pnode()` itself needs zero changes, it already resolves whatever `imgKey`/`active` the
caller hands it.

**Fixing a base image before animating — Flux.2 Klein image edit.** For instruction-based
edits to a still ("make the colors more saturated", "remove the X") — the local equivalent
of Nano-Banana-style editing — use
`temp/comfyui-jobs/12_flux2_klein_edit_EDITABLE_UI_FORMAT.json`. It's the stock
ComfyUI "Flux.2 Klein 9B distilled image edit" template with the input image, prompt and
output prefix pre-filled; all three model files it needs are already downloaded. The
sampler/model plumbing lives inside a **subgraph** node (double-click it to open); the
LoadImage, edit prompt and SaveImage sit at the top level. 4 steps, cfg 1 — renders in
well under a minute. Put source images in `~/src/comfyui/basedir/input/` so LoadImage can
see them; a second (bypassed, purple) branch in the same workflow shows a two-reference
edit — right-click → "Set Group Nodes to never/always" or Ctrl+B to un-bypass it.

**Avoid anything prefixed `api_` in the Templates gallery or Node Library** — those are
comfy.org's paid hosted-cloud nodes, not local generation, and will error with a credits
message. This install has `--disable-api-nodes` set (`COMFY_CMDLINE_EXTRA` in
`~/src/comfyui/docker-compose.yml`) specifically so these fail loudly instead of silently
trying to bill anything — if you ever need one, that's the flag to remove.

Render lands in `~/src/comfyui/basedir/output/` on the GPU box, named by the `SaveVideo`/
`SaveAnimatedWEBP` node's `filename_prefix` widget. Quality settings (20 steps, no speed
LoRA) take ~10-13 minutes per render on the RTX 4080 Super; the 4-step turbo LoRA path is
much faster but was intentionally avoided for these icons (quality over speed, per this
project's choice).

## 2. Finalize into an APNG

Two scripts on the GPU box, `~/src/comfyui/scripts/`, both do the same four things —
extract frames → downscale → thin frame rate → crossfade-blend the tail into frame 0 for a
seamless loop → save as APNG — differing only in how they handle transparency:

### 2a. `finalize_icon_alpha.py` — for T2V-alpha `.webp` output (already transparent)

```bash
python3 ~/src/comfyui/scripts/finalize_icon_alpha.py \
  ~/src/comfyui/basedir/output/<name>_00001_.webp \
  /tmp/<output-name>.apng
```

Just re-encodes the model's own alpha channel — no color/background processing.

### 2b. `finalize_icon_i2v_chromakey.py` — for I2V `.mp4` output (needs background removal)

```bash
python3 ~/src/comfyui/scripts/finalize_icon_i2v_chromakey.py \
  ~/src/comfyui/basedir/output/<name>_00001_.mp4 \
  /tmp/<output-name>.apng
```

Uses a **flood-fill from the image border** (not a flat "near-white" threshold) to find the
background — important because a naive per-pixel white-distance threshold either leaves a
translucent haze (if the model rendered a soft glow/gradient background instead of flat
white) or, if tuned more aggressively, eats through the object's own bright highlights. The
flood-fill only removes pixels *connected* to the border through a smoothly-varying color
path, anchored to each border seed's own color (`tol`/`anchor_max` in the script) so a long
chain of small gradient steps can't drift all the way into a differently-colored object.

**`deadzone` param (added 2026-08-05, default `0.06`).** I2V renders drift slightly in
brightness frame-to-frame even in a nominally-static background region — this produces tiny
nonzero per-frame diffs against the reference frame that yield a few percent of *oscillating*
residual alpha: invisible composited on a light theme, visible as a faint flickering
box/halo around the icon on a dark theme. `frac` values below `deadzone` now snap to fully
transparent (0) instead of leaving that low-level flicker in. This is a general fix — it
benefits every future i2v-chromakeyed icon, not just the one that surfaced it — so there's no
need to re-derive or re-diagnose this if a new icon shows the same faint-halo symptom; check
this script's `deadzone` value is still in place before assuming it's a new bug. If a new
render shows a *stronger* halo than this default clears, raise `deadzone` rather than
re-tuning `tol`/`anchor_max` (those control the border flood-fill's *reach*, not per-frame
noise).

Both scripts default to 160×160 output (the powerflow card's node icons render at ~52-60px
diameter by default — 160px gives headroom for `icon_scale`/retina without shipping a
needlessly huge file) and thin to every 2nd source frame (~41 frames from an 81-frame/16fps
source). Edit the `target`/`thin`/`loop_blend` args in the script if a specific render needs
different tuning (e.g. a busier animation may need a bigger `loop_blend` window to hide the
seam, or a less padded `anchor_max` if chroma-keying eats into the subject).

## 3. Review before deploying

Pull a frame or two down to actually look at it — cheap sanity check before committing to a
2-minute-plus round trip:

```bash
# on the GPU box
python3 -c "
from PIL import Image
im = Image.open('/tmp/<output-name>.apng')
im.seek(20); im.save('/tmp/preview.png')
"
```
```bash
# from here
scp -i /homeassistant/tariff_compare/.ssh/id_ed25519_gpu \
  dankasak@192.168.1.128:/tmp/preview.png /tmp/preview.png
```
Then `Read` the file to view it. Also worth checking alpha is *actually* transparent rather
than trusting how a viewer renders it — corners should read alpha≈0:
```python
im = Image.open(path); im.seek(20); im = im.convert("RGBA")
print(im.getchannel("A").getextrema(), im.getpixel((0, 0)))
```

## 4. Deploy

Filenames the card expects (see `icons_active`/`icons_idle_active` defaults in
`grid-lens-powerflow-card.js`'s `setConfig()`): `grid-lens-solar-active.apng` (shown while
generating) and `grid-lens-solar-idle.apng` (shown otherwise).

```bash
scp -i /homeassistant/tariff_compare/.ssh/id_ed25519_gpu \
  dankasak@192.168.1.128:/tmp/<output-name>.apng \
  /homeassistant/tariff_compare/gridlens/custom_components/grid_lens/www/icons/grid-lens-solar-<active-or-idle>.apng
```

Copy to the **repo** path above first (source of truth), then to the **live** HA path so it
actually takes effect:

```bash
cp .../gridlens/custom_components/grid_lens/www/icons/grid-lens-solar-*.apng \
   /homeassistant/custom_components/grid_lens/www/icons/
```

**No HA restart or `_CARD_VERSION` bump needed for icon-only changes** — icon URLs aren't
cache-busted with a `?v=` query string the way card JS is, so a plain file copy takes effect
immediately server-side. The browser may still have the old file cached client-side though —
**hard-refresh**, not just reload.

**If the change also teaches the card about a new icon *key*** (new `imgKey`, new bucket
function, new default URL — i.e. anything in §1c's state-matrix pattern), that's a
`grid-lens-powerflow-card.js` edit in the **private** `gridlens-api` repo, on top of the icon
files themselves, and needs the full two-stage cache-bust + verification (see §0's bullet on
this, and root `CLAUDE.md`'s Lovelace cache-busting section): bump `ICON_V` in the card JS,
commit + push `gridlens-api` (auto-deploys to the LXC via the self-hosted Actions runner —
confirm with the `ssh lxc "docker exec ..."` check in §0, not `gh run list`), bump
`_CARD_VERSION` in the public repo's `__init__.py`, then `sync-to-ha.sh` (copies + restarts
HA). Verify end-to-end with:
```bash
curl -s -m 10 "http://homeassistant:8123/api/grid_lens/cards/grid-lens-powerflow-card.js?v=<new _CARD_VERSION>" \
  | grep -o 'battery_mid_charge'   # or whatever new key should be present
curl -s -o /dev/null -w '%{http_code}\n' "http://homeassistant:8123/grid_lens/icons/<new-asset>.apng"
```
Icon-*file*-only changes (same key, new art) skip all of this — just the plain copy above.

## 5. Previewing the idle-state animation without waiting for dark

The idle (moon) icon only shows when `conn.solar.active` is false (no real solar
generation). To preview it while solar is actively generating, temporarily copy the idle
file's content over the active file's path, hard-refresh, review, then copy back:

```bash
cp .../grid-lens-solar-active.apng /tmp/sun-backup.apng   # keep the real one safe
cp .../grid-lens-solar-idle.apng .../grid-lens-solar-active.apng   # moon now shows in the active slot
# ...review, hard-refresh...
cp /tmp/sun-backup.apng .../grid-lens-solar-active.apng   # restore
```
