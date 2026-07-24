/*
 * Grid Lens Power Flow Card — PROOF OF CONCEPT
 * Animated Solar / Grid / Battery / Home / EV flow diagram, in the spirit of
 * power-flow-card-plus, but skinned with our own Leonardo-generated icons.
 *
 * The icon images ship on a plain white background (no alpha channel on the
 * JPEGs, and the PNG isn't guaranteed transparent either) — stripWhiteBg()
 * chroma-keys them client-side via canvas so they drop onto the node circles
 * cleanly in both light and dark theme. Cached per URL so it only runs once.
 *
 * EV has no generated icon yet (out of Leonardo credits for today) — it falls
 * back to a dashed ring + mdi placeholder icon until one is supplied via the
 * `icons.ev` config key.
 *
 * Config (all optional — defaults match this install's Sigenergy/EVConduit entities):
 *   type: custom:grid-lens-powerflow-card
 *   solar_power_entity, load_power_entity, grid_power_entity,
 *   battery_power_entity, soc_entity, ev_power_entity, ev_active_entity
 *   icons: { solar, battery, home, grid, ev }   // URLs; omit/null = placeholder
 *   max_height: 420   // fixed height (px) for the diagram; set 0/null for natural (aspect-ratio) height
 *   max_width: null   // cap (px) on how wide the card grows; set 0/null to fill its container
 *   icon_scale: 1.0   // multiplier on node/icon size (1.5 = 50% bigger); viewBox grows to fit
 */

const _bgCache = new Map();

// Chroma-keys near-white pixels to transparent. Returns a Promise<string> (a
// data: URL) so the caller can await it once and cache the result — this runs
// a per-pixel scan over a 1024x1024 image, not something to redo every render.
function stripWhiteBg(src) {
  if (!src) return Promise.resolve(null);
  if (_bgCache.has(src)) return _bgCache.get(src);
  const p = new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      try {
        const canvas = document.createElement('canvas');
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0);
        const imgData = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const d = imgData.data;
        for (let i = 0; i < d.length; i += 4) {
          const dist = 255 - Math.min(d[i], d[i + 1], d[i + 2]); // 0 = pure white
          const alpha = Math.max(0, Math.min(1, (dist - 6) / 36)); // feather the edge
          d[i + 3] = Math.round(d[i + 3] * alpha);
        }
        ctx.putImageData(imgData, 0, 0);
        resolve(canvas.toDataURL('image/png'));
      } catch (e) {
        resolve(src); // e.g. canvas op failed — fall back to the original image
      }
    };
    img.onerror = () => resolve(null);
    img.src = src;
  });
  _bgCache.set(src, p);
  return p;
}

const MIN_KW = 0.05; // below this, treat a flow as idle (no animation)

// Layout: home hub in the middle, four satellites, elbow-curve connectors —
// fixed geometry, not configurable. This is a POC for one specific dashboard,
// not a general-purpose card.
const NODES = {
  solar:   { cx: 70,  cy: 60,  r: 30, name: 'Solar',   color: '--c-solar' },
  grid:    { cx: 330, cy: 60,  r: 30, name: 'Grid',    color: '--c-grid' },
  home:    { cx: 200, cy: 190, r: 42, name: 'Home',    color: '--c-home' },
  battery: { cx: 70,  cy: 320, r: 30, name: 'Battery', color: '--c-battery' },
  ev:      { cx: 330, cy: 320, r: 30, name: 'EV',      color: '--c-ev' },
};
const PATHS = {
  solar:   'M70,60 Q70,190 200,190',
  grid:    'M330,60 Q330,190 200,190',
  battery: 'M70,320 Q70,190 200,190',
  ev:      'M200,190 Q330,190 330,320',
};

class GridLensPowerFlowCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._dark = false;
    this._imgData = {};   // key -> resolved data URL (or null once we know there's no source)
    this._pending = new Set();
  }

  setConfig(config) {
    const icons = Object.assign(
      {
        solar: '/grid_lens/icons/grid-lens-solar.png',
        battery: '/grid_lens/icons/grid-lens-battery.jpg',
        home: '/grid_lens/icons/grid-lens-home.jpg',
        grid: '/grid_lens/icons/grid-lens-grid.jpg',
        ev: null, // no asset yet — placeholder icon
      },
      (config && config.icons) || {}
    );
    this._config = Object.assign(
      {
        solar_power_entity: 'sensor.sigen_0_total_pv_power',
        load_power_entity: 'sensor.sigen_plant_general_load_power',
        grid_power_entity: 'sensor.sigen_plant_grid_sensor_active_power',
        battery_power_entity: 'sensor.sigen_plant_ess_power',
        soc_entity: 'sensor.sigen_plant_ess_soc',
        ev_power_entity: 'sensor.evconduit_charge_rate',
        ev_active_entity: 'sensor.evconduit_is_charging',
        max_height: 420, // fixed height (px); set null/0 for natural (aspect-ratio) height
        max_width: null, // fixed width (px); set null/0 to fill the card's container
        icon_scale: 1.0, // multiplier on node/icon size (1.5 = 50% bigger); viewBox grows to fit
      },
      config
    );
    this._config.icons = icons;
    this._kickOffImageLoads();
    this._sig = '';
    this.render();
  }

  getCardSize() { return 5; }

  _kickOffImageLoads() {
    for (const [key, src] of Object.entries(this._config.icons)) {
      if (!src || this._imgData[key] !== undefined || this._pending.has(key)) continue;
      this._pending.add(key);
      stripWhiteBg(src).then((dataUrl) => {
        this._imgData[key] = dataUrl;
        this._pending.delete(key);
        this.render();
      });
    }
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this.classList.toggle('dark', dark); }

    const c = this._config;
    const watch = [
      c.solar_power_entity, c.load_power_entity, c.grid_power_entity,
      c.battery_power_entity, c.soc_entity, c.ev_power_entity, c.ev_active_entity,
    ];
    const sig = watch.map((e) => (hass.states[e] ? hass.states[e].state : '')).join('|');
    if (sig !== this._sig) { this._sig = sig; this.render(); }
  }

  _toKw(eid) {
    const st = this._hass && this._hass.states[eid];
    if (!st) return 0;
    const v = parseFloat(st.state);
    if (isNaN(v)) return 0;
    return (st.attributes && st.attributes.unit_of_measurement === 'W') ? v / 1000 : v;
  }

  _flows() {
    const c = this._config;
    const solarKw = this._toKw(c.solar_power_entity);
    const loadKw = this._toKw(c.load_power_entity);
    const gridKw = this._toKw(c.grid_power_entity);       // >0 import, <0 export
    const battKw = this._toKw(c.battery_power_entity);    // >0 charge, <0 discharge
    const evKw = this._toKw(c.ev_power_entity);
    const socSt = this._hass && this._hass.states[c.soc_entity];
    const socPct = socSt ? parseFloat(socSt.state) : NaN;
    const evActiveSt = this._hass && this._hass.states[c.ev_active_entity];
    const evActive = !!evActiveSt && String(evActiveSt.state).toLowerCase() === 'true';
    return { solarKw, loadKw, gridKw, battKw, socPct, evKw, evActive };
  }

  // Per-connector: active?, reversed (relative to the path's drawn direction),
  // magnitude (kW, for animation speed), and a human label.
  _connectors(f) {
    const soc = isNaN(f.socPct) ? '' : ` · ${f.socPct.toFixed(0)}%`;
    return {
      solar: {
        active: f.solarKw > MIN_KW, reverse: false, kw: f.solarKw,
        label: `${f.solarKw.toFixed(2)} kW`,
      },
      grid: {
        active: Math.abs(f.gridKw) > MIN_KW, reverse: f.gridKw < 0, kw: Math.abs(f.gridKw),
        label: f.gridKw > MIN_KW ? `Importing ${f.gridKw.toFixed(2)} kW`
          : f.gridKw < -MIN_KW ? `Exporting ${(-f.gridKw).toFixed(2)} kW` : 'Idle',
      },
      battery: {
        active: Math.abs(f.battKw) > MIN_KW, reverse: f.battKw > 0, kw: Math.abs(f.battKw),
        label: (f.battKw > MIN_KW ? `Charging ${f.battKw.toFixed(2)} kW`
          : f.battKw < -MIN_KW ? `Discharging ${(-f.battKw).toFixed(2)} kW` : 'Idle') + soc,
      },
      ev: {
        active: f.evActive && f.evKw > MIN_KW, reverse: false, kw: f.evKw,
        label: (f.evActive && f.evKw > MIN_KW) ? `Charging ${f.evKw.toFixed(2)} kW` : 'Not charging',
      },
    };
  }

  _node(key, valueLabel) {
    const n = NODES[key];
    // Node centres stay put (the connector paths anchor to them); only the radius
    // scales, so a bigger icon_scale grows the circle/icon/labels without moving the
    // layout. render() grows the viewBox height to keep the enlarged labels in frame.
    const r = n.r * (this._config.icon_scale || 1);
    const dataUrl = this._imgData[key];
    const hasImage = !!dataUrl;
    const clipId = `pf-clip-${key}`;
    const image = hasImage
      ? `<clipPath id="${clipId}"><circle cx="${n.cx}" cy="${n.cy}" r="${r - 3}"/></clipPath>
         <image href="${dataUrl}" x="${n.cx - r}" y="${n.cy - r}" width="${r * 2}" height="${r * 2}"
                clip-path="url(#${clipId})" preserveAspectRatio="xMidYMid slice"/>`
      : `<foreignObject x="${n.cx - r}" y="${n.cy - r}" width="${r * 2}" height="${r * 2}">
           <div xmlns="http://www.w3.org/1999/xhtml" class="placeholder-icon">
             <ha-icon icon="mdi:${key === 'ev' ? 'ev-station' : 'help-circle-outline'}"></ha-icon>
           </div>
         </foreignObject>`;
    return `
      <g class="node">
        <circle class="ring ${hasImage ? '' : 'placeholder'}" cx="${n.cx}" cy="${n.cy}" r="${r}"
                style="--nc: var(${n.color})"/>
        ${image}
        <text x="${n.cx}" y="${n.cy + r + 16}" text-anchor="middle" class="node-name">${n.name}</text>
        <text x="${n.cx}" y="${n.cy + r + 30}" text-anchor="middle" class="node-value">${valueLabel}</text>
      </g>`;
  }

  _connectorSvg(key, conn) {
    const d = PATHS[key];
    const n = NODES[key];
    const dur = Math.max(0.4, 2.0 / (0.4 + conn.kw)).toFixed(2);
    const cls = ['flow', conn.active ? 'active' : 'idle', conn.reverse ? 'reverse' : ''].join(' ').trim();
    return `<path class="${cls}" d="${d}" style="--nc: var(${n.color}); animation-duration: ${dur}s"/>`;
  }

  render() {
    if (!this._config) return;
    const f = this._flows();
    const conn = this._connectors(f);
    const maxH = this._config.max_height;
    // An explicit height (not max-height on an auto-height, aspect-locked SVG — that
    // combination is unreliable across browsers and was observed not to shrink the
    // rendered box at all). The viewBox still scales/letterboxes to fit via the
    // default preserveAspectRatio, so the diagram itself isn't distorted or cropped.
    const heightRule = maxH ? `height: ${maxH}px;` : 'height: auto;';
    const maxW = this._config.max_width;
    // max-width (not an explicit width) so the card still shrinks responsively on
    // narrow/mobile screens — it only caps how wide it grows on a big desktop. Unlike
    // the SVG's height above, this is a plain block element (ha-card), so max-width
    // here doesn't have the same auto-height replaced-element ambiguity.
    const widthRule = maxW ? `max-width: ${maxW}px;` : '';

    const styles = `
      <style>
        :host {
          --c-solar:#f59e0b; --c-grid:#8b7cf6; --c-battery:#22c55e; --c-ev:#06b6d4;
          --c-home: var(--primary-text-color);
        }
        :host(.dark) {
          --c-solar:#fbbf24; --c-grid:#a78bfa; --c-battery:#4ade80; --c-ev:#22d3ee;
        }
        ha-card { ${widthRule} }
        .head { display:flex; align-items:center; gap:8px; padding:10px 14px 0; }
        .head .title { font-size:14px; font-weight:500; color:var(--primary-text-color); }
        .head .badge {
          font-size:10px; font-weight:700; letter-spacing:.4px; padding:2px 7px;
          border-radius:20px; border:1px solid var(--divider-color); color:var(--secondary-text-color);
        }
        svg { width:100%; ${heightRule} display:block; padding:2px 6px 6px; box-sizing:border-box; }
        .ring { fill:var(--card-background-color); stroke:var(--divider-color); stroke-width:2; }
        .ring.placeholder { stroke-dasharray:4 4; }
        .placeholder-icon {
          width:100%; height:100%; display:flex; align-items:center; justify-content:center;
        }
        .placeholder-icon ha-icon { color: var(--secondary-text-color); --mdc-icon-size: 30px; }
        .node-name { font-size:12px; font-weight:600; fill:var(--primary-text-color); }
        .node-value { font-size:10.5px; fill:var(--secondary-text-color); }
        .flow { fill:none; stroke-width:2; }
        .flow.idle { stroke:var(--divider-color); opacity:.6; }
        .flow.active {
          stroke: var(--nc); stroke-width:3; stroke-dasharray:5 9; stroke-linecap:round;
          filter: drop-shadow(0 0 3px var(--nc));
          animation-name: pf-flow; animation-timing-function: linear; animation-iteration-count: infinite;
        }
        .flow.active.reverse { animation-direction: reverse; }
        @keyframes pf-flow { to { stroke-dashoffset: -56; } }
        .node .ring.placeholder ~ .node-value,
        .node .ring.placeholder ~ .node-name { opacity: .85; }
      </style>
    `;

    const nodesHtml = [
      this._node('solar', conn.solar.label),
      this._node('grid', conn.grid.label),
      this._node('battery', conn.battery.label),
      this._node('ev', conn.ev.label),
      this._node('home', `${f.loadKw.toFixed(2)} kW`),
    ].join('');

    const connectorsHtml = ['solar', 'grid', 'battery', 'ev']
      .map((key) => this._connectorSvg(key, conn[key])).join('');

    // The lowest-drawn element is the bottom satellites' value label, at
    // cy(320) + r + 30 (+ text descender). Grow the viewBox height to match icon_scale
    // so the labels never clip; width stays 400 (fine for scales up to ~2×).
    const scale = this._config.icon_scale || 1;
    const vbH = Math.max(380, Math.ceil(356 + 30 * scale));

    this.shadowRoot.innerHTML = `
      ${styles}
      <ha-card>
        <div class="head">
          <span class="title">Power Flow</span>
          <span class="badge">PROOF OF CONCEPT</span>
        </div>
        <svg viewBox="0 0 400 ${vbH}">
          ${connectorsHtml}
          ${nodesHtml}
        </svg>
      </ha-card>
    `;
  }
}

customElements.define('grid-lens-powerflow-card', GridLensPowerFlowCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-powerflow-card',
  name: 'Grid Lens Power Flow (POC)',
  description: 'Animated solar/grid/battery/home/EV flow diagram with custom icons.',
  preview: true,
});

console.info(
  '%c GRID-LENS-POWERFLOW-CARD %c POC ',
  'color: white; background: #8b7cf6; font-weight: 700;',
  'color: #8b7cf6; background: white; font-weight: 700;',
);
