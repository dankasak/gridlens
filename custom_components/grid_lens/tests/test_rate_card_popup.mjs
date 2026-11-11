/*
 * grid-lens-advisory-card: the rate-card popup behind the header buy/sell readout.
 *
 *   node custom_components/grid_lens/tests/test_rate_card_popup.mjs
 *
 * Drives the real card class against a DOM stub and asserts on the markup it builds from
 * a `plan_rates` payload (retailer_plans.build_rate_card's shape — test_rate_card.py
 * covers the payload itself).
 *
 * The load-bearing claim is the HIGHLIGHT: exactly the tier whose mask bit is set for the
 * current half-hour is marked, and nothing is marked at all when the masks describe a
 * different day (just past midnight, before the sensor's next refresh). Getting that
 * wrong does not break anything visibly — it quietly tells the user they are on a rate
 * they are not on — so it is asserted here rather than left to a glance at the browser.
 */

// ---------------------------------------------------------------- DOM stub
globalThis.customElements = { define(n, c) { if (!globalThis.__card) globalThis.__card = c; },
  get() { return null; } };
globalThis.window = { addEventListener() {}, removeEventListener() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }) };
globalThis.document = {
  createElement: () => ({ style: {}, classList: { add() {}, remove() {}, toggle() {} } }),
};
globalThis.HTMLElement = class {
  attachShadow() {
    this._shadow = { querySelector: () => null, querySelectorAll: () => [], addEventListener() {} };
    return this._shadow;
  }
  get shadowRoot() { return this._shadow; }
  classList = { toggle() {} };
};

await import('../www/cards/grid-lens-advisory-card.js');
const Card = globalThis.__card;

const FAILURES = [];
function check(name, cond, detail = '') {
  if (cond) console.log(`  ok  ${name}`);
  else { console.log(`  FAIL ${name} ${detail}`); FAILURES.push(name); }
}

const SLOTS = 48;
function maskOf(...ranges) {              // ranges are [fromSlot, toSlotExclusive]
  const m = new Array(SLOTS).fill('0');
  for (const [a, b] of ranges) for (let i = a; i < b; i++) m[i] = '1';
  return m.join('');
}
function localDateStr(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    + `-${String(d.getDate()).padStart(2, '0')}`;
}

// "Now" is whatever time this test runs at, so the fixture is built around it rather than
// pinned to a clock hour — a test that only passed in the afternoon would be worse than
// no test.
const NOW = new Date();
const NOW_SLOT = Math.floor((NOW.getHours() * 60 + NOW.getMinutes()) / 30);
const OTHER_SLOT = (NOW_SLOT + 6) % SLOTS;

function planRates(extra = {}) {
  return Object.assign({
    retailer: 'Testco',
    plan_name: 'Time Of Use',
    network: 'Ausgrid',
    mask_date: localDateStr(NOW),
    slot_minutes: 30,
    daily_supply_charge: 1.58631,
    monthly_subscription: 0,
    fixed_daily_credit: 0,
    market_linked: false,
    spot_export: false,
    strategy: '',
    import: [
      { label: 'Peak', rate: 0.54175, range: '3pm–9pm', daily_cap_kwh: null,
        rate_after_cap: null, cap_period: 'day', cap_application: 'strict',
        mask: maskOf([NOW_SLOT, NOW_SLOT + 1]) },
      { label: 'Off-Peak', rate: 0.21626, range: '9pm–3pm', daily_cap_kwh: null,
        rate_after_cap: null, cap_period: 'day', cap_application: 'strict',
        mask: maskOf([OTHER_SLOT, OTHER_SLOT + 1]) },
    ],
    export: [
      { label: 'Morning Export', rate: 0.03, range: '7am–5pm', daily_cap_kwh: null,
        rate_after_cap: null, cap_period: 'day', cap_application: 'strict',
        mask: maskOf([NOW_SLOT, NOW_SLOT + 1]) },
      { label: 'Off-Peak Export', rate: 0.03, range: '9pm–7am', daily_cap_kwh: null,
        rate_after_cap: null, cap_period: 'day', cap_application: 'strict',
        mask: maskOf([OTHER_SLOT, OTHER_SLOT + 1]) },
    ],
  }, extra);
}

function cardWith(rates, traj) {
  const el = new Card();
  el._summary = { status: 'ok', plan_name: 'Testco - Time Of Use' };
  el._traj = traj === undefined ? [{
    start: new Date(NOW.getTime() - 60000).toISOString(),
    import_rate: 0.54175, export_rate: 0.03,
  }] : traj;
  el._planRates = rates;
  return el;
}

// Row extraction: one entry per .rc-row, with its label text and whether it is marked.
function rows(html) {
  const out = [];
  const re = /<div class="rc-row([^"]*)"[^>]*>([\s\S]*?)<\/div>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const body = m[2].replace(/<[^>]+>/g, '|').replace(/\|+/g, '|');
    out.push({ active: / active/.test(m[1]), text: body, raw: m[2] });
  }
  return out;
}

function test_button_is_a_toggle() {
  const el = cardWith(planRates());
  const html = el._currentRatesHtml();
  check('the readout is a button, not a div', /^<button/.test(html.trim()), html.slice(0, 40));
  check('it carries the toggle hook', html.includes('data-rates-toggle'));
  check('and reports its state to assistive tech', html.includes('aria-expanded="false"'));
  el._ratesPopOpen = true;
  check('which follows the open state', el._currentRatesHtml().includes('aria-expanded="true"'));
  check('both rates are still shown on the button',
        html.includes('Buy') && html.includes('Sell'));
}

function test_only_the_tier_in_force_is_marked() {
  const html = cardWith(planRates())._ratesPopHtml();
  const r = rows(html);
  const marked = r.filter((x) => x.active).map((x) => x.text);
  check('exactly one import and one export tier are marked', marked.length === 2,
        JSON.stringify(marked));
  check('the marked import tier is the one holding this slot',
        marked.some((t) => t.includes('Peak') && !t.includes('Off-Peak')), JSON.stringify(marked));
  check('the marked export tier is the one holding this slot',
        marked.some((t) => t.includes('Morning Export')), JSON.stringify(marked));
  check('a marked row carries the now chip', html.includes('rc-nowtag'));
  check('every tier is still listed, marked or not', r.length >= 4, String(r.length));
  check('rates print at bill precision (2dp)', html.includes('54.17c/kWh'), html);
}

function test_stale_masks_drop_the_highlight() {
  const yesterday = new Date(NOW.getTime() - 86400000);
  const el = cardWith(planRates({ mask_date: localDateStr(yesterday) }));
  const html = el._ratesPopHtml();
  check('no slot resolves against another day s masks', el._rcSlotIdx(el._planRates) === -1);
  check('nothing is marked', rows(html).every((x) => !x.active));
  check('and the popup says which day it is describing',
        html.includes('Rates for') && html.includes(localDateStr(yesterday)));
}

function test_capped_tier_renders_both_tranches() {
  const rates = planRates();
  rates.export = [{
    label: 'Solar Feed-in', rate: 0.12, range: null, daily_cap_kwh: 10,
    rate_after_cap: 0.02, cap_period: 'day', cap_application: 'pooled',
    mask: maskOf([NOW_SLOT, NOW_SLOT + 1]),
  }];
  const html = cardWith(rates)._ratesPopHtml();
  check('a capped tier becomes two lines, not one blended one',
        html.includes('first 10 kWh/day avg') && html.includes('after 10 kWh/day avg'), html);
  check('each tranche prints its own rate',
        html.includes('12.00c/kWh') && html.includes('2.00c/kWh'));
  check('an all-day tier is labelled as such', html.includes('All day'));
}

function test_market_linked_plan_is_flagged() {
  const rates = planRates({
    market_linked: true, spot_export: true,
    spot: { region: 'NSW1', import_adder_c: 9.5, import_cap_c: 100, export_floor_c: -1 },
  });
  const html = cardWith(rates)._ratesPopHtml();
  check('the wholesale caveat is shown', html.includes('rc-warn'));
  check('with the plan s own adder', html.includes('9.50c/kWh'), html);
  check('and its cap', html.includes('100c/kWh'));
}

function test_fixed_charges_and_credits() {
  const rates = planRates({
    monthly_subscription: 5, fixed_daily_credit: 1,
    controlled_load: [{ register: 'controlled_load_1', label: 'Controlled Load 1',
      rate: 0.17369, daily_supply_charge: 0.04114, note: 'provenance prose' }],
    demand: [{ label: 'Summer demand', rate_per_kw_per_day: 0.2, range: '3pm–9pm (weekdays)',
      active: true }],
    credits: [{ label: 'Quiet evening credit', amount_per_day: 1, condition: 'max_import_kwh',
      threshold_kwh: 0.03, range: '6pm–9pm', active: false }],
  });
  const html = cardWith(rates)._ratesPopHtml();
  check('supply charge as dollars per day', html.includes('$1.59/day'));
  check('subscription as dollars per month', html.includes('$5.00/mth'));
  check('demand charge per kW per day', html.includes('$0.20/kW/day'));
  check('an active demand period is marked',
        rows(html).some((x) => x.active && x.text.includes('Summer demand')));
  check('controlled load rate and its own supply charge',
        html.includes('17.37c/kWh') && html.includes('$0.04/day'));
  check('the CL provenance note is a tooltip, not body copy',
        html.includes('title="provenance prose"'));
  check('credits show as a positive amount', html.includes('+$1.00/day'));
  check('an out-of-window credit is not marked',
        !rows(html).some((x) => x.active && x.text.includes('Quiet evening')));
}

function test_missing_payload_still_renders_the_live_numbers() {
  const html = cardWith(null)._ratesPopHtml();
  check('the plan name survives', html.includes('Testco - Time Of Use'));
  check('the live buy rate survives', html.includes('54.17c/kWh'));
  check('and it says the detail is unavailable rather than rendering an empty table',
        html.includes('rc-none'));
}

function test_next_change_hint() {
  const traj = [
    { start: new Date(NOW.getTime() - 60000).toISOString(), import_rate: 0.54175, export_rate: 0.03 },
    { start: new Date(NOW.getTime() + 3600000).toISOString(), import_rate: 0.54175, export_rate: 0.28 },
    { start: new Date(NOW.getTime() + 7200000).toISOString(), import_rate: 0.21626, export_rate: 0.28 },
  ];
  const el = cardWith(planRates(), traj);
  check('the buy side points at its own next change',
        el._nextRateChange('import_rate') === traj[2].start);
  check('the sell side points at its own, earlier one',
        el._nextRateChange('export_rate') === traj[1].start);
  check('a rate that never changes in the horizon has no hint',
        el._nextRateChange('nope_rate') === null);
  check('the hint reaches the popup', el._ratesPopHtml().includes('until'));
}

for (const [name, fn] of Object.entries({
  test_button_is_a_toggle,
  test_only_the_tier_in_force_is_marked,
  test_stale_masks_drop_the_highlight,
  test_capped_tier_renders_both_tranches,
  test_market_linked_plan_is_flagged,
  test_fixed_charges_and_credits,
  test_missing_payload_still_renders_the_live_numbers,
  test_next_change_hint,
})) {
  console.log(`${name}:`);
  fn();
}

console.log();
if (FAILURES.length) {
  console.log(`FAILED: ${FAILURES.length} — ${FAILURES.join(', ')}`);
  process.exit(1);
}
console.log('OK — the rate card marks the rate actually in force, and only that one.');
