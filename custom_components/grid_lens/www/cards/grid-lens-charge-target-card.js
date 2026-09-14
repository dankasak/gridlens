/*
 * Grid Lens Charge Target Card
 *
 * Ad-hoc, one-off "charge to X% by a datetime" target per SOC-tracked deferrable load
 * (e.g. an EV) — "100% by 7am Saturday" before a trip. See FEATURES.md §9a.
 *
 * Auto-discovers the paired number/datetime entities (number.py's
 * GridLensChargeTargetPercentNumber, datetime.py's GridLensChargeTargetTimeDateTime) and
 * renders one row per device as native HA `tile` cards — same approach as
 * grid-lens-boost-tuning-card.js, so styling stays pixel-identical to native HA. Only a
 * device with an SOC sensor + capacity configured gets this entity pair at all (the
 * optimizer needs both to compute how much energy is actually needed), so a device with
 * no SOC tracking simply never produces a row here — nothing to configure to hide it.
 *
 * Auto-discovery fingerprint: `charge_target_role` ('percent' | 'time') on the entity's
 * state attributes, paired across the two by the `deferrable_sensor_id` attribute they
 * both also carry (same attribute Today Boost's entities use — `charge_target_role` is
 * what distinguishes these from a Today Boost number, which has no such attribute).
 *
 * Device naming: same `deferrable_loads` sensor attribute lookup as
 * grid-lens-boost-tuning-card.js / grid-lens-powerflow-card.js's _resolveDeferLoads(), so
 * labels never drift between cards.
 *
 * Config:
 *   type: custom:grid-lens-charge-target-card
 *   title: Charge Targets    (optional)
 */
class GridLensChargeTargetCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._sig = '';
    this._devices = []; // [{ key, name, percentEntity, timeEntity, statusEl, tiles: [] }]
  }

  setConfig(config) {
    this._config = Object.assign({ title: 'Charge Targets' }, config);
    this._renderShell();
  }

  getCardSize() {
    return this._devices.length === 0 ? 2 : this._devices.length * 2 + 1;
  }

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

  _resolveDevices() {
    const hass = this._hass;
    if (!hass) return [];
    const nameMap = this._resolveDeferrableNames();
    const byKey = {};
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('number.') && !eid.startsWith('datetime.')) continue;
      const attrs = hass.states[eid].attributes || {};
      const role = attrs.charge_target_role;
      if (role !== 'percent' && role !== 'time') continue;
      const key = attrs.deferrable_sensor_id || eid;
      const entry = (byKey[key] = byKey[key] || { key, name: nameMap[key] || key });
      if (role === 'percent') entry.percentEntity = eid;
      else entry.timeEntity = eid;
    }
    const devices = Object.values(byKey).filter((d) => d.percentEntity && d.timeEntity);
    devices.sort((a, b) => a.name.localeCompare(b.name));
    return devices;
  }

  set hass(hass) {
    this._hass = hass;
    const devices = this._resolveDevices();
    const sig = devices.map((d) => `${d.percentEntity}:${d.timeEntity}:${d.name}`).join(',');
    if (sig !== this._sig) {
      this._sig = sig;
      this._buildRows(devices);
    } else {
      for (const d of this._devices) {
        for (const t of d.tiles) t.el.hass = hass;
        this._updateStatus(d);
      }
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
        .device { margin-bottom: 16px; }
        .device:last-child { margin-bottom: 0; }
        .device-name {
          font-size: 0.85rem;
          font-weight: 500;
          color: var(--secondary-text-color);
          padding: 0 4px 6px;
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
          padding: 6px 4px 0;
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
      <div class="devices"></div>
    `;
  }

  async _buildRows(devices) {
    const root = this.shadowRoot.querySelector('.devices');
    if (!root) return;

    if (!devices.length) {
      root.innerHTML = '<div class="empty">No SOC-tracked deferrable loads configured</div>';
      this._devices = [];
      return;
    }
    if (!window.loadCardHelpers) {
      root.innerHTML = '<div class="empty" style="color:var(--error-color)">Card helpers unavailable</div>';
      return;
    }

    const helpers = await window.loadCardHelpers();
    root.innerHTML = '';
    this._devices = devices.map((d) => {
      const percentTile = helpers.createCardElement({
        type: 'tile', entity: d.percentEntity, name: 'Target %',
        features: [{ type: 'numeric-input' }],
      });
      percentTile.hass = this._hass;
      const timeTile = helpers.createCardElement({
        type: 'tile', entity: d.timeEntity, name: 'By',
      });
      timeTile.hass = this._hass;

      const wrap = document.createElement('div');
      wrap.className = 'device';
      const label = document.createElement('div');
      label.className = 'device-name';
      label.textContent = d.name;
      const grid = document.createElement('div');
      grid.className = 'grid';
      for (const el of [percentTile, timeTile]) {
        const item = document.createElement('div');
        item.className = 'item';
        item.appendChild(el);
        grid.appendChild(item);
      }
      const status = document.createElement('div');
      status.className = 'status';
      wrap.appendChild(label);
      wrap.appendChild(grid);
      wrap.appendChild(status);
      root.appendChild(wrap);

      const entry = {
        ...d,
        statusEl: status,
        tiles: [{ entity_id: d.percentEntity, el: percentTile }, { entity_id: d.timeEntity, el: timeTile }],
      };
      this._updateStatus(entry);
      return entry;
    });
  }

  _updateStatus(d) {
    if (!d.statusEl || !this._hass) return;
    const pState = this._hass.states[d.percentEntity];
    const tState = this._hass.states[d.timeEntity];
    const percent = pState ? parseFloat(pState.state) : 0;
    const targetIso = tState && tState.state !== 'unknown' && tState.state !== 'unavailable' ? tState.state : null;
    if (percent > 0 && targetIso) {
      const when = new Date(targetIso);
      const formatted = isNaN(when.getTime())
        ? targetIso
        : when.toLocaleString(undefined, {
            weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit',
          });
      d.statusEl.textContent = `Target: ${percent}% by ${formatted}`;
      d.statusEl.classList.add('active');
    } else {
      d.statusEl.textContent = 'No target set';
      d.statusEl.classList.remove('active');
    }
  }

  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}

customElements.define('grid-lens-charge-target-card', GridLensChargeTargetCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-charge-target-card',
  name: 'Grid Lens Charge Target',
  description: 'Auto-discovered ad-hoc "charge to X% by a datetime" targets, per SOC-tracked deferrable load.',
});
