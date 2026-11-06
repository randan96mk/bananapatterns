// 🧪 Home visual check: renders the built Home against a running backend with a FAKED live stream (15 positions, a
// busy timeline, every panel filled) and fails if anything overlaps or spills: card text vs sparkline, map tiles vs
// their box, the stop-distance text vs its cell. Run before pushing a Home change:
//   node frontend/scripts/home_visual_check.cjs http://127.0.0.1:8000 out.png 1600   (and 390 for the phone)
const { chromium } = require('playwright');
const base = process.argv[2], out = process.argv[3] || 'fake_home.png', width = +(process.argv[4] || 1600);
const sp = (a, b, n = 30) => Array.from({ length: n }, (_, i) => a + (b - a) * i / (n - 1) + Math.sin(i) * (b - a) / 8);
const pos = (book, account, strategy, symbol, qty, entry, now, stop, live) => ({ book, account, strategy, symbol, side: 'long', qty, entry, now, stop,
  pnl: Math.round((now - entry) * qty), pct: +(((now - entry) / entry) * 100).toFixed(2), stop_dist: +(((now - stop) / now) * 100).toFixed(2), opened: '2026-11-03', live });
const positions = [
  pos('real', 'Real', 'Trend Breakout E+', 'DIVISLAB', 2, 9965, 10168, 9394.36, true), pos('real', 'Real', 'Trend Breakout E+', 'PHOENIXLTD', 9, 2027, 2111.3, 1897.03, true),
  pos('real', 'Real', 'Trend Breakout E+', 'GAIL', 113, 177.03, 184.89, 167.39, true), pos('real', 'Real', 'Trend Breakout E+', 'GLENMARK', 8, 2393.8, 2549.7, 2261.17, true),
  pos('real', 'Real', 'Trend Breakout E+', 'HEGAM', 69, 299.96, 284.9, 269.56, true), pos('real', 'Real', 'Trend Breakout E+', 'WELSPUNLIV', 79, 253, 244.51, 231.2, true),
  pos('real', 'Real', 'Trend Breakout E+', 'SONACOMS', 22, 902.6, 890.1, 850.24, true), pos('real', 'Real', 'Trend Breakout E+', 'ANANDRATHI', 8, 2371.6, 2359.5, 2296.89, true),
  pos('paper_E', 'Paper E+', 'Trend Breakout E+', 'DIVISLAB', 2, 9965, 10168, 9394.36, true), pos('paper_E', 'Paper E+', 'Trend Breakout E+', 'PHOENIXLTD', 9, 2027, 2111.3, 1897.03, true),
  pos('paper_E', 'Paper E+', 'Trend Breakout E+', 'GAIL', 112, 177.03, 184.89, 167.39, true), pos('paper_E', 'Paper E+', 'Trend Breakout E+', 'GLENMARK', 8, 2393.8, 2549.7, 2261.17, true),
  pos('paper_E', 'Paper E+', 'Trend Breakout E+', 'WELSPUNLIV', 79, 253, 244.51, 231.2, true),
  pos('intraday', 'Intraday · signals (tracked)', 'Intraday Flow', 'GLENMARK', 39, 2525, 2549.7, 2500, true), pos('intraday', 'Intraday · signals (tracked)', 'Intraday Flow', 'M&M', 30, 3310, 3300, 3280, true),
];
const now = new Date();
const iso = (h, m) => { const d = new Date(now); d.setHours(h, m, 0, 0); return d.toISOString(); };
const overview = { at: now.toISOString(), today: -1202, feed: 'live', positions,
  accounts: { real: { name: 'Real · Tradetron', value: 202070, net: 2070, return: 1.0, last_day: '2026-11-05', day_pnl: -529, positions: 8, on: true, variant: 'E', spark: sp(200000, 202070) },
    paper_A: { name: 'Paper A', value: 200000, net: 0, return: 0, day_pnl: 0, positions: 0, spark: sp(200000, 200000) },
    paper_E: { name: 'Paper E+', value: 202656, net: 2656, return: 1.3, day_pnl: -528, positions: 5, spark: sp(199000, 202656) },
    intraday: { name: 'Intraday · paper', day_pnl: -145, realised: 1000, open_pnl: -1145, trades: 3, open: 2, at_risk: 1793, loss_limit: 3000, capital: 200000, mode: 'signals', spark: sp(2000, 500) },
    follow: { name: 'Screens · Follow', net: 504329, open_pnl: 137270, open: 33, breakouts: 540, win_rate: 30.6, per_position: 100000 } },
  facts: { today: { high: 2140, high_at: '09:27', low: -969, low_at: '10:22' }, real: { in_use: 156842, slots: 8, max_pos: 10, capital: 202070 }, paper_A: { vs_nifty: -4.7, filter: 'ON', open: 0 }, paper_E: { vs_nifty: -3.4, filter: 'FULL', open: 5 }, intraday: { loss_used: 86, loss_limit: 3000, signals: 3, per_day: 6 }, follow: { open: 33, open_pnl: 137270, pending: 3 } },
  activity: [{ at: iso(9, 40), account: 'Intraday', text: 'BUY signal GLENMARK' }, { at: iso(9, 16), account: 'Real', text: 'BUY MOTHERSON 121 · sent' }, { at: iso(9, 16), account: 'Real', text: 'BUY HEGAM 69 · sent' }],
  stats: { win_streak: 0, best_day: { day: '2026-11-04', net: 4989 }, worst_day: { day: '2026-11-05', net: -1405 }, days_since_loss: 0, signals_today: 3, hit_rate_30d: 42.9, closed_30d: 7, orders_week: 10, orders_failed_week: 0 },
  risk: { in_use: 396900, capital: 860000, worst_case: -22817, biggest: { symbol: 'GAIL', value: 20950 }, orders_today: { sent: 4, failed: 0 }, near_stop: ['M&M'], loss_used: 0, loss_limit: 3000 },
  timeline: [{ t: '09:16', kind: 'order', text: 'BUY MOTHERSON 121 · sent' }, { t: '09:16', kind: 'order', text: 'BUY HEGAM 69 · sent' }, { t: '09:16', kind: 'order', text: 'BUY SONACOMS 22 · sent' }, { t: '09:16', kind: 'order', text: 'BUY ANANDRATHI 8 · sent' },
    { t: '09:25', kind: 'signal', text: 'BUY signal GLENMARK' }, { t: '09:40', kind: 'signal', text: 'BUY signal ONESOURCE' }, { t: '09:58', kind: 'exit', text: 'ONESOURCE out · stop · −₹647' }, { t: '10:05', kind: 'signal', text: 'SELL signal M&M' }],
  pulse: { regime: 'correction', as_of: '2026-11-05', breadth_50: 47.1, dist_days: 9, breakouts: { working: 26, total: 38, rate: 0.68 }, advancing: 207, declining: 261, above_vwap_pct: 45.3, nifty_vs_vwap: 'above', live: true, signals_live: 2 },
  today_curve: Array.from({ length: 25 }, (_, i) => [iso(10, 11 + i), -1298 + Math.round(Math.sin(i / 3) * 400 - i * 10)]) };
const attention = { breakout: { level: 'filter', items: [{ level: 'filter', text: 'Market filter changed: A OFF → ON, E HALF → FULL' }, { level: 'buy', text: '9 breakout(s) for the next open' }], sig: 'a' },
  paper: { level: 'buy', items: [{ level: 'filter', text: 'Market filter changed: A OFF → ON, E HALF → FULL' }, { level: 'buy', text: 'A: buy at the next open (5)' }, { level: 'buy', text: 'E: buy at the next open (5)' }], sig: 'b' } };
const schedule = { now: now.toISOString(), jobs: [{ key: 'real_stops', name: 'Real stop check', icon: '🛡', at: iso(10, 40) }, { key: 'intraday', name: 'Intraday scan', icon: '⚡', at: iso(10, 40) }, { key: 'filings', name: 'Filings check', icon: '📰', at: iso(11, 22) }, { key: 'close', name: 'Close check', icon: '🕒', at: iso(15, 15) }, { key: 'eod', name: 'Evening sync + scan', icon: '🌙', at: iso(18, 35) }] };
(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width, height: 900 }, deviceScaleFactor: 1 });
  const errs = []; p.on('pageerror', (e) => errs.push(e.message));
  await p.route('**/api/stream**', (route) => {
    const topics = new URL(route.request().url()).searchParams.get('topics').split(',');
    const data = { overview, attention, schedule };
    const body = 'retry: 3000\n\n' + topics.filter((t) => data[t]).map((t) => `event: ${t}\ndata: ${JSON.stringify(data[t])}\n\n`).join('');
    route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body });
  });
  await p.route('**/api/health', (route) => route.fulfill({ json: { at: now.toISOString(), red: 0, amber: 1, checks: [{ key: 'kite', name: 'Kite', ok: true, warn: false, state: 'logged in', detail: '' }, { key: 'filings', name: 'Filings', ok: true, warn: true, state: '31 h ago', detail: '' }] } }));
  await p.goto(`${base}/#/home/home`, { waitUntil: 'domcontentloaded' }); await p.waitForTimeout(2500);
  await p.screenshot({ path: out, fullPage: true });
  const problems = await p.evaluate(() => {
    const out = [], R = (el) => el.getBoundingClientRect();
    document.querySelectorAll('.hcard').forEach((c) => {
      const s = c.querySelector('.s'), sp = c.querySelector('.hspark'), v = c.querySelector('.vrow'), bl = c.querySelector('.bl');
      if (s && sp && R(s).bottom > R(sp).top + 0.5) out.push(`card text under the sparkline: ${c.textContent.slice(0, 30)}`);
      if (bl && sp && R(bl).right > R(sp).left + 0.5) out.push(`card fact under the sparkline: ${c.textContent.slice(0, 30)}`);
      if (s && bl && R(s).bottom > R(bl).top + 0.5) out.push(`card sub-line over the fact: ${c.textContent.slice(0, 30)}`);
      [...c.children].forEach((ch) => { if (R(ch).width && (R(ch).right > R(c).right + 0.5 || R(ch).bottom > R(c).bottom + 0.5)) out.push(`card part spills: ${ch.className}`); });
      if (v && R(v).bottom > R(c).bottom) out.push(`card value outside the card: ${c.textContent.slice(0, 30)}`);
      if (R(c).height > 120) out.push(`card taller than 120: ${c.textContent.slice(0, 30)}`);
    });
    const m = document.querySelector('.hmap');
    if (m) document.querySelectorAll('.hmap .tile').forEach((t) => { if (R(t).right > R(m).right + 0.5 || R(t).bottom > R(m).bottom + 0.5) out.push(`map tile spills: ${t.textContent}`); });
    document.querySelectorAll('.hpos .stopmeter').forEach((sm) => { const cell = sm.closest('td'); if (cell && R(sm).right > R(cell).right + 0.5) out.push('stop meter wider than its cell'); });
    const tl = document.querySelector('.htl');
    if (tl) tl.querySelectorAll('.cl, .ev').forEach((e) => { if (R(e).right > R(tl).right + 0.5 || R(e).left < R(tl).left - 0.5) out.push('timeline dot outside the bar'); });
    if (document.documentElement.scrollWidth > window.innerWidth + 1) out.push(`horizontal page overflow: ${document.documentElement.scrollWidth} > ${window.innerWidth}`);
    return out;
  });
  console.log(problems.length ? `PROBLEMS\n${problems.join('\n')}` : 'HOME LAYOUT OK', 'errors', errs); await b.close();
  process.exit(problems.length || errs.length ? 1 : 0);
})();
