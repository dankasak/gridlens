"""The Power Flow card's paywall stub.

Served by PowerflowCardView (__init__.py) in place of the real card, at the exact
same Lovelace resource URL and under the exact same custom-element tag name
(grid-lens-powerflow-card), whenever the install isn't entitled to it (or the API
can't confirm entitlement and there's no last-known-good cached copy to fall back
to — see PowerflowCardView's docstring). Deliberately self-contained: no imports,
no dependency on chart-common.js or the API being reachable, so it always renders
something rather than a blank/broken custom-element error.

This card is one of GridLens's strongest marketing assets — the animated flow is
the thing people actually stop and watch (see MARKETING_VIDEO_PLAN.md) — so the
paywall is written to sell, not just to block: it names what you're missing.
"""

LOCKED_CARD_JS = r"""
class GridLensPowerflowLockedCard extends HTMLElement {
  setConfig(config) { this._config = config || {}; this._render(); }
  set hass(hass) { this._hass = hass; }
  getCardSize() { return 4; }

  _render() {
    if (this.shadowRoot) return;
    this.attachShadow({ mode: 'open' });
    const maxHeight = this._config.max_height || 420;
    this.shadowRoot.innerHTML = `
      <style>
        .card { display: flex; flex-direction: column; align-items: center; justify-content: center;
                gap: 10px; text-align: center; padding: 28px 20px;
                min-height: ${Math.min(220, maxHeight)}px;
                background: var(--card-background-color, #fff);
                border: 1px solid var(--divider-color, #e0e0e0); border-radius: 14px;
                font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
                color: var(--primary-text-color); box-sizing: border-box; }
        .icon { width: 40px; height: 40px; border-radius: 50%;
                background: color-mix(in srgb, var(--primary-color, #03a9f4) 15%, transparent);
                display: flex; align-items: center; justify-content: center; }
        .icon ha-icon { color: var(--primary-color, #03a9f4); --mdc-icon-size: 22px; }
        h3 { margin: 4px 0 0; font-size: 16px; font-weight: 650; }
        p { margin: 0; font-size: 13px; color: var(--secondary-text-color); max-width: 340px;
            line-height: 1.45; }
        a.cta { margin-top: 6px; font-size: 13px; font-weight: 600; text-decoration: none;
                color: #fff; background: var(--primary-color, #03a9f4); padding: 8px 16px;
                border-radius: 20px; }
        a.cta:hover { filter: brightness(1.08); }
      </style>
      <div class="card">
        <div class="icon"><ha-icon icon="mdi:flash-alert"></ha-icon></div>
        <h3>Unlock the Power Flow card</h3>
        <p>See your solar, battery, grid and every deferrable load as one live animated
           diagram — the same view GridLens uses to decide what to do next.</p>
        <a class="cta" href="https://gridlens.au/subscribe.html?addon=1" target="_blank" rel="noopener">
          Unlock with Battery Control + Power Flow
        </a>
      </div>`;
  }
}
if (!customElements.get('grid-lens-powerflow-card')) {
  customElements.define('grid-lens-powerflow-card', GridLensPowerflowLockedCard);
}
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === 'grid-lens-powerflow-card')) {
  window.customCards.push({
    type: 'grid-lens-powerflow-card',
    name: 'Grid Lens Power Flow',
    description: 'Live animated energy flow diagram (Battery Control + Power Flow add-on).',
  });
}
"""
