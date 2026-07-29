/*
 * Grid Lens Boost Tuning Card
 *
 * Auto-discovers all deferrable-load boost override number entities and renders each
 * as a real HA `tile` card with the numeric-input feature — same component "Min export
 * price" uses, so styling stays pixel-identical to native HA and free of any future
 * tile-card restyle. Scales with config: add a deferrable load, a new tile appears.
 *
 * Auto-discovery fingerprint: a `number.*` entity carrying a `deferrable_sensor_id`
 * state attribute (GridLensDeferrableOverrideNumber.extra_state_attributes in
 * number.py) — unique_id is registry-only data and never appears in hass.states
 * attributes, so the fingerprint must be a real state attribute, same as the
 * load-control card's `on_threshold_w`/`switch` pattern.
 *
 * Naming: each tile is labelled with the same device name the Power Flow card shows —
 * read from the `deferrable_loads` attribute published by the GridLens cost sensor
 * (sensor.py's _build_deferrable_loads, sourced from the HA Energy dashboard's device
 * name where available — see resolve_device_name) — rather than the boost entity's own
 * friendly_name, which is derived independently and can drift out of sync. Same
 * auto-discovery pattern as grid-lens-powerflow-card.js's _resolveDeferLoads().
 *
 * Config:
 *   type: custom:grid-lens-boost-tuning-card
 *   title: Deferrable Load Targets    (optional)
 */
class GridLensBoostTuningCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._sig = '';
    this._tiles = [];
  }

  setConfig(config) {
    this._config = Object.assign({ title: 'Deferrable Load Targets' }, config);
    this._renderShell();
  }

  getCardSize() {
    return this._tiles.length === 0 ? 2 : this._tiles.length + 1;
  }

  // Canonical device names, keyed by the underlying energy sensor — same source and
  // "first sensor exposing it wins" rule as the Power Flow card, so labels never drift
  // between the two cards.
  _resolveDeferrableNames() {
    const hass = this._hass;
    const map = {};
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('sensor.')) continue;
      const a = hass.states[eid].attributes;
      if (a && Array.isArray(a.deferrable_loads)) {
        for (const d of a.deferrable_loads) {
          if (d && d.energy_entity) map[d.energy_entity] = d.name;
        }
        break;
      }
    }
    return map;
  }

  _resolveBoosts() {
    const hass = this._hass;
    if (!hass) return [];
    const nameMap = this._resolveDeferrableNames();
    const found = [];
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('number.')) continue;
      const attrs = hass.states[eid].attributes || {};
      if ('deferrable_sensor_id' in attrs) {
        found.push({
          entity_id: eid,
          name: nameMap[attrs.deferrable_sensor_id] || attrs.friendly_name || eid,
        });
      }
    }
    found.sort((a, b) => a.name.localeCompare(b.name));
    return found;
  }

  set hass(hass) {
    this._hass = hass;
    const boosts = this._resolveBoosts();
    const sig = boosts.map((b) => `${b.entity_id}:${b.name}`).join(',');
    if (sig !== this._sig) {
      this._sig = sig;
      this._buildTiles(boosts);
    } else {
      for (const t of this._tiles) t.el.hass = hass;
    }
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        .title {
          font-size: 1.1rem;
          font-weight: 500;
          margin-bottom: 16px;
          color: var(--primary-text-color);
          padding: 0 4px;
        }
        .grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
          gap: 12px;
        }
        .item { min-width: 0; }
        .item > * { height: 100%; }
        .empty {
          font-size: 0.9rem;
          color: var(--secondary-text-color);
          padding: 16px;
          text-align: center;
        }
      </style>
      <div class="title">${this._escapeHtml(this._config.title)}</div>
      <div class="grid"></div>
    `;
  }

  async _buildTiles(boosts) {
    const grid = this.shadowRoot.querySelector('.grid');
    if (!grid) return;

    if (!boosts.length) {
      grid.innerHTML = '<div class="empty">No deferrable loads configured</div>';
      this._tiles = [];
      return;
    }
    if (!window.loadCardHelpers) {
      grid.innerHTML = '<div class="empty" style="color:var(--error-color)">Card helpers unavailable</div>';
      return;
    }

    const helpers = await window.loadCardHelpers();
    grid.innerHTML = '';
    this._tiles = boosts.map((b) => {
      const el = helpers.createCardElement({
        type: 'tile',
        entity: b.entity_id,
        name: b.name,
        features: [{ type: 'numeric-input' }],
      });
      el.hass = this._hass;
      const item = document.createElement('div');
      item.className = 'item';
      item.appendChild(el);
      grid.appendChild(item);
      return { entity_id: b.entity_id, el };
    });
  }

  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}

customElements.define('grid-lens-boost-tuning-card', GridLensBoostTuningCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-boost-tuning-card',
  name: 'Grid Lens Boost Tuning',
  description: 'Auto-discovered deferrable-load boost targets, rendered as native HA tile cards.',
});
