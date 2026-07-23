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
    this._activeSource = null;
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
    } catch (_) {}
    this.render();
  }

  set hass(_hass) {
    // We don't use entity states — just need to know HA is connected.
    // Never store the hass object: it's huge and HA reassigns it on every entity
    // state change, which causes severe GC pressure in Chrome.
    if (!this._connected) {
      this._connected = true;
      // Use saved dates on init so navigating back restores the last comparison.
      const s = this._startDate ? `${this._startDate}T00:00:00` : null;
      const e = this._endDate   ? `${this._endDate}T23:59:59`   : null;
      this.fetchData(s, e);
    }
  }

  async fetchData(startDate = null, endDate = null, forceRefresh = false) {
    if (!this._connected || this._fetching) return;

    // Return cached result instantly when navigating back to the card.
    const cacheKey = `${startDate || ''}|${endDate || ''}`;
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
    // Close any previous stream
    if (this._activeSource) { this._activeSource.close(); this._activeSource = null; }

    this._fetching = true;
    this._streamPhase = 'fetching';
    this._plansDone = 0;
    this._plansTotal = 0;
    this._fetchStep = 0;
    this._fetchTotal = 0;
    this._fetchMessage = '';
    this._data = null;   // clear stale data so we render from stream only
    if (startDate) this._startDate = startDate.substring(0, 10);
    if (endDate)   this._endDate   = endDate.substring(0, 10);
    this._showStreamProgress();

    const params = (startDate && endDate)
      ? `?start_date=${startDate}&end_date=${endDate}` : '';
    const src = new EventSource(`/api/grid_lens/plan_stream${params}`);
    this._activeSource = src;

    src.addEventListener('status', (e) => {
      const d = JSON.parse(e.data);
      this._streamPhase   = d.phase || 'fetching';
      this._plansTotal    = d.plans_total || this._plansTotal;
      this._fetchStep     = d.fetch_step  || this._fetchStep;
      this._fetchTotal    = d.fetch_total || this._fetchTotal;
      this._fetchMessage  = d.message || this._fetchMessage;
      this._showStreamProgress();
    });

    src.addEventListener('plan', (e) => {
      const d = JSON.parse(e.data);
      this._streamPhase = 'optimising';
      this._plansDone   = d.plans_done;
      this._plansTotal  = d.plans_total;
      // Build/update _data with what's arrived so far so render() works unchanged
      if (!this._data) {
        this._data = {
          plan_details: {},
          current_plan_name: d.current_plan_name,
          alternative_plans: {},
          current_plan_total: 0,
          usage_days: d.usage_days || 0,
          start_date: d.start_date || '',
          end_date: d.end_date || '',
          energy_flows: d.energy_flows || {},
          deferrable_devices: d.deferrable_devices || [],
          calculation_date: null,
        };
        this._updateDatesFromData(startDate);
      }
      this._data.plan_details[d.plan_key] = d.detail;
      this._data.alternative_plans = d.alternative_plans || {};
      this._data.current_plan_total = d.current_plan_total || 0;
      this.render();
    });

    src.addEventListener('complete', (e) => {
      src.close();
      this._activeSource = null;
      this._streamPhase = null;
      this._plansDone   = 0;
      this._plansTotal  = 0;
      this._fetching    = false;
      const full = JSON.parse(e.data);
      this._data = full;
      const cacheKey = `${startDate || ''}|${endDate || ''}`;
      GridLensCard._cache[cacheKey] = full;
      this._updateDatesFromData(startDate);
      this.render();
    });

    src.onerror = () => {
      src.close();
      this._activeSource = null;
      this._streamPhase = null;
      this._fetching = false;
      this.renderError('Calculation stream failed — check HA logs.');
    };
  }

  _showStreamProgress() {
    const phase = this._streamPhase;
    const isFetching = phase === 'fetching';
    const msg = isFetching
      ? (this._fetchMessage
          ? `${this._fetchMessage} (${this._fetchStep} / ${this._fetchTotal || '?'})`
          : 'Fetching energy data…')
      : `Optimising plans… ${this._plansDone} / ${this._plansTotal}`;
    const pct = isFetching
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

  renderDivergingChart(profile, upKey, downKey, maxVal, upColor, downColor, scale = 1) {
    if (!profile || !profile.length) return '';
    const W = 288, H = Math.round(80 * scale), BAR = 11, GAP = 1, MID = Math.round(38 * scale);
    const barScale = (MID - 3) / (maxVal || 1);
    const bars = profile.map((slot, i) => {
      const x = i * (BAR + GAP);
      const upH = Math.min(Math.max(slot[upKey] * barScale, 0), MID - 3);
      const dnH = Math.min(Math.max(slot[downKey] * barScale, 0), H - MID - 3);
      const parts = [];
      if (upH > 0.3) parts.push(
        `<rect x="${x}" y="${MID - upH}" width="${BAR}" height="${upH}" fill="${upColor}">` +
        `<title>${slot.hour}:00  ${slot[upKey].toFixed(3)}</title></rect>`
      );
      if (dnH > 0.3) parts.push(
        `<rect x="${x}" y="${MID}" width="${BAR}" height="${dnH}" fill="${downColor}">` +
        `<title>${slot.hour}:00  ${slot[downKey].toFixed(3)}</title></rect>`
      );
      return parts.join('');
    }).join('');
    return `<svg width="100%" viewBox="0 0 ${W} ${H}" style="display:block;height:${H}px">
      <line x1="0" y1="${MID}" x2="${W}" y2="${MID}" stroke="var(--divider-color)" stroke-width="0.8"/>
      ${bars}
    </svg>`;
  }

  renderStackedBarChart(profile, maxVal, scale = 1, deferrable_devices = []) {
    if (!profile || !profile.length) return '';
    const DEVICE_COLORS = ['#7B1FA2','#0288D1','#00897B','#F57F17','#E53935','#5C6BC0'];
    const W = 288, H = Math.round(70 * scale), BAR = 11, GAP = 1;
    const barScale = (H - 4) / (maxVal || 1);
    const bars = profile.map((slot, i) => {
      const x = i * (BAR + GAP);
      const homeH = Math.min(Math.max((slot.home_load_kwh || 0) * barScale, 0), H - 4);
      const perDev = slot.deferrable_per_device || [];
      const total = (slot.home_load_kwh||0) + perDev.reduce((s, v) => s + v, 0) || (slot.deferrable_kwh||0);
      const parts = [];
      if (homeH > 0.3) parts.push(
        `<rect x="${x}" y="${H - homeH}" width="${BAR}" height="${homeH}" fill="#FF9800">` +
        `<title>${slot.hour}:00  Household ${(slot.home_load_kwh||0).toFixed(3)} kWh\nTotal ${total.toFixed(3)} kWh</title></rect>`
      );
      let stackTop = homeH;
      if (perDev.length > 0) {
        perDev.forEach((kw, ii) => {
          const devH = Math.min(Math.max(kw * barScale, 0), H - 4 - stackTop);
          if (devH > 0.3) {
            const devName = (deferrable_devices[ii] && deferrable_devices[ii].name) || `Device ${ii + 1}`;
            const col = DEVICE_COLORS[ii % DEVICE_COLORS.length];
            parts.push(
              `<rect x="${x}" y="${H - stackTop - devH}" width="${BAR}" height="${devH}" fill="${col}">` +
              `<title>${slot.hour}:00  ${devName} ${kw.toFixed(3)} kWh\nTotal ${total.toFixed(3)} kWh</title></rect>`
            );
            stackTop += devH;
          }
        });
      } else {
        const defH = Math.min(Math.max((slot.deferrable_kwh || 0) * barScale, 0), H - 4 - stackTop);
        if (defH > 0.3) parts.push(
          `<rect x="${x}" y="${H - stackTop - defH}" width="${BAR}" height="${defH}" fill="${DEVICE_COLORS[0]}">` +
          `<title>${slot.hour}:00  Deferrable ${(slot.deferrable_kwh||0).toFixed(3)} kWh\nTotal ${total.toFixed(3)} kWh</title></rect>`
        );
      }
      return parts.join('');
    }).join('');
    return `<svg width="100%" viewBox="0 0 ${W} ${H}" style="display:block;height:${H}px">
      ${bars}
    </svg>`;
  }

  renderSolarChart(profile, maxVal, scale = 1) {
    if (!profile || !profile.length) return '';
    const W = 288, H = Math.round(55 * scale), BAR = 11, GAP = 1;
    const barScale = (H - 4) / (maxVal || 1);
    const bars = profile.map((slot, i) => {
      const x = i * (BAR + GAP);
      const solH = Math.min(Math.max((slot.solar_kwh || 0) * barScale, 0), H - 4);
      if (solH < 0.3) return '';
      return `<rect x="${x}" y="${H - solH}" width="${BAR}" height="${solH}" fill="#FDD835">` +
             `<title>${slot.hour}:00  Solar ${(slot.solar_kwh||0).toFixed(3)} kWh</title></rect>`;
    }).join('');
    return `<svg width="100%" viewBox="0 0 ${W} ${H}" style="display:block;height:${H}px">
      ${bars}
    </svg>`;
  }

  renderSocChart(profile, scale = 1) {
    if (!profile || !profile.length) return '';
    const W = 288, H = Math.round(50 * scale), BAR = 11, GAP = 1;
    const pts = profile.map((slot, i) => {
      const x = i * (BAR + GAP) + BAR / 2;
      const y = H - (slot.soc_percent || 0) / 100 * (H - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const y20 = H - 20 / 100 * (H - 4) - 2;
    const y80 = H - 80 / 100 * (H - 4) - 2;
    return `<svg width="100%" viewBox="0 0 ${W} ${H}" style="display:block;height:${H}px">
      <line x1="0" y1="${y80.toFixed(1)}" x2="${W}" y2="${y80.toFixed(1)}" stroke="var(--divider-color)" stroke-width="0.5" stroke-dasharray="3,3"/>
      <line x1="0" y1="${y20.toFixed(1)}" x2="${W}" y2="${y20.toFixed(1)}" stroke="var(--divider-color)" stroke-width="0.5" stroke-dasharray="3,3"/>
      <polyline points="${pts}" fill="none" stroke="#00BCD4" stroke-width="1.5" stroke-linejoin="round"/>
      ${profile.map((slot, i) => {
        const x = i * (BAR + GAP) + BAR / 2;
        const y = H - (slot.soc_percent || 0) / 100 * (H - 4) - 2;
        return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2" fill="#00BCD4">` +
               `<title>${slot.hour}:00  SOC ${(slot.soc_percent||0).toFixed(0)}%</title></circle>`;
      }).join('')}
    </svg>`;
  }

  renderHourLabels() {
    return `<div style="display:flex;justify-content:space-between;font-size:9px;color:var(--secondary-text-color);margin-top:1px">
      <span>12am</span><span>3am</span><span>6am</span><span>9am</span><span>noon</span><span>3pm</span><span>6pm</span><span>9pm</span><span>11pm</span>
    </div>`;
  }

  // ── Plan history ─────────────────────────────────────────────────────────

  _esc(str) {
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
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

  // ── Main render ───────────────────────────────────────────────────────────

  render() {
    if (!this._config || !this._connected) return;

    const showBreakdown = this._config.show_breakdown !== false;
    const showCharts = this._config.show_charts !== false;
    const chartScale = this._chartScale * (parseFloat(this._config.chart_scale) || 1.0);
    const planFilter = this._config.plan;

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
        .plan-title {
          font-size: 16px;
          font-weight: 500;
          margin-bottom: 8px;
          color: var(--primary-text-color);
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
        .breakdown-value { color: var(--primary-text-color); font-weight: 500; white-space: nowrap; margin-left: 8px; }
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
        .bill-fit { color: var(--success-color, #4CAF50); }
        .bill-note { font-size: 11px; color: var(--warning-color, #FF9800); font-style: italic; padding: 4px 0; }
        .chart-section { margin-top: 14px; }
        .chart-label {
          font-size: 11px;
          color: var(--secondary-text-color);
          margin-bottom: 3px;
        }
        .strategy-box {
          background: var(--secondary-background-color);
          padding: 12px;
          border-radius: 6px;
          margin-top: 12px;
        }
        .strategy-title { font-size: 12px; font-weight: 600; margin-bottom: 6px; color: var(--primary-text-color); }
        .strategy-text { font-size: 12px; color: var(--secondary-text-color); line-height: 1.4; white-space: pre-line; }
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
      </style>
    `;

    if (!this._data) {
      this.shadowRoot.innerHTML = `${styles}<ha-card>
        <div class="loading">Loading plan data...</div>
      </ha-card>`;
      return;
    }

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
      const optimization = details.optimization || {};
      const schedule = optimization.schedule || [];
      const profile = details.hourly_profile || null;
      const isCurrentPlan = planName === currentPlanName;
      const total = breakdown.total || 0;
      const savings = total - currentPlanTotal;
      const isCheaper = savings < -0.05;

      // Banner colour: orange=current, cyan=cheapest, green=cheaper, red=more expensive
      let bannerColor;
      if (isCurrentPlan) {
        bannerColor = '#FF9800';
      } else if (Math.abs(total - minTotal) < 0.01) {
        bannerColor = '#00BCD4';
      } else if (isCheaper) {
        bannerColor = '#4CAF50';
      } else {
        bannerColor = '#EF5350';
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
        if (bi) {
          let rows = '';

          // Energy lines
          rows += '<div class="bill-section-head">Energy charges</div>';
          bi.energy_lines.forEach(line => {
            rows += `<div class="breakdown-row">
              <div class="breakdown-label">${line.label}<br>
                <span style="font-size:11px;opacity:0.7">${line.rate_c.toFixed(2)}&thinsp;c/kWh &times; ${line.kwh.toFixed(1)}&thinsp;kWh</span>
              </div>
              <div class="breakdown-value">$${line.amount.toFixed(2)}</div>
            </div>`;
          });

          // Supply charge
          const s = bi.supply;
          rows += '<div class="bill-section-head">Daily supply charge</div>';
          rows += `<div class="breakdown-row">
            <div class="breakdown-label">Supply charge<br>
              <span style="font-size:11px;opacity:0.7">${(s.rate_per_day * 100).toFixed(2)}&thinsp;c/day &times; ${s.days}&thinsp;days</span>
            </div>
            <div class="breakdown-value">$${s.amount.toFixed(2)}</div>
          </div>`;

          // Subscription fee (e.g. Amber $25/month)
          if (bi.subscription) {
            const sub = bi.subscription;
            rows += '<div class="bill-section-head">Subscription fee</div>';
            rows += `<div class="breakdown-row">
              <div class="breakdown-label">Membership subscription<br>
                <span style="font-size:11px;opacity:0.7">$${sub.rate_per_month.toFixed(2)}/month &times; ${sub.months.toFixed(1)}&thinsp;months</span>
              </div>
              <div class="breakdown-value">$${sub.amount.toFixed(2)}</div>
            </div>`;
          }

          // Demand charge (peak-kW), only present when on a demand tariff
          if (bi.demand) {
            const dm = bi.demand;
            rows += '<div class="bill-section-head">Demand charge</div>';
            rows += `<div class="breakdown-row">
              <div class="breakdown-label">${dm.label}<br>
                <span style="font-size:11px;opacity:0.7">${dm.peak_kw.toFixed(2)}&thinsp;kW peak &times; ${(dm.rate_per_kw_per_day * 100).toFixed(2)}&thinsp;c/kW/day &times; ${dm.days}&thinsp;days</span>
              </div>
              <div class="breakdown-value">$${dm.amount.toFixed(2)}</div>
            </div>`;
          }

          // VPP participation credit
          if (bi.vpp_credit) {
            rows += '<div class="bill-section-head bill-fit">VPP credit</div>';
            rows += `<div class="breakdown-row bill-fit">
              <div class="breakdown-label" style="color:inherit">VPP participation credit</div>
              <div class="breakdown-value" style="color:inherit">&minus;$${bi.vpp_credit.toFixed(2)}</div>
            </div>`;
          }

          // Feed-in credit
          const f = bi.fit;
          if (f.kwh > 0) {
            rows += '<div class="bill-section-head bill-fit">Feed-in credit</div>';
            rows += `<div class="breakdown-row bill-fit">
              <div class="breakdown-label" style="color:inherit">Feed-in tariff<br>
                <span style="font-size:11px;opacity:0.7">${f.rate_c.toFixed(2)}&thinsp;c/kWh &times; ${f.kwh.toFixed(1)}&thinsp;kWh</span>
              </div>
              <div class="breakdown-value" style="color:inherit">&minus;$${f.credit.toFixed(2)}</div>
            </div>`;
          }

          // PEA (Price Efficiency Adjustment — Flow Power)
          if (bi.pea_credit != null || bi.pea_breakdown) {
            const pb = bi.pea_breakdown;
            rows += '<div class="bill-section-head bill-fit">Other credits</div>';
            if (pb) {
              const creditSign = bi.pea_credit >= 0 ? '&minus;' : '+';
              const creditAbs = Math.abs(bi.pea_credit).toFixed(2);
              rows += `<div class="breakdown-row bill-fit">
                <div class="breakdown-label" style="color:inherit">Price Efficiency Adjustment<br>
                  <span style="font-size:11px;opacity:0.7">
                    LWAP ${pb.lwap_c.toFixed(3)}c &minus; TWAP ${pb.twap_c.toFixed(3)}c = CPEA ${pb.cpea_c.toFixed(3)}c<br>
                    PEA = CPEA &minus; BPEA ${pb.bpea_c.toFixed(1)}c
                    = <strong>${pb.pea_c.toFixed(3)}c/kWh</strong>
                    &times; ${pb.total_kwh.toFixed(1)}&thinsp;kWh
                  </span>
                </div>
                <div class="breakdown-value" style="color:inherit">${creditSign}$${creditAbs}</div>
              </div>`;
            } else {
              rows += `<div class="breakdown-row bill-fit">
                <div class="breakdown-label" style="color:inherit">Price Efficiency Adjustment<br>
                  <span style="font-size:11px;opacity:0.7">Estimated — actual amount varies</span>
                </div>
                <div class="breakdown-value" style="color:inherit">&minus;$${bi.pea_credit.toFixed(2)}</div>
              </div>`;
            }
          }

          // Conditional day-credits (e.g. GloBird ZEROHERO's $1/day)
          if (bi.conditional_credits) {
            rows += '<div class="bill-section-head bill-fit">Conditional credits</div>';
            Object.entries(bi.conditional_credits).forEach(([label, c]) => {
              rows += `<div class="breakdown-row bill-fit">
                <div class="breakdown-label" style="color:inherit">${label}<br>
                  <span style="font-size:11px;opacity:0.7">Earned ${c.days_earned}/${c.days_total}&thinsp;days</span>
                </div>
                <div class="breakdown-value" style="color:inherit">&minus;$${c.amount.toFixed(2)}</div>
              </div>`;
            });
          }

          // Total + GST
          rows += `<div class="bill-total-row">
            <span>Total (inc. GST)</span><span>$${bi.total.toFixed(2)}</span>
          </div>`;
          rows += `<div class="bill-gst-row">
            <span>GST included (1/11)</span><span>$${bi.gst_included.toFixed(2)}</span>
          </div>`;

          if (bi.optimisation_note) {
            rows += `<div class="bill-note">${bi.optimisation_note}</div>`;
          }

          breakdownHtml = `<div class="breakdown-section">${rows}</div>`;

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
        const DEVICE_COLORS = ['#7B1FA2','#0288D1','#00897B','#F57F17','#E53935','#5C6BC0'];
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
          : (hasEv ? '&nbsp;<span style="color:#7B1FA2;font-weight:600">■ deferrable</span>' : '');

        const loadChartHtml = (hasHomeLoad || hasEv) ? `
            <div class="chart-label" style="margin-top:10px">
              Avg hourly load &nbsp;
              ${hasHomeLoad ? '<span style="color:#FF9800;font-weight:600">■ household</span>' : ''}
              ${devLegend}
              &nbsp;(kWh)
            </div>
            ${this.renderStackedBarChart(profile, loadSolarMax, chartScale, deferrable_devices)}` : '';

        const solarChartHtml = hasSolar ? `
            <div class="chart-label" style="margin-top:10px">
              Avg hourly solar generation &nbsp;
              <span style="color:#FDD835;font-weight:600;text-shadow:0 0 2px #999">■</span>
              <span style="font-weight:600"> solar</span>
              &nbsp;(kWh)
            </div>
            ${this.renderSolarChart(profile, loadSolarMax, chartScale)}` : '';

        const socChartHtml = hasSoc ? `
            <div class="chart-label" style="margin-top:10px">
              Avg battery SOC &nbsp;(%)
            </div>
            ${this.renderSocChart(profile, chartScale)}` : '';

        chartHtml = `
          <div class="chart-section">
            ${loadChartHtml}
            ${solarChartHtml}
            <div class="chart-label"${(hasHomeLoad || hasEv || hasSolar) ? ' style="margin-top:10px"' : ''}>
              Average hourly energy &nbsp;
              <span style="color:#2196F3;font-weight:600">■ buying</span> ↑ &nbsp;
              <span style="color:#4CAF50;font-weight:600">■ selling</span> ↓ &nbsp; (kWh)
            </div>
            ${this.renderDivergingChart(profile, 'import_kwh', 'export_kwh', maxKwh, '#2196F3', '#4CAF50', chartScale)}
            <div class="chart-label" style="margin-top:10px">
              Average hourly cost &nbsp;
              <span style="color:#EF5350;font-weight:600">■ spend</span> ↑ &nbsp;
              <span style="color:#26A69A;font-weight:600">■ income</span> ↓ &nbsp; ($)
            </div>
            ${this.renderDivergingChart(profile, 'import_cost', 'export_income', maxCost, '#EF5350', '#26A69A', chartScale)}
            ${socChartHtml}
            ${this.renderHourLabels()}
          </div>`;
      }


      const strategyHtml = details.strategy ? `
        <div class="strategy-box">
          <div class="strategy-title">📋 Optimisation Strategy</div>
          <div class="strategy-text">${details.strategy}</div>
        </div>` : '';

      return `
        <div class="plan-card${isCurrentPlan ? ' current-plan' : ''}">
          <div class="plan-title">${planName}</div>
          <div class="cost-display" style="background:${bannerColor}">
            <div class="cost-amount">$${total.toFixed(2)}</div>
            <div class="cost-label">${savingsLabel}</div>
          </div>
          ${chartHtml}
          ${breakdownHtml}
          ${strategyHtml}
        </div>`;
    }).join('');

    const _busy = this._streamPhase !== null;
    const dateControlsHtml = `
      <div class="date-controls">
        <button id="epc-prev" class="nav-btn" title="Back 1 month" ${_busy ? 'disabled' : ''}>&#8592; 1 mo</button>
        <label>From</label>
        <input type="date" id="epc-start" value="${this._startDate}" ${_busy ? 'disabled' : ''}>
        <label>to</label>
        <input type="date" id="epc-end" value="${this._endDate}" ${_busy ? 'disabled' : ''}>
        <button id="epc-next" class="nav-btn" title="Forward 1 month" ${_busy ? 'disabled' : ''}>1 mo &#8594;</button>
        <button id="epc-calc" ${_busy ? 'disabled' : ''}>${_busy ? 'Calculating…' : 'Calculate'}</button>
        <span class="zoom-controls">
          <button id="epc-zoom-out" class="nav-btn" title="Shrink charts">&#8722;</button>
          <span class="zoom-label">zoom</span>
          <button id="epc-zoom-in" class="nav-btn" title="Grow charts">&#43;</button>
        </span>
        ${calcDate ? `<span class="date-meta">${usageDays} days &nbsp;·&nbsp; updated ${calcDate}</span>` : ''}
        ${(this._plansDone > 0 && this._plansTotal > 0) ? `
          <span class="stream-label">${this._plansDone}/${this._plansTotal} plans</span>
          <span class="stream-track"><span class="stream-bar" style="width:${Math.round(this._plansDone/this._plansTotal*100)}%"></span></span>` : ''}
        <button id="epc-history-btn" class="nav-btn${this._showHistory ? ' active' : ''}">History</button>
        <a href="/api/grid_lens/diagnostic_export" download class="nav-btn" title="Download diagnostic zip for bug reporting" style="text-decoration:none;">&#8659; Diagnostic</a>
      </div>`;

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

    const bodyHtml = this._showHistory
      ? this.renderHistoryPanel(planNames)
      : `<div class="plan-grid">${plansHtml}${skeletonsHtml}</div>`;

    this.shadowRoot.innerHTML = `
      ${styles}
      <ha-card>
        ${dateControlsHtml}
        ${bodyHtml}
      </ha-card>`;

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
      this._editingId = null;
      this._addingNew = false;
      if (this._showHistory && this._history === null) this.fetchHistory();
      this.render();
    });

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
