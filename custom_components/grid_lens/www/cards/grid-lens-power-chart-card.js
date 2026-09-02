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
  GridLensChartCardBase, multiLineChart, esc, fmtHour, deferColorFor, clampPct, fmtPct,
} from './grid-lens-chart-common.js?v=20260830d';

// Free-energy shading (see _freeEnergyBands). CSS custom props rather than literals so
// both bands follow the viewer's light/dark theme like every other colour on this card;
// the values are set in extraStyle below. Deliberately NOT reusing --solar/--gridflow:
// these are background washes behind those very lines, so they have to stay clearly
// separable from them rather than echoing them.
const FREE_SPILL = 'var(--free-spill)';
const FREE_IMPORT = 'var(--free-import)';

// GreedyEnergyTracker's sensor only emits a new recorded sample when its cumulative
// counter actually ticks up (greedy_energy.py's accumulate(), edge-triggered) — it never
// writes a periodic "still greedy" heartbeat while flat. So a big gap between two
// recorded samples (very often the synthetic "value at the start of the query window"
// row through to the first real bump hours later) means the device was greedy for a
// short stretch immediately before the second sample, not for the whole gap. Caps how
// far _fetchGreedyBands() below can extend a band backward from its end — set to
// LoadControlManager's default 5-minute tick (load_control_manager.py's
// `interval_minutes`), since greedy_reason is only re-derived that often, so no single
// step should ever need to represent more than that.
const MAX_GREEDY_STEP_MS = 5 * 60 * 1000;

class GridLensPowerChartCard extends GridLensChartCardBase {
  get title() { return 'Power — measured & forecast (kW)'; }
  get wantsEnergyHistory() { return true; }
  // Pulls measured SOC into this._actual for the right-axis overlay. Planned SOC comes
  // from the trajectory itself (soc_percent) and needs no entity, so an install whose
  // soc_entity doesn't resolve still gets the planned curve — it just loses the
  // measured one, exactly as the standalone SOC card already degrades.
  get wantsSocHistory() { return true; }

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
      + ` .card { padding: 10px 16px; } .legend { margin: 0 0 4px; gap: 10px; }`
      // Free-energy band washes. Amber-ish for wasted surplus (it's a "you're throwing
      // this away" warning) and teal for a free-import window (a good thing, and the
      // same family this project's charts already use for sell/credit).
      //
      // Re-picked 2026-08-30 (dataviz skill's validate_palette.js): the old values
      // duplicated other series on this exact chart — --free-import was the literal
      // same hex as --defer2 (a device line and a background band reading as one
      // colour in the legend), and --free-spill sat only 2.0-2.7 ΔE from --solar under
      // protanopia (both warm gold/orange), well below the 15 floor. New values clear
      // 15+ against every series this chart can show at once (solar/gridflow/battery/
      // soc/defer1-4) with margin; --free-spill's CVD separation from --solar lands in
      // the 6-8 floor band (WARN, not FAIL) — legal because the band already carries a
      // secondary encoding: a legend entry ("Free energy wasted") and a hover tooltip
      // that names it in text (_bandNote), so colour is never the only cue.
      + ` :host { --free-spill:#ff8d0a; --free-import:#0ccadf; --soc:#0284c7; }`
      + ` :host(.dark) { --free-spill:#ffcc33; --free-import:#8fffff; --soc:#38bdf8; }`
      // Click-to-isolate legend entries (see _legendItem()/_wireLegendToggle()). `.dim`
      // is opacity only, not display:none — the entry stays clickable so switching
      // isolation straight to a different series (or back to "all") is one click, not two.
      + ` .legend-item { cursor: pointer; padding: 1px 4px; margin: -1px -4px; border-radius: 4px; transition: opacity .12s ease, background .12s ease; }`
      + ` .legend-item:hover { background: color-mix(in srgb, var(--ink) 8%, transparent); }`
      + ` .legend-item.dim { opacity: .4; }`;
  }

  // Matches the Power Flow card's own per-device colour assignment (a hot-water device gets
  // a dedicated colour pulled out of the rotation) — see deferColorFor() in chart-common.js.
  _deferColor(i) { return deferColorFor(this._deferNames, i); }

  // Scans for the `deferrable_loads` attribute the integration publishes (whichever
  // sensor exposes it — same auto-discovery pattern as _resolveDeferLoads() in
  // grid-lens-powerflow-card.js — or the explicit deferrable_source_entity override).
  // Shared by _deferPowerEntities() and _deferGreedyEntities() below so both key off
  // exactly the same attribute scan.
  _deferrableLoadsAttr() {
    const hass = this._hass;
    if (!hass) return null;
    const src = this._config.deferrable_source_entity;
    if (src && hass.states[src]) {
      const a = hass.states[src].attributes.deferrable_loads;
      return Array.isArray(a) ? a : null;
    }
    for (const eid of Object.keys(hass.states)) {
      if (!eid.startsWith('sensor.')) continue;
      const a = hass.states[eid].attributes;
      if (a && Array.isArray(a.deferrable_loads)) return a.deferrable_loads;
    }
    return null;
  }

  // Real power sensors for the configured deferrable devices, keyed by each device's
  // configured energy entity_id (matches GridLensChartCardBase._deferSensorIds — the
  // dispatch sensor's own per-device join key).
  //
  // Deliberately NOT keyed by name: both this attribute's `name` and the dispatch sensor's
  // deferrable_names now go through the same resolve_device_name priority (entity-registry
  // name / Energy Dashboard rename / trimmed suffixes), so they'll usually agree — but a
  // user is still free to rename one entity and not the other, so the energy entity_id
  // (the actual join key) is the only join that's guaranteed not to silently match nothing.
  _deferPowerEntities() {
    const attr = this._deferrableLoadsAttr();
    if (!Array.isArray(attr)) return {};
    const byId = {};
    for (const d of attr) {
      if (d && d.energy_entity && d.power_entity) byId[d.energy_entity] = d.power_entity;
    }
    return byId;
  }

  // Each device's GreedyEnergyTracker sensor (cumulative kWh added only while Greedy
  // Consumption was actually driving it — see greedy_energy.py and sensor.py's
  // _build_deferrable_loads), keyed the same way as _deferPowerEntities() above. Absent
  // (falsy greedy_energy_entity) for a device with no controller — forecast-only/
  // declared loads can never be greedy-driven, so they simply never get a hatch band.
  _deferGreedyEntities() {
    const attr = this._deferrableLoadsAttr();
    if (!Array.isArray(attr)) return {};
    const byId = {};
    for (const d of attr) {
      if (d && d.energy_entity && d.greedy_energy_entity) byId[d.energy_entity] = d.greedy_energy_entity;
    }
    return byId;
  }

  // A clickable legend entry for one logical series (forecast + measured pair share a
  // `group` — see _energySeries()). Click isolates the chart to just that group; clicking
  // the already-isolated one again clears isolation and restores every series — wired in
  // _wireLegendToggle(). `.dim`'d when something else is isolated, so the isolated one
  // reads as the obvious "on" state rather than everything just looking identical.
  _legendItem(group, swatchHtml, label) {
    const dim = this._isolatedGroup && this._isolatedGroup !== group ? ' dim' : '';
    const tip = this._isolatedGroup === group ? 'Click to show every series again' : 'Click to show only this series';
    return `<span class="legend-item${dim}" data-group="${esc(group)}" tabindex="0" title="${esc(tip)}">${swatchHtml}${esc(label)}</span>`;
  }

  _legendHtml() {
    const dnames = this._deferNames || [];
    const deferLegend = dnames.map((nm, i) => this._legendItem(
      `defer_${i}`, `<i style="border-top:2px solid ${this._deferColor(i)}"></i>`, nm,
    )).join('');
    // Only advertise the free-energy shading when there actually is some in view — on a
    // plan with no $0 window and no spill the legend would otherwise carry two
    // permanently-unused entries. Filtered to the selected view range for the same
    // reason: Today and Full horizon can legitimately disagree about what's on screen.
    const bands = this._visibleBands();
    const hasSpill = bands.some((b) => b.kind === 'spill');
    const hasFree = bands.some((b) => b.kind === 'free_import');
    const hasGreedy = bands.some((b) => b.kind === 'greedy');
    const bandLegend =
      (hasSpill ? `<span><span class="swatch" style="background:${FREE_SPILL};opacity:.55"></span>Free energy wasted</span>` : '')
      + (hasFree ? `<span><span class="swatch" style="background:${FREE_IMPORT};opacity:.55"></span>Free import window</span>` : '')
      // Neutral hatch preview — the actual hatch is drawn in each device's own colour on
      // the chart, so the legend swatch is generic rather than picking one device's hue.
      + (hasGreedy ? `<span><span class="swatch" style="background-image:repeating-linear-gradient(45deg,var(--ink) 0,var(--ink) 2px,transparent 2px,transparent 4px);opacity:.65"></span>Greedy-driven (hatched)</span>` : '');
    return `
      ${this._legendItem('solar', '<span class="swatch" style="background:var(--solar)"></span>', 'Solar')}
      ${this._legendItem('load', '<span class="swatch" style="background:var(--load)"></span>', 'Load')}
      ${this._legendItem('grid', '<span class="swatch" style="background:var(--gridflow)"></span>', 'Grid (+import / -export)')}
      ${this._legendItem('battery', '<span class="swatch" style="background:var(--battery)"></span>', 'Battery (+charge / -discharge)')}
      ${deferLegend}
      ${this._legendItem('soc', '<i style="border-top:3px dashed var(--soc)"></i>', 'SOC % (right axis)')}
      ${bandLegend}
      <span style="color:var(--muted)">— thin = measured</span>
    `;
  }

  // Click-to-isolate: click a legend entry to show only its series (forecast + measured),
  // click the already-isolated one again to restore all. Re-wired every paint since
  // GridLensChartCardBase._paint() replaces .body's innerHTML wholesale each time.
  _wireLegendToggle() {
    const items = this.shadowRoot.querySelectorAll('.legend [data-group]');
    items.forEach((el) => {
      el.addEventListener('click', () => {
        const g = el.getAttribute('data-group');
        this._isolatedGroup = this._isolatedGroup === g ? null : g;
        this._paint();
      });
    });
  }

  // Stretches of the plan where energy is free and the plan does not use all of it —
  // exactly what Greedy Consumption's forecast-surplus condition sums up, drawn so a
  // deferrable load starting "for no visible reason" is legible against the spill that
  // caused it (see DeferrableLoadController's module docstring).
  //
  //   spill       — the plan exports into a $0 (or negative) export price: that energy
  //                 is being given away. sell_kwh is already net of every load the plan
  //                 schedules, so what's shaded is genuinely surplus.
  //   free_import — import costs nothing this slot (a plan's $0 window), so anything
  //                 running is running for free.
  //
  // Computed from the same trajectory rows the lines are drawn from, so it needs no
  // extra entity and stays correct for any retailer/plan.
  _visibleBands() {
    const traj = this._traj || [];
    if (!traj.length) return [];
    const { t0, t1 } = this._timeScale();
    return [...this._freeEnergyBands(), ...this._greedyBands()].filter((b) => b.t1 > t0 && b.t0 < t1);
  }

  // Stretches where a deferrable device's MEASURED consumption was actually driven by
  // Greedy Consumption rather than the plan or a manual command — computed from each
  // device's GreedyEnergyTracker sensor history (_fetchGreedyBands()), never the
  // trajectory (that's a plan/forecast concept; this is what really happened, so it's
  // drawn hatched rather than as a flat wash like the plan's own bands above — it must
  // never read as one of those even when a device's own colour happens to repeat).
  // `group` matches the device's own legend/isolation group (see _energySeries()) so
  // isolating one device's line also isolates its hatch.
  _greedyBands() {
    const dnames = this._deferNames || [];
    const byDevice = this._greedyBandsByDevice || [];
    const out = [];
    dnames.forEach((nm, i) => {
      for (const b of (byDevice[i] || [])) {
        out.push({
          t0: b.t0, t1: b.t1, kind: 'greedy', group: `defer_${i}`,
          color: this._deferColor(i), pattern: 'diagonal', opacity: 0.5,
        });
      }
    });
    return out;
  }

  _freeEnergyBands() {
    const traj = this._traj || [];
    if (!traj.length) return [];
    const step = this._timeScale().step;
    const out = [];
    traj.forEach((row, i) => {
      const t0 = new Date(row.start).getTime();
      const t1 = i + 1 < traj.length ? new Date(traj[i + 1].start).getTime() : t0 + step;
      const imp = row.import_rate != null ? +row.import_rate : null;
      const exp = row.export_rate != null ? +row.export_rate : null;
      if (imp != null && imp <= 1e-6) {
        out.push({ t0, t1, kind: 'free_import', color: FREE_IMPORT, opacity: 0.14 });
      } else if (exp != null && exp <= 1e-6 && (+row.sell_kwh || 0) > 1e-6) {
        // `else if` so a slot that is both free to import and spilling is shaded once —
        // free import is the stronger statement and wins.
        out.push({ t0, t1, kind: 'spill', color: FREE_SPILL, opacity: 0.16 });
      }
    });
    // Merge touching same-kind slots so a 5-hour spill is one rect, not 10 abutting ones
    // whose translucent edges would otherwise band visibly against each other.
    const merged = [];
    for (const b of out) {
      const prev = merged[merged.length - 1];
      if (prev && prev.kind === b.kind && Math.abs(prev.t1 - b.t0) < 1000) prev.t1 = b.t1;
      else merged.push({ ...b });
    }
    return merged;
  }

  // Net grid flow for a trajectory row: +import / -export, one line instead of two —
  // matches the powerflow card's own grid_power_entity sign convention.
  _gridNet(row) { return (+row.buy_kwh || 0) - (+row.sell_kwh || 0); }

  // Rides GridLensChartCardBase's own throttled fetch cadence (60s, gated on
  // wantsEnergyHistory/wantsSocHistory) — the greedy hatch is refreshed alongside every
  // other "actual" series rather than on its own timer.
  async _fetchActual(hass, curSoc) {
    await super._fetchActual(hass, curSoc);
    await this._fetchGreedyBands(hass).catch(() => {});
  }

  // Per-device stretches where Greedy Consumption actually drove the device, read from
  // each device's GreedyEnergyTracker sensor (a cumulative kWh counter that only ever
  // rises while greedy_reason was truthy — see greedy_energy.py's accumulate()). HA
  // history only tells us WHEN the counter ticked up, not the underlying power sample's
  // own resolution, so an increase between two consecutive samples is treated as genuine
  // greedy consumption ending at the later sample — but, unlike _series()/stepValueAt()'s
  // "holds until the next recorded change" convention elsewhere in this file, NOT starting
  // all the way back at the earlier one: this sensor is edge-triggered (a new sample only
  // ever appears when the counter actually moves, never a periodic "still greedy"
  // heartbeat), so a big gap between two samples is almost always a long quiet
  // (non-greedy) stretch followed by a short greedy burst right before the later sample,
  // not hours of continuous greedy. Confirmed against real data 2026-09-02: a single
  // 0.02kWh bump at 14:30 after a flat overnight baseline was rendering as one band from
  // midnight to 14:30, hatching an entire plan-driven charging session as "greedy". Each
  // step's band is capped to MAX_GREEDY_STEP_MS ending at the later sample — see that
  // constant's own comment. Populates this._greedyBandsByDevice (parallel to
  // this._deferNames), one array of {t0,t1} per device.
  async _fetchGreedyBands(hass) {
    const greedyMap = this._deferGreedyEntities();
    const sids = this._deferSensorIds || [];
    const eids = sids.map((sid) => greedyMap[sid]).filter(Boolean);
    if (!eids.length) { this._greedyBandsByDevice = []; return; }
    let start;
    if (this._viewMode === 'today') {
      const now = new Date();
      start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    } else {
      start = new Date(new Date(this._traj[0].start).getTime() - VIEW_BACK_MS);
    }
    const end = new Date();
    const uniq = [...new Set(eids)];
    const url = `history/period/${start.toISOString()}?filter_entity_id=${uniq.join(',')}`
      + `&end_time=${encodeURIComponent(end.toISOString())}&minimal_response&significant_changes_only`;
    const res = await hass.callApi('GET', url);
    const byId = {};
    for (const arr of (res || [])) {
      if (arr && arr.length && arr[0].entity_id) byId[arr[0].entity_id] = arr;
    }
    this._greedyBandsByDevice = sids.map((sid) => {
      const eid = greedyMap[sid];
      const rows = eid ? byId[eid] : null;
      if (!rows || rows.length < 2) return [];
      const pts = rows
        .map((r) => ({ t: new Date(r.last_changed || r.lu).getTime(), v: parseFloat(r.state) }))
        .filter((p) => !isNaN(p.v))
        .sort((a, b) => a.t - b.t);
      const out = [];
      for (let i = 1; i < pts.length; i++) {
        if (pts[i].v > pts[i - 1].v + 1e-6) {
          out.push({ t0: Math.max(pts[i - 1].t, pts[i].t - MAX_GREEDY_STEP_MS), t1: pts[i].t });
        }
      }
      // A device that's been continuously greedy for hours reports a state change
      // roughly every minute, which would otherwise produce hundreds of abutting
      // 1-minute rects (and as many redundant pattern defs) instead of one clean band —
      // merge touching intervals, same as _freeEnergyBands()'s own same-kind merge.
      const merged = [];
      for (const b of out) {
        const prev = merged[merged.length - 1];
        if (prev && Math.abs(prev.t1 - b.t0) < 1000) prev.t1 = b.t1;
        else merged.push({ ...b });
      }
      return merged;
    });
    this._sig = '';
    this._paint();
  }

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
      // `group` pairs each series with its legend entry (forecast + measured share one) —
      // see _legendItem()/_wireLegendToggle(). Purely a client-side tag, multiLineChart
      // never looks at it; _chartSvg() filters the array down to one group before handing
      // it off when a legend entry is isolated.
      series: [
        { key: 'solar_kwh', group: 'solar', color: 'var(--solar)', area: true, scale: kwScale },
        { key: 'load_kwh', group: 'load', color: 'var(--load)', area: true, scale: kwScale },
        { calc: (row) => this._gridNet(row), group: 'grid', color: 'var(--gridflow)', area: true, scale: kwScale },
        { key: 'battery_kwh', group: 'battery', color: 'var(--battery)', area: true, scale: kwScale },
        // step: true — a deferrable device's planned power is piecewise-constant (off, or on
        // at ~max_kw for a slot), not a smooth ramp. Without it smoothPath's cubic spline
        // curves gradually up from 0 toward the turn-on slot instead of holding flat at 0
        // until the device actually switches on. See stepPath()'s own comment.
        ...dnames.map((nm, i) => ({ key: `defer_${i}`, group: `defer_${i}`, color: this._deferColor(i), area: true, scale: kwScale, step: true })),
        { points: this._actualEnergy.solar, group: 'solar', color: 'var(--solar)', actual: true, area: true },
        { points: this._actualEnergy.load, group: 'load', color: 'var(--load)', actual: true, area: true },
        { points: this._actualEnergy.grid, group: 'grid', color: 'var(--gridflow)', actual: true, area: true },
        { points: this._actualEnergy.battery, group: 'battery', color: 'var(--battery)', actual: true, area: true },
        // step: true here too — a real deferrable appliance (EV charger, hot water element)
        // switches on/off in seconds on the hardware side, but HA's history API only samples
        // on significant_changes_only, so two sparse readings (last "off", first "on") would
        // otherwise get smoothPath'd into the exact same diagonal-ramp artifact as the
        // forecast series above, just drawn from real sensor data instead of planned data.
        ...dnames.map((nm, i) => ({ points: actualDefer[i], group: `defer_${i}`, color: this._deferColor(i), actual: true, area: true, step: true })),
        // SOC on its own 0-100% right axis. Deliberately unlike every other series here:
        // no area fill (its baseline would be the LEFT axis' zero, which means nothing on
        // a percentage scale), heavier stroke, and drawn last so it sits above every
        // wash. One hue for both, told apart by dash — planned dashed, measured solid,
        // the same shape language the standalone SOC card uses — because adding two new
        // hues to a chart that already carries ten was the opposite of standing out.
        { key: 'soc_percent', group: 'soc', color: 'var(--soc)', axis: 'right', dash: true, width: 3.5 },
        { points: this._actual, group: 'soc', color: 'var(--soc)', axis: 'right', width: 3 },
      ],
    };
  }

  _chartSvg() {
    let { series } = this._energySeries();
    // Isolated to one legend group (see _wireLegendToggle): drop every other series
    // rather than just dimming them, so the y-axis also rescales to that series' own
    // range — a small signal like Battery is otherwise squashed flat next to Solar/Load.
    // SOC survives isolation of any OTHER group. Isolating exists to rescale the kW axis
    // to one series; SOC is on a separate axis, so keeping it costs that nothing, and it
    // is context for whatever you just isolated ("battery charges here — does SOC agree?").
    // Isolating SOC itself still shows SOC alone.
    if (this._isolatedGroup) {
      series = series.filter((s) => s.group === this._isolatedGroup
        || (this._isolatedGroup !== 'soc' && s.group === 'soc'));
    }
    // Taller than the other line charts (default 160) so this pairs visually with the
    // Power Flow card next to it in the dashboard section, which is roughly square.
    // symmetric: true — grid/battery are signed (import/charge positive, export/discharge
    // negative), so the y-axis is forced to [-m, m] and 0 sits at the vertical centre
    // instead of hugging the bottom the way an all-positive chart would.
    const hasSoc = series.some((s) => s.group === 'soc');
    // Isolating a device also isolates its greedy hatch; isolating anything else (a
    // flow, or SOC) hides every device hatch since none of them are the isolated signal.
    let greedyBands = this._greedyBands();
    if (this._isolatedGroup) {
      greedyBands = greedyBands.filter((b) => b.group === this._isolatedGroup);
    }
    return multiLineChart(this._traj, this._timeScale(), series, {
      fmt: (v) => v.toFixed(1), height: 480, symmetric: true,
      bands: [...this._freeEnergyBands(), ...greedyBands],
      // Ticks and axis line are drawn in --soc, the same colour as the curves, so it is
      // visually unambiguous which scale SOC is read against — the one real hazard of a
      // secondary axis is taking a value off the wrong side.
      rightAxis: hasSoc ? {
        min: 0, max: 100, ticks: [0, 25, 50, 75, 100],
        fmt: (v) => `${v}%`, label: 'SOC', color: 'var(--soc)',
      } : null,
    });
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

  // Whether device i's greedy hatch covers a given hovered moment — see
  // _fetchGreedyBands()/_greedyBands(). Used to annotate the tooltip's per-device row so
  // hovering the hatch explains itself in words, not just colour.
  _isGreedyAt(i, ms) {
    return ((this._greedyBandsByDevice || [])[i] || []).some((b) => ms >= b.t0 && ms < b.t1);
  }

  // Measured SOC nearest a hovered time. this._actual is the SOC history the base class
  // fetches for wantsSocHistory (percent), NOT this._actualEnergy (kW) — different units,
  // deliberately different fields.
  _socRow(bestMs, best) {
    const meas = this._nearest(this._actual, bestMs);
    const plan = best && best.soc_percent != null ? clampPct(best.soc_percent) : null;
    if (plan == null && meas == null) return '';
    const parts = [];
    if (plan != null) parts.push(`<span class="k" style="color:var(--soc)">SOC plan</span> ${fmtPct(plan)}`);
    if (meas != null) parts.push(`<span class="k" style="color:var(--soc)">measured</span> ${fmtPct(meas)}`);
    return `<div>${parts.join(' · ')}</div>`;
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
        const g = this._isGreedyAt(i, bestMs) ? ' <span style="color:var(--muted)">(greedy)</span>' : '';
        return v != null && v > 0.01 ? `<div><span class="k" style="color:${this._deferColor(i)}">${esc(nm)}</span> ${v.toFixed(2)} kW${g}</div>` : '';
      }).join('');
      return `<b>${fmtHour(bestMs)}</b>` +
        `<div><span class="k" style="color:var(--solar)">sun</span> ${(actualSolar || 0).toFixed(2)} · <span class="k" style="color:var(--load)">load</span> ${(actualLoad || 0).toFixed(2)} kW</div>` +
        `<div>${this._signedRow(actualGrid || 0, 'buy', 'sell', '--gridflow')} · ${this._signedRow(actualBattery || 0, 'charge', 'discharge', '--battery')} kW</div>` +
        deferRows +
        this._socRow(bestMs, null) +
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
        const g = this._isGreedyAt(i, bestMs) ? ' <span style="color:var(--muted)">(greedy)</span>' : '';
        return v > 0.01 ? `<div><span class="k" style="color:${this._deferColor(i)}">${esc(nm)}</span> ${v.toFixed(2)} kW${g}</div>` : '';
      }).join('')
      + this._socRow(bestMs, best)
      // Name the shaded band the cursor is sitting in, so the wash isn't just decoration.
      + this._bandNote(best, kwScale);
  }

  _bandNote(row, kwScale) {
    const imp = row.import_rate != null ? +row.import_rate : null;
    const exp = row.export_rate != null ? +row.export_rate : null;
    if (imp != null && imp <= 1e-6) {
      return `<div style="font-size:11px;color:var(--free-import)">Free import window — anything running here is free</div>`;
    }
    if (exp != null && exp <= 1e-6 && (+row.sell_kwh || 0) > 1e-6) {
      const kw = (+row.sell_kwh || 0) * kwScale;
      return `<div style="font-size:11px;color:var(--free-spill)">Spilling ${kw.toFixed(2)} kW at $0 — free energy wasted</div>`;
    }
    return '';
  }
}

customElements.define('grid-lens-power-chart-card', GridLensPowerChartCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-power-chart-card',
  name: 'Grid Lens Power Chart',
  description: 'Measured & forecast solar/load/grid/battery power (kW).',
});
