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
 */
import {
  STYLE, esc, fmtTime, fmtDayHour, modeLabel, MODE_COLORS, execMode, reasonFor, deferColorFor,
} from './grid-lens-chart-common.js?v=20260802b';

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

  getCardSize() { return 3; }

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

    const sig = `${st.last_updated}|${switchSt ? switchSt.last_updated : ''}`;
    if (sig !== this._sig) { this._sig = sig; this._paint(); }
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
      <style>${STYLE}</style>
      <div class="card"><div class="body"></div></div>
    `;
  }

  _paint() {
    const body = this.shadowRoot && this.shadowRoot.querySelector('.body');
    if (!body) return;
    const s = this._summary || {};

    const header = `
      <div class="hd">
        <div>
          <div class="title">Battery Plan &amp; SOC Forecast</div>
          <div class="sub">${s.plan_name ? esc(s.plan_name) : 'Grid Lens advisory'}${s.solver ? ' · ' + esc(s.solver) : ''}${s.generated_at ? ' · ' + fmtTime(s.generated_at) : ''}</div>
        </div>
        <div class="badge ${s.restored ? 'stale' : (s.status === 'ok' ? 'ok' : '')}">${s.restored ? 'LAST PLAN' : esc((s.status || 'unknown').toUpperCase())}</div>
      </div>`;

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
