/*
 * Grid Lens Deferrable Load Control Card
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
import { STYLE, esc } from './grid-lens-chart-common.js?v=20260802b';

const HISTORY_DAYS = 14;
const HISTORY_REFRESH_MS = 15 * 60000;

function friendlyNote(note) {
  if (!note) return '';
  if (note.startsWith('command_error')) return 'Command error';
  return note.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

function localMidnightDaysAgo(n) {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - n);
  return d;
}

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
  }

  setConfig(config) {
    this._config = Object.assign({ title: 'Deferrable Loads' }, config);
    this._renderShell();
  }

  getCardSize() { return Math.max(2, (this._rows || []).length + 1); }

  // Canonical device list — every configured deferrable load, controllable or not (same
  // source as grid-lens-defer-schedule-card.js / the old boost-tuning-card).
  _resolveDevices() {
    const hass = this._hass;
    if (!hass) return [];
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('sensor.')) continue;
      const a = hass.states[eid].attributes;
      if (a && Array.isArray(a.deferrable_loads)) return a.deferrable_loads;
    }
    return [];
  }

  _controlSwitchFor(phys) {
    const hass = this._hass;
    if (!phys) return null;
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('switch.')) continue;
      const a = hass.states[eid].attributes || {};
      if (a.switch === phys && 'on_threshold_w' in a) return eid;
    }
    return null;
  }

  // The manual-override selector paired with a control switch: a select.* entity whose
  // `switch` attribute names the same physical appliance switch (see select.py's
  // GridLensLoadOverrideSelect — the shared `switch` value is the deliberate join key,
  // so no naming convention or per-install config is assumed).
  _overrideSelectFor(phys) {
    const hass = this._hass;
    if (!phys) return null;
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('select.')) continue;
      const a = hass.states[eid].attributes || {};
      if (a.switch === phys && 'override' in a) return eid;
    }
    return null;
  }

  // Greedy Consumption's two per-device toggles (GridLensDeferrableGreedySwitch /
  // GridLensDeferrableGreedyScheduleSwitch — see switch.py): same `switch` join key as
  // the override select above, disambiguated by `role` since both otherwise carry an
  // identically-shaped attribute set.
  _greedySwitchFor(phys, role) {
    const hass = this._hass;
    if (!phys) return null;
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('switch.')) continue;
      const a = hass.states[eid].attributes || {};
      if (a.switch === phys && a.role === role) return eid;
    }
    return null;
  }

  // Today Boost override (GridLensDeferrableOverrideNumber — see number.py), joined on
  // the device's energy sensor rather than the control switch, since a boost target is
  // meaningful even for a forecast-only device with no switch at all.
  _boostFor(energyEntity) {
    const hass = this._hass;
    if (!energyEntity) return null;
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('number.')) continue;
      const a = hass.states[eid].attributes || {};
      if (a.deferrable_sensor_id === energyEntity) return eid;
    }
    return null;
  }

  // Max-current ceiling number (modulating devices only — `number.*_<device>_max_current`,
  // MODULATING_CONTRACT.md §6). See the file header comment for why this join key
  // (`deferrable_sensor_id` + `role`) is a best-effort mirror of two existing conventions
  // rather than a documented fingerprint — the contract doesn't specify one for this entity.
  _maxCurrentFor(d) {
    const hass = this._hass;
    if (!d || d.control_type !== 'modulating' || !d.energy_entity) return null;
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('number.')) continue;
      const a = hass.states[eid].attributes || {};
      if (a.deferrable_sensor_id === d.energy_entity && a.role === 'max_current') return eid;
    }
    return null;
  }

  // Allowed HOURS in the 24 hours starting now, from a device's 7x48 half-hour weekly
  // grid (Monday first). Mirrors schedule_grid.rolling_window_hours on the Python side:
  // this — not "allowed hours per day" — is what bounds a Today Boost, because the
  // optimizer's first day-chunk is anchored to "now", not local midnight, so it spans
  // two weekdays and excludes the part of today already gone. Fails open (counts an
  // unreadable slot as allowed), matching the Python helper.
  _windowHours(week) {
    if (!Array.isArray(week) || week.length !== 7) return 24;
    const now = new Date();
    const wd = (now.getDay() + 6) % 7;   // JS Sunday=0 → Python Monday=0
    const start = wd * 48 + now.getHours() * 2 + (now.getMinutes() >= 30 ? 1 : 0);
    let n = 0;
    for (let k = 0; k < 48; k++) {
      const idx = (start + k) % (7 * 48);
      const row = week[Math.floor(idx / 48)];
      const slot = row ? row[idx % 48] : undefined;
      if (slot === undefined || slot) n++;
    }
    return n / 2;
  }

  // Most kWh this device can physically take in the next 24 h. A boost above this is
  // silently clamped by the LP, so the card shows the ceiling rather than letting the
  // user set a number that quietly does nothing.
  _boostCeiling(d) {
    const kw = parseFloat(d.max_kw);
    if (!Number.isFinite(kw) || kw <= 0) return null;
    return this._windowHours(d.schedule || d.default_schedule) * kw;
  }

  // Daily kWh for the last HISTORY_DAYS days (today's bucket included, partial), from the
  // recorder's own long-term "change" statistic for a total_increasing energy sensor — the
  // exact mechanism advisory/load_history.py uses to build the optimizer's own 14-day
  // average, so the sparkline shows the same number Today Boost is overriding rather than
  // a second, possibly-disagreeing computation. Returns null (not []) on failure so the
  // caller can tell "no data yet" apart from "queried, nothing came back".
  async _fetchHistory(eid) {
    try {
      const res = await this._hass.callWS({
        type: 'recorder/statistics_during_period',
        start_time: localMidnightDaysAgo(HISTORY_DAYS - 1).toISOString(),
        end_time: new Date().toISOString(),
        statistic_ids: [eid],
        period: 'day',
        types: ['change'],
      });
      const rows = (res && res[eid]) || [];
      return rows
        .map((r) => ({ start: new Date(r.start), kwh: r.change == null ? null : Math.max(0, +r.change) }))
        .filter((d) => d.kwh != null && !isNaN(d.start.getTime()));
    } catch (e) {
      return null;
    }
  }

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
      this._fetchHistory(eid).then((days) => {
        this._historyPending.delete(eid);
        this._historyCache[eid] = { ts: Date.now(), days };
        this._paint();
      });
    }
  }

  _resolveRows() {
    const devices = this._resolveDevices();
    // Deliberately NOT re-sorted (e.g. alphabetically) — devices stay in the same order
    // as the `deferrable_loads` attribute itself, matching every other card that reads
    // it (grid-lens-defer-schedule-card.js, the Power Flow card, the Power Chart card).
    // This card used to alphabetize its rows, which put it out of step with those —
    // same device, different row position depending which card you were looking at.
    return devices.map((d) => {
      // Falls back to the setpoint entity when there's no separate on/off switch — the
      // common case for a modulating device (see file header comment). For an on/off
      // device or a modulating device that DOES have a switch configured, this is
      // unchanged from before (switch_entity wins).
      const phys = d.switch_entity || d.setpoint_entity || null;
      return {
        device: d,
        controlEid: this._controlSwitchFor(phys),
        selEid: this._overrideSelectFor(phys),
        gEid: this._greedySwitchFor(phys, 'greedy'),
        gsEid: this._greedySwitchFor(phys, 'greedy_schedule'),
        gfEid: this._greedySwitchFor(phys, 'greedy_surplus'),
        boostEid: this._boostFor(d.energy_entity),
        maxCurEid: this._maxCurrentFor(d),
      };
    });
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this.classList.toggle('dark', dark); }

    const rows = this._resolveRows();
    this._pollHistory(rows);
    const sig = rows.map((r) => {
      const d = r.device;
      const c = r.controlEid && hass.states[r.controlEid];
      const s = r.selEid && hass.states[r.selEid];
      const g = r.gEid && hass.states[r.gEid];
      const gs = r.gsEid && hass.states[r.gsEid];
      const gf = r.gfEid && hass.states[r.gfEid];
      const b = r.boostEid && hass.states[r.boostEid];
      const ceiling = this._boostCeiling(r.device);
      // Modulating-only live fields — a device's own setpoint/power readings and its
      // max-current ceiling, none of which are covered by the control switch's `c`
      // signature above. Gated on control_type so a non-modulating row's signature is
      // byte-for-byte identical to before this feature (no spurious repaints, no risk of
      // ever tripping the "must render exactly as it does today" requirement).
      const isMod = d.control_type === 'modulating';
      const mc = isMod && r.maxCurEid ? hass.states[r.maxCurEid] : null;
      const sp = isMod && d.setpoint_entity ? hass.states[d.setpoint_entity] : null;
      const pw = isMod && d.power_entity ? hass.states[d.power_entity] : null;
      return [
        r.device.energy_entity,
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
               flex: 0 0 auto; font-variant-numeric: tabular-nums; }
        .maxcur { display: flex; align-items: center; gap: 3px; flex: 0 0 auto;
               border: 1px solid var(--border); border-radius: 7px; padding: 3px 7px; }
        .maxcur-label { font-size: 10px; color: var(--ink2); }
        .maxcur-input { width: 32px; border: none; background: transparent; color: var(--ink);
               font-size: 12px; font-family: inherit; text-align: right; }
        .maxcur-input::-webkit-outer-spin-button, .maxcur-input::-webkit-inner-spin-button { margin: 0; }
        .maxcur-unit { font-size: 10px; color: var(--ink2); }
        /* Daily-kWh sparkline: how much this device has actually drawn per day over the
           last two weeks, so a user reaching for Today Boost has a number to react to
           instead of guessing. Deliberately not colour-coded per device (unlike the
           schedule/power-flow cards) — this is a single-series magnitude read, not an
           identity to keep consistent across cards. */
        .spark { display: flex; flex-direction: column; align-items: center; gap: 2px;
                 flex: 0 0 auto; padding: 0 2px; }
        .sbars { display: flex; align-items: flex-end; gap: 1.5px; height: 22px; }
        .sbar { width: 4px; min-height: 1.5px; background: var(--ink2); opacity: .5;
                border-radius: 1px 1px 0 0; }
        .sbar.today { background: var(--ink); opacity: .85; }
        .spark-avg { font-size: 9.5px; color: var(--ink2); white-space: nowrap; }
        .spark-ph { width: 62px; height: 22px; }
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
    this._initTooltip();
  }

  // Fast, delegated replacement for native title="" tooltips (see the .tt-pop CSS above
  // for why). Bound once on the shadow root — mouseover/mouseout/focusin/focusout all
  // bubble, so a single pair of listeners covers every [data-tip] element this card ever
  // renders, including rows repainted later by _paint(). TT_DELAY is the whole point of
  // this: short enough that a tooltip actually appears before the pointer moves on,
  // unlike the native browser default.
  _initTooltip() {
    if (this._ttBound) return;
    this._ttBound = true;
    const TT_DELAY = 150;
    const root = this.shadowRoot;
    let timer = null;
    let shownFor = null;

    const place = (el) => {
      const card = root.querySelector('.card');
      const pop = root.querySelector('.tt-pop');
      if (!card || !pop) return;
      const er = el.getBoundingClientRect();
      const cr = card.getBoundingClientRect();
      const anchorX = er.left - cr.left + er.width / 2;
      pop.style.left = `${anchorX}px`;
      pop.style.top = `${er.top - cr.top}px`;
      pop.classList.add('show');
      requestAnimationFrame(() => {
        const pw = pop.offsetWidth;
        const half = pw / 2;
        let left = anchorX;
        if (left - half < 4) left = 4 + half;
        if (left + half > cr.width - 4) left = cr.width - 4 - half;
        pop.style.left = `${left}px`;
      });
    };
    const show = (el) => {
      const text = el.getAttribute('data-tip');
      const pop = root.querySelector('.tt-pop');
      if (!text || !pop) return;
      pop.textContent = text;
      shownFor = el;
      place(el);
    };
    const hide = (el) => {
      if (el && shownFor !== el) return;
      shownFor = null;
      const pop = root.querySelector('.tt-pop');
      if (pop) pop.classList.remove('show');
    };
    root.addEventListener('mouseover', (e) => {
      const el = e.target.closest('[data-tip]');
      if (!el || el === shownFor) return;
      clearTimeout(timer);
      timer = setTimeout(() => show(el), TT_DELAY);
    });
    root.addEventListener('mouseout', (e) => {
      const el = e.target.closest('[data-tip]');
      if (!el) return;
      clearTimeout(timer);
      hide(el);
    });
    // Keyboard tab-focus and touch (many mobile browsers focus a tapped control before
    // firing its click) — shown immediately, no delay, since focus is already a
    // deliberate, discrete user action rather than an in-flight pointer move.
    root.addEventListener('focusin', (e) => {
      const el = e.target.closest('[data-tip]');
      if (!el) return;
      clearTimeout(timer);
      show(el);
    });
    root.addEventListener('focusout', (e) => {
      const el = e.target.closest('[data-tip]');
      if (el) hide(el);
    });
  }

  // One-line live greedy status from the control switch's attributes (see
  // DeferrableLoadController.status()). Three states worth showing, in priority order:
  // greedy is holding the device on (and why); greedy is armed but blocked; or the
  // forecast-surplus condition is armed and tracking, in which case we show progress
  // toward its bar rather than a silent boolean that flips with no warning. Empty when
  // greedy is off entirely — no point adding a line that just says "nothing".
  _greedyLine(a) {
    if (!a || !a.greedy) return '';
    const reason = a.greedy_reason;
    if (reason) {
      const label = {
        import_free: 'On — import is free right now',
        export_surplus: 'On — running on surplus export',
        forecast_surplus: 'On — forecast surplus',
      }[reason] || `On — ${esc(reason)}`;
      const nums = (reason === 'forecast_surplus' && a.forecast_free_kwh != null)
        ? ` (${(+a.forecast_free_kwh).toFixed(1)} of ${(+a.forecast_needed_kwh).toFixed(1)} kWh)`
        : '';
      return `<div class="greedy-line active">Greedy: ${label}${nums}</div>`;
    }
    if (a.greedy_blocked) {
      const why = a.greedy_blocked === 'override'
        ? 'suppressed by a manual override'
        : 'outside this load\'s availability window';
      return `<div class="greedy-line">Greedy: armed, ${why}</div>`;
    }
    if (a.greedy_forecast_surplus && a.forecast_free_kwh != null && a.forecast_needed_kwh) {
      const have = +a.forecast_free_kwh, need = +a.forecast_needed_kwh;
      const pct = Math.max(0, Math.min(100, (have / need) * 100));
      return `<div class="greedy-line">Greedy: armed · forecast surplus `
        + `${have.toFixed(1)} / ${need.toFixed(1)} kWh`
        + `<span class="gbar"><span style="width:${pct.toFixed(0)}%"></span></span></div>`;
    }
    return `<div class="greedy-line">Greedy: armed, waiting for free energy</div>`;
  }

  // Modulating-only "why" line — answers "why is my car charging at 8 A right now?"
  // (FEATURES.md §11 observability) straight from the control switch's own attributes,
  // no extra entity lookup. `plugged_in === false` is called out ahead of the source
  // label since it explains a 0 A readout outright; `plugged_in == null` (unconfigured/
  // unavailable plug sensor) means "assume plugged" per the contract, so it says nothing.
  // Reuses the .greedy-line/.greedy-line.active styling below rather than inventing a new
  // look — 'surplus' gets the same "good news, it's free" accent the greedy line uses.
  _modulationLine(a, d) {
    if (!a || d.control_type !== 'modulating') return '';
    if (a.plugged_in === false) {
      return `<div class="greedy-line">Unplugged — charging stopped</div>`;
    }
    const label = {
      plan: 'Following the plan',
      surplus: 'Charging on surplus solar/export',
      override: 'Manual override',
      off: 'Not charging',
    }[a.modulation_source];
    if (!label) return '';
    return `<div class="greedy-line${a.modulation_source === 'surplus' ? ' active' : ''}">${esc(label)}</div>`;
  }

  // Live current/power readout chip for a modulating device — the setpoint entity's own
  // current value (ground truth, not the controller's internal target) plus the device's
  // live power draw when a power sensor resolved (same `power_entity` every other feature
  // on this card already uses, no new discovery). Degrades to an em-dash rather than
  // hiding the chip outright when the setpoint entity is unavailable/unknown, so the row
  // keeps its shape instead of jumping when a charger briefly drops offline.
  _currentReadoutHtml(d) {
    if (d.control_type !== 'modulating') return '';
    const hass = this._hass;
    const spSt = d.setpoint_entity ? hass.states[d.setpoint_entity] : null;
    const spVal = spSt ? parseFloat(spSt.state) : NaN;
    const spOk = spSt && Number.isFinite(spVal) && !['unavailable', 'unknown'].includes(spSt.state);
    const spUnit = (spSt && spSt.attributes && spSt.attributes.unit_of_measurement) || 'A';
    const pwSt = d.power_entity ? hass.states[d.power_entity] : null;
    const pwVal = pwSt ? parseFloat(pwSt.state) : NaN;
    const pwOk = pwSt && Number.isFinite(pwVal) && !['unavailable', 'unknown'].includes(pwSt.state);
    const pwUnit = (pwSt && pwSt.attributes && pwSt.attributes.unit_of_measurement) || 'W';
    const pwKw = pwOk ? (pwUnit.toLowerCase() === 'kw' ? pwVal : pwVal / 1000) : null;
    const parts = [];
    if (spOk) parts.push(`${spVal.toFixed(1)} ${esc(spUnit)}`);
    if (pwKw != null) parts.push(`${pwKw.toFixed(2)} kW`);
    const text = parts.length ? parts.join(' · ') : '–';
    const tip = 'Live charging current'
      + (d.power_entity ? ' and measured power draw' : '')
      + '. Shows "–" when the setpoint entity is unavailable.';
    return `<div class="modcur" tabindex="0" data-tip="${esc(tip)}">${text}</div>`;
  }

  // Max-current ceiling input (modulating devices only) — a user-set cap on the current
  // Grid Lens may command this charger to, feeding the controller's `cap_w` (see
  // MODULATING_CONTRACT.md §3.1 step 3 / §6). Same input-then-commit pattern as the
  // Today Boost box below. Omitted (not disabled) when the entity hasn't resolved yet —
  // a modulating device with no max_current number at all reads as "not configured yet",
  // same treatment the rest of this card gives a missing auxiliary entity.
  _maxCurrentHtml(r, d) {
    if (d.control_type !== 'modulating' || !r.maxCurEid) return '';
    const hass = this._hass;
    const st = hass.states[r.maxCurEid];
    if (!st) return '';
    const ba = st.attributes || {};
    const val = parseFloat(st.state);
    const shown = Number.isFinite(val) ? val : (ba.max ?? 0);
    const unit = ba.unit_of_measurement || 'A';
    const tip = `Ceiling on the current Grid Lens may command this charger to — turn down `
      + `to reserve capacity for something else sharing the circuit. `
      + `Native max: ${ba.max ?? '–'} ${esc(unit)}.`;
    return `
      <div class="maxcur" tabindex="0" data-tip="${esc(tip)}">
        <span class="maxcur-label">Max</span>
        <input type="number" class="maxcur-input" data-eid="${esc(r.maxCurEid)}"
          min="${ba.min ?? 0}" max="${ba.max ?? 32}" step="${ba.step ?? 1}" value="${shown}">
        <span class="maxcur-unit">${esc(unit)}</span>
      </div>`;
  }

  // 14-day daily-kWh bar sparkline for a device's energy sensor — a quick visual answer
  // to "what does this thing normally use per day?" right next to the Today Boost input
  // it informs. Undefined cache entry = still loading (blank placeholder, no layout
  // jump once data arrives); null `days` = the stats query failed or came back empty
  // (sensor too new, no long-term stats yet) — rendered as nothing rather than an error,
  // since a device without two weeks of history yet is a normal, expected state.
  _sparklineHtml(eid) {
    const entry = this._historyCache[eid];
    if (!entry) return '<div class="spark-ph"></div>';
    const days = entry.days;
    if (!days || !days.length) return '';
    const todayKey = new Date().toDateString();
    const full = days.filter((d) => d.start.toDateString() !== todayKey);
    const avg = full.length ? full.reduce((s, d) => s + d.kwh, 0) / full.length : null;
    const max = Math.max(0.1, ...days.map((d) => d.kwh));
    const bars = days.map((d) => {
      const isToday = d.start.toDateString() === todayKey;
      const h = Math.max(1.5, (d.kwh / max) * 22).toFixed(1);
      const when = d.start.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
      const label = `${when}: ${d.kwh.toFixed(1)} kWh${isToday ? ' (so far today)' : ''}`;
      return `<div class="sbar${isToday ? ' today' : ''}" style="height:${h}px" tabindex="0" data-tip="${esc(label)}"></div>`;
    }).join('');
    return `
      <div class="spark" tabindex="0" data-tip="Daily energy use, last ${days.length} days">
        <div class="sbars">${bars}</div>
        ${avg != null ? `<div class="spark-avg">avg ${avg.toFixed(1)} kWh/d</div>` : ''}
      </div>`;
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
      const on = controlSt ? controlSt.state === 'on' : false;
      const a = controlSt ? (controlSt.attributes || {}) : {};
      const note = friendlyNote(a.note);
      const isErr = (a.note || '').startsWith('command_error');
      // A modulating device with no separate on/off switch (the common case) has no
      // switch_entity to show here — fall back to the setpoint entity so the meta line
      // still names the entity actually being driven instead of trailing off after "· ".
      const drivenEntity = d.switch_entity || d.setpoint_entity || '';
      const meta = d.controllable
        ? `${on ? 'Controlling' : 'Not controlling'} · ${esc(drivenEntity)}${note ? ' · ' + esc(note) : ''}`
        : 'Forecast only — no control switch configured';
      // Second meta line: the live greedy story. Answers "is greedy doing anything right
      // now, and if not, how close is it?" — the boolean toggles above only say it's
      // armed. Rendered off the control switch's own attributes (the controller's
      // status()), so it needs no extra entity lookup.
      const greedyLine = this._greedyLine(a);
      // Third meta line, modulating devices only: why the current is where it is right
      // now (plan/surplus/override/plugged-in) — see _modulationLine above.
      const modLine = this._modulationLine(a, d);

      // Control area: a fully-wired device gets the real segmented control (or a plain
      // toggle fallback if the override select hasn't appeared yet); a device with no
      // switch configured at all gets the same-shaped control, disabled, with a tooltip
      // explaining why — so "no control" reads as a configuration gap, not a bug.
      let controlHtml;
      if (!d.controllable) {
        controlHtml = `
          <div class="ovr disabled" tabindex="0" data-tip="Full control has not yet been configured — assign a switch via the Grid Lens integration's Reconfigure flow to enable Off now / On now / Auto.">
            <button disabled>Off now</button>
            <button disabled>On now</button>
            <button disabled>Auto</button>
          </div>`;
      } else if (r.selEid) {
        const cur = hass.states[r.selEid] ? hass.states[r.selEid].state : null;
        controlHtml = `
          <div class="ovr" data-sel="${esc(r.selEid)}" data-ctl="${esc(r.controlEid)}" data-tip="Manual override">
            <button data-opt="Force Off" class="${cur === 'Force Off' ? 'active off' : ''}">Off now</button>
            <button data-opt="Force On" class="${cur === 'Force On' ? 'active' : ''}">On now</button>
            <button data-opt="Auto" class="${cur === 'Auto' && on ? 'active' : ''}"
              data-tip="Grid Lens schedules this load">Auto</button>
          </div>`;
      } else if (r.controlEid) {
        controlHtml = `<div class="sw ${on ? 'on' : ''}" data-eid="${esc(r.controlEid)}" tabindex="0" data-tip="${on ? 'Turn off' : 'Turn on'}"></div>`;
      } else {
        // controllable per config, but the control entities haven't appeared yet (a
        // startup race) — same disabled placeholder, self-heals on the next repaint.
        controlHtml = `
          <div class="ovr disabled" tabindex="0" data-tip="Control entities are still loading…">
            <button disabled>Off now</button><button disabled>On now</button><button disabled>Auto</button>
          </div>`;
      }

      // Greedy Consumption's per-device toggles (see switch.py) — opportunistic "on"
      // whenever import is free, export is being wasted, or the plan forecasts a spill
      // bigger than this load, on top of Auto/plan control. Always renders all three
      // icons — disabled/dimmed with an explanatory tooltip when the device isn't
      // controllable (or its switches haven't appeared yet) — so every row keeps the
      // same shape and rows line up regardless of what's actually configured, the same
      // treatment the control segment above already gets.
      const greedyWhy = !d.controllable
        ? "Only available once this load has a control switch — assign one via the Grid Lens integration's Reconfigure flow."
        : 'Loading…';
      const gBtn = (icon, eid, on2, label) => eid
        ? `<div class="gbtn${on2 ? ' on' : ''}" data-eid="${esc(eid)}" tabindex="0" data-tip="${esc(label)}">
             <ha-icon icon="${icon}"></ha-icon>
           </div>`
        : `<div class="gbtn disabled" tabindex="0" data-tip="${esc(greedyWhy)}">
             <ha-icon icon="${icon}"></ha-icon>
           </div>`;
      const gOn = r.gEid && hass.states[r.gEid] && hass.states[r.gEid].state === 'on';
      const gsOn = r.gsEid && hass.states[r.gsEid] && hass.states[r.gsEid].state === 'on';
      const gfOn = r.gfEid && hass.states[r.gfEid] && hass.states[r.gfEid].state === 'on';
      const greedyHtml = `
        <div class="greedy">
          ${gBtn('mdi:leaf', r.gEid, gOn,
            'Greedy Consumption: turn on whenever import is free or export is being wasted')}
          ${gBtn('mdi:calendar-clock', r.gsEid, gsOn,
            "Greedy Respects Schedule: only greedy-fire inside this load's own availability window")}
          ${gBtn('mdi:weather-sunny-alert', r.gfEid, gfOn,
            'Greedy Forecast Surplus: also start early when the plan forecasts more free energy going to waste than this load could use (needs Greedy Consumption on; may import briefly)')}
        </div>`;

      // 14-day daily-kWh sparkline — shown for every device with an energy sensor
      // (always true for a configured deferrable load today), so it sits right beside
      // the boost input it's meant to inform.
      const sparkHtml = this._sparklineHtml(d.energy_entity);

      // Modulating-only additions — appended after the existing sparkline/boost elements
      // so a non-modulating row's markup up to this point is byte-for-byte unchanged.
      // Both return '' for a non-modulating device, so nothing is inserted for one.
      const currentHtml = this._currentReadoutHtml(d);
      const maxCurHtml = this._maxCurrentHtml(r, d);

      // Today Boost override (merged in from the old boost-tuning-card 2026-07-31) —
      // shown for every device that has one, controllable or not.
      let boostHtml = '';
      if (r.boostEid && hass.states[r.boostEid]) {
        const bst = hass.states[r.boostEid];
        const ba = bst.attributes || {};
        const val = parseFloat(bst.state);
        const shown = Number.isFinite(val) ? val : 0;
        const active = shown > 0;
        // A target above what the availability window can deliver is silently clamped
        // by the optimizer, which reads as "my boost did nothing" — call it out here,
        // at the input, rather than leaving it to the log.
        const ceiling = this._boostCeiling(d);
        const over = ceiling != null && shown > ceiling + 1e-6;
        const tip = ceiling == null
          ? "Today's kWh target override — 0 uses the 14-day historical average"
          : `Today's kWh target override — 0 uses the 14-day historical average. `
            + `At ${d.max_kw} kW, this load's schedule allows at most `
            + `${ceiling.toFixed(1)} kWh over the next 24 h; anything above that `
            + `cannot be scheduled.`;
        boostHtml = `
          <div class="boost${active ? ' active' : ''}${over ? ' over' : ''}" tabindex="0" data-tip="${esc(tip)}">
            <input type="number" class="boost-input" data-eid="${esc(r.boostEid)}"
              min="${ba.min ?? 0}" max="${ba.max ?? 999}" step="${ba.step ?? 0.5}"
              value="${shown}">
            <span class="boost-unit">kWh</span>
            ${over ? `<span class="boost-cap">max ${ceiling.toFixed(1)}</span>` : ''}
          </div>`;
      }

      return `
        <div class="row" data-eid="${esc(r.controlEid || d.energy_entity)}">
          <ha-icon class="icon${d.controllable ? '' : ' dim'}" icon="mdi:power-plug"></ha-icon>
          <div class="info">
            <div class="name">${esc(d.name || d.energy_entity)}</div>
            <div class="meta${isErr ? ' err' : ''}">${meta}</div>
            ${greedyLine}
            ${modLine}
          </div>
          ${sparkHtml}
          ${boostHtml}
          ${currentHtml}
          ${maxCurHtml}
          ${greedyHtml}
          ${controlHtml}
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
