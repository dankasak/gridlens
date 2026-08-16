/*
 * Shared plumbing + pure helpers for the Grid Lens advisory chart cards
 * (grid-lens-{soc,dispatch,power,price,cash}-chart-card.js) and the slimmed
 * grid-lens-advisory-card.js status card. Not a lovelace resource itself —
 * just an ES module the registered card files import from.
 *
 * Each chart used to live inside one monolithic card sharing a single
 * crosshair; they're now independent cards (so users can place/resize them
 * with the standard Lovelace section editor), each with its own crosshair,
 * its own Today/Horizon toggle, and its own throttled history fetch (scoped
 * to only the series it actually plots — see wantsSocHistory/wantsEnergyHistory
 * on GridLensChartCardBase).
 */

export const GW = 720, GML = 44, GMR = 12;  // shared plot geometry across every chart
export const VIEW_BACK_MS = 2 * 3600000;    // show 2h of history to the left of "now"

// ---------------------------------------------------------------- pure helpers

// Downsample a {t,v} series to at most `max` points (keeps rendering light). Buckets by
// TIME, not by raw array index: index-based striding assumes points are evenly spaced, but
// real HA history isn't — a power sensor updates near-continuously while a device is active
// and barely at all while idle. An index stride sized for the dense (active) stretch can
// step clean over an entire idle stretch, including the transition point where the value
// actually drops back to zero — leaving a step-drawn series holding its last "on" reading
// all the way out to wherever the stride next happens to land, however much later that is.
// Found via a real EV charger: 20 minutes of genuine ~1.8kW charging, stride-sampled down to
// where the off-transition fell between two kept indices, then held "on" for the following
// ~3 idle hours until the next kept (and coincidentally also off) sample. Time buckets can't
// do that — each bucket is a fixed wall-clock slice, so the transition survives in whichever
// bucket it actually falls into, capping the worst-case error at one bucket width.
export function ds(pts, max = 160) {
  if (!pts || pts.length <= max) return pts || [];
  const t0 = pts[0].t.getTime(), t1 = pts[pts.length - 1].t.getTime();
  if (t1 <= t0) return [pts[0], pts[pts.length - 1]];
  const bucketMs = (t1 - t0) / max;
  const out = [];
  let bucket = -1;
  for (const p of pts) {
    const b = Math.min(max - 1, Math.floor((p.t.getTime() - t0) / bucketMs));
    if (b !== bucket) { out.push(p); bucket = b; }
    else { out[out.length - 1] = p; }
  }
  if (out[out.length - 1] !== pts[pts.length - 1]) out.push(pts[pts.length - 1]);
  return out;
}
// Step-carry-forward value of a {t,v} series (time-ascending, as returned by _series()) in
// effect at `ms` — the last point at-or-before `ms`, or 0 before the series starts. Matches
// how HA history actually behaves (a state persists until its next recorded change).
function stepValueAt(pts, ms) {
  if (!pts || !pts.length || ms < pts[0].t.getTime()) return 0;
  let v = pts[0].v;
  for (const p of pts) {
    if (p.t.getTime() > ms) break;
    v = p.v;
  }
  return v;
}
// Subtract one or more step-held series from a base series, sampled at the base series' own
// timestamps. Used to turn a measured whole-home load reading into a "general load" reading
// (whole-home minus deferrable devices) matching what the forecast side already plots —
// mirrors the server-side coordinator._subtract_deferrable_from_load, including the floor at 0
// (a noisy whole-home meter can momentarily read below a deferrable device's own reading).
export function subtractStepSeries(basePts, subtractorArrays) {
  if (!basePts || !basePts.length) return [];
  const subs = (subtractorArrays || []).filter(s => s && s.length);
  if (!subs.length) return basePts;
  return basePts.map(p => {
    const ms = p.t.getTime();
    const sub = subs.reduce((acc, s) => acc + stepValueAt(s, ms), 0);
    return { t: p.t, v: Math.max(0, p.v - sub) };
  });
}
// Catmull-Rom → cubic-bezier smoothing. pts = [[x,y],…] → SVG path 'd'.
export function smoothPath(pts) {
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
export function stepPath(pts) {
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
export function tickTimes(t0, t1, everyH = 3) {
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
export function xAxisTicks(X, t0, t1, axisY, fontSize = 10) {
  let s = '';
  for (const ms of tickTimes(t0, t1)) {
    const x = X(ms);
    s += `<line x1="${x}" y1="${axisY}" x2="${x}" y2="${axisY + 3}" stroke="var(--axis)"/>`;
    s += `<text x="${x}" y="${axisY + 14}" text-anchor="middle" font-size="${fontSize}" fill="var(--muted)">${fmtHour(ms)}</text>`;
  }
  return s;
}
// Vertical fade gradient (color → transparent) for area fills.
export function gradDef(id, color, topOpacity) {
  return `<linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">`
    + `<stop offset="0" style="stop-color:${color};stop-opacity:${topOpacity}"/>`
    + `<stop offset="1" style="stop-color:${color};stop-opacity:0.02"/></linearGradient>`;
}
export function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

// Same hot-water detection as the Power Flow card's DEFER_ICON_RULES water_heater rule
// (grid-lens-powerflow-card.js) — kept in sync manually since the two cards don't share a
// module family; if you touch one pattern, touch the other.
export const HOT_WATER_RE = /(hot[\s_-]*water|water[\s_-]*heat|\bhws\b|boiler|immersion)/i;

// Colour for the i-th deferrable device in `names` (a sensor's deferrable_names attribute),
// matching the Power Flow card's own per-device colour assignment (_bottomActors): a
// hot-water device gets the dedicated --hotwater colour pulled OUT of the --deferN rotation
// (not counted against it) instead of cycling through it like every other load, so a given
// slot index means the same device — and reads the same colour — on both cards regardless of
// where a hot-water device happens to sit in the configured list. Only checks the device's
// display name (that's all a chart/timeline has); the Power Flow card also matches against
// the power/switch entity ids, so a device named ambiguously but wired to an entity id that
// mentions "hot water" can still show mismatched colours between the two — narrower gap than
// not checking at all.
export function deferColorFor(names, i) {
  if (HOT_WATER_RE.test((names && names[i]) || '')) return 'var(--hotwater)';
  let idx = 0;
  for (let j = 0; j < i; j += 1) {
    if (!HOT_WATER_RE.test((names && names[j]) || '')) idx += 1;
  }
  return `var(--defer${(idx % 4) + 1})`;
}
export function clampPct(v) { v = parseFloat(v); return isNaN(v) ? 0 : Math.max(0, Math.min(100, v)); }
export function fmtPct(v) { v = parseFloat(v); return isNaN(v) ? '–' : v.toFixed(0) + '%'; }
export function fmtC(v) { v = parseFloat(v); return isNaN(v) ? '–' : (v * 100).toFixed(0) + 'c'; }
export function actionLabel(a) { return ({ charge: 'Charge', discharge: 'Discharge', self_use: 'Self-use', idle: 'Idle' }[a]) || (a ? esc(a) : '–'); }
// Sigenergy EMS mode a slot's plan `action` will be executed as, for the control-state timeline.
export const MODE_LABELS = {
  self_use: 'Maximum Self-consumption',
  charge: 'Command Charging (PV first)',
  discharge: 'Command Discharging (battery first)',
  idle: 'Standby',
};
export const MODE_COLORS = { self_use: 'var(--good)', charge: 'var(--charge)', discharge: 'var(--discharge)', idle: 'var(--idle)' };
export function modeLabel(a) { return MODE_LABELS[a] || (a ? esc(a) : '–'); }
export function fmtHour(ms) { const d = new Date(ms); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); }
// HH:MM plus a day qualifier ("Today"/"Tomorrow"/weekday) whenever ms falls on a
// different calendar day than `today` — the mode-timeline list has no x-axis position
// to carry date info the way the charts do, so a bare "19:00" from a 36h horizon can
// read as earlier than an earlier-listed "21:00" from the day before. Two dates never
// go further apart than the horizon (36h), so "Today"/"Tomorrow" always suffices.
export function fmtDayHour(ms, today) {
  const d = new Date(ms);
  const dayDiff = Math.round((startOfDay(d) - startOfDay(today)) / 86400000);
  const hm = fmtHour(ms);
  if (dayDiff === 0) return hm;
  if (dayDiff === 1) return `Tomorrow ${hm}`;
  return `${d.toLocaleDateString([], { weekday: 'short' })} ${hm}`;
}
export function startOfDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }
export function fmtTime(iso) { try { const d = new Date(iso); return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); } catch (e) { return ''; } }

// The EMS mode the executor will actually command for a slot. Mirrors
// ScheduleExecutor._resolve_charge: a CHARGE slot runs as Command Charging (PV first)
// when the plan wants a *material* grid top-up (a real share of the slot AND above an
// absolute floor), OR when any above-floor grid contribution falls in a genuinely FREE
// import slot (import_rate ~0) — there's no cost risk in claiming the full ceiling when
// import is free, so that case is force-charged even as a minority share. Otherwise
// (solar charge, or a trivial LP grid nibble) it runs as Maximum Self-consumption,
// which resets the charge-rate cap to hardware max and fills the battery from all
// surplus PV without importing.
//
// Symmetrically mirrors ScheduleExecutor._resolve_discharge: a DISCHARGE slot only
// runs as Command Discharging (battery first) when the plan wants a *material* export
// — a real share of the slot AND above an absolute floor. Otherwise (load-covering
// discharge, or a trivial LP nibble) it runs as self-consumption, which already
// discharges to match load with no rate forced.
//
// `stepMs` is the trajectory's slot duration — callers pass it explicitly (from their
// own _timeScale()/_stepMs()) since this is a pure function, not a class method.
export function execMode(row, stepMs) {
  if (row.action === 'charge') {
    let gw = row.grid_charge_w;
    if (gw == null) {
      const dtH = stepMs / 3600000;
      gw = Math.max(0, ((+row.buy_kwh || 0) - (+row.load_kwh || 0) - (+row.deferrable_kwh || 0))) / dtH * 1000;
    }
    if (gw <= 250) return 'self_use';
    const imp = row.import_rate != null ? +row.import_rate : null;
    if (imp != null && imp <= 1e-6) return 'charge';  // free window — claim the full ceiling
    const pw = +row.power_w || 0;
    return (gw >= 0.5 * pw) ? 'charge' : 'self_use';
  }
  if (row.action === 'discharge') {
    let ew = row.export_w;
    if (ew == null) {
      const dtH = stepMs / 3600000;
      const pw = +row.power_w || 0;
      ew = Math.min(Math.max(0, +row.sell_kwh || 0), pw * dtH / 1000) / dtH * 1000;
    }
    const pw = +row.power_w || 0;
    return (ew > 250 && ew >= 0.5 * pw) ? 'discharge' : 'self_use';
  }
  return row.action;
}

// Human-readable "why" for a row's executed mode — built entirely from numbers
// already in the trajectory. Kept in sync with the branches execMode() actually
// took so the explanation never contradicts the mode shown next to it.
export function reasonFor(row, mode) {
  const imp = row.import_rate != null ? +row.import_rate : null;
  const exp = row.export_rate != null ? +row.export_rate : null;
  const free = imp != null && imp <= 1e-6;
  const soc = clampPct(row.soc_percent);
  if (row.action === 'charge') {
    if (mode === 'charge') {
      return free
        ? `Free import window (${fmtC(imp)}/kWh) — topping up beyond solar to bank cheap energy for later.`
        : `Grid-charging at ${fmtC(imp)}/kWh — the plan judges it worth buying now rather than later.`;
    }
    const gw = row.grid_charge_w || 0;
    return gw > 1
      ? `Charging mostly from solar — the grid share is too small to force a command, so it runs on self-consumption.`
      : `Charging from solar surplus only — no grid import needed.`;
  }
  if (row.action === 'discharge') {
    if (mode === 'discharge') {
      return `Discharging to sell at ${fmtC(exp)}/kWh — a real export opportunity worth forcing.`;
    }
    return `Discharging to cover house load only — no export value here, so no rate is forced.`;
  }
  if (soc >= 99) {
    return `Battery full — holding charge, no free capacity or arbitrage value right now.`;
  }
  return `Self-consumption — solar/battery cover load directly, no material charge or discharge planned.`;
}

// Multi-series line/area chart (forecast + optional "measured" overlays), used by the
// power/price/cash chart cards. Pure function: gradient ids are derived from series
// index (not a mutable counter), since each card only ever calls this once per render.
// Each forecast series reads `row[s.key]` unless it supplies `s.calc(row)` instead (e.g.
// combining two trajectory fields into one signed net value). opts.symmetric forces the
// y-domain to [-m, m] (m = max abs of the natural range) so 0 sits at the vertical centre
// instead of wherever the data's own min/max happen to land — for a chart mixing
// positive-only series with a signed one (e.g. net grid import/export).
export function multiLineChart(traj, timeScale, series, opts = {}) {
  if (!traj || !traj.length) return '';
  const g = { w: GW, h: opts.height || 160, ml: GML, mr: GMR, mt: 10, mb: 22 };
  const { t0, t1 } = timeScale;
  const X = (ms) => g.ml + (ms - t0) / (t1 - t0) * (g.w - g.ml - g.mr);
  const nowMs = Date.now();
  // Only plot slots inside the current view window — with Today selected, t0/t1 span
  // just 24h while the trajectory itself can run 72h+, so unfiltered points map past
  // the right edge and (since the SVG doesn't clip) visibly overflow the card.
  const raw = series.map(s => {
    if (s.points) return (s.points || [])
      .filter(p => p.t.getTime() >= t0 && p.t.getTime() <= nowMs + 60000)
      .map(p => ({ ms: p.t.getTime(), v: p.v }));
    return traj
      .filter(row => { const ms = new Date(row.start).getTime(); return ms >= t0 && ms <= t1; })
      .map(row => ({ ms: new Date(row.start).getTime(), v: (s.calc ? s.calc(row) : (+row[s.key] || 0)) * (s.scale || 1) }));
  });
  let yMax = 0, yMin = opts.yMin != null ? opts.yMin : 0;
  for (const pts of raw) for (const p of pts) { if (p.v > yMax) yMax = p.v; if (p.v < yMin) yMin = p.v; }
  if (opts.symmetric) { const m = Math.max(Math.abs(yMin), Math.abs(yMax)); yMin = -m; yMax = m; }
  if (yMax === yMin) yMax = yMin + 1;
  const Y = (v) => g.mt + (1 - (v - yMin) / (yMax - yMin)) * (g.h - g.mt - g.mb);
  const fmt = opts.fmt || ((v) => v.toFixed(1));
  let grid = '';
  for (let k = 0; k <= 4; k++) {
    const v = yMin + (yMax - yMin) * k / 4, y = Y(v);
    grid += `<line x1="${g.ml}" y1="${y}" x2="${g.w - g.mr}" y2="${y}" stroke="var(--grid)"/>`;
    grid += `<text x="${g.ml - 5}" y="${y + 3}" text-anchor="end" font-size="9" fill="var(--muted)">${fmt(v)}</text>`;
  }
  // Optional shaded time bands drawn UNDER everything (grid lines included), from
  // opts.bands = [{ t0, t1, color, opacity? }] in epoch-ms. Used to mark stretches the
  // plan itself flags as special — currently "free energy the plan expects to waste"
  // (see grid-lens-power-chart-card). Clipped to the visible window and skipped when
  // they'd collapse to nothing, so a caller can hand over the whole trajectory's worth
  // of bands regardless of which view range is selected.
  let bands = '';
  for (const b of (opts.bands || [])) {
    const bs = Math.max(b.t0, t0), be = Math.min(b.t1, t1);
    if (!(be > bs)) continue;
    const x0 = X(bs), x1 = X(be);
    if (x1 - x0 < 0.5) continue;
    bands += `<rect x="${x0.toFixed(1)}" y="${g.mt}" width="${(x1 - x0).toFixed(1)}"`
      + ` height="${(g.h - g.mb - g.mt).toFixed(1)}" fill="${b.color}"`
      + ` opacity="${b.opacity != null ? b.opacity : 0.16}"/>`;
  }
  const xt = xAxisTicks(X, t0, t1, g.h - g.mb, 9);
  const nowX = X(Math.min(Date.now(), t1));
  const now = `<line x1="${nowX}" y1="${g.mt}" x2="${nowX}" y2="${g.h - g.mb}" stroke="var(--now-line)" stroke-width="1.5" stroke-dasharray="3 3" opacity="0.65"/>`;
  const zero = (yMin < 0) ? `<line x1="${g.ml}" y1="${Y(0)}" x2="${g.w - g.mr}" y2="${Y(0)}" stroke="var(--axis)"/>` : '';
  let defs = '', paths = '';
  const base = Y(Math.max(yMin, 0));
  // A forecast/planned area fill only washes the future (right of "now") — but only on
  // a chart that also draws a measured `actual` series: there, the past portion of the
  // forecast fill was stacking a second same-colour wash on top of the actual series'
  // own fill (which already only spans its own t0→now data), reading as one solid
  // continuous block even where nothing measured ran. A chart with no actual series at
  // all (e.g. the cash chart's cumulative-cost line) has nothing to show in its place,
  // so it keeps shading the full range as before. The forecast *line* itself still
  // draws full-width (unclipped) so a planned-vs-actual comparison is still possible —
  // only the fill is ever cut off.
  const hasActual = series.some((s) => s.actual);
  const futureClipId = 'futureclip';
  if (hasActual) {
    defs += `<clipPath id="${futureClipId}"><rect x="${nowX.toFixed(1)}" y="0" width="${Math.max(0, g.w - nowX).toFixed(1)}" height="${g.h}"/></clipPath>`;
  }
  // Two-pass render. Pass 1 collects each series' path geometry plus its "size"
  // (Σ|v| ≈ the area its fill would cover). Pass 2 emits gradient fills back-to-front
  // ordered LARGEST first, so a big series' wash sits behind the smaller ones and every
  // blend stays visible instead of the last-drawn fill burying the rest. Pass 3 emits
  // all line strokes in caller order, above every fill (a line must never be hidden
  // under another series' wash).
  const geo = [];
  series.forEach((s, si) => {
    const rp = raw[si];
    if (!rp.length) return;
    const pts = rp.map(p => [X(p.ms), Y(p.v)]);
    let d, rightX = pts[pts.length - 1][0];
    if (s.step) {
      d = stepPath(pts);
      // Hold the last value flat out to the chart's right edge — sensible for a forecast
      // (nothing better to show past the last planned slot) but wrong for an `actual`
      // (measured) series: its last point is simply "now", the real data boundary, and
      // extending it would draw a real sensor reading as if it continued forever into the
      // future instead of stopping where the measurement actually stops.
      if (!s.actual) {
        const endX = X(t1);
        if (endX > rightX) {
          d += ` L${endX.toFixed(1)},${pts[pts.length - 1][1].toFixed(1)}`;
          rightX = endX;
        }
      }
    } else {
      d = smoothPath(pts);
    }
    geo.push({ s, si, d, pts, rightX, mag: rp.reduce((a, p) => a + Math.abs(p.v), 0) });
  });
  for (const g2 of geo.filter((x) => x.s.area).sort((a, b) => b.mag - a.mag)) {
    const gid = 'g' + g2.si;
    defs += gradDef(gid, g2.s.color, 0.42);
    const clip = (hasActual && !g2.s.actual) ? ` clip-path="url(#${futureClipId})"` : '';
    paths += `<path d="${g2.d} L${g2.rightX.toFixed(1)},${base.toFixed(1)} L${g2.pts[0][0].toFixed(1)},${base.toFixed(1)} Z" fill="url(#${gid})"${clip}/>`;
  }
  for (const { s, d } of geo) {
    paths += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.actual ? 1.75 : 2.5}" opacity="${s.actual ? 0.9 : 1}" stroke-linejoin="round" stroke-linecap="round" ${s.dash ? 'stroke-dasharray="5 4"' : ''}/>`;
  }
  // preserveAspectRatio="none": a line/area chart has no inherent aspect ratio to
  // protect (x is time, y is an independent unit) — stretching to exactly fill
  // whatever box CSS gives it is correct here. Without this, a caller that pins an
  // explicit CSS height (e.g. grid-lens-power-chart-card's max_height) whose aspect
  // ratio doesn't match the viewBox gets letterboxed: the default "xMidYMid meet"
  // scales uniformly and pads the mismatch as empty space above/below the plot.
  return `<svg viewBox="0 0 ${g.w} ${g.h}" preserveAspectRatio="none" class="chart-svg" role="img"><defs>${defs}</defs>${bands}${grid}${zero}${xt}${now}${paths}`
    + `<line class="xhair" x1="0" x2="0" y1="${g.mt}" y2="${g.h - g.mb}" stroke="var(--ink2)" stroke-width="1" opacity="0"/></svg>`;
}

// Shared inner CSS (no <style> wrapper) — imported by both the chart-card base class
// and the standalone advisory-status card so both look consistent without depending on
// class inheritance to share styling.
export const STYLE = `
  :host {
    --surface: var(--card-background-color);
    --plane: var(--secondary-background-color);
    --ink: var(--primary-text-color);
    --ink2: var(--secondary-text-color);
    --muted: var(--disabled-text-color, var(--secondary-text-color));
    --grid: var(--divider-color);
    --axis: var(--divider-color);
    --border: var(--divider-color);
    /* primary-text-color tracks light/dark theme (near-black on light, near-white on
       dark) so the "now" divider always contrasts against the card surface, unlike
       --axis/--divider-color which is a subtle gray that barely reads in either theme. */
    --now-line: var(--primary-text-color);
    --predicted:#2563eb; --actual:#ea580c; --charge:#2563eb; --discharge:#ea580c;
    --idle:#94918a; --fit:rgba(245,158,11,.20); --good:#059669;
    /* solar/gridflow/battery are shared with the Power Flow card's --c-solar/--c-grid/
       --c-battery (grid-lens-powerflow-card.js) so the same flow reads as the same
       colour across both cards. Re-validated as a set (solar/load/gridflow/battery,
       OKLab CVD deltaE, dataviz skill's validate_palette.js, --pairs all): all checks
       pass in light mode; dark mode passes CVD separation and the normal-vision floor
       (only the lightness-band guideline sits outside range, same as the pre-existing
       defer/battery/grid dark hues below — not a distinguishability issue). --buy/--sell
       are for the Price chart's import/export *rate* lines only (two genuinely separate
       rates, not a flow) — the power chart plots one signed net --gridflow line instead. */
    /* --load is a neutral ink (black / near-white by theme), NOT a hue — user request
       2026-07-30: the aggregate-load line's old pink-red (#db2777) clashed with the EV
       charger's --defer1 red on the power chart. A neutral is maximally distinct from
       every categorical hue, so it sits outside the validated hue rotation by design
       (same reasoning as --hotwater/--idle). No cross-card drift: the powerflow card
       has no load hue token (its Home node isn't hue-coded). */
    --solar:#9c8208; --load:#000000; --gridflow:#8b7cf6; --battery:#22c55e;
    --buy:#e11d48; --sell:#0d9488; --cum:#2563eb;
    /* defer1-4 and hotwater are the SAME hex values as the Power Flow card's own
       --c-def1-4/--c-hotwater (grid-lens-powerflow-card.js), so a device reads as the same
       colour whether you're looking at its icon on the flow diagram or its dashed line here
       — validated together via the dataviz skill (validate_palette.js, against
       solar/load/gridflow/battery, adjacent pairs): all checks pass except --hotwater's
       chroma floor (it's a deliberately desaturated "pulled out of rotation" neutral, same
       exception already relied on by --idle above — legend + tooltip name labels carry the
       identity, not colour alone). */
    --defer1:#e11d48; --defer2:#0d9488; --defer3:#7c3aed; --defer4:#ca8a04;
    --hotwater:#94a3b8;
  }
  :host(.dark) {
    --predicted:#60a5fa; --actual:#fb923c; --charge:#60a5fa; --discharge:#fb923c;
    --fit:rgba(251,191,36,.22); --good:#10b981;
    --solar:#b8960a; --load:#f1f5f9; --gridflow:#a78bfa; --battery:#4ade80;
    --buy:#fb7185; --sell:#2dd4bf; --cum:#60a5fa;
    /* --defer3 deliberately differs from the Power Flow card's own dark --c-def3 (#a78bfa) —
       that value duplicates --c-grid in that card's own dark palette (a pre-existing bug,
       fixed there too, see grid-lens-powerflow-card.js), not something to propagate here.
       Same CVD-exception profile as light mode above (lightness-band non-issue + hotwater
       chroma floor). */
    --defer1:#fb7185; --defer2:#2dd4bf; --defer3:#8b5cf6; --defer4:#facc15;
    --hotwater:#cbd5e1;
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
  .modeline { list-style:none; margin:6px 0 0; padding:0; display:flex; flex-direction:column; gap:8px; }
  .modeline li { display:flex; flex-direction:column; gap:2px; font-size:12.5px; color:var(--ink); }
  .modeline .row { display:flex; align-items:baseline; gap:7px; }
  .modeline .dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto; align-self:center; }
  .modeline .t { font-variant-numeric:tabular-nums; font-weight:650; color:var(--ink2); min-width:38px; white-space:nowrap; }
  .modeline .arrow { color:var(--muted); }
  .modeline .m { font-weight:550; }
  .modeline .reason { font-size:11px; color:var(--muted); padding-left:15px; line-height:1.35; }
  .note { font-size:11px; color:var(--muted); margin-top:8px; line-height:1.4; }
  .waiting { padding:26px 8px; text-align:center; color:var(--ink2); font-size:13px; }
  .view-btn { transition:all 0.15s ease; font-weight:600; cursor:pointer; }
  .view-btn:hover { background:color-mix(in srgb, var(--ink) 8%, transparent); }
  .view-btn.active { background:var(--surface); color:var(--good); }
`;

// ---------------------------------------------------------------- base class

// Shared plumbing for the 5 standalone chart cards. Subclasses implement:
//   get title()            -> string
//   _legendHtml()           -> string
//   _chartSvg()              -> string (must include a `.chart-svg` root and,
//                                        if it wants a crosshair, an `.xhair` line)
//   _tooltipHtml(bestMs, best, isHistory)  -> string | falsy (optional; falsy = no
//                                              floating tooltip, just the crosshair line)
//   get wantsSocHistory()    -> boolean (default false)
//   get wantsEnergyHistory() -> boolean (default false)
export class GridLensChartCardBase extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._traj = null;
    this._summary = {};
    this._actual = [];
    this._lastFetch = 0;
    this._sig = '';
    this._actualEnergy = { solar: [], load: [], grid: [], battery: [], defer: [] };
    this._deferNames = [];
    this._deferMaxKw = [];
    this._deferSensorIds = [];
    this._viewMode = 'today';
    this._dark = false;
  }

  get wantsSocHistory() { return false; }
  get wantsEnergyHistory() { return false; }

  // Hook for a wantsEnergyHistory subclass to name its deferrable devices' real power
  // sensors: { [sensor_id]: power_entity }, keyed by the device's configured energy
  // entity_id (matches _deferSensorIds, not _deferNames — see that field's comment).
  // Default = none (no deferrable devices configured, or this chart doesn't care about
  // them) — _fetchActual() below fetches nothing extra and _actualEnergy.defer stays empty.
  _deferPowerEntities() { return {}; }

  setConfig(config) {
    if (!config || !config.entity) throw new Error('Define "entity" (the planned_dispatch sensor)');
    this._config = Object.assign({
      soc_entity: 'sensor.sigen_0_plant_battery_soc',
      solar_power_entity: 'sensor.sigen_0_total_pv_power',
      load_power_entity: 'sensor.sigen_0_general_load_power',
      grid_power_entity: 'sensor.sigen_plant_grid_sensor_active_power',
      battery_power_entity: 'sensor.sigen_0_plant_battery_power',
    }, config);
    this._sig = '';
    this._renderShell();
  }

  getCardSize() { return 4; }

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
    // Join key for matching a trajectory device slot to its real power sensor — see
    // _deferPowerEntities() in grid-lens-power-chart-card.js. Not the same string as
    // deferrable_names (that's a display label; this is the configured energy entity_id).
    this._deferSensorIds = Array.isArray(a.deferrable_sensor_ids) ? a.deferrable_sensor_ids : [];

    this._summary = {
      status: a.status || st.state,
      plan_name: a.plan_name,
      reason: a.reason,
      restored: a.restored === true,
    };

    const wantsHistory = this.wantsSocHistory || this.wantsEnergyHistory;
    const socSt = hass.states[this._config.soc_entity];
    const curSoc = socSt ? parseFloat(socSt.state) : NaN;

    const now = Date.now();
    if (this._traj && wantsHistory && (now - this._lastFetch > 60000)) {
      this._lastFetch = now;
      this._fetchActual(hass, curSoc).catch(() => {});
    } else if (this.wantsSocHistory && !isNaN(curSoc)) {
      this._mergeNow(curSoc);
    }

    const sig = `${st.last_updated}|${curSoc}|${this._actual.length}`;
    if (sig !== this._sig) { this._sig = sig; this._paint(); }
  }

  async _fetchActual(hass, curSoc) {
    try {
      if (!this._traj || !this._traj.length) return;
      let start;
      if (this._viewMode === 'today') {
        const now = new Date();
        start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      } else {
        start = new Date(new Date(this._traj[0].start).getTime() - VIEW_BACK_MS);
      }
      const end = new Date();
      const c = this._config;
      const eids = [];
      if (this.wantsSocHistory) eids.push(c.soc_entity);
      const deferMap = this.wantsEnergyHistory ? (this._deferPowerEntities() || {}) : {};
      if (this.wantsEnergyHistory) eids.push(c.solar_power_entity, c.load_power_entity, c.grid_power_entity, c.battery_power_entity, ...Object.values(deferMap));
      const uniq = [...new Set(eids)].filter(Boolean);
      if (!uniq.length) return;
      const url = `history/period/${start.toISOString()}?filter_entity_id=${uniq.join(',')}` +
                  `&end_time=${encodeURIComponent(end.toISOString())}&minimal_response&significant_changes_only`;
      const res = await hass.callApi('GET', url);
      const byId = {};
      for (const arr of (res || [])) {
        if (arr && arr.length && arr[0].entity_id) byId[arr[0].entity_id] = arr;
      }
      if (this.wantsSocHistory) {
        const soc = this._series(byId[c.soc_entity]);
        if (!isNaN(curSoc)) soc.push({ t: end, v: curSoc });
        this._actual = ds(soc);
      }
      if (this.wantsEnergyHistory) {
        const toKw = (eid) => {
          const s = hass.states[eid];
          return (s && s.attributes && s.attributes.unit_of_measurement === 'W') ? 0.001 : 1;
        };
        const fS = toKw(c.solar_power_entity), fL = toKw(c.load_power_entity), fG = toKw(c.grid_power_entity);
        const fB = toKw(c.battery_power_entity);
        const grid = this._series(byId[c.grid_power_entity]);
        const loadRaw = this._series(byId[c.load_power_entity]).map(p => ({ t: p.t, v: Math.max(0, p.v * fL) }));
        // Per-device deferrable actuals, same order as _deferNames/_deferSensorIds (so they
        // share the forecast dashed lines' colours) — joined on the device's sensor_id, not
        // its display name. deferrable_names now goes through the same resolve_device_name
        // priority as the `deferrable_loads` attribute's name, so they'll usually agree, but
        // a user renaming just one of the two entities is still possible — see
        // AdvisoryResult.deferrable_sensor_ids. Devices with no resolvable power sensor
        // contribute an empty series and are simply not drawn/subtracted.
        const deferRaw = (this._deferSensorIds || []).map(sid => {
          const eid = deferMap[sid];
          if (!eid || !byId[eid]) return [];
          const fD = toKw(eid);
          return this._series(byId[eid]).map(p => ({ t: p.t, v: Math.max(0, p.v * fD) }));
        });
        this._actualEnergy = {
          solar: ds(this._series(byId[c.solar_power_entity]).map(p => ({ t: p.t, v: Math.max(0, p.v * fS) }))),
          // "Load" here means the same thing the forecast side means by it — whole-home
          // load minus deferrable devices — so the two sides of "now" read consistently.
          load: ds(subtractStepSeries(loadRaw, deferRaw)),
          // Signed, unsplit: >0 import, <0 export — one line instead of two, matching the
          // forecast side's net grid_kwh and the powerflow card's own sign convention.
          grid: ds(grid.map(p => ({ t: p.t, v: p.v * fG }))),
          // Signed: >0 charging (into battery), <0 discharging (out), same convention as
          // the powerflow card's battery_power_entity reading.
          battery: ds(this._series(byId[c.battery_power_entity]).map(p => ({ t: p.t, v: p.v * fB }))),
          defer: deferRaw.map(pts => ds(pts)),
        };
      }
      this._sig = '';
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

  _timeScale() {
    const t = this._traj;
    const planStart = new Date(t[0].start).getTime();
    const step = t.length > 1 ? (new Date(t[1].start).getTime() - planStart) : 1800000;
    let t1 = new Date(t[t.length - 1].start).getTime() + step;

    let t0;
    if (this._viewMode === 'today') {
      const now = new Date();
      const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
      t0 = midnight;
      const endOfDay = midnight + 24 * 3600000;
      t1 = Math.min(t1, endOfDay);
    } else {
      t0 = planStart - VIEW_BACK_MS;
    }
    return { t0, t1, step, planStart };
  }

  _xOf(ms) {
    const { t0, t1 } = this._timeScale();
    return GML + (ms - t0) / (t1 - t0) * (GW - GML - GMR);
  }

  // Hook for a subclass to add card-specific CSS on top of the shared STYLE block
  // (e.g. a max-height cap) without every other chart card inheriting it too.
  get extraStyle() { return ''; }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}${this.extraStyle}</style>
      <div class="card">
        <div class="hd">
          <div class="title">${esc(this.title)}</div>
          <div class="toggle-wrap"></div>
        </div>
        <div class="body"></div>
      </div>
    `;
  }

  _paint() {
    const body = this.shadowRoot && this.shadowRoot.querySelector('.body');
    const toggleWrap = this.shadowRoot && this.shadowRoot.querySelector('.toggle-wrap');
    if (!body) return;
    const s = this._summary || {};

    if (toggleWrap) toggleWrap.innerHTML = this._toggleHtml();
    this._wireViewToggle();

    if (!this._traj || s.status !== 'ok') {
      body.innerHTML = `<div class="waiting">Advisory plan not available yet${s.reason ? '<br><span class="sub">' + esc(s.reason) + '</span>' : ''}</div>`;
      return;
    }

    body.innerHTML = `
      <div class="legend">${this._legendHtml()}</div>
      <div class="charts"><div class="xtip"></div>${this._chartSvg()}</div>
    `;
    this._wireCrosshair();
    // Optional per-subclass hook — only grid-lens-power-chart-card defines this today
    // (click-a-legend-name-to-isolate-its-series). innerHTML above wipes any previously
    // wired listeners, so this has to re-run every paint, same as _wireCrosshair.
    if (this._wireLegendToggle) this._wireLegendToggle();
  }

  _toggleHtml() {
    return `
      <div style="display:flex;gap:4px;padding:2px;background:var(--plane);border:1px solid var(--border);border-radius:6px">
        <button id="toggle-today" class="view-btn ${this._viewMode === 'today' ? 'active' : ''}" style="padding:4px 10px;border:none;background:transparent;color:var(--ink);cursor:pointer;font-size:11px;border-radius:4px">Today</button>
        <button id="toggle-horizon" class="view-btn ${this._viewMode === 'horizon' ? 'active' : ''}" style="padding:4px 10px;border:none;background:transparent;color:var(--ink);cursor:pointer;font-size:11px;border-radius:4px">Full horizon</button>
      </div>`;
  }

  _wireViewToggle() {
    const todayBtn = this.shadowRoot.querySelector('#toggle-today');
    const horizonBtn = this.shadowRoot.querySelector('#toggle-horizon');
    if (!todayBtn || !horizonBtn) return;
    const switchTo = (mode) => {
      if (this._viewMode === mode) return;
      this._viewMode = mode;
      this._lastFetch = 0;
      this._sig = '';
      if (this._hass && (this.wantsSocHistory || this.wantsEnergyHistory)) {
        this._fetchActual(this._hass, parseFloat(this._hass.states[this._config.soc_entity]?.state ?? 'NaN')).catch(() => {});
      }
      this._paint();
    };
    todayBtn.addEventListener('click', () => switchTo('today'));
    horizonBtn.addEventListener('click', () => switchTo('horizon'));
  }

  _wireCrosshair() {
    const charts = this.shadowRoot.querySelector('.charts');
    if (!charts || !this._traj || !this._traj.length) return;
    const svg = this.shadowRoot.querySelector('.chart-svg');
    const xhair = this.shadowRoot.querySelector('.xhair');
    const tip = this.shadowRoot.querySelector('.xtip');
    if (!svg || !tip) return;

    const move = (ev) => {
      const { t0, t1 } = this._timeScale();
      const r = ev.currentTarget.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1,
        ((ev.clientX - r.left) / r.width * GW - GML) / (GW - GML - GMR)));
      const ms = t0 + frac * (t1 - t0);
      const trajStart = new Date(this._traj[0].start).getTime();
      const isHistory = ms < trajStart;

      let bestMs, best = null;
      if (!isHistory) {
        best = this._traj[0];
        let bd = Infinity;
        for (const s of this._traj) { const d = Math.abs(new Date(s.start).getTime() - ms); if (d < bd) { bd = d; best = s; } }
        bestMs = new Date(best.start).getTime();
      } else {
        bestMs = ms;
      }

      const bx = this._xOf(bestMs);
      if (xhair) { xhair.setAttribute('x1', bx); xhair.setAttribute('x2', bx); xhair.setAttribute('opacity', '1'); }

      const html = this._tooltipHtml ? this._tooltipHtml(bestMs, best, isHistory) : null;
      if (html) {
        tip.innerHTML = html;
        const cr = charts.getBoundingClientRect();
        const flip = (ev.clientX - cr.left) > cr.width * 0.62;
        tip.style.left = (ev.clientX - cr.left) + 'px';
        tip.style.top = (ev.clientY - cr.top) + 'px';
        tip.style.transform = flip ? 'translate(calc(-100% - 14px), -50%)' : 'translate(14px, -50%)';
        tip.style.opacity = '1';
      } else {
        tip.style.opacity = '0';
      }
    };
    const leave = () => {
      if (xhair) xhair.setAttribute('opacity', '0');
      tip.style.opacity = '0';
    };

    svg.addEventListener('mousemove', move);
    charts.addEventListener('mouseleave', leave);
  }
}
