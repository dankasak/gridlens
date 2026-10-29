/*
 * grid-lens-card: a bill's window must not become the household's date range,
 * and the period-ranking phase must show its own progress.
 *
 *   node custom_components/grid_lens/tests/test_bill_date_isolation.mjs
 *
 * Both bugs were reported together on 2026-10-26 ("it set my date range to 3
 * months … now the dashboard is not loading") and they compound: selecting an
 * uploaded bill persisted the BILL's 92-day period into the same localStorage
 * keys the live source reads back, so switching to "My data" silently ran a
 * 93-day comparison (~620 LP solves, ~4 min measured live) — and for the last
 * ~130s of that the card showed a frozen "Optimising plans… 207 / 207" at
 * 100%, because the per-period ranking phase reports 'optimising' too, by
 * which point plansDone === plansTotal. The backend had a per-plan progress
 * callback for exactly this; the card was discarding its message.
 *
 * Drives the real card class against a DOM stub — the second half runs the
 * actual _pollCalc loop over a stubbed fetch, so the status-event routing
 * under test is the shipped code path, not a re-implementation of it.
 */

let failures = 0;
const check = (name, cond, extra = '') => {
  if (cond) console.log(`  ok   ${name}`);
  else { failures++; console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`); }
};

// ---------------------------------------------------------------- DOM stub
const shadow = {
  innerHTML: '',
  activeElement: null,
  getElementById: () => null,
  querySelectorAll: () => [],
};
let Card = null;
globalThis.HTMLElement = class {
  attachShadow() { this.shadowRoot = shadow; return shadow; }
};
globalThis.customElements = { define(_n, cls) { Card = cls; } };
globalThis.window = globalThis;
globalThis.document = { createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }) };
const store = {
  _d: {},
  getItem(k) { return k in this._d ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};
globalThis.localStorage = store;
globalThis.fetch = async () => ({ ok: false, status: 0, json: async () => ({}) });

await import(new URL('../www/cards/grid-lens-card.js', import.meta.url).href);

const fresh = () => {
  const c = new Card();
  c._config = { entity: 'sensor.x' };
  c._connected = true;
  return c;
};

// ======================================================= 1. the date leak
console.log('\nA bill run leaves the household\'s own date range alone');

store._d = { 'epc-date-start': '2026-09-26', 'epc-date-end': '2026-10-26' };
{
  const c = fresh();
  c._startDate = '2026-09-26';
  c._endDate = '2026-10-26';
  c._source = 'bill:5362cfa1';
  // What the backend returns for a bill run: the bill's own fixed period,
  // and — because _setSource deliberately sends no dates — startDate is null.
  c._data = {
    start_date: '2026-06-06T00:00:00+10:00',
    end_date: '2026-09-06T23:59:59+10:00',
  };
  c._updateDatesFromData(null);

  check('saved start date untouched', store.getItem('epc-date-start') === '2026-09-26',
        `got ${store.getItem('epc-date-start')}`);
  check('saved end date untouched', store.getItem('epc-date-end') === '2026-10-26',
        `got ${store.getItem('epc-date-end')}`);
  check('_startDate still the household\'s', c._startDate === '2026-09-26', `got ${c._startDate}`);
  check('_endDate still the household\'s', c._endDate === '2026-10-26', `got ${c._endDate}`);
  check('bill window captured for display', c._billStart === '2026-06-06' && c._billEnd === '2026-09-06',
        `got ${c._billStart}..${c._billEnd}`);

  // The pickers are disabled in bill mode, but they must still SHOW the bill's
  // period — that display was the reason the two were ever sharing one slot.
  c._data.periods = [];
  c._data.plan_details = {};
  c._bills = [{ id: '5362cfa1', label: 'A bill' }];
  c.render();
  check('date inputs show the bill\'s period',
        shadow.innerHTML.includes('value="2026-06-06"') && shadow.innerHTML.includes('value="2026-09-06"'));
}

// Switching back to "My data" must ask for the household's range, not the bill's.
{
  const c = fresh();
  c._startDate = '2026-09-26';
  c._endDate = '2026-10-26';
  c._source = 'bill:5362cfa1';
  let asked = null;
  c.fetchData = (s, e) => { asked = [s, e]; };
  c._setSource('live');
  check('_setSource("live") refetches the household\'s own range',
        asked && asked[0] === '2026-09-26T00:00:00' && asked[1] === '2026-10-26T23:59:59',
        `asked ${JSON.stringify(asked)}`);
}

// The live path must still adopt a default window the backend chose for it.
{
  const c = fresh();
  c._source = 'live';
  c._data = { start_date: '2026-09-26T00:00:00+10:00', end_date: '2026-10-26T23:59:59+10:00' };
  store._d = {};
  c._updateDatesFromData(null);
  check('live default window still adopted + persisted',
        c._startDate === '2026-09-26' && store.getItem('epc-date-start') === '2026-09-26');
}

// =============================================== 2. the frozen progress bar
console.log('\nThe period-ranking phase shows its own progress, not 207/207');

const RANKING = 'Ranking alternatives for period 2/2 (150/207 plans)';

// The bare progress panel (nothing rendered yet).
{
  const c = fresh();
  c._streamPhase = 'optimising';
  c._plansDone = 207;
  c._plansTotal = 207;
  c._periodMessage = RANKING;
  c._fetchStep = 150;
  c._fetchTotal = 207;
  c._showStreamProgress();
  const msg = (shadow.innerHTML.match(/sp-msg">([^<]*)</) || [])[1];
  const bar = (shadow.innerHTML.match(/sp-bar" style="width:(\d+)%/) || [])[1];
  check('progress text names the period being ranked', msg === RANKING, `got ${msg}`);
  check('progress bar tracks plans ranked in this period, not 100%', bar === '72', `got ${bar}%`);

  // And the whole-window loop is unchanged.
  c._periodMessage = '';
  c._plansDone = 42;
  c._showStreamProgress();
  check('whole-window loop still counts plans priced',
        (shadow.innerHTML.match(/sp-msg">([^<]*)</) || [])[1] === 'Optimising plans… 42 / 207');
}

// The real poll loop: status-event routing + the inline strip, with results
// already on screen (which is when the bare panel must NOT take over).
{
  const c = fresh();
  c._bills = [];
  const planKey = 'AGL - Battery Rewards';
  const planEvent = {
    event: 'plan',
    data: {
      plan_key: planKey,
      detail: { breakdown: { total: 137.41, bill_items: null }, strategy: null, plan_info: {} },
      plans_done: 207, plans_total: 207,
      current_plan_name: planKey, alternative_plans: { [planKey]: 137.41 },
      current_plan_total: 137.41, usage_days: 93,
      start_date: '2026-06-06T00:00:00+10:00', end_date: '2026-09-06T23:59:59+10:00',
      energy_flows: {}, deferrable_devices: [],
    },
  };
  const statusEvent = {
    event: 'status',
    data: {
      phase: 'optimising', message: RANKING,
      fetch_step: 150, fetch_total: 207, plans_total: 207,
    },
  };
  const polls = [
    { status: 'running', events: [planEvent] },
    { status: 'running', events: [statusEvent] },
    { status: 'complete', events: [], result: { periods: [], plan_details: {} } },
  ];
  let i = 0;
  let snapshot = null;
  globalThis.fetch = async (url) => {
    if (url.includes('/plan_calc/start')) {
      return { ok: true, status: 200, json: async () => ({ token: 1 }) };
    }
    // Snapshot what the user is looking at after the status event was handled.
    if (i === 2) snapshot = { html: shadow.innerHTML, period: c._periodMessage, fetchMsg: c._fetchMessage };
    const p = polls[Math.min(i++, polls.length - 1)];
    return {
      ok: true, status: 200,
      json: async () => ({
        token: 1, status: p.status, events: p.events,
        next_since: 0, plans_total: 207,
        result: p.result || null, error: null,
      }),
    };
  };

  c._startDate = '2026-06-06';
  c._endDate = '2026-09-06';
  await c._pollCalc('?start_date=2026-06-06T00:00:00&end_date=2026-09-06T23:59:59',
                    '2026-06-06T00:00:00', '2026-09-06T23:59:59', ++c._pollGeneration);

  check('an optimising status message routes to _periodMessage',
        snapshot && snapshot.period === RANKING, `got ${snapshot && snapshot.period}`);
  check('_fetchMessage is left to the fetch phase',
        snapshot && snapshot.fetchMsg === '', `got ${JSON.stringify(snapshot && snapshot.fetchMsg)}`);
  check('results stay on screen during the ranking phase',
        snapshot && snapshot.html.includes('class="date-controls"') && !snapshot.html.includes('sp-wrap'));
  check('the toolbar strip carries the ranking message',
        snapshot && snapshot.html.includes(RANKING), 'strip still showed the plans counter');
  check('_periodMessage cleared once complete', c._periodMessage === '');
}

console.log(failures ? `\n${failures} check(s) failed` : '\nall checks passed');
process.exit(failures ? 1 : 0);
