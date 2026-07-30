# Animated Icons for Power Flow Card Nodes — Design Doc

Status: **implemented and deployed 2026-07-30** for the `solar` node — `icons_active` /
`icons_idle_active` mechanism built as designed below, generalized slightly to support an
animated *idle* variant too (not just active), not just the "static idle" v1 assumption this
doc originally scoped to (see §"What this doc does not decide", item 1 — resolved: idle got
its own animation, not left static). Sun (active) / moon (idle) assets generated via
ComfyUI — see `ANIMATED_ICON_GENERATION.md` for that half of the process (rendering,
chroma-key/alpha handling, finalizing into an APNG, deploying). The hot-water motivating
case below is still unbuilt; the mechanism is generic and ready for it whenever an asset
exists.

Originally written 2026-07-29, grounded in the current `grid-lens-powerflow-card.js` via
direct file/line reads. Motivating case: an animated hot-water icon (steam rising from a
kettle) that plays while the hot-water deferrable load is drawing power, and sits
static/idle otherwise. Read `GRIDLENS_CHECKLIST.md` first for overall project state — this
doc is scoped to one card, not a project-wide feature.

## Current behavior (why this doesn't already work)

Every node icon (`solar`, `battery`, `home`, `grid`, `ev`, `water_heater`, plus any
`deferrable_icons` override) is a static raster file, loaded once and chroma-keyed through
`stripWhiteBg()` (`grid-lens-powerflow-card.js:60-90`):

```js
img.onload = () => {
  const canvas = document.createElement('canvas');
  ...
  ctx.drawImage(img, 0, 0);          // <- draws whatever frame is current at THIS instant
  const imgData = ctx.getImageData(...);
  ...
  resolve(canvas.toDataURL('image/png'));   // <- flattened, single-frame, forever
};
```

The result is cached per source URL in `this._imgData[key]` (`_kickOffImageLoads`,
`:237-247`) and that one static data URL is what `_pnode()` embeds via `<image href="...">`
(`:444-450`) for the rest of the card's life. Even if the source file were an animated
APNG/GIF, `drawImage` only ever captures the single frame the browser happened to be
displaying at `onload` — canvas has no concept of "keep animating this." So today's pipeline
structurally cannot pass animation through, independent of source format.

## Design

### 1. Format: APNG, not GIF or "animated JPEG"

- **Animated JPEG isn't a real raster format** — Motion JPEG (MJPEG) is a video container
  convention, not something `<image href>` or `<img>` decodes frame-by-frame. Rule it out.
- **APNG** over GIF: proper 8-bit alpha channel (matches what `stripWhiteBg` currently fakes
  via chroma-keying), no 256-colour palette banding on a steam/smoke gradient, and is decoded
  natively by every browser HA's frontend targets (Chrome/Edge since 2020, Firefox and Safari
  for years prior) inside both `<img>` and SVG `<image>`.
- Generate the animated asset **with the background already transparent** — this sidesteps
  `stripWhiteBg` entirely rather than trying to make canvas-based chroma-keying animation-aware
  (a materially bigger, riskier change to a function every existing static icon also depends on).

### 2. Bypass `stripWhiteBg` for animated assets

Static icons keep the exact current path (chroma-key at load time, cache the flattened data
URL). Animated icons need a **separate, parallel path** that skips canvas processing entirely
and hands the raw APNG URL straight to `<image href>`:

```js
// _kickOffImageLoads(), alongside the existing static-icon loop:
for (const [key, src] of Object.entries(this._config.icons_active || {})) {
  if (!src || this._imgActive[key] !== undefined) continue;
  this._imgActive[key] = src;   // no stripWhiteBg — used as-is, pre-transparent
}
```

No preload promise/caching dance needed here — unlike `stripWhiteBg`, there's no per-pixel scan
to amortize, so the raw URL can be read directly at render time.

### 3. Active/idle swap — reuse the `active` flag already on every actor

Every actor descriptor already carries a live `active`/`on` boolean, computed from real device
state — this is not new plumbing:

- Battery: `active: conn.battery.active` (`:639`)
- EV: `active: conn.ev.active` (`:646`)
- Deferrable loads (hot water, etc.): `active: on && kw > MIN_KW` (`:673`), where `on` is read
  from the load's control switch if it has one, else from its live power crossing `MIN_KW`
  (`:657-662`)

`_pnode()` (`:436-483`) just needs to prefer the active-variant data URL when `a.active` is
true and one exists, falling back to today's static image otherwise:

```js
const dataUrl = a.imgKey
  ? (a.active && this._imgActive[a.imgKey]) || this._imgData[a.imgKey]
  : null;
```

Swap is instant on the next render (same as `dim`/opacity already is) — no fade/transition
needed for v1. Nodes without a configured active variant are byte-identical to today's
behavior; this is purely additive.

### 4. Config surface

```yaml
type: custom:grid-lens-powerflow-card
icons:
  water_heater: /grid_lens/icons/grid-lens-water-heater.jpg   # unchanged, static/idle frame
icons_active:
  water_heater: /grid_lens/icons/grid-lens-water-heater-active.png   # new, animated APNG
```

- `icons_active` is new, optional, keyed identically to `icons` — no source is required to have
  an animated counterpart.
- `deferrable_icons` (per-load name → icon override, `:34`) currently accepts a bare key or URL
  string. To let a specific *load* (not just a built-in device type) carry its own animated
  variant, extend accepted values to also take `{icon, icon_active}` — bare-string values keep
  meaning exactly what they mean today. Needs a small parse-time branch in wherever
  `deferrable_icons` values are consumed (`:161-164`, `_deferIcon()` near `:610-626`).

### 5. Shipped defaults

Match the motivating case: ship one built-in animated asset (`water_heater`) generated by the
user and dropped into `www/icons/`, wired into the default `icons_active` map in `setConfig()`
(`:150-160`) the same way today's six static defaults are. Everything else (`ev`, `battery`,
etc.) stays static unless/until an animated asset exists for it — the mechanism is generic, so
adding another later is a one-line default plus an asset file, not a code change.

## What this doc does *not* decide (open questions)

1. **Idle-state behavior** — fully static when idle (v1 assumption above), or a second, subtler
   idle-loop animation (e.g. a barely-visible simmer) versus true single-frame stillness?
2. **Asset sourcing** — this doc assumes you generate/source the APNG externally (an AI image
   tool, or hand-built frames assembled via `ffmpeg`/`apngasm`, neither of which is currently
   installed in this container). No code here can produce the artwork itself.
3. **File size** — APNGs with alpha and multiple frames are meaningfully larger than the current
   static JPG/PNG icons (tens of KB each currently). Since `icons_active` bypasses caching-via-
   data-URL and is fetched as a plain URL, the browser's normal HTTP cache handles repeat loads,
   but worth checking rendered file size once a real asset exists.
4. **Per-load override granularity** — is a global `water_heater` variant enough, or does the
   user want different animated art per *device* (e.g. two different hot-water systems on one
   install)? The `deferrable_icons` extension in §4 covers this if needed, but adds config
   surface that may not be worth it for a single-kettle use case.

## Changes (once an asset exists)

All in `custom_components/grid_lens/www/cards/grid-lens-powerflow-card.js` — this is a pure
frontend card feature, no backend/`custom_components` Python or config_flow changes required for
the built-in `water_heater` case:

- `setConfig()` (`:149-233`): add `icons_active` default map (start with just `water_heater`),
  merge with `config.icons_active` like `icons` already does.
- `_kickOffImageLoads()` (`:237-247`): add the parallel active-icon loop from §2, storing into a
  new `this._imgActive = {}` (initialized in the constructor near `:114`).
- `_pnode()` (`:436-483`): the `dataUrl` selection change from §3.
- (Optional, only if per-load overrides are wanted) `_deferIcon()` (`~:610-626`) and the
  `deferrable_icons` normalization loop (`:161-164`): accept the `{icon, icon_active}` object
  form from §4.
- Bump `_CARD_VERSION` in `__init__.py` and run `sync-to-ha.sh` per the usual cache-busting
  workflow (see root `CLAUDE.md`).

## Verification

Pure frontend change — no LP/optimizer path involved, so no scipy/LXC round-trip needed (unlike
most other plan docs in this repo). After syncing:

1. Confirm the static case is unaffected: an install with no `icons_active` entry renders
   exactly as it does today.
2. Toggle the hot-water load's underlying switch/power sensor via HA Developer Tools → States
   (or wait for it to actually run) and confirm the icon swaps from static to animated within
   one render cycle, and back when it goes idle.
3. Check both light and dark theme — the animated asset needs to read correctly against both
   card backgrounds, same as today's chroma-keyed static icons.
4. Spot-check in both Chrome and Firefox (APNG support differs in maturity, though both have
   supported it for years at this point) since the HA app on this install may render through
   either engine depending on platform.
