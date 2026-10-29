import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { useIsPhone } from '../useMedia';

const SCEN = [['normal', 'Normal slippage'], ['stress', 'Stressed slippage']];
const EXIT = { stop: '🛑 stop', exit: '📉 trend exit (close under SMA)', end: '⏹ end of test' };
const SKIP = { no_slot: 'no free slot', market_off: 'market filter off', too_small: 'too small (under 1 share)' };
const PAGE = 100;

const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const inr = (v) => (v == null ? '—' : `${v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
const pct = (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down');
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' }) : '');
const lakh = (v) => (v == null ? '—' : `₹${(v / 1e5).toFixed(2).replace(/\.00$/, '')}L`);

function Chips({ items, value, onChange }) {
  return items.map(([id, label]) => (
    <button key={id} className={`spill ${value === id ? 'on' : ''}`} aria-pressed={value === id}
            onClick={() => onChange(id)}>{label}</button>));
}

// Account value (₹): normal slippage (solid), stressed (amber), NIFTY 50 bought with the same capital (dashed).
function Curve({ c, capital }) {
  if (!c?.dates?.length) return <p className="muted">No curve yet.</p>;
  const W = 560, H = 200, L = 46, T = 14, B = 24;
  const all = [...c.normal, ...c.stress, ...c.nifty, capital];
  const lo = Math.min(...all), hi = Math.max(...all);
  const n = c.dates.length;
  const x = (i) => L + ((W - L - 8) * i) / Math.max(1, n - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
  const line = (vals) => vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const raw = (hi - lo) / 6, mag = 10 ** Math.floor(Math.log10(raw || 1));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((v) => v >= raw);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v);
  const years = [];
  c.dates.forEach((d, i) => { if (i === 0 || d.slice(0, 4) !== c.dates[i - 1].slice(0, 4)) years.push([i, d]); });
  return (
    <svg className="orbcurve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Account value vs NIFTY 50">
      {ticks.map((v) => <g key={v}><line x1={L} x2={W - 8} y1={y(v)} y2={y(v)} className={v === capital ? 'zero' : 'grid'} />
        <text x={L - 6} y={y(v) + 3} textAnchor="end">{lakh(v)}</text></g>)}
      {years.map(([i, d]) => <text key={d} x={x(i)} y={H - 6} textAnchor="middle">{d.slice(0, 4)}</text>)}
      <polyline points={line(c.nifty)} className="nifty" />
      <polyline points={line(c.stress)} className="stress" />
      <polyline points={line(c.normal)} className="strat" />
    </svg>
  );
}

// The 📈 Trend Breakout tab: the swing backtest (development / validation / out-of-sample, years, robustness) and
// the evening scan. Signals only: nothing here places an order.
export default function BreakoutPage() {
  const phone = useIsPhone();
  const [scen, setScen] = useState('normal');
  const [d, setD] = useState(null);
  const [trades, setTrades] = useState({ total: 0, rows: [] });
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true, timer;
    setLoading(true);
    const load = () => Promise.all([api.getBreakout(), api.getBreakoutTrades(scen, PAGE, 0)])
      .then(([s, t]) => {
        if (!alive) return;
        setD(s); setTrades(t); setErr(''); setLoading(false);
        if (s.progress?.state !== 'idle') timer = setTimeout(load, 3000);   // running: refresh
      })
      .catch((e) => { if (alive) { setErr(e.message); setLoading(false); } });
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, [scen, tick]);

  const more = () => api.getBreakoutTrades(scen, PAGE, trades.rows.length)
    .then((t) => setTrades({ total: t.total, rows: [...trades.rows, ...t.rows] })).catch((e) => setErr(e.message));
  const run = () => api.runBreakout().then(() => setTick((x) => x + 1)).catch((e) => setErr(e.message));

  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;
  const prog = d.progress || {};
  const R = d.rules || {};
  const running = prog.state && prog.state !== 'idle';
  const rulesText = (
    <p className="muted small">Long only, Zerodha delivery, decided on the close and filled at the next open. New buys only while NIFTY 50
      closes over its 200-day average. Universe: each day the {R.top} Nifty 500 stocks with the highest 20-day average turnover, close ≥ ₹{R.min_price}.
      Entry: close &gt; SMA50 &gt; SMA150 &gt; SMA200, SMA200 rising over 20 sessions, close within 25% of the 52-week high and 30%+ over the 52-week
      low, close over the previous {R.hi_window}-day high on ≥ {R.vol_mult}× the 50-day volume; more signals than slots: the highest 6-month return
      first. Up to {R.max_pos} equal-weight positions. Stop = max(close − {R.atr_mult} × ATR14, close − {R.stop_pct}%), fixed; out at the next open
      after a close under SMA{R.exit_sma}; no target. Costs: STT 0.1% both sides, exchange, SEBI, GST, stamp 0.015% on buys, DP ₹15.93 a sale,
      {' '}{R.slippage_pct}% slippage a fill ({R.stress_slippage_pct}% stressed). Signals only: no orders.</p>);
  const runLine = running ? <p><b>Running…</b> <span className="muted small">{prog.message}</span></p>
    : <><button className="btn primary" onClick={run}>{d.built ? 'Re-run' : 'Run the backtest'}</button>
      {prog.message && <span className="muted small"> &nbsp;{prog.message}</span>}</>;

  if (!d.built) {
    return (
      <div className="panel"><h3>📈 Trend Breakout</h3>
        <p>A 6-year swing backtest from the stored daily candles (no 5-minute data, no Kite requests), plus the evening scan.</p>
        {rulesText}{runLine}{err && <div className="warn small">{err}</div>}
      </div>);
  }
  const r = d.run;
  const T = r[scen] || {};
  const other = scen === 'normal' ? 'stress' : 'normal';
  const f = r.funnel || {};
  const sc = d.scan;

  return (
    <div className={`orb ${loading ? 'busy' : ''}`}>
      <div className="orbfilters"><Chips items={SCEN} value={scen} onChange={setScen} />
        <span className="muted small">{scen === 'stress' ? R.stress_slippage_pct : R.slippage_pct}% a fill</span></div>
      {err && <div className="warn small">{err}</div>}

      <div className="orbstatus">
        <span className="s"><b>{r.sessions}</b> sessions</span><span className="ar">→</span>
        <span className="s"><b>{(f.stock_days ?? 0).toLocaleString('en-IN')}</b> stock-days</span><span className="ar">→</span>
        <span className="s"><b>{f.signals ?? 0}</b> signals</span><span className="ar">→</span>
        <span className="s"><b>{T.trades}</b> trades</span>
        <span className="muted small orbmeta">{day(r.from)} → {day(r.to)} · Data to {day(r.to)} · updated {stamp(r.updated_at)}
          {running ? ' · running…' : <> · <button className="linkbtn" onClick={run}>Re-run</button></>}</span>
      </div>

      <div className={`panel ${r.verdict ? '' : 'warnpanel'}`}>
        <b>{r.verdict ? '✅ Profitable after costs in all three periods, at stressed slippage too, and beats NIFTY 50'
          : `❌ ${!r.ok_periods ? 'Not profitable after costs in every period (normal and stressed)' : ''}${!r.ok_periods && !r.beats_nifty ? ' · ' : ''}${!r.beats_nifty ? 'does not beat NIFTY 50' : ''}`}</b>
        <div className="muted small">Universe is today's Nifty 500 list (survivorship bias): real results will be somewhat lower. Past results don't guarantee future returns.</div>
      </div>

      <div className="orbkpis">
        <div className="k"><div className={`v ${cls(T.net)}`}>{inr(T.net)}</div><div className="l">net · {inr(T.gross)} gross − {inr(T.costs)} costs</div></div>
        <div className="k"><div className={`v ${T.cagr > r.nifty.cagr ? 'up' : 'down'}`}>{pct(T.cagr)}</div><div className="l">CAGR after costs · NIFTY 50 {pct(r.nifty.cagr)}</div></div>
        <div className="k"><div className="v">{T.win_rate?.toFixed(0)}%</div><div className="l">won after costs</div></div>
        <div className="k"><div className={`v ${T.profit_factor >= 1 ? 'up' : 'down'}`}>{T.profit_factor ?? '—'}</div><div className="l">profit factor</div></div>
        <div className="k"><div className="v down">{pct(-T.drawdown, 0)}</div><div className="l">worst drawdown · NIFTY 50 {pct(-r.nifty.drawdown, 0)}</div></div>
        <div className="k"><div className="v">{T.avg_hold?.toFixed(0)} days</div><div className="l">average hold</div></div>
        <div className="k"><div className="v">{inr(T.cost_per_trade)}</div><div className="l">costs a trade (charges + slippage)</div></div>
      </div>

      <div className="orbcmp">
        <div className="panel"><h3>{lakh(R.capital)} on these rules</h3>
          <Curve c={r.curve} capital={R.capital} />
          <div className="muted small"><span className="up"><b>━ Normal {pct(r.normal.cagr)} CAGR</b></span> &nbsp;
            <span style={{ color: 'var(--warn, #d08a00)' }}>━ Stressed {pct(r.stress.cagr)}</span> &nbsp; ╍ NIFTY 50 {pct(r.nifty.cagr)} (buy and hold)</div></div>
        <div className="panel"><h3>Development · validation · out-of-sample</h3>
          <div className="tablewrap"><table className="orbvs"><thead><tr><th />{r.splits.map((x) => (
            <th key={x.name}>{x.name}<div className="muted small">{day(x.from)} – {day(x.to)}</div></th>))}</tr></thead><tbody>
            <tr><td>Return before costs</td>{r.splits.map((x) => <td key={x.name}>{pct(x.gross)}</td>)}</tr>
            <tr><td>Costs</td>{r.splits.map((x) => <td key={x.name} className="muted">{pct(-x.costs)}</td>)}</tr>
            <tr><td>Return after costs</td>{r.splits.map((x) => <td key={x.name}><b className={cls(x.net)}>{pct(x.net)}</b></td>)}</tr>
            <tr><td>After costs, stressed</td>{r.splits.map((x) => <td key={x.name} className={cls(x.stress)}>{pct(x.stress)}</td>)}</tr>
            <tr><td>NIFTY 50</td>{r.splits.map((x) => <td key={x.name} className="muted">{pct(x.nifty)}</td>)}</tr>
          </tbody></table></div>
          <div className="muted small orbnote">The first 50% / next 25% / last 25% of the test sessions. An edge must hold in all three, at stressed slippage too.</div></div>
      </div>

      <div className="orbcmp">
        <div className="panel"><h3>Year by year <span className="muted small">· after costs, normal slippage</span></h3>
          <table className="orbvs"><thead><tr><th>Year</th><th>Trend Breakout</th><th>NIFTY 50</th></tr></thead><tbody>
            {r.years.map((y) => <tr key={y.year}><td>{y.year}</td><td><b className={cls(y.net)}>{pct(y.net)}</b></td>
              <td className={cls(y.nifty)}>{pct(y.nifty)}</td></tr>)}</tbody></table></div>
        <div className="panel"><h3>How trades ended · signals not taken</h3>
          <table className="orbvs"><tbody>
            {Object.entries(T.exits || {}).map(([k, v]) => <tr key={k}><td>{EXIT[k] || k}</td><td>{v}</td></tr>)}
            {Object.entries(T.skipped || {}).map(([k, v]) => <tr key={k}><td className="muted">⛔ {SKIP[k] || k}</td><td>{v}</td></tr>)}
          </tbody></table></div>
      </div>

      <div className="panel"><h3>Robustness <span className="muted small">· one setting changed at a time</span></h3>
        <p><b>{r.robust_pass} of {r.robust.length} pass</b> <span className="muted small">(✅ = profitable after costs in all three periods,
          stressed too, and beats NIFTY 50). {r.robust_pass >= 0.75 * r.robust.length ? 'Robust: the edge does not hinge on exact settings.'
          : 'The edge is modest and lumpy: many nearby settings fail, so expect real results to vary.'}</span></p>
        <div className="tablewrap"><table className="grid"><thead><tr><th>Changed</th><th className="num">Trades</th><th className="num">CAGR</th>
          {!phone && <th className="num">Stressed</th>}{!phone && <th className="num">Worst DD</th>}<th className="num">Dev / Val / OOS</th><th /></tr></thead>
          <tbody>{r.robust.map((x) => (
            <tr key={x.label}><td>{x.label}</td><td className="num">{x.trades}</td><td className={`num ${cls(x.cagr)}`}>{pct(x.cagr)}</td>
              {!phone && <td className={`num ${cls(x.stress_cagr)}`}>{pct(x.stress_cagr)}</td>}
              {!phone && <td className="num down">{pct(-x.drawdown, 0)}</td>}
              <td className="num">{x.periods.map((v) => pct(v, 0)).join(' / ')}</td><td>{x.ok ? '✅' : '❌'}</td></tr>))}</tbody></table></div>
      </div>

      <div className="panel"><h3>Tonight's scan <span className="muted small">· signals only, no orders{sc ? ` · ${day(sc.as_of)} · ${stamp(sc.computed_at)}` : ''}</span></h3>
        {!sc ? <p className="muted">Runs after the evening sync (and with Re-run).</p> : <>
          <p>Market filter: {sc.market_on ? <b className="up">✅ on</b> : <b className="down">⛔ off: no new buys</b>}
            <span className="muted small"> · NIFTY 50 {sc.nifty?.toLocaleString('en-IN')} vs 200-day {sc.nifty_sma200?.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span></p>
          {sc.buys?.length ? <div className="tablewrap"><table className="grid"><thead><tr><th>#</th><th>Buy at the next open</th><th className="num">Close</th>
            <th className="num">Stop</th><th className="num">Qty · value</th><th className="num">₹ at risk</th></tr></thead>
            <tbody>{sc.buys.map((b, k) => <tr key={b.symbol}><td className="muted">{k + 1}</td><td><b>{b.symbol}</b>
              {!phone && <span className="muted small"> 6M {pct(b.ret126, 0)}</span>}</td><td className="num">{rs(b.close)}</td>
              <td className="num">{rs(b.stop)} <span className="muted small">{b.stop_pct.toFixed(1)}%</span></td>
              <td className="num">{b.qty} · {inr(b.value)}</td><td className="num down">{inr(b.risk)}</td></tr>)}</tbody></table></div>
            : <p className="muted">{sc.market_on ? 'No breakout buys today.' : `${sc.signals_filtered?.length || 0} breakouts not bought (market filter off).`}</p>}
          <h4>Your Kite holdings</h4>
          {sc.holdings?.length ? <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th className="num">Qty</th>
            <th className="num">Avg · close</th>{!phone && <th className="num">SMA{R.exit_sma}</th>}<th>Action</th></tr></thead>
            <tbody>{sc.holdings.map((h) => <tr key={h.symbol}><td><b>{h.symbol}</b></td><td className="num">{h.qty}</td>
              <td className="num">{rs(h.avg)} · {rs(h.close)}</td>{!phone && <td className="num">{rs(h.sma)}</td>}
              <td><b className={h.action === 'HOLD' ? 'up' : 'down'}>{h.action}</b> <span className="muted small">{h.why}</span></td></tr>)}</tbody></table></div>
            : <p className="muted">{sc.holdings_read ? 'No holdings.' : 'Not checked: log in to Kite (read-only holdings).'}</p>}
        </>}
      </div>

      <div className="panel"><h3>Trades <span className="muted small">· newest first · {scen === 'stress' ? 'stressed' : 'normal'} slippage</span></h3>
        <div className="tablewrap"><table className="grid orbtrades"><thead><tr><th>Stock</th><th>Entry</th><th>Exit</th>
          <th className="num">Days</th>{!phone && <th className="num">Gross</th>}{!phone && <th className="num">Costs</th>}<th className="num">Net</th><th>Exit reason</th></tr></thead>
          <tbody>{trades.rows.map((t) => (
            <tr key={`${t.sym}${t.in}`}><td><b>{t.sym}</b> <span className="muted small">{t.qty} sh</span></td>
              <td>{day(t.in)}<div className="muted small">{rs(t.buy)}</div></td>
              <td>{day(t.out)}<div className="muted small">{rs(t.sell)}</div></td>
              <td className="num">{t.days}</td>
              {!phone && <td className={`num ${cls(t.gross)}`}>{inr(t.gross)}</td>}
              {!phone && <td className="num muted">{inr(t.costs)}</td>}
              <td className={`num ${cls(t.net)}`}><b>{inr(t.net)}</b></td><td>{EXIT[t.why]?.split(' (')[0] || t.why}</td></tr>))}</tbody></table></div>
        {trades.total > trades.rows.length && (
          <p className="orbmore"><button className="btn" onClick={more}>Show {Math.min(PAGE, trades.total - trades.rows.length)} more</button>
            <span className="muted small"> &nbsp;{trades.rows.length} of {trades.total}</span></p>)}
        {rulesText}
      </div>
    </div>
  );
}
