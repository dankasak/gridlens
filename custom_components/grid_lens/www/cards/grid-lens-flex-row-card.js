/*
 * Grid Lens Flex Row Card
 * Lays out child cards in a row where one can grow to fill whatever space its
 * sibling doesn't use, while still stacking on a narrow/mobile viewport. Neither
 * native HA primitive does both:
 *   - horizontal-stack: fixed flex:1 per child (always equal shares, no per-card
 *     control), and its children never wrap to a new line on narrow screens.
 *   - type:grid: a rigid CSS grid with a fixed column count — never collapses on
 *     mobile regardless of viewport width (confirmed by hand in this dashboard).
 *   - flex-wrap (an earlier version of this card): line-wrapping in a wrapping flex
 *     container is decided from each item's flex-BASIS, not its post-shrink resolved
 *     size — so items wrapped well before running out of actual room, at almost any
 *     combination of basis values tried (confirmed by hand — not just a tuning miss).
 *
 * Fix: a CSS container query. `container-type: inline-size` on the host makes this
 * card's OWN rendered width queryable, and the breakpoint below switches
 * flex-direction row→column explicitly at a size *we* choose — deterministic, not
 * dependent on flex-wrap's implicit per-item basis heuristics.
 *
 * Config:
 *   type: custom:grid-lens-flex-row-card
 *   gap: 16          // px gap between children (optional, default 16)
 *   breakpoint: 700  // px — below this container width, stacks to one column (optional)
 *   max_row_height: 540
 *                    // (optional) px ceiling on the DIAGRAM height of aspect-reporting
 *                    // children (see below). Also the one knob that sizes the row: with it
 *                    // set, those children fill the line at equal heights and never exceed
 *                    // this. Unset, each falls back to its own card's max_height, then to
 *                    // DEFAULT_DIAGRAM_HEIGHT.
 *   cards:           // array of normal card configs, each with optional extra keys:
 *     - type: custom:some-card
 *       flex: "0 1 350px"   // CSS flex shorthand for this child when in row layout
 *                           // (optional, default "1 1 300px"). Ignored for a child that
 *                           // reports its own aspect — see below.
 *       own_line_when_siblings: 2
 *                           // (optional) drop to a full-width line of its own once this
 *                           // many OTHER children are visible. Counts live visibility, so
 *                           // a row of conditional cards reflows as they come and go —
 *                           // e.g. a chart that sits beside a single diagram but moves
 *                           // underneath once a second diagram is switched on.
 *
 * Children that hide themselves — a native `type: conditional` card whose condition is
 * unmet sets its own `hidden` property — collapse out of the row entirely, rather than
 * leaving an empty slot the surviving siblings can't reclaim. This needs explicit handling
 * because each child gets `display: block` set on it inline (so an `ha-card` fills its
 * flex item), and an inline `display` beats the browser's built-in `[hidden]{display:none}`
 * — so without _syncVisibility() below, a hidden conditional child renders as a blank gap
 * AND its wrapper keeps its flex basis. See _syncVisibility()/_observeVisibility().
 *
 * Aspect-driven sizing ("justified row"). A child that knows its own natural shape — today
 * grid-lens-powerflow-card, whose classic layout's aspect moves with node count and whose
 * scene layout's is its background image's — dispatches a bubbling, composed
 * `grid-lens-aspect` event carrying { aspect, maxHeight }. This card then ignores that
 * child's configured `flex` and sets `flex: 0 1 <aspect x cap>px` instead.
 *
 * Why that one line does the whole job: a flex basis proportional to aspect makes the
 * resolved WIDTH proportional to aspect, because flex-shrink is distributed in proportion
 * to basis — so when the line overflows, every item shrinks by the same factor and the
 * ratios survive. Width proportional to aspect means height EQUAL. No measuring, no
 * ResizeObserver, no height arithmetic on either side; the browser does it, and it stays
 * right as the classic diagram reshapes itself. Shrink-only (grow 0) keeps a diagram from
 * inflating past `cap` when there's slack, leaving it for a growable sibling (the power
 * chart) or, failing that, to `justify-content: center`, which splits leftover space either
 * side of the row instead of dumping it all on the right.
 *
 * This replaced a fixed px basis + `max_width` per card, hand-tuned for one combination of
 * two conditional children (2026-10-23): with flex-grow 0 and a fixed basis, a surviving
 * card could never claim the space a hidden sibling had just given back, so every other
 * combination of the Power Flow layout toggles looked wrong — measured on a 1571px viewport
 * with both on: 105px dead to the right of the row and a 101px height mismatch between the
 * two diagrams.
 *
 * `own_line_when_siblings` deliberately does NOT use flex-wrap on the main row: wrapping is
 * decided from each item's flex-BASIS rather than its post-shrink size (the reason the
 * original wrapping version of this card was abandoned — see above), so a 900px and a 700px
 * diagram would break onto separate lines long before they actually ran out of room. Instead
 * the non-own-line children live in their own nested nowrap `.group`, and only the own-line
 * child's flex-basis is switched to 100% — so the wrap point is one we choose, and the
 * grouped children still shrink to share a line the way they do today.
 */
// Fallback diagram-height cap for an aspect-reporting child when neither the row's
// `max_row_height` nor the child's own `max_height` says anything. Only an upper bound: a
// row narrower than the sum of the resulting bases still justifies itself down to fit, so
// this value is what a very wide desktop row settles at, not what a normal one renders.
const DEFAULT_DIAGRAM_HEIGHT = 560;

class GridLensFlexRowCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hass = null;
    this._children = [];
    this._items = [];       // [{ el, item }] — child element paired with its flex wrapper
    this._visObserver = null;
  }

  disconnectedCallback() {
    if (this._visObserver) { this._visObserver.disconnect(); this._visObserver = null; }
  }

  setConfig(config) {
    if (!config || !Array.isArray(config.cards) || !config.cards.length) {
      throw new Error('Define "cards" — an array of card configs to lay out in a flex row');
    }
    this._config = config;
    this._buildChildren();
  }

  async _buildChildren() {
    if (!window.loadCardHelpers) {
      this.shadowRoot.innerHTML = '<div style="padding:16px;color:var(--error-color)">grid-lens-flex-row-card: card helpers unavailable</div>';
      return;
    }
    const helpers = await window.loadCardHelpers();
    const gap = this._config.gap != null ? this._config.gap : 16;
    this._gap = gap; // _syncVisibility sums it into the .group's basis
    const breakpoint = this._config.breakpoint != null ? this._config.breakpoint : 700;

    // Only introduce the nested .group wrapper when some child actually asks for own-line
    // behaviour — every other usage keeps exactly today's single flat nowrap row.
    const hasOwnLine = this._config.cards.some(
      (c) => c && c.own_line_when_siblings != null
    );

    const style = document.createElement('style');
    style.textContent = `
      :host { display: block; container-type: inline-size; }
      /* justify-content: center matters once children are shrink-only (the aspect-driven
         case): leftover space with nothing growable to absorb it gets split either side of
         the row rather than all landing to the right of the last card. */
      .wrap { display: flex; gap: ${gap}px; align-items: flex-start; justify-content: center;
              flex-wrap: ${hasOwnLine ? 'wrap' : 'nowrap'}; }
      .group { display: flex; gap: ${gap}px; align-items: flex-start; justify-content: center;
               flex-wrap: nowrap; flex: 1 1 auto; min-width: 0; }
      .item { min-width: 0; }
      @container (max-width: ${breakpoint}px) {
        .wrap, .group { flex-direction: column; }
        .wrap > .item, .group > .item { flex: 1 1 auto !important; width: 100%; }
        .group { flex: 1 1 auto !important; width: 100%; }
      }
    `;

    const wrap = document.createElement('div');
    wrap.className = 'wrap';
    let group = null;
    if (hasOwnLine) {
      group = document.createElement('div');
      group.className = 'group';
      wrap.appendChild(group);
    }
    this._groupEl = group;

    // ONE delegated listener, attached to .wrap before a single child element exists, so
    // there is no window in which a child can announce its aspect and be heard by nobody.
    // A child's first announcement is the only one it is obliged to make — the card
    // deduplicates, so a scene layout (whose aspect is a constant 1.81) that is missed once
    // is missed for the life of the page. That is not hypothetical: the listener used to be
    // added per item AFTER `el.hass = ...` had already run on a DETACHED element, and the
    // powerflow card renders synchronously on `set hass`, so whether it worked at all came
    // down to whether this (async) build ran before or after the row's first `set hass`.
    // composedPath() carries the event's full route including the shadow roots it crossed,
    // so the wrapper it passed through identifies the child without touching event.target,
    // which has been retargeted to the `conditional` wrapper by the time it arrives.
    wrap.addEventListener('grid-lens-aspect', (ev) => {
      const path = ev.composedPath();
      const rec = this._items.find((x) => path.indexOf(x.item) !== -1);
      if (!rec) return;
      ev.stopPropagation(); // innermost row wins if these are ever nested
      this._applyAspect(rec, ev.detail);
    });

    this._items = this._config.cards.map((cardCfg) => {
      const { flex, own_line_when_siblings: ownLine, ...rest } = cardCfg;
      const el = helpers.createCardElement(rest);

      const item = document.createElement('div');
      item.className = 'item';
      const baseFlex = flex || '1 1 300px';
      item.style.flex = baseFlex;
      item.appendChild(el);
      // Publish the row's height cap IMMEDIATELY, on every item, whether or not that child
      // ever announces an aspect. A card that knows the cap sizes itself to the width it was
      // given; a card that doesn't falls back to its own default (420px for the powerflow
      // card), which is smaller than the width the row hands it and leaves dead space beside
      // the drawing. Hanging that on the handshake made a missed announcement show up as a
      // visible gap rather than as merely imperfect width allocation — which is the whole
      // difference between a layout that degrades and one that looks broken. _applyAspect
      // refines this later if the child turns out to have a tighter cap of its own.
      if (this._config.max_row_height != null) {
        item.style.setProperty('--gl-diagram-max-h',
          `${Math.round(Number(this._config.max_row_height))}px`);
      }
      // Own-line children hang off .wrap directly (so their basis can force a break);
      // everything else goes inside the nowrap .group that occupies the first line.
      (ownLine != null ? wrap : (group || wrap)).appendChild(item);
      return {
        el, item, baseFlex, aspectFlex: null,
        ownLine: ownLine != null ? Number(ownLine) : null,
      };
    });
    this._children = this._items.map((x) => x.el);

    this.shadowRoot.innerHTML = '';
    this.shadowRoot.appendChild(style);
    this.shadowRoot.appendChild(wrap);
    this._syncVisibility();
    this._observeVisibility();
    // hass LAST — after every child is in the tree, under the delegated listener above and
    // actually connected to the document. A card that renders synchronously on `set hass`
    // (the powerflow card does) then has both somewhere to announce its aspect to and a real
    // width to compute it from. This used to run per child before the child was appended.
    if (this._hass) for (const el of this._children) el.hass = this._hass;
  }

  // Turn a child's reported natural aspect into a flex basis (see the header comment).
  // `cap` is the diagram height the basis is derived from: the row's own `max_row_height`
  // if set, intersected with the child's own max_height so the two can never disagree and
  // leave dead space inside the item (the card would cap its own diagram shorter than the
  // width we gave it). Either alone is honoured; neither falls back to a default.
  _applyAspect(rec, detail) {
    const aspect = Number(detail && detail.aspect);
    if (!(aspect > 0.05) || !(aspect < 20)) return;
    const rowCap = this._config.max_row_height != null
      ? Number(this._config.max_row_height) : null;
    const ownCap = Number(detail.maxHeight) || null;
    let cap;
    if (rowCap && ownCap) cap = Math.min(rowCap, ownCap);
    else cap = rowCap || ownCap || DEFAULT_DIAGRAM_HEIGHT;
    const flex = `0 1 ${Math.round(aspect * cap)}px`;
    if (flex === rec.aspectFlex) return;
    rec.aspectFlex = flex;
    // Re-answer with the cap we actually used, which may be tighter than the row's own
    // (an explicit per-card max_height). The row-level cap was already published when the
    // item was built, so this only ever narrows it — the card is never left guessing.
    rec.item.style.setProperty('--gl-diagram-max-h', `${Math.round(cap)}px`);
    this._syncVisibility();
  }

  // Collapse (or restore) each child's flex wrapper to match whether the child has hidden
  // itself. Sets display on BOTH: the wrapper so it stops consuming flex basis, and the
  // child because its own inline `display: block` would otherwise defeat [hidden].
  // Then re-evaluates `own_line_when_siblings` against how many children are *currently*
  // visible, so the row reflows as conditional siblings appear and disappear.
  _syncVisibility() {
    for (const { el, item } of this._items) {
      const hidden = el.hidden === true;
      item.style.display = hidden ? 'none' : '';
      el.style.display = hidden ? 'none' : 'block';
    }
    // "Siblings" = visible children that aren't themselves own-line candidates, i.e. the
    // ones sharing the first line. Two own-line children don't crowd each other onto it.
    const visibleSiblings = this._items.filter(
      (x) => x.ownLine == null && x.el.hidden !== true
    ).length;
    for (const x of this._items) {
      // Basis 100% forces an own-line item past the .group on the first line and onto its
      // own; otherwise an aspect-driven basis (if the child has reported one) wins over the
      // configured flex. Re-asserted on every sync, so a child that starts reporting an
      // aspect mid-session — or changes it — is picked up without rebuilding the row.
      const share = x.aspectFlex || x.baseFlex;
      x.item.style.flex = (x.ownLine != null && visibleSiblings >= x.ownLine)
        ? '1 1 100%' : share;
    }
    // The nested .group needs an EXPLICIT basis once everything inside it is shrink-only.
    // Two wrong answers were tried first, in this order:
    //   `flex: 1 1 auto` (the original) — the group grows to fill the line, but no child
    //     is willing to expand into it, so the leftover is stranded INSIDE the group where
    //     no sibling can reach it (139px between one diagram and the chart).
    //   `flex: 0 1 auto` — worse: an `auto` basis makes the group size to its max-CONTENT,
    //     which for a flex container of shrink-only children is derived from the children's
    //     own intrinsic sizes, NOT from the flex bases we just set. The cards' intrinsic
    //     size is the CSS default replaced-element box (300x150), so the whole row
    //     collapsed to ~150px tall. Reported as "both views are very very very small".
    // Summing the bases ourselves is the only formulation that is neither content-derived
    // nor grabby: the group is exactly as wide as its children want to be, it still shrinks
    // proportionally when the line overflows, and the chart gets the real remainder.
    if (this._groupEl) {
      const kids = this._items.filter((x) => x.ownLine == null && x.el.hidden !== true);
      const fixed = kids.length > 0 && kids.every(
        (x) => x.aspectFlex && parseFloat(x.aspectFlex) === 0
      );
      if (fixed) {
        const sum = kids.reduce((s, x) => s + parseFloat(x.aspectFlex.split(/\s+/)[2]), 0)
          + this._gap * (kids.length - 1);
        this._groupEl.style.flex = `0 1 ${Math.round(sum)}px`;
      } else {
        this._groupEl.style.flex = '1 1 auto';
      }
    }
  }

  // A conditional card flips its `hidden` when IT receives hass — which can land after our
  // own `set hass` has already run (and also on re-renders we're not part of), so syncing
  // only from `set hass` would leave the row a frame stale. Watch the attribute directly.
  _observeVisibility() {
    if (this._visObserver) this._visObserver.disconnect();
    this._visObserver = new MutationObserver(() => this._syncVisibility());
    for (const { el } of this._items) {
      this._visObserver.observe(el, { attributes: true, attributeFilter: ['hidden'] });
    }
  }

  set hass(hass) {
    this._hass = hass;
    for (const el of this._children) {
      el.hass = hass;
    }
    this._syncVisibility();
  }

  getCardSize() { return 6; }
}

customElements.define('grid-lens-flex-row-card', GridLensFlexRowCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-flex-row-card',
  name: 'Grid Lens Flex Row',
  description: 'Lays out child cards in a row with per-card flex control (or each card\'s own natural aspect), stacking below a configurable breakpoint.',
});
