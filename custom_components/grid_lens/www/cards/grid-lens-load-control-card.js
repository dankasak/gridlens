/*
 * Grid Lens Deferrable Load Control Card
 *
 * One toggle per configured deferrable-load control switch (GridLensDeferrableLoadSwitch —
 * see switch.py), auto-discovered so this card needs zero editing as loads are added,
 * removed, or reconfigured on any install — including a fresh install with none configured
 * yet, which renders a helpful empty state rather than a blank card.
 *
 * Auto-discovery fingerprint: a `switch.*` entity whose attributes carry both `switch`
 * (the physical appliance switch it drives) and `on_threshold_w` — the exact shape of
 * GridLensDeferrableLoadSwitch.status() (switch.py) and DeferrableLoadController.status()
 * (control/load_controller.py). This is unique among GridLens switches — the battery
 * control switch's attributes (control/manager.py status()) carry neither key — so no
 * explicit entity list is required. An explicit `switches:` config list still overrides,
 * for anyone who wants to hand-pick/order a subset.
 *
 * Config:
 *   type: custom:grid-lens-load-control-card
 *   title: Deferrable Loads          (optional)
 *   switches: [switch.foo_control]   (optional — explicit override of auto-discovery)
 */
import { STYLE, esc } from './grid-lens-chart-common.js?v=20260729d';

function friendlyNote(note) {
  if (!note) return '';
  if (note.startsWith('command_error')) return 'Command error';
  return note.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

class GridLensLoadControlCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._sig = '';
    this._dark = false;
  }

  setConfig(config) {
    this._config = Object.assign({ title: 'Deferrable Loads' }, config);
    this._renderShell();
  }

  getCardSize() { return 2; }

  // Explicit override wins; otherwise scan every switch.* entity for the load-controller
  // attribute fingerprint (see file header) rather than assuming a fixed naming scheme —
  // installs vary in retailer/hardware/device count, per the project's generic-design rule.
  _resolveSwitches() {
    const hass = this._hass;
    if (!hass) return [];
    if (Array.isArray(this._config.switches) && this._config.switches.length) {
      return this._config.switches.filter((eid) => hass.states[eid]);
    }
    const found = [];
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('switch.')) continue;
      const a = hass.states[eid].attributes || {};
      if ('on_threshold_w' in a && 'switch' in a) found.push(eid);
    }
    found.sort((a, b) => {
      const na = (hass.states[a].attributes.name || a);
      const nb = (hass.states[b].attributes.name || b);
      return na.localeCompare(nb);
    });
    return found;
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this.classList.toggle('dark', dark); }

    const switches = this._resolveSwitches();
    const sig = switches.map((eid) => {
      const st = hass.states[eid];
      return `${eid}=${st.state}|${(st.attributes || {}).note}|${(st.attributes || {}).commanded}`;
    }).join(',');
    if (sig !== this._sig) { this._sig = sig; this._switches = switches; this._paint(); }
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}
        .rows { display: flex; flex-direction: column; gap: 2px; margin-top: 8px; }
        .row { display: flex; align-items: center; gap: 12px; padding: 9px 2px; border-bottom: 1px solid var(--border); }
        .row:last-child { border-bottom: none; }
        .row .icon { color: var(--ink2); flex: 0 0 auto; }
        .row .info { flex: 1 1 auto; min-width: 0; }
        .row .name { font-size: 13.5px; font-weight: 550; color: var(--ink); }
        .row .meta { font-size: 11px; color: var(--ink2); margin-top: 1px; }
        .row .meta.err { color: var(--buy); }
        .sw { position: relative; flex: 0 0 auto; width: 40px; height: 22px; border-radius: 12px;
              background: var(--border); cursor: pointer; transition: background .15s ease; }
        .sw::after { content: ''; position: absolute; top: 2px; left: 2px; width: 18px; height: 18px;
              border-radius: 50%; background: var(--surface); box-shadow: 0 1px 3px rgba(0,0,0,.3);
              transition: transform .15s ease; }
        .sw.on { background: var(--good); }
        .sw.on::after { transform: translateX(18px); }
        .sw.unavail { opacity: .45; cursor: default; }
      </style>
      <div class="card"><div class="body"></div></div>
    `;
  }

  _paint() {
    const body = this.shadowRoot && this.shadowRoot.querySelector('.body');
    if (!body) return;
    const hass = this._hass;
    const switches = this._switches || [];

    const header = `
      <div class="hd">
        <div class="title">${esc(this._config.title)}</div>
        ${switches.length ? `<div class="sub">${switches.length} configured</div>` : ''}
      </div>`;

    if (!switches.length) {
      body.innerHTML = header +
        `<div class="waiting">No deferrable loads have a control switch assigned yet.<br>
         <span class="sub">Assign one via the Grid Lens integration's Reconfigure flow to enable control.</span></div>`;
      return;
    }

    const rows = switches.map((eid) => {
      const st = hass.states[eid];
      const a = st.attributes || {};
      const on = st.state === 'on';
      const note = friendlyNote(a.note);
      const isErr = (a.note || '').startsWith('command_error');
      const meta = `${on ? 'Controlling' : 'Not controlling'} · ${esc(a.switch || '')}${note ? ' · ' + esc(note) : ''}`;
      return `
        <div class="row" data-eid="${esc(eid)}">
          <ha-icon class="icon" icon="mdi:power-plug"></ha-icon>
          <div class="info">
            <div class="name">${esc(a.name || eid)}</div>
            <div class="meta${isErr ? ' err' : ''}">${meta}</div>
          </div>
          <div class="sw ${on ? 'on' : ''}" data-eid="${esc(eid)}" title="${on ? 'Turn off' : 'Turn on'}"></div>
        </div>`;
    }).join('');

    body.innerHTML = header + `<div class="rows">${rows}</div>`;

    body.querySelectorAll('.sw').forEach((el) => {
      el.addEventListener('click', () => {
        const eid = el.getAttribute('data-eid');
        this._hass.callService('switch', 'toggle', { entity_id: eid });
      });
    });
  }
}

customElements.define('grid-lens-load-control-card', GridLensLoadControlCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-load-control-card',
  name: 'Grid Lens Load Control',
  description: 'Toggle control on/off for each configured deferrable load, auto-discovered.',
});
