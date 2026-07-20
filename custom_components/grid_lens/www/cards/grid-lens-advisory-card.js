/*
 * Grid Lens Advisory Card
 * Visualises sensor.<...>_planned_dispatch: predicted vs actual SOC, and the
 * planned battery dispatch timeline. Read-only (advisory mode).
 *
 * Config:
 *   type: custom:grid-lens-advisory-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch   (required)
 *   soc_entity: sensor.sigen_plant_ess_soc               (actual SOC, optional)
 *
 * Self-contained: inline SVG, validated categorical palette (blue=predicted,
 * orange=actual), theme-aware (light/dark), legend + direct labels + hover.
 */
class GridLensAdvisoryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._traj = null;        // trajectory array from the sensor
    this._summary = {};       // status/solver/soc/cost/etc.
    this._actual = [];        // [{t: Date, v: soc%}] measured SOC history
    this._lastFetch = 0;
    this._sig = '';           // render signature to avoid thrashing
    this._gc = 0;             // gradient-id counter (unique per render)
    this._actualEnergy = { solar: [], load: [], buy: [], sell: [] };
    this._deferNames = [];    // deferrable device names, one series each
    this._viewMode = 'today'; // 'today' = today only; 'horizon' = full 36h
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error('Define "entity" (the planned_dispatch sensor)');
    this._config = Object.assign({
      soc_entity: 'sensor.sigen_plant_ess_soc',
      solar_power_entity: 'sensor.sigen_0_total_pv_power',           // kW
      load_power_entity: 'sensor.sigen_plant_general_load_power',    // kW
      grid_power_entity: 'sensor.sigen_plant_grid_sensor_active_power', // kW, >0 import / <0 export
      control_switch_entity: 'switch.roof_grid_lens_nsw_battery_control', // executor status (optional)
    }, config);
    this._sig = '';
    this._applied = null;  // real-time applied action from executor
    this._renderShell();
  }

  getCardSize() { return 8; }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config.entity];
    if (!st) { this._summary = { status: 'unknown' }; this._traj = null; this._paint(); return; }

    const a = st.attributes || {};
    this._traj = Array.isArray(a.trajectory) ? a.trajectory : null;
    this._deferNames = Array.isArray(a.deferrable_names) ? a.deferrable_names : [];

    // Read real-time applied action from the control switch (executor status)
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
      next_action: st.state,
      next_power_w: a.next_power_w,
      plan_name: a.plan_name,
      solver: a.solver,
      generated_at: a.generated_at,
      initial_soc: a.initial_soc_percent,
      final_soc: a.final_soc_percent,
      net_cost: a.net_cost,
      reason: a.reason,
      restored: a.restored === true,
    };

    // Current measured SOC as the newest actual point.
    const socSt = hass.states[this._config.soc_entity];
    const curSoc = socSt ? parseFloat(socSt.state) : NaN;

    // Throttled history fetch for the actual-SOC overlay (once a minute).
    const now = Date.now();
    if (this._traj && (now - this._lastFetch > 60000)) {
      this._lastFetch = now;
      this._fetchActual(hass, curSoc).catch(() => {});
    } else if (!isNaN(curSoc)) {
      this._mergeNow(curSoc);
    }

    // Re-render only when something meaningful changed.
    const sig = `${st.last_updated}|${curSoc}|${this._actual.length}`;
    if (sig !== this._sig) { this._sig = sig; this._paint(); }
  }

  async _fetchActual(hass, curSoc) {
    try {
      if (!this._traj || !this._traj.length) return;
      // Fetch history from the appropriate start time based on view mode
      let start;
      if (this._viewMode === 'today') {
        // In today mode, fetch from midnight (start of today)
        const now = new Date();
        start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      } else {
        // In horizon mode, fetch from 2h before plan start
        start = new Date(new Date(this._traj[0].start).getTime() - VIEW_BACK_MS);
      }
      const end = new Date();
      const c = this._config;
      const eids = [c.soc_entity, c.solar_power_entity, c.load_power_entity, c.grid_power_entity]
        .filter(Boolean).join(',');
      const url = `history/period/${start.toISOString()}?filter_entity_id=${eids}` +
                  `&end_time=${encodeURIComponent(end.toISOString())}&minimal_response&significant_changes_only`;
      const res = await hass.callApi('GET', url);
      const byId = {};
      for (const arr of (res || [])) {
        if (arr && arr.length && arr[0].entity_id) byId[arr[0].entity_id] = arr;
      }
      // Measured SOC (%)
      const soc = this._series(byId[c.soc_entity]);
      if (!isNaN(curSoc)) soc.push({ t: end, v: curSoc });
      this._actual = ds(soc);
      // Measured power (kW) — matches the forecast, which is plotted as average kW
      // per interval. Sensors may report W or kW (e.g. PV is W, load is kW) — normalise.
      const toKw = (eid) => {
        const st = hass.states[eid];
        return (st && st.attributes && st.attributes.unit_of_measurement === 'W') ? 0.001 : 1;
      };
      const fS = toKw(c.solar_power_entity), fL = toKw(c.load_power_entity), fG = toKw(c.grid_power_entity);
      const grid = this._series(byId[c.grid_power_entity]);  // >0 import / <0 export
      this._actualEnergy = {
        solar: ds(this._series(byId[c.solar_power_entity]).map(p => ({ t: p.t, v: Math.max(0, p.v * fS) }))),
        load: ds(this._series(byId[c.load_power_entity]).map(p => ({ t: p.t, v: Math.max(0, p.v * fL) }))),
        buy: ds(grid.map(p => ({ t: p.t, v: Math.max(0, p.v * fG) }))),
        sell: ds(grid.map(p => ({ t: p.t, v: Math.max(0, -p.v * fG) }))),
      };
      this._sig = '';           // force repaint
      this._paint();
    } catch (e) { /* history unavailable — forecast-only is fine */ }
  }

  _series(rows) {
    const pts = [];
    for (const r of (rows || [])) {
      const v = parseFloat(r.state);
      if (!isNaN(v)) pts.push({ t: new Date(r.last_changed || r.lu), v });
    }
    return pts;
  }

  _mergeNow(curSoc) {
    const end = new Date();
    if (this._actual.length && (end - this._actual[this._actual.length - 1].t) < 60000) return;
    this._actual = this._actual.concat([{ t: end, v: curSoc }]);
  }

  /* ------------------------------------------------------------------ styles */
  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e;
          --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
          --predicted:#2563eb; --actual:#ea580c; --charge:#2563eb; --discharge:#ea580c;
          --idle:#94918a; --fit:rgba(245,158,11,.20); --good:#059669;
          --solar:#f59e0b; --load:#7c3aed; --buy:#e11d48; --sell:#0d9488; --cum:#2563eb;
          --defer1:#db2777; --defer2:#0891b2; --defer3:#65a30d; --defer4:#c2410c;
        }
        @media (prefers-color-scheme: dark) {
          :host {
            --surface:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink2:#c3c2b7;
            --muted:#898781; --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
            --predicted:#60a5fa; --actual:#fb923c; --charge:#60a5fa; --discharge:#fb923c;
            --fit:rgba(251,191,36,.22); --good:#10b981;
            --solar:#fbbf24; --load:#a78bfa; --buy:#fb7185; --sell:#2dd4bf; --cum:#60a5fa;
            --defer1:#f472b6; --defer2:#22d3ee; --defer3:#a3e635; --defer4:#fb923c;
          }
        }
        .card { background:var(--surface); border:1px solid var(--border); border-radius:14px;
                padding:16px 18px; font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
                color:var(--ink); }
        .hd { display:flex; align-items:baseline; justify-content:space-between; gap:10px; flex-wrap:wrap; }
        .title { font-size:15px; font-weight:650; letter-spacing:.2px; }
        .sub { font-size:12px; color:var(--ink2); }
        .badge { font-size:11px; font-weight:650; padding:2px 8px; border-radius:20px;
                 border:1px solid var(--border); color:var(--ink2); }
        .badge.ok { color:var(--good); border-color:color-mix(in srgb,var(--good) 40%,transparent); }
        .badge.stale { color:var(--solar); border-color:color-mix(in srgb,var(--solar) 45%,transparent); }
        .tiles { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin:14px 0 6px; }
        .tile { background:var(--plane); border:1px solid var(--border); border-radius:10px; padding:9px 11px; }
        .tile .k { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.4px; }
        .tile .v { font-size:19px; font-weight:650; margin-top:2px; }
        .tile .v small { font-size:12px; font-weight:500; color:var(--ink2); }
        .sec { margin-top:14px; }
        .sec h4 { margin:0 0 4px; font-size:12px; font-weight:600; color:var(--ink2); }
        .legend { display:flex; gap:14px; font-size:11px; color:var(--ink2); margin:2px 0 6px; flex-wrap:wrap; }
        .legend i { display:inline-block; width:16px; height:0; vertical-align:middle; margin-right:5px; }
        .swatch { display:inline-block; width:10px; height:10px; border-radius:2px; vertical-align:middle; margin-right:5px; }
        svg { width:100%; height:auto; display:block; overflow:visible; }
        .charts { position:relative; }
        .chart-svg { cursor:crosshair; }
        .xtip { position:absolute; pointer-events:none; background:var(--surface); color:var(--ink);
                border:1px solid var(--border); border-radius:9px; padding:7px 10px; font-size:11px;
                line-height:1.55; box-shadow:0 6px 18px rgba(0,0,0,.24); white-space:nowrap;
                opacity:0; transition:opacity .08s; z-index:6; }
        .xtip .k { color:var(--muted); }
        .modeline { list-style:none; margin:6px 0 0; padding:0; display:flex; flex-direction:column; gap:5px; }
        .modeline li { display:flex; align-items:baseline; gap:7px; font-size:12.5px; color:var(--ink); }
        .modeline .dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto; align-self:center; }
        .modeline .t { font-variant-numeric:tabular-nums; font-weight:650; color:var(--ink2); min-width:38px; }
        .modeline .arrow { color:var(--muted); }
        .modeline .m { font-weight:550; }
        .note { font-size:11px; color:var(--muted); margin-top:8px; line-height:1.4; }
        .waiting { padding:26px 8px; text-align:center; color:var(--ink2); font-size:13px; }
        .view-btn { transition:all 0.15s ease; font-weight:600; cursor:pointer; }
        .view-btn:hover { background:rgba(255,255,255,0.08); }
        .view-btn.active { background:var(--surface); color:var(--good); }
      </style>
      <div class="card"><div class="body"></div></div>
    `;
  }

  /* ------------------------------------------------------------------ paint */
  _paint() {
    const body = this.shadowRoot && this.shadowRoot.querySelector('.body');
    if (!body) return;
    const s = this._summary || {};
    this._gc = 0;  // reset gradient ids each render

    const header = `
      <div class="hd">
        <div>
          <div class="title">Battery Plan &amp; SOC Forecast</div>
          <div class="sub">${s.plan_name ? esc(s.plan_name) : 'Grid Lens advisory'}${s.solver ? ' · ' + esc(s.solver) : ''}${s.generated_at ? ' · ' + fmtTime(s.generated_at) : ''}</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <div style="display:flex;gap:4px;padding:2px;background:var(--plane);border:1px solid var(--border);border-radius:6px">
            <button id="toggle-today" class="view-btn ${this._viewMode === 'today' ? 'active' : ''}" style="padding:4px 10px;border:none;background:transparent;color:var(--ink);cursor:pointer;font-size:11px;border-radius:4px">Today</button>
            <button id="toggle-horizon" class="view-btn ${this._viewMode === 'horizon' ? 'active' : ''}" style="padding:4px 10px;border:none;background:transparent;color:var(--ink);cursor:pointer;font-size:11px;border-radius:4px">Full horizon</button>
          </div>
          <div class="badge ${s.restored ? 'stale' : (s.status === 'ok' ? 'ok' : '')}">${s.restored ? 'LAST PLAN' : esc((s.status || 'unknown').toUpperCase())}</div>
        </div>
      </div>`;

    if (!this._traj || s.status !== 'ok') {
      body.innerHTML = header +
        `<div class="waiting">Advisory plan not available yet${s.reason ? '<br><span class="sub">' + esc(s.reason) + '</span>' : ''}</div>`;
      return;
    }

    const tiles = `
      <div class="tiles">
        <div class="tile"><div class="k">Now</div><div class="v">${actionLabel(s.next_action)} <small>${s.next_power_w ? (Math.round(s.next_power_w) + ' W') : ''}</small></div></div>
        <div class="tile"><div class="k">SOC now</div><div class="v">${fmtPct(s.initial_soc)}</div></div>
        <div class="tile"><div class="k">Planned end</div><div class="v">${fmtPct(s.final_soc)}</div></div>
        <div class="tile"><div class="k">Plan net cost</div><div class="v">${fmtMoney(s.net_cost)}</div></div>
      </div>`;

    // One dashed series per deferrable device (colour cycles through --defer1..4).
    const deferColor = (i) => `var(--defer${(i % 4) + 1})`;
    const dnames = this._deferNames || [];
    const deferLegend = dnames.map((nm, i) =>
      `<span><i style="border-top:2px dashed ${deferColor(i)}"></i>${esc(nm)}</span>`).join('');
    // Forecast rows carry kWh-per-interval; divide by the interval length (hours) to
    // plot average kW, matching the measured overlays and the instantaneous power feel.
    const kwScale = 3600000 / this._timeScale().step;
    const energySeries = [
      { key: 'solar_kwh', color: 'var(--solar)', area: true, scale: kwScale },
      { key: 'load_kwh', color: 'var(--load)', scale: kwScale },
      { key: 'buy_kwh', color: 'var(--buy)', scale: kwScale },
      { key: 'sell_kwh', color: 'var(--sell)', scale: kwScale },
      ...dnames.map((nm, i) => ({ key: `defer_${i}`, color: deferColor(i), dash: true, scale: kwScale })),
      { points: this._actualEnergy.solar, color: 'var(--solar)', actual: true },
      { points: this._actualEnergy.load, color: 'var(--load)', actual: true },
      { points: this._actualEnergy.buy, color: 'var(--buy)', actual: true },
      { points: this._actualEnergy.sell, color: 'var(--sell)', actual: true },
    ];

    body.innerHTML = header + tiles +
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
      <div class="charts"><div class="xtip"></div>
      <div class="sec"><h4>SOC — planned vs measured</h4>
        <div class="legend">
          <span><i style="border-top:2px dashed var(--predicted)"></i>Planned (if controlled)</span>
          <span><i style="border-top:2px solid var(--actual)"></i>Measured (native EMS)</span>
          <span style="color:var(--muted)">— divergence is expected until control is enabled</span>
        </div>
        ${this._socSvg()}
      </div>
      <div class="sec"><h4>Planned dispatch</h4>
        <div class="legend">
          <span><span class="swatch" style="background:var(--charge)"></span>Charge</span>
          <span><span class="swatch" style="background:var(--discharge)"></span>Discharge</span>
          <span><span class="swatch" style="background:var(--idle)"></span>Self-use</span>
          <span><span class="swatch" style="background:var(--fit)"></span>Export window</span>
        </div>
        ${this._dispatchSvg()}
      </div>
      <div class="sec"><h4>Power — measured &amp; forecast (kW)</h4>
        <div class="legend">
          <span><span class="swatch" style="background:var(--solar)"></span>Solar</span>
          <span><span class="swatch" style="background:var(--load)"></span>Load</span>
          <span><span class="swatch" style="background:var(--buy)"></span>Buy (import)</span>
          <span><span class="swatch" style="background:var(--sell)"></span>Sell (export)</span>
          ${deferLegend}
          <span style="color:var(--muted)">— thin line left of "now" = measured</span>
        </div>
        ${this._multiLine(energySeries, { fmt: (v) => v.toFixed(1) })}
      </div>
      <div class="sec"><h4>Price ($/kWh)</h4>
        <div class="legend">
          <span><i style="border-top:2px solid var(--buy)"></i>Buy rate</span>
          <span><i style="border-top:2px solid var(--sell)"></i>Sell rate</span>
        </div>
        ${this._multiLine([
          { key: 'import_rate', color: 'var(--buy)', step: true },
          { key: 'export_rate', color: 'var(--sell)', step: true },
        ], { fmt: (v) => v.toFixed(2) })}
      </div>
      <div class="sec"><h4>Cumulative cost / profit ($)</h4>
        <div class="legend"><span><i style="border-top:2px solid var(--cum)"></i>Running net cost — below zero = ahead</span></div>
        ${this._cashSvg()}
      </div>
      </div>
      <div class="note">Advisory only — the battery follows its native EMS, so actual SOC won't track the plan until control is enabled. What's validated now is the solar/load/price forecasting. All series are the forecast for the current plan (${s.plan_name ? esc(s.plan_name) : '—'}).</div>`;

    this._wireCrosshair();
    this._wireViewToggle();
  }

  _wireViewToggle() {
    const todayBtn = this.shadowRoot.querySelector('#toggle-today');
    const horizonBtn = this.shadowRoot.querySelector('#toggle-horizon');
    if (!todayBtn || !horizonBtn) return;
    todayBtn.addEventListener('click', () => {
      if (this._viewMode !== 'today') {
        this._viewMode = 'today';
        this._lastFetch = 0;  // force history re-fetch with new time range
        this._sig = '';  // force re-render
        if (this._hass) {
          this._fetchActual(this._hass, parseFloat(this._hass.states[this._config.soc_entity]?.state || 'NaN')).catch(() => {});
        }
        this._paint();
      }
    });
    horizonBtn.addEventListener('click', () => {
      if (this._viewMode !== 'horizon') {
        this._viewMode = 'horizon';
        this._lastFetch = 0;  // force history re-fetch with new time range
        this._sig = '';  // force re-render
        if (this._hass) {
          this._fetchActual(this._hass, parseFloat(this._hass.states[this._config.soc_entity]?.state || 'NaN')).catch(() => {});
        }
        this._paint();
      }
    });
  }

  _geom() {
    return { w: GW, h: 210, ml: GML, mr: GMR, mt: 10, mb: 22 };
  }

  _timeScale() {
    const t = this._traj;
    const planStart = new Date(t[0].start).getTime();
    const step = t.length > 1 ? (new Date(t[1].start).getTime() - planStart) : 1800000;
    let t1 = new Date(t[t.length - 1].start).getTime() + step;

    let t0;
    if (this._viewMode === 'today') {
      // Show from midnight (today's start) to end of today
      const now = new Date();
      const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
      t0 = midnight;
      // Clamp t1 to end of today (11:59:59 PM)
      const endOfDay = midnight + 24 * 3600000;
      t1 = Math.min(t1, endOfDay);
    } else {
      // Full horizon mode: 2h of history + full plan ahead
      t0 = planStart - VIEW_BACK_MS;
    }
    return { t0, t1, step, planStart };
  }

  // viewBox-x for a timestamp — identical across every chart (shared GML/GMR/GW),
  // so a crosshair set to this x lines up in all of them.
  _xOf(ms) {
    const { t0, t1 } = this._timeScale();
    return GML + (ms - t0) / (t1 - t0) * (GW - GML - GMR);
  }

  _socSvg() {
    const g = this._geom(); const { t0, t1 } = this._timeScale();
    const X = (ms) => g.ml + (ms - t0) / (t1 - t0) * (g.w - g.ml - g.mr);
    const Y = (v) => g.mt + (1 - v / 100) * (g.h - g.mt - g.mb);

    // gridlines + y labels
    let grid = '';
    [0, 25, 50, 75, 100].forEach(v => {
      const y = Y(v);
      grid += `<line x1="${g.ml}" y1="${y}" x2="${g.w - g.mr}" y2="${y}" stroke="var(--grid)" stroke-width="1"/>`;
      grid += `<text x="${g.ml - 6}" y="${y + 3}" text-anchor="end" font-size="10" fill="var(--muted)">${v}</text>`;
    });
    // x ticks every 3h on local clock boundaries
    const xticks = xAxisTicks(X, t0, t1, g.h - g.mb);

    // predicted line (smooth, dashed) with gradient area fill under it
    const predPts = this._traj.map(s => [X(new Date(s.start).getTime()), Y(clampPct(s.soc_percent))]);
    const predD = smoothPath(predPts);
    const gid = 'soc' + (this._gc++);
    const base = g.h - g.mb;
    const predFill = gradDef(gid, 'var(--predicted)', 0.42)
      + `<path d="${predD} L${predPts[predPts.length - 1][0].toFixed(1)},${base} L${predPts[0][0].toFixed(1)},${base} Z" fill="url(#${gid})"/>`;
    const pred = `<path d="${predD}" fill="none" stroke="var(--predicted)" stroke-width="2.5" stroke-dasharray="5 4" stroke-linejoin="round" stroke-linecap="round"/>`;

    // actual line (measured history within window) — smooth, solid, thicker
    let actual = '';
    const ap = (this._actual || []).filter(p => p.t.getTime() >= t0 && p.t.getTime() <= Date.now() + 60000);
    if (ap.length > 1) {
      const pts = ap.map(p => [X(p.t.getTime()), Y(clampPct(p.v))]);
      actual = `<path d="${smoothPath(pts)}" fill="none" stroke="var(--actual)" stroke-width="2.75" stroke-linecap="round" stroke-linejoin="round"/>`;
    } else if (ap.length === 1) {
      actual = `<circle cx="${X(ap[0].t.getTime())}" cy="${Y(clampPct(ap[0].v))}" r="4" fill="var(--actual)"/>`;
    }

    // "now" marker
    const nowX = X(Math.min(Date.now(), t1));
    const nowLine = `<line x1="${nowX}" y1="${g.mt}" x2="${nowX}" y2="${g.h - g.mb}" stroke="var(--axis)" stroke-width="1" stroke-dasharray="2 3"/>` +
      `<text x="${nowX}" y="${g.mt - 1}" text-anchor="middle" font-size="9" fill="var(--muted)">now</text>`;

    return `<svg viewBox="0 0 ${g.w} ${g.h}" class="chart-svg" role="img" aria-label="Predicted versus actual state of charge">
      ${grid}${xticks}
      <line x1="${g.ml}" y1="${g.h - g.mb}" x2="${g.w - g.mr}" y2="${g.h - g.mb}" stroke="var(--axis)"/>
      ${predFill}${nowLine}${pred}${actual}
      <line class="xhair" x1="0" x2="0" y1="${g.mt}" y2="${g.h - g.mb}" stroke="var(--ink2)" stroke-width="1" opacity="0"/>
    </svg>`;
  }

  _dispatchSvg() {
    const g = { w: GW, h: 130, ml: GML, mr: GMR, mt: 8, mb: 22 };
    const t = this._traj; const n = t.length;
    const { t0, t1, step } = this._timeScale();
    const X = (ms) => g.ml + (ms - t0) / (t1 - t0) * (g.w - g.ml - g.mr);
    const bw = (g.w - g.ml - g.mr) * step / (t1 - t0);  // one slot's width (time-based)
    const maxP = Math.max(1000, ...t.map(s => Math.abs(s.power_w || 0)));
    const midY = g.mt + (g.h - g.mt - g.mb) / 2;
    const half = (g.h - g.mt - g.mb) / 2;

    let bars = '', shade = '';
    t.forEach((s, i) => {
      const x = X(new Date(s.start).getTime());
      if ((s.export_rate || 0) > 0) {
        shade += `<rect x="${x}" y="${g.mt}" width="${bw}" height="${g.h - g.mt - g.mb}" fill="var(--fit)"/>`;
      }
      const p = s.power_w || 0;
      const hgt = Math.abs(p) / maxP * (half - 2);
      const gap = 1.5;
      let color = 'var(--idle)', y, h2;
      if (s.action === 'charge') { color = 'var(--charge)'; y = midY - hgt; h2 = hgt; }
      else if (s.action === 'discharge') { color = 'var(--discharge)'; y = midY; h2 = hgt; }
      else { color = 'var(--idle)'; y = midY - 1.5; h2 = 3; }
      // Charge slots note their source: solar-only runs as self-consumption (no grid
      // import); a grid top-up says how much of the charge power comes from the grid.
      let src = '';
      if (s.action === 'charge') {
        const gw = this._execMode(s) === 'charge' ? (s.grid_charge_w || 0) : 0;
        src = gw > 1 ? ` (grid ${Math.round(gw)} W)` : ' (solar)';
      }
      bars += `<rect x="${x + gap}" y="${y}" width="${Math.max(1, bw - 2 * gap)}" height="${Math.max(2, h2)}" rx="2" fill="${color}">` +
        `<title>${fmtHour(new Date(s.start).getTime())} · ${actionLabel(s.action)} ${Math.round(Math.abs(p))} W${src} · SOC ${fmtPct(s.soc_percent)}` +
        ` · imp ${fmtC(s.import_rate)} exp ${fmtC(s.export_rate)}</title></rect>`;
    });
    let xt = xAxisTicks(X, t0, t1, g.h - g.mb);
    const nowX = X(Math.min(Date.now(), t1));
    const nowLine = `<line x1="${nowX}" y1="${g.mt}" x2="${nowX}" y2="${g.h - g.mb}" stroke="var(--axis)" stroke-width="1" stroke-dasharray="2 3"/>`;
    xt += nowLine;

    return `<svg viewBox="0 0 ${g.w} ${g.h}" class="chart-svg" role="img" aria-label="Planned battery dispatch by hour">
      ${shade}
      <line x1="${g.ml}" y1="${midY}" x2="${g.w - g.mr}" y2="${midY}" stroke="var(--axis)"/>
      ${bars}${xt}
      <line class="xhair" x1="0" x2="0" y1="${g.mt}" y2="${g.h - g.mb}" stroke="var(--ink2)" stroke-width="1" opacity="0"/>
      <text x="${g.ml - 6}" y="${g.mt + 8}" text-anchor="end" font-size="9" fill="var(--muted)">chg</text>
      <text x="${g.ml - 6}" y="${g.h - g.mb - 2}" text-anchor="end" font-size="9" fill="var(--muted)">dis</text>
    </svg>`;
  }

  _multiLine(series, opts = {}) {
    const t = this._traj; if (!t || !t.length) return '';
    const g = { w: GW, h: 160, ml: GML, mr: GMR, mt: 10, mb: 22 };
    const { t0, t1 } = this._timeScale();
    const X = (ms) => g.ml + (ms - t0) / (t1 - t0) * (g.w - g.ml - g.mr);
    const nowMs = Date.now();
    // per-series raw points {ms,v}: forecast reads the trajectory (key); measured overlays
    // pass a points[] array (plotted only in the past, up to now).
    const raw = series.map(s => {
      if (s.points) return (s.points || [])
        .filter(p => p.t.getTime() >= t0 && p.t.getTime() <= nowMs + 60000)
        .map(p => ({ ms: p.t.getTime(), v: p.v }));
      return t.map(row => ({ ms: new Date(row.start).getTime(), v: (+row[s.key] || 0) * (s.scale || 1) }));
    });
    let yMax = 0, yMin = opts.yMin != null ? opts.yMin : 0;
    for (const pts of raw) for (const p of pts) { if (p.v > yMax) yMax = p.v; if (p.v < yMin) yMin = p.v; }
    if (yMax === yMin) yMax = yMin + 1;
    const Y = (v) => g.mt + (1 - (v - yMin) / (yMax - yMin)) * (g.h - g.mt - g.mb);
    const fmt = opts.fmt || ((v) => v.toFixed(1));
    let grid = '';
    for (let k = 0; k <= 4; k++) {
      const v = yMin + (yMax - yMin) * k / 4, y = Y(v);
      grid += `<line x1="${g.ml}" y1="${y}" x2="${g.w - g.mr}" y2="${y}" stroke="var(--grid)"/>`;
      grid += `<text x="${g.ml - 5}" y="${y + 3}" text-anchor="end" font-size="9" fill="var(--muted)">${fmt(v)}</text>`;
    }
    const xt = xAxisTicks(X, t0, t1, g.h - g.mb, 9);
    const nowX = X(Math.min(Date.now(), t1));
    const now = `<line x1="${nowX}" y1="${g.mt}" x2="${nowX}" y2="${g.h - g.mb}" stroke="var(--axis)" stroke-dasharray="2 3"/>`;
    const zero = (yMin < 0) ? `<line x1="${g.ml}" y1="${Y(0)}" x2="${g.w - g.mr}" y2="${Y(0)}" stroke="var(--axis)"/>` : '';
    let defs = '', paths = '';
    const base = Y(Math.max(yMin, 0));
    series.forEach((s, si) => {
      const rp = raw[si];
      if (!rp.length) return;
      const pts = rp.map(p => [X(p.ms), Y(p.v)]);
      // Piecewise-constant series (e.g. per-slot prices) get a stepped path — a cubic
      // smooth curve through a step function overshoots at every discontinuity (dips
      // below the pre-window value, spikes past the held value at each jump). A step
      // path only ever passes through sampled y-values, so it can't overshoot, and it's
      // also the truthful shape: the rate holds constant from one slot's start to the next.
      let d, rightX = pts[pts.length - 1][0];
      if (s.step) {
        d = stepPath(pts);
        // Hold the final slot's value out to the right edge of the plotted window,
        // rather than stopping at its start time, so the last level reads as "held".
        const endX = X(t1);
        if (endX > rightX) {
          d += ` L${endX.toFixed(1)},${pts[pts.length - 1][1].toFixed(1)}`;
          rightX = endX;
        }
      } else {
        d = smoothPath(pts);
      }
      if (s.area) {
        const gid = 'g' + (this._gc++);
        defs += gradDef(gid, s.color, 0.48);
        paths += `<path d="${d} L${rightX.toFixed(1)},${base.toFixed(1)} L${pts[0][0].toFixed(1)},${base.toFixed(1)} Z" fill="url(#${gid})"/>`;
      }
      // measured overlays render thinner + slightly faded to read as "actual → forecast".
      paths += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.actual ? 1.75 : 2.5}" opacity="${s.actual ? 0.9 : 1}" stroke-linejoin="round" stroke-linecap="round" ${s.dash ? 'stroke-dasharray="5 4"' : ''}/>`;
    });
    return `<svg viewBox="0 0 ${g.w} ${g.h}" class="chart-svg" role="img"><defs>${defs}</defs>${grid}${zero}${xt}${now}${paths}`
      + `<line class="xhair" x1="0" x2="0" y1="${g.mt}" y2="${g.h - g.mb}" stroke="var(--ink2)" stroke-width="1" opacity="0"/></svg>`;
  }

  _cashSvg() {
    if (!this._traj || !this._traj.length) return '';
    let cum = 0;
    for (const r of this._traj) { cum += (+r.cost || 0) - (+r.credit || 0); r._cum = cum; }
    return this._multiLine([{ key: '_cum', color: 'var(--cum)', area: true }],
      { fmt: (v) => '$' + v.toFixed(1) });
  }

  // The EMS mode the executor will actually command for a slot. Mirrors
  // ScheduleExecutor._resolve_charge: a CHARGE slot only runs as Command Charging
  // (PV first) when the plan wants a *material* grid top-up — a real share of the
  // slot AND above an absolute floor. Otherwise (solar charge, or a trivial LP grid
  // nibble) it runs as Maximum Self-consumption, which resets the charge-rate cap to
  // hardware max and fills the battery from all surplus PV without importing.
  //
  // Symmetrically mirrors ScheduleExecutor._resolve_discharge: a DISCHARGE slot only
  // runs as Command Discharging (battery first) when the plan wants a *material* export
  // — a real share of the slot AND above an absolute floor. Otherwise (load-covering
  // discharge, or a trivial LP nibble) it runs as self-consumption, which already
  // discharges to match load with no rate forced.
  _execMode(row) {
    if (row.action === 'charge') {
      let gw = row.grid_charge_w;
      if (gw == null) {
        // Older sensor payloads lack grid_charge_w — derive it the same way the
        // planner does: import beyond house + deferrable load must be battery charge.
        const { step } = this._timeScale();
        const dtH = step / 3600000;
        gw = Math.max(0, ((+row.buy_kwh || 0) - (+row.load_kwh || 0) - (+row.deferrable_kwh || 0))) / dtH * 1000;
      }
      const pw = +row.power_w || 0;
      return (gw > 250 && gw >= 0.5 * pw) ? 'charge' : 'self_use';
    }
    if (row.action === 'discharge') {
      let ew = row.export_w;
      if (ew == null) {
        // Older sensor payloads lack export_w — derive it the same way the planner
        // does: export energy, capped at the slot's total discharge.
        const { step } = this._timeScale();
        const dtH = step / 3600000;
        const pw = +row.power_w || 0;
        ew = Math.min(Math.max(0, +row.sell_kwh || 0), pw * dtH / 1000) / dtH * 1000;
      }
      const pw = +row.power_w || 0;
      return (ew > 250 && ew >= 0.5 * pw) ? 'discharge' : 'self_use';
    }
    return row.action;
  }

  // Collapse the per-slot executed EMS mode into just the points where it
  // changes — derived client-side from the trajectory already on the sensor, no
  // Python involved. Local time (row.start parses as an ISO timestamp; fmtHour()
  // reads it in the browser's local timezone).
  _modeTransitions() {
    const t = this._traj || [];
    const out = [];
    let prev = null;
    for (const row of t) {
      const a = this._execMode(row);
      if (a !== prev) {
        out.push({ ms: new Date(row.start).getTime(), action: a });
        prev = a;
      }
    }
    return out;
  }

  _modeTimelineHtml() {
    const trans = this._modeTransitions();
    if (!trans.length) return '<div class="sub">No plan data.</div>';
    const items = trans.map(x =>
      `<li><span class="dot" style="background:${MODE_COLORS[x.action] || 'var(--idle)'}"></span>` +
      `<span class="t">${fmtHour(x.ms)}</span><span class="arrow">&rarr;</span>` +
      `<span class="m">${esc(modeLabel(x.action))}</span></li>`
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

  _wireCrosshair() {
    const charts = this.shadowRoot.querySelector('.charts');
    if (!charts || !this._traj || !this._traj.length) return;
    const svgs = [...this.shadowRoot.querySelectorAll('.chart-svg')];
    const xhairs = [...this.shadowRoot.querySelectorAll('.xhair')];
    const tip = this.shadowRoot.querySelector('.xtip');
    if (!svgs.length || !tip) return;

    const move = (ev) => {
      // Get time scale dynamically so view mode changes are reflected
      const { t0, t1, step } = this._timeScale();
      const kwScale = 3600000 / step;
      const r = ev.currentTarget.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1,
        ((ev.clientX - r.left) / r.width * GW - GML) / (GW - GML - GMR)));
      const ms = t0 + frac * (t1 - t0);
      // Check if hovering over historical time (before forecast starts)
      const trajStart = new Date(this._traj[0].start).getTime();
      const isHistory = ms < trajStart;

      let bestMs, best = null;
      if (!isHistory) {
        // Hovering over forecast: find nearest trajectory slot
        best = this._traj[0];
        let bd = Infinity;
        for (const s of this._traj) { const d = Math.abs(new Date(s.start).getTime() - ms); if (d < bd) { bd = d; best = s; } }
        bestMs = new Date(best.start).getTime();
      } else {
        // Hovering over history: show data for the hovered time, not nearest forecast
        bestMs = ms;
      }

      const bx = this._xOf(bestMs);
      // move every chart's crosshair to the same time-column
      xhairs.forEach(l => { l.setAttribute('x1', bx); l.setAttribute('x2', bx); l.setAttribute('opacity', '1'); });

      // Get measured data for this time
      let av = null, actualSolar = null, actualLoad = null, actualBuy = null, actualSell = null;
      if (this._actual && this._actual.length) {
        let d2 = Infinity;
        for (const p of this._actual) { const d = Math.abs(p.t.getTime() - bestMs); if (d < d2) { d2 = d; av = (d < 5400000) ? p.v : null; } }
      }
      // Get actual energy data for this time if available
      if (this._actualEnergy.solar && this._actualEnergy.solar.length) {
        let d2 = Infinity;
        for (const p of this._actualEnergy.solar) { const d = Math.abs(p.t.getTime() - bestMs); if (d < d2) { d2 = d; actualSolar = (d < 5400000) ? p.v : null; } }
      }
      if (this._actualEnergy.load && this._actualEnergy.load.length) {
        let d2 = Infinity;
        for (const p of this._actualEnergy.load) { const d = Math.abs(p.t.getTime() - bestMs); if (d < d2) { d2 = d; actualLoad = (d < 5400000) ? p.v : null; } }
      }
      if (this._actualEnergy.buy && this._actualEnergy.buy.length) {
        let d2 = Infinity;
        for (const p of this._actualEnergy.buy) { const d = Math.abs(p.t.getTime() - bestMs); if (d < d2) { d2 = d; actualBuy = (d < 5400000) ? p.v : null; } }
      }
      if (this._actualEnergy.sell && this._actualEnergy.sell.length) {
        let d2 = Infinity;
        for (const p of this._actualEnergy.sell) { const d = Math.abs(p.t.getTime() - bestMs); if (d < d2) { d2 = d; actualSell = (d < 5400000) ? p.v : null; } }
      }

      // Build tooltip content
      if (isHistory && !best) {
        // Historical data only
        tip.innerHTML = `<b>${fmtHour(bestMs)}</b>` +
          (av != null ? `<div><span class="k" style="color:var(--actual)">SOC</span> ${fmtPct(av)}</div>` : '') +
          (actualSolar != null || actualLoad != null ? `<div><span class="k" style="color:var(--solar)">sun</span> ${(actualSolar || 0).toFixed(2)} · <span class="k" style="color:var(--load)">load</span> ${(actualLoad || 0).toFixed(2)} kW</div>` : '') +
          (actualBuy != null || actualSell != null ? `<div><span class="k" style="color:var(--buy)">buy</span> ${(actualBuy || 0).toFixed(2)} · <span class="k" style="color:var(--sell)">sell</span> ${(actualSell || 0).toFixed(2)} kW</div>` : '') +
          `<div style="font-size:10px;color:var(--muted);margin-top:4px">Historical data only (no forecast)</div>`;
      } else if (best) {
        // Forecast data with actual overlay
        tip.innerHTML =
          `<b>${fmtHour(bestMs)}</b>` +
          `<div><span class="k" style="color:var(--predicted)">SOC plan</span> ${fmtPct(best.soc_percent)}` +
          (av != null ? ` · <span class="k" style="color:var(--actual)">actual</span> ${fmtPct(av)}` : '') + `</div>` +
          `<div><b>${actionLabel(best.action)}</b>${best.power_w ? ' · ' + Math.round(best.power_w) + ' W' : ''}</div>` +
          `<div><span class="k" style="color:var(--solar)">sun</span> ${((actualSolar != null ? actualSolar : (+best.solar_kwh || 0) * kwScale)).toFixed(2)} · <span class="k" style="color:var(--load)">load</span> ${((actualLoad != null ? actualLoad : (+best.load_kwh || 0) * kwScale)).toFixed(2)} kW</div>` +
          `<div><span class="k" style="color:var(--buy)">buy</span> ${((actualBuy != null ? actualBuy : (+best.buy_kwh || 0) * kwScale)).toFixed(2)} · <span class="k" style="color:var(--sell)">sell</span> ${((actualSell != null ? actualSell : (+best.sell_kwh || 0) * kwScale)).toFixed(2)} kW</div>` +
          (this._deferNames || []).map((nm, i) => {
            const v = (+best['defer_' + i] || 0) * kwScale;
            return v > 0.01 ? `<div><span class="k" style="color:var(--defer${(i % 4) + 1})">${esc(nm)}</span> ${v.toFixed(2)} kW</div>` : '';
          }).join('') +
          `<div><span class="k">rate</span> ${fmtC(best.import_rate)} in / ${fmtC(best.export_rate)} out</div>`;
      } else {
        // No data at all
        tip.innerHTML = `<b>${fmtHour(bestMs)}</b><div style="font-size:11px;color:var(--muted)">No data available</div>`;
      }
      const cr = charts.getBoundingClientRect();
      const flip = (ev.clientX - cr.left) > cr.width * 0.62;
      tip.style.left = (ev.clientX - cr.left) + 'px';
      tip.style.top = (ev.clientY - cr.top) + 'px';
      tip.style.transform = flip ? 'translate(calc(-100% - 14px), -50%)' : 'translate(14px, -50%)';
      tip.style.opacity = '1';
    };
    const leave = () => {
      xhairs.forEach(l => l.setAttribute('opacity', '0'));
      tip.style.opacity = '0';
    };

    svgs.forEach(svg => svg.addEventListener('mousemove', move));
    charts.addEventListener('mouseleave', leave);
  }
}

const GW = 720, GML = 44, GMR = 12;  // shared plot geometry across all charts
const VIEW_BACK_MS = 2 * 3600000;    // show 2h of history to the left of "now"

/* helpers */
// Downsample a {t,v} series to at most `max` points (keeps rendering light).
function ds(pts, max = 160) {
  if (!pts || pts.length <= max) return pts || [];
  const step = Math.ceil(pts.length / max);
  const out = pts.filter((_, i) => i % step === 0);
  if (out[out.length - 1] !== pts[pts.length - 1]) out.push(pts[pts.length - 1]);
  return out;
}
// Catmull-Rom → cubic-bezier smoothing. pts = [[x,y],…] → SVG path 'd'.
function smoothPath(pts) {
  if (!pts || !pts.length) return '';
  if (pts.length < 3) return 'M' + pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' L');
  const k = 0.16;
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i ? i - 1 : 0], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    const c1x = p1[0] + (p2[0] - p0[0]) * k, c1y = p1[1] + (p2[1] - p0[1]) * k;
    const c2x = p2[0] - (p3[0] - p1[0]) * k, c2y = p2[1] - (p3[1] - p1[1]) * k;
    d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}
// Step-after interpolation: pts = [[x,y],…] sampled at each slot's start → SVG path 'd'
// that holds each y at its sampled value until the next x, then jumps. Correct shape for
// piecewise-constant series (e.g. per-slot import/export rate) — never overshoots past a
// sampled value, unlike a cubic spline through a step function.
function stepPath(pts) {
  if (!pts || !pts.length) return '';
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1];
    const [x1, y1] = pts[i];
    d += ` L${x1.toFixed(1)},${y0.toFixed(1)} L${x1.toFixed(1)},${y1.toFixed(1)}`;
  }
  return d;
}
// Timestamps for x-axis ticks: every local hour in [t0, t1] whose hour-of-day is a
// multiple of everyH. Walks hour-by-hour reading localized hours, so ticks stay on
// clean local clock times across a DST change.
function tickTimes(t0, t1, everyH = 3) {
  const out = [];
  const d = new Date(t0);
  d.setMinutes(0, 0, 0);
  while (d.getTime() < t0) d.setTime(d.getTime() + 3600000);
  for (; d.getTime() <= t1; d.setTime(d.getTime() + 3600000)) {
    if (d.getHours() % everyH === 0) out.push(d.getTime());
  }
  return out;
}
// Tick marks + HH:MM labels along the bottom axis, shared by every chart.
function xAxisTicks(X, t0, t1, axisY, fontSize = 10) {
  let s = '';
  for (const ms of tickTimes(t0, t1)) {
    const x = X(ms);
    s += `<line x1="${x}" y1="${axisY}" x2="${x}" y2="${axisY + 3}" stroke="var(--axis)"/>`;
    s += `<text x="${x}" y="${axisY + 14}" text-anchor="middle" font-size="${fontSize}" fill="var(--muted)">${fmtHour(ms)}</text>`;
  }
  return s;
}
// Vertical fade gradient (color → transparent) for area fills.
function gradDef(id, color, topOpacity) {
  return `<linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">`
    + `<stop offset="0" style="stop-color:${color};stop-opacity:${topOpacity}"/>`
    + `<stop offset="1" style="stop-color:${color};stop-opacity:0.02"/></linearGradient>`;
}
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function clampPct(v) { v = parseFloat(v); return isNaN(v) ? 0 : Math.max(0, Math.min(100, v)); }
function fmtPct(v) { v = parseFloat(v); return isNaN(v) ? '–' : v.toFixed(0) + '%'; }
function fmtMoney(v) { v = parseFloat(v); if (isNaN(v)) return '–'; const s = v < 0 ? '+$' : '$'; return s + Math.abs(v).toFixed(2); }
function fmtC(v) { v = parseFloat(v); return isNaN(v) ? '–' : (v * 100).toFixed(0) + 'c'; }
function actionLabel(a) { return ({ charge: 'Charge', discharge: 'Discharge', self_use: 'Self-use', idle: 'Idle' }[a]) || (a ? esc(a) : '–'); }
// Sigenergy EMS mode a slot's plan `action` will be executed as, for the control-state timeline.
const MODE_LABELS = {
  self_use: 'Maximum Self-consumption',
  charge: 'Command Charging (PV first)',
  discharge: 'Command Discharging (battery first)',
  idle: 'Standby',
};
const MODE_COLORS = { self_use: 'var(--good)', charge: 'var(--charge)', discharge: 'var(--discharge)', idle: 'var(--idle)' };
function modeLabel(a) { return MODE_LABELS[a] || (a ? esc(a) : '–'); }
function fmtHour(ms) { const d = new Date(ms); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); }
function fmtTime(iso) { try { const d = new Date(iso); return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); } catch (e) { return ''; } }

customElements.define('grid-lens-advisory-card', GridLensAdvisoryCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-advisory-card',
  name: 'Grid Lens Advisory',
  description: 'Predicted vs actual battery SOC and the planned dispatch timeline.',
});
