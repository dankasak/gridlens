/*
 * Grid Lens Cash Chart Card — standalone (split out of grid-lens-advisory-card).
 * Running cumulative cost/profit ($) for the current plan's forecast. No history
 * fetch needed — this is derived entirely from the trajectory's own cost/credit
 * fields, not a measured quantity.
 *
 * Config:
 *   type: custom:grid-lens-cash-chart-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch   (required)
 */
import { GridLensChartCardBase, multiLineChart, fmtHour } from './grid-lens-chart-common.js?v=20260730f';

class GridLensCashChartCard extends GridLensChartCardBase {
  get title() { return 'Cumulative cost / profit ($)'; }

  _legendHtml() {
    return `<span><i style="border-top:2px solid var(--cum)"></i>Running net cost — below zero = ahead</span>`;
  }

  _chartSvg() {
    let cum = 0;
    for (const r of this._traj) { cum += (+r.cost || 0) - (+r.credit || 0); r._cum = cum; }
    return multiLineChart(this._traj, this._timeScale(), [{ key: '_cum', color: 'var(--cum)', area: true }],
      { fmt: (v) => '$' + v.toFixed(1) });
  }

  _tooltipHtml(bestMs, best) {
    if (!best || best._cum == null) return `<b>${fmtHour(bestMs)}</b><div style="font-size:11px;color:var(--muted)">No data available</div>`;
    const sign = best._cum < 0 ? '+$' : '$';
    return `<b>${fmtHour(bestMs)}</b>` +
      `<div><span class="k" style="color:var(--cum)">net</span> ${sign}${Math.abs(best._cum).toFixed(2)}</div>`;
  }
}

customElements.define('grid-lens-cash-chart-card', GridLensCashChartCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-cash-chart-card',
  name: 'Grid Lens Cash Chart',
  description: 'Running cumulative cost/profit ($) for the current plan.',
});
