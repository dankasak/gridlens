/*
 * Grid Lens Price Chart Card — standalone (split out of grid-lens-advisory-card).
 * Buy/sell rate ($/kWh) for the current plan. No history fetch needed — rates are
 * plan data, not measured: the forward half comes from the dispatch sensor's
 * `trajectory`, the elapsed-today half from its `past_rates` attribute (both are
 * the plan's own tariff structure, so they join seamlessly at "now").
 *
 * Config:
 *   type: custom:grid-lens-price-chart-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch   (required)
 */
import { GridLensChartCardBase, multiLineChart, fmtHour, fmtC, RIGHT_AXIS_MR } from './grid-lens-chart-common.js?v=20261014a';

class GridLensPriceChartCard extends GridLensChartCardBase {
  get title() { return 'Price ($/kWh)'; }
  // This card draws no right-axis series of its own, but it sits directly under
  // grid-lens-power-chart-card on the Power Flow view and both cover the same time
  // range — their X axes need to land on the SAME pixel columns for "now", the start
  // and the end, or a glance between the two reads as subtly misaligned even though
  // both are technically correct. The power chart reserves RIGHT_AXIS_MR of extra right
  // margin for its SOC axis' tick labels; matching that here (via hasRightAxis, which
  // keeps the crosshair's mouse->time math in sync — see its own comment — and via
  // opts.mr below, which keeps the actual plotted line in sync) means this chart's own
  // unused right-hand strip is simply left blank, at the exact width the power chart's
  // SOC labels occupy, rather than being GMR narrower and letting this chart's plot run
  // all the way to its own right edge instead.
  get hasRightAxis() { return true; }

  _legendHtml() {
    return `
      <span><i style="border-top:2px solid var(--buy)"></i>Buy rate</span>
      <span><i style="border-top:2px solid var(--sell)"></i>Sell rate</span>
    `;
  }

  // Elapsed-today rate slots ({start, import_rate, export_rate}) published alongside the
  // forward trajectory. Absent on a restored/cached plan or an older integration — the
  // chart then just shows the forward half, unchanged.
  _pastRates() {
    const st = this._hass && this._hass.states[this._config.entity];
    const a = (st && st.attributes) || {};
    return Array.isArray(a.past_rates) ? a.past_rates : [];
  }

  // Past slots + forward trajectory as one time-ascending array. multiLineChart only
  // reads `row.start` and `row[key]`, so the rate-only past rows slot in fine; the
  // "today" window (t0 = local midnight) keeps them in view, "horizon" clips to 2h back
  // like every other chart.
  _combinedTraj() {
    const past = this._pastRates();
    return past.length ? past.concat(this._traj || []) : (this._traj || []);
  }

  _chartSvg() {
    return multiLineChart(this._combinedTraj(), this._timeScale(), [
      { key: 'import_rate', color: 'var(--buy)', step: true },
      { key: 'export_rate', color: 'var(--sell)', step: true },
    ], { fmt: (v) => v.toFixed(2), mr: RIGHT_AXIS_MR });
  }

  // The slot covering `ms` on the elapsed-today side — the base's crosshair leaves `best`
  // null and `isHistory` true anywhere left of trajectory[0], so resolve it here.
  _pastSlotAt(ms) {
    const past = this._pastRates();
    if (!past.length) return null;
    const step = this._timeScale().step || 1800000;
    for (const r of past) {
      const t = new Date(r.start).getTime();
      if (ms >= t && ms < t + step) return { ...r, __ms: t };
    }
    return null;
  }

  _tooltipHtml(bestMs, best, isHistory) {
    if (!best && isHistory) {
      const slot = this._pastSlotAt(bestMs);
      if (slot) { best = slot; bestMs = slot.__ms; }
    }
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
  description: 'Buy/sell rate ($/kWh) for the current plan — elapsed today plus the forecast.',
});
