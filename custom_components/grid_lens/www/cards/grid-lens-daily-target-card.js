/*
 * Grid Lens Daily Target Card (standalone)
 *
 * Master + per-device percent-of-average daily-kWh targets for deferrable loads,
 * plus today/tomorrow solar forecast — see FEATURES.md §9b for the full story
 * and grid-lens-chart-common.js's "Daily Target helpers" section for the shared
 * logic this card is built from.
 *
 * Relocated 2026-09-22: the primary, default-visible home for this UI is now
 * grid-lens-advisory-card.js's compact header (top of the Power Flow page —
 * solar forecast + master slider always visible, per-device sliders behind an
 * expander) — see that file. This standalone card is no longer seeded onto the
 * default Settings view, but stays registered/installed for anyone who wants
 * it as its own card on a different dashboard. Both cards import the SAME
 * resolver/computation functions from grid-lens-chart-common.js rather than
 * keeping two copies that could drift apart (the exact mistake HOT_WATER_RE/
 * deferColorFor were centralised to avoid, repeated once already with the
 * Today Boost/charge-target entity-resolution logic before that).
 *
 * Config:
 *   type: custom:grid-lens-daily-target-card
 *   title: Daily Targets                         (optional)
 *   solar_forecast_entity: sensor.xyz_tomorrow    (optional override)
 */
import {
  STYLE, esc, resolveDeferrableLoads, resolveDailyTargetMasterEid, resolveDailyTargetEidFor,
  resolveBoostEidFor, resolveChargeTargetPercentEidFor, resolveSolarForecastEid, solarSummary,
  fmtKwh, clampTargetPct, fetchDailyAverageKwh,
} from './grid-lens-chart-common.js?v=20260923b';

const HISTORY_REFRESH_MS = 15 * 60000;

class GridLensDailyTargetCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._sig = '';
    this._dark = false;
    // Per-device daily-kWh history (14-day average, same source Today Boost's own
    // sparkline uses), keyed by energy_entity — fetched lazily and cached, since
    // it's a stats query and not part of live hass state.
    this._historyCache = {};
    this._historyPending = new Set();
  }

  setConfig(config) {
    this._config = Object.assign({ title: 'Daily Targets' }, config);
    this._renderShell();
  }

  getCardSize() { return Math.max(3, (this._rows || []).length + 2); }

  _pollHistory(devices) {
    for (const d of devices) {
      const eid = d.energy_entity;
      if (!eid || this._historyPending.has(eid)) continue;
      const cached = this._historyCache[eid];
      if (cached && Date.now() - cached.ts < HISTORY_REFRESH_MS) continue;
      this._historyPending.add(eid);
      fetchDailyAverageKwh(this._hass, eid).then((avgKwh) => {
        this._historyPending.delete(eid);
        this._historyCache[eid] = { ts: Date.now(), avgKwh };
        this._paint();
      });
    }
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this.classList.toggle('dark', dark); }

    const devices = resolveDeferrableLoads(hass);
    this._pollHistory(devices);
    const masterEid = resolveDailyTargetMasterEid(hass);
    const rows = devices.map((d) => ({
      device: d,
      targetEid: resolveDailyTargetEidFor(hass, d.energy_entity),
      boostEid: resolveBoostEidFor(hass, d.energy_entity),
      ctEid: resolveChargeTargetPercentEidFor(hass, d.energy_entity),
    }));
    const solarEid = resolveSolarForecastEid(hass, this._config.solar_forecast_entity);
    const solarSt = solarEid && hass.states[solarEid];

    const sig = [
      masterEid && hass.states[masterEid] ? hass.states[masterEid].state : '',
      rows.map((r) => {
        const t = r.targetEid && hass.states[r.targetEid];
        const b = r.boostEid && hass.states[r.boostEid];
        const c = r.ctEid && hass.states[r.ctEid];
        return [
          r.device.energy_entity,
          t ? `${t.state}|${(t.attributes || {}).is_override}` : '',
          b ? b.state : '',
          c ? c.state : '',
        ].join('~');
      }).join(','),
      // Minute-granularity tick so the "today remaining" forecast keeps drifting
      // downward through the day even between solar-sensor updates.
      solarSt ? `${solarSt.state}@${solarSt.last_updated || ''}@${Math.floor(Date.now() / 60000)}` : '',
    ].join('#');

    if (sig !== this._sig) {
      this._sig = sig;
      this._masterEidCache = masterEid;
      this._solarEidCache = solarEid;
      this._rows = rows;
      this._paint();
    }
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}
        /* Solar boxes + master slider all in one row (user request 2026-09-22) — solar
           boxes don't grow (flex:0 1 …) so the master slider (flex:1 1 …) takes the
           remaining width. flex-wrap stays as a narrow-viewport fallback only. */
        .top-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; flex-wrap: wrap;
                   padding-bottom: 10px; border-bottom: 2px solid var(--border); }
        .solar-box { flex: 0 1 130px; display: flex; align-items: center; gap: 8px;
                     border: 1px solid var(--border); border-radius: 9px; padding: 8px 10px; }
        .solar-box ha-icon { --mdc-icon-size: 22px; color: var(--good); flex: 0 0 auto; }
        .solar-box.dim ha-icon { color: var(--ink2); opacity: .5; }
        .solar-box .solar-label { font-size: 10.5px; color: var(--ink2); text-transform: uppercase;
                                   letter-spacing: .02em; }
        .solar-box .solar-value { font-size: 14px; font-weight: 650; color: var(--ink); }
        .solar-box .solar-cond { font-size: 11px; color: var(--ink2); }
        .master-inline { display: flex; align-items: center; gap: 7px; flex: 1 1 160px; min-width: 140px; }
        .master-inline input[type=range] { flex: 1 1 auto; accent-color: var(--good); }
        .master-inline .name { font-size: 13px; font-weight: 650; color: var(--ink); flex: 0 0 auto; white-space: nowrap; }
        .rows { display: flex; flex-direction: column; gap: 2px; }
        .row { display: flex; align-items: center; gap: 10px; padding: 9px 2px;
               border-bottom: 1px solid var(--border); flex-wrap: wrap; }
        .row:last-child { border-bottom: none; }
        .row .info { flex: 1 1 120px; min-width: 0; }
        .row .name { font-size: 13.5px; font-weight: 550; color: var(--ink); }
        .row .meta { font-size: 11px; color: var(--ink2); margin-top: 1px; }
        .row .meta.note { color: var(--buy); }
        .slider-wrap { display: flex; align-items: center; gap: 8px; flex: 1 1 160px; min-width: 140px; }
        .slider-wrap input[type=range] { flex: 1 1 auto; accent-color: var(--good); }
        .pct { font-size: 13px; font-weight: 600; color: var(--ink); width: 42px; text-align: right;
               font-variant-numeric: tabular-nums; }
        .reset-btn { font-size: 10.5px; font-weight: 600; padding: 4px 8px; border-radius: 7px;
               border: 1px solid var(--border); background: transparent; color: var(--ink2);
               cursor: pointer; font-family: inherit; flex: 0 0 auto; }
        .reset-btn:hover { border-color: var(--good); color: var(--good); }
        .empty { padding: 14px 2px; font-size: 12.5px; color: var(--ink2); }
      </style>
      <div class="card">
        <div class="hd">
          <div class="title">${esc(this._config.title)}</div>
        </div>
        <div class="top-row">
          <div class="solar-box" data-solar-box="today">
            <ha-icon></ha-icon>
            <div>
              <div class="solar-label">Today (remaining)</div>
              <div class="solar-value"></div>
              <div class="solar-cond"></div>
            </div>
          </div>
          <div class="solar-box" data-solar-box="tomorrow">
            <ha-icon></ha-icon>
            <div>
              <div class="solar-label">Tomorrow</div>
              <div class="solar-value"></div>
              <div class="solar-cond"></div>
            </div>
          </div>
          <div class="master-wrap"></div>
        </div>
        <div class="rows"></div>
      </div>
    `;
  }

  _sliderRowHtml(eid, pct, kwhReadout, extraClass = '') {
    return `
      <div class="slider-wrap ${extraClass}">
        <input type="range" min="0" max="300" step="5" value="${pct}" data-eid="${eid}">
        <span class="pct" data-pct-for="${eid}">${pct.toFixed(0)}%</span>
      </div>
      <div class="meta" data-kwh-for="${eid}">${kwhReadout}</div>
    `;
  }

  _paintSolarBox(which, kwh, weather) {
    const box = this.shadowRoot.querySelector(`[data-solar-box="${which}"]`);
    if (!box) return;
    box.classList.toggle('dim', !!weather.dim);
    box.querySelector('ha-icon').setAttribute('icon', weather.icon);
    box.querySelector('.solar-value').textContent = kwh == null ? '–' : fmtKwh(kwh);
    box.querySelector('.solar-cond').textContent = weather.label;
  }

  _paint() {
    if (!this.shadowRoot.querySelector('.card')) this._renderShell();
    const solar = solarSummary(this._hass, this._config.solar_forecast_entity);
    this._paintSolarBox('today', solar.todayKwh, solar.todayWeather);
    this._paintSolarBox('tomorrow', solar.tomorrowKwh, solar.tomorrowWeather);

    const hass = this._hass;
    const masterEid = this._masterEidCache;
    const masterSt = masterEid && hass.states[masterEid];
    const masterPct = masterSt ? clampTargetPct(masterSt.state) : 100;
    const masterWrap = this.shadowRoot.querySelector('.master-wrap');
    masterWrap.innerHTML = masterEid ? `
      <div class="master-inline">
        <span class="name">Daily Target</span>
        <input type="range" min="0" max="300" step="5" value="${masterPct}" data-eid="${masterEid}">
        <span class="pct" data-pct-for="${masterEid}">${masterPct.toFixed(0)}%</span>
      </div>
    ` : '';

    const body = this.shadowRoot.querySelector('.rows');
    const rows = this._rows || [];
    if (!rows.length) {
      body.innerHTML = '<div class="empty">No deferrable loads configured yet.</div>';
      return;
    }

    body.innerHTML = rows.map((r) => {
      const d = r.device;
      const targetSt = r.targetEid && hass.states[r.targetEid];
      const isOverride = !!(targetSt && (targetSt.attributes || {}).is_override);
      const pct = targetSt ? clampTargetPct(targetSt.state) : masterPct;
      const avgKwh = this._historyCache[d.energy_entity] ? this._historyCache[d.energy_entity].avgKwh : null;
      const effectiveKwh = avgKwh == null ? null : avgKwh * pct / 100.0;
      const kwhReadout = avgKwh == null
        ? 'history loading…'
        : `≈ ${fmtKwh(effectiveKwh)} at this rate (avg ${fmtKwh(avgKwh)})`;

      const boostSt = r.boostEid && hass.states[r.boostEid];
      const boostActive = boostSt && parseFloat(boostSt.state) > 0;
      const ctSt = r.ctEid && hass.states[r.ctEid];
      const ctActive = ctSt && parseFloat(ctSt.state) > 0;
      let note = '';
      if (ctActive) note = 'Charge target active — overrides this until reached/expired';
      else if (boostActive) note = `Today Boost active (${fmtKwh(parseFloat(boostSt.state))}) — overrides this today`;

      const resetBtn = r.targetEid && isOverride
        ? `<button class="reset-btn" data-reset-for="${d.energy_entity}">Follow master</button>` : '';

      if (!r.targetEid) {
        return `
          <div class="row">
            <div class="info"><div class="name">${esc(d.name)}</div>
              <div class="meta">Daily Target entity not found for this device</div></div>
          </div>`;
      }

      return `
        <div class="row">
          <div class="info">
            <div class="name">${esc(d.name)}</div>
            ${note ? `<div class="meta note">${esc(note)}</div>` : ''}
          </div>
          ${this._sliderRowHtml(r.targetEid, pct, kwhReadout, isOverride ? 'is-override' : '')}
          ${resetBtn}
        </div>
      `;
    }).join('');

    this._attachListeners();
  }

  _attachListeners() {
    const root = this.shadowRoot;
    root.querySelectorAll('input[type=range][data-eid]').forEach((el) => {
      const eid = el.getAttribute('data-eid');
      const readout = root.querySelector(`[data-pct-for="${eid}"]`);
      el.addEventListener('input', () => {
        if (readout) readout.textContent = `${parseFloat(el.value).toFixed(0)}%`;
      });
      el.addEventListener('change', () => {
        this._hass.callService('number', 'set_value', { entity_id: eid, value: clampTargetPct(el.value) });
      });
    });
    root.querySelectorAll('[data-reset-for]').forEach((el) => {
      el.addEventListener('click', () => {
        const sensorId = el.getAttribute('data-reset-for');
        this._hass.callService('grid_lens', 'clear_daily_target', { sensor_id: sensorId });
      });
    });
  }
}

customElements.define('grid-lens-daily-target-card', GridLensDailyTargetCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-daily-target-card',
  name: 'Grid Lens Daily Target',
  description: 'Master + per-device percent-of-average targets for deferrable loads, with today/tomorrow solar forecast.',
});
