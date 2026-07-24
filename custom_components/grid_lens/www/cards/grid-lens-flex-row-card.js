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
 *   cards:           // array of normal card configs, each with an optional extra key:
 *     - type: custom:some-card
 *       flex: "0 1 350px"   // CSS flex shorthand for this child when in row layout
 *                           // (optional, default "1 1 300px")
 */
class GridLensFlexRowCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hass = null;
    this._children = [];
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

    const style = document.createElement('style');
    style.textContent = `
      :host { display: block; container-type: inline-size; }
      .wrap { display: flex; gap: ${gap}px; align-items: flex-start; }
      .wrap > .item { min-width: 0; }
      @container (max-width: ${breakpoint}px) {
        .wrap { flex-direction: column; }
        .wrap > .item { flex: 1 1 auto !important; width: 100%; }
      }
    `;

    const wrap = document.createElement('div');
    wrap.className = 'wrap';

    this._children = this._config.cards.map((cardCfg) => {
      const { flex, ...rest } = cardCfg;
      const el = helpers.createCardElement(rest);
      el.style.display = 'block';
      if (this._hass) el.hass = this._hass;

      const item = document.createElement('div');
      item.className = 'item';
      item.style.flex = flex || '1 1 300px';
      item.appendChild(el);
      wrap.appendChild(item);
      return el;
    });

    this.shadowRoot.innerHTML = '';
    this.shadowRoot.appendChild(style);
    this.shadowRoot.appendChild(wrap);
  }

  set hass(hass) {
    this._hass = hass;
    for (const el of this._children) {
      el.hass = hass;
    }
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
