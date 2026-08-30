/*
 * Grid Lens Advisory Status Card
 * Header/status + the control-mode timeline (planned vs applied EMS mode) and
 * deferrable-load recommendations for sensor.<...>_planned_dispatch. Read-only
 * (advisory mode).
 *
 * The 5 forecast charts (SOC, dispatch, power, price, cash) that used to live
 * inside this card are now standalone cards — grid-lens-{soc,dispatch,power,
 * price,cash}-chart-card.js — so they can be placed/resized independently in a
 * `sections` dashboard view. This card no longer needs a Today/Horizon toggle
 * (the mode timeline always shows the full trajectory, unwindowed) or a
 * measured-history fetch (nothing here plots a "measured" overlay).
 *
 * Config:
 *   type: custom:grid-lens-advisory-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch     (required)
 *   control_switch_entity: switch.roof_grid_lens_nsw_battery_control (optional)
 *   compact: true                                          (optional)
 *   title: "Optimiser & Plan"                               (optional, compact mode only)
 *   layout_toggles:                                         (optional)
 *     - entity: switch.<...>_show_scene_power_flow
 *       label: Scene
 *     - entity: switch.<...>_show_classic_power_flow
 *       label: Classic
 *
 * `compact: true` renders only the header row (title, plan name/solver/last-run
 * time, status badge) and skips the mode timeline / deferrable-load sections below
 * it — a slim status bar for surfacing "when did the optimiser last run" on a page
 * that isn't the full Battery Plan view (e.g. at the top of the Power Flow page).
 *
 * `layout_toggles` adds a clickable on/off chip per entry to the header, toggling that
 * entity (any `switch.*` — nothing here is specific to the Power Flow layouts it was
 * built for). The chips are deliberately co-located with the plan status rather than
 * living on the cards they control, so "what am I looking at, and when was it computed"
 * reads as one bar. Independent of `compact` — works in the full card too.
 */
import {
  STYLE, esc, fmtTime, fmtDayHour, modeLabel, MODE_COLORS, execMode, reasonFor, deferColorFor,
} from './grid-lens-chart-common.js?v=20260830d';

class GridLensAdvisoryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._traj = null;
    this._summary = {};
    this._deferNames = [];
    this._deferMaxKw = [];
    this._applied = null;
    this._sig = '';
    this._dark = false;
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error('Define "entity" (the planned_dispatch sensor)');
    this._config = Object.assign({
      control_switch_entity: 'switch.roof_grid_lens_nsw_battery_control',
    }, config);
    this._sig = '';
    this._applied = null;
    this._renderShell();
  }

  getCardSize() { return this._config.compact ? 1 : 3; }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this.classList.toggle('dark', dark); }

    const st = hass.states[this._config.entity];
    if (!st) { this._summary = { status: 'unknown' }; this._traj = null; this._paint(); return; }

    const a = st.attributes || {};
    this._traj = Array.isArray(a.trajectory) ? a.trajectory : null;
    this._deferNames = Array.isArray(a.deferrable_names) ? a.deferrable_names : [];
    this._deferMaxKw = Array.isArray(a.deferrable_max_kw) ? a.deferrable_max_kw : [];

    const switchSt = hass.states[this._config.control_switch_entity];
    if (switchSt) {
      const sa = switchSt.attributes || {};
      this._applied = {
        action: sa.applied_action || null,
        power_w: sa.applied_power_w || 0,
        at: sa.applied_at || null,
      };
    }

    this._summary = {
      status: a.status || st.state,
      plan_name: a.plan_name,
      solver: a.solver,
      generated_at: a.generated_at,
      reason: a.reason,
      restored: a.restored === true,
    };

    // Toggle chips live in this card but reflect OTHER entities' state, so their states
    // have to be part of the repaint signature — otherwise flipping one wouldn't redraw
    // the chip until the dispatch sensor happened to update (up to a full plan interval).
    const toggleSig = this._layoutToggles()
      .map((t) => `${t.entity}=${hass.states[t.entity] ? hass.states[t.entity].state : '?'}`)
      .join(',');

    const sig = `${st.last_updated}|${switchSt ? switchSt.last_updated : ''}|${toggleSig}`;
    if (sig !== this._sig) { this._sig = sig; this._paint(); }
  }

  // Configured toggles, normalised and filtered to those naming an entity. Label falls
  // back to the entity's friendly name so a minimal `- entity: switch.x` still reads.
  _layoutToggles() {
    const raw = this._config.layout_toggles;
    if (!Array.isArray(raw)) return [];
    return raw
      .map((t) => (typeof t === 'string' ? { entity: t } : t))
      .filter((t) => t && t.entity);
  }

  _toggleChipsHtml() {
    const toggles = this._layoutToggles();
    if (!toggles.length || !this._hass) return '';
    const chips = toggles.map((t) => {
      const st = this._hass.states[t.entity];
      if (!st) return '';
      const on = String(st.state) === 'on';
      const label = t.label
        || (st.attributes && st.attributes.friendly_name)
        || t.entity;
      return `<button class="chip${on ? ' on' : ''}" type="button"
        data-toggle-entity="${esc(t.entity)}"
        role="switch" aria-checked="${on}"
        title="${on ? 'Showing' : 'Hidden'} — click to toggle">
        <span class="chip-dot"></span>${esc(label)}</button>`;
    }).join('');
    return chips ? `<div class="chips">${chips}</div>` : '';
  }

  _onChipClick(ev) {
    const btn = ev.target && ev.target.closest && ev.target.closest('[data-toggle-entity]');
    if (!btn || !this._hass) return;
    ev.stopPropagation();
    const entity_id = btn.getAttribute('data-toggle-entity');
    // Optimistic paint so the chip responds instantly; the real state arrives via the
    // hass update that follows and re-paints from the entity's actual state.
    btn.classList.toggle('on');
    this._hass.callService('switch', 'toggle', { entity_id });
  }

  // Trajectory slot duration (ms) — the only piece of _timeScale() this card still
  // needs (for execMode()'s implied-grid-charge-power fallback calc).
  _stepMs() {
    const t = this._traj;
    if (!t || t.length < 2) return 1800000;
    return new Date(t[1].start).getTime() - new Date(t[0].start).getTime();
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}
        .hd-right { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
        .chips { display:flex; gap:6px; flex-wrap:wrap; }
        .chip { display:inline-flex; align-items:center; gap:6px; cursor:pointer;
                font:inherit; font-size:11px; font-weight:600; line-height:1;
                padding:4px 9px; border-radius:20px; color:var(--ink2);
                background:transparent; border:1px solid var(--border); }
        .chip:hover { color:var(--ink); }
        .chip:focus-visible { outline:2px solid var(--good); outline-offset:2px; }
        .chip-dot { width:7px; height:7px; border-radius:50%; background:var(--idle);
                    flex:none; }
        .chip.on { color:var(--good);
                   border-color:color-mix(in srgb,var(--good) 40%,transparent); }
        .chip.on .chip-dot { background:var(--good); }
      </style>
      <div class="card"><div class="body"></div></div>
    `;
    // Delegated: _paint() replaces .body's innerHTML on every repaint, so per-chip
    // listeners would be torn off. The listener lives on .body, which survives.
    const body = this.shadowRoot.querySelector('.body');
    if (body) body.addEventListener('click', (ev) => this._onChipClick(ev));
  }

  _paint() {
    const body = this.shadowRoot && this.shadowRoot.querySelector('.body');
    if (!body) return;
    const s = this._summary || {};

    const title = this._config.title ? esc(this._config.title) : 'Battery Plan &amp; SOC Forecast';
    const header = `
      <div class="hd">
        <div>
          <div class="title">${title}</div>
          <div class="sub">${s.plan_name ? esc(s.plan_name) : 'Grid Lens advisory'}${s.solver ? ' · ' + esc(s.solver) : ''}${s.generated_at ? ' · ' + fmtTime(s.generated_at) : ''}</div>
        </div>
        <div class="hd-right">
          ${this._toggleChipsHtml()}
          <div class="badge ${s.restored ? 'stale' : (s.status === 'ok' ? 'ok' : '')}">${s.restored ? 'LAST PLAN' : esc((s.status || 'unknown').toUpperCase())}</div>
        </div>
      </div>`;

    if (this._config.compact) {
      body.innerHTML = header;
      return;
    }

    if (!this._traj || s.status !== 'ok') {
      body.innerHTML = header +
        `<div class="waiting">Advisory plan not available yet${s.reason ? '<br><span class="sub">' + esc(s.reason) + '</span>' : ''}</div>`;
      return;
    }

    const dnames = this._deferNames || [];
    // Matches the Power Flow card's own per-device colour assignment (a hot-water device
    // gets a dedicated colour pulled out of the rotation) — see deferColorFor() in chart-common.js.
    const deferColor = (i) => deferColorFor(dnames, i);

    body.innerHTML = header +
      `<div class="sec"><h4>Control-mode timeline (EMS)</h4>
        <div style="display:flex;gap:16px;flex-wrap:wrap">
          <div style="flex:1;min-width:200px">
            <div style="font-size:11px;color:var(--ink2);margin-bottom:6px">Planned (forecast)</div>
            ${this._modeTimelineHtml()}
          </div>
          <div style="flex:1;min-width:200px">
            <div style="font-size:11px;color:var(--ink2);margin-bottom:6px">Applied (real-time)</div>
            ${this._appliedModeHtml()}
          </div>
        </div>
      </div>
      ${dnames.length ? `
      <div class="sec"><h4>Deferrable loads — recommended on/off</h4>
        <div style="display:flex;gap:16px;flex-wrap:wrap">
          ${dnames.map((nm, i) => `
            <div style="flex:1;min-width:200px">
              <div style="font-size:11px;color:var(--ink2);margin-bottom:6px">${esc(nm)}</div>
              ${this._deferTimelineHtml(i, deferColor(i))}
            </div>`).join('')}
        </div>
      </div>` : ''}
      <div class="note">Advisory only — the battery follows its native EMS, so actual SOC won't track the plan until control is enabled. See the SOC/Power chart cards for the solar/load/price forecast validation. All series are the forecast for the current plan (${s.plan_name ? esc(s.plan_name) : '—'}).</div>`;
  }

  _modeTransitions() {
    const t = this._traj || [];
    const stepMs = this._stepMs();
    const out = [];
    let prev = null;
    for (const row of t) {
      const a = execMode(row, stepMs);
      if (a !== prev) {
        out.push({ ms: new Date(row.start).getTime(), action: a, reason: reasonFor(row, a) });
        prev = a;
      }
    }
    return out;
  }

  _modeTimelineHtml() {
    const trans = this._modeTransitions();
    if (!trans.length) return '<div class="sub">No plan data.</div>';
    const today = new Date();
    const items = trans.map(x =>
      `<li>` +
        `<div class="row"><span class="dot" style="background:${MODE_COLORS[x.action] || 'var(--idle)'}"></span>` +
        `<span class="t">${fmtDayHour(x.ms, today)}</span><span class="arrow">&rarr;</span>` +
        `<span class="m">${esc(modeLabel(x.action))}</span></div>` +
        `<div class="reason">${esc(x.reason)}</div>` +
      `</li>`
    ).join('');
    return `<ul class="modeline">${items}</ul>`;
  }

  // Recommended on/off for deferrable device i in a given trajectory row. Devices like
  // an EV charger or pool pump are physically only ever fully-on or off, but the LP's
  // def_i is a continuous kWh-per-slot variable — a slot can legitimately land on a
  // fractional value (e.g. 0.3 of a 1.8kW max) that has no direct on/off reading. Judge
  // it against the device's own rated power (deferrable_max_kw) the same way execMode()
  // judges a charge/discharge slot against the battery's power_w — >=50% of max counts
  // as "on". Falls back to an absolute 0.05kW floor (matches AdvisoryPlanner's own
  // power_threshold_kw default) if an older sensor payload predates deferrable_max_kw.
  _deferMode(i, row) {
    const kwScale = 3600000 / this._stepMs();
    const kw = (+row[`defer_${i}`] || 0) * kwScale;
    const maxKw = (this._deferMaxKw && this._deferMaxKw[i]) || 0;
    return maxKw > 0 ? (kw >= 0.5 * maxKw ? 'on' : 'off') : (kw >= 0.05 ? 'on' : 'off');
  }

  _deferTransitions(i) {
    const t = this._traj || [];
    const out = [];
    let prev = null;
    for (const row of t) {
      const m = this._deferMode(i, row);
      if (m !== prev) { out.push({ ms: new Date(row.start).getTime(), mode: m }); prev = m; }
    }
    return out;
  }

  _deferTimelineHtml(i, color) {
    const trans = this._deferTransitions(i);
    if (!trans.length) return '<div class="sub">No plan data.</div>';
    const today = new Date();
    const items = trans.map(x =>
      `<li><div class="row">` +
        `<span class="dot" style="background:${x.mode === 'on' ? color : 'var(--idle)'}"></span>` +
        `<span class="t">${fmtDayHour(x.ms, today)}</span><span class="arrow">&rarr;</span>` +
        `<span class="m">${x.mode === 'on' ? 'Recommended ON' : 'Off'}</span>` +
      `</div></li>`
    ).join('');
    return `<ul class="modeline">${items}</ul>`;
  }

  _appliedModeHtml() {
    if (!this._applied || !this._applied.action) {
      return '<div class="sub">Not yet applied.</div>';
    }
    const a = this._applied.action;
    const power = this._applied.power_w ? ` · ${Math.round(this._applied.power_w)} W` : '';
    const time = this._applied.at ? fmtTime(this._applied.at) : '—';
    return `
      <ul class="modeline">
        <li>
          <span class="dot" style="background:${MODE_COLORS[a] || 'var(--idle)'}"></span>
          <span class="m">${esc(modeLabel(a))}</span>
          <span class="t" style="margin-left:auto;text-align:right">${time}${power}</span>
        </li>
      </ul>
      <div class="note" style="margin-top:4px">Executor's real-time command — confirms control is active and following the plan.</div>
    `;
  }
}

customElements.define('grid-lens-advisory-card', GridLensAdvisoryCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-advisory-card',
  name: 'Grid Lens Advisory',
  description: 'Battery plan status, control-mode timeline, and deferrable-load recommendations.',
});
