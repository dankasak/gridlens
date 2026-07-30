/*
 * Grid Lens SOC Chart Card — standalone (split out of grid-lens-advisory-card).
 * Predicted vs measured battery SOC, forecast for the current plan.
 *
 * Config:
 *   type: custom:grid-lens-soc-chart-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch   (required)
 *   soc_entity: sensor.sigen_0_plant_battery_soc          (actual SOC, optional)
 */
import {
  GW, GML, GMR, GridLensChartCardBase,
  clampPct, esc, fmtPct, fmtC, fmtHour, smoothPath, gradDef, xAxisTicks, actionLabel, reasonFor, execMode,
} from './grid-lens-chart-common.js?v=20260730f';

class GridLensSocChartCard extends GridLensChartCardBase {
  get title() { return 'SOC — planned vs measured'; }
  get wantsSocHistory() { return true; }

  _legendHtml() {
    return `
      <span><i style="border-top:2px dashed var(--predicted)"></i>Planned (if controlled)</span>
      <span><i style="border-top:2px solid var(--actual)"></i>Measured (native EMS)</span>
      <span style="color:var(--muted)">— divergence is expected until control is enabled</span>
    `;
  }

  _chartSvg() {
    const g = { w: GW, h: 210, ml: GML, mr: GMR, mt: 10, mb: 22 };
    const { t0, t1 } = this._timeScale();
    const X = (ms) => g.ml + (ms - t0) / (t1 - t0) * (g.w - g.ml - g.mr);
    const Y = (v) => g.mt + (1 - v / 100) * (g.h - g.mt - g.mb);

    let grid = '';
    [0, 25, 50, 75, 100].forEach(v => {
      const y = Y(v);
      grid += `<line x1="${g.ml}" y1="${y}" x2="${g.w - g.mr}" y2="${y}" stroke="var(--grid)" stroke-width="1"/>`;
      grid += `<text x="${g.ml - 6}" y="${y + 3}" text-anchor="end" font-size="10" fill="var(--muted)">${v}</text>`;
    });
    const xticks = xAxisTicks(X, t0, t1, g.h - g.mb);

    // Only plot slots inside the current view window — with Today selected, t0/t1 span
    // just 24h while the trajectory itself can run 72h+, so unfiltered points map past
    // the right edge and (since the SVG doesn't clip) visibly overflow the card.
    let visible = this._traj.filter(s => {
      const ms = new Date(s.start).getTime();
      return ms >= t0 && ms <= t1;
    });
    if (!visible.length) visible = this._traj; // safety net — shouldn't happen, avoids a blank chart
    const predPts = visible.map(s => [X(new Date(s.start).getTime()), Y(clampPct(s.soc_percent))]);
    const predD = smoothPath(predPts);
    const gid = 'soc-pred';
    const base = g.h - g.mb;
    const predFill = gradDef(gid, 'var(--predicted)', 0.42)
      + `<path d="${predD} L${predPts[predPts.length - 1][0].toFixed(1)},${base} L${predPts[0][0].toFixed(1)},${base} Z" fill="url(#${gid})"/>`;
    const pred = `<path d="${predD}" fill="none" stroke="var(--predicted)" stroke-width="2.5" stroke-dasharray="5 4" stroke-linejoin="round" stroke-linecap="round"/>`;

    let actual = '';
    const ap = (this._actual || []).filter(p => p.t.getTime() >= t0 && p.t.getTime() <= Date.now() + 60000);
    if (ap.length > 1) {
      const pts = ap.map(p => [X(p.t.getTime()), Y(clampPct(p.v))]);
      actual = `<path d="${smoothPath(pts)}" fill="none" stroke="var(--actual)" stroke-width="2.75" stroke-linecap="round" stroke-linejoin="round"/>`;
    } else if (ap.length === 1) {
      actual = `<circle cx="${X(ap[0].t.getTime())}" cy="${Y(clampPct(ap[0].v))}" r="4" fill="var(--actual)"/>`;
    }

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

  _tooltipHtml(bestMs, best, isHistory) {
    let av = null;
    if (this._actual && this._actual.length) {
      let d2 = Infinity;
      for (const p of this._actual) { const d = Math.abs(p.t.getTime() - bestMs); if (d < d2) { d2 = d; av = (d < 5400000) ? p.v : null; } }
    }
    if (isHistory && !best) {
      return `<b>${fmtHour(bestMs)}</b>` +
        (av != null ? `<div><span class="k" style="color:var(--actual)">SOC</span> ${fmtPct(av)}</div>` : '') +
        `<div style="font-size:10px;color:var(--muted);margin-top:4px">Historical data only (no forecast)</div>`;
    }
    if (!best) return `<b>${fmtHour(bestMs)}</b><div style="font-size:11px;color:var(--muted)">No data available</div>`;
    const { step } = this._timeScale();
    const mode = execMode(best, step);
    return `<b>${fmtHour(bestMs)}</b>` +
      `<div><span class="k" style="color:var(--predicted)">SOC plan</span> ${fmtPct(best.soc_percent)}` +
      (av != null ? ` · <span class="k" style="color:var(--actual)">actual</span> ${fmtPct(av)}` : '') + `</div>` +
      `<div><b>${actionLabel(best.action)}</b>${best.power_w ? ' · ' + Math.round(best.power_w) + ' W' : ''}</div>` +
      `<div style="max-width:220px;white-space:normal;color:var(--ink2);font-size:10.5px;margin:1px 0 3px">${esc(reasonFor(best, mode))}</div>` +
      `<div><span class="k">rate</span> ${fmtC(best.import_rate)} in / ${fmtC(best.export_rate)} out</div>`;
  }
}

customElements.define('grid-lens-soc-chart-card', GridLensSocChartCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-soc-chart-card',
  name: 'Grid Lens SOC Chart',
  description: 'Predicted vs measured battery SOC.',
});
