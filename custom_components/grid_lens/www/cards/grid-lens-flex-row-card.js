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
 *   cards:           // array of normal card configs, each with optional extra keys:
 *     - type: custom:some-card
 *       flex: "0 1 350px"   // CSS flex shorthand for this child when in row layout
 *                           // (optional, default "1 1 300px")
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
 * `own_line_when_siblings` deliberately does NOT use flex-wrap on the main row: wrapping is
 * decided from each item's flex-BASIS rather than its post-shrink size (the reason the
 * original wrapping version of this card was abandoned — see above), so a 900px and a 700px
 * diagram would break onto separate lines long before they actually ran out of room. Instead
 * the non-own-line children live in their own nested nowrap `.group`, and only the own-line
 * child's flex-basis is switched to 100% — so the wrap point is one we choose, and the
 * grouped children still shrink to share a line the way they do today.
 */
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
    const breakpoint = this._config.breakpoint != null ? this._config.breakpoint : 700;

    // Only introduce the nested .group wrapper when some child actually asks for own-line
    // behaviour — every other usage keeps exactly today's single flat nowrap row.
    const hasOwnLine = this._config.cards.some(
      (c) => c && c.own_line_when_siblings != null
    );

    const style = document.createElement('style');
    style.textContent = `
      :host { display: block; container-type: inline-size; }
      .wrap { display: flex; gap: ${gap}px; align-items: flex-start;
              flex-wrap: ${hasOwnLine ? 'wrap' : 'nowrap'}; }
      .group { display: flex; gap: ${gap}px; align-items: flex-start;
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

    this._items = this._config.cards.map((cardCfg) => {
      const { flex, own_line_when_siblings: ownLine, ...rest } = cardCfg;
      const el = helpers.createCardElement(rest);
      if (this._hass) el.hass = this._hass;

      const item = document.createElement('div');
      item.className = 'item';
      const baseFlex = flex || '1 1 300px';
      item.style.flex = baseFlex;
      item.appendChild(el);
      // Own-line children hang off .wrap directly (so their basis can force a break);
      // everything else goes inside the nowrap .group that occupies the first line.
      (ownLine != null ? wrap : (group || wrap)).appendChild(item);
      return { el, item, baseFlex, ownLine: ownLine != null ? Number(ownLine) : null };
    });
    this._children = this._items.map((x) => x.el);

    this.shadowRoot.innerHTML = '';
    this.shadowRoot.appendChild(style);
    this.shadowRoot.appendChild(wrap);
    this._syncVisibility();
    this._observeVisibility();
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
      if (x.ownLine == null) continue;
      // Basis 100% forces this item past the .group on the first line and onto its own.
      x.item.style.flex = visibleSiblings >= x.ownLine ? '1 1 100%' : x.baseFlex;
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
  description: 'Lays out child cards in a row with per-card flex control, stacking below a configurable breakpoint.',
});
