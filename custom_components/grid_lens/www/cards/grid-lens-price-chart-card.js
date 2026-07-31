/*
 * Grid Lens Price Chart Card — standalone (split out of grid-lens-advisory-card).
 * Forecast buy/sell rate ($/kWh) for the current plan. No history fetch needed —
 * rates are plan data, not measured.
 *
 * Config:
 *   type: custom:grid-lens-price-chart-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch   (required)
 */
import { GridLensChartCardBase, multiLineChart, fmtHour, fmtC } from './grid-lens-chart-common.js?v=20260731a';

class GridLensPriceChartCard extends GridLensChartCardBase {
  get title() { return 'Price ($/kWh)'; }

  _legendHtml() {
    return `
      <span><i style="border-top:2px solid var(--buy)"></i>Buy rate</span>
      <span><i style="border-top:2px solid var(--sell)"></i>Sell rate</span>
    `;
  }

  _chartSvg() {
    return multiLineChart(this._traj, this._timeScale(), [
      { key: 'import_rate', color: 'var(--buy)', step: true },
      { key: 'export_rate', color: 'var(--sell)', step: true },
    ], { fmt: (v) => v.toFixed(2) });
  }

  _tooltipHtml(bestMs, best) {
    if (!best) return `<b>${fmtHour(bestMs)}</b><div style="font-size:11px;color:var(--muted)">No data available</div>`;
    return `<b>${fmtHour(bestMs)}</b>` +
      `<div><span class="k" style="color:var(--buy)">buy</span> ${fmtC(best.import_rate)}/kWh · <span class="k" style="color:var(--sell)">sell</span> ${fmtC(best.export_rate)}/kWh</div>`;
  }
}

customElements.define('grid-lens-price-chart-card', GridLensPriceChartCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-price-chart-card',
  name: 'Grid Lens Price Chart',
  description: 'Forecast buy/sell rate ($/kWh) for the current plan.',
});
