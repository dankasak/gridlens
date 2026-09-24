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
 *   show_current_rates: true                                (optional)
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
 * `show_current_rates: true` adds a one-line buy/sell readout ("Buy 22c/kWh · Sell 3c/kWh")
 * under the plan-status line — the rate in effect for the slot covering now, read from the
 * same `trajectory` attribute. Just the numbers; the rate *graph* is the standalone
 * `grid-lens-price-chart-card` (placed under the power chart on the Power Flow view).
 * Independent of `compact` — works in the full card too.
 *
 * `layout_toggles` adds a clickable on/off chip per entry to the header, toggling that
 * entity (any `switch.*` — nothing here is specific to the Power Flow layouts it was
 * built for). The chips are deliberately co-located with the plan status rather than
 * living on the cards they control, so "what am I looking at, and when was it computed"
 * reads as one bar. Independent of `compact` — works in the full card too.
 */
import {
  STYLE, esc, fmtTime, fmtDayHour, modeLabel, MODE_COLORS, execMode, reasonFor, deferColorFor,
  fmtC, resolveDeferrableLoads, resolveDailyTargetMasterEid, resolveDailyTargetEidFor,
  resolveBoostEidFor, resolveChargeTargetPercentEidFor, resolveSolarForecastEid, solarSummary,
  fmtKwh, clampTargetPct, fetchDailyAverageKwh, resolveLoadControlRows, fetchDailyHistory,
  estimatorFor, boostCeiling, socCapFor, friendlyNote, greedyLine, modulationLine, socCapHtml,
  sparklineHtml, estimatorToggleHtml, estimatorPanelHtml, controlHtml, greedyButtonsHtml,
  boostInputHtml, currentReadoutHtml, maxCurrentHtml, attachTooltip,
} from './grid-lens-chart-common.js?v=20260924d';

const DAILY_TARGET_HISTORY_REFRESH_MS = 15 * 60000;
// Minimum time the optimizing dot stays visible once triggered, regardless of how
// quickly the underlying run actually finishes — see the constructor's _optDotUntil.
const OPT_DOT_MIN_MS = 900;

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
    // Minimum-dwell for the optimizing dot (see _paint's showOptDot) — on a fast
    // install the LP solve can finish in well under 200ms, faster than a human can
    // register a flash. Forces the dot to stay visible for OPT_DOT_MIN_MS from
    // whenever it last turned true, even after is_optimizing itself has already
    // flipped back to false, via a one-shot setTimeout repaint (see set hass() below).
    this._optDotUntil = 0;
    this._optHideTimer = null;
    // Daily Target (FEATURES.md §9b) — relocated here 2026-09-22 from its own standalone
    // card so the solar forecast + master slider are visible on the Power Flow page
    // without a trip to Settings; the per-device sliders live behind the expander below
    // so the collapsed header stays a genuinely "slim status bar" (this card's original
    // job). Local-only UI state — never part of the hass-diffing signature, and never
    // reset on repaint (only on setConfig, matching _expandedEstimator's convention in
    // grid-lens-load-control-card.js).
    this._dtExpanded = false;
    this._dtDevices = [];
    this._dtMasterEid = null;
    this._dtSolarEid = null;
    this._dtHistoryCache = {};
    this._dtHistoryPending = new Set();
    // Load control (FEATURES.md §6/§6a) — merged into this same per-device expander
    // 2026-09-24 from the formerly Settings-only grid-lens-load-control-card.js, so a
    // device's Today Boost/Greedy/On-Off controls don't have to live on two different
    // pages. Separate cache from _dtHistoryCache above (that one stores a single average
    // number for the %-slider readout; this one stores the day-by-day series the
    // sparkline needs — different shape, kept in its own object rather than one cache
    // guessing which shape a given entry is). _dtExpandedEstimator mirrors
    // grid-lens-load-control-card.js's own `_expandedEstimator` — local-only UI state,
    // never part of `dtSig`, never reset on repaint.
    this._dtSparkCache = {};
    this._dtSparkPending = new Set();
    this._dtExpandedEstimator = new Set();
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

  getCardSize() {
    if (!this._config.compact) return 3;
    if (!this._dtExpanded) return 2;
    // The expanded panel now carries the merged load-control row content too (sparkline,
    // boost, greedy, control segment, and an estimator debug panel per open row) —
    // Home Assistant's masonry/sections layout only takes this as an approximate hint,
    // and the exact constants here may need visual tuning once seen in a browser.
    const rows = (this._dtRows || []).length;
    const openEstimators = this._dtExpandedEstimator ? this._dtExpandedEstimator.size : 0;
    return Math.max(3, rows + 2 + openEstimators);
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this.classList.toggle('dark', dark); }

    // Daily Target resolution — independent of the dispatch sensor below, so it still
    // works even in the (unlikely) case that sensor is unavailable.
    this._dtDevices = resolveDeferrableLoads(hass);
    this._dtPollHistory(this._dtDevices);
    this._dtPollSparkline(this._dtDevices);
    this._dtMasterEid = resolveDailyTargetMasterEid(hass);
    this._dtSolarEid = resolveSolarForecastEid(hass, this._config.solar_forecast_entity);
    const loadControlRows = resolveLoadControlRows(hass, this._dtDevices);
    const dtRows = this._dtDevices.map((d, i) => ({
      device: d,
      targetEid: resolveDailyTargetEidFor(hass, d.energy_entity),
      boostEid: resolveBoostEidFor(hass, d.energy_entity),
      ctEid: resolveChargeTargetPercentEidFor(hass, d.energy_entity),
      ...loadControlRows[i],
    }));
    this._dtRows = dtRows;
    const dtSolarSt = this._dtSolarEid && hass.states[this._dtSolarEid];
    const dtSig = [
      this._dtMasterEid && hass.states[this._dtMasterEid] ? hass.states[this._dtMasterEid].state : '',
      dtRows.map((r) => {
        const d = r.device;
        const t = r.targetEid && hass.states[r.targetEid];
        const b = r.boostEid && hass.states[r.boostEid];
        const c = r.ctEid && hass.states[r.ctEid];
        // Load control fields — same set grid-lens-load-control-card.js's own `hass`
        // setter signature includes, ported verbatim so its merged row here repaints on
        // exactly the same state changes as the standalone card does.
        const ctl = r.controlEid && hass.states[r.controlEid];
        const sel = r.selEid && hass.states[r.selEid];
        const g = r.gEid && hass.states[r.gEid];
        const gs = r.gsEid && hass.states[r.gsEid];
        const gf = r.gfEid && hass.states[r.gfEid];
        const ceiling = boostCeiling(d);
        const isMod = d.control_type === 'modulating';
        const mc = isMod && r.maxCurEid ? hass.states[r.maxCurEid] : null;
        const sp = isMod && d.setpoint_entity ? hass.states[d.setpoint_entity] : null;
        const pw = isMod && d.power_entity ? hass.states[d.power_entity] : null;
        const est = estimatorFor(hass, d);
        const estA = est && est.attrs;
        const sc = socCapFor(hass, d.energy_entity);
        return [
          r.device.energy_entity,
          t ? `${t.state}|${(t.attributes || {}).is_override}` : '',
          b ? b.state : '',
          c ? c.state : '',
          sc ? `${sc.day0_charge_kwh}|${sc.target_kwh}|${sc.max_percent}|${sc.initial_percent}` : '',
          ctl ? [ctl.state, (ctl.attributes || {}).note, (ctl.attributes || {}).greedy_reason,
                 (ctl.attributes || {}).greedy_blocked,
                 (ctl.attributes || {}).forecast_free_kwh,
                 (ctl.attributes || {}).modulation_source,
                 (ctl.attributes || {}).plugged_in].join('|') : '',
          sel ? sel.state : '',
          g ? g.state : '',
          gs ? gs.state : '',
          gf ? gf.state : '',
          ceiling == null ? '' : ceiling.toFixed(1),
          mc ? mc.state : '',
          sp ? sp.state : '',
          pw ? pw.state : '',
          estA ? `${estA.sample_count}|${estA.last_sample_at}|${estA.estimated_kw}|${est.state.state}` : '',
        ].join('~');
      }).join(','),
      // Minute-granularity tick so "today remaining" keeps drifting through the day even
      // between solar-sensor updates.
      dtSolarSt ? `${dtSolarSt.state}@${dtSolarSt.last_updated || ''}@${Math.floor(Date.now() / 60000)}` : '',
      this._dtExpanded ? 'x1' : 'x0',
    ].join('#');

    const st = hass.states[this._config.entity];
    if (!st) {
      this._summary = { status: 'unknown' };
      this._traj = null;
      const sig = `unknown|${dtSig}`;
      if (sig !== this._sig) { this._sig = sig; this._paint(); }
      return;
    }

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
      is_optimizing: a.is_optimizing === true,
    };

    // Extend the dot's forced-visible window every time we see is_optimizing true —
    // covers both the moment it starts AND (since the coordinator's own listener push
    // means we may see "true" more than once before "false" lands) a run that's still
    // going. A trailing setTimeout repaints once the window closes, so the dot goes
    // away on its own even though nothing else about hass has changed by then.
    if (this._summary.is_optimizing) {
      this._optDotUntil = Date.now() + OPT_DOT_MIN_MS;
      if (!this._optHideTimer) {
        this._optHideTimer = setTimeout(() => {
          this._optHideTimer = null;
          this._paint();
        }, OPT_DOT_MIN_MS + 20);
      }
    }

    // Toggle chips live in this card but reflect OTHER entities' state, so their states
    // have to be part of the repaint signature — otherwise flipping one wouldn't redraw
    // the chip until the dispatch sensor happened to update (up to a full plan interval).
    const toggleSig = this._layoutToggles()
      .map((t) => `${t.entity}=${hass.states[t.entity] ? hass.states[t.entity].state : '?'}`)
      .join(',');

    const sig = `${st.last_updated}|${switchSt ? switchSt.last_updated : ''}|${toggleSig}|${dtSig}`;
    if (sig !== this._sig) { this._sig = sig; this._paint(); }
  }

  // Same lazy-fetch-and-cache pattern as grid-lens-load-control-card.js's own
  // _pollHistory/_fetchHistory (now shared via fetchDailyAverageKwh) — a stats query, not
  // part of live hass state, so it needs its own refresh timer rather than piggy-backing
  // on the hass tick.
  _dtPollHistory(devices) {
    for (const d of devices) {
      const eid = d.energy_entity;
      if (!eid || this._dtHistoryPending.has(eid)) continue;
      const cached = this._dtHistoryCache[eid];
      if (cached && Date.now() - cached.ts < DAILY_TARGET_HISTORY_REFRESH_MS) continue;
      this._dtHistoryPending.add(eid);
      fetchDailyAverageKwh(this._hass, eid).then((avgKwh) => {
        this._dtHistoryPending.delete(eid);
        this._dtHistoryCache[eid] = { ts: Date.now(), avgKwh };
        this._paint();
      });
    }
  }

  // Same pattern as _dtPollHistory above, but for the merged Today Boost sparkline's
  // day-by-day series (fetchDailyHistory) rather than a single average — a separate cache
  // because the two entries have different shapes ({avgKwh} vs {days}).
  _dtPollSparkline(devices) {
    for (const d of devices) {
      const eid = d.energy_entity;
      if (!eid || this._dtSparkPending.has(eid)) continue;
      const cached = this._dtSparkCache[eid];
      if (cached && Date.now() - cached.ts < DAILY_TARGET_HISTORY_REFRESH_MS) continue;
      this._dtSparkPending.add(eid);
      fetchDailyHistory(this._hass, eid).then((days) => {
        this._dtSparkPending.delete(eid);
        this._dtSparkCache[eid] = { ts: Date.now(), days };
        this._paint();
      });
    }
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

  // One-line buy/sell readout for the slot covering now — the rate the plan is pricing
  // against this instant. Empty string unless show_current_rates is set and a plan is
  // available, so it drops cleanly out of both the compact and full layouts.
  _currentRatesHtml() {
    if (!this._config.show_current_rates) return '';
    const t = this._traj;
    if (!t || !t.length || (this._summary && this._summary.status !== 'ok')) return '';
    const nowMs = Date.now();
    let cur = null;
    for (const row of t) {
      if (new Date(row.start).getTime() <= nowMs) cur = row; else break;
    }
    cur = cur || t[0];
    const buy = cur.import_rate != null ? `${fmtC(cur.import_rate)}/kWh` : '–';
    const sell = cur.export_rate != null ? `${fmtC(cur.export_rate)}/kWh` : '–';
    return `<div class="rates-now">` +
      `<span><span class="rk" style="color:var(--buy)">Buy</span> <b>${buy}</b></span>` +
      `<span><span class="rk" style="color:var(--sell)">Sell</span> <b>${sell}</b></span>` +
      `</div>`;
  }

  // ------------------------------------------------------------ Daily Target (§9b)

  _dtSliderRowHtml(eid, pct, kwhReadout, extraClass = '') {
    return `
      <div class="dt-slider-wrap ${extraClass}">
        <input type="range" min="0" max="300" step="5" value="${pct}" data-dt-eid="${eid}">
        <span class="dt-pct" data-dt-pct-for="${eid}">${pct.toFixed(0)}%</span>
      </div>
      <div class="dt-meta" data-dt-kwh-for="${eid}">${kwhReadout}</div>
    `;
  }

  // Solar-today/tomorrow chips + master slider + expand/collapse toggle — the part of
  // Daily Target visible in BOTH compact (Power Flow page) and full (Battery Plan page)
  // layouts, since it's genuinely useful either place and this keeps one render path
  // instead of two that could drift.
  // Solar chips + master slider + expand button — spliced as DIRECT CHILDREN of the
  // card's own `.hd` flex row (not a separate block below it), so the whole header —
  // title, status badge, solar, master slider, expander — reads as ONE row (user
  // request 2026-09-22, second pass: the first pass still put Daily Target on its own
  // row below `.hd`, just no longer split across two rows of its OWN). Single-line
  // chip markup (icon + value only, condition/label carried in the `title` tooltip
  // instead of a second/third visible line) — the 3-line box from the first pass was
  // part of what made two rows unavoidable at normal card widths.
  _dtInlineHtml() {
    const hass = this._hass;
    if (!hass) return '';
    const solar = solarSummary(hass, this._config.solar_forecast_entity);
    const solarChip = (label, kwh, weather) => `
      <div class="dt-solar-chip${weather.dim ? ' dim' : ''}"
           title="${esc(label)}: ${kwh == null ? 'unavailable' : fmtKwh(kwh)} — ${esc(weather.label)}">
        <ha-icon icon="${weather.icon}"></ha-icon>
        <span class="dt-solar-value">${kwh == null ? '–' : fmtKwh(kwh)}</span>
      </div>`;

    const masterSt = this._dtMasterEid && hass.states[this._dtMasterEid];
    const masterPct = masterSt ? clampTargetPct(masterSt.state) : 100;
    const hasDevices = (this._dtDevices || []).length > 0;

    const masterInline = this._dtMasterEid ? `
      <div class="dt-master-inline">
        <span class="dt-master-name">Daily Target</span>
        <input type="range" min="0" max="300" step="5" value="${masterPct}" data-dt-eid="${this._dtMasterEid}">
        <span class="dt-pct" data-dt-pct-for="${this._dtMasterEid}">${masterPct.toFixed(0)}%</span>
      </div>` : '';

    const expandBtn = (this._dtMasterEid && hasDevices) ? `
      <button class="dt-expand-btn" type="button" data-dt-expand-toggle
        aria-expanded="${this._dtExpanded}" title="${this._dtExpanded ? 'Hide' : 'Show'} per-device targets">
        <ha-icon icon="${this._dtExpanded ? 'mdi:chevron-up' : 'mdi:chevron-down'}"></ha-icon>
      </button>` : '';

    return `
      ${solarChip('Today (remaining)', solar.todayKwh, solar.todayWeather)}
      ${solarChip('Tomorrow', solar.tomorrowKwh, solar.tomorrowWeather)}
      ${masterInline}
      ${expandBtn}`;
  }

  // Expanded per-device panel — full-width, rendered below the header row, only
  // when the user has clicked the expander open. Separate from _dtInlineHtml
  // precisely so opening it does NOT push anything out of the single header row.
  _dtExpandedHtml() {
    if (!this._dtMasterEid || !this._dtExpanded) return '';
    return `<div class="dt-block">${this._dtPanelHtml()}</div>`;
  }

  // Per-device rows — only rendered while expanded. Same row shape (slider + kWh readout +
  // Today-Boost/charge-target annotation + "Follow master" reset) as the original standalone
  // grid-lens-daily-target-card.js, since this replaces that card's default visibility.
  _dtPanelHtml() {
    const hass = this._hass;
    const rows = this._dtRows || [];
    if (!rows.length) return '<div class="dt-empty">No deferrable loads configured yet.</div>';
    const masterSt = this._dtMasterEid && hass.states[this._dtMasterEid];
    const masterPct = masterSt ? clampTargetPct(masterSt.state) : 100;

    return `<div class="dt-panel">` + rows.map((r) => {
      const d = r.device;
      if (!r.targetEid) {
        return `<div class="dt-row"><div class="dt-info"><div class="dt-name">${esc(d.name)}</div>
          <div class="dt-meta">Daily Target entity not found for this device</div></div></div>`;
      }
      const targetSt = hass.states[r.targetEid];
      const isOverride = !!(targetSt && (targetSt.attributes || {}).is_override);
      const pct = targetSt ? clampTargetPct(targetSt.state) : masterPct;
      const cached = this._dtHistoryCache[d.energy_entity];
      const avgKwh = cached ? cached.avgKwh : null;
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

      const resetBtn = isOverride
        ? `<button class="dt-reset-btn" data-dt-reset-for="${d.energy_entity}">Follow master</button>` : '';

      // Load control (FEATURES.md §6/§6a) — merged into this same per-device row
      // 2026-09-24, same content as grid-lens-load-control-card.js's own row, built from
      // the same shared chart-common.js functions with { prefix: 'dt-' } so their class
      // names don't collide with this card's own bare `.row` (used elsewhere for the
      // mode-transition timeline — see chart-common.js's "Load control helpers" header
      // comment for why the prefix matters here specifically).
      const controlSt = r.controlEid ? hass.states[r.controlEid] : null;
      const a = controlSt ? (controlSt.attributes || {}) : {};
      const controlMeta = friendlyNote(a.note);
      const isErr = (a.note || '').startsWith('command_error');
      const est = estimatorFor(hass, d);
      const estOpen = !!(est && this._dtExpandedEstimator.has(est.eid));
      const lcOpts = { prefix: 'dt-' };

      return `
        <div class="dt-row">
          <div class="dt-info">
            <div class="dt-name">${esc(d.name)}</div>
            ${note ? `<div class="dt-meta dt-note">${esc(note)}</div>` : ''}
            ${controlMeta ? `<div class="dt-meta${isErr ? ' dt-note' : ''}">${esc(controlMeta)}</div>` : ''}
            ${greedyLine(a, lcOpts)}
            ${modulationLine(a, d, lcOpts)}
            ${socCapHtml(hass, d, lcOpts)}
          </div>
          ${this._dtSliderRowHtml(r.targetEid, pct, kwhReadout, isOverride ? 'is-override' : '')}
          ${resetBtn}
          ${sparklineHtml(this._dtSparkCache[d.energy_entity], d.energy_entity, lcOpts)}
          ${boostInputHtml(hass, r, d, lcOpts)}
          ${currentReadoutHtml(hass, d, lcOpts)}
          ${maxCurrentHtml(hass, r, d, lcOpts)}
          ${greedyButtonsHtml(hass, r, d, lcOpts)}
          ${estimatorToggleHtml(est, estOpen, lcOpts)}
          ${controlHtml(hass, r, d, lcOpts)}
          ${est && estOpen ? estimatorPanelHtml(d, est, lcOpts) : ''}
        </div>`;
    }).join('') + `</div>`;
  }

  _onDtExpandClick(ev) {
    const btn = ev.target && ev.target.closest && ev.target.closest('[data-dt-expand-toggle]');
    if (!btn) return;
    ev.stopPropagation();
    this._dtExpanded = !this._dtExpanded;
    this._paint();
  }

  _onDtResetClick(ev) {
    const btn = ev.target && ev.target.closest && ev.target.closest('[data-dt-reset-for]');
    if (!btn || !this._hass) return;
    ev.stopPropagation();
    const sensorId = btn.getAttribute('data-dt-reset-for');
    this._hass.callService('grid_lens', 'clear_daily_target', { sensor_id: sensorId });
  }

  _onDtSliderInput(ev) {
    const el = ev.target;
    if (!el || !el.matches || !el.matches('input[type=range][data-dt-eid]')) return;
    const eid = el.getAttribute('data-dt-eid');
    const readout = this.shadowRoot.querySelector(`[data-dt-pct-for="${eid}"]`);
    if (readout) readout.textContent = `${parseFloat(el.value).toFixed(0)}%`;
  }

  _onDtSliderChange(ev) {
    const el = ev.target;
    if (!el || !el.matches || !el.matches('input[type=range][data-dt-eid]') || !this._hass) return;
    const eid = el.getAttribute('data-dt-eid');
    this._hass.callService('number', 'set_value', { entity_id: eid, value: clampTargetPct(el.value) });
  }

  // ---------------------------------------------- Load control (merged 2026-09-24)
  //
  // All delegated on `.body` (bound once in _renderShell, below), unlike
  // grid-lens-load-control-card.js's own per-element querySelectorAll(...).forEach(...)
  // wiring — that pattern only works there because it re-attaches after every _paint()
  // call; this card's _paint() only ever replaces `.body`'s innerHTML, and every other
  // handler on this card is already delegated for exactly that reason.

  _onDtEstToggleClick(ev) {
    const btn = ev.target && ev.target.closest && ev.target.closest('.dt-est-toggle[data-est-eid]');
    if (!btn) return;
    ev.stopPropagation();
    const eid = btn.getAttribute('data-est-eid');
    if (this._dtExpandedEstimator.has(eid)) this._dtExpandedEstimator.delete(eid);
    else this._dtExpandedEstimator.add(eid);
    this._paint();
  }

  _onDtGreedyClick(ev) {
    const btn = ev.target && ev.target.closest && ev.target.closest('.dt-gbtn[data-eid]');
    if (!btn || !this._hass) return;
    ev.stopPropagation();
    this._hass.callService('switch', 'toggle', { entity_id: btn.getAttribute('data-eid') });
  }

  _onDtOverrideClick(ev) {
    const btn = ev.target && ev.target.closest && ev.target.closest('.dt-ovr button:not([disabled])');
    if (!btn || !this._hass) return;
    ev.stopPropagation();
    const grp = btn.closest('.dt-ovr');
    const sel = grp.getAttribute('data-sel');
    const opt = btn.getAttribute('data-opt');
    this._hass.callService('select', 'select_option', { entity_id: sel, option: opt });
    if (opt === 'Auto') {
      // Auto means "GridLens controls it" — also engage the per-device enable switch,
      // same as grid-lens-load-control-card.js's own override-button handler.
      this._hass.callService('switch', 'turn_on', { entity_id: grp.getAttribute('data-ctl') });
    }
  }

  // Plain-toggle fallback (an older integration build with no override select yet).
  _onDtSwitchClick(ev) {
    const el = ev.target && ev.target.closest && ev.target.closest('.dt-sw[data-eid]');
    if (!el || !this._hass) return;
    ev.stopPropagation();
    this._hass.callService('switch', 'toggle', { entity_id: el.getAttribute('data-eid') });
  }

  _onDtBoostInputChange(ev) {
    const el = ev.target;
    if (!el || !el.matches || !el.matches('.dt-boost-input') || !this._hass) return;
    const eid = el.getAttribute('data-eid');
    let v = parseFloat(el.value);
    if (!Number.isFinite(v) || v < 0) v = 0;
    this._hass.callService('number', 'set_value', { entity_id: eid, value: v });
  }

  _onDtBoostInputKeydown(ev) {
    if (ev.key === 'Enter' && ev.target && ev.target.matches && ev.target.matches('.dt-boost-input')) {
      ev.target.blur();
    }
  }

  _onDtMaxCurInputChange(ev) {
    const el = ev.target;
    if (!el || !el.matches || !el.matches('.dt-maxcur-input') || !this._hass) return;
    const eid = el.getAttribute('data-eid');
    let v = parseFloat(el.value);
    const lo = parseFloat(el.min), hi = parseFloat(el.max);
    if (!Number.isFinite(v)) return;
    if (Number.isFinite(lo)) v = Math.max(lo, v);
    if (Number.isFinite(hi)) v = Math.min(hi, v);
    this._hass.callService('number', 'set_value', { entity_id: eid, value: v });
  }

  _onDtMaxCurInputKeydown(ev) {
    if (ev.key === 'Enter' && ev.target && ev.target.matches && ev.target.matches('.dt-maxcur-input')) {
      ev.target.blur();
    }
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}
        /* .hd itself (shared STYLE) is justify-content:space-between, meant for exactly
           two children (title block, right-side badge). Daily Target adds several more
           direct children in between (user request 2026-09-22, second pass — putting
           them all IN this row, not a second row below it) — flex-start packs everything
           left with its natural gap instead, and .hd-right's margin-left:auto below pins
           just the badge/chips group to the far right the way space-between used to. */
        .hd { justify-content:flex-start; }
        .hd-plan { flex:0 1 auto; min-width:0; }
        .hd-right { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-left:auto; }
        .rates-now { display:flex; gap:14px; margin-top:4px; font-size:12px;
                     color:var(--ink2); font-variant-numeric:tabular-nums; }
        .rates-now .rk { font-weight:650; }
        .rates-now b { color:var(--ink); font-weight:650; }
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

        /* Daily Target (§9b) — relocated here 2026-09-22. Header content (solar chips,
           master slider, expand button) is spliced directly into .hd above as its own
           direct children — see _dtInlineHtml() — so it's part of the SAME row as the
           title/badge, not a second row below it. .dt-block below is only the expanded
           per-device panel, which legitimately gets its own space underneath. */
        .dt-block { margin-top:10px; padding-top:10px; border-top:1px solid var(--border); }
        /* Single-line chip — icon + value only, condition/label live in the title=""
           tooltip. The first pass's 3-line box (icon + label + value + condition) was
           part of why two rows were unavoidable at normal card widths. */
        .dt-solar-chip { flex:0 0 auto; display:flex; align-items:center; gap:5px;
                        border:1px solid var(--border); border-radius:20px; padding:4px 9px; }
        .dt-solar-chip ha-icon { --mdc-icon-size:16px; color:var(--good); flex:0 0 auto; }
        .dt-solar-chip.dim ha-icon { color:var(--ink2); opacity:.5; }
        .dt-solar-value { font-size:12px; font-weight:650; color:var(--ink); white-space:nowrap; }
        /* Compact master control — label + slider + % inline, no wrapping kWh readout
           (unlike .dt-slider-wrap/.dt-meta below, which the per-device panel rows use and
           deliberately DO force the readout onto its own line). flex:1 1 … so it's the
           element that actually grows to fill whatever room is left in the row. */
        .dt-master-inline { display:flex; align-items:center; gap:7px; flex:1 1 140px; min-width:110px; }
        .dt-master-inline input[type=range] { flex:1 1 auto; accent-color:var(--good); }
        .dt-master-name { font-size:12px; font-weight:600; color:var(--ink); flex:0 0 auto; white-space:nowrap; }
        .dt-slider-wrap { display:flex; align-items:center; gap:8px; flex:1 1 140px; min-width:120px; }
        .dt-slider-wrap input[type=range] { flex:1 1 auto; accent-color:var(--good); }
        .dt-pct { font-size:12.5px; font-weight:600; color:var(--ink); width:38px; text-align:right;
                  font-variant-numeric:tabular-nums; }
        .dt-meta { font-size:10.5px; color:var(--ink2); flex:0 0 100%; }
        .dt-meta.dt-note { color:var(--buy); }
        .dt-expand-btn { display:flex; align-items:center; justify-content:center;
                         width:26px; height:26px; border-radius:7px; border:1px solid var(--border);
                         background:transparent; color:var(--ink2); cursor:pointer; flex:0 0 auto; }
        .dt-expand-btn:hover { border-color:var(--good); color:var(--good); }
        .dt-panel { margin-top:8px; display:flex; flex-direction:column; gap:2px; }
        .dt-row { display:flex; align-items:center; gap:10px; padding:7px 2px;
                  border-top:1px solid var(--border); flex-wrap:wrap; }
        .dt-info { flex:1 1 110px; min-width:0; }
        .dt-name { font-size:12.5px; font-weight:550; color:var(--ink); }
        .dt-reset-btn { font-size:10px; font-weight:600; padding:3px 7px; border-radius:6px;
                        border:1px solid var(--border); background:transparent; color:var(--ink2);
                        cursor:pointer; font-family:inherit; flex:0 0 auto; }
        .dt-reset-btn:hover { border-color:var(--good); color:var(--good); }
        .dt-empty { padding:8px 2px; font-size:11.5px; color:var(--ink2); }

        /* Load control (FEATURES.md §6/§6a) — merged into the per-device panel 2026-09-24
           from grid-lens-load-control-card.js's own row. Ported verbatim from that card's
           CSS with every class 'dt-'-prefixed: this card's OWN '.row' already means
           something else ('.modeline .row', the mode-transition timeline below), so bare
           class names here would leak through that compound selector and corrupt it — see
           chart-common.js's "Load control helpers" section header for the full story.
           NOTE: no backtick characters anywhere in this comment block — it lives inside
           this method's own shadowRoot.innerHTML template literal, so a literal backtick
           here closes that string early and corrupts everything after it (found live
           2026-09-24, a Firefox "SyntaxError: unexpected token: identifier" thrown loading
           this exact file). '.card { position: relative }' is needed for the tooltip's own
           position math (attachTooltip below) — this card had no tooltip before this merge. */
        .card { position: relative; }
        .dt-row .gbar { flex: 0 0 auto; width: 42px; height: 4px; border-radius: 2px;
                background: var(--border); overflow: hidden; }
        .dt-row .gbar > span { display: block; height: 100%; background: var(--good); }
        .dt-row .greedy-line { font-size: 11px; color: var(--ink2); opacity: .8; margin-top: 1px;
                            display: flex; align-items: center; gap: 6px; }
        .dt-row .greedy-line.active { color: var(--good); opacity: 1; }
        .dt-row .soc-cap { font-size: 11px; color: var(--buy); opacity: .9; margin-top: 1px;
                        display: flex; align-items: center; gap: 5px; }
        .dt-row .soc-cap ha-icon { --mdc-icon-size: 14px; }
        .dt-sw { position: relative; flex: 0 0 auto; width: 40px; height: 22px; border-radius: 12px;
              background: var(--border); cursor: pointer; transition: background .15s ease; }
        .dt-sw::after { content: ''; position: absolute; top: 2px; left: 2px; width: 18px; height: 18px;
              border-radius: 50%; background: var(--surface); box-shadow: 0 1px 3px rgba(0,0,0,.3);
              transition: transform .15s ease; }
        .dt-sw.on { background: var(--good); }
        .dt-sw.on::after { transform: translateX(18px); }
        .dt-sw.unavail { opacity: .45; cursor: default; }
        .dt-ovr { display: flex; gap: 0; flex: 0 0 auto; border: 1px solid var(--border);
               border-radius: 9px; overflow: hidden; }
        .dt-ovr button { font-size: 10.5px; font-weight: 600; padding: 4px 9px; border: none;
               background: transparent; color: var(--ink2); cursor: pointer;
               font-family: inherit; border-left: 1px solid var(--border); }
        .dt-ovr button:first-child { border-left: none; }
        .dt-ovr button.active { background: var(--good); color: #fff; }
        .dt-ovr button.active.off { background: var(--buy); }
        .dt-ovr.disabled { cursor: not-allowed; }
        .dt-ovr.disabled button { opacity: .4; cursor: not-allowed; }
        .dt-greedy { display: flex; gap: 4px; flex: 0 0 auto; }
        .dt-greedy .dt-gbtn { display: flex; align-items: center; justify-content: center;
               width: 26px; height: 26px; border-radius: 7px; border: 1px solid var(--border);
               background: transparent; color: var(--ink2); cursor: pointer; }
        .dt-gbtn.on { background: var(--good); color: #fff; border-color: var(--good); }
        .dt-gbtn ha-icon { --mdc-icon-size: 15px; }
        .dt-gbtn.disabled { opacity: .35; cursor: not-allowed; }
        .dt-boost { display: flex; align-items: center; gap: 3px; flex: 0 0 auto;
               border: 1px solid var(--border); border-radius: 7px; padding: 3px 7px; }
        .dt-boost.active { border-color: var(--good); }
        .dt-boost.over { border-color: var(--buy); }
        .dt-boost-cap { font-size: 10px; font-weight: 600; color: var(--buy); white-space: nowrap; }
        .dt-boost-input { width: 42px; border: none; background: transparent; color: var(--ink);
               font-size: 12px; font-family: inherit; text-align: right; }
        .dt-boost-input::-webkit-outer-spin-button, .dt-boost-input::-webkit-inner-spin-button { margin: 0; }
        .dt-boost-unit { font-size: 10px; color: var(--ink2); }
        .dt-modcur { font-size: 11px; font-weight: 600; color: var(--ink); white-space: nowrap;
               border: 1px solid var(--border); border-radius: 7px; padding: 3px 8px;
               flex: 0 0 auto; font-variant-numeric: tabular-nums;
               min-width: 100px; box-sizing: border-box; text-align: center; }
        .dt-maxcur { display: flex; align-items: center; gap: 3px; flex: 0 0 auto;
               border: 1px solid var(--border); border-radius: 7px; padding: 3px 7px;
               min-width: 84px; box-sizing: border-box; }
        .dt-maxcur-label { font-size: 10px; color: var(--ink2); }
        .dt-maxcur-input { width: 32px; border: none; background: transparent; color: var(--ink);
               font-size: 12px; font-family: inherit; text-align: right; }
        .dt-maxcur-input::-webkit-outer-spin-button, .dt-maxcur-input::-webkit-inner-spin-button { margin: 0; }
        .dt-maxcur-unit { font-size: 10px; color: var(--ink2); }
        .dt-modcur.ph, .dt-maxcur.ph, .dt-gbtn.ph { visibility: hidden; border-color: transparent; }
        /* Sparkline — fixed width (14 bars * 4px + 13 gaps * 1.5px = 75.5px) regardless of
           how many real days came back, matching the fix applied to
           grid-lens-load-control-card.js's own copy for the same reason (2026-09-24
           misalignment bug) — sparklineHtml() pads with invisible '.dt-sbar.ph' bars. */
        .dt-spark { display: flex; flex-direction: column; align-items: center; gap: 2px;
                 flex: 0 0 auto; padding: 0 2px; }
        .dt-sbars { display: flex; align-items: flex-end; gap: 1.5px; height: 22px; min-width: 75.5px; }
        .dt-sbar { width: 4px; min-height: 1.5px; background: var(--ink2); opacity: .5;
                border-radius: 1px 1px 0 0; }
        .dt-sbar.today { background: var(--ink); opacity: .85; }
        .dt-sbar.ph { visibility: hidden; }
        .dt-spark-avg { font-size: 9.5px; color: var(--ink2); white-space: nowrap; }
        .dt-spark-ph { width: 75.5px; height: 22px; }
        .dt-est-toggle.on { background: var(--ink); color: var(--surface); border-color: var(--ink); }
        .dt-est-panel { flex: 1 1 100%; margin: 2px 0 6px 34px; padding: 10px 12px;
               border: 1px solid var(--border); border-radius: 9px; background: var(--panel-bg, rgba(127,127,127,.06)); }
        .dt-est-stats { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-bottom: 8px; }
        .dt-est-stat { display: flex; flex-direction: column; gap: 1px; }
        .dt-est-stat .v { font-size: 13px; font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; }
        .dt-est-stat .l { font-size: 9.5px; color: var(--ink2); text-transform: uppercase; letter-spacing: .02em; }
        .dt-est-chart { margin: 4px 0 8px; }
        .dt-est-chart .chart-svg { width: 100%; height: 90px; display: block; }
        .dt-est-empty { font-size: 11.5px; color: var(--ink2); font-style: italic; padding: 4px 0; }
        .dt-est-samples { display: flex; flex-direction: column; gap: 3px; }
        .dt-est-sample-hd { font-size: 9.5px; color: var(--ink2); text-transform: uppercase; letter-spacing: .02em; margin-bottom: 2px; }
        .dt-est-sample { display: flex; align-items: center; gap: 8px; font-size: 11.5px; color: var(--ink); padding: 2px 0; }
        .dt-est-sample .t { color: var(--ink2); flex: 0 0 64px; }
        .dt-est-sample .d { flex: 0 0 64px; font-variant-numeric: tabular-nums; }
        .dt-est-sample .r { flex: 1 1 auto; font-size: 10.5px; }
        .dt-est-sample.ok .r { color: var(--good); }
        .dt-est-sample.rej .r { color: var(--buy); }
        [data-tip] { outline: none; }
        [data-tip]:focus-visible { box-shadow: 0 0 0 2px var(--good); border-radius: 4px; }
        .dt-tt-pop { position: absolute; left: 0; top: 0; transform: translate(-50%, calc(-100% - 8px));
               background: var(--ink); color: var(--surface); font-size: 11px; font-weight: 500;
               line-height: 1.4; padding: 6px 9px; border-radius: 7px; max-width: 230px;
               white-space: normal; pointer-events: none; opacity: 0; visibility: hidden;
               box-shadow: 0 4px 14px rgba(0,0,0,.28); z-index: 30; transition: opacity .08s ease; }
        .dt-tt-pop.show { opacity: 1; visibility: visible; }
      </style>
      <div class="card"><div class="body"></div><div class="dt-tt-pop"></div></div>
    `;
    // Delegated: _paint() replaces .body's innerHTML on every repaint, so per-element
    // listeners would be torn off. The listeners live on .body, which survives.
    const body = this.shadowRoot.querySelector('.body');
    if (body) {
      body.addEventListener('click', (ev) => this._onChipClick(ev));
      body.addEventListener('click', (ev) => this._onDtExpandClick(ev));
      body.addEventListener('click', (ev) => this._onDtResetClick(ev));
      body.addEventListener('input', (ev) => this._onDtSliderInput(ev));
      body.addEventListener('change', (ev) => this._onDtSliderChange(ev));
      body.addEventListener('click', (ev) => this._onDtEstToggleClick(ev));
      body.addEventListener('click', (ev) => this._onDtGreedyClick(ev));
      body.addEventListener('click', (ev) => this._onDtOverrideClick(ev));
      body.addEventListener('click', (ev) => this._onDtSwitchClick(ev));
      body.addEventListener('change', (ev) => this._onDtBoostInputChange(ev));
      body.addEventListener('keydown', (ev) => this._onDtBoostInputKeydown(ev));
      body.addEventListener('change', (ev) => this._onDtMaxCurInputChange(ev));
      body.addEventListener('keydown', (ev) => this._onDtMaxCurInputKeydown(ev));
    }
    attachTooltip(this.shadowRoot, { popupClass: 'dt-tt-pop' });
  }

  _paint() {
    const body = this.shadowRoot && this.shadowRoot.querySelector('.body');
    if (!body) return;
    const s = this._summary || {};

    const title = this._config.title ? esc(this._config.title) : 'Battery Plan &amp; SOC Forecast';
    const header = `
      <div class="hd">
        <div class="hd-plan">
          <div class="title">${title}</div>
          <div class="sub">${s.plan_name ? esc(s.plan_name) : 'Grid Lens advisory'}${s.solver ? ' · ' + esc(s.solver) : ''}${s.generated_at ? ' · ' + fmtTime(s.generated_at) : ''}</div>
          ${this._currentRatesHtml()}
        </div>
        ${this._dtInlineHtml()}
        <div class="hd-right">
          ${this._toggleChipsHtml()}
          ${Date.now() < this._optDotUntil ? '<span class="opt-dot" title="Optimizer is running — recalculating the plan now"></span>' : ''}
          <div class="badge ${s.restored ? 'stale' : (s.status === 'ok' ? 'ok' : '')}">${s.restored ? 'LAST PLAN' : esc((s.status || 'unknown').toUpperCase())}</div>
        </div>
      </div>
      ${this._dtExpandedHtml()}`;

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
      <div class="note">See the SOC/Power chart cards for the solar/load/price forecast validation. All series are the forecast for the current plan (${s.plan_name ? esc(s.plan_name) : '—'}).</div>`;
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
