/*
 * Grid Lens Power Chart Card — standalone (split out of grid-lens-advisory-card).
 * Measured & forecast solar/load/grid/battery (+ deferrable device) power, in kW. Grid and
 * battery are signed (+import/-export, +charge/-discharge) and share the chart's one
 * y-axis, forced symmetric around 0 (see _chartSvg()) so 0 sits at the vertical centre.
 *
 * Config:
 *   type: custom:grid-lens-power-chart-card
 *   entity: sensor.roof_grid_lens_nsw_planned_dispatch   (required)
 *   solar_power_entity, load_power_entity, grid_power_entity, battery_power_entity   (optional, have defaults)
 *   max_height: 420   // fixed height (px) for the chart; set 0/null for natural (aspect-ratio) height
 *   max_width: null   // cap (px) on how wide the card grows; set 0/null to fill its container
 */
import {
  GridLensChartCardBase, multiLineChart, esc, fmtHour, deferColorFor,
} from './grid-lens-chart-common.js?v=20260731a';

class GridLensPowerChartCard extends GridLensChartCardBase {
  get title() { return 'Power — measured & forecast (kW)'; }
  get wantsEnergyHistory() { return true; }

  // Caps how tall this card can grow — paired with Power Flow's own max_height so
  // neither one is happy to fill most of the screen on a wide viewport.
  // An explicit height (not max-height on an auto-height, aspect-locked SVG — that
  // combination is unreliable across browsers and was observed not to shrink the
  // rendered box at all). multiLineChart() renders with preserveAspectRatio="none",
  // so the plot stretches to exactly fill this fixed box instead of letterboxing.
  get extraStyle() {
    const maxH = this._config.max_height != null ? this._config.max_height : 420;
    const maxW = this._config.max_width;
    // max-width (not an explicit width) on the plain .card div so it still shrinks
    // responsively on narrow/mobile screens — only caps growth on a big desktop.
    // Tighter padding/legend spacing than the shared default (same font sizes, just
    // less whitespace around them) so more of the fixed max_height above goes to the
    // chart itself rather than to the header/legend chrome above it.
    return (maxH ? `.chart-svg { height: ${maxH}px; }` : '')
      + (maxW ? ` .card { max-width: ${maxW}px; }` : '')
      + ` .card { padding: 10px 16px; } .legend { margin: 0 0 4px; gap: 10px; }`;
  }

  // Matches the Power Flow card's own per-device colour assignment (a hot-water device gets
  // a dedicated colour pulled out of the rotation) — see deferColorFor() in chart-common.js.
  _deferColor(i) { return deferColorFor(this._deferNames, i); }

  // Real power sensors for the configured deferrable devices, keyed by each device's
  // configured energy entity_id (matches GridLensChartCardBase._deferSensorIds — the
  // dispatch sensor's own per-device join key). Sourced from the `deferrable_loads`
  // attribute the integration publishes (energy_entity + auto-discovered power_entity) —
  // same auto-discovery pattern as _resolveDeferLoads() in grid-lens-powerflow-card.js,
  // scanning for any sensor exposing it rather than assuming a fixed entity id.
  //
  // Deliberately NOT keyed by name: both this attribute's `name` and the dispatch sensor's
  // deferrable_names now go through the same resolve_device_name priority (entity-registry
  // name / Energy Dashboard rename / trimmed suffixes), so they'll usually agree — but a
  // user is still free to rename one entity and not the other, so the energy entity_id
  // (the actual join key) is the only join that's guaranteed not to silently match nothing.
  _deferPowerEntities() {
    const hass = this._hass;
    if (!hass) return {};
    let attr = null;
    const src = this._config.deferrable_source_entity;
    if (src && hass.states[src]) {
      attr = hass.states[src].attributes.deferrable_loads;
    } else {
      for (const eid of Object.keys(hass.states)) {
        if (!eid.startsWith('sensor.')) continue;
        const a = hass.states[eid].attributes;
        if (a && Array.isArray(a.deferrable_loads)) { attr = a.deferrable_loads; break; }
      }
    }
    if (!Array.isArray(attr)) return {};
    const byId = {};
    for (const d of attr) {
      if (d && d.energy_entity && d.power_entity) byId[d.energy_entity] = d.power_entity;
    }
    return byId;
  }

  _legendHtml() {
    const dnames = this._deferNames || [];
    const deferLegend = dnames.map((nm, i) =>
      `<span><i style="border-top:2px solid ${this._deferColor(i)}"></i>${esc(nm)}</span>`).join('');
    return `
      <span><span class="swatch" style="background:var(--solar)"></span>Solar</span>
      <span><span class="swatch" style="background:var(--load)"></span>Load</span>
      <span><span class="swatch" style="background:var(--gridflow)"></span>Grid (+import / -export)</span>
      <span><span class="swatch" style="background:var(--battery)"></span>Battery (+charge / -discharge)</span>
      ${deferLegend}
      <span style="color:var(--muted)">— thin = measured</span>
    `;
  }

  // Net grid flow for a trajectory row: +import / -export, one line instead of two —
  // matches the powerflow card's own grid_power_entity sign convention.
  _gridNet(row) { return (+row.buy_kwh || 0) - (+row.sell_kwh || 0); }

  _energySeries() {
    const dnames = this._deferNames || [];
    const kwScale = 3600000 / this._timeScale().step;
    const actualDefer = this._actualEnergy.defer || [];
    return {
      kwScale,
      // area: true on every series, planned AND measured — each gets a gradient wash
      // down to the x-axis; multiLineChart draws the washes back-to-front by series
      // size (largest behind) so they all stay visible. A measured series' wash only
      // spans t0→now (its data boundary), so left of "now" the planned + measured
      // washes of the same colour overlap and read as a deeper tint of that colour —
      // acceptable, and the user explicitly asked for history to be filled too.
      series: [
        { key: 'solar_kwh', color: 'var(--solar)', area: true, scale: kwScale },
        { key: 'load_kwh', color: 'var(--load)', area: true, scale: kwScale },
        { calc: (row) => this._gridNet(row), color: 'var(--gridflow)', area: true, scale: kwScale },
        { key: 'battery_kwh', color: 'var(--battery)', area: true, scale: kwScale },
        // step: true — a deferrable device's planned power is piecewise-constant (off, or on
        // at ~max_kw for a slot), not a smooth ramp. Without it smoothPath's cubic spline
        // curves gradually up from 0 toward the turn-on slot instead of holding flat at 0
        // until the device actually switches on. See stepPath()'s own comment.
        ...dnames.map((nm, i) => ({ key: `defer_${i}`, color: this._deferColor(i), area: true, scale: kwScale, step: true })),
        { points: this._actualEnergy.solar, color: 'var(--solar)', actual: true, area: true },
        { points: this._actualEnergy.load, color: 'var(--load)', actual: true, area: true },
        { points: this._actualEnergy.grid, color: 'var(--gridflow)', actual: true, area: true },
        { points: this._actualEnergy.battery, color: 'var(--battery)', actual: true, area: true },
        // step: true here too — a real deferrable appliance (EV charger, hot water element)
        // switches on/off in seconds on the hardware side, but HA's history API only samples
        // on significant_changes_only, so two sparse readings (last "off", first "on") would
        // otherwise get smoothPath'd into the exact same diagonal-ramp artifact as the
        // forecast series above, just drawn from real sensor data instead of planned data.
        ...dnames.map((nm, i) => ({ points: actualDefer[i], color: this._deferColor(i), actual: true, area: true, step: true })),
      ],
    };
  }

  _chartSvg() {
    const { series } = this._energySeries();
    // Taller than the other line charts (default 160) so this pairs visually with the
    // Power Flow card next to it in the dashboard section, which is roughly square.
    // symmetric: true — grid/battery are signed (import/charge positive, export/discharge
    // negative), so the y-axis is forced to [-m, m] and 0 sits at the vertical centre
    // instead of hugging the bottom the way an all-positive chart would.
    return multiLineChart(this._traj, this._timeScale(), series, { fmt: (v) => v.toFixed(1), height: 480, symmetric: true });
  }

  _nearest(points, bestMs) {
    if (!points || !points.length) return null;
    let d2 = Infinity, val = null;
    for (const p of points) { const d = Math.abs(p.t.getTime() - bestMs); if (d < d2) { d2 = d; val = (d < 5400000) ? p.v : null; } }
    return val;
  }

  // A signed flow (+import/-export, +charge/-discharge) as a colored "label value" span —
  // the label flips on sign, mirroring the powerflow card's own Importing/Exporting,
  // Charging/Discharging wording for the same entities.
  _signedRow(v, posLabel, negLabel, colorVar) {
    return `<span class="k" style="color:var(${colorVar})">${v >= 0 ? posLabel : negLabel}</span> ${Math.abs(v).toFixed(2)}`;
  }

  _tooltipHtml(bestMs, best, isHistory) {
    const actualSolar = this._nearest(this._actualEnergy.solar, bestMs);
    const actualLoad = this._nearest(this._actualEnergy.load, bestMs);
    const actualGrid = this._nearest(this._actualEnergy.grid, bestMs);
    const actualBattery = this._nearest(this._actualEnergy.battery, bestMs);
    const actualDefer = (this._actualEnergy.defer || []).map(pts => this._nearest(pts, bestMs));

    if (isHistory && !best) {
      if (actualSolar == null && actualLoad == null && actualGrid == null && actualBattery == null) {
        return `<b>${fmtHour(bestMs)}</b><div style="font-size:11px;color:var(--muted)">No data available</div>`;
      }
      const deferRows = (this._deferNames || []).map((nm, i) => {
        const v = actualDefer[i];
        return v != null && v > 0.01 ? `<div><span class="k" style="color:${this._deferColor(i)}">${esc(nm)}</span> ${v.toFixed(2)} kW</div>` : '';
      }).join('');
      return `<b>${fmtHour(bestMs)}</b>` +
        `<div><span class="k" style="color:var(--solar)">sun</span> ${(actualSolar || 0).toFixed(2)} · <span class="k" style="color:var(--load)">load</span> ${(actualLoad || 0).toFixed(2)} kW</div>` +
        `<div>${this._signedRow(actualGrid || 0, 'buy', 'sell', '--gridflow')} · ${this._signedRow(actualBattery || 0, 'charge', 'discharge', '--battery')} kW</div>` +
        deferRows +
        `<div style="font-size:10px;color:var(--muted);margin-top:4px">Historical data only (no forecast)</div>`;
    }
    if (!best) return `<b>${fmtHour(bestMs)}</b><div style="font-size:11px;color:var(--muted)">No data available</div>`;
    const { kwScale } = this._energySeries();
    const gridKw = actualGrid != null ? actualGrid : this._gridNet(best) * kwScale;
    const battKw = actualBattery != null ? actualBattery : (+best.battery_kwh || 0) * kwScale;
    return `<b>${fmtHour(bestMs)}</b>` +
      `<div><span class="k" style="color:var(--solar)">sun</span> ${((actualSolar != null ? actualSolar : (+best.solar_kwh || 0) * kwScale)).toFixed(2)} · <span class="k" style="color:var(--load)">load</span> ${((actualLoad != null ? actualLoad : (+best.load_kwh || 0) * kwScale)).toFixed(2)} kW</div>` +
      `<div>${this._signedRow(gridKw, 'buy', 'sell', '--gridflow')} · ${this._signedRow(battKw, 'charge', 'discharge', '--battery')} kW</div>` +
      (this._deferNames || []).map((nm, i) => {
        const v = actualDefer[i] != null ? actualDefer[i] : (+best['defer_' + i] || 0) * kwScale;
        return v > 0.01 ? `<div><span class="k" style="color:${this._deferColor(i)}">${esc(nm)}</span> ${v.toFixed(2)} kW</div>` : '';
      }).join('');
  }
}

customElements.define('grid-lens-power-chart-card', GridLensPowerChartCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-power-chart-card',
  name: 'Grid Lens Power Chart',
  description: 'Measured & forecast solar/load/grid/battery power (kW).',
});
