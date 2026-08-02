/*
 * Grid Lens Deferrable Load Control Card
 *
 * One row per configured deferrable load (controllable or not), each with:
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
 * Config:
 *   type: custom:grid-lens-load-control-card
 *   title: Deferrable Loads          (optional)
 */
import { STYLE, esc } from './grid-lens-chart-common.js?v=20260802b';

function friendlyNote(note) {
  if (!note) return '';
  if (note.startsWith('command_error')) return 'Command error';
  return note.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

class GridLensLoadControlCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._sig = '';
    this._dark = false;
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

  _resolveRows() {
    const devices = this._resolveDevices();
    const rows = devices.map((d) => {
      const phys = d.switch_entity || null;
      return {
        device: d,
        controlEid: this._controlSwitchFor(phys),
        selEid: this._overrideSelectFor(phys),
        gEid: this._greedySwitchFor(phys, 'greedy'),
        gsEid: this._greedySwitchFor(phys, 'greedy_schedule'),
        gfEid: this._greedySwitchFor(phys, 'greedy_surplus'),
        boostEid: this._boostFor(d.energy_entity),
      };
    });
    rows.sort((r1, r2) => (r1.device.name || '').localeCompare(r2.device.name || ''));
    return rows;
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this.classList.toggle('dark', dark); }

    const rows = this._resolveRows();
    const sig = rows.map((r) => {
      const c = r.controlEid && hass.states[r.controlEid];
      const s = r.selEid && hass.states[r.selEid];
      const g = r.gEid && hass.states[r.gEid];
      const gs = r.gsEid && hass.states[r.gsEid];
      const gf = r.gfEid && hass.states[r.gfEid];
      const b = r.boostEid && hass.states[r.boostEid];
      const ceiling = this._boostCeiling(r.device);
      return [
        r.device.energy_entity,
        // greedy_reason / forecast_free_kwh live in the control switch's ATTRIBUTES and
        // move while its state stays "on", so they need to be in the signature or the
        // greedy status line would freeze at whatever it said on the last state change.
        c ? [c.state, (c.attributes || {}).note, (c.attributes || {}).greedy_reason,
             (c.attributes || {}).greedy_blocked,
             (c.attributes || {}).forecast_free_kwh].join('|') : '',
        s ? s.state : '',
        g ? g.state : '',
        gs ? gs.state : '',
        gf ? gf.state : '',
        b ? b.state : '',
        // Time-dependent (the window rolls forward), so it belongs in the repaint
        // signature — otherwise the ceiling hint goes stale as the day advances.
        ceiling == null ? '' : ceiling.toFixed(1),
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
        .boost { display: flex; align-items: center; gap: 3px; flex: 0 0 auto;
               border: 1px solid var(--border); border-radius: 7px; padding: 3px 7px; }
        .boost.active { border-color: var(--good); }
        .boost.over { border-color: var(--buy); }
        .boost-cap { font-size: 10px; font-weight: 600; color: var(--buy); white-space: nowrap; }
        .boost-input { width: 42px; border: none; background: transparent; color: var(--ink);
               font-size: 12px; font-family: inherit; text-align: right; }
        .boost-input::-webkit-outer-spin-button, .boost-input::-webkit-inner-spin-button { margin: 0; }
        .boost-unit { font-size: 10px; color: var(--ink2); }
      </style>
      <div class="card"><div class="body"></div></div>
    `;
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
      const meta = d.controllable
        ? `${on ? 'Controlling' : 'Not controlling'} · ${esc(d.switch_entity || '')}${note ? ' · ' + esc(note) : ''}`
        : 'Forecast only — no control switch configured';
      // Second meta line: the live greedy story. Answers "is greedy doing anything right
      // now, and if not, how close is it?" — the boolean toggles above only say it's
      // armed. Rendered off the control switch's own attributes (the controller's
      // status()), so it needs no extra entity lookup.
      const greedyLine = this._greedyLine(a);

      // Control area: a fully-wired device gets the real segmented control (or a plain
      // toggle fallback if the override select hasn't appeared yet); a device with no
      // switch configured at all gets the same-shaped control, disabled, with a tooltip
      // explaining why — so "no control" reads as a configuration gap, not a bug.
      let controlHtml;
      if (!d.controllable) {
        controlHtml = `
          <div class="ovr disabled" title="Full control has not yet been configured — assign a switch via the Grid Lens integration's Reconfigure flow to enable Off now / On now / Auto.">
            <button disabled>Off now</button>
            <button disabled>On now</button>
            <button disabled>Auto</button>
          </div>`;
      } else if (r.selEid) {
        const cur = hass.states[r.selEid] ? hass.states[r.selEid].state : null;
        controlHtml = `
          <div class="ovr" data-sel="${esc(r.selEid)}" data-ctl="${esc(r.controlEid)}" title="Manual override">
            <button data-opt="Force Off" class="${cur === 'Force Off' ? 'active off' : ''}">Off now</button>
            <button data-opt="Force On" class="${cur === 'Force On' ? 'active' : ''}">On now</button>
            <button data-opt="Auto" class="${cur === 'Auto' && on ? 'active' : ''}"
              title="Grid Lens schedules this load">Auto</button>
          </div>`;
      } else if (r.controlEid) {
        controlHtml = `<div class="sw ${on ? 'on' : ''}" data-eid="${esc(r.controlEid)}" title="${on ? 'Turn off' : 'Turn on'}"></div>`;
      } else {
        // controllable per config, but the control entities haven't appeared yet (a
        // startup race) — same disabled placeholder, self-heals on the next repaint.
        controlHtml = `
          <div class="ovr disabled" title="Control entities are still loading…">
            <button disabled>Off now</button><button disabled>On now</button><button disabled>Auto</button>
          </div>`;
      }

      // Greedy Consumption's per-device toggles (see switch.py) — opportunistic "on"
      // whenever import is free, export is being wasted, or the plan forecasts a spill
      // bigger than this load, on top of Auto/plan control. Only meaningful (and only
      // created) for a controllable device.
      let greedyHtml = '';
      if (r.controlEid && r.gEid) {
        const gOn = hass.states[r.gEid] && hass.states[r.gEid].state === 'on';
        const gsOn = r.gsEid && hass.states[r.gsEid] && hass.states[r.gsEid].state === 'on';
        const gfOn = r.gfEid && hass.states[r.gfEid] && hass.states[r.gfEid].state === 'on';
        greedyHtml = `
          <div class="greedy">
            <div class="gbtn${gOn ? ' on' : ''}" data-eid="${esc(r.gEid)}"
                 title="Greedy Consumption: turn on whenever import is free or export is being wasted">
              <ha-icon icon="mdi:leaf"></ha-icon>
            </div>
            ${r.gsEid ? `
            <div class="gbtn${gsOn ? ' on' : ''}" data-eid="${esc(r.gsEid)}"
                 title="Greedy Respects Schedule: only greedy-fire inside this load's own availability window">
              <ha-icon icon="mdi:calendar-clock"></ha-icon>
            </div>` : ''}
            ${r.gfEid ? `
            <div class="gbtn${gfOn ? ' on' : ''}" data-eid="${esc(r.gfEid)}"
                 title="Greedy Forecast Surplus: also start early when the plan forecasts more free energy going to waste than this load could use (needs Greedy Consumption on; may import briefly)">
              <ha-icon icon="mdi:weather-sunny-alert"></ha-icon>
            </div>` : ''}
          </div>`;
      }

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
          <div class="boost${active ? ' active' : ''}${over ? ' over' : ''}" title="${esc(tip)}">
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
          </div>
          ${boostHtml}
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
    body.querySelectorAll('.gbtn').forEach((el) => {
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
  }
}

customElements.define('grid-lens-load-control-card', GridLensLoadControlCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-load-control-card',
  name: 'Grid Lens Load Control',
  description: 'Per-device deferrable load control, Greedy Consumption, and Today Boost target, auto-discovered.',
});
