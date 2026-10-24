/*
 * grid-lens-flex-row-card: aspect-driven ("justified") row sizing.
 *
 *   node custom_components/grid_lens/tests/test_flex_row_sizing.mjs
 *
 * Drives the real card class against a DOM stub, reads the flex shorthand it puts on each
 * wrapper, then resolves that line with a miniature implementation of the CSS flexbox
 * distribution algorithm (CSS Flexbox §9.7) to get the widths a browser would.
 *
 * Why resolve the line at all rather than just assert the flex strings: the whole claim
 * being made is that a basis proportional to aspect yields widths proportional to aspect
 * EVEN WHEN THE LINE OVERFLOWS — which is only true because flex-shrink is weighted by
 * basis. That is the load-bearing step and it lives in the browser, not in our code, so a
 * test that only checked the strings would be asserting the easy half. The resolver below
 * is ~20 lines and makes the actual user-visible outcome (equal diagram heights, no dead
 * space) checkable.
 */

// ---------------------------------------------------------------- DOM stub
class StubEl {
  constructor(tag) {
    this.tagName = tag;
    // style.setProperty is how custom properties are set; keep them inspectable.
    this.style = { _props: {}, setProperty(k, v) { this._props[k] = v; } };
    this.children = [];
    this.className = '';
    this._listeners = {};
    this.hidden = false;
    this.textContent = '';
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  removeEventListener() {}
  querySelector() { return null; }
  // Bubbling + composed are what the real event relies on; the stub only needs to walk up.
  // composedPath() is the full route including every shadow root crossed — which is how the
  // row identifies the wrapper an announcement came through, since event.target has been
  // retargeted to the outermost host by the time it arrives.
  dispatch(type, detail) {
    const path = [];
    for (let n = this; n; n = n.parentNode) path.push(n);
    const ev = {
      type, detail, target: this,
      composedPath: () => path.slice(),
      stopPropagation() { ev._stop = true; },
    };
    for (const node of path) {
      if (ev._stop) break;
      for (const fn of (node._listeners[type] || [])) fn.call(node, ev);
    }
  }
  set innerHTML(_v) { this.children = []; }
  get innerHTML() { return ''; }
}

globalThis.HTMLElement = class {
  constructor() { this._shadow = null; }
  attachShadow() { this._shadow = new StubEl('#shadow'); return this._shadow; }
  get shadowRoot() { return this._shadow; }
};
globalThis.document = { createElement: (t) => new StubEl(t) };
let CardClass = null;
globalThis.customElements = { define(_name, cls) { CardClass = cls; } };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
// A child that announces its aspect the instant it is given hass — synchronously, exactly
// as the powerflow card does, and exactly once (it dedupes afterwards). Any row that is not
// listening at that moment never hears from it again.
let ANNOUNCE_ON_HASS = null; // { 'card type': aspect }
globalThis.window = {
  loadCardHelpers: async () => ({ createCardElement: (cfg) => {
    const el = new StubEl(cfg.type);
    el.config = cfg;
    const inner = cfg.card ? cfg.card.type : cfg.type;
    Object.defineProperty(el, 'hass', {
      set(v) {
        this._hass = v;
        const a = ANNOUNCE_ON_HASS && ANNOUNCE_ON_HASS[inner];
        if (a && !this._announced) {
          this._announced = true;
          this.dispatch('grid-lens-aspect', { aspect: a, maxHeight: null });
        }
      },
      get() { return this._hass; },
    });
    return el;
  } }),
};

// The module only exposes itself by registering with customElements, so the stub registry
// above captures the class as it defines itself.
await import(new URL('../www/cards/grid-lens-flex-row-card.js', import.meta.url).href);
if (!CardClass) throw new Error('grid-lens-flex-row-card did not register a class');

// ---------------------------------------------------------------- flex resolver
// One flex line. items: [{ basis, grow, shrink }]. Returns resolved widths.
function resolveLine(items, container, gap) {
  const avail = container - gap * Math.max(0, items.length - 1);
  const total = items.reduce((s, it) => s + it.basis, 0);
  const widths = items.map((it) => it.basis);
  if (total <= avail) {
    const growSum = items.reduce((s, it) => s + it.grow, 0);
    if (growSum > 0) {
      const extra = avail - total;
      items.forEach((it, i) => { widths[i] += extra * (it.grow / growSum); });
    }
  } else {
    // Shrink is scaled by the item's own basis — this is the clause the whole design rests on.
    const scaled = items.map((it) => it.shrink * it.basis);
    const scaledSum = scaled.reduce((a, b) => a + b, 0);
    const deficit = total - avail;
    items.forEach((_it, i) => { widths[i] -= deficit * (scaled[i] / scaledSum); });
  }
  return widths;
}

function parseFlex(s) {
  const [grow, shrink, basis] = s.trim().split(/\s+/);
  return {
    grow: Number(grow),
    shrink: Number(shrink),
    basisRaw: basis,
    basis: basis.endsWith('%') ? null : Number(String(basis).replace('px', '')),
  };
}

// ---------------------------------------------------------------- harness
const SCENE_ASPECT = 1360 / 752;          // the shipped v9 cabin background
const CLASSIC_ASPECT_5 = 502 / 469;       // measured: 5 actors, staggered fan
const CLASSIC_ASPECT_9 = 690 / 505;       // measured: 9 actors, wider box

async function buildRow({ rowCap = 540, sceneMaxH = null, classicMaxH = null } = {}) {
  const card = new CardClass();
  const cfg = {
    gap: 16,
    ...(rowCap != null ? { max_row_height: rowCap } : {}),
    cards: [
      { type: 'conditional', card: { type: 'scene' }, flex: '0 1 900px' },
      { type: 'conditional', card: { type: 'classic' }, flex: '0 1 550px' },
      { type: 'chart', flex: '1 1 300px', own_line_when_siblings: 2 },
    ],
  };
  card.setConfig(cfg);
  await new Promise((r) => setTimeout(r, 0)); // _buildChildren is async
  const [scene, classic, chart] = card._items;
  const report = (rec, aspect, maxHeight) =>
    rec.item.dispatch('grid-lens-aspect', { aspect, maxHeight });
  report(scene, SCENE_ASPECT, sceneMaxH);
  report(classic, CLASSIC_ASPECT_5, classicMaxH);
  return { card, scene, classic, chart, report };
}

// Lay the row out the way the card's DOM does: non-own-line items share a nested .group,
// the own-line item sits on .wrap. Returns { widths: Map(name->px), ownLine: bool }.
function layout(card, rowWidth, gap = 16) {
  const visible = card._items.filter((x) => x.el.hidden !== true);
  const grouped = visible.filter((x) => x.ownLine == null);
  const ownLineItems = visible.filter((x) => x.ownLine != null);
  const siblings = grouped.length;
  const out = new Map();

  const groupKids = grouped.map((x) => parseFlex(x.item.style.flex));
  // The group's flex is set live by _syncVisibility: an explicit px basis once every
  // visible child is shrink-only, `1 1 auto` otherwise. An `auto` basis would be
  // content-derived in a real browser (the bug that shrank the row to 150px), so modelling
  // it as the sum of the children's bases is only valid for the non-auto case — assert it.
  const groupFlex = parseFlex(card._groupEl.style.flex || '1 1 auto');
  const groupBasis = groupFlex.basis != null
    ? groupFlex.basis
    : groupKids.reduce((s, k) => s + k.basis, 0) + gap * Math.max(0, groupKids.length - 1);

  const onFirstLine = ownLineItems.filter((x) => !(siblings >= x.ownLine));
  const lineItems = [
    ...(grouped.length
      ? [{ basis: groupBasis, grow: groupFlex.grow, shrink: 1, name: '#group' }] : []),
    ...onFirstLine.map((x) => ({ ...parseFlex(x.item.style.flex), name: x.el.config.type })),
  ];
  const lineW = resolveLine(lineItems, rowWidth, gap);
  lineItems.forEach((it, i) => { if (it.name !== '#group') out.set(it.name, lineW[i]); });

  if (grouped.length) {
    const groupW = lineW[lineItems.findIndex((it) => it.name === '#group')];
    const kidW = resolveLine(groupKids, groupW, gap);
    grouped.forEach((x, i) => out.set(x.el.config.card
      ? x.el.config.card.type : x.el.config.type, kidW[i]));
  }
  for (const x of ownLineItems) {
    if (siblings >= x.ownLine) out.set(x.el.config.type, rowWidth); // full-width line below
  }
  return out;
}

// ---------------------------------------------------------------- assertions
let pass = 0; const fails = [];
function ok(label, cond, extra = '') {
  if (cond) { pass += 1; console.log(`  ok   ${label}${extra ? ' — ' + extra : ''}`); }
  else { fails.push(label); console.log(`  FAIL ${label}${extra ? ' — ' + extra : ''}`); }
}
const near = (a, b, tol) => Math.abs(a - b) <= tol;

const ROW = 1571; // the viewport the 2026-10-23 screenshot was measured at
const GAP = 16;

console.log('\n1. both diagrams on — equal heights, no dead space');
{
  const { card } = await buildRow();
  const w = layout(card, ROW, GAP);
  const sceneH = w.get('scene') / SCENE_ASPECT;
  const classicH = w.get('classic') / CLASSIC_ASPECT_5;
  ok('diagram heights equal', near(sceneH, classicH, 1),
     `scene ${w.get('scene').toFixed(0)}x${sceneH.toFixed(0)}, classic ${w.get('classic').toFixed(0)}x${classicH.toFixed(0)}`);
  ok('row fully used', near(w.get('scene') + w.get('classic') + GAP, ROW, 1),
     `used ${(w.get('scene') + w.get('classic') + GAP).toFixed(0)} of ${ROW}`);
  ok('neither diagram exceeds the cap', sceneH <= 541 && classicH <= 541);
  ok('chart dropped to its own full-width line', w.get('chart') === ROW);
  // The old fixed-basis seed: 900 + 550 + 16 = 1466 of 1571 -> 105px dead, heights 498/514.
  ok('beats the fixed-basis layout it replaces',
     w.get('scene') + w.get('classic') + GAP > 900 + 550 + GAP);
}

console.log('\n2. scene only');
{
  const { card, classic } = await buildRow();
  classic.el.hidden = true;
  card._syncVisibility();
  const w = layout(card, ROW, GAP);
  ok('scene capped at aspect x cap', near(w.get('scene'), SCENE_ASPECT * 540, 1),
     `${w.get('scene').toFixed(0)}px`);
  ok('chart shares the line and absorbs the remainder',
     near(w.get('scene') + w.get('chart') + GAP, ROW, 1),
     `chart ${w.get('chart').toFixed(0)}px`);
  ok('scene height still at the cap', near(w.get('scene') / SCENE_ASPECT, 540, 1));
}

console.log('\n3. classic only');
{
  const { card, scene } = await buildRow();
  scene.el.hidden = true;
  card._syncVisibility();
  const w = layout(card, ROW, GAP);
  ok('classic capped at aspect x cap', near(w.get('classic'), CLASSIC_ASPECT_5 * 540, 1),
     `${w.get('classic').toFixed(0)}px`);
  ok('no dead space', near(w.get('classic') + w.get('chart') + GAP, ROW, 1),
     `classic ${w.get('classic').toFixed(0)} + chart ${w.get('chart').toFixed(0)}`);
}

console.log('\n3b. a narrow row: the survivor reclaims the hidden sibling\'s space');
{
  // 1200px — narrow enough that both-on overflows and the cap is no longer what binds, so
  // this is where "can a survivor grow back into a hidden sibling's space" is actually
  // testable. It is the exact failure of the fixed-basis seed: flex-grow 0 on a fixed px
  // basis meant the answer was no, at every width.
  const { card, classic } = await buildRow();
  const bothOn = layout(card, 1200, GAP).get('scene');
  classic.el.hidden = true;
  card._syncVisibility();
  const only = layout(card, 1200, GAP);
  ok('scene is wider once classic hides', only.get('scene') > bothOn,
     `${bothOn.toFixed(0)} -> ${only.get('scene').toFixed(0)}`);
  ok('and both diagrams shrank together while both were on',
     bothOn < SCENE_ASPECT * 540);
}

console.log('\n4. neither on — row collapses to the chart alone');
{
  const { card, scene, classic } = await buildRow();
  scene.el.hidden = true; classic.el.hidden = true;
  card._syncVisibility();
  const w = layout(card, ROW, GAP);
  ok('chart takes the whole row', near(w.get('chart'), ROW, 1), `${w.get('chart').toFixed(0)}px`);
  ok('hidden items display:none', scene.item.style.display === 'none'
     && classic.item.style.display === 'none');
}

console.log('\n5. a classic aspect change re-justifies the row');
{
  const { card, classic, report } = await buildRow();
  const before = layout(card, ROW, GAP);
  report(classic, CLASSIC_ASPECT_9, null); // two more deferrable loads -> wider fan
  const after = layout(card, ROW, GAP);
  ok('classic grew, scene gave way',
     after.get('classic') > before.get('classic') && after.get('scene') < before.get('scene'),
     `classic ${before.get('classic').toFixed(0)} -> ${after.get('classic').toFixed(0)}`);
  const h = [after.get('scene') / SCENE_ASPECT, after.get('classic') / CLASSIC_ASPECT_9];
  ok('heights still equal after the change', near(h[0], h[1], 1),
     `${h[0].toFixed(0)} vs ${h[1].toFixed(0)}`);
  ok('row still fully used',
     near(after.get('scene') + after.get('classic') + GAP, ROW, 1));
}

console.log('\n6. a wide row clamps at the cap instead of inflating');
{
  const { card } = await buildRow();
  const w = layout(card, 3000, GAP);
  const sceneH = w.get('scene') / SCENE_ASPECT;
  const classicH = w.get('classic') / CLASSIC_ASPECT_5;
  ok('scene height stops at the cap', near(sceneH, 540, 1), `${sceneH.toFixed(0)}px`);
  ok('classic height stops at the cap', near(classicH, 540, 1), `${classicH.toFixed(0)}px`);
  ok('shrink-only, so the row does NOT stretch them',
     w.get('scene') + w.get('classic') + GAP < 3000);
}

console.log('\n7. cap resolution: row cap, own max_height, and both');
{
  const a = await buildRow({ rowCap: null, sceneMaxH: 498, classicMaxH: 780 });
  const wa = layout(a.card, ROW, GAP);
  ok('no row cap: each child\'s own max_height is the cap',
     parseFlex(a.scene.item.style.flex).basis === Math.round(SCENE_ASPECT * 498)
     && parseFlex(a.classic.item.style.flex).basis === Math.round(CLASSIC_ASPECT_5 * 780),
     `scene basis ${parseFlex(a.scene.item.style.flex).basis}px`);
  ok('...which is exactly the un-justified case, so heights differ',
     !near(wa.get('scene') / SCENE_ASPECT, wa.get('classic') / CLASSIC_ASPECT_5, 1));

  const b = await buildRow({ rowCap: 540, classicMaxH: 400 });
  ok('both present: the smaller wins (no dead space inside the item)',
     parseFlex(b.classic.item.style.flex).basis === Math.round(CLASSIC_ASPECT_5 * 400),
     `${parseFlex(b.classic.item.style.flex).basis}px`);

  const c = await buildRow({ rowCap: null });
  ok('neither present: falls back to the module default',
     parseFlex(c.scene.item.style.flex).basis === Math.round(SCENE_ASPECT * 560),
     `${parseFlex(c.scene.item.style.flex).basis}px`);
}

console.log('\n8. a child that never reports an aspect keeps its configured flex');
{
  const { card, chart, classic } = await buildRow();
  ok('no aspect recorded for it', chart.aspectFlex === null);
  ok('own-line rule still governs it while both diagrams show',
     chart.item.style.flex === '1 1 100%', chart.item.style.flex);
  classic.el.hidden = true;
  card._syncVisibility();
  ok('back on the shared line it gets its configured flex, not an aspect basis',
     chart.item.style.flex === '1 1 300px', chart.item.style.flex);
}

console.log('\n9. the .group never gets a content-derived basis (the "very very small" bug)');
{
  // An `auto` basis on a flex container of shrink-only children resolves to its
  // max-CONTENT, computed from the children's intrinsic sizes rather than the flex bases
  // we set — for an ha-card wrapping an SVG that is the CSS default 300x150 replaced box,
  // which collapsed the whole row to ~150px tall. Shipped and caught on screen
  // 2026-10-23; this is the regression guard.
  const { card, scene, classic } = await buildRow();
  const states = [
    ['both visible', () => {}],
    ['scene only', () => { classic.el.hidden = true; }],
    ['classic only', () => { classic.el.hidden = false; scene.el.hidden = true; }],
  ];
  for (const [label, mutate] of states) {
    mutate();
    card._syncVisibility();
    const f = parseFlex(card._groupEl.style.flex);
    ok(`${label}: group basis is an explicit px sum, not auto`,
       f.basis !== null && f.basis > 200, card._groupEl.style.flex);
  }
  scene.el.hidden = false; classic.el.hidden = false;
  card._syncVisibility();
  const f = parseFlex(card._groupEl.style.flex);
  const kids = [scene, classic].map((x) => parseFlex(x.item.style.flex).basis);
  ok('and it is exactly the children\'s bases plus the gap',
     f.basis === Math.round(kids[0] + kids[1] + 16), `${f.basis}px`);
}

console.log('\n10. the cap is handed back to the child as --gl-diagram-max-h');
{
  // Without this the card's OWN default height cap (420px) would letterbox it inside the
  // width the row just gave it. The row is the one that knows the cap it used.
  const { scene, classic } = await buildRow({ rowCap: 540 });
  ok('scene is told the row cap',
     scene.item.style._props['--gl-diagram-max-h'] === '540px',
     scene.item.style._props['--gl-diagram-max-h']);
  ok('classic too', classic.item.style._props['--gl-diagram-max-h'] === '540px');

  const b = await buildRow({ rowCap: 540, classicMaxH: 400 });
  ok('an explicit per-card max_height still wins, and the card is told that number',
     b.classic.item.style._props['--gl-diagram-max-h'] === '400px',
     b.classic.item.style._props['--gl-diagram-max-h']);

  const c = await buildRow({ rowCap: null });
  ok('the module default is handed over too, so the card never guesses',
     c.scene.item.style._props['--gl-diagram-max-h'] === '560px');
}

console.log('\n10b. the cap is published even to a child that never announces anything');
{
  // The degradation path. If an announcement is missed for any reason, the row still can't
  // allocate width by aspect — but the child must not be left falling back to its OWN
  // height cap, which is smaller than the width the row gave it and shows up as dead space
  // beside the drawing. Published at build time, before any child can render.
  const card = new CardClass();
  card.setConfig({
    gap: 16,
    max_row_height: 540,
    cards: [
      { type: 'conditional', card: { type: 'scene' }, flex: '0 1 900px' },
      { type: 'chart', flex: '1 1 300px' },
    ],
  });
  await new Promise((r) => setTimeout(r, 0));
  for (const rec of card._items) {
    ok(`${rec.el.config.type}: cap published without any announcement`,
       rec.item.style._props['--gl-diagram-max-h'] === '540px',
       rec.item.style._props['--gl-diagram-max-h']);
  }
  ok('and the flex is still the configured fallback',
     card._items[0].item.style.flex === '0 1 900px' && card._items[0].aspectFlex === null);

  const noCap = new CardClass();
  noCap.setConfig({ gap: 16, cards: [{ type: 'chart' }] });
  await new Promise((r) => setTimeout(r, 0));
  ok('no max_row_height configured -> nothing published, child keeps its own answer',
     noCap._items[0].item.style._props['--gl-diagram-max-h'] === undefined);
}

console.log('\n11. a child that announces on `set hass` is still heard');
{
  // The regression that shipped: the row set `el.hass` on a DETACHED element, before
  // appending it and before attaching the aspect listener. A card that renders
  // synchronously on `set hass` announced into nothing, and because it dedupes, a constant
  // aspect (the scene layout's 1360/752) was then never announced again — the row silently
  // fell back to the configured flex for the life of the page. Whether it broke depended on
  // whether the row already had hass by the time this async build ran, which is why it
  // worked in one screenshot and not the next.
  ANNOUNCE_ON_HASS = { scene: SCENE_ASPECT, classic: CLASSIC_ASPECT_5 };
  try {
    for (const hassFirst of [true, false]) {
      const card = new CardClass();
      // hassFirst: hass arrives BEFORE the async _buildChildren resolves (the broken order).
      if (hassFirst) card.hass = { states: {} };
      card.setConfig({
        gap: 16,
        max_row_height: 540,
        cards: [
          { type: 'conditional', card: { type: 'scene' }, flex: '0 1 900px' },
          { type: 'conditional', card: { type: 'classic' }, flex: '0 1 550px' },
          { type: 'chart', flex: '1 1 300px', own_line_when_siblings: 2 },
        ],
      });
      await new Promise((r) => setTimeout(r, 0));
      if (!hassFirst) card.hass = { states: {} };
      const [scene, classic] = card._items;
      ok(`hass ${hassFirst ? 'before' : 'after'} build: scene's announcement was heard`,
         scene.aspectFlex === `0 1 ${Math.round(SCENE_ASPECT * 540)}px`,
         scene.item.style.flex);
      ok(`hass ${hassFirst ? 'before' : 'after'} build: classic's was too`,
         classic.aspectFlex === `0 1 ${Math.round(CLASSIC_ASPECT_5 * 540)}px`,
         classic.item.style.flex);
    }
  } finally {
    ANNOUNCE_ON_HASS = null;
  }
}

console.log('\n12. garbage and duplicate reports are ignored');
{
  const { scene, report } = await buildRow();
  const good = scene.item.style.flex;
  for (const bad of [0, -1, NaN, 99, undefined, null]) report(scene, bad, null);
  ok('out-of-range aspects rejected', scene.item.style.flex === good, good);
  const before = scene.aspectFlex;
  report(scene, SCENE_ASPECT, null);
  ok('a repeat of the same aspect is a no-op', scene.aspectFlex === before);
}

console.log(`\n${pass} checks passed, ${fails.length} failed`);
if (fails.length) { for (const f of fails) console.log(`  - ${f}`); process.exit(1); }
