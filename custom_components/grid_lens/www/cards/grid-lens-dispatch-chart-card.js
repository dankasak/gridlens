/*
 * Grid Lens Dispatch Chart Card — standalone (split out of grid-lens-advisory-card).
 * Planned battery charge/discharge dispatch by hour.
 *
 * Config:
 *   type: custom:grid-lens-dispatch-chart-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch   (required)
 *
 * No history fetch needed — this chart only plots the forecast trajectory.
 * Keeps the existing native per-bar <title> tooltips rather than the shared
 * crosshair tooltip (returning falsy from _tooltipHtml skips the floating box,
 * the crosshair line still tracks the pointer).
 */
import {
  GW, GML, GMR, GridLensChartCardBase, xAxisTicks, fmtHour, actionLabel, fmtPct, fmtC, execMode,
} from './grid-lens-chart-common.js?v=20260830d';

class GridLensDispatchChartCard extends GridLensChartCardBase {
  get title() { return 'Planned dispatch'; }

  _legendHtml() {
    return `
      <span><span class="swatch" style="background:var(--charge)"></span>Charge</span>
      <span><span class="swatch" style="background:var(--discharge)"></span>Discharge</span>
      <span><span class="swatch" style="background:var(--idle)"></span>Self-use</span>
      <span><span class="swatch" style="background:var(--fit)"></span>Export window</span>
    `;
  }

  _chartSvg() {
    const g = { w: GW, h: 130, ml: GML, mr: GMR, mt: 8, mb: 22 };
    const { t0, t1, step } = this._timeScale();
    // Only draw slots inside the current view window — with Today selected, t0/t1 span
    // just 24h while the trajectory itself can run 72h+, so unfiltered bars land past
    // the right edge and (since the SVG doesn't clip) visibly overflow the card.
    let t = this._traj.filter(s => {
      const ms = new Date(s.start).getTime();
      return ms >= t0 && ms <= t1;
    });
    if (!t.length) t = this._traj; // safety net — shouldn't happen, avoids a blank chart
    const X = (ms) => g.ml + (ms - t0) / (t1 - t0) * (g.w - g.ml - g.mr);
    const bw = (g.w - g.ml - g.mr) * step / (t1 - t0);
    const maxP = Math.max(1000, ...t.map(s => Math.abs(s.power_w || 0)));
    const midY = g.mt + (g.h - g.mt - g.mb) / 2;
    const half = (g.h - g.mt - g.mb) / 2;

    let bars = '', shade = '';
    t.forEach((s) => {
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
      let src = '';
      if (s.action === 'charge') {
        const gw = execMode(s, step) === 'charge' ? (s.grid_charge_w || 0) : 0;
        src = gw > 1 ? ` (grid ${Math.round(gw)} W)` : ' (solar)';
      }
      bars += `<rect x="${x + gap}" y="${y}" width="${Math.max(1, bw - 2 * gap)}" height="${Math.max(2, h2)}" rx="2" fill="${color}">` +
        `<title>${fmtHour(new Date(s.start).getTime())} · ${actionLabel(s.action)} ${Math.round(Math.abs(p))} W${src} · SOC ${fmtPct(s.soc_percent)}` +
        ` · imp ${fmtC(s.import_rate)} exp ${fmtC(s.export_rate)}</title></rect>`;
    });
    let xt = xAxisTicks(X, t0, t1, g.h - g.mb);
    const nowX = X(Math.min(Date.now(), t1));
    xt += `<line x1="${nowX}" y1="${g.mt}" x2="${nowX}" y2="${g.h - g.mb}" stroke="var(--now-line)" stroke-width="1.5" stroke-dasharray="3 3" opacity="0.65"/>`;

    return `<svg viewBox="0 0 ${g.w} ${g.h}" class="chart-svg" role="img" aria-label="Planned battery dispatch by hour">
      ${shade}
      <line x1="${g.ml}" y1="${midY}" x2="${g.w - g.mr}" y2="${midY}" stroke="var(--axis)"/>
      ${bars}${xt}
      <line class="xhair" x1="0" x2="0" y1="${g.mt}" y2="${g.h - g.mb}" stroke="var(--ink2)" stroke-width="1" opacity="0"/>
      <text x="${g.ml - 6}" y="${g.mt + 8}" text-anchor="end" font-size="9" fill="var(--muted)">chg</text>
      <text x="${g.ml - 6}" y="${g.h - g.mb - 2}" text-anchor="end" font-size="9" fill="var(--muted)">dis</text>
    </svg>`;
  }

  _tooltipHtml() { return null; } // native per-bar <title> tooltips already cover this
}

customElements.define('grid-lens-dispatch-chart-card', GridLensDispatchChartCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-dispatch-chart-card',
  name: 'Grid Lens Dispatch Chart',
  description: 'Planned battery charge/discharge dispatch by hour.',
});
