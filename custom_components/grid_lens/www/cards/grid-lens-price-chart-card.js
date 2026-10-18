/*
 * Grid Lens Price Chart Card — standalone (split out of grid-lens-advisory-card).
 * Buy/sell rate ($/kWh) for the current plan. No history fetch needed — rates are
 * plan data, not measured: the forward half comes from the dispatch sensor's
 * `trajectory`, the elapsed-today half from its `past_rates` attribute (both are
 * the plan's own tariff structure, so they join seamlessly at "now").
 *
 * Also overlays what each 30-min slot cost and earned, as bars: cost rising from the
 * bottom edge, feed-in credit hanging from the top. For the elapsed part of today
 * those are ACTUAL figures — metered import/export priced against the plan (see
 * advisory/today_cash.py) — and for the forward part they're the LP's own planned
 * cost/credit, drawn faintly to keep the distinction visible. Tick marks (no values)
 * mark the bar scale on the right-hand edge; the crosshair tooltip has the figures.
 *
 * Config:
 *   type: custom:grid-lens-price-chart-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch   (required)
 *   show_cash_bars: true     (optional, default true — false = rates only, as before)
 */
import { GridLensChartCardBase, multiLineChart, fmtHour, fmtC, RIGHT_AXIS_MR } from './grid-lens-chart-common.js?v=20261018a';

class GridLensPriceChartCard extends GridLensChartCardBase {
  get title() { return this._cashBars() ? 'Price ($/kWh) & today\'s cost / credit' : 'Price ($/kWh)'; }

  // Opt-out rather than opt-in: an install with no grid export sensor configured
  // simply has no credit bars to draw (and no cost bars either, if it also has no
  // import sensor), so the overlay costs nothing when the data isn't there — see
  // _barRows(), which drops the bars entirely when no row carries a figure.
  _cashBars() { return this._config.show_cash_bars !== false; }
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
    const lines = `
      <span><i style="border-top:2px solid var(--buy)"></i>Buy rate</span>
      <span><i style="border-top:2px solid var(--sell)"></i>Sell rate</span>
    `;
    if (!this._cashBars()) return lines;
    // The swatches are filled blocks, not the 2px rules the rate lines use, so the
    // legend itself says which entries are the bar overlay and which are the lines.
    // "Actual / planned" is called out once rather than per entry: both bar
    // directions use the same solid-vs-faint convention.
    const t = this._todayTotals();
    // Day-to-date totals, actual rows only. The bars answer "when" and this answers
    // "how much so far" — without it the overlay can only be read comparatively,
    // since it deliberately prints no axis values.
    const totals = t ? `
      <span style="color:var(--ink)">Today so far:
        <b style="color:var(--buy)">${this._money(t.cost)}</b> cost ·
        <b style="color:var(--sell)">${this._money(t.credit)}</b> credit ·
        net <b>${t.net < 0 ? '+' + this._money(-t.net) : this._money(t.net)}</b></span>
    ` : '';
    return lines + `
      <span><i style="height:8px;background:var(--buy);opacity:0.55;border:0"></i>Cost (from bottom)</span>
      <span><i style="height:8px;background:var(--sell);opacity:0.55;border:0"></i>Credit (from top)</span>
      <span style="color:var(--muted)">solid = actual · faint = planned</span>
    ` + totals;
  }

  _money(v) { return '$' + Math.abs(v).toFixed(2); }

  // Cost/credit summed over the METERED rows only (`actual`), so this is today's real
  // spend to date and never the optimizer's view of it — the distinction CLAUDE.md's
  // "Current-plan cost" rule exists to protect. Null when nothing is metered yet
  // (before the first slot of the day closes, or no energy sensor configured), rather
  // than a misleading $0.00.
  _todayTotals() {
    const rows = this._pastRates().filter((r) => r.actual);
    if (!rows.length) return null;
    let cost = 0, credit = 0;
    for (const r of rows) { cost += +r.cost || 0; credit += +r.credit || 0; }
    return { cost, credit, net: cost - credit };
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

  // Rows carrying a per-slot dollar figure, or null when none do — a restored/cached
  // plan, an older integration, or an install with neither grid energy sensor
  // configured. Null means "draw no overlay at all", which is not the same as an
  // overlay of zeroes: a day with genuinely no trade should still show its empty
  // scale, whereas missing data must not imply a free day.
  _barRows() {
    if (!this._cashBars()) return null;
    const rows = this._combinedTraj();
    const any = rows.some((r) => (+r.cost || 0) !== 0 || (+r.credit || 0) !== 0);
    return any ? rows : null;
  }

  _chartSvg() {
    const barRows = this._barRows();
    return multiLineChart(this._combinedTraj(), this._timeScale(), [
      { key: 'import_rate', color: 'var(--buy)', step: true },
      { key: 'export_rate', color: 'var(--sell)', step: true },
    ], {
      fmt: (v) => v.toFixed(2),
      mr: RIGHT_AXIS_MR,
      // Cost shares the buy line's colour and credit the sell line's, because each bar
      // is literally that rate times the energy it moved — the pairing is the point,
      // and a fourth and fifth hue here would read as two more unrelated series.
      bars: barRows ? {
        rows: barRows,
        up: { key: 'cost', color: 'var(--buy)' },
        down: { key: 'credit', color: 'var(--sell)' },
      } : null,
    });
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
    let out = `<b>${fmtHour(bestMs)}</b>` +
      `<div><span class="k" style="color:var(--buy)">buy</span> ${fmtC(best.import_rate)}/kWh · <span class="k" style="color:var(--sell)">sell</span> ${fmtC(best.export_rate)}/kWh</div>`;
    // The bars carry no axis labels by design, so this is the only place the actual
    // dollars appear — hence kWh alongside them, and an explicit actual/planned word:
    // a tooltip that didn't say which it was reporting would make the two
    // indistinguishable the moment you stop looking at the bar's opacity.
    if (this._cashBars() && this._barRows()) {
      const cost = +best.cost || 0, credit = +best.credit || 0;
      const buy = +best.buy_kwh || 0, sell = +best.sell_kwh || 0;
      const money = (v) => '$' + v.toFixed(2);
      out += `<div><span class="k" style="color:var(--buy)">cost</span> ${money(cost)}`
        + (buy ? ` <span style="color:var(--muted)">(${buy.toFixed(2)} kWh)</span>` : '')
        + ` · <span class="k" style="color:var(--sell)">credit</span> ${money(credit)}`
        + (sell ? ` <span style="color:var(--muted)">(${sell.toFixed(2)} kWh)</span>` : '')
        + `</div>`;
      const net = cost - credit;
      out += `<div style="font-size:11px;color:var(--muted)">`
        + `net ${net < 0 ? '+' + money(-net) : money(net)} · ${best.actual ? 'actual' : 'planned'}</div>`;
    }
    return out;
  }
}

customElements.define('grid-lens-price-chart-card', GridLensPriceChartCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-price-chart-card',
  name: 'Grid Lens Price Chart',
  description: "Buy/sell rate ($/kWh) for the current plan — elapsed today plus the forecast — with today's cost and feed-in credit per 30 minutes.",
});
