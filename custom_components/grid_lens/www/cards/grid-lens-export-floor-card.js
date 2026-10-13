/*
 * Grid Lens Export Floor Card
 *
 * Ad-hoc, one-off "don't discharge/export the main battery below X% until a
 * datetime" reserve — e.g. "keep at least 50% until 7am Tuesday" ahead of a
 * planned grid outage. See FEATURES.md §9d. The mirror image of
 * grid-lens-charge-target-card.js (§9a): that raises the LP's ceiling to force
 * charging UP by a deadline; this raises its lower bound so the optimizer still
 * discharges/exports freely for economics above the floor, never below it,
 * until the deadline passes and normal cost-optimal dispatch resumes on its own.
 *
 * Unlike the charge target card, there is exactly ONE floor per Grid Lens config
 * entry (the main battery is singular, not a list of devices), so this renders a
 * single row, not one per device. Auto-discovers the paired number/datetime
 * entities (number.py's GridLensExportFloorPercentNumber, datetime.py's
 * GridLensExportFloorUntilDateTime) as native HA `tile` cards — same approach as
 * grid-lens-charge-target-card.js, so styling stays pixel-identical to native HA.
 *
 * Auto-discovery fingerprint: `export_floor_role` ('percent' | 'until') on the
 * entity's state attributes, paired across the two by the `config_entry_id`
 * attribute they both also carry — there's no per-device key to pair by here
 * (unlike the charge target pair's `deferrable_sensor_id`), so a multi-site
 * install (more than one Grid Lens config entry) needs this explicit shared key
 * to avoid cross-wiring one entry's percent tile to a different entry's
 * datetime tile.
 *
 * Config:
 *   type: custom:grid-lens-export-floor-card
 *   title: Export Floor    (optional)
 */
class GridLensExportFloorCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._sig = '';
    this._entry = null; // { percentEntity, untilEntity, statusEl, tiles: [] }
  }

  setConfig(config) {
    this._config = Object.assign({ title: 'Export Floor' }, config);
    this._renderShell();
  }

  getCardSize() {
    return this._entry ? 3 : 2;
  }

  _resolveEntries() {
    const hass = this._hass;
    if (!hass) return [];
    const byKey = {};
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('number.') && !eid.startsWith('datetime.')) continue;
      const attrs = hass.states[eid].attributes || {};
      const role = attrs.export_floor_role;
      if (role !== 'percent' && role !== 'until') continue;
      const key = attrs.config_entry_id || eid;
      const entry = (byKey[key] = byKey[key] || { key });
      if (role === 'percent') entry.percentEntity = eid;
      else entry.untilEntity = eid;
    }
    // Exactly one battery per config entry, so exactly one complete pair is
    // expected per install — still filtered (not just `[0]`) the same way the
    // charge target card filters its device list, in case only one half of a
    // pair has loaded yet during startup.
    return Object.values(byKey).filter((e) => e.percentEntity && e.untilEntity);
  }

  set hass(hass) {
    this._hass = hass;
    const entries = this._resolveEntries();
    const sig = entries.map((e) => `${e.percentEntity}:${e.untilEntity}`).join(',');
    if (sig !== this._sig) {
      this._sig = sig;
      this._buildRow(entries[0] || null);
    } else if (this._entry) {
      for (const t of this._entry.tiles) t.el.hass = hass;
      this._updateStatus(this._entry);
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
        .status {
          font-size: 0.8rem;
          color: var(--secondary-text-color);
          padding: 10px 4px 0;
        }
        .status.active { color: var(--primary-color); }
        .empty {
          font-size: 0.9rem;
          color: var(--secondary-text-color);
          padding: 16px;
          text-align: center;
        }
      </style>
      <div class="title">${this._escapeHtml(this._config.title)}</div>
      <div class="content"></div>
    `;
  }

  async _buildRow(entry) {
    const root = this.shadowRoot.querySelector('.content');
    if (!root) return;

    if (!entry) {
      root.innerHTML = '<div class="empty">No battery configured</div>';
      this._entry = null;
      return;
    }
    if (!window.loadCardHelpers) {
      root.innerHTML = '<div class="empty" style="color:var(--error-color)">Card helpers unavailable</div>';
      return;
    }

    const helpers = await window.loadCardHelpers();
    root.innerHTML = '';

    const percentTile = helpers.createCardElement({
      type: 'tile', entity: entry.percentEntity, name: 'Floor %',
      features: [{ type: 'numeric-input' }],
    });
    percentTile.hass = this._hass;
    const untilTile = helpers.createCardElement({
      type: 'tile', entity: entry.untilEntity, name: 'Until',
    });
    untilTile.hass = this._hass;

    const grid = document.createElement('div');
    grid.className = 'grid';
    for (const el of [percentTile, untilTile]) {
      const item = document.createElement('div');
      item.className = 'item';
      item.appendChild(el);
      grid.appendChild(item);
    }
    const status = document.createElement('div');
    status.className = 'status';
    root.appendChild(grid);
    root.appendChild(status);

    this._entry = {
      ...entry,
      statusEl: status,
      tiles: [
        { entity_id: entry.percentEntity, el: percentTile },
        { entity_id: entry.untilEntity, el: untilTile },
      ],
    };
    this._updateStatus(this._entry);
  }

  _updateStatus(e) {
    if (!e.statusEl || !this._hass) return;
    const pState = this._hass.states[e.percentEntity];
    const uState = this._hass.states[e.untilEntity];
    const percent = pState ? parseFloat(pState.state) : 0;
    const untilIso = uState && uState.state !== 'unknown' && uState.state !== 'unavailable' ? uState.state : null;
    if (percent > 0 && untilIso) {
      const when = new Date(untilIso);
      const formatted = isNaN(when.getTime())
        ? untilIso
        : when.toLocaleString(undefined, {
            weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit',
          });
      e.statusEl.textContent = `Reserve: at least ${percent}% held until ${formatted}`;
      e.statusEl.classList.add('active');
    } else {
      e.statusEl.textContent = 'No floor set — normal cost-optimal dispatch';
      e.statusEl.classList.remove('active');
    }
  }

  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}

customElements.define('grid-lens-export-floor-card', GridLensExportFloorCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-export-floor-card',
  name: 'Grid Lens Export Floor',
  description: 'Auto-discovered ad-hoc "don\'t discharge below X% until a datetime" reserve on the main battery.',
});
