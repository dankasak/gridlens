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
 * Deferrable-load nodes pick an icon by matching their name (and power/switch
 * entity ids) against DEFER_ICON_RULES below — e.g. an "EV Mobile Charger" gets
 * the EV charger icon, a hot water system gets the water heater one. Anything
 * unmatched keeps the dashed ring + mdi placeholder. `deferrable_icons` maps a
 * load name onto a built-in key or a URL of your own when the name doesn't say.
 *
 * Config (all optional — defaults match this install's Sigenergy/EVConduit entities):
 *   type: custom:grid-lens-powerflow-card
 *   solar_power_entity, load_power_entity, grid_power_entity,
 *   battery_power_entity, soc_entity, ev_power_entity, ev_active_entity
 *   icons: { solar, battery, home, grid, ev, water_heater }  // URLs; omit/null = placeholder
 *   deferrable_icons: { "Smart Load 01": water_heater }      // load name -> icon key or URL
 *   max_height: 420   // fixed height (px) for the diagram; set 0/null for natural (aspect-ratio) height
 *   max_width: null   // cap (px) on how wide the card grows; set 0/null to fill its container
 *   icon_scale: 1.0   // multiplier on node/icon size (1.5 = 50% bigger); viewBox grows to fit
 *   font_scale: 1.0   // multiplier on label text size, independent of icon_scale
 *   name_font_size: null    // px override for the node-name label (e.g. "Battery"); wins over font_scale
 *   value_font_size: null   // px override for the value label (e.g. "1.23 kW"); wins over font_scale
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

// Deferrable-load nodes have no configured icon of their own, so pick one from what the load
// is called (falling back to its power/switch entity ids, which often carry the appliance
// name when the friendly name doesn't). First match wins — keep the specific patterns first.
// `key` indexes into config.icons; `mdi` is the placeholder used when that icon is unset.
const DEFER_ICON_RULES = [
  { re: /(hot[\s_-]*water|water[\s_-]*heat|\bhws\b|boiler|immersion)/i, key: 'water_heater', mdi: 'mdi:water-boiler' },
  { re: /(\bevse?\b|electric[\s_-]*vehicle|wallbox|\bcharger\b|\bcar\b)/i, key: 'ev', mdi: 'mdi:ev-station' },
];

// Layout is fully computed (see render()/_bottomActors): Solar + Grid feed the central Home
// hub from the top; Battery + deferrable loads (+ optional EV) fan out on an even radial arc
// below it, generic in the item count. The viewBox is derived from the resulting node bounds.

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
        ev: '/grid_lens/icons/grid-lens-ev-charger.jpg',
        water_heater: '/grid_lens/icons/grid-lens-water-heater.jpg',
      },
      (config && config.icons) || {}
    );
    // Per-load icon overrides given as a URL are registered under a synthetic `defer:<name>`
    // key so they go through the same preload/chroma-key path as the built-in icons. Values
    // that name a built-in key instead ("water_heater") need no registration.
    for (const [name, val] of Object.entries((config && config.deferrable_icons) || {})) {
      if (typeof val === 'string' && /^(\/|https?:|data:)/.test(val)) {
        icons[`defer:${name.trim().toLowerCase()}`] = val;
      }
    }
    this._config = Object.assign(
      {
        solar_power_entity: 'sensor.sigen_0_total_pv_power',
        load_power_entity: 'sensor.sigen_plant_general_load_power',
        grid_power_entity: 'sensor.sigen_plant_grid_sensor_active_power',
        battery_power_entity: 'sensor.sigen_plant_ess_power',
        soc_entity: 'sensor.sigen_plant_ess_soc',
        ev_power_entity: 'sensor.evconduit_charge_rate',
        ev_active_entity: 'sensor.evconduit_is_charging',
        // Type-1 deferrable loads (simple on/off appliances), shown as their own nodes off
        // Home with live power. By default (null) the card AUTO-DISCOVERS them from a GridLens
        // sensor's `deferrable_loads` attribute (name + auto-resolved power_entity + switch),
        // so no hand-config is needed. Set an explicit array of { name, power_entity,
        // switch_entity? } to override, or [] to hide the row. deferrable_source_entity pins
        // which GridLens sensor to read (else the card finds the first one exposing the attr).
        deferrable_loads: null,
        deferrable_source_entity: null,
        // Icon override per deferrable load, keyed by load name (case-insensitive). The value
        // is either a built-in icons key ('water_heater', 'ev', …) or a URL. Only needed when
        // the name doesn't match DEFER_ICON_RULES — e.g. a generic "Smart Load 01".
        deferrable_icons: {},
        // Show the dedicated EV satellite node (evconduit by default). Set false when the EV
        // is already represented as a deferrable load (its own switch/plug) to avoid drawing
        // it twice.
        show_ev: true,
        max_height: 420, // fixed height (px); set null/0 for natural (aspect-ratio) height
        max_width: null, // fixed width (px); set null/0 to fill the card's container
        icon_scale: 1.0, // multiplier on node/icon size (1.5 = 50% bigger); viewBox grows to fit
        font_scale: 1.0, // multiplier on label text size, independent of icon_scale
        name_font_size: null, // px override for the node-name label; wins over font_scale
        value_font_size: null, // px override for the value label (e.g. "1.23 kW"); wins over font_scale
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
    if (c.deferrable_source_entity) watch.push(c.deferrable_source_entity);
    const loads = this._resolveDeferLoads();
    for (const ld of loads) {
      if (ld && ld.power_entity) watch.push(ld.power_entity);
      if (ld && ld.switch_entity) watch.push(ld.switch_entity);
    }
    // Include the resolved load identity so a reconfigure (set of loads changes) re-renders.
    const sig = watch.map((e) => (hass.states[e] ? hass.states[e].state : '')).join('|')
      + '#' + loads.map((l) => l.power_entity).join(',');
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

  // Label font sizes track icon_scale by default so the text stays legible next to enlarged
  // icons (previously fixed, so it looked tiny at icon_scale 2). font_scale adjusts text size
  // independently (e.g. bigger icons, same labels), and name_font_size/value_font_size are
  // explicit px overrides for when a multiplier isn't precise enough.
  _fs() {
    const c = this._config;
    const scale = (c.icon_scale || 1) * (c.font_scale || 1);
    return {
      name: c.name_font_size != null ? +c.name_font_size : +(12 * scale).toFixed(1),
      val: c.value_font_size != null ? +c.value_font_size : +(11 * scale).toFixed(1),
    };
  }

  // Generic node renderer, driven by a descriptor { cx, cy, r, colorVar, imgKey, icon,
  // name, value, dim }. Used for every node (sources, hub, and the radial actors) so the
  // layout can be fully computed instead of hardcoded per node.
  _pnode(a) {
    const fs = this._fs();
    const nameY = a.cy + a.r + fs.name + 2;
    const valY = nameY + fs.val + 3;
    const dataUrl = a.imgKey ? this._imgData[a.imgKey] : null;
    const hasImage = !!dataUrl;
    const clipId = `pf-clip-${Math.round(a.cx)}-${Math.round(a.cy)}`;
    const inner = hasImage
      ? `<clipPath id="${clipId}"><circle cx="${a.cx}" cy="${a.cy}" r="${a.r - 3}"/></clipPath>
         <image href="${dataUrl}" x="${a.cx - a.r}" y="${a.cy - a.r}" width="${a.r * 2}" height="${a.r * 2}"
                clip-path="url(#${clipId})" preserveAspectRatio="xMidYMid slice"/>`
      : `<foreignObject x="${a.cx - a.r}" y="${a.cy - a.r}" width="${a.r * 2}" height="${a.r * 2}">
           <div xmlns="http://www.w3.org/1999/xhtml" class="placeholder-icon">
             <ha-icon icon="${a.icon || 'mdi:help-circle-outline'}"></ha-icon>
           </div>
         </foreignObject>`;
    return `
      <g class="node${a.dim ? ' dim' : ''}">
        <circle class="ring ${hasImage ? '' : 'placeholder'}" cx="${a.cx}" cy="${a.cy}" r="${a.r}"
                style="--nc: var(${a.colorVar})"/>
        ${inner}
        <text x="${a.cx}" y="${nameY}" text-anchor="middle" class="node-name" style="font-size:${fs.name}px">${a.name}</text>
        <text x="${a.cx}" y="${valY}" text-anchor="middle" class="node-value" style="font-size:${fs.val}px">${a.value}</text>
      </g>`;
  }

  // A straight radial spoke from a peripheral node to the hub. Animation flows node→hub by
  // default (a source/discharge feeding Home); `reverse` flows hub→node (import, battery
  // charge, or a load drawing from Home).
  _spoke(a, hub) {
    const dur = Math.max(0.4, 2.0 / (0.4 + a.kw)).toFixed(2);
    const cls = ['flow', a.active ? 'active' : 'idle', a.reverse ? 'reverse' : ''].join(' ').trim();
    return `<path class="${cls}" d="M${a.cx.toFixed(1)},${a.cy.toFixed(1)} L${hub.cx},${hub.cy}"
             style="--nc: var(${a.colorVar}); animation-duration: ${dur}s"/>`;
  }

  // Type-1 deferrable loads: a dynamic row of nodes below the main diagram, each a sub-load
  // of Home. Positions are computed from the count so 1..N loads spread evenly across the
  // width; each cycles through the --c-defN palette (matches the advisory card's device
  // colours). Returns [{ cx, cy, r, name, kw, on, color, path }].
  // The effective deferrable-load list: an explicit config array wins; otherwise read the
  // `deferrable_loads` attribute the GridLens integration publishes (auto-discovered power
  // sensors), from the pinned source entity or the first sensor exposing it. Only loads with
  // a real power_entity are drawable.
  _resolveDeferLoads() {
    const c = this._config;
    if (Array.isArray(c.deferrable_loads)) return c.deferrable_loads;
    const hass = this._hass;
    if (!hass) return [];
    let attr = null;
    if (c.deferrable_source_entity && hass.states[c.deferrable_source_entity]) {
      attr = hass.states[c.deferrable_source_entity].attributes.deferrable_loads;
    } else {
      for (const eid of Object.keys(hass.states)) {
        if (!eid.startsWith('sensor.')) continue;
        const a = hass.states[eid].attributes;
        if (a && Array.isArray(a.deferrable_loads)) { attr = a.deferrable_loads; break; }
      }
    }
    if (!Array.isArray(attr)) return [];
    return attr
      .filter((d) => d && d.power_entity)
      .map((d) => ({ name: d.name, power_entity: d.power_entity, switch_entity: d.switch_entity || null }));
  }

  // Which icon a deferrable load draws with: an explicit deferrable_icons entry wins,
  // otherwise the name/entity keyword rules, otherwise the generic plug placeholder.
  // Returns { imgKey, mdi } — imgKey null means "no image, use the mdi glyph".
  _deferIcon(ld) {
    const overrides = this._config.deferrable_icons || {};
    const name = (ld.name || '').trim();
    const hit = Object.keys(overrides).find((k) => k.trim().toLowerCase() === name.toLowerCase());
    if (hit) {
      const val = overrides[hit];
      if (!val) return { imgKey: null, mdi: 'mdi:power-plug' };
      // A built-in key resolves directly; anything else was registered as `defer:<name>`.
      const imgKey = this._config.icons[val] !== undefined ? val : `defer:${hit.trim().toLowerCase()}`;
      return { imgKey, mdi: 'mdi:power-plug' };
    }
    const haystack = [name, ld.power_entity || '', ld.switch_entity || ''].join(' ');
    for (const rule of DEFER_ICON_RULES) {
      if (rule.re.test(haystack)) return { imgKey: rule.key, mdi: rule.mdi };
    }
    return { imgKey: null, mdi: 'mdi:power-plug' };
  }

  // The "consumer" group — Battery + every deferrable load (+ the optional EV node) — placed
  // on an even radial arc below the Home hub. Generic in the count: 1 item sits straight down,
  // N items fan out symmetrically, and the arc radius is grown so nodes never overlap. Returns
  // positioned actor descriptors (the same shape _pnode/_spoke consume).
  _bottomActors(f, conn, hub) {
    const scale = this._config.icon_scale || 1;
    const actors = [{
      baseR: 30, colorVar: '--c-battery', imgKey: 'battery', icon: 'mdi:battery',
      name: 'Battery', value: conn.battery.label,
      active: conn.battery.active, reverse: conn.battery.reverse, kw: conn.battery.kw, dim: false,
    }];
    if (this._config.show_ev !== false) {
      actors.push({
        baseR: 26, colorVar: '--c-ev', imgKey: 'ev', icon: 'mdi:ev-station',
        name: 'EV', value: conn.ev.label,
        active: conn.ev.active, reverse: true, kw: conn.ev.kw, dim: !conn.ev.active,
      });
    }
    this._resolveDeferLoads().forEach((ld, i) => {
      const kw = this._toKw(ld.power_entity);
      let on;
      if (ld.switch_entity) {
        const st = this._hass && this._hass.states[ld.switch_entity];
        on = !!st && String(st.state).toLowerCase() === 'on';
      } else {
        on = kw > MIN_KW;
      }
      const ic = this._deferIcon(ld);
      actors.push({
        baseR: 26, colorVar: `--c-def${(i % 4) + 1}`, imgKey: ic.imgKey, icon: ic.mdi,
        name: ld.name || `Load ${i + 1}`, value: `${kw.toFixed(2)} kW`,
        active: on && kw > MIN_KW, reverse: true, kw, dim: !on,
      });
    });

    const M = actors.length;
    const maxR = Math.max(...actors.map((a) => a.baseR * scale));
    // Even angular spread, centred straight-down (phi=0); wider fan for more items.
    const spanDeg = M <= 1 ? 0 : Math.min(170, 52 * (M - 1));
    const span = (spanDeg * Math.PI) / 180;
    let R = 120 + maxR; // base clearance from the hub centre
    if (M > 1) {
      const dphi = span / (M - 1);
      R = Math.max(R, (2 * maxR + 18) / (2 * Math.sin(dphi / 2))); // guarantee no overlap
    }
    actors.forEach((a, i) => {
      const phi = M === 1 ? 0 : (-span / 2 + (i * span) / (M - 1));
      a.cx = hub.cx + R * Math.sin(phi);
      a.cy = hub.cy + R * Math.cos(phi);
      a.r = a.baseR * scale;
    });
    return actors;
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
          --c-def1:#e11d48; --c-def2:#0d9488; --c-def3:#7c3aed; --c-def4:#ca8a04;
        }
        :host(.dark) {
          --c-solar:#fbbf24; --c-grid:#a78bfa; --c-battery:#4ade80; --c-ev:#22d3ee;
          --c-def1:#fb7185; --c-def2:#2dd4bf; --c-def3:#a78bfa; --c-def4:#facc15;
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
        /* Halo: paint a card-background-coloured stroke behind the glyphs so an animated
           flow line passing under a label is knocked out instead of showing through it.
           font-size is set inline per node so it can scale with icon_scale. */
        .node-name, .node-value {
          paint-order: stroke; stroke: var(--card-background-color); stroke-width: 4px;
          stroke-linejoin: round;
        }
        .node-name { font-weight:600; fill:var(--primary-text-color); }
        .node-value { fill:var(--secondary-text-color); }
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
        .node.dim { opacity: .4; }
      </style>
    `;

    const scale = this._config.icon_scale || 1;
    const fs = this._fs();

    // Home hub in the centre; Solar and Grid are the two top sources feeding it.
    const hub = {
      cx: 200, cy: 190, r: 42 * scale, colorVar: '--c-home', imgKey: 'home', icon: null,
      name: 'Home', value: `${f.loadKw.toFixed(2)} kW`, dim: false,
    };
    const sources = [
      { cx: 70, cy: 60, r: 30 * scale, colorVar: '--c-solar', imgKey: 'solar', icon: null,
        name: 'Solar', value: conn.solar.label, active: conn.solar.active, reverse: false, kw: conn.solar.kw, dim: false },
      { cx: 330, cy: 60, r: 30 * scale, colorVar: '--c-grid', imgKey: 'grid', icon: null,
        name: 'Grid', value: conn.grid.label, active: conn.grid.active, reverse: conn.grid.reverse, kw: conn.grid.kw, dim: false },
    ];
    // Consumer group (Battery + deferrable loads + optional EV), spread evenly on a radial arc.
    const actors = this._bottomActors(f, conn, hub);
    const peripherals = [...sources, ...actors];

    // Spokes under nodes; hub drawn last-of-the-fixed so its icon sits above the spoke ends.
    const connectorsHtml = peripherals.map((a) => this._spoke(a, hub)).join('');
    const nodesHtml = [...sources, hub, ...actors].map((a) => this._pnode(a)).join('');

    // Fully computed viewBox: fit every node's circle + its labels below it, with padding.
    // Keeps the diagram framed for any actor count / icon_scale, even when nodes fall outside
    // the old fixed 400-wide box.
    const labelDrop = fs.name + fs.val + 8;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const a of [...peripherals, hub]) {
      minX = Math.min(minX, a.cx - a.r);
      maxX = Math.max(maxX, a.cx + a.r);
      minY = Math.min(minY, a.cy - a.r);
      maxY = Math.max(maxY, a.cy + a.r + labelDrop);
    }
    const pad = 10;
    const vb = `${(minX - pad).toFixed(1)} ${(minY - pad).toFixed(1)} ` +
               `${(maxX - minX + 2 * pad).toFixed(1)} ${(maxY - minY + 2 * pad).toFixed(1)}`;

    this.shadowRoot.innerHTML = `
      ${styles}
      <ha-card>
        <div class="head">
          <span class="title">Power Flow</span>
          <span class="badge">PROOF OF CONCEPT</span>
        </div>
        <svg viewBox="${vb}">
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
