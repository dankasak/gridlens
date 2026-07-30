# Animated Icon Generation — Process Doc

Companion to `ANIMATED_LOAD_ICONS_PLAN.md` (the card-side mechanism: `icons_active` /
`icons_idle_active`, APNG-with-alpha, active/idle swap). This doc covers the *other* half —
how an animated `.apng` asset actually gets produced, from a ComfyUI render to a deployed
file the card can load.

GPU server: `ssh gpu` (192.168.1.128, `tesla.duckdns.org`). ComfyUI at
`http://192.168.1.128:8188`, service `comfyui.service`, files under `~/src/comfyui/`.

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
