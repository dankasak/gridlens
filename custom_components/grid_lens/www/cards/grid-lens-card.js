import { smoothPath, gradDef } from './grid-lens-chart-common.js?v=20261018b';

class GridLensCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._connected = false;  // true once HA sets hass; never store the hass object itself
    this._data = null;
    this._fetching = false;
    this._startDate = '';
    this._endDate = '';
    this._chartScale = 1.0;
    this._showHistory = false;
    // Full-screen breakout — click a plan's cost banner to expand just that plan
    // (bigger charts + fonts, dims/hides everything else). Holds the plan's key
    // (same string as planDetails/plansToShow), or null when nothing is expanded.
    // Rendered as a native <dialog> (see render()'s plansHtml build) — .showModal()
    // promotes it to the browser's top layer, which is the only thing that reliably
    // escapes an ANCESTOR establishing a containing block for position:fixed (our own
    // :host { contain: content }, but also — the actual reason a plain fixed div
    // wasn't enough here — ha-card's own internal styling). Top-layer rendering
    // ignores containment/overflow/stacking-context on every ancestor by construction.
    this._fullscreenPlan = null;
    this._retailerFilter = '';   // live retailer search box, applied as the user types
    this._excludeGreedy = false; // "exclude greedy consumption" checkbox — see setConfig
    this._topN = 5;              // "Show best N" declutter filter — see setConfig

    // "What if?" panel — hypothetical battery/solar sizing. Not persisted across
    // reloads (unlike excludeGreedy/topN above): this is a one-off exploration
    // mode, not a standing preference, so a fresh page load always starts back
    // on the household's real comparison. `_whatifBatteryKwh`/`_whatifSolarPct`
    // are the input field STRINGS (not numbers) so an empty/blank field can be
    // told apart from "0" while typing; `_whatifApplied` is the snapshot of both
    // actually sent with the last fetch (what `this._data.whatif` reflects),
    // which can lag the live input values until Apply is clicked again.
    this._showWhatif = false;
    this._whatifBatteryKwh = null;
    this._whatifSolarPct = null;
    this._whatifApplied = null;  // {batteryKwh, solarPct} | null

    // ── Data source ─────────────────────────────────────────────────────────
    // 'live' reads this household's recorder (the default, and the only source
    // before bill upload existed). 'bill:<id>' drives the whole page from an
    // uploaded bill's reconstructed half-hourly model instead — see
    // bill_source.py. Everything on this page works either way because it is
    // all downstream of one seam in the backend; the only things that change
    // here are the selector, the confidence banner, and the date inputs (a
    // bill's period is fixed, so they are disabled).
    //
    // Persisted, unlike the What-If panel: picking a source is a standing
    // choice ("I'm looking at my relative's bill today"), not a one-off
    // exploration, and a page reload mid-analysis losing it is worse than the
    // small surprise of coming back to it.
    this._source = 'live';
    this._billScenario = null;   // which controlled-load scenario, when source is a bill
    this._bills = null;          // null = not loaded yet; [] = none stored
    this._archetypes = [];
    this._showBills = false;
    this._billDraft = '';        // the paste-a-bill textarea's contents
    this._billError = '';

    this._history = null;
    this._editingId = null;
    this._addingNew = false;
    // Streaming calculation state
    this._streamPhase = null;   // 'fetching' | 'optimising' | null
    this._plansDone = 0;
    this._plansTotal = 0;
    this._fetchStep = 0;
    this._fetchTotal = 0;
    this._fetchMessage = '';
    // The period-ranking phase's own progress message, kept separate from
    // _fetchMessage (which belongs to the data-fetch phase) — see _pollCalc.
    this._periodMessage = '';
    // A bill run's window, for display only. Deliberately NOT _startDate/_endDate:
    // see _updateDatesFromData for what went wrong when the two shared one slot.
    this._billStart = '';
    this._billEnd = '';
    this._pollGeneration = 0;   // bumped on every _streamCalc call to cancel a stale poll loop
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error('You need to define an entity');
    }
    this._config = config;
    // Restore last-used date range across page reloads
    try {
      if (!this._startDate) this._startDate = localStorage.getItem('epc-date-start') || '';
      if (!this._endDate)   this._endDate   = localStorage.getItem('epc-date-end')   || '';
      if (!this._excludeGreedy) this._excludeGreedy = localStorage.getItem('epc-exclude-greedy') === 'true';
      if (this._source === 'live') this._source = localStorage.getItem('epc-source') || 'live';
      if (!this._billScenario) this._billScenario = localStorage.getItem('epc-bill-scenario') || null;
      const storedTopN = localStorage.getItem('epc-top-n');
      if (storedTopN != null) this._topN = storedTopN === 'all' ? Infinity : parseInt(storedTopN, 10) || 5;
    } catch (_) {}
    this.render();
  }

  set hass(_hass) {
    // We don't use entity states — just need to know HA is connected.
    // Never store the hass object: it's huge and HA reassigns it on every entity
    // state change, which causes severe GC pressure in Chrome.
    if (!this._connected) {
      this._connected = true;
      // Bills first, and awaited: a stored source of 'bill:<id>' restored from
      // localStorage has to be validated against what is actually stored before
      // the comparison is kicked off, or a bill deleted in another tab leaves
      // this one requesting a 404 on every load.
      this._loadBills().then(() => {
        // Use saved dates on init so navigating back restores the last comparison.
        const s = this._startDate ? `${this._startDate}T00:00:00` : null;
        const e = this._endDate   ? `${this._endDate}T23:59:59`   : null;
        this.fetchData(s, e);
      });
    }
  }

  async _loadBills() {
    try {
      const resp = await fetch('/api/grid_lens/bills');
      if (!resp.ok) throw new Error(`bills: ${resp.status}`);
      const data = await resp.json();
      this._bills = data.bills || [];
      this._archetypes = data.archetypes || [];
    } catch (err) {
      this._bills = [];
      this._archetypes = [];
    }
    if (this._source !== 'live') {
      const id = this._source.slice(5);
      if (!this._bills.some(b => b.id === id)) this._setSource('live', false);
    }
  }

  _setSource(source, refetch = true) {
    this._source = source;
    this._billScenario = null;
    try {
      localStorage.setItem('epc-source', source);
      localStorage.removeItem('epc-bill-scenario');
    } catch (_) {}
    if (!refetch) return;
    if (source === 'live') {
      // Back to the household's own data, on its own saved date range.
      const s = this._startDate ? `${this._startDate}T00:00:00` : null;
      const e = this._endDate   ? `${this._endDate}T23:59:59`   : null;
      this.fetchData(s, e, true);
    } else {
      // The backend forces the bill's own period, so send no dates at all
      // rather than quietly handing it a range it is going to override.
      this.fetchData(null, null, true);
    }
  }

  _currentBill() {
    if (this._source === 'live' || !this._bills) return null;
    const id = this._source.slice(5);
    return this._bills.find(b => b.id === id) || null;
  }

  // Unique id for an inline <svg>'s <linearGradient>, scoped to one render() pass.
  // Every hourly chart across every plan-card lives in the SAME shadow root, so ids
  // must be unique across the whole render, not just within one chart's own <svg>.
  _nextGid() {
    this._gidCounter = (this._gidCounter || 0) + 1;
    return `epcgrad${this._gidCounter}`;
  }

  // Fragment identifying the currently-applied What-If override, if any — folded
  // into every cache key so a "no battery, 0% solar" scenario can never be served
  // from a cache entry populated by the real comparison (or a different scenario).
  _whatifCacheFrag() {
    if (!this._whatifApplied) return '';
    return `whatif:${this._whatifApplied.batteryKwh}:${this._whatifApplied.solarPct}`;
  }

  // Folded into every cache key alongside _whatifCacheFrag, for the same
  // reason: a bill's comparison must never be served from the live household's
  // cache entry, and one controlled-load scenario must never be served from
  // another's — they differ by hundreds of dollars a year.
  _sourceCacheFrag() {
    if (this._source === 'live') return '';
    return `src:${this._source}:${this._billScenario || 'default'}`;
  }

  _cacheKey(startDate, endDate) {
    return `${startDate || ''}|${endDate || ''}|${this._excludeGreedy}` +
           `|${this._whatifCacheFrag()}|${this._sourceCacheFrag()}`;
  }

  async fetchData(startDate = null, endDate = null, forceRefresh = false) {
    if (!this._connected || this._fetching) return;

    // Return cached result instantly when navigating back to the card.
    const cacheKey = this._cacheKey(startDate, endDate);
    if (!forceRefresh) {
      const hit = GridLensCard._cache[cacheKey];
      if (hit) {
        this._data = hit;
        this._updateDatesFromData(startDate);
        this.render();
        return;
      }
    }

    // No cache hit (initial load or forced refresh): stream with live progress.
    this._streamCalc(startDate, endDate);
  }

  _streamCalc(startDate, endDate) {
    // Cancel any in-flight poll loop from a previous call — _pollCalc checks
    // this generation counter after every await and bails out once it no
    // longer matches, the same "stop working for a superseded caller"
    // pattern the old EventSource's src.close() achieved by tearing down the
    // connection outright.
    this._pollGeneration += 1;
    const generation = this._pollGeneration;

    this._fetching = true;
    this._streamPhase = 'fetching';
    this._plansDone = 0;
    this._plansTotal = 0;
    this._fetchStep = 0;
    this._fetchTotal = 0;
    this._fetchMessage = '';
    this._periodMessage = '';
    this._data = null;   // clear stale data so we render from the poll loop only
    if (startDate) this._startDate = startDate.substring(0, 10);
    if (endDate)   this._endDate   = endDate.substring(0, 10);
    this._showStreamProgress();

    // Callers (epc-calc / prev / next) pass bare "YYYY-MM-DD" strings. Without a
    // time component the backend parses both as midnight, so the end date's whole
    // day (the last day of the billing period) contributes zero usage/supply-charge
    // — a full calendar day short of what the user's paper bill covers. Widen bare
    // dates to the full local day so the range is inclusive of both endpoints.
    const withTime = (d, suffix) => (d && d.length === 10) ? `${d}T${suffix}` : d;
    const rangeStart = withTime(startDate, '00:00:00');
    const rangeEnd   = withTime(endDate,   '23:59:59');

    let params = (rangeStart && rangeEnd)
      ? `?start_date=${rangeStart}&end_date=${rangeEnd}` : '';
    if (this._excludeGreedy) {
      params += params ? '&exclude_greedy=true' : '?exclude_greedy=true';
    }
    const add = (k, v) => { params += params ? `&${k}=${v}` : `?${k}=${v}`; };
    if (this._whatifApplied) {
      if (this._whatifApplied.batteryKwh !== null) add('battery_kwh', this._whatifApplied.batteryKwh);
      if (this._whatifApplied.solarPct !== null) add('solar_pct', this._whatifApplied.solarPct);
    }
    if (this._source !== 'live') {
      add('source', encodeURIComponent(this._source));
      if (this._billScenario) add('scenario', encodeURIComponent(this._billScenario));
    }
    this._pollCalc(params, startDate, endDate, generation);
  }

  // Drives a plan comparison via short-lived polls instead of one long-lived
  // SSE connection (this replaced grid-lens-card.js's old EventSource-based
  // _streamCalc on 2026-09-29). A ~80s single streamed response was reliably
  // getting killed partway through by the browser's own Service Worker
  // (confirmed live: the failure vanished entirely in a Private Browsing
  // window, where no Service Worker is registered) — a duration problem, not
  // a payload-size one (2026-09-28's payload trim alone didn't fix it, since
  // it didn't shorten the ~80s wall-clock time, which is LP-solve compute,
  // not data transfer). Every request here (start, and each poll) completes
  // in well under a second, so there's nothing long-lived left for a Service
  // Worker — or any other intermediary — to choke on, regardless of the
  // exact mechanism that was tripping on the old stream. See
  // __init__.py's PlanCalcStartView/PlanCalcPollView and
  // GRIDLENS_CHECKLIST.md, 2026-09-29.
  async _pollCalc(params, startDate, endDate, generation) {
    try {
      const startResp = await fetch(`/api/grid_lens/plan_calc/start${params}`);
      if (!startResp.ok) throw new Error(`start failed: ${startResp.status}`);
      const { token } = await startResp.json();
      if (this._pollGeneration !== generation) return;  // superseded while starting

      let since = 0;
      while (this._pollGeneration === generation) {
        const pollResp = await fetch(`/api/grid_lens/plan_calc/poll?since=${since}&token=${token}`);
        if (!pollResp.ok) throw new Error(`poll failed: ${pollResp.status}`);
        const d = await pollResp.json();
        if (this._pollGeneration !== generation) return;  // superseded mid-request

        let gotPlan = false, gotStatus = false;
        for (const ev of d.events) {
          if (ev.event === 'status') {
            const s = ev.data;
            this._streamPhase   = s.phase || 'fetching';
            this._plansTotal    = s.plans_total || this._plansTotal;
            this._fetchStep     = s.fetch_step  || this._fetchStep;
            this._fetchTotal    = s.fetch_total || this._fetchTotal;
            // Two different phases report through this one event. 'fetching' is
            // the recorder/statistics pull; 'optimising' here is the per-period
            // re-ranking that runs AFTER the whole-window loop has already hit
            // N/N plans (calculate_plan_costs's on_period_progress). Keeping
            // them in one slot made the second one invisible — see
            // _showStreamProgress for what the user saw instead.
            if (s.phase === 'optimising') {
              this._periodMessage = s.message || this._periodMessage;
            } else {
              this._fetchMessage = s.message || this._fetchMessage;
            }
            gotStatus = true;
          } else if (ev.event === 'plan') {
            const p = ev.data;
            this._streamPhase = 'optimising';
            this._plansDone   = p.plans_done;
            this._plansTotal  = p.plans_total;
            // Build/update _data with what's arrived so far so render() works unchanged
            if (!this._data) {
              this._data = {
                plan_details: {},
                current_plan_name: p.current_plan_name,
                alternative_plans: {},
                current_plan_total: 0,
                usage_days: p.usage_days || 0,
                start_date: p.start_date || '',
                end_date: p.end_date || '',
                energy_flows: p.energy_flows || {},
                deferrable_devices: p.deferrable_devices || [],
                calculation_date: null,
              };
              this._updateDatesFromData(startDate);
            }
            this._data.plan_details[p.plan_key] = p.detail;
            this._data.alternative_plans = p.alternative_plans || {};
            this._data.current_plan_total = p.current_plan_total || 0;
            gotPlan = true;
          }
        }
        // One render/progress-update per poll batch (not per event) — cheaper
        // than the old one-render-per-SSE-event behaviour with no loss of
        // freshness, since a poll batch is already at most ~1s of events.
        //
        // Once any plan has arrived, keep rendering the card rather than
        // dropping back to the bare progress panel: the period-ranking phase
        // emits nothing but 'status' events, and on a long window that is
        // minutes of _showStreamProgress() wiping a page full of results back
        // to a spinner. The inline progress strip in the date controls carries
        // the same message.
        if (gotPlan || (gotStatus && this._data)) this.render();
        else if (gotStatus) this._showStreamProgress();
        since = d.next_since;

        if (d.status === 'complete') {
          const full = d.result;
          // The server omits plan_details here (see PlanCalcStartView) — each
          // plan's detail already arrived via its own 'plan' event above and
          // was merged into this._data.plan_details, so restore it rather
          // than losing it to the empty dict the final payload carries in
          // its place.
          full.plan_details = (this._data && this._data.plan_details) || {};
          this._data = full;
          const cacheKey = this._cacheKey(startDate, endDate);
          GridLensCard._cache[cacheKey] = full;
          this._streamPhase = null;
          this._periodMessage = '';   // or the toolbar keeps showing the last ranking message
          this._fetching = false;
          this._updateDatesFromData(startDate);
          this.render();
          return;
        }
        if (d.status === 'error' || d.status === 'not_found' || d.status === 'stale') {
          this._streamPhase = null;
          this._periodMessage = '';
          this._fetching = false;
          this.renderError('Calculation failed — check HA logs.');
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    } catch (err) {
      if (this._pollGeneration !== generation) return;  // superseded, not a real failure
      this._streamPhase = null;
      this._periodMessage = '';
      this._fetching = false;
      this.renderError('Calculation failed — check HA logs.');
    }
  }

  _showStreamProgress() {
    const phase = this._streamPhase;
    const isFetching = phase === 'fetching';
    // ⚠ The period-ranking phase (one extra LP solve per candidate plan per
    // period, after the whole-window loop) reports 'optimising' too — by which
    // point plansDone === plansTotal. Showing the plans counter there pinned the
    // text at "Optimising plans… 207 / 207" and the bar at 100% for the whole
    // phase: on a 93-day two-period window that is ~130 seconds of a page that
    // reads as hung, which is exactly how it was reported (2026-10-26). The
    // backend was given a per-plan progress callback precisely so this wouldn't
    // happen; the card was throwing the message away. Its own step/total
    // (plans ranked within the current period) drive the bar.
    const msg = isFetching
      ? (this._fetchMessage
          ? `${this._fetchMessage} (${this._fetchStep} / ${this._fetchTotal || '?'})`
          : 'Fetching energy data…')
      : (this._periodMessage || `Optimising plans… ${this._plansDone} / ${this._plansTotal}`);
    const pct = (isFetching || this._periodMessage)
      ? (this._fetchTotal > 0 ? Math.round(this._fetchStep / this._fetchTotal * 100) : 0)
      : (this._plansTotal > 0 ? Math.round(this._plansDone / this._plansTotal * 100) : 0);
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block}
        .sp-wrap{padding:24px 16px;text-align:center;color:var(--secondary-text-color)}
        .sp-msg{font-size:14px;margin-bottom:12px}
        .sp-track{background:var(--divider-color);border-radius:4px;height:6px;overflow:hidden;max-width:320px;margin:0 auto}
        .sp-bar{background:var(--primary-color);height:100%;border-radius:4px;transition:width 0.4s ease}
      </style>
      <ha-card>
        <div class="sp-wrap">
          <div class="sp-msg">${msg}</div>
          <div class="sp-track"><div class="sp-bar" style="width:${pct}%"></div></div>
        </div>
      </ha-card>`;
  }

  _updateDatesFromData(startDate) {
    // When fetching the default period (no custom dates), let the API response
    // set the date inputs so they reflect what's actually displayed.
    // When custom dates were requested, the inputs are already correct.
    if (!startDate) {
      // ⚠ A bill run also arrives here with no dates — _setSource sends none
      // deliberately, because the backend forces the bill's own period. But
      // that period is the BILL's, not a range this household ever picked, and
      // it is typically a 92-day quarter. Writing it into _startDate/_endDate
      // (and into the SAME localStorage keys the live source reads back) meant
      // selecting a bill silently replaced the household's own saved range,
      // so switching back to "My data" kicked off a 3-month live comparison
      // nobody asked for: ~620 LP solves, ~4 minutes, and a page that looks
      // hung for most of it (found live 2026-10-26). Keep the bill's window
      // display-only — the pickers are disabled in bill mode anyway.
      if (this._source !== 'live') {
        this._billStart = (this._data.start_date || '').substring(0, 10);
        this._billEnd   = (this._data.end_date   || '').substring(0, 10);
        return;
      }
      if (this._data.start_date) {
        this._startDate = this._data.start_date.substring(0, 10);
        try { localStorage.setItem('epc-date-start', this._startDate); } catch (_) {}
      }
      if (this._data.end_date) {
        this._endDate = this._data.end_date.substring(0, 10);
        try { localStorage.setItem('epc-date-end', this._endDate); } catch (_) {}
      }
    }
  }

  _showMessage(msg) {
    const styles = `<style>:host{display:block}.loading{padding:16px;text-align:center;color:var(--secondary-text-color)}</style>`;
    this.shadowRoot.innerHTML = `${styles}<ha-card>
      <div class="loading">${msg}</div>
    </ha-card>`;
  }

  // ── Chart helpers ─────────────────────────────────────────────────────────

  // Faint dashed vertical gridlines — every 3 hours (0,3,6,9,noon,3pm,6pm,9pm) on the
  // compact card, every hour once full-screen gives them room to breathe — added to
  // EVERY individual chart so an hour position can be read, and lined up against the
  // same hour on the chart above/below it, straight off any one graph. Same visual
  // language as this file's existing reference lines (the diverging charts' MID
  // divider, the SOC chart's 20/80% lines).
  _hourTicks(H, BAR, GAP, isFullscreen = false) {
    const everyH = isFullscreen ? 1 : 3;
    let s = '';
    for (let h = 0; h < 24; h += everyH) {
      const x = (h * (BAR + GAP) + BAR / 2).toFixed(1);
      s += `<line x1="${x}" y1="0" x2="${x}" y2="${H}" stroke="var(--divider-color)" stroke-width="0.6" stroke-dasharray="2,2" opacity="0.55"/>`;
    }
    return s;
  }

  // Hour labels ("12a"…"11p") for the ticks above — every 3 hours normally, every
  // hour once full-screen (matching _hourTicks' own step) gives them room. Plain
  // HTML, not SVG <text> inside the chart: every mini-chart's viewBox is a fixed 288
  // units wide but renders at width:100% with preserveAspectRatio="none" (so it
  // stretches to fill whatever the card gives it — see the "charts should take up
  // all horizontal space" full-screen fix). That stretch is only ~1:1 on the compact
  // card (whose container is close to 288px already) but ~5x on a full-screen
  // dialog — and because it's non-uniform (only horizontal, matching W:288 → a much
  // wider box), SVG text sized in the same viewBox units gets stretched sideways by
  // that same ~5x, rendering wildly oversized in full-screen regardless of what
  // font-size is picked. Plain HTML text below the chart isn't part of that
  // coordinate system, so it sizes normally — and picks up the same modest
  // fullscreen bump as every other caption via the .fullscreen-plan .hour-axis rule.
  _hourLabelRow(isFullscreen = false) {
    const everyH = isFullscreen ? 1 : 3;
    let spans = '';
    for (let h = 0; h < 24; h += everyH) {
      const label = h === 0 ? '12a' : h === 12 ? '12p' : (h < 12 ? `${h}a` : `${h - 12}p`);
      spans += `<span>${label}</span>`;
    }
    return `<div class="hour-axis">${spans}</div>`;
  }

  // Invisible per-slot hit targets: a smoothed curve has no discrete bar to hover,
  // so a native <title> tooltip is carried by a transparent full-height rect per
  // slot instead — same hourly granularity the old per-bar <title> gave.
  _chartHovers(profile, W, H, BAR, GAP, titleFor) {
    return profile.map((slot, i) => {
      const x = i * (BAR + GAP);
      return `<rect x="${x}" y="0" width="${(BAR + GAP).toFixed(1)}" height="${H}" fill="transparent">` +
             `<title>${titleFor(slot, i)}</title></rect>`;
    }).join('');
  }

  renderDivergingChart(profile, upKey, downKey, maxVal, upColor, downColor, scale = 1, isFullscreen = false) {
    if (!profile || !profile.length) return '';
    const W = 288, H = Math.round(80 * scale), BAR = 11, GAP = 1, MID = Math.round(38 * scale);
    const barScale = (MID - 3) / (maxVal || 1);
    const upPts = profile.map((slot, i) =>
      [i * (BAR + GAP) + BAR / 2, MID - Math.min(Math.max(slot[upKey] * barScale, 0), MID - 3)]);
    const dnPts = profile.map((slot, i) =>
      [i * (BAR + GAP) + BAR / 2, MID + Math.min(Math.max(slot[downKey] * barScale, 0), H - MID - 3)]);
    const upLine = smoothPath(upPts), dnLine = smoothPath(dnPts);
    const upFirstX = upPts[0][0].toFixed(1), upLastX = upPts[upPts.length - 1][0].toFixed(1);
    const dnFirstX = dnPts[0][0].toFixed(1), dnLastX = dnPts[dnPts.length - 1][0].toFixed(1);
    const upGid = this._nextGid(), dnGid = this._nextGid();
    const hovers = this._chartHovers(profile, W, H, BAR, GAP, (slot) =>
      `${slot.hour}:00  ${slot[upKey].toFixed(3)} / ${slot[downKey].toFixed(3)}`);
    return `<svg width="100%" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="display:block;height:${H}px">
      <defs>
        ${gradDef(upGid, upColor, 0.82, 1)}
        ${gradDef(dnGid, downColor, 0.82, 0)}
      </defs>
      <line x1="0" y1="${MID}" x2="${W}" y2="${MID}" stroke="var(--divider-color)" stroke-width="0.8"/>
      <path d="${upLine} L${upLastX},${MID} L${upFirstX},${MID} Z" fill="url(#${upGid})"/>
      <path d="${dnLine} L${dnLastX},${MID} L${dnFirstX},${MID} Z" fill="url(#${dnGid})"/>
      ${this._hourTicks(H, BAR, GAP, isFullscreen)}
      <path d="${upLine}" fill="none" stroke="${upColor}" stroke-width="1.5" stroke-linejoin="round" opacity="0.95"/>
      <path d="${dnLine}" fill="none" stroke="${downColor}" stroke-width="1.5" stroke-linejoin="round" opacity="0.95"/>
      ${hovers}
    </svg>${this._hourLabelRow(isFullscreen)}`;
  }

  // Stacked SMOOTH area chart: household on the bottom, then one band per deferrable
  // device on top, each band bounded by two smoothed cumulative curves (its own bottom
  // = the previous layer's own top, computed from the SAME point arrays so the two
  // curves are pixel-identical and no seam shows between adjacent bands).
  renderStackedBarChart(profile, maxVal, scale = 1, deferrable_devices = [], isFullscreen = false) {
    if (!profile || !profile.length) return '';
    const DEVICE_COLORS = GridLensCard.DEVICE_COLORS;
    const W = 288, H = Math.round(70 * scale), BAR = 11, GAP = 1;
    const barScale = (H - 4) / (maxVal || 1);

    const usePerDevice = profile.some(s => (s.deferrable_per_device || []).length > 0);
    const deviceCount = usePerDevice
      ? Math.max(deferrable_devices.length, ...profile.map(s => (s.deferrable_per_device || []).length))
      : (profile.some(s => (s.deferrable_kwh || 0) > 0.01) ? 1 : 0);

    const layers = [{
      color: GridLensCard.HOUSEHOLD_COLOR,
      name: 'Household',
      vals: profile.map(s => s.home_load_kwh || 0),
    }];
    for (let ii = 0; ii < deviceCount; ii++) {
      layers.push({
        color: DEVICE_COLORS[ii % DEVICE_COLORS.length],
        name: (deferrable_devices[ii] && deferrable_devices[ii].name) || `Device ${ii + 1}`,
        vals: profile.map(s => usePerDevice
          ? ((s.deferrable_per_device || [])[ii] || 0)
          : (ii === 0 ? (s.deferrable_kwh || 0) : 0)),
      });
    }

    const xAt = (i) => i * (BAR + GAP) + BAR / 2;
    const yAt = (v) => H - Math.min(Math.max(v * barScale, 0), H - 4);

    let cum = profile.map(() => 0);
    let bandsSvg = '';
    const hoverLines = profile.map(() => []);
    layers.forEach((layer) => {
      const top = cum.map((b, i) => b + layer.vals[i]);
      const topPts = top.map((v, i) => [xAt(i), yAt(v)]);
      const botPts = cum.map((v, i) => [xAt(i), yAt(v)]);
      const topLine = smoothPath(topPts);
      const botLineRev = smoothPath([...botPts].reverse()).replace(/^M/, 'L');
      const gid = this._nextGid();
      bandsSvg += `<defs>${gradDef(gid, layer.color, 0.8, 1)}</defs>`;
      bandsSvg += `<path d="${topLine}${botLineRev} Z" fill="url(#${gid})"/>`;
      bandsSvg += `<path d="${topLine}" fill="none" stroke="${layer.color}" stroke-width="1.2" stroke-linejoin="round" opacity="0.95"/>`;
      profile.forEach((slot, i) => {
        if (layer.vals[i] > 0.0005) hoverLines[i].push(`${layer.name} ${layer.vals[i].toFixed(3)} kWh`);
      });
      cum = top;
    });
    const totals = cum;
    const hovers = this._chartHovers(profile, W, H, BAR, GAP, (slot, i) =>
      `${slot.hour}:00\n${hoverLines[i].join('\n')}\nTotal ${totals[i].toFixed(3)} kWh`);

    return `<svg width="100%" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="display:block;height:${H}px">
      ${bandsSvg}
      ${this._hourTicks(H, BAR, GAP, isFullscreen)}
      ${hovers}
    </svg>${this._hourLabelRow(isFullscreen)}`;
  }

  renderSolarChart(profile, maxVal, scale = 1, isFullscreen = false) {
    if (!profile || !profile.length) return '';
    const W = 288, H = Math.round(55 * scale), BAR = 11, GAP = 1;
    const barScale = (H - 4) / (maxVal || 1);
    const pts = profile.map((slot, i) =>
      [i * (BAR + GAP) + BAR / 2, H - Math.min(Math.max((slot.solar_kwh || 0) * barScale, 0), H - 4)]);
    const line = smoothPath(pts);
    const firstX = pts[0][0].toFixed(1), lastX = pts[pts.length - 1][0].toFixed(1);
    const gid = this._nextGid();
    const hovers = this._chartHovers(profile, W, H, BAR, GAP, (slot) =>
      `${slot.hour}:00  Solar ${(slot.solar_kwh || 0).toFixed(3)} kWh`);
    return `<svg width="100%" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="display:block;height:${H}px">
      <defs>${gradDef(gid, GridLensCard.SOLAR_COLOR, 0.85, 1)}</defs>
      <path d="${line} L${lastX},${H} L${firstX},${H} Z" fill="url(#${gid})"/>
      <path d="${line}" fill="none" stroke="${GridLensCard.SOLAR_COLOR}" stroke-width="1.5" stroke-linejoin="round" opacity="0.95"/>
      ${this._hourTicks(H, BAR, GAP, isFullscreen)}
      ${hovers}
    </svg>${this._hourLabelRow(isFullscreen)}`;
  }

  // Average hourly buy/sell PRICE (c/kWh) over the day. Two polylines —
  // import rate and export rate — on a shared c/kWh axis, with a zero line
  // when the export price goes negative (spot plans: you pay to export).
  // Rates come from the profile slots (import_cost/import_kwh etc.), so an
  // hour with no grid flow at all reports 0; those points are dropped and
  // the line bridges the gap rather than diving to zero.
  renderRateChart(profile, scale = 1, isFullscreen = false) {
    if (!profile || !profile.length) return '';
    const W = 288, H = Math.round(58 * scale), BAR = 11, GAP = 1, PAD = 3;
    const imp = profile.map(s => (s.import_rate || 0) * 100);
    const exp = profile.map(s => (s.export_rate || 0) * 100);
    const vals = [...imp, ...exp].filter(v => v !== 0);
    if (!vals.length) return '';
    let hi = Math.max(...vals, 0), lo = Math.min(...vals, 0);
    if (hi - lo < 1) hi = lo + 1;                 // avoid a flat/degenerate axis
    const y = c => H - PAD - ((c - lo) / (hi - lo)) * (H - 2 * PAD);
    const line = (series, color) => {
      const pts = series
        .map((c, i) => (c === 0 ? null : `${(i * (BAR + GAP) + BAR / 2).toFixed(1)},${y(c).toFixed(1)}`))
        .filter(Boolean).join(' ');
      if (!pts) return '';
      return `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/>`;
    };
    const dots = (series, color, label) => series.map((c, i) => {
      if (c === 0) return '';
      const x = i * (BAR + GAP) + BAR / 2;
      return `<circle cx="${x.toFixed(1)}" cy="${y(c).toFixed(1)}" r="1.8" fill="${color}">` +
             `<title>${profile[i].hour}:00  ${label} ${c.toFixed(1)} c/kWh</title></circle>`;
    }).join('');
    const zeroLine = lo < 0
      ? `<line x1="0" y1="${y(0).toFixed(1)}" x2="${W}" y2="${y(0).toFixed(1)}" stroke="var(--divider-color)" stroke-width="0.8"/>`
      : '';
    return `<svg width="100%" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="display:block;height:${H}px">
      ${zeroLine}
      ${this._hourTicks(H, BAR, GAP, isFullscreen)}
      ${line(imp, GridLensCard.SPEND_COLOR)}
      ${line(exp, GridLensCard.INCOME_COLOR)}
      ${dots(imp, GridLensCard.SPEND_COLOR, 'buy')}
      ${dots(exp, GridLensCard.INCOME_COLOR, 'sell')}
    </svg>${this._hourLabelRow(isFullscreen)}`;
  }

  // "Wed 20 Aug, 10pm" — compact enough for a one-line spike row.
  _fmtSpikeTime(iso) {
    try {
      const d = new Date(iso);
      const day = d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
      let h = d.getHours();
      const ampm = h >= 12 ? 'pm' : 'am';
      h = h % 12; if (h === 0) h = 12;
      return `${day}, ${h}${ampm}`;
    } catch (e) { return ''; }
  }

  // Real historical intervals a spot-priced plan's rate cleared 2x its own
  // period-median — see plan_calculator._calculate_plan_cost_with_battery_
  // optimization's "Spikes" comment for why these are detected on the raw
  // schedule rather than the (hour-of-day averaged) profile a single spike
  // would otherwise vanish into. `spikes` is pre-sorted highest-rate-first.
  renderSpikes(spikes) {
    if (!spikes || !spikes.length) return '';
    const SHOW = 6;
    const rows = spikes.slice(0, SHOW).map(sp => {
      const when = this._fmtSpikeTime(sp.timestamp);
      let color, label, rateTxt, amtTxt;
      if (sp.direction === 'both') {
        color = GridLensCard.SOC_COLOR;
        label = 'Buy &amp; sell';
        rateTxt = `${(sp.import_rate * 100).toFixed(1)}c / ${(sp.export_rate * 100).toFixed(1)}c`;
        amtTxt = `$${sp.export_credit.toFixed(2)}`;
      } else if (sp.direction === 'export') {
        color = GridLensCard.INCOME_COLOR;
        label = 'Sell';
        rateTxt = `${(sp.export_rate * 100).toFixed(1)}c/kWh`;
        amtTxt = `$${sp.export_credit.toFixed(2)}`;
      } else {
        color = GridLensCard.SPEND_COLOR;
        label = 'Buy';
        rateTxt = `${(sp.import_rate * 100).toFixed(1)}c/kWh`;
        amtTxt = `$${sp.import_cost.toFixed(2)}`;
      }
      return `<div class="spike-row">
        <span class="spike-dot" style="background:${color}"></span>
        <span class="spike-label" style="color:${color}">${label} ${rateTxt}</span>
        <span class="spike-when">${when}</span>
        <span class="spike-amt" style="color:${color}">${amtTxt}</span>
      </div>`;
    }).join('');
    const more = spikes.length > SHOW
      ? `<div class="spike-more">+${spikes.length - SHOW} more this period</div>` : '';
    return `
      <div class="spike-section">
        <div class="chart-label" style="margin-top:10px">⚡ Spikes (&gt;2&times; normal price)</div>
        ${rows}
        ${more}
      </div>`;
  }

  renderSocChart(profile, scale = 1, isFullscreen = false) {
    if (!profile || !profile.length) return '';
    const W = 288, H = Math.round(50 * scale), BAR = 11, GAP = 1;
    const yOf = (soc) => H - (soc || 0) / 100 * (H - 4) - 2;
    const pts = profile.map((slot, i) => [i * (BAR + GAP) + BAR / 2, yOf(slot.soc_percent)]);
    const line = smoothPath(pts);
    const firstX = pts[0][0].toFixed(1), lastX = pts[pts.length - 1][0].toFixed(1);
    const gid = this._nextGid();
    const y20 = yOf(20), y80 = yOf(80);
    const hovers = this._chartHovers(profile, W, H, BAR, GAP, (slot) =>
      `${slot.hour}:00  SOC ${(slot.soc_percent || 0).toFixed(0)}%`);
    return `<svg width="100%" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="display:block;height:${H}px">
      <defs>${gradDef(gid, GridLensCard.SOC_COLOR, 0.75, 1)}</defs>
      <line x1="0" y1="${y80.toFixed(1)}" x2="${W}" y2="${y80.toFixed(1)}" stroke="var(--divider-color)" stroke-width="0.5" stroke-dasharray="3,3"/>
      <line x1="0" y1="${y20.toFixed(1)}" x2="${W}" y2="${y20.toFixed(1)}" stroke="var(--divider-color)" stroke-width="0.5" stroke-dasharray="3,3"/>
      <path d="${line} L${lastX},${H} L${firstX},${H} Z" fill="url(#${gid})"/>
      <path d="${line}" fill="none" stroke="${GridLensCard.SOC_COLOR}" stroke-width="1.5" stroke-linejoin="round" opacity="0.95"/>
      ${this._hourTicks(H, BAR, GAP, isFullscreen)}
      ${hovers}
    </svg>${this._hourLabelRow(isFullscreen)}`;
  }

  // Diagonal hazard-stripe flag shown under an alternative plan's charts whenever
  // "Exclude Greedy Consumption" is checked (see the checkbox's own tooltip and
  // GRIDLENS_CHECKLIST.md 2026-08-30). That toggle subtracts each deferrable device's
  // tracked opportunistic Greedy Consumption energy from its LP demand target before
  // pricing every ALTERNATIVE plan — never the current plan, whose cost is always the
  // real bill (plan_calculator._get_deferrable_data) — so an alternative plan's hourly
  // profile and total shown while the toggle is on are a deliberately adjusted estimate,
  // not this household's literal historical usage. User request 2026-08-30: make that
  // adjustment visually obvious rather than something only the checkbox's tooltip says.
  _greedyStripeHtml(isCurrentPlan) {
    if (isCurrentPlan || !this._excludeGreedy) return '';
    return `
      <div class="greedy-stripe" title="Computed with each deferrable device's opportunistic Greedy Consumption energy subtracted from its demand target — an adjusted estimate for comparing plans, not this household's literal usage on this plan.">
        <span class="greedy-stripe-icon">⚡</span>
        Excludes Greedy Consumption — adjusted estimate
      </div>`;
  }

  // ── Plan history ─────────────────────────────────────────────────────────

  _esc(str) {
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // Small "(2pm–8pm)" suffix for a period-based bill_items line — omitted
  // entirely for flat/all-hours items (backend sends time_range: null there).
  _timeRangeHtml(timeRange) {
    return timeRange ? ` <span style="font-weight:400;opacity:0.65">(${this._esc(timeRange)})</span>` : '';
  }

  // Plan keys are "Retailer - Plan Name"; the retailer is everything before the
  // FIRST " - ", because plan names contain the separator too ("Standing Offer -
  // Time of Use", "Origin Go Variable Ongoing - New & Move Customers only").
  _retailerOf(key) {
    return String(key).split(' - ')[0].trim();
  }

  _planNameOf(key) {
    const idx = String(key).indexOf(' - ');
    return idx === -1 ? '' : String(key).slice(idx + 3).trim();
  }

  // "9 Sep" or "9 – 14 Sep" for a bill_items segment's date range. seg_end from
  // the backend is exclusive (half-open, see _plan_history_segments) — the last
  // real day covered is one day earlier, so that's what gets shown as the end.
  _fmtBillDateRange(startIso, endIso) {
    const opts = { day: 'numeric', month: 'short' };
    const start = new Date(startIso);
    const inclusiveEnd = new Date(new Date(endIso).getTime() - 1);
    const startStr = start.toLocaleDateString([], opts);
    const endStr = inclusiveEnd.toLocaleDateString([], opts);
    return startStr === endStr ? startStr : `${startStr} – ${endStr}`;
  }

  // Itemised bill rows for one bill_items object (from a plan priced whole,
  // or from one segment of a multi-segment split — same shape either way, see
  // _compute_bill_items/_compute_multi_segment_bill_items in plan_calculator.py).
  // Section order deliberately mirrors a real retailer bill (fixed charges
  // first, then usage, then export credits, then bonus credits, then total) so
  // a customer can tick this off against their actual bill line by line — see
  // the GloBird ZEROHERO reconciliation this was modelled on. Don't reorder
  // without re-checking against a real bill sample.
  // GST is a flat 10%-of-ex-GST / (1/11)-of-inc-GST addition on every amount
  // in bill_items (see plan_calculator.py's _compute_bill_items docstring) —
  // so a per-line ex-GST figure is pure display arithmetic, not something
  // the backend needs to compute and carry per line. `sign` is a literal
  // HTML prefix (e.g. '&minus;' for a credit row) applied to both figures.
  _dualAmountHtml(amount, sign = '') {
    const inc = (amount || 0).toFixed(2);
    const exc = ((amount || 0) / 1.1).toFixed(2);
    return `${sign}$${exc}<span class="amount-secondary">incl. GST ${sign}$${inc}</span>`;
  }

  // Feed-in/export credits aren't a taxable supply, so there's no ex-GST/incl-GST
  // split to show — but the amount still goes on the 2nd line so it lines up
  // vertically with every other row's (GST-bearing) figure.
  _amountHtmlNoGst(amount, sign = '') {
    return `&nbsp;<span class="amount-secondary">${sign}$${(amount || 0).toFixed(2)}</span>`;
  }

  _renderBillRows(bi) {
    let rows = '';

    // Supply charge
    const s = bi.supply;
    rows += '<div class="bill-section-head">Daily supply charge</div>';
    rows += `<div class="breakdown-row">
      <div class="breakdown-label">Supply charge<br>
        <span style="font-size:11px;opacity:0.7">${(s.rate_per_day * 100).toFixed(2)}&thinsp;c/day &times; ${s.days}&thinsp;days</span>
      </div>
      <div class="breakdown-value">${this._dualAmountHtml(s.amount)}</div>
    </div>`;

    // Subscription fee (e.g. Amber $25/month)
    if (bi.subscription) {
      const sub = bi.subscription;
      rows += '<div class="bill-section-head">Subscription fee</div>';
      rows += `<div class="breakdown-row">
        <div class="breakdown-label">Membership subscription<br>
          <span style="font-size:11px;opacity:0.7">$${sub.rate_per_month.toFixed(2)}/month &times; ${sub.months.toFixed(1)}&thinsp;months</span>
        </div>
        <div class="breakdown-value">${this._dualAmountHtml(sub.amount)}</div>
      </div>`;
    }

    // Demand charge (peak-kW), only present when on a demand tariff.
    // `dm.lines` = per-season sub-lines (a plan carrying demand_periods,
    // itemised the way the retailer bills each season); otherwise a
    // single flat line (the network-level demand charge).
    if (bi.demand) {
      const dm = bi.demand;
      rows += '<div class="bill-section-head">Demand charge</div>';
      const dmLines = (dm.lines && dm.lines.length) ? dm.lines : [dm];
      dmLines.forEach(ln => {
        rows += `<div class="breakdown-row">
          <div class="breakdown-label">${ln.label}${this._timeRangeHtml(ln.time_range)}<br>
            <span style="font-size:11px;opacity:0.7">${(ln.peak_kw || 0).toFixed(2)}&thinsp;kW peak &times; ${((ln.rate_per_kw_per_day || 0) * 100).toFixed(2)}&thinsp;c/kW/day &times; ${ln.days}&thinsp;days</span>
          </div>
          <div class="breakdown-value">${this._dualAmountHtml(ln.amount || 0)}</div>
        </div>`;
      });
    }

    // Controlled Load — device(s) wired to a CL register, priced separately
    // at the plan's flat CL rate instead of the general import tiers below.
    if (bi.controlled_load) {
      rows += '<div class="bill-section-head">Controlled load</div>';
      bi.controlled_load.lines.forEach(line => {
        rows += `<div class="breakdown-row">
          <div class="breakdown-label">${line.label}<br>
            <span style="font-size:11px;opacity:0.7">${line.rate_c.toFixed(2)}&thinsp;c/kWh &times; ${line.kwh.toFixed(1)}&thinsp;kWh</span>
          </div>
          <div class="breakdown-value">${this._dualAmountHtml(line.amount)}</div>
        </div>`;
      });
    }

    // Energy (usage) lines
    rows += '<div class="bill-section-head">Usage charges</div>';
    bi.energy_lines.forEach(line => {
      rows += `<div class="breakdown-row">
        <div class="breakdown-label">${line.label}${this._timeRangeHtml(line.time_range)}<br>
          <span style="font-size:11px;opacity:0.7">${line.rate_c.toFixed(2)}&thinsp;c/kWh &times; ${line.kwh.toFixed(1)}&thinsp;kWh</span>
        </div>
        <div class="breakdown-value">${this._dualAmountHtml(line.amount)}</div>
      </div>`;
    });

    // Feed-in credit — one line per FiT tier/window (e.g. a capped "top up"
    // rate separate from the base feed-in rate), same as a real bill.
    const f = bi.fit;
    if (f.kwh > 0 && f.lines && f.lines.length) {
      rows += '<div class="bill-section-head bill-fit">Solar / feed-in credit</div>';
      f.lines.forEach(line => {
        const rateLabel = line.rate_c != null
          ? `${line.rate_c.toFixed(2)}&thinsp;c/kWh &times; ${line.kwh.toFixed(1)}&thinsp;kWh`
          : `spot price &times; ${line.kwh.toFixed(1)}&thinsp;kWh`;
        rows += `<div class="breakdown-row bill-fit">
          <div class="breakdown-label" style="color:inherit">${line.label}${this._timeRangeHtml(line.time_range)}<br>
            <span style="font-size:11px;opacity:0.7">${rateLabel}</span>
          </div>
          <div class="breakdown-value" style="color:inherit">${this._amountHtmlNoGst(line.amount, '&minus;')}</div>
        </div>`;
      });
    }

    // VPP participation credit
    if (bi.vpp_credit) {
      rows += '<div class="bill-section-head bill-fit">VPP credit</div>';
      rows += `<div class="breakdown-row bill-fit">
        <div class="breakdown-label" style="color:inherit">VPP participation credit</div>
        <div class="breakdown-value" style="color:inherit">${this._amountHtmlNoGst(bi.vpp_credit, '&minus;')}</div>
      </div>`;
    }

    // PEA (Price Efficiency Adjustment — Flow Power)
    if (bi.pea_credit != null || bi.pea_breakdown) {
      const pb = bi.pea_breakdown;
      rows += '<div class="bill-section-head bill-fit">Other credits</div>';
      if (pb) {
        const creditSign = bi.pea_credit >= 0 ? '&minus;' : '+';
        const creditAbs = Math.abs(bi.pea_credit);
        rows += `<div class="breakdown-row bill-fit">
          <div class="breakdown-label" style="color:inherit">Price Efficiency Adjustment<br>
            <span style="font-size:11px;opacity:0.7">
              LWAP ${pb.lwap_c.toFixed(3)}c &minus; TWAP ${pb.twap_c.toFixed(3)}c = CPEA ${pb.cpea_c.toFixed(3)}c<br>
              PEA = CPEA &minus; BPEA ${pb.bpea_c.toFixed(1)}c
              = <strong>${pb.pea_c.toFixed(3)}c/kWh</strong>
              &times; ${pb.total_kwh.toFixed(1)}&thinsp;kWh
            </span>
          </div>
          <div class="breakdown-value" style="color:inherit">${this._amountHtmlNoGst(creditAbs, creditSign)}</div>
        </div>`;
      } else {
        rows += `<div class="breakdown-row bill-fit">
          <div class="breakdown-label" style="color:inherit">Price Efficiency Adjustment<br>
            <span style="font-size:11px;opacity:0.7">Estimated — actual amount varies</span>
          </div>
          <div class="breakdown-value" style="color:inherit">${this._amountHtmlNoGst(bi.pea_credit, '&minus;')}</div>
        </div>`;
      }
    }

    // Conditional day-credits (e.g. GloBird ZEROHERO's $1/day)
    if (bi.conditional_credits) {
      rows += '<div class="bill-section-head bill-fit">Conditional credits</div>';
      Object.entries(bi.conditional_credits).forEach(([label, c]) => {
        rows += `<div class="breakdown-row bill-fit">
          <div class="breakdown-label" style="color:inherit">${label}${this._timeRangeHtml(c.time_range)}<br>
            <span style="font-size:11px;opacity:0.7">Earned ${c.days_earned}/${c.days_total}&thinsp;days</span>
          </div>
          <div class="breakdown-value" style="color:inherit">${this._amountHtmlNoGst(c.amount, '&minus;')}</div>
        </div>`;
      });
    }

    // Subtotal / GST / Total — shown as a 3-line summary (rather than just
    // the inc-GST total) because retailer bills split this two different
    // ways: some itemise ex-GST and add GST at the bottom, others fold GST
    // into every line like we do and print only the inc-GST total. Showing
    // all three lets a customer match ours against whichever their bill uses.
    if (bi.total_ex_gst != null) {
      rows += `<div class="bill-gst-row bill-subtotal-row">
        <span>Subtotal (ex. GST)</span><span>$${bi.total_ex_gst.toFixed(2)}</span>
      </div>`;
    }
    rows += `<div class="bill-gst-row">
      <span>GST (10%)</span><span>$${bi.gst_included.toFixed(2)}</span>
    </div>`;
    rows += `<div class="bill-total-row">
      <span>Total (inc. GST)</span><span>$${bi.total.toFixed(2)}</span>
    </div>`;

    if (bi.optimisation_note) {
      rows += `<div class="bill-note">${bi.optimisation_note}</div>`;
    }
    if (bi.spot_note) {
      rows += `<div class="bill-spot-note">${bi.spot_note}</div>`;
    }

    return rows;
  }

  // Compact plan card for a period section (see _renderPeriodSection) — same
  // visual language as the full plan-card (title, coloured cost banner,
  // itemised breakdown via _renderBillRows) but with no hourly chart: a
  // period's alternatives are priced against just that period's own usage
  // slice (_rank_plans_for_period in plan_calculator.py), which carries no
  // hour-of-day averages to chart from. Colour legend matches the main flat
  // view: amber=the plan actually held that period, deep-cyan=cheapest
  // contender, green=cheaper than the actual bill, red=pricier.
  // No dispatch summary here (unlike the flat view's _renderDispatchSummary): with
  // no hourly profile there is nothing computed to summarise, and the static
  // per-plan `strategy` text it used to show is retired (see that method).
  _renderPeriodPlanCard(planName, billItems, total, isCurrentPlan, isCheapest, savings, showBreakdown) {
    let bannerColor, savingsLabel;
    if (isCurrentPlan) {
      bannerColor = GridLensCard.HOUSEHOLD_COLOR;
      savingsLabel = 'ACTUAL BILL THIS PERIOD';
    } else if (isCheapest) {
      bannerColor = GridLensCard.SOC_COLOR;
      savingsLabel = savings < -0.05 ? `SAVE $${Math.abs(savings).toFixed(2)}` : 'CHEAPEST THIS PERIOD';
    } else if (savings < -0.05) {
      bannerColor = GridLensCard.SELLING_COLOR;
      savingsLabel = `SAVE $${Math.abs(savings).toFixed(2)}`;
    } else {
      bannerColor = GridLensCard.SPEND_COLOR;
      savingsLabel = `COSTS $${savings.toFixed(2)} more`;
    }
    const breakdownHtml = (showBreakdown && billItems)
      ? `<div class="breakdown-section">${this._renderBillRows(billItems)}</div>` : '';
    return `
      <div class="plan-card period-plan-card${isCurrentPlan ? ' current-plan' : ''}" data-retailer="${this._esc(this._retailerOf(planName))}">
        <div class="plan-title">
          <div class="plan-title-retailer">${this._esc(this._retailerOf(planName))}</div>
          <div class="plan-title-name">${this._esc(this._planNameOf(planName))}</div>
        </div>
        <div class="cost-display" style="background:${bannerColor}">
          <div class="cost-amount">$${total.toFixed(2)}</div>
          <div class="cost-label">${savingsLabel}</div>
        </div>
        ${breakdownHtml}
      </div>`;
  }

  // Plain-language summary of what the LP actually did on this plan, built from
  // the 24-slot hour-of-day averaged `hourly_profile` (kWh per slot = kWh/day at
  // that hour). Replaces the hand/AI-written per-plan `strategy` text (retired
  // 2026-10-16): that text quoted rates that went stale on every price update,
  // gave battery advice to installs with no battery, and could contradict the LP
  // ranking the very same card. Everything here comes from this install's own
  // optimisation, so rates always match the plan's priced schedule and clauses
  // for hardware the install doesn't have simply never appear.
  _renderDispatchSummary(profile) {
    if (!profile || profile.length !== 24) return '';
    const v = (s, k) => s[k] || 0;
    const defOf = (s) => {
      const per = s.deferrable_per_device || [];
      return per.length ? per.reduce((a, x) => a + x, 0) : v(s, 'deferrable_kwh');
    };
    // Grid share of each hour's battery charge: whatever charge solar surplus
    // (solar beyond household + flexible load) can't account for, bounded by
    // what was actually imported. Approximate on averaged slots, which is fine
    // for picking out a window.
    const gridChg = profile.map(s => {
      const surplus = Math.max(0, v(s, 'solar_kwh') - v(s, 'home_load_kwh') - defOf(s));
      return Math.min(v(s, 'import_kwh'), Math.max(0, v(s, 'charge_kwh') - surplus));
    });
    const series = {
      grid: gridChg,
      dis: profile.map(s => v(s, 'discharge_kwh')),
      exp: profile.map(s => v(s, 'export_kwh')),
      def: profile.map(defOf),
    };
    // An hour counts toward a window when it carries a meaningful share of that
    // flow — absolute floor so a near-idle series yields no window at all, plus
    // a relative floor so one dominant window isn't smeared across the day by
    // averaging noise.
    const active = (arr) => {
      const max = Math.max(...arr);
      const thr = Math.max(0.05, max * 0.15);
      const on = arr.map(x => x >= thr);
      // Bridge a single quiet hour between two active ones, so one dispatch
      // window isn't printed as "09:00–10:00, 11:00–15:00" over an averaging dip.
      return on.map((x, h) => x || (on[(h + 23) % 24] && on[(h + 1) % 24]));
    };
    // Contiguous runs of active hours, joined across midnight; at most 3, largest
    // energy first, then shown in clock order.
    const windows = (arr) => {
      const on = active(arr);
      if (!on.some(Boolean)) return [];
      if (on.every(Boolean)) return [[0, 24]];
      const start = on.findIndex((x, i) => !x && on[(i + 1) % 24]) + 1;
      const runs = [];
      let cur = null;
      for (let k = 0; k < 24; k++) {
        const h = (start + k) % 24;
        if (on[h]) {
          if (!cur) cur = { s: h, n: 0, kwh: 0 };
          cur.n++; cur.kwh += arr[h];
        } else if (cur) { runs.push(cur); cur = null; }
      }
      if (cur) runs.push(cur);
      return runs.sort((a, b) => b.kwh - a.kwh).slice(0, 3)
        .sort((a, b) => a.s - b.s).map(r => [r.s, r.s + r.n]);
    };
    const hh = (h) => `${String(h % 24).padStart(2, '0')}:00`;
    const fmtWin = (ws) => ws.map(([a, b]) => b - a >= 24 ? 'all day' : `${hh(a)}–${hh(b)}`).join(', ');
    const total = (arr) => arr.reduce((a, x) => a + x, 0);
    // kWh-weighted average of a rate over a flow — the price that flow actually
    // met, not a plain mean over hours where it barely happened.
    const wRate = (arr, key) => {
      const kwh = total(arr);
      if (kwh <= 0) return null;
      return profile.reduce((a, s, h) => a + arr[h] * v(s, key), 0) / kwh;
    };
    const c = (r) => `${(r * 100).toFixed(1)}c`;
    const kwh = (x) => `${x.toFixed(1)} kWh/day`;

    const lines = [];
    const add = (arr, minDaily, text) => {
      if (total(arr) < minDaily) return;
      const ws = windows(arr);
      if (ws.length) lines.push(text(fmtWin(ws), total(arr)));
    };
    add(series.grid, 0.3, (w, k) => {
      const r = wRate(series.grid, 'import_rate');
      return `Charges the battery from the grid ${w}${r != null ? ` at ~${c(r)}` : ''} (${kwh(k)})`;
    });
    add(series.dis, 0.3, (w, k) => {
      const r = wRate(series.dis, 'import_rate');
      // Discharge may serve the home or be exported, so state the buy price it
      // coincides with rather than claiming it all displaced imports.
      return `Discharges the battery ${w}${r != null ? `, while grid power costs ~${c(r)}` : ''} (${kwh(k)})`;
    });
    add(series.exp, 0.3, (w, k) => {
      const r = wRate(series.exp, 'export_rate');
      return `Exports ${kwh(k)}, ${w === 'all day' ? 'across the day' : `mostly ${w}`}${r != null ? ` at ~${c(r)}` : ''}`;
    });
    add(series.def, 0.3, (w, k) => `Schedules flexible loads ${w} (${kwh(k)})`);
    if (!lines.length) return '';
    return `
      <div class="strategy-box">
        <div class="strategy-title">📋 How the optimiser runs this plan</div>
        <ul class="strategy-list">${lines.map(l => `<li>${this._esc(l)}</li>`).join('')}</ul>
        <div class="strategy-foot">Typical day, averaged over the comparison period — not a fixed daily schedule.</div>
      </div>`;
  }

  // One vertical section per bill boundary: the plan actually held that period
  // (priced from its own real usage), then the top `topN` cheapest contenders
  // re-priced against that SAME period's own usage slice and ranked — not a
  // shortlist carried over from the whole-window ranking, since a short or
  // unusual period can genuinely favour a different plan (see
  // _rank_plans_for_period's docstring in plan_calculator.py). `topN` can be
  // Infinity ("Show best: All").
  _renderPeriodSection(period, topN, showBreakdown) {
    const curTotal = period.current_plan_total || 0;

    const allAlts = period.alternatives || [];
    const alts = Number.isFinite(topN) ? allAlts.slice(0, topN) : allAlts;
    // Cheapest across the actual plan AND its shown contenders together — not
    // just the contenders — so an alternative only gets the "cheapest" banner
    // when it genuinely beats (or ties) what was actually held, same rule the
    // single-period flat view uses.
    const minTotal = alts.reduce((m, a) => Math.min(m, a.total), curTotal);

    // One cost-sorted grid, actual plan included inline rather than pulled into
    // its own row above — easier to compare at a glance. isCurrentPlan still
    // forces the amber banner regardless of where it lands in the sort.
    const cards = [
      {
        total: curTotal,
        html: this._renderPeriodPlanCard(
          period.current_plan_name || '(unknown plan)', period.current_plan_bill_items,
          curTotal, true, false, 0, showBreakdown,
        ),
      },
      ...alts.map(a => {
        const bi = (a.breakdown || {}).bill_items || null;
        const isCheapest = Math.abs(a.total - minTotal) < 0.01;
        return {
          total: a.total,
          html: this._renderPeriodPlanCard(
            a.plan_key, bi, a.total, false, isCheapest, a.total - curTotal, showBreakdown,
          ),
        };
      }),
    ];
    cards.sort((a, b) => a.total - b.total);
    const cardsHtml = cards.map(c => c.html).join('');

    const dateRange = this._fmtBillDateRange(period.start_date, period.end_date);
    const countNote = allAlts.length > alts.length
      ? ` <span class="period-alt-count">(best ${alts.length} of ${allAlts.length} alternatives)</span>` : '';

    return `
      <div class="period-section">
        <div class="period-section-head">
          <span class="period-section-dates">${dateRange}</span>
          <span class="period-section-days">${period.days}&thinsp;day${period.days === 1 ? '' : 's'}${countNote}</span>
        </div>
        <div class="plan-grid">${cardsHtml}</div>
      </div>`;
  }

  async fetchHistory() {
    try {
      const res = await fetch('/api/grid_lens/plan_history');
      if (res.ok) { this._history = (await res.json()).entries || []; this.render(); }
    } catch (e) { console.error('fetchHistory:', e); }
  }

  async saveHistoryEntry(entry) {
    const isNew = !entry.id;
    const url = isNew
      ? '/api/grid_lens/plan_history'
      : `/api/grid_lens/plan_history/${entry.id}`;
    try {
      const res = await fetch(url, {
        method: isNew ? 'POST' : 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(entry),
      });
      if (res.ok) await this.fetchHistory();
    } catch (e) { console.error('saveHistoryEntry:', e); }
  }

  async deleteHistoryEntry(id) {
    try {
      const res = await fetch(`/api/grid_lens/plan_history/${id}`, {method: 'DELETE'});
      if (res.ok) { this._history = this._history.filter(e => e.id !== id); this.render(); }
    } catch (e) { console.error('deleteHistoryEntry:', e); }
  }

  /** Show only plans whose retailer matches the search box, plus the plan the
   *  user is currently on — which stays visible under any filter so there's
   *  always a baseline to compare the shown alternatives against. Presentational
   *  only — nothing is removed from this._data, so chart scaling stays global
   *  and the History panel still sees every plan. */
  _applyRetailerFilter() {
    if (!this.shadowRoot) return;
    const needle = (this._retailerFilter || '').trim().toLowerCase();
    const cards = this.shadowRoot.querySelectorAll('.plan-card[data-retailer]');
    let shown = 0;
    cards.forEach((el) => {
      const hit = !needle
        || el.classList.contains('current-plan')
        || (el.getAttribute('data-retailer') || '').toLowerCase().includes(needle);
      el.hidden = !hit;
      if (hit) shown++;
    });
    // Placeholders stand in for plans not yet priced, and there is no way to
    // know their retailer — showing them under an active filter would claim a
    // match we cannot support.
    this.shadowRoot.querySelectorAll('.plan-card.skeleton')
      .forEach((el) => { el.hidden = !!needle; });

    const count = this.shadowRoot.getElementById('epc-filter-count');
    if (count) {
      count.textContent = needle ? `${shown} of ${cards.length}` : '';
    }
  }

  renderHistoryPanel(planNames) {
    if (this._history === null) {
      return '<div style="padding:24px;text-align:center;color:var(--secondary-text-color)">Loading…</div>';
    }
    const entries = [...this._history].sort((a, b) => b.date.localeCompare(a.date));
    const planOpts = planNames.map(n => `<option value="${this._esc(n)}">${this._esc(n)}</option>`).join('');

    const addRow = this._addingNew ? `
      <tr>
        <td><input type="date" id="epc-new-date" value="${new Date().toISOString().substring(0,10)}" class="hist-input"></td>
        <td><select id="epc-new-plan" class="hist-input">${planOpts}</select></td>
        <td><input type="text" id="epc-new-notes" placeholder="Notes (optional)" class="hist-input" style="width:100%"></td>
        <td class="hist-actions">
          <button id="epc-new-save" class="hist-btn">Save</button>
          <button id="epc-new-cancel" class="hist-btn hist-btn-sec">Cancel</button>
        </td>
      </tr>` : `
      <tr><td colspan="4"><button id="epc-add-btn" class="hist-btn">+ Add plan change</button></td></tr>`;

    const rows = entries.map(e => {
      if (this._editingId === e.id) {
        const selOpts = planNames.map(n =>
          `<option value="${this._esc(n)}"${n === e.plan_name ? ' selected' : ''}>${this._esc(n)}</option>`
        ).join('');
        return `<tr>
          <td><input type="date" id="epc-edit-date" value="${e.date}" class="hist-input"></td>
          <td><select id="epc-edit-plan" class="hist-input">${selOpts}</select></td>
          <td><input type="text" id="epc-edit-notes" value="${this._esc(e.notes||'')}" class="hist-input" style="width:100%"></td>
          <td class="hist-actions">
            <button id="epc-edit-save" data-id="${e.id}" class="hist-btn">Save</button>
            <button id="epc-edit-cancel" class="hist-btn hist-btn-sec">Cancel</button>
          </td>
        </tr>`;
      }
      return `<tr>
        <td>${e.date}</td>
        <td>${this._esc(e.plan_name)}</td>
        <td style="color:var(--secondary-text-color);font-size:12px">${this._esc(e.notes||'')}</td>
        <td class="hist-actions">
          <button class="epc-hist-edit hist-btn hist-btn-sec" data-id="${e.id}">Edit</button>
          <button class="epc-hist-del hist-btn hist-btn-del" data-id="${e.id}">Delete</button>
        </td>
      </tr>`;
    }).join('');

    return `<div class="hist-section">
      <p class="hist-note">When querying a historical date range, the plan you were on at that time is automatically used as the baseline for comparison.</p>
      <table class="hist-table">
        <thead><tr><th>Switched to on</th><th>Plan</th><th>Notes</th><th></th></tr></thead>
        <tbody>${addRow}${rows}</tbody>
      </table>
    </div>`;
  }

  // ── Bill upload / intake ──────────────────────────────────────────────────

  // The panel behind the "Bills" button: what is stored, and how to add one.
  //
  // Bills are entered as the parsed figures, not as a PDF: PDF extraction needs
  // the fact-sheet pipeline (pdftotext/OCR on the GPU box plus a local LLM with
  // the grounding guards), which lives in gridlens-api and is reached over the
  // network — so it arrives with the cloud parser, and this panel is already
  // shaped to take its output. Nothing here has a field for a name, address,
  // NMI or account number, and the backend REJECTS any key it does not
  // recognise, so there is nowhere for those to land even by accident.
  renderBillsPanel() {
    if (this._bills === null) {
      return '<div style="padding:24px;text-align:center;color:var(--secondary-text-color)">Loading…</div>';
    }
    const _e = (t) => this._esc(t == null ? '' : t);
    const rows = this._bills.map(b => {
      const f = b.facts || {};
      const days = (f.periods || []).reduce((n, p) => n + (p.days || 0), 0);
      const gen = (f.periods || []).reduce((n, p) => n + (p.general_kwh || 0), 0);
      const cl = (f.periods || []).reduce((n, p) => n + (p.cl_kwh || 0), 0);
      const exp = (f.periods || []).reduce((n, p) => n + (p.export_kwh || 0), 0);
      const chk = b.self_check || {};
      const chkLabel = !chk.verifiable
        ? '<span class="bill-chip bill-chip-warn" title="The parse captured no printed amounts, so its arithmetic has not been checked against anything — not the same as verified.">unverified</span>'
        : (chk.ok
            ? '<span class="bill-chip bill-chip-ok" title="Every line, period and total reconstructs from kWh x rate.">reconciles</span>'
            : `<span class="bill-chip bill-chip-bad" title="${_e((chk.problems || []).join(' / '))}">does not reconcile</span>`);
      const a = b.answers || {};
      const pv = a.pv_kw ? `${a.pv_kw} kW` : 'PV size not given';
      const active = this._source === `bill:${b.id}`;
      return `<tr class="${active ? 'bill-active' : ''}">
        <td>
          <strong>${_e(b.label)}</strong><br>
          <span class="bill-sub">${_e(f.retailer)} · ${_e(f.plan_name)} · ${_e(f.network)}</span>
        </td>
        <td>${days} days<br><span class="bill-sub">${gen.toFixed(0)} kWh general${cl > 0 ? ` · ${cl.toFixed(0)} kWh CL` : ''}${exp > 0 ? ` · ${exp.toFixed(0)} kWh export` : ''}</span></td>
        <td>${chkLabel}<br><span class="bill-sub">${_e(pv)} · ${_e(a.archetype || '')}</span></td>
        <td class="hist-actions">
          ${active
            ? '<button class="hist-btn hist-btn-sec" disabled>In use</button>'
            : `<button class="epc-bill-use hist-btn" data-id="${_e(b.id)}">Compare</button>`}
          <button class="epc-bill-del hist-btn hist-btn-del" data-id="${_e(b.id)}">Delete</button>
        </td>
      </tr>`;
    }).join('');

    const archOpts = this._archetypes.map(a =>
      `<option value="${_e(a.id)}">${_e(a.label)} — ${_e(a.question)}</option>`).join('');

    return `<div class="hist-section">
      <p class="hist-note">
        Compare a household that has never run Grid Lens, from its paper bill alone.
        Everything on this page then works as usual — ranking, bill breakdown, charts
        and What-If — driven by a reconstructed half-hourly model instead of the
        recorder. The bill's own arithmetic has to reconcile before it is accepted.
      </p>
      <table class="hist-table">
        <thead><tr><th>Bill</th><th>Period</th><th>Model</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="bill-sub">No bills stored yet.</td></tr>'}</tbody>
      </table>

      <div class="bill-add">
        <h4>Add a bill</h4>
        <p class="bill-sub">
          Paste the bill's figures as JSON — one entry in <code>periods</code> per rate
          period the bill prints (retailers split a bill whenever rates change
          mid-cycle, and each slice carries its own rates and day count).
          Include <code>printed_energy_total</code> so the arithmetic can be checked.
          Rates are in dollars, not cents.
        </p>
        <textarea id="epc-bill-json" rows="12" spellcheck="false"
                  placeholder="Bill JSON">${_e(this._billDraft)}</textarea>
        <div class="bill-fields">
          <label>Inverter size
            <input type="number" id="epc-bill-pv" min="0" step="0.1" placeholder="kW (leave blank if unknown)">
          </label>
          <label>Daytime occupancy
            <select id="epc-bill-arch">${archOpts}</select>
          </label>
          <label>Hot water element
            <input type="number" id="epc-bill-clkw" min="0" step="0.1" placeholder="kW (optional)">
          </label>
        </div>
        <p class="bill-sub">
          The inverter's kW rating is the single most valuable input here. Without it
          every system from roughly 3.5 kW to 12 kW fits a bill's import and export
          registers equally well, each with a compensating daytime load share — and a
          10 kWh battery's annual saving spans a factor of two and a half across that
          range. Entering it collapses the ambiguity; leaving it blank is allowed but
          every modelled figure is then presented as a range.
        </p>
        <div class="whatif-presets">
          <button id="epc-bill-example" class="nav-btn">Insert example</button>
          <button id="epc-bill-save">Add bill</button>
        </div>
        ${this._billError ? `<div class="bill-err">${_e(this._billError)}</div>` : ''}
      </div>
    </div>`;
  }

  // The confidence banner. A bill-sourced figure must never appear without the
  // assumptions that produced it visible beside it — showing a battery payback
  // at the same visual weight as a flat-rate saving is the core presentational
  // mistake this is here to prevent.
  renderBillBanner() {
    const b = this._data && this._data.bill;
    if (!b) return '';
    const _e = (t) => this._esc(t == null ? '' : t);
    const m = b.model || {};
    const cl = b.controlled_load || {};
    const _busy = this._streamPhase !== null;

    const pv = m.pv_identified
      ? `${m.pv_kw_implied} kW (as entered)`
      : `${(m.pv_range_kw || [])[0]}–${(m.pv_range_kw || [])[1]} kW — <strong>not identified</strong>`;

    const scenarios = (this._data.scenarios || []);
    const scenarioPicker = scenarios.length > 1 ? `
      <label class="bill-scenario">Hot water
        <select id="epc-bill-scenario" ${_busy ? 'disabled' : ''}>
          ${scenarios.map(s => `<option value="${_e(s.id)}"${
            s.id === (b.scenario || {}).id ? ' selected' : ''}>${_e(s.label)}</option>`).join('')}
        </select>
      </label>` : '';

    const excluded = this._data.excluded_plans || {};
    const nExcluded = Object.keys(excluded).length;
    const reasonCounts = {};
    // Every gate reason is "<short kind>: <detail>" (see BillGates.reason), so
    // the kind is the part before the first colon — grouping does not depend on
    // parsing the prose.
    for (const v of Object.values(excluded)) {
      const k = String(v.reason || '').split(':')[0];
      reasonCounts[k] = (reasonCounts[k] || 0) + 1;
    }
    const excludedHtml = nExcluded ? `
      <div class="bill-excluded">
        <strong>${nExcluded} plan${nExcluded === 1 ? '' : 's'} not comparable from this bill:</strong>
        ${Object.entries(reasonCounts).map(([r, n]) => `${n} &times; ${_e(r)}`).join(' · ')}.
        These are excluded rather than priced on missing data — a plan priced on
        data a bill cannot supply does not drop out of a ranking, it tops it.
        ${cl.present && cl.assumed_rate == null ? `
          <button id="epc-bill-assume-cl" class="nav-btn" ${_busy ? 'disabled' : ''}
                  title="Carry one assumed controlled-load rate onto plans that have none, so they can be compared. Every figure that uses it is labelled.">
            Assume ${(cl.suggested_rate != null ? (cl.suggested_rate * 100).toFixed(3) : '?')}c/kWh controlled load
          </button>` : ''}
      </div>` : '';

    const assumedHtml = cl.assumed_rate != null ? `
      <div class="bill-assumed">
        &#9888; Controlled load is being priced at an <strong>assumed
        ${(cl.assumed_rate * 100).toFixed(3)}c/kWh</strong> on every plan that has no
        controlled-load rate of its own. That is this household's own current rate
        carried across, not a rate any of those retailers published — and
        ${cl.share_pct}% of this household's usage is on that register, so it can move
        the ranking. Lines using it are marked "(assumed rate)".
        <button id="epc-bill-unassume-cl" class="nav-btn" ${_busy ? 'disabled' : ''}>Stop assuming</button>
      </div>` : '';

    // Our plan data against the rates this bill was actually charged at. The
    // only external check the product gets on its own plan data — everywhere
    // else our rates are verified against PRD, which is to say against
    // themselves. Shown with both figures because the numbers ARE the finding:
    // grouped into the excluded-plans counts line it reads as "1 x Plan data
    // disagrees", which is true and useless. Which side is wrong is genuinely
    // open, so this states the discrepancy and does not adjudicate it.
    const pdc = b.plan_data_check || {};
    const PD_LABELS = {
      general_rate: ['General usage', 'c/kWh'],
      cl_rate: ['Controlled load', 'c/kWh'],
      supply_per_day: ['Daily supply', 'c/day'],
      cl_supply_per_day: ['Controlled-load supply', 'c/day'],
      fit_rate: ['Feed-in tariff', 'c/kWh'],
    };
    const pdDrift = pdc.drift || {};
    const pdKeys = Object.keys(pdDrift);
    const planDataHtml = pdKeys.length ? `
      <div class="bill-plandata">
        <strong>Our rates for ${_e(b.plan_name)} are not the rates this bill was
        charged at.</strong>
        <table class="bill-pd-table">
          <tr><th></th><th>We hold</th><th>Bill charged</th><th></th></tr>
          ${pdKeys.map(k => {
            const d = pdDrift[k];
            const [label, unit] = PD_LABELS[k] || [k, ''];
            return `<tr>
              <td>${_e(label)}</td>
              <td>${(d.ours * 100).toFixed(3)}${_e(unit)}</td>
              <td>${(d.billed * 100).toFixed(3)}${_e(unit)}</td>
              <td class="bill-pd-pct">${d.pct == null ? '' : (d.pct >= 0 ? '+' : '') + d.pct.toFixed(1) + '%'}</td>
            </tr>`;
          }).join('')}
        </table>
        Both sides are GST-inclusive and the bill figure is its most recent rate
        period, so this is a real difference — not GST and not a mid-bill price
        change. Until it is resolved <strong>this plan is excluded from the
        ranking rather than priced</strong>: the row would be labelled as the
        plan you are on, and a wrong figure there mis-states every saving
        measured against it. The baseline above is the bill's own printed
        total, which is unaffected.
      </div>` : (pdc.checked ? `
      <div class="bill-assumed">
        Our rates for ${_e(b.plan_name)} match what this bill was charged — the
        bill independently confirms the plan data behind this comparison.
      </div>` : '');

    const warnings = (m.warnings || []).map(w => `<li>${_e(w)}</li>`).join('');

    return `
      <div class="bill-banner">
        <div class="bill-banner-head">
          <span class="bill-chip bill-chip-modelled">MODELLED</span>
          <strong>${_e(b.retailer)} — ${_e(b.plan_name)}</strong>
          <span class="bill-sub">${_e(b.period)} · ${b.days} days · ${_e(b.network)}</span>
          ${scenarioPicker}
          <button id="epc-bill-exit" class="nav-btn" ${_busy ? 'disabled' : ''}>Back to my data</button>
        </div>
        <div class="bill-banner-grid">
          <div><span class="bill-sub">Bill as issued</span><br>$${(b.billed_total || 0).toFixed(2)}</div>
          <div><span class="bill-sub">Solar system</span><br>${pv}</div>
          <div><span class="bill-sub">Load profile</span><br>${_e(m.archetype_label)} (${m.day_share_pct}% daytime)</div>
          <div><span class="bill-sub">Reproduces import / export</span><br>${(m.import_err_pct >= 0 ? '+' : '') + Number(m.import_err_pct).toFixed(2)}% / ${(m.export_err_pct >= 0 ? '+' : '') + Number(m.export_err_pct).toFixed(2)}%</div>
          ${cl.present ? `<div><span class="bill-sub">Controlled load</span><br>${cl.daily_kwh} kWh/day (${cl.share_pct}% of usage)</div>` : ''}
        </div>
        <div class="bill-caveat">${_e(b.one_season_caveat)}</div>
        ${warnings ? `<ul class="bill-warnings">${warnings}</ul>` : ''}
        ${planDataHtml}
        ${assumedHtml}
        ${excludedHtml}
      </div>`;
  }

  // ── Main render ───────────────────────────────────────────────────────────

  render() {
    if (!this._config || !this._connected) return;

    // render() replaces the whole shadow root, so remember whether the user was
    // mid-type in the filter box and restore focus afterwards. Streaming 'plan'
    // events re-render this card once per plan priced.
    this._filterHadFocus =
      this.shadowRoot?.activeElement?.id === 'epc-retailer';

    const showBreakdown = this._config.show_breakdown !== false;
    const showCharts = this._config.show_charts !== false;
    const chartScale = this._chartScale * (parseFloat(this._config.chart_scale) || 1.0);
    const planFilter = this._config.plan;
    this._gidCounter = 0;  // every chart's gradient ids must be unique across this whole render pass

    const styles = `
      <style>
        :host { display: block; contain: content; }
        .plan-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
          gap: 16px;
          padding: 16px;
        }
        .plan-card {
          background: var(--card-background-color);
          border-radius: 8px;
          padding: 16px;
          border: 1px solid var(--divider-color);
        }
        .plan-card.current-plan {
          border: 2px solid var(--primary-color);
        }
        /* Fixed two-line title (retailer on line 1, plan name on line 2) so cards
           with a long plan name don't grow taller than their neighbours and throw
           the cost banner/chart below out of vertical alignment across the row.
           Each line is clamped to itself with an ellipsis rather than wrapping, so
           a long plan name never spills onto a third line. */
        .plan-title {
          margin-bottom: 8px;
        }
        .plan-title-retailer {
          font-size: 13px;
          font-weight: 400;
          line-height: 1.3;
          color: var(--secondary-text-color);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .plan-title-name {
          font-size: 16px;
          font-weight: 500;
          line-height: 1.3;
          color: var(--primary-text-color);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .cost-display {
          color: #fff;
          padding: 16px;
          border-radius: 6px;
          text-align: center;
          margin: 12px 0;
        }
        .cost-amount { font-size: 32px; font-weight: 600; }
        .cost-label { font-size: 12px; opacity: 0.9; margin-top: 4px; }
        .breakdown-section { margin-top: 12px; }
        .breakdown-title {
          font-size: 14px;
          font-weight: 500;
          margin-bottom: 8px;
          color: var(--secondary-text-color);
        }
        .breakdown-row {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          padding: 6px 0;
          border-bottom: 1px solid var(--divider-color);
          font-size: 13px;
        }
        .breakdown-row:last-child { border-bottom: none; }
        .breakdown-label { color: var(--secondary-text-color); flex: 1; }
        .breakdown-value { color: var(--primary-text-color); font-weight: 500; white-space: nowrap; margin-left: 8px; text-align: right; }
        .amount-secondary { display: block; font-weight: 400; opacity: 0.65; }
        .bill-section-head {
          font-size: 10px; font-weight: 700; letter-spacing: 0.8px;
          text-transform: uppercase; color: var(--disabled-text-color);
          padding: 8px 0 2px;
        }
        .bill-total-row {
          display: flex; justify-content: space-between;
          padding: 8px 0 4px; border-top: 2px solid var(--divider-color);
          font-size: 14px; font-weight: 700; color: var(--primary-text-color);
        }
        .bill-gst-row {
          display: flex; justify-content: space-between;
          padding: 2px 0; font-size: 11px; color: var(--disabled-text-color);
        }
        .bill-subtotal-row {
          border-top: 1px solid var(--divider-color);
          margin-top: 4px; padding-top: 6px;
        }
        .bill-fit { color: var(--success-color, #4CAF50); }
        .bill-note { font-size: 11px; color: var(--warning-color, #FF9800); font-style: italic; padding: 4px 0; }
        /* Quieter than .bill-note — informational, not a caveat: this plan is
           variable-rate, so the lines above are period averages by design. */
        .bill-spot-note { font-size: 11px; color: var(--secondary-text-color); font-style: italic; padding: 4px 0; }
        /* A window spanning a plan-history switch (see _plan_history_segments):
           one full itemised bill per plan actually held, stacked vertically —
           however many that is — instead of one merged table. */
        .bill-multi-segment-note {
          font-size: 11px; color: var(--secondary-text-color); font-style: italic;
          padding: 0 0 6px;
        }
        .bill-segment {
          border: 1px solid var(--divider-color); border-radius: 8px;
          padding: 4px 10px 8px; margin-bottom: 10px;
        }
        .bill-segment-head {
          display: flex; justify-content: space-between; align-items: baseline;
          gap: 8px; padding: 6px 0 4px; border-bottom: 1px solid var(--divider-color);
          flex-wrap: wrap;
        }
        .bill-segment-plan { font-size: 13px; font-weight: 700; color: var(--primary-text-color); }
        .bill-segment-dates { font-size: 11px; color: var(--secondary-text-color); white-space: nowrap; }
        .bill-multi-segment-total {
          border-top: 2px solid var(--divider-color);
        }
        .spike-section { margin-top: 4px; }
        .spike-row { display: flex; align-items: center; gap: 6px; font-size: 11px; padding: 2px 0; }
        .spike-dot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; }
        .spike-label { font-weight: 600; white-space: nowrap; }
        .spike-when { color: var(--secondary-text-color); margin-left: auto; white-space: nowrap; }
        .spike-amt { font-weight: 600; min-width: 46px; text-align: right; white-space: nowrap; }
        .spike-more { font-size: 11px; color: var(--secondary-text-color); font-style: italic; padding: 2px 0; }
        /* Hazard-stripe flag shown under an alternative plan's charts whenever "Exclude
           Greedy Consumption" is active — see _greedyStripeHtml(). Deliberately louder
           than .bill-note's quiet italic caption: it's not a per-plan data quirk, it's a
           statement that every number above was computed on an adjusted assumption. */
        .greedy-stripe {
          margin-top: 10px; padding: 7px 10px; border-radius: 6px;
          font-size: 11px; font-weight: 600; color: var(--primary-text-color);
          display: flex; align-items: center; gap: 6px; cursor: default;
          border: 1px solid color-mix(in srgb, var(--warning-color, #FF9800) 50%, transparent);
          background-image: repeating-linear-gradient(45deg,
            color-mix(in srgb, var(--warning-color, #FF9800) 20%, transparent) 0,
            color-mix(in srgb, var(--warning-color, #FF9800) 20%, transparent) 6px,
            transparent 6px, transparent 12px);
        }
        .greedy-stripe-icon { font-size: 13px; flex: 0 0 auto; }
        .chart-section { margin-top: 14px; }
        .chart-label {
          font-size: 11px;
          color: var(--secondary-text-color);
          margin-bottom: 3px;
        }
        /* Plain HTML, not SVG text — see _hourLabelRow()'s comment for why. */
        .hour-axis {
          display: flex;
          justify-content: space-between;
          font-size: 9px;
          color: var(--secondary-text-color);
          margin: 1px 0 8px;
        }
        .strategy-box {
          background: var(--secondary-background-color);
          padding: 12px;
          border-radius: 6px;
          margin-top: 12px;
        }
        .strategy-title { font-size: 12px; font-weight: 600; margin-bottom: 6px; color: var(--primary-text-color); }
        .retailer-filter { display: inline-flex; align-items: center; gap: 6px; }
        .retailer-filter input {
          font: inherit;
          padding: 4px 8px;
          min-width: 150px;
          border: 1px solid var(--divider-color, #ccc);
          border-radius: 4px;
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color);
        }
        .retailer-filter input:focus {
          outline: none;
          border-color: var(--primary-color, #03a9f4);
        }
        .filter-count { font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
        .greedy-toggle {
          display: inline-flex; align-items: center; gap: 5px;
          font-size: 12px; color: var(--secondary-text-color);
          white-space: nowrap; cursor: pointer;
        }
        .greedy-toggle input[type="checkbox"] { margin: 0; cursor: pointer; }
        .topn-control {
          display: inline-flex; align-items: center; gap: 5px;
          font-size: 12px; color: var(--secondary-text-color); white-space: nowrap;
        }
        .topn-control select {
          padding: 3px 5px; border: 1px solid var(--divider-color); border-radius: 4px;
          background: var(--card-background-color); color: var(--primary-text-color);
          font-size: 12px;
        }
        .plan-card[hidden] { display: none; }
        /* One vertical block per bill boundary (see _renderPeriodSection) — a plan
           switch inside the comparison window splits it into these instead of one
           flat plan-card grid. */
        .period-section {
          padding: 4px 16px 8px;
          border-bottom: 1px solid var(--divider-color);
        }
        .period-section:last-child { border-bottom: none; }
        .period-section-head {
          display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
          padding: 12px 0 4px;
        }
        .period-section-dates { font-size: 15px; font-weight: 600; color: var(--primary-text-color); }
        .period-section-days { font-size: 12px; color: var(--secondary-text-color); }
        .period-alt-count { opacity: 0.75; }
        .strategy-list { font-size: 12px; color: var(--secondary-text-color); line-height: 1.4; margin: 0; padding-left: 18px; }
        .strategy-list li { margin: 2px 0; }
        .strategy-foot { font-size: 10px; color: var(--secondary-text-color); opacity: 0.8; margin-top: 6px; font-style: italic; }
        .error { padding: 16px; color: var(--error-color); text-align: center; }
        .loading { padding: 16px; text-align: center; color: var(--secondary-text-color); }
        .stream-track {
          display: inline-flex; align-items: center;
          width: 80px; height: 6px;
          background: var(--divider-color); border-radius: 3px; overflow: hidden;
        }
        .stream-bar { height: 100%; background: var(--primary-color); border-radius: 3px; transition: width 0.4s ease; }
        .stream-label { font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
        .plan-card.skeleton { opacity: 0.45; pointer-events: none; }
        .sk-line { height: 12px; border-radius: 6px; background: var(--divider-color); margin-bottom: 10px; }
        .sk-line.long { width: 70%; }
        .sk-line.med  { width: 50%; }
        .sk-line.short{ width: 35%; }
        .sk-box { height: 80px; border-radius: 6px; background: var(--divider-color); margin: 12px 0; }
        .date-controls {
          display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
          padding: 10px 16px;
          background: var(--secondary-background-color);
          border-bottom: 1px solid var(--divider-color);
        }
        .date-controls label { font-size: 12px; color: var(--secondary-text-color); }
        .date-controls input[type="date"] {
          padding: 4px 6px;
          border: 1px solid var(--divider-color);
          border-radius: 4px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
          font-size: 13px;
        }
        .date-controls button {
          padding: 5px 14px;
          background: var(--primary-color);
          color: var(--text-primary-color);
          border: none; border-radius: 4px; cursor: pointer; font-size: 13px;
        }
        .date-controls button.nav-btn {
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
          border: 1px solid var(--divider-color);
          font-size: 12px; padding: 5px 10px;
        }
        .date-controls button:disabled { opacity: 0.6; cursor: default; }
        .zoom-controls { display: flex; align-items: center; gap: 4px; margin-left: 8px; }
        .zoom-label { font-size: 11px; color: var(--secondary-text-color); }
        .date-meta { font-size: 11px; color: var(--secondary-text-color); margin-left: auto; }
        .nav-btn.active {
          background: var(--primary-color);
          color: var(--text-primary-color);
          border-color: var(--primary-color);
        }
        .hist-section { padding: 16px; }
        .hist-note { font-size: 12px; color: var(--secondary-text-color); margin: 0 0 12px; font-style: italic; }
        .hist-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .hist-table th {
          text-align: left; padding: 6px 8px;
          font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
          color: var(--secondary-text-color);
          border-bottom: 2px solid var(--divider-color);
        }
        .hist-table td { padding: 7px 8px; border-bottom: 1px solid var(--divider-color); vertical-align: middle; }
        .hist-table tbody tr:last-child td { border-bottom: none; }
        .hist-input {
          padding: 3px 6px;
          border: 1px solid var(--divider-color); border-radius: 4px;
          background: var(--card-background-color);
          color: var(--primary-text-color); font-size: 13px;
        }
        .hist-actions { white-space: nowrap; text-align: right; }
        .hist-btn {
          padding: 3px 10px;
          background: var(--primary-color); color: var(--text-primary-color);
          border: none; border-radius: 4px; cursor: pointer; font-size: 12px; margin-right: 4px;
        }
        .hist-btn-sec {
          background: var(--secondary-background-color); color: var(--primary-text-color);
          border: 1px solid var(--divider-color);
        }
        .hist-btn-del { background: var(--error-color, #F44336); }
        .whatif-panel {
          display: flex; flex-wrap: wrap; align-items: flex-end; gap: 16px;
          padding: 12px 16px; margin: 0 16px 12px;
          background: var(--secondary-background-color);
          border: 1px solid var(--divider-color); border-radius: 8px;
        }
        .whatif-field { display: flex; flex-direction: column; gap: 3px; }
        .whatif-field label { font-size: 11px; color: var(--secondary-text-color); }
        .whatif-field input {
          width: 110px; padding: 5px 7px;
          border: 1px solid var(--divider-color); border-radius: 4px;
          background: var(--card-background-color); color: var(--primary-text-color);
          font-size: 13px;
        }
        .whatif-presets { display: flex; gap: 6px; flex-wrap: wrap; }
        .whatif-note { flex-basis: 100%; font-size: 11px; color: var(--secondary-text-color); }
        .whatif-banner {
          margin: 0 16px 12px; padding: 8px 14px; border-radius: 6px;
          background: repeating-linear-gradient(
            45deg, var(--warning-color, #ff9800) 0, var(--warning-color, #ff9800) 10px,
            rgba(255,152,0,0.15) 10px, rgba(255,152,0,0.15) 20px);
          color: var(--primary-text-color); font-size: 13px; font-weight: 500;
          display: flex; align-items: center; justify-content: space-between; gap: 12px;
        }
        .whatif-banner button {
          background: var(--card-background-color); color: var(--primary-text-color);
          border: 1px solid var(--divider-color); border-radius: 4px;
          padding: 3px 10px; cursor: pointer; font-size: 12px; white-space: nowrap;
        }
        /* Bill-sourced comparison: the confidence banner and the bills panel.
           Deliberately quieter than .whatif-banner's hazard stripes — a bill
           comparison is a legitimate mode, not a temporary hypothetical — but
           it still has to be impossible to read a figure below without seeing
           what it rests on. */
        .bill-banner {
          margin: 0 16px 12px; padding: 10px 14px; border-radius: 6px;
          border: 1px solid var(--divider-color);
          border-left: 4px solid var(--info-color, #2196f3);
          background: var(--secondary-background-color);
          font-size: 13px; color: var(--primary-text-color);
        }
        .bill-banner-head {
          display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
          margin-bottom: 8px;
        }
        .bill-banner-head .nav-btn { margin-left: auto; }
        .bill-banner-grid {
          display: grid; gap: 10px 20px;
          grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          padding: 8px 0; border-top: 1px solid var(--divider-color);
        }
        .bill-caveat, .bill-warnings {
          margin: 8px 0 0; font-size: 12px; color: var(--secondary-text-color);
        }
        .bill-warnings { padding-left: 18px; }
        .bill-warnings li { margin: 4px 0; }
        .bill-assumed, .bill-excluded {
          margin-top: 8px; padding: 7px 10px; border-radius: 4px; font-size: 12px;
          background: var(--card-background-color);
          border: 1px solid var(--divider-color);
        }
        .bill-assumed {
          border-color: var(--warning-color, #ff9800);
        }
        .bill-assumed .nav-btn, .bill-excluded .nav-btn { margin-left: 8px; }
        /* Louder than .bill-assumed on purpose: this one says a figure the
           user can check against the paper in their hand is wrong. */
        .bill-plandata {
          margin-top: 10px;
          padding: 10px 12px;
          border-radius: 6px;
          border-left: 4px solid var(--error-color, #db4437);
          background: rgba(219, 68, 55, 0.08);
          font-size: 0.9em;
          line-height: 1.45;
        }
        .bill-pd-table {
          border-collapse: collapse;
          margin: 8px 0;
          font-variant-numeric: tabular-nums;
        }
        .bill-pd-table th, .bill-pd-table td {
          padding: 2px 14px 2px 0;
          text-align: right;
        }
        .bill-pd-table th { font-weight: 500; color: var(--secondary-text-color); }
        .bill-pd-table th:first-child, .bill-pd-table td:first-child { text-align: left; }
        .bill-pd-pct { color: var(--error-color, #db4437); }
        .bill-scenario, .source-control {
          display: inline-flex; align-items: center; gap: 6px;
          font-size: 12px; color: var(--secondary-text-color);
        }
        .bill-scenario select, .source-control select {
          font: inherit; padding: 2px 4px;
          background: var(--card-background-color); color: var(--primary-text-color);
          border: 1px solid var(--divider-color); border-radius: 4px;
        }
        .bill-chip {
          display: inline-block; padding: 1px 7px; border-radius: 10px;
          font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
        }
        .bill-chip-modelled { background: var(--info-color, #2196f3); color: #fff; }
        .bill-chip-ok { background: var(--success-color, #43a047); color: #fff; }
        .bill-chip-warn { background: var(--warning-color, #ff9800); color: #fff; }
        .bill-chip-bad { background: var(--error-color, #e53935); color: #fff; }
        .bill-sub { color: var(--secondary-text-color); font-size: 12px; }
        .bill-active { background: var(--secondary-background-color); }
        .bill-add { margin: 16px 0 0; }
        .bill-add h4 { margin: 0 0 6px; }
        .bill-add textarea {
          width: 100%; box-sizing: border-box; font-family: monospace;
          font-size: 12px; padding: 8px; border-radius: 4px;
          background: var(--card-background-color); color: var(--primary-text-color);
          border: 1px solid var(--divider-color);
        }
        .bill-fields {
          display: flex; gap: 14px; flex-wrap: wrap; margin: 8px 0;
        }
        .bill-fields label {
          display: flex; flex-direction: column; gap: 3px; font-size: 12px;
          color: var(--secondary-text-color);
        }
        .bill-fields input, .bill-fields select {
          font: inherit; padding: 4px 6px; border-radius: 4px;
          background: var(--card-background-color); color: var(--primary-text-color);
          border: 1px solid var(--divider-color);
        }
        .bill-err {
          margin-top: 8px; padding: 8px 10px; border-radius: 4px; font-size: 12px;
          background: var(--error-color, #e53935); color: #fff; white-space: pre-wrap;
        }
        /* Full-screen plan breakout — click a plan's cost banner (below) to expand
           just that one plan-card over the whole viewport, charts and all. */
        .cost-display {
          cursor: pointer;
          position: relative;
          transition: filter 0.15s ease, transform 0.1s ease;
        }
        .cost-display:hover { filter: brightness(1.08); }
        .cost-display:active { transform: scale(0.99); }
        .cost-display::after {
          content: "⛶";
          position: absolute; top: 8px; right: 10px;
          font-size: 13px; opacity: 0.7;
        }
        .plan-card.fs-hidden { display: none; }
        /* Full-screen plan breakout renders as a native <dialog>, shown via
           .showModal() (see render()'s post-innerHTML wiring). Top-layer rendering
           is the only thing that reliably escapes EVERY ancestor's containing-block/
           overflow/stacking-context quirks (our own :host's contain:content, and
           ha-card's own internal styling both defeated a plain position:fixed div
           here) — a <dialog> promoted via showModal() ignores all of that by
           construction, same mechanism browsers use for <video> fullscreen. Reset
           the UA's default dialog box model (border/centering/max-size) rather than
           fight it, and use its own ::backdrop instead of a hand-rolled overlay div.
        */
        dialog.fullscreen-plan {
          position: fixed;
          inset: 3vh 3vw;
          width: auto; height: auto;
          max-width: none; max-height: none;
          margin: 0;
          border: none;
          overflow-y: auto;
          box-shadow: 0 12px 48px rgba(0,0,0,0.55);
          border-radius: 14px;
          padding: 28px 32px 32px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
        }
        dialog.fullscreen-plan::backdrop {
          background: rgba(0,0,0,0.62);
          backdrop-filter: blur(2px);
        }
        .fs-close-btn {
          position: sticky;
          display: block;
          top: 0; margin: 0 0 12px auto;
          width: max-content;
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          padding: 7px 16px;
          font-size: 14px; font-weight: 500;
          cursor: pointer;
          box-shadow: 0 2px 10px rgba(0,0,0,0.3);
          z-index: 1;
        }
        .fs-close-btn:hover { filter: brightness(1.1); }
        /* Everything inside an expanded plan reads bigger, not just its charts —
           the point is a whole-card "presentation mode", not just zoomed graphs. */
        .fullscreen-plan .plan-title { margin-bottom: 16px; }
        .fullscreen-plan .plan-title-retailer { font-size: 18px; }
        .fullscreen-plan .plan-title-name { font-size: 26px; }
        .fullscreen-plan .cost-display { padding: 28px; margin: 18px 0; }
        .fullscreen-plan .cost-display::after { font-size: 20px; top: 14px; right: 16px; }
        .fullscreen-plan .cost-amount { font-size: 56px; }
        .fullscreen-plan .cost-label { font-size: 16px; }
        .fullscreen-plan .chart-section { margin-top: 24px; }
        .fullscreen-plan .chart-label { font-size: 15px; margin-bottom: 6px; }
        .fullscreen-plan .hour-axis { font-size: 12px; }
        /* Left un-constrained, .breakdown-row's existing flex + space-between (label
           flex:1, value margin-left) stretches across the dialog's full ~90vw width,
           spreading each line's description and amount to opposite edges of the
           screen instead of reading as one bill line. Cap the column at a sane
           reading width so they stay right next to each other, same as the compact
           card already does just by virtue of being narrow. */
        .fullscreen-plan .breakdown-section { max-width: 640px; }
        .fullscreen-plan .breakdown-title { font-size: 18px; }
        .fullscreen-plan .breakdown-row { font-size: 16px; padding: 9px 0; }
        .fullscreen-plan .bill-total-row { font-size: 19px; }
        .fullscreen-plan .bill-gst-row { font-size: 14px; }
        .fullscreen-plan .bill-section-head { font-size: 12px; }
        .fullscreen-plan .strategy-box { padding: 18px; }
        .fullscreen-plan .strategy-title { font-size: 17px; }
        .fullscreen-plan .strategy-list { font-size: 15px; }
        .fullscreen-plan .strategy-foot { font-size: 13px; }
      </style>
    `;

    if (!this._data) {
      this.shadowRoot.innerHTML = `${styles}<ha-card>
        <div class="loading">Loading plan data...</div>
      </ha-card>`;
      return;
    }

    const _esc = (t) => String(t).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // Plan keys are "Retailer - Plan Name"; the retailer is everything before the
    // FIRST " - ", because plan names contain the separator too ("Standing Offer -
    // Time of Use", "Origin Go Variable Ongoing - New & Move Customers only").
    const _retailerOf = (key) => String(key).split(' - ')[0].trim();
    const _planNameOf = (key) => {
      const idx = String(key).indexOf(' - ');
      return idx === -1 ? '' : String(key).slice(idx + 3).trim();
    };

    const planDetails = this._data.plan_details || {};
    const currentPlanTotalFallback = this._data.current_plan_total || 0;
    const currentPlanName = this._data.current_plan_name || null;
    const usageDays = this._data.usage_days || 30;
    const calcDate = (this._data.calculation_date || '').substring(0, 10);

    let plansToShow = Object.entries(planDetails);
    if (planFilter) {
      plansToShow = plansToShow.filter(([name]) => name.includes(planFilter));
    }

    // Cheapest to most expensive
    plansToShow.sort(([, detA], [, detB]) =>
      (detA.breakdown?.total || 0) - (detB.breakdown?.total || 0)
    );

    // "Show best N" declutter filter — the current plan is always shown regardless
    // of its rank (there must always be a baseline to compare against), then the
    // cheapest N alternatives on top of it. Only applies to the flat single-period
    // view below; a multi-period view applies its own per-period version of this
    // inside _renderPeriodSection, since "cheapest" means something different once
    // there's more than one period to rank against.
    const topN = this._topN;
    if (Number.isFinite(topN)) {
      const curEntry = plansToShow.find(([n]) => n === currentPlanName);
      const rest = plansToShow.filter(([n]) => n !== currentPlanName).slice(0, topN);
      plansToShow = curEntry ? [curEntry, ...rest] : rest;
      plansToShow.sort(([, detA], [, detB]) =>
        (detA.breakdown?.total || 0) - (detB.breakdown?.total || 0)
      );
    }

    // Totals needed for colour-coding (avoid ?? for broad browser compat)
    const _cpEntry = currentPlanName ? planDetails[currentPlanName] : null;
    const _cpRaw = _cpEntry && _cpEntry.breakdown ? _cpEntry.breakdown.total : null;
    const currentPlanTotal = (_cpRaw != null) ? _cpRaw : currentPlanTotalFallback;
    const minTotal = plansToShow.reduce((m, entry) => {
      const t = entry[1].breakdown ? entry[1].breakdown.total : null;
      return (t != null && t < m) ? t : m;
    }, Infinity);

    // Global scale: compute maxima across all plans so bars represent the same value in every card.
    const _deferrable_devices = (this._data && this._data.deferrable_devices) || [];
    const _allProfiles = plansToShow
      .map(([, d]) => d.hourly_profile)
      .filter(p => p && p.length === 24);
    const globalMaxKwh = Math.max(
      ..._allProfiles.flatMap(p => p.map(s => Math.max(s.import_kwh || 0, s.export_kwh || 0))),
      0.001
    );
    const globalMaxCost = Math.max(
      ..._allProfiles.flatMap(p => p.map(s => Math.max(s.import_cost || 0, s.export_income || 0))),
      0.0001
    );
    const _gHasLoad = _allProfiles.some(p => p.some(s => (s.home_load_kwh || 0) > 0.01));
    const _gHasDef  = _allProfiles.some(p => p.some(s =>
      (s.deferrable_kwh || 0) > 0.01 || (s.deferrable_per_device || []).some(v => v > 0.01)
    ));
    const _gHasSolar = _allProfiles.some(p => p.some(s => (s.solar_kwh || 0) > 0.01));
    const globalMaxLoad = (_gHasLoad || _gHasDef) ? Math.max(
      ..._allProfiles.flatMap(p => p.map(s => {
        const perDev = s.deferrable_per_device || [];
        const def = perDev.length > 0 ? perDev.reduce((a, v) => a + v, 0) : (s.deferrable_kwh || 0);
        return (s.home_load_kwh || 0) + def;
      })),
      0.001
    ) : 0;
    const globalMaxSolar = _gHasSolar
      ? Math.max(..._allProfiles.flatMap(p => p.map(s => s.solar_kwh || 0)), 0.001)
      : 0;
    const globalLoadSolarMax = Math.max(globalMaxLoad, globalMaxSolar, 0.001);

    const planNames = plansToShow.map(([n]) => n);

    const plansHtml = plansToShow.map(([planName, details]) => {
      const breakdown = details.breakdown || {};
      const profile = details.hourly_profile || null;
      const isCurrentPlan = planName === currentPlanName;
      const total = breakdown.total || 0;
      const savings = total - currentPlanTotal;
      const isCheaper = savings < -0.05;
      // Full-screen breakout, triggered by clicking this plan's cost banner below.
      // Only affects this one card's own chart scale/markup — untouched plans in
      // the grid keep rendering normally, just hidden behind the fs-hidden class
      // while a plan is expanded.
      const isFullscreen = planName === this._fullscreenPlan;
      const scale = isFullscreen ? chartScale * 2.1 : chartScale;

      // Banner colour: amber=current, deep-cyan=cheapest, green=cheaper, red=more expensive
      // — same hexes as the household/SOC/selling/spend chart roles below, kept in sync
      // via the GridLensCard.*_COLOR statics (see their definition for why these values).
      let bannerColor;
      if (isCurrentPlan) {
        bannerColor = GridLensCard.HOUSEHOLD_COLOR;
      } else if (Math.abs(total - minTotal) < 0.01) {
        bannerColor = GridLensCard.SOC_COLOR;
      } else if (isCheaper) {
        bannerColor = GridLensCard.SELLING_COLOR;
      } else {
        bannerColor = GridLensCard.SPEND_COLOR;
      }

      let savingsLabel = '';
      if (isCurrentPlan) {
        savingsLabel = 'YOUR CURRENT PLAN';
      } else if (isCheaper) {
        savingsLabel = `SAVE $${Math.abs(savings).toFixed(2)} over ${usageDays} days`;
      } else {
        savingsLabel = `COSTS $${savings.toFixed(2)} more over ${usageDays} days`;
      }

      // ── Cost breakdown ──────────────────────────────────────────────────
      let breakdownHtml = '';
      if (showBreakdown) {
        const bi = breakdown.bill_items;
        if (bi && bi.is_multi_segment && bi.segments && bi.segments.length) {
          // The window covers more than one plan (a plan-history switch fell
          // inside it — see _plan_history_segments/_compute_multi_segment_bill_items
          // in plan_calculator.py): one real, fully itemised bill per plan actually
          // held, dynamically however many that is — not a blended/merged table,
          // since a "Peak" line from two different retailers matches neither plan's
          // real bill. Each segment reuses the exact same row renderer as a normal
          // single-plan bill, just scoped to that plan's own date range.
          const segHtml = bi.segments.map(seg => `
            <div class="bill-segment">
              <div class="bill-segment-head">
                <span class="bill-segment-plan">${this._esc(seg.plan_name)}</span>
                <span class="bill-segment-dates">${this._fmtBillDateRange(seg.start_date, seg.end_date)} &middot; ${seg.days}&thinsp;day${seg.days === 1 ? '' : 's'}</span>
              </div>
              ${this._renderBillRows(seg.bill_items)}
            </div>`).join('');
          breakdownHtml = `
            <div class="breakdown-section bill-multi-segment">
              <div class="bill-multi-segment-note">Plan changed during this period — showing each plan's own bill below.</div>
              ${segHtml}
              <div class="bill-total-row bill-multi-segment-total">
                <span>Combined total across ${bi.segments.length} plans (inc. GST)</span><span>$${bi.total.toFixed(2)}</span>
              </div>
            </div>`;

        } else if (bi) {
          breakdownHtml = `<div class="breakdown-section">${this._renderBillRows(bi)}</div>`;

        } else if (breakdown.energy_cost !== undefined || breakdown.note) {
          // Fallback: market-linked without bill_items
          breakdownHtml = `
            <div class="breakdown-section">
              <div class="breakdown-title">Cost Breakdown</div>
              <div class="breakdown-row">
                <div class="breakdown-label">Energy cost (actual prices)</div>
                <div class="breakdown-value">$${(breakdown.energy_cost || 0).toFixed(2)}</div>
              </div>
              <div class="breakdown-row">
                <div class="breakdown-label">Supply charge</div>
                <div class="breakdown-value">$${(breakdown.supply_charge || 0).toFixed(2)}</div>
              </div>
              <div class="bill-total-row">
                <span>Total (${usageDays} days)</span><span>$${(breakdown.total || 0).toFixed(2)}</span>
              </div>
            </div>`;
        }
      }

      // ── Hourly charts ───────────────────────────────────────────────────
      let chartHtml = '';
      if (showCharts && profile && profile.length === 24) {
        const DEVICE_COLORS = GridLensCard.DEVICE_COLORS;
        const deferrable_devices = _deferrable_devices;
        const maxKwh  = globalMaxKwh;
        const maxCost = globalMaxCost;
        const hasHomeLoad = profile.some(s => (s.home_load_kwh || 0) > 0.01);
        const hasEv       = profile.some(s => (s.deferrable_kwh || 0) > 0.01 || (s.deferrable_per_device || []).some(v => v > 0.01));
        const hasSoc      = profile.some(s => (s.soc_percent   || 0) > 0);
        const hasSolar    = profile.some(s => (s.solar_kwh     || 0) > 0.01);
        const loadSolarMax = globalLoadSolarMax;

        // Build per-device legend entries
        const devLegend = deferrable_devices.length > 0
          ? deferrable_devices.map((d, ii) =>
              `&nbsp;<span style="color:${DEVICE_COLORS[ii % DEVICE_COLORS.length]};font-weight:600">■ ${d.name}</span>`
            ).join('')
          : (hasEv ? `&nbsp;<span style="color:${DEVICE_COLORS[0]};font-weight:600">■ deferrable</span>` : '');

        const loadChartHtml = (hasHomeLoad || hasEv) ? `
            <div class="chart-label" style="margin-top:10px">
              Avg hourly load &nbsp;
              ${hasHomeLoad ? `<span style="color:${GridLensCard.HOUSEHOLD_COLOR};font-weight:600">■ household</span>` : ''}
              ${devLegend}
              &nbsp;(kWh)
            </div>
            ${this.renderStackedBarChart(profile, loadSolarMax, scale, deferrable_devices, isFullscreen)}` : '';

        const solarChartHtml = hasSolar ? `
            <div class="chart-label" style="margin-top:10px">
              Avg hourly solar generation &nbsp;
              <span style="color:${GridLensCard.SOLAR_COLOR};font-weight:600;text-shadow:0 0 2px #999">■</span>
              <span style="font-weight:600"> solar</span>
              &nbsp;(kWh)
            </div>
            ${this.renderSolarChart(profile, loadSolarMax, scale, isFullscreen)}` : '';

        const socChartHtml = hasSoc ? `
            <div class="chart-label" style="margin-top:10px">
              Avg battery SOC &nbsp;(%)
            </div>
            ${this.renderSocChart(profile, scale, isFullscreen)}` : '';

        // Price chart: only worth showing when the rate actually moves across
        // the day (spot / TOU plans). A flat single-rate plan would just draw a
        // straight line, so skip it there.
        const _impRates = profile.map(s => s.import_rate || 0).filter(v => v > 0);
        const _rateSpread = _impRates.length
          ? Math.max(..._impRates) - Math.min(..._impRates) : 0;
        const rateChartHtml = _rateSpread > 0.02 ? `
            <div class="chart-label" style="margin-top:10px">
              Average hourly price &nbsp;
              <span style="color:${GridLensCard.SPEND_COLOR};font-weight:600">■ buy</span> &nbsp;
              <span style="color:${GridLensCard.INCOME_COLOR};font-weight:600">■ sell</span> &nbsp; (c/kWh)
            </div>
            ${this.renderRateChart(profile, scale, isFullscreen)}` : '';

        const spikesHtml = this.renderSpikes(details.spikes);

        chartHtml = `
          <div class="chart-section">
            ${loadChartHtml}
            ${solarChartHtml}
            <div class="chart-label"${(hasHomeLoad || hasEv || hasSolar) ? ' style="margin-top:10px"' : ''}>
              Average hourly energy &nbsp;
              <span style="color:${GridLensCard.BUYING_COLOR};font-weight:600">■ buying</span> ↑ &nbsp;
              <span style="color:${GridLensCard.SELLING_COLOR};font-weight:600">■ selling</span> ↓ &nbsp; (kWh)
            </div>
            ${this.renderDivergingChart(profile, 'import_kwh', 'export_kwh', maxKwh, GridLensCard.BUYING_COLOR, GridLensCard.SELLING_COLOR, scale, isFullscreen)}
            <div class="chart-label" style="margin-top:10px">
              Average hourly cost &nbsp;
              <span style="color:${GridLensCard.SPEND_COLOR};font-weight:600">■ spend</span> ↑ &nbsp;
              <span style="color:${GridLensCard.INCOME_COLOR};font-weight:600">■ income</span> ↓ &nbsp; ($)
            </div>
            ${this.renderDivergingChart(profile, 'import_cost', 'export_income', maxCost, GridLensCard.SPEND_COLOR, GridLensCard.INCOME_COLOR, scale, isFullscreen)}
            ${rateChartHtml}
            ${spikesHtml}
            ${socChartHtml}
            ${this._greedyStripeHtml(isCurrentPlan)}
          </div>`;
      }


      // Only an LP-dispatched profile describes a strategy. The current plan's
      // profile (outside a what-if) and the no-battery SIMPLE path come from
      // actual usage — there `optimization` is null/absent.
      const strategyHtml = details.optimization
        ? this._renderDispatchSummary(profile) : '';

      // The fullscreen instance renders as a <dialog> (see render()'s post-innerHTML
      // wiring, which calls .showModal() on it) — top-layer rendering is what actually
      // escapes ancestor containment/overflow, not the class name. Every other plan
      // stays a plain <div>, just hidden behind fs-hidden while one is expanded.
      const tag = isFullscreen ? 'dialog' : 'div';
      const fsClass = isFullscreen ? ' fullscreen-plan' : (this._fullscreenPlan ? ' fs-hidden' : '');
      const fsId = isFullscreen ? ' id="epc-fs-dialog"' : '';
      return `
        <${tag} class="plan-card${isCurrentPlan ? ' current-plan' : ''}${fsClass}"${fsId} data-retailer="${_esc(_retailerOf(planName))}" data-plan="${_esc(planName)}">
          ${isFullscreen ? `<button class="fs-close-btn" id="epc-fs-close" title="Close full screen (Esc)">✕ Close</button>` : ''}
          <div class="plan-title">
            <div class="plan-title-retailer">${_esc(_retailerOf(planName))}</div>
            <div class="plan-title-name">${_esc(_planNameOf(planName))}</div>
          </div>
          <div class="cost-display" style="background:${bannerColor}" title="Click to view this plan full-screen">
            <div class="cost-amount">$${total.toFixed(2)}</div>
            <div class="cost-label">${savingsLabel}</div>
          </div>
          ${chartHtml}
          ${breakdownHtml}
          ${strategyHtml}
        </${tag}>`;
    }).join('');

    // Autocomplete over the retailers actually present, so the box suggests
    // "EnergyAustralia" rather than making the user guess the exact spelling.
    const _retailerOptions = [...new Set(planNames.map(_retailerOf))].sort(
      (a, b) => a.localeCompare(b));

    const _busy = this._streamPhase !== null;

    // A bill's model only covers the days the bill covers, so the date range is
    // the bill's and the pickers are disabled rather than silently ignored.
    const _onBill = this._source !== 'live';
    const _dateDis = (_busy || _onBill) ? 'disabled' : '';
    // In bill mode the pickers show the bill's own period, but from a separate
    // slot — the household's saved range must survive a trip through a bill and
    // still be there when they switch back (see _updateDatesFromData).
    const _startVal = _onBill ? this._billStart : this._startDate;
    const _endVal   = _onBill ? this._billEnd   : this._endDate;
    const _sourceHtml = (this._bills && this._bills.length) ? `
      <label class="source-control" title="Compare this household's own metered data, or a household you only have a paper bill for.">
        Source
        <select id="epc-source" ${_busy ? 'disabled' : ''}>
          <option value="live"${this._source === 'live' ? ' selected' : ''}>My data</option>
          ${this._bills.map(b => `<option value="bill:${this._esc(b.id)}"${
            this._source === `bill:${b.id}` ? ' selected' : ''}>${this._esc(b.label)}</option>`).join('')}
        </select>
      </label>` : '';

    const dateControlsHtml = `
      <div class="date-controls">
        ${_sourceHtml}
        <button id="epc-prev" class="nav-btn" title="Back 1 month" ${_dateDis}>&#8592; 1 mo</button>
        <label>From</label>
        <input type="date" id="epc-start" value="${_startVal}" ${_dateDis}>
        <label>to</label>
        <input type="date" id="epc-end" value="${_endVal}" ${_dateDis}>
        <button id="epc-next" class="nav-btn" title="Forward 1 month" ${_dateDis}>1 mo &#8594;</button>
        <button id="epc-calc" ${_busy ? 'disabled' : ''}>${_busy ? 'Calculating…' : 'Calculate'}</button>
        <span class="zoom-controls">
          <button id="epc-zoom-out" class="nav-btn" title="Shrink charts">&#8722;</button>
          <span class="zoom-label">zoom</span>
          <button id="epc-zoom-in" class="nav-btn" title="Grow charts">&#43;</button>
        </span>
        ${calcDate ? `<span class="date-meta">${usageDays} days &nbsp;·&nbsp; updated ${calcDate}</span>` : ''}
        ${this._periodMessage ? `
          <span class="stream-label">${_esc(this._periodMessage)}</span>
          <span class="stream-track"><span class="stream-bar" style="width:${
            this._fetchTotal > 0 ? Math.round(this._fetchStep/this._fetchTotal*100) : 0}%"></span></span>`
        : (this._plansDone > 0 && this._plansTotal > 0) ? `
          <span class="stream-label">${this._plansDone}/${this._plansTotal} plans</span>
          <span class="stream-track"><span class="stream-bar" style="width:${Math.round(this._plansDone/this._plansTotal*100)}%"></span></span>` : ''}
        <span class="retailer-filter">
          <input type="search" id="epc-retailer" list="epc-retailer-list"
                 placeholder="Filter retailer…" autocomplete="off"
                 value="${_esc(this._retailerFilter)}">
          <datalist id="epc-retailer-list">
            ${_retailerOptions.map(r => `<option value="${_esc(r)}"></option>`).join('')}
          </datalist>
          <span class="filter-count" id="epc-filter-count"></span>
        </span>
        <label class="greedy-toggle"
               title="Only affects alternative plans below — your current plan's cost is always your real bill. Only counts Greedy Consumption energy tracked since this feature shipped; older periods have nothing to exclude yet.">
          <input type="checkbox" id="epc-exclude-greedy" ${this._excludeGreedy ? 'checked' : ''} ${_busy ? 'disabled' : ''}>
          Exclude Greedy Consumption
        </label>
        <label class="topn-control" title="How many alternative plans to show — the plan you're actually on is always shown too, regardless of rank.">
          Show best
          <select id="epc-topn" ${_busy ? 'disabled' : ''}>
            ${[3, 5, 10, 20].map(n => `<option value="${n}"${this._topN === n ? ' selected' : ''}>${n}</option>`).join('')}
            <option value="all"${!Number.isFinite(this._topN) ? ' selected' : ''}>All</option>
          </select>
        </label>
        <button id="epc-history-btn" class="nav-btn${this._showHistory ? ' active' : ''}">History</button>
        <button id="epc-bills-btn" class="nav-btn${this._showBills ? ' active' : ''}"
                title="Compare a household you only have a paper bill for">Bills</button>
        <button id="epc-whatif-btn" class="nav-btn${(this._showWhatif || this._whatifApplied) ? ' active' : ''}"
                title="Simulate a different battery/solar system size, including none at all">What if?</button>
        <a href="/api/grid_lens/diagnostic_export" download class="nav-btn" title="Download diagnostic zip for bug reporting" style="text-decoration:none;">&#8659; Diagnostic</a>
      </div>`;

    // Prefills default to the household's REAL configured setup (household_battery_kwh,
    // 100% solar) so opening the panel shows "your current system" — the user then
    // drags one or both down (0 = none) or up (a bigger system) to see the effect.
    const _realBatteryKwh = this._data.household_battery_kwh ?? 0;
    const _batteryInputVal = this._whatifBatteryKwh ?? String(_realBatteryKwh);
    const _solarInputVal = this._whatifSolarPct ?? '100';
    const whatifPanelHtml = this._showWhatif ? `
      <div class="whatif-panel">
        <div class="whatif-field">
          <label for="epc-whatif-battery">Battery size (kWh)</label>
          <input type="number" id="epc-whatif-battery" min="0" step="0.5" value="${_esc(_batteryInputVal)}" ${_busy ? 'disabled' : ''}>
        </div>
        <div class="whatif-field">
          <label for="epc-whatif-solar">Solar production (% of your real system)</label>
          <input type="number" id="epc-whatif-solar" min="0" step="10" value="${_esc(_solarInputVal)}" ${_busy ? 'disabled' : ''}>
        </div>
        <div class="whatif-presets">
          <button id="epc-whatif-none" class="nav-btn" ${_busy ? 'disabled' : ''}>No solar or battery</button>
          <button id="epc-whatif-current" class="nav-btn" ${_busy ? 'disabled' : ''}>My current setup</button>
          <button id="epc-whatif-apply" ${_busy ? 'disabled' : ''}>${_busy ? 'Calculating…' : 'Apply'}</button>
          ${this._whatifApplied ? `<button id="epc-whatif-clear" class="nav-btn" ${_busy ? 'disabled' : ''}>Exit what-if</button>` : ''}
        </div>
        <div class="whatif-note">
          Estimates every plan — including the one you're on — as if it were run through
          this hypothetical system, so it's the LP's optimal-dispatch result rather than
          your actual bill. Solar is your real measured production scaled by this
          percentage (0% = no solar), not a true panel-physics model.
        </div>
      </div>` : '';

    const whatifBannerHtml = this._data.whatif ? `
      <div class="whatif-banner">
        <span>&#9888; What-if scenario — ${
          this._data.whatif.battery_kwh != null ? `${this._data.whatif.battery_kwh} kWh battery` : 'your real battery'
        }, ${
          this._data.whatif.solar_pct != null ? `${this._data.whatif.solar_pct}% solar` : 'your real solar'
        }. Every total below is hypothetical, not your actual bill.</span>
        <button id="epc-whatif-banner-clear" ${_busy ? 'disabled' : ''}>Exit what-if</button>
      </div>` : '';

    const skeletonCount = (this._streamPhase === 'optimising' && this._plansTotal > 0)
      ? Math.max(0, this._plansTotal - this._plansDone) : 0;
    const skeletonsHtml = Array.from({length: skeletonCount}, () =>
      `<div class="plan-card skeleton">
        <div class="sk-line long"></div>
        <div class="sk-box"></div>
        <div class="sk-line med"></div>
        <div class="sk-line short"></div>
      </div>`
    ).join('');

    // A window with a plan-history switch in it comes back as more than one
    // `period` (see plan_calculator.py's `periods` construction) — one vertical
    // section per bill boundary, each with its own actual bill + top-N
    // contenders for that specific period. No switch (the common case, or a
    // stale cached response from before this field existed) falls back to the
    // flat single-grid view above, just with the same "Show best N" filter
    // already applied to `plansToShow`.
    const periodsArr = this._data.periods || [];
    const periodsHtml = (periodsArr.length > 1)
      ? periodsArr.map(p => this._renderPeriodSection(p, topN, showBreakdown)).join('')
      : `<div class="plan-grid">${plansHtml}${skeletonsHtml}</div>`;

    const bodyHtml = this._showBills
      ? this.renderBillsPanel()
      : (this._showHistory ? this.renderHistoryPanel(planNames) : periodsHtml);

    this.shadowRoot.innerHTML = `
      ${styles}
      <ha-card>
        ${dateControlsHtml}
        ${whatifPanelHtml}
        ${whatifBannerHtml}
        ${this.renderBillBanner()}
        ${bodyHtml}
      </ha-card>`;

    this._applyRetailerFilter();

    const _filterBox = this.shadowRoot.getElementById('epc-retailer');
    if (_filterBox) {
      // Filtering hides DOM nodes instead of re-rendering. A re-render would
      // replace the <input> mid-keystroke, losing focus and caret position on
      // every character typed — and the streaming 'plan' events re-render this
      // card dozens of times during a calculation, so that is not hypothetical.
      _filterBox.addEventListener('input', (ev) => {
        this._retailerFilter = ev.target.value;
        this._applyRetailerFilter();
      });
      // type="search" clears via the native ✕ and via Escape; both fire 'input'
      // in Chromium but Escape does not in Firefox, so handle it explicitly.
      _filterBox.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape' && this._retailerFilter) {
          ev.preventDefault();
          ev.target.value = '';
          this._retailerFilter = '';
          this._applyRetailerFilter();
        }
      });
      if (this._filterHadFocus) {
        _filterBox.focus();
        const n = _filterBox.value.length;
        try { _filterBox.setSelectionRange(n, n); } catch (_) {}
      }
    }

    const shiftMonth = (dateStr, delta) => {
      if (!dateStr) return dateStr;
      const [y, m, d] = dateStr.split('-').map(Number);
      const date = new Date(y, m - 1 + delta, d);
      return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
    };

    const triggerFetch = (s, e, btnId) => {
      if (!s || !e) return;
      this._startDate = s;
      this._endDate   = e;
      try { localStorage.setItem('epc-date-start', s); } catch (_) {}
      try { localStorage.setItem('epc-date-end',   e); } catch (_) {}
      const btn = this.shadowRoot.getElementById(btnId);
      if (btn) { btn.disabled = true; btn.textContent = '…'; }
      this.fetchData(s, e, true); // forceRefresh — user explicitly navigated
    };

    this.shadowRoot.getElementById('epc-prev')?.addEventListener('click', () => {
      const s = this.shadowRoot.getElementById('epc-start')?.value || this._startDate;
      const e = this.shadowRoot.getElementById('epc-end')?.value   || this._endDate;
      triggerFetch(shiftMonth(s, -1), shiftMonth(e, -1), 'epc-prev');
    });

    this.shadowRoot.getElementById('epc-next')?.addEventListener('click', () => {
      const s = this.shadowRoot.getElementById('epc-start')?.value || this._startDate;
      const e = this.shadowRoot.getElementById('epc-end')?.value   || this._endDate;
      triggerFetch(shiftMonth(s, 1), shiftMonth(e, 1), 'epc-next');
    });

    this.shadowRoot.getElementById('epc-calc')?.addEventListener('click', () => {
      const s = this.shadowRoot.getElementById('epc-start')?.value;
      const e = this.shadowRoot.getElementById('epc-end')?.value;
      if (!s || !e) return;
      this._startDate = s;
      this._endDate   = e;
      try { localStorage.setItem('epc-date-start', s); } catch (_) {}
      try { localStorage.setItem('epc-date-end',   e); } catch (_) {}
      const btn = this.shadowRoot.getElementById('epc-calc');
      if (btn) { btn.disabled = true; btn.textContent = 'Calculating…'; }
      this.fetchData(s, e, true); // forceRefresh — user explicitly requested
    });

    this.shadowRoot.getElementById('epc-exclude-greedy')?.addEventListener('change', (ev) => {
      this._excludeGreedy = ev.target.checked;
      try { localStorage.setItem('epc-exclude-greedy', String(this._excludeGreedy)); } catch (_) {}
      const s = this.shadowRoot.getElementById('epc-start')?.value || this._startDate;
      const e = this.shadowRoot.getElementById('epc-end')?.value   || this._endDate;
      triggerFetch(s, e, 'epc-calc');
    });

    // Purely a display filter over data already fetched and fully ranked — no
    // refetch needed, just a re-render.
    this.shadowRoot.getElementById('epc-topn')?.addEventListener('change', (ev) => {
      const v = ev.target.value;
      this._topN = v === 'all' ? Infinity : (parseInt(v, 10) || 5);
      try { localStorage.setItem('epc-top-n', v); } catch (_) {}
      this.render();
    });

    this.shadowRoot.getElementById('epc-zoom-in')?.addEventListener('click', () => {
      this._chartScale = Math.min(4.0, +(this._chartScale * 1.33).toFixed(2));
      this.render();
    });
    this.shadowRoot.getElementById('epc-zoom-out')?.addEventListener('click', () => {
      this._chartScale = Math.max(0.25, +(this._chartScale / 1.33).toFixed(2));
      this.render();
    });

    this.shadowRoot.getElementById('epc-history-btn')?.addEventListener('click', () => {
      this._showHistory = !this._showHistory;
      this._showBills = false;
      this._editingId = null;
      this._addingNew = false;
      if (this._showHistory && this._history === null) this.fetchHistory();
      this.render();
    });

    // ── Bill source ─────────────────────────────────────────────────────────

    this.shadowRoot.getElementById('epc-bills-btn')?.addEventListener('click', async () => {
      this._showBills = !this._showBills;
      this._showHistory = false;
      this._billError = '';
      if (this._showBills) await this._loadBills();
      this.render();
    });

    this.shadowRoot.getElementById('epc-source')?.addEventListener('change', (ev) => {
      this._setSource(ev.target.value);
    });

    this.shadowRoot.getElementById('epc-bill-exit')?.addEventListener('click', () => {
      this._setSource('live');
    });

    this.shadowRoot.getElementById('epc-bill-scenario')?.addEventListener('change', (ev) => {
      this._billScenario = ev.target.value;
      try { localStorage.setItem('epc-bill-scenario', this._billScenario); } catch (_) {}
      this.fetchData(null, null, true);
    });

    this.shadowRoot.querySelectorAll('.epc-bill-use').forEach(btn => {
      btn.addEventListener('click', () => {
        this._showBills = false;
        this._setSource(`bill:${btn.dataset.id}`);
      });
    });

    this.shadowRoot.querySelectorAll('.epc-bill-del').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = btn.dataset.id;
        const bill = (this._bills || []).find(b => b.id === id);
        if (!confirm(`Delete "${bill ? bill.label : id}"? The comparison for it goes with it.`)) return;
        try {
          await fetch(`/api/grid_lens/bills/${encodeURIComponent(id)}`, { method: 'DELETE' });
        } catch (_) {}
        // Drop the deleted bill's cached comparisons, or switching back to a
        // re-added bill with the same label could serve the old one's numbers.
        for (const k of Object.keys(GridLensCard._cache)) {
          if (k.includes(`src:bill:${id}`)) delete GridLensCard._cache[k];
        }
        await this._loadBills();
        if (this._source === `bill:${id}`) this._setSource('live');
        else this.render();
      });
    });

    this.shadowRoot.getElementById('epc-bill-example')?.addEventListener('click', () => {
      // Illustrative figures, not anyone's bill — round numbers so the shape is
      // obvious, and they reconcile exactly: 233.30 + 450.60 = 683.90, so the
      // arithmetic self-check passes as-is and a user editing one line can see
      // it start to fail. Two contiguous periods because a mid-cycle price
      // change is the common case on a quarterly bill, and the periods must
      // join end-to-start (the model walks them by cumulative day offset).
      this._billDraft = JSON.stringify({
        state: 'NSW',
        network: 'Endeavour Energy',
        postcode: '2500',
        latitude: -34.4,
        retailer: 'Example Energy',
        plan_name: 'Example Saver',
        plan_id: '',
        printed_energy_total: 683.90,
        periods: [{
          start: '2026-06-01', days: 30,
          general_kwh: 400.0, general_rate: 0.3000,
          cl_kwh: 500.0, cl_rate: 0.1600,
          export_kwh: 200.0, fit_rate: 0.03,
          supply_per_day: 1.2000, cl_supply_per_day: 0.1100,
        }, {
          start: '2026-07-01', days: 60,
          general_kwh: 800.0, general_rate: 0.2800,
          cl_kwh: 1000.0, cl_rate: 0.1600,
          export_kwh: 500.0, fit_rate: 0.03,
          supply_per_day: 1.2500, cl_supply_per_day: 0.1100,
        }],
      }, null, 2);
      this._billError = '';
      this.render();
    });

    const _draftBox = this.shadowRoot.getElementById('epc-bill-json');
    if (_draftBox) {
      // Keep the draft across the re-renders the streaming calculation causes,
      // so a half-typed bill is not lost by a plan event landing mid-edit.
      _draftBox.addEventListener('input', (ev) => { this._billDraft = ev.target.value; });
    }

    this.shadowRoot.getElementById('epc-bill-save')?.addEventListener('click', async () => {
      const raw = this.shadowRoot.getElementById('epc-bill-json')?.value || '';
      this._billDraft = raw;
      let facts;
      try {
        facts = JSON.parse(raw);
      } catch (err) {
        this._billError = `That is not valid JSON: ${err.message}`;
        this.render();
        return;
      }
      const pv = parseFloat(this.shadowRoot.getElementById('epc-bill-pv')?.value);
      const clkw = parseFloat(this.shadowRoot.getElementById('epc-bill-clkw')?.value);
      const answers = {
        pv_kw: Number.isFinite(pv) && pv > 0 ? pv : null,
        archetype: this.shadowRoot.getElementById('epc-bill-arch')?.value || 'evening_peak',
      };
      if (Number.isFinite(clkw) && clkw > 0) answers.cl_max_kw = clkw;
      try {
        const resp = await fetch('/api/grid_lens/bills', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ facts, answers }),
        });
        const body = await resp.json();
        if (!resp.ok) {
          // A 422 is the arithmetic self-check refusing a parse that does not
          // reconcile. Show exactly which figures disagree — that is the whole
          // value of the check, and it is also the user's repair instructions.
          const probs = ((body.self_check || {}).problems || []).join('\n');
          this._billError = (body.error || `HTTP ${resp.status}`) +
            (probs ? `:\n${probs}` : '');
          this.render();
          return;
        }
        this._billDraft = '';
        this._billError = '';
        this._showBills = false;
        await this._loadBills();
        this._setSource(`bill:${body.bill.id}`);
      } catch (err) {
        this._billError = String(err);
        this.render();
      }
    });

    // The assumed controlled-load rate: an explicit opt-in, stored on the
    // bill's own intake answers so it survives a reload and is visible in the
    // bills panel rather than being an invisible session flag.
    const _setAssumedCl = async (on) => {
      const bill = this._currentBill();
      if (!bill) return;
      const cl = ((this._data || {}).bill || {}).controlled_load || {};
      const answers = { ...(bill.answers || {}) };
      answers.assume_cl_rate = on ? cl.suggested_rate : null;
      answers.assume_cl_supply = on ? (cl.suggested_supply || 0) : 0;
      try {
        await fetch(`/api/grid_lens/bills/${encodeURIComponent(bill.id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ answers }),
        });
      } catch (_) {}
      for (const k of Object.keys(GridLensCard._cache)) {
        if (k.includes(`src:bill:${bill.id}`)) delete GridLensCard._cache[k];
      }
      await this._loadBills();
      this.fetchData(null, null, true);
    };
    this.shadowRoot.getElementById('epc-bill-assume-cl')
      ?.addEventListener('click', () => _setAssumedCl(true));
    this.shadowRoot.getElementById('epc-bill-unassume-cl')
      ?.addEventListener('click', () => _setAssumedCl(false));

    this.shadowRoot.getElementById('epc-whatif-btn')?.addEventListener('click', () => {
      this._showWhatif = !this._showWhatif;
      this.render();
    });

    // Track the fields as the user types WITHOUT re-rendering on every keystroke —
    // a re-render replaces the <input> mid-keystroke (see the retailer filter's own
    // note above on the same problem), so the value is only read back on Apply/preset.
    this.shadowRoot.getElementById('epc-whatif-battery')?.addEventListener('input', (ev) => {
      this._whatifBatteryKwh = ev.target.value;
    });
    this.shadowRoot.getElementById('epc-whatif-solar')?.addEventListener('input', (ev) => {
      this._whatifSolarPct = ev.target.value;
    });

    const _applyWhatif = (batteryStr, solarStr) => {
      const b = parseFloat(batteryStr);
      const s = parseFloat(solarStr);
      this._whatifBatteryKwh = Number.isFinite(b) ? String(b) : null;
      this._whatifSolarPct = Number.isFinite(s) ? String(s) : null;
      this._whatifApplied = {
        batteryKwh: Number.isFinite(b) ? b : null,
        solarPct: Number.isFinite(s) ? s : null,
      };
      const s0 = this.shadowRoot.getElementById('epc-start')?.value || this._startDate;
      const e0 = this.shadowRoot.getElementById('epc-end')?.value   || this._endDate;
      triggerFetch(s0, e0, 'epc-whatif-apply');
    };

    this.shadowRoot.getElementById('epc-whatif-apply')?.addEventListener('click', () => {
      const bVal = this.shadowRoot.getElementById('epc-whatif-battery')?.value ?? this._whatifBatteryKwh;
      const sVal = this.shadowRoot.getElementById('epc-whatif-solar')?.value ?? this._whatifSolarPct;
      _applyWhatif(bVal, sVal);
    });

    this.shadowRoot.getElementById('epc-whatif-none')?.addEventListener('click', () => {
      _applyWhatif('0', '0');
    });

    this.shadowRoot.getElementById('epc-whatif-current')?.addEventListener('click', () => {
      _applyWhatif(String(this._data.household_battery_kwh ?? 0), '100');
    });

    const _clearWhatif = () => {
      this._whatifApplied = null;
      this._whatifBatteryKwh = null;
      this._whatifSolarPct = null;
      const s0 = this.shadowRoot.getElementById('epc-start')?.value || this._startDate;
      const e0 = this.shadowRoot.getElementById('epc-end')?.value   || this._endDate;
      triggerFetch(s0, e0, 'epc-whatif-clear');
    };
    this.shadowRoot.getElementById('epc-whatif-clear')?.addEventListener('click', _clearWhatif);
    this.shadowRoot.getElementById('epc-whatif-banner-clear')?.addEventListener('click', _clearWhatif);

    if (this._showHistory) {
      this.shadowRoot.getElementById('epc-add-btn')?.addEventListener('click', () => {
        this._addingNew = true; this._editingId = null; this.render();
      });
      this.shadowRoot.getElementById('epc-new-save')?.addEventListener('click', async () => {
        const date = this.shadowRoot.getElementById('epc-new-date')?.value;
        const plan_name = this.shadowRoot.getElementById('epc-new-plan')?.value;
        const notes = this.shadowRoot.getElementById('epc-new-notes')?.value || '';
        if (date && plan_name) { this._addingNew = false; await this.saveHistoryEntry({date, plan_name, notes}); }
      });
      this.shadowRoot.getElementById('epc-new-cancel')?.addEventListener('click', () => {
        this._addingNew = false; this.render();
      });
      this.shadowRoot.getElementById('epc-edit-save')?.addEventListener('click', async () => {
        const btn = this.shadowRoot.getElementById('epc-edit-save');
        const id = btn?.dataset.id;
        const date = this.shadowRoot.getElementById('epc-edit-date')?.value;
        const plan_name = this.shadowRoot.getElementById('epc-edit-plan')?.value;
        const notes = this.shadowRoot.getElementById('epc-edit-notes')?.value || '';
        if (id && date && plan_name) { this._editingId = null; await this.saveHistoryEntry({id, date, plan_name, notes}); }
      });
      this.shadowRoot.getElementById('epc-edit-cancel')?.addEventListener('click', () => {
        this._editingId = null; this.render();
      });
      this.shadowRoot.querySelectorAll('.epc-hist-edit').forEach(btn => {
        btn.addEventListener('click', () => {
          this._editingId = btn.dataset.id; this._addingNew = false; this.render();
        });
      });
      this.shadowRoot.querySelectorAll('.epc-hist-del').forEach(btn => {
        btn.addEventListener('click', async () => {
          if (confirm('Delete this plan change entry?')) await this.deleteHistoryEntry(btn.dataset.id);
        });
      });
    }

    // Full-screen plan breakout — click a plan's cost banner to expand it; click it
    // again (or the ✕, the dialog's own backdrop, or Escape) to close. Rendered as a
    // native <dialog> (see the plansHtml build above), so its 'close' event — which
    // fires for every one of those paths, including Escape, since that's native
    // <dialog> behaviour — is the ONE place _fullscreenPlan ever gets cleared.
    this.shadowRoot.querySelectorAll('.plan-card .cost-display').forEach((el) => {
      el.addEventListener('click', () => {
        const card = el.closest('.plan-card');
        const plan = card?.dataset.plan;
        if (!plan) return;
        if (this._fullscreenPlan === plan) {
          card.close();  // already-expanded plan's own banner — triggers 'close' below
        } else {
          this._fullscreenPlan = plan;
          this.render();
        }
      });
    });
    const fsDialog = this.shadowRoot.getElementById('epc-fs-dialog');
    if (fsDialog) {
      fsDialog.addEventListener('close', () => {
        this._fullscreenPlan = null;
        this.render();
      });
      // <dialog> has no built-in light-dismiss (unlike the Popover API) — a click
      // that lands on the dialog element itself, rather than a descendant, means it
      // hit the backdrop area, so treat that as "click outside to close".
      fsDialog.addEventListener('click', (ev) => {
        if (ev.target === fsDialog) fsDialog.close();
      });
      this.shadowRoot.getElementById('epc-fs-close')?.addEventListener('click', () => fsDialog.close());
      // render() fully replaces the shadow root every time (including on every
      // streamed plan-priced event while this is open), so this is always a fresh,
      // never-yet-shown node — showModal() promotes it to the top layer, which is
      // what actually escapes ha-card/:host's containment (see the CSS comment above).
      if (!fsDialog.open) {
        try { fsDialog.showModal(); } catch (_) {}
      }
    }
  }

  renderError(message) {
    this.shadowRoot.innerHTML = `
      <ha-card>
        <div class="error">Error: ${message}</div>
      </ha-card>`;
  }

  getCardSize() { return 5; }

  static getConfigElement() {
    return document.createElement('grid-lens-card-editor');
  }

  static getStubConfig() {
    return {
      entity: 'sensor.grid_lens_current_plan_monthly_cost',
      show_breakdown: true,
      show_charts: true,
    };
  }
}

// Class-level response cache: persists across navigation within the same browser
// session so that returning to the dashboard is instant.
GridLensCard._cache = {};

// Chart palette — single source of truth so the hourly-profile charts, their legends,
// and the cost banner never disagree. Re-picked 2026-08-30 (dataviz skill's
// validate_palette.js, OKLab CVD deltaE, --pairs all against every colour this card can
// show at once — up to 6 device colours plus household/solar/buying/selling/spend/income/
// SOC all render in the same plan-card, stacked vertically). The original values had real
// clashes, not just theoretical ones: --spend (#EF5350) and the old selling green
// (#4CAF50) were only 1.2 ΔE apart under deuteranopia — a red-vs-green pair on a page
// about money — and DEVICE_COLORS reused near-identical blue/teal/orange/red to the
// buying/income/household/spend roles it sits right above in the same card. See
// GRIDLENS_CHECKLIST.md 2026-08-30 for the full before/after. Known accepted gap: SOC vs
// device colour 0 clears the normal-vision floor but sits in the 6-8 CVD floor band
// (WARN) — legal because every swatch on this card is always paired with a text label
// (legend name, tooltip), so colour is never the sole identity cue.
GridLensCard.HOUSEHOLD_COLOR = '#c49a1c';
GridLensCard.SOLAR_COLOR = '#FDD835';
GridLensCard.BUYING_COLOR = '#2196F3';
GridLensCard.SELLING_COLOR = '#31d37d';
GridLensCard.SPEND_COLOR = '#EF5350';
GridLensCard.INCOME_COLOR = '#26A69A';
GridLensCard.SOC_COLOR = '#046d9a';
// Categorical — fixed order, one slot per configured deferrable device, never re-sorted.
GridLensCard.DEVICE_COLORS = ['#7b247b', '#180ced', '#a91504', '#bc2fae', '#ff38eb', '#27920c'];

customElements.define('grid-lens-card', GridLensCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-card',
  name: 'Grid Lens',
  description: 'Compare electricity plans with hourly buy/sell charts',
  preview: true,
});

console.info(
  '%c ELECTRICITY-PLAN-COMPARISON-CARD %c v3.14.0 ',
  'color: white; background: #039be5; font-weight: 700;',
  'color: #039be5; background: white; font-weight: 700;',
);
