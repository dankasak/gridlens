/*
 * Grid Lens Deferrable Load Control Card
 *
 * Relocated 2026-09-24: this card's full per-device row content (sparkline, Today Boost,
 * Greedy toggles, Off now/On now/Auto, live status lines, the estimator debug panel) now
 * ALSO renders on the Power Flow page's "Optimiser & Plan" header
 * (grid-lens-advisory-card.js), behind the same chevron expander Daily Target already
 * uses there — see that file's `_dt*` methods. This standalone card is no longer seeded
 * onto the default Settings view for that reason (a second copy would just drift), but
 * stays fully functional and registered for anyone using it on a separate dashboard.
 * Every resolver and HTML-producing function below is a thin call into
 * grid-lens-chart-common.js's "Load control helpers" section, shared with
 * grid-lens-advisory-card.js, rather than owned here — the project has already been bitten
 * once by two cards' entity-resolution logic quietly drifting apart (see that section's own
 * header comment), and this merge was the second time.
 *
 * One row per configured deferrable load (controllable or not), each with:
 *   - a 14-day daily-kWh sparkline (client-side recorder statistics fetch, no new
 *     entity) so a user reaching for Today Boost can see what "typical" looks like;
 *   - a Today Boost kWh input (GridLensDeferrableOverrideNumber — see number.py),
 *     merged in from the formerly-separate Boost Tuning card/tiles 2026-07-31;
 *   - Greedy Consumption / Greedy Respects Schedule toggles, when the device has a
 *     control switch configured;
 *   - a single segmented control: Off now / On now (manual override — force the
 *     appliance and stop GridLens driving it) and Auto (GridLens schedules the load).
 * A device with no control switch configured still gets a row (boost + name/meta), but
 * the segmented control renders disabled with a tooltip explaining why — previously
 * such devices were invisible on this card entirely, which read as "load control isn't
 * working" rather than "this device just isn't wired for control yet".
 *
 * Auto-discovered from the `deferrable_loads` attribute published by the GridLens cost
 * sensor (sensor.py's _build_deferrable_loads — same source `grid-lens-defer-schedule-
 * card.js`/the old boost-tuning-card used), so this card needs zero editing as loads are
 * added, removed, or reconfigured on any install — including a fresh install with none
 * configured yet, which renders a helpful empty state rather than a blank card.
 *
 * Per-device entity resolution, all joined on the device's `switch_entity`/`energy_entity`
 * (never a naming convention — installs vary in retailer/hardware/device count, per the
 * project's generic-design rule):
 *   - control switch: `switch.*` with `switch === switch_entity` and `on_threshold_w` in
 *     attrs (GridLensDeferrableLoadSwitch / DeferrableLoadController.status()).
 *   - override select: `select.*` with `switch === switch_entity` and `override` in attrs
 *     (GridLensLoadOverrideSelect).
 *   - greedy toggles: `switch.*` with `switch === switch_entity` and `role === 'greedy'` /
 *     `'greedy_schedule'` / `'greedy_surplus'` (GridLensDeferrableGreedySwitch /
 *     …ScheduleSwitch / …SurplusSwitch).
 *   - boost number: `number.*` with `deferrable_sensor_id === energy_entity`
 *     (GridLensDeferrableOverrideNumber).
 *
 * Modulating (current-controlled) devices — `d.control_type === 'modulating'` (a
 * `number.*` setpoint entity, e.g. an OCPP/Zaptec/Wallbox EV charger driven continuously
 * in amps rather than switched on/off; see MODULATING_CONTRACT.md) — get the exact same
 * row shape as every other device (segmented Off now/On now/Auto + all 3 Greedy buttons,
 * never restructured or omitted per FEATURES.md §6), plus three additions appended after
 * the existing sparkline/boost elements:
 *   - a live current/power readout chip, read directly off `d.setpoint_entity` and
 *     `d.power_entity` (both already resolved server-side, no new discovery needed);
 *   - a max-current ceiling number input (`number.*_<device>_max_current` —
 *     MODULATING_CONTRACT.md §6). The contract does not pin down this entity's discovery
 *     fingerprint the way it documents Today Boost's, so this mirrors the closest existing
 *     convention: joined on the same `deferrable_sensor_id` energy-sensor key Today Boost
 *     already uses (a per-device ceiling is meaningful the same way a daily-kWh override
 *     is), disambiguated from the Boost number by a `role === 'max_current'` marker, the
 *     same way switch.py's three greedy switches already share one `switch` join key and
 *     disambiguate with `role`. If number.py lands with a different attribute shape, only
 *     `_maxCurrentFor()` below needs to change.
 *   - a one-line "why" readout (`modulation_source`: plan/surplus/override/off, and
 *     `plugged_in`) sourced from the control switch's own attributes — same entity/field
 *     the greedy status line already reads, no extra lookup.
 * A modulating device's physical join key (`phys`, used to find the control switch/select/
 * greedy switches) falls back to `d.setpoint_entity` when `d.switch_entity` is empty — the
 * common case per the contract ("setpoint present + no switch" — an OCPP charger has no
 * separate on/off switch, `maximum_current: 0` stops delivery). Without this fallback every
 * switchless modulating device's `phys` would be `null`/`""`, which would either fail to
 * resolve those entities at all, or — worse — collide with every OTHER switchless
 * modulating device on the same install sharing the same empty join key. See
 * `_resolveRows()` for the exact fallback.
 *
 * Config:
 *   type: custom:grid-lens-load-control-card
 *   title: Deferrable Loads          (optional)
 */
import {
  STYLE, esc, resolveDeferrableLoads, resolveLoadControlRows, socCapFor, boostCeiling,
  estimatorFor, friendlyNote, fetchDailyHistory, greedyLine, modulationLine,
  currentReadoutHtml, maxCurrentHtml, socCapHtml, sparklineHtml, estimatorToggleHtml,
  estimatorPanelHtml, controlHtml, greedyButtonsHtml, boostInputHtml, attachTooltip,
} from './grid-lens-chart-common.js?v=20260924d';

const HISTORY_REFRESH_MS = 15 * 60000;

class GridLensLoadControlCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._sig = '';
    this._dark = false;
    // Per-device daily-kWh history for the sparkline, keyed by energy_entity — fetched
    // lazily and cached, since it's a stats query and not part of the live hass state
    // (nothing pushes an update when a new day's statistic lands, so it's refreshed on a
    // timer rather than on every hass tick).
    this._historyCache = {};
    this._historyPending = new Set();
    // Which rows have their LoadEstimator debug panel open — keyed by the device's
    // energy_entity, purely local UI state (no entity write), same "Set of keys" shape
    // as _historyPending. Never cleared on repaint so it survives every hass tick.
    this._expandedEstimator = new Set();
  }

  setConfig(config) {
    this._config = Object.assign({ title: 'Deferrable Loads' }, config);
    this._renderShell();
  }

  getCardSize() { return Math.max(2, (this._rows || []).length + 1); }

  // Kicks off (or reuses) a history fetch for every row's energy sensor. Cheap to call on
  // every hass tick — a pending fetch or a cache entry younger than HISTORY_REFRESH_MS is a
  // no-op — so it can just live at the top of the hass setter rather than needing its own
  // scheduling.
  _pollHistory(rows) {
    for (const r of rows) {
      const eid = r.device.energy_entity;
      if (!eid || this._historyPending.has(eid)) continue;
      const cached = this._historyCache[eid];
      if (cached && Date.now() - cached.ts < HISTORY_REFRESH_MS) continue;
      this._historyPending.add(eid);
      fetchDailyHistory(this._hass, eid).then((days) => {
        this._historyPending.delete(eid);
        this._historyCache[eid] = { ts: Date.now(), days };
        this._paint();
      });
    }
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this.classList.toggle('dark', dark); }

    // Deliberately NOT re-sorted (e.g. alphabetically) — devices stay in the same order
    // as the `deferrable_loads` attribute itself, matching every other card that reads
    // it (grid-lens-defer-schedule-card.js, the Power Flow card, the Power Chart card).
    const devices = resolveDeferrableLoads(hass);
    const rows = resolveLoadControlRows(hass, devices);
    this._pollHistory(rows);
    const sig = rows.map((r) => {
      const d = r.device;
      const c = r.controlEid && hass.states[r.controlEid];
      const s = r.selEid && hass.states[r.selEid];
      const g = r.gEid && hass.states[r.gEid];
      const gs = r.gsEid && hass.states[r.gsEid];
      const gf = r.gfEid && hass.states[r.gfEid];
      const b = r.boostEid && hass.states[r.boostEid];
      const ceiling = boostCeiling(r.device);
      // Modulating-only live fields — a device's own setpoint/power readings and its
      // max-current ceiling, none of which are covered by the control switch's `c`
      // signature above. Gated on control_type so a non-modulating row's signature is
      // byte-for-byte identical to before this feature (no spurious repaints, no risk of
      // ever tripping the "must render exactly as it does today" requirement).
      const isMod = d.control_type === 'modulating';
      const mc = isMod && r.maxCurEid ? hass.states[r.maxCurEid] : null;
      const sp = isMod && d.setpoint_entity ? hass.states[d.setpoint_entity] : null;
      const pw = isMod && d.power_entity ? hass.states[d.power_entity] : null;
      // LoadEstimator fields — sample_count/last_sample_at change only when a new
      // observation lands (rarely, on the order of minutes), so they're a cheap proxy
      // for "sample_history changed" without needing to serialize the whole array into
      // the signature on every tick. null (join renders as '') for a directly-metered
      // device, which never repaints from this alone.
      const est = estimatorFor(hass, d);
      const estA = est && est.attrs;
      const sc = socCapFor(hass, d.energy_entity);
      return [
        r.device.energy_entity,
        sc ? `${sc.day0_charge_kwh}|${sc.target_kwh}|${sc.max_percent}|${sc.initial_percent}` : '',
        // greedy_reason / forecast_free_kwh live in the control switch's ATTRIBUTES and
        // move while its state stays "on", so they need to be in the signature or the
        // greedy status line would freeze at whatever it said on the last state change.
        // modulation_source / plugged_in are the same story for the modulating "why" line.
        c ? [c.state, (c.attributes || {}).note, (c.attributes || {}).greedy_reason,
             (c.attributes || {}).greedy_blocked,
             (c.attributes || {}).forecast_free_kwh,
             (c.attributes || {}).modulation_source,
             (c.attributes || {}).plugged_in].join('|') : '',
        s ? s.state : '',
        g ? g.state : '',
        gs ? gs.state : '',
        gf ? gf.state : '',
        b ? b.state : '',
        // Time-dependent (the window rolls forward), so it belongs in the repaint
        // signature — otherwise the ceiling hint goes stale as the day advances.
        ceiling == null ? '' : ceiling.toFixed(1),
        mc ? mc.state : '',
        sp ? sp.state : '',
        pw ? pw.state : '',
        estA ? `${estA.sample_count}|${estA.last_sample_at}|${estA.estimated_kw}|${est.state.state}` : '',
      ].join('~');
    }).join(',');
    if (sig !== this._sig) { this._sig = sig; this._rows = rows; this._paint(); }
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}
        .rows { display: flex; flex-direction: column; gap: 2px; margin-top: 8px; }
        .row { display: flex; align-items: center; gap: 10px; padding: 9px 2px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
        .row:last-child { border-bottom: none; }
        .row .icon { color: var(--ink2); flex: 0 0 auto; }
        .row .icon.dim { opacity: .4; }
        .row .info { flex: 1 1 120px; min-width: 0; }
        .row .name { font-size: 13.5px; font-weight: 550; color: var(--ink); }
        .row .meta { font-size: 11px; color: var(--ink2); margin-top: 1px; }
        .row .meta.err { color: var(--buy); }
        /* Greedy status line — same size as .meta but muted further, so it reads as a
           sub-note of the row rather than competing with the control state above it.
           Turns the accent colour only when greedy is actually the reason the load is on. */
        .row .greedy-line { font-size: 11px; color: var(--ink2); opacity: .8; margin-top: 1px;
                            display: flex; align-items: center; gap: 6px; }
        .row .greedy-line.active { color: var(--good); opacity: 1; }
        /* "SOC-limited" line — same meta-line shape as .greedy-line, tinted like .boost.over
           (the "your input can't fully apply" colour) since it's the same class of message:
           the schedule is doing less than the numbers next to it suggest, and here's why. */
        .row .soc-cap { font-size: 11px; color: var(--buy); opacity: .9; margin-top: 1px;
                        display: flex; align-items: center; gap: 5px; }
        .row .soc-cap ha-icon { --mdc-icon-size: 14px; }
        /* Progress toward the forecast-surplus trigger: the bar is the point — a number
           pair alone doesn't convey "nearly there" at a glance. */
        .gbar { flex: 0 0 auto; width: 42px; height: 4px; border-radius: 2px;
                background: var(--border); overflow: hidden; }
        .gbar > span { display: block; height: 100%; background: var(--good); }
        .sw { position: relative; flex: 0 0 auto; width: 40px; height: 22px; border-radius: 12px;
              background: var(--border); cursor: pointer; transition: background .15s ease; }
        .sw::after { content: ''; position: absolute; top: 2px; left: 2px; width: 18px; height: 18px;
              border-radius: 50%; background: var(--surface); box-shadow: 0 1px 3px rgba(0,0,0,.3);
              transition: transform .15s ease; }
        .sw.on { background: var(--good); }
        .sw.on::after { transform: translateX(18px); }
        .sw.unavail { opacity: .45; cursor: default; }
        .ovr { display: flex; gap: 0; flex: 0 0 auto; border: 1px solid var(--border);
               border-radius: 9px; overflow: hidden; }
        .ovr button { font-size: 10.5px; font-weight: 600; padding: 4px 9px; border: none;
               background: transparent; color: var(--ink2); cursor: pointer;
               font-family: inherit; border-left: 1px solid var(--border); }
        .ovr button:first-child { border-left: none; }
        .ovr button.active { background: var(--good); color: #fff; }
        .ovr button.active.off { background: var(--buy); }
        .ovr.disabled { cursor: not-allowed; }
        .ovr.disabled button { opacity: .4; cursor: not-allowed; }
        .greedy { display: flex; gap: 4px; flex: 0 0 auto; }
        .greedy .gbtn { display: flex; align-items: center; justify-content: center;
               width: 26px; height: 26px; border-radius: 7px; border: 1px solid var(--border);
               background: transparent; color: var(--ink2); cursor: pointer; }
        .greedy .gbtn.on { background: var(--good); color: #fff; border-color: var(--good); }
        .greedy .gbtn ha-icon { --mdc-icon-size: 15px; }
        /* Always rendered, even for a non-controllable device — same disabled treatment as
           .ovr.disabled, so every row keeps the same three-icon width and rows line up. */
        .greedy .gbtn.disabled { opacity: .35; cursor: not-allowed; }
        .boost { display: flex; align-items: center; gap: 3px; flex: 0 0 auto;
               border: 1px solid var(--border); border-radius: 7px; padding: 3px 7px; }
        .boost.active { border-color: var(--good); }
        .boost.over { border-color: var(--buy); }
        .boost-cap { font-size: 10px; font-weight: 600; color: var(--buy); white-space: nowrap; }
        .boost-input { width: 42px; border: none; background: transparent; color: var(--ink);
               font-size: 12px; font-family: inherit; text-align: right; }
        .boost-input::-webkit-outer-spin-button, .boost-input::-webkit-inner-spin-button { margin: 0; }
        .boost-unit { font-size: 10px; color: var(--ink2); }
        /* Modulating-device additions (current-controlled EV chargers etc — see the file
           header comment). Same chip look as .boost so they read as siblings rather than
           a bolted-on new style, but their own classes since they're not a boost value. */
        .modcur { font-size: 11px; font-weight: 600; color: var(--ink); white-space: nowrap;
               border: 1px solid var(--border); border-radius: 7px; padding: 3px 8px;
               flex: 0 0 auto; font-variant-numeric: tabular-nums;
               min-width: 100px; box-sizing: border-box; text-align: center; }
        .maxcur { display: flex; align-items: center; gap: 3px; flex: 0 0 auto;
               border: 1px solid var(--border); border-radius: 7px; padding: 3px 7px;
               min-width: 84px; box-sizing: border-box; }
        .maxcur-label { font-size: 10px; color: var(--ink2); }
        .maxcur-input { width: 32px; border: none; background: transparent; color: var(--ink);
               font-size: 12px; font-family: inherit; text-align: right; }
        .maxcur-input::-webkit-outer-spin-button, .maxcur-input::-webkit-inner-spin-button { margin: 0; }
        .maxcur-unit { font-size: 10px; color: var(--ink2); }
        /* Reserved-but-empty variant of either box above (added 2026-09-11) — a
           non-modulating row (or a modulating one whose max-current entity hasn't resolved
           yet) still occupies the same min-width, so every *other* column on the row (the
           greedy icons, Today Boost, the control segment) lines up across every row
           regardless of which ones happen to be modulating. */
        .modcur.ph, .maxcur.ph, .gbtn.ph { visibility: hidden; border-color: transparent; }
        /* Daily-kWh sparkline: how much this device has actually drawn per day over the
           last two weeks, so a user reaching for Today Boost has a number to react to
           instead of guessing. Deliberately not colour-coded per device (unlike the
           schedule/power-flow cards) — this is a single-series magnitude read, not an
           identity to keep consistent across cards.
           Fixed width (14 bars * 4px + 13 gaps * 1.5px = 75.5px), NOT sized to however many
           real days came back — a device with less than a full 14-day history used to
           render a narrower `.sbars` (or nothing at all on a failed/empty query), which
           shifted the Today Boost box/Greedy icons/segmented control leftward on that row
           relative to a full-history row. sparklineHtml() (chart-common.js) now always pads
           to a full 14 slots with invisible `.sbar.ph` bars — found from a screenshot
           showing exactly this misalignment (2026-09-24). */
        .spark { display: flex; flex-direction: column; align-items: center; gap: 2px;
                 flex: 0 0 auto; padding: 0 2px; }
        .sbars { display: flex; align-items: flex-end; gap: 1.5px; height: 22px; min-width: 75.5px; }
        .sbar { width: 4px; min-height: 1.5px; background: var(--ink2); opacity: .5;
                border-radius: 1px 1px 0 0; }
        .sbar.today { background: var(--ink); opacity: .85; }
        .sbar.ph { visibility: hidden; }
        .spark-avg { font-size: 9.5px; color: var(--ink2); white-space: nowrap; }
        .spark-ph { width: 75.5px; height: 22px; }
        /* LoadEstimator debug panel — toggle button on a row backed by an estimator
           (no real energy sensor), and the expanded detail block underneath it. Reuses
           .gbtn's icon-button look for the toggle so it reads as a sibling of the
           Greedy icons rather than a new control language. */
        .est-toggle.on { background: var(--ink); color: var(--surface); border-color: var(--ink); }
        .est-panel { flex: 1 1 100%; margin: 2px 0 6px 34px; padding: 10px 12px;
               border: 1px solid var(--border); border-radius: 9px; background: var(--panel-bg, rgba(127,127,127,.06)); }
        .est-stats { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-bottom: 8px; }
        .est-stat { display: flex; flex-direction: column; gap: 1px; }
        .est-stat .v { font-size: 13px; font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; }
        .est-stat .l { font-size: 9.5px; color: var(--ink2); text-transform: uppercase; letter-spacing: .02em; }
        .est-chart { margin: 4px 0 8px; }
        .est-chart .chart-svg { width: 100%; height: 90px; display: block; }
        .est-empty { font-size: 11.5px; color: var(--ink2); font-style: italic; padding: 4px 0; }
        .est-samples { display: flex; flex-direction: column; gap: 3px; }
        .est-sample-hd { font-size: 9.5px; color: var(--ink2); text-transform: uppercase; letter-spacing: .02em; margin-bottom: 2px; }
        .est-sample { display: flex; align-items: center; gap: 8px; font-size: 11.5px; color: var(--ink); padding: 2px 0; }
        .est-sample .t { color: var(--ink2); flex: 0 0 64px; }
        .est-sample .d { flex: 0 0 64px; font-variant-numeric: tabular-nums; }
        .est-sample .r { flex: 1 1 auto; font-size: 10.5px; }
        .est-sample.ok .r { color: var(--good); }
        .est-sample.rej .r { color: var(--buy); }
        /* Custom tooltip: a JS-delegated popup (see _initTooltip) standing in for the
           native title="" tooltip everywhere on this card. Native title tooltips have a
           long, browser-controlled show delay (~1s in Chrome) and are unreliable to
           trigger on touch; this shows fast on hover and instantly on keyboard/touch
           focus, and [data-tip] elements get tabindex so touch/keyboard can reach them. */
        .card { position: relative; }
        [data-tip] { outline: none; }
        [data-tip]:focus-visible { box-shadow: 0 0 0 2px var(--good); border-radius: 4px; }
        .tt-pop { position: absolute; left: 0; top: 0; transform: translate(-50%, calc(-100% - 8px));
               background: var(--ink); color: var(--surface); font-size: 11px; font-weight: 500;
               line-height: 1.4; padding: 6px 9px; border-radius: 7px; max-width: 230px;
               white-space: normal; pointer-events: none; opacity: 0; visibility: hidden;
               box-shadow: 0 4px 14px rgba(0,0,0,.28); z-index: 30; transition: opacity .08s ease; }
        .tt-pop.show { opacity: 1; visibility: visible; }
      </style>
      <div class="card"><div class="body"></div><div class="tt-pop"></div></div>
    `;
    attachTooltip(this.shadowRoot);
  }

  _paint() {
    const body = this.shadowRoot && this.shadowRoot.querySelector('.body');
    if (!body) return;
    const hass = this._hass;
    const rows = this._rows || [];

    const header = `
      <div class="hd">
        <div class="title">${esc(this._config.title)}</div>
        ${rows.length ? `<div class="sub">${rows.length} configured</div>` : ''}
      </div>`;

    if (!rows.length) {
      body.innerHTML = header + `<div class="waiting">No deferrable loads configured yet.</div>`;
      return;
    }

    const rowsHtml = rows.map((r) => {
      const d = r.device;
      const controlSt = r.controlEid ? hass.states[r.controlEid] : null;
      const a = controlSt ? (controlSt.attributes || {}) : {};
      const note = friendlyNote(a.note);
      const isErr = (a.note || '').startsWith('command_error');
      // "Controlling"/"Not controlling" + the driven entity id was dropped 2026-09-11 —
      // pure boilerplate that repeated on every row (the master switch above already
      // shows on/off) and widened rows enough to break the card's layout. The note
      // itself stays: for a modulating device it's usually redundant with the modulation
      // "why" line below, but for an on/off device it's the only diagnostic text on the
      // row (e.g. a command_error), so it's worth the one line when non-empty.
      const meta = d.controllable ? note : 'Forecast only — no control switch configured';
      // Second meta line: the live greedy story. Third, modulating-only: why the current
      // is where it is right now. Fourth: "SOC-limited". All three shared with
      // grid-lens-advisory-card.js's merged panel via chart-common.js.
      const greedyLineHtml = greedyLine(a);
      const modLine = modulationLine(a, d);
      const socCapLine = socCapHtml(hass, d);

      // 14-day daily-kWh sparkline — shown for every device with an energy sensor, right
      // beside the boost input it's meant to inform.
      const sparkHtml = sparklineHtml(this._historyCache[d.energy_entity], d.energy_entity);

      // LoadEstimator debug toggle + panel — only for a device whose usage is inferred
      // rather than measured. estimatorToggleHtml renders a same-sized invisible
      // placeholder when there's no estimator, so the segmented control after it doesn't
      // shift row to row depending on whether the previous row had one.
      const est = estimatorFor(hass, d);
      const estToggleHtml = estimatorToggleHtml(est, !!(est && this._expandedEstimator.has(est.eid)));
      const estPanelHtml = est && this._expandedEstimator.has(est.eid)
        ? estimatorPanelHtml(d, est) : '';

      // Modulating-only additions — both render a same-sized placeholder for a
      // non-modulating device, so a row's shape never depends on control_type.
      const currentHtml = currentReadoutHtml(hass, d);
      const maxCurHtml = maxCurrentHtml(hass, r, d);

      // Segmented Off now/On now/Auto control (or its disabled/loading placeholder), the
      // 3 Greedy toggle icons, and the Today Boost override input.
      const controlEl = controlHtml(hass, r, d);
      const greedyHtml = greedyButtonsHtml(hass, r, d);
      const boostHtml = boostInputHtml(hass, r, d);

      return `
        <div class="row" data-eid="${esc(r.controlEid || d.energy_entity)}">
          <ha-icon class="icon${d.controllable ? '' : ' dim'}" icon="mdi:power-plug"></ha-icon>
          <div class="info">
            <div class="name">${esc(d.name || d.energy_entity)}</div>
            ${meta ? `<div class="meta${isErr ? ' err' : ''}">${esc(meta)}</div>` : ''}
            ${greedyLineHtml}
            ${modLine}
            ${socCapLine}
          </div>
          ${sparkHtml}
          ${boostHtml}
          ${currentHtml}
          ${maxCurHtml}
          ${greedyHtml}
          ${estToggleHtml}
          ${controlEl}
          ${estPanelHtml}
        </div>`;
    }).join('');

    body.innerHTML = header + `<div class="rows">${rowsHtml}</div>`;

    // Fallback rows only (no override select found — older integration build).
    body.querySelectorAll('.sw').forEach((el) => {
      el.addEventListener('click', () => {
        const eid = el.getAttribute('data-eid');
        this._hass.callService('switch', 'toggle', { entity_id: eid });
      });
    });
    // [data-eid] excludes the always-rendered disabled placeholders (no entity to toggle).
    body.querySelectorAll('.gbtn[data-eid]').forEach((el) => {
      el.addEventListener('click', () => {
        const eid = el.getAttribute('data-eid');
        this._hass.callService('switch', 'toggle', { entity_id: eid });
      });
    });
    body.querySelectorAll('.ovr button:not([disabled])').forEach((el) => {
      el.addEventListener('click', () => {
        const grp = el.closest('.ovr');
        const sel = grp.getAttribute('data-sel');
        const opt = el.getAttribute('data-opt');
        this._hass.callService('select', 'select_option', { entity_id: sel, option: opt });
        if (opt === 'Auto') {
          // Auto means "GridLens controls it" — also engage the per-device enable
          // switch, which the merged-away toggle used to do.
          this._hass.callService('switch', 'turn_on', {
            entity_id: grp.getAttribute('data-ctl'),
          });
        }
      });
    });
    // LoadEstimator debug panel toggle — local UI state only, no service call.
    body.querySelectorAll('.est-toggle[data-est-eid]').forEach((el) => {
      el.addEventListener('click', () => {
        const eid = el.getAttribute('data-est-eid');
        if (this._expandedEstimator.has(eid)) this._expandedEstimator.delete(eid);
        else this._expandedEstimator.add(eid);
        this._paint();
      });
    });
    body.querySelectorAll('.boost-input').forEach((el) => {
      const commit = () => {
        const eid = el.getAttribute('data-eid');
        let v = parseFloat(el.value);
        if (!Number.isFinite(v) || v < 0) v = 0;
        this._hass.callService('number', 'set_value', { entity_id: eid, value: v });
      };
      el.addEventListener('change', commit);
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter') el.blur(); });
    });
    // Max-current ceiling — same commit-on-change/Enter pattern as the Today Boost input.
    body.querySelectorAll('.maxcur-input').forEach((el) => {
      const commit = () => {
        const eid = el.getAttribute('data-eid');
        let v = parseFloat(el.value);
        const lo = parseFloat(el.min), hi = parseFloat(el.max);
        if (!Number.isFinite(v)) return;
        if (Number.isFinite(lo)) v = Math.max(lo, v);
        if (Number.isFinite(hi)) v = Math.min(hi, v);
        this._hass.callService('number', 'set_value', { entity_id: eid, value: v });
      };
      el.addEventListener('change', commit);
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter') el.blur(); });
    });
  }
}

customElements.define('grid-lens-load-control-card', GridLensLoadControlCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-load-control-card',
  name: 'Grid Lens Load Control',
  description: 'Per-device deferrable load control, Greedy Consumption, and Today Boost target, auto-discovered.',
});
