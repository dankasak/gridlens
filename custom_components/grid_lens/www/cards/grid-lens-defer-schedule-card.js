/*
 * Grid Lens Deferrable Schedule Card
 *
 * Gantt-style weekly editor for each deferrable load's allowed run times: 7 weekday
 * rows (Monday first, matching Python's date.weekday()) x 48 half-hour cells. Click or
 * drag to paint periods allowed/blocked, then Save — persisted via the
 * grid_lens.set_deferrable_schedule service into the integration's schedule store,
 * which the LP optimizer reads per-slot by each slot's actual local weekday (see
 * advisory/coordinator._deferrable_for_horizon and plan_calculator).
 *
 * Use-case: "on Tuesdays I drive the EV to work, so it can only charge overnight;
 * on weekends it can charge all day."
 *
 * Auto-discovery: devices come from the `deferrable_loads` attribute published by the
 * GridLens cost sensor (sensor.py's _build_deferrable_loads — same pattern as the
 * power-flow and boost-tuning cards), which now also carries each device's stored
 * `schedule` and its config-derived `default_schedule`, so this card needs zero
 * per-install config. Editing state is optimistic: after Save the painted grid is
 * kept locally (the sensor attribute only refreshes on its next coordinator write).
 *
 * Colour: each device's "allowed" cells use the same validated --defer1..4 /
 * --hotwater palette as every other GridLens card (deferColorFor in chart-common),
 * so a device reads as the same colour here as on the flow/advisory charts. Blocked
 * cells are an empty neutral — state is fill-vs-empty (large lightness difference),
 * never hue-vs-hue.
 *
 * Config:
 *   type: custom:grid-lens-defer-schedule-card
 *   title: Allowed Run Times            (optional)
 *   source_entity: sensor.xyz           (optional — pin the GridLens sensor to read)
 */
import { STYLE, esc, deferColorFor } from './grid-lens-chart-common.js?v=20260802b';

const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const SLOTS = 48; // half-hour resolution

// Canonical rows are 48 half-hour cells; expand a 24-hour row (older payload) in place.
function toHalfHour(row) {
  if (!row) return Array(SLOTS).fill(1);
  if (row.length === SLOTS) return row.slice();
  const out = [];
  for (const v of row) { out.push(v ? 1 : 0, v ? 1 : 0); }
  return out;
}

function copyWeek(week) {
  return (week || []).map((row) => toHalfHour(row));
}

function weeksEqual(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  for (let d = 0; d < a.length; d++) {
    for (let s = 0; s < SLOTS; s++) if ((a[d][s] ? 1 : 0) !== (b[d][s] ? 1 : 0)) return false;
  }
  return true;
}

function slotLabel(s) {
  const h = Math.floor(s / 2);
  return s % 2 ? `${h}:30–${h + 1}:00` : `${h}:00–${h}:30`;
}

class GridLensDeferScheduleCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._devices = [];
    // Per-device editing state, keyed by energy_entity:
    // { week, hasStored, dirty, baseline } — baseline is what Save/Discard reverts to.
    this._edit = {};
    this._painting = null; // {sensorId, value} while a drag-paint is active
    this._sig = '';
    this._dark = false;
    this._onPointerUp = () => { this._painting = null; };
  }

  setConfig(config) {
    this._config = Object.assign({ title: 'Allowed Run Times' }, config);
    this._renderShell();
  }

  getCardSize() { return Math.max(3, this._devices.length * 4); }

  connectedCallback() { window.addEventListener('pointerup', this._onPointerUp); }
  disconnectedCallback() { window.removeEventListener('pointerup', this._onPointerUp); }

  _resolveDevices() {
    const hass = this._hass;
    if (!hass) return [];
    const pin = this._config.source_entity;
    const candidates = pin ? [pin] : Object.keys(hass.states);
    for (const eid of candidates) {
      if (!pin && !eid.startsWith('sensor.')) continue;
      const st = hass.states[eid];
      const a = st && st.attributes;
      if (a && Array.isArray(a.deferrable_loads) && a.deferrable_loads.length) {
        // Only devices that expose a schedule shape (older integration builds won't).
        const withSched = a.deferrable_loads.filter((d) => d && d.default_schedule);
        if (withSched.length) return withSched;
      }
      if (pin) break;
    }
    return [];
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this.classList.toggle('dark', dark); }

    const devices = this._resolveDevices();
    // Seed/refresh per-device editing state from attributes — but never clobber an
    // in-progress edit (dirty), and never clobber a just-saved grid with a stale
    // attribute snapshot: after Save/Revert the local state is `pinned` (authoritative)
    // until the sensor's attribute catches up and agrees (the backend rewrites it on
    // every schedule service call, but the sensor otherwise only refreshes on a
    // coordinator run, so the old snapshot can outlive the save by a while).
    for (const d of devices) {
      const key = d.energy_entity;
      const stored = Array.isArray(d.schedule) ? d.schedule : null;
      const base = copyWeek(stored || d.default_schedule);
      const cur = this._edit[key];
      if (cur && cur.pinned && weeksEqual(cur.baseline, base)) cur.pinned = false;
      if (!cur) {
        this._edit[key] = {
          week: copyWeek(base), baseline: copyWeek(base), hasStored: !!stored,
          dirty: false, pinned: false,
        };
      } else if (!cur.dirty && !cur.pinned && !weeksEqual(cur.baseline, base)) {
        this._edit[key] = {
          week: copyWeek(base), baseline: copyWeek(base), hasStored: !!stored,
          dirty: false, pinned: false,
        };
      }
    }

    const sig = devices.map((d) => {
      const e = this._edit[d.energy_entity] || {};
      return `${d.energy_entity}|${e.dirty}|${e.hasStored}|${JSON.stringify(e.baseline || null)}`;
    }).join(',') + `#${devices.length}`;
    if (sig !== this._sig) { this._sig = sig; this._devices = devices; this._paint(); }
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}
        .dev { margin-top: 14px; }
        .dev:first-of-type { margin-top: 8px; }
        .dev .nm { display: flex; align-items: center; gap: 8px; font-size: 13.5px;
                   font-weight: 550; color: var(--ink); margin-bottom: 6px; }
        .dev .nm .dot { width: 10px; height: 10px; border-radius: 3px; flex: 0 0 auto; }
        .gridwrap { overflow-x: auto; }
        .sched { display: grid; grid-template-columns: 34px repeat(48, minmax(6px, 1fr));
                 gap: 2px 1px; min-width: 520px; touch-action: none; user-select: none; }
        .hlab { font-size: 9.5px; color: var(--ink2); text-align: left; align-self: end;
                white-space: nowrap; }
        .dlab { font-size: 10.5px; color: var(--ink2); align-self: center; padding-right: 4px; }
        .dlab.today { color: var(--ink); font-weight: 650; }
        .cell { min-height: 15px; height: 100%; border-radius: 2px; cursor: pointer;
                background: var(--plane); border: 1px solid var(--border); box-sizing: border-box; }
        .cell.onc { border-color: transparent; }
        /* Visually pair the two half-hours of each clock hour: the :30 cell carries a
           small right inset so hour boundaries stay readable at 48 columns. */
        .cell.he { margin-right: 2px; }
        .ft { display: flex; align-items: center; gap: 10px; margin-top: 7px; flex-wrap: wrap; }
        .ft .state { font-size: 11px; color: var(--ink2); flex: 1 1 auto; }
        .btn { font-size: 11.5px; font-weight: 600; padding: 4px 12px; border-radius: 8px;
               border: 1px solid var(--border); background: var(--plane); color: var(--ink);
               cursor: pointer; font-family: inherit; }
        .btn.primary { background: var(--good); border-color: var(--good); color: #fff; }
        .btn.linkish { border-color: transparent; background: transparent; color: var(--ink2);
                       text-decoration: underline; padding: 4px 4px; font-weight: 500; }
        .hint { font-size: 11px; color: var(--ink2); margin-top: 10px; }
      </style>
      <div class="card"><div class="body"></div></div>
    `;
  }

  _paint() {
    const body = this.shadowRoot && this.shadowRoot.querySelector('.body');
    if (!body) return;
    const devices = this._devices || [];
    const names = devices.map((d) => d.name || d.energy_entity);
    const todayRow = (new Date().getDay() + 6) % 7; // JS Sunday=0 → Monday-first index

    const header = `
      <div class="hd">
        <div class="title">${esc(this._config.title)}</div>
        <div class="sub">paint when each load may run</div>
      </div>`;

    if (!devices.length) {
      body.innerHTML = header +
        `<div class="waiting">No deferrable loads found.<br>
         <span class="sub">Configure deferrable load sensors in the Grid Lens integration first.</span></div>`;
      return;
    }

    const blocks = devices.map((d, i) => {
      const key = d.energy_entity;
      const edit = this._edit[key];
      const color = deferColorFor(names, i);
      const hourLabels = ['<div></div>'].concat(Array.from({ length: SLOTS }, (_, s) =>
        `<div class="hlab">${s % 12 === 0 ? (s / 2) + ':00' : ''}</div>`)).join('');
      const rows = edit.week.map((row, dIdx) => {
        const cells = row.map((v, s) => {
          const t = `${DAY_LABELS[dIdx]} ${slotLabel(s)} — ${v ? 'allowed' : 'blocked'}`;
          return `<div class="cell${v ? ' onc' : ''}${s % 2 ? ' he' : ''}" data-k="${esc(key)}" data-d="${dIdx}" data-h="${s}"
                    title="${esc(t)}" style="${v ? `background:${color};` : ''}"></div>`;
        }).join('');
        return `<div class="dlab${dIdx === todayRow ? ' today' : ''}">${DAY_LABELS[dIdx]}</div>${cells}`;
      }).join('');
      const stateTxt = edit.dirty
        ? 'Unsaved changes'
        : (edit.hasStored ? 'Weekly schedule active' : 'Using default (same hours every day)');
      const btns = [
        edit.dirty ? `<button class="btn primary" data-act="save" data-k="${esc(key)}">Save</button>` : '',
        edit.dirty ? `<button class="btn" data-act="discard" data-k="${esc(key)}">Discard</button>` : '',
        (edit.hasStored && !edit.dirty)
          ? `<button class="btn linkish" data-act="reset" data-k="${esc(key)}">Revert to default</button>` : '',
      ].join('');
      return `
        <div class="dev">
          <div class="nm"><span class="dot" style="background:${color}"></span>${esc(d.name || key)}</div>
          <div class="gridwrap"><div class="sched">${hourLabels}${rows}</div></div>
          <div class="ft"><span class="state">${stateTxt}</span>${btns}</div>
        </div>`;
    }).join('');

    body.innerHTML = header + blocks +
      `<div class="hint">Blocked hours are never scheduled by the optimizer. For an immediate
       stop/start of a load that is under Grid Lens control, use the override buttons on the
       Deferrable Loads card instead.</div>`;

    body.querySelectorAll('.cell').forEach((el) => {
      el.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        // Touch/pen implicitly capture the pointer on the down-target, which would stop
        // pointerenter firing on neighbouring cells and break drag-painting — release it.
        try { el.releasePointerCapture(e.pointerId); } catch (_) { /* mouse: not captured */ }
        const k = el.getAttribute('data-k');
        const dIdx = +el.getAttribute('data-d'); const h = +el.getAttribute('data-h');
        const edit = this._edit[k];
        const value = edit.week[dIdx][h] ? 0 : 1;
        this._painting = { key: k, value };
        this._setCell(k, dIdx, h, value);
      });
      el.addEventListener('pointerenter', (e) => {
        if (!this._painting || !(e.buttons & 1)) return;
        const k = el.getAttribute('data-k');
        if (k !== this._painting.key) return;
        this._setCell(k, +el.getAttribute('data-d'), +el.getAttribute('data-h'), this._painting.value);
      });
    });
    body.querySelectorAll('.btn').forEach((el) => {
      el.addEventListener('click', () => this._action(el.getAttribute('data-act'), el.getAttribute('data-k')));
    });
  }

  _setCell(key, dIdx, h, value) {
    const edit = this._edit[key];
    if (!edit || edit.week[dIdx][h] === value) return;
    edit.week[dIdx][h] = value;
    edit.dirty = !weeksEqual(edit.week, edit.baseline);
    this._sig = ''; // force repaint on next hass set, and repaint now
    this._paint();
  }

  _action(act, key) {
    const edit = this._edit[key];
    if (!edit) return;
    if (act === 'save') {
      this._hass.callService('grid_lens', 'set_deferrable_schedule', {
        sensor_id: key, days: edit.week.map((r) => r.slice()),
      });
      edit.baseline = copyWeek(edit.week);
      edit.hasStored = true;
      edit.dirty = false;
      edit.pinned = true; // authoritative until the sensor attribute confirms the save
    } else if (act === 'discard') {
      edit.week = copyWeek(edit.baseline);
      edit.dirty = false;
    } else if (act === 'reset') {
      const dev = (this._devices || []).find((d) => d.energy_entity === key);
      const def = dev && dev.default_schedule ? dev.default_schedule : edit.week;
      this._hass.callService('grid_lens', 'clear_deferrable_schedule', { sensor_id: key });
      edit.week = copyWeek(def);
      edit.baseline = copyWeek(def);
      edit.hasStored = false;
      edit.dirty = false;
      edit.pinned = true; // authoritative until the sensor attribute confirms the clear
    }
    this._sig = '';
    this._paint();
  }
}

customElements.define('grid-lens-defer-schedule-card', GridLensDeferScheduleCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-defer-schedule-card',
  name: 'Grid Lens Deferrable Schedule',
  description: 'Weekly gantt-style editor of when each deferrable load is allowed to run.',
});
