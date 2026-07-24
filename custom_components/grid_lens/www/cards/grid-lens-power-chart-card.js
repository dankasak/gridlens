/*
 * Grid Lens Power Chart Card — standalone (split out of grid-lens-advisory-card).
 * Measured & forecast solar/load/buy/sell (+ deferrable device) power, in kW.
 *
 * Config:
 *   type: custom:grid-lens-power-chart-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch   (required)
 *   solar_power_entity, load_power_entity, grid_power_entity   (optional, have defaults)
 *   max_height: 420   // fixed height (px) for the chart; set 0/null for natural (aspect-ratio) height
 *   max_width: null   // cap (px) on how wide the card grows; set 0/null to fill its container
 */
import {
  GridLensChartCardBase, multiLineChart, esc, fmtHour,
} from './grid-lens-chart-common.js?v=20260724c';

class GridLensPowerChartCard extends GridLensChartCardBase {
  get title() { return 'Power — measured & forecast (kW)'; }
  get wantsEnergyHistory() { return true; }

  // Caps how tall this card can grow — paired with Power Flow's own max_height so
  // neither one is happy to fill most of the screen on a wide viewport.
  // An explicit height (not max-height on an auto-height, aspect-locked SVG — that
  // combination is unreliable across browsers and was observed not to shrink the
  // rendered box at all). The viewBox still scales/letterboxes to fit via the
  // default preserveAspectRatio, so the chart itself isn't distorted or cropped.
  get extraStyle() {
    const maxH = this._config.max_height != null ? this._config.max_height : 420;
    const maxW = this._config.max_width;
    // max-width (not an explicit width) on the plain .card div so it still shrinks
    // responsively on narrow/mobile screens — only caps growth on a big desktop.
    return (maxH ? `.chart-svg { height: ${maxH}px; }` : '')
      + (maxW ? ` .card { max-width: ${maxW}px; }` : '');
  }

  _deferColor(i) { return `var(--defer${(i % 4) + 1})`; }

  _legendHtml() {
    const dnames = this._deferNames || [];
    const deferLegend = dnames.map((nm, i) =>
      `<span><i style="border-top:2px dashed ${this._deferColor(i)}"></i>${esc(nm)}</span>`).join('');
    return `
      <span><span class="swatch" style="background:var(--solar)"></span>Solar</span>
      <span><span class="swatch" style="background:var(--load)"></span>Load</span>
      <span><span class="swatch" style="background:var(--buy)"></span>Buy (import)</span>
      <span><span class="swatch" style="background:var(--sell)"></span>Sell (export)</span>
      ${deferLegend}
      <span style="color:var(--muted)">— thin line left of "now" = measured</span>
    `;
  }

  _energySeries() {
    const dnames = this._deferNames || [];
    const kwScale = 3600000 / this._timeScale().step;
    return {
      kwScale,
      series: [
        { key: 'solar_kwh', color: 'var(--solar)', area: true, scale: kwScale },
        { key: 'load_kwh', color: 'var(--load)', scale: kwScale },
        { key: 'buy_kwh', color: 'var(--buy)', scale: kwScale },
        { key: 'sell_kwh', color: 'var(--sell)', scale: kwScale },
        ...dnames.map((nm, i) => ({ key: `defer_${i}`, color: this._deferColor(i), dash: true, scale: kwScale })),
        { points: this._actualEnergy.solar, color: 'var(--solar)', actual: true },
        { points: this._actualEnergy.load, color: 'var(--load)', actual: true },
        { points: this._actualEnergy.buy, color: 'var(--buy)', actual: true },
        { points: this._actualEnergy.sell, color: 'var(--sell)', actual: true },
      ],
    };
  }

  _chartSvg() {
    const { series } = this._energySeries();
    // Taller than the other line charts (default 160) so this pairs visually with the
    // Power Flow card next to it in the dashboard section, which is roughly square.
    return multiLineChart(this._traj, this._timeScale(), series, { fmt: (v) => v.toFixed(1), height: 480 });
  }

  _nearest(points, bestMs) {
    if (!points || !points.length) return null;
    let d2 = Infinity, val = null;
    for (const p of points) { const d = Math.abs(p.t.getTime() - bestMs); if (d < d2) { d2 = d; val = (d < 5400000) ? p.v : null; } }
    return val;
  }

  _tooltipHtml(bestMs, best, isHistory) {
    const actualSolar = this._nearest(this._actualEnergy.solar, bestMs);
    const actualLoad = this._nearest(this._actualEnergy.load, bestMs);
    const actualBuy = this._nearest(this._actualEnergy.buy, bestMs);
    const actualSell = this._nearest(this._actualEnergy.sell, bestMs);

    if (isHistory && !best) {
      if (actualSolar == null && actualLoad == null && actualBuy == null && actualSell == null) {
        return `<b>${fmtHour(bestMs)}</b><div style="font-size:11px;color:var(--muted)">No data available</div>`;
      }
      return `<b>${fmtHour(bestMs)}</b>` +
        `<div><span class="k" style="color:var(--solar)">sun</span> ${(actualSolar || 0).toFixed(2)} · <span class="k" style="color:var(--load)">load</span> ${(actualLoad || 0).toFixed(2)} kW</div>` +
        `<div><span class="k" style="color:var(--buy)">buy</span> ${(actualBuy || 0).toFixed(2)} · <span class="k" style="color:var(--sell)">sell</span> ${(actualSell || 0).toFixed(2)} kW</div>` +
        `<div style="font-size:10px;color:var(--muted);margin-top:4px">Historical data only (no forecast)</div>`;
    }
    if (!best) return `<b>${fmtHour(bestMs)}</b><div style="font-size:11px;color:var(--muted)">No data available</div>`;
    const { kwScale } = this._energySeries();
    return `<b>${fmtHour(bestMs)}</b>` +
      `<div><span class="k" style="color:var(--solar)">sun</span> ${((actualSolar != null ? actualSolar : (+best.solar_kwh || 0) * kwScale)).toFixed(2)} · <span class="k" style="color:var(--load)">load</span> ${((actualLoad != null ? actualLoad : (+best.load_kwh || 0) * kwScale)).toFixed(2)} kW</div>` +
      `<div><span class="k" style="color:var(--buy)">buy</span> ${((actualBuy != null ? actualBuy : (+best.buy_kwh || 0) * kwScale)).toFixed(2)} · <span class="k" style="color:var(--sell)">sell</span> ${((actualSell != null ? actualSell : (+best.sell_kwh || 0) * kwScale)).toFixed(2)} kW</div>` +
      (this._deferNames || []).map((nm, i) => {
        const v = (+best['defer_' + i] || 0) * kwScale;
        return v > 0.01 ? `<div><span class="k" style="color:${this._deferColor(i)}">${esc(nm)}</span> ${v.toFixed(2)} kW</div>` : '';
      }).join('');
  }
}

customElements.define('grid-lens-power-chart-card', GridLensPowerChartCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-power-chart-card',
  name: 'Grid Lens Power Chart',
  description: 'Measured & forecast solar/load/buy/sell power (kW).',
});
