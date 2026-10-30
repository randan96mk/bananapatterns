import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { useIsPhone } from '../useMedia';
import HowItWorks from './HowItWorks';
import AttentionStrip from './AttentionStrip';
import { FilterBadge } from './BreakoutPage';

const EXIT = { stop: '🛑 stop', exit: '📉 trend exit' };
const inr = (v) => (v == null ? '—' : `${v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
const sgn = (v) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down');
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const lakh = (v) => `₹${(v / 1e5).toFixed(2).replace(/\.00$/, '')}L`;

// Both accounts after costs, and NIFTY 50 with the same capital (dashed).
function Curve({ accts, capital }) {
  const keys = Object.keys(accts);
  const c0 = accts[keys[0]].curve;
  if (!c0?.dates?.length || c0.dates.length < 2) return <p className="muted">The curve starts after the first session.</p>;
  const W = 1000, H = 170, L = 50, T = 12, B = 22;
  const nifty = c0.nifty.filter((v) => v != null);
  const all = [...keys.flatMap((k) => accts[k].curve.net), ...nifty, capital];
  const lo = Math.min(...all), hi = Math.max(...all);
  const n = c0.dates.length;
  const x = (i) => L + ((W - L - 8) * i) / Math.max(1, n - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
  const line = (vals) => vals.map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean).join(' ');
  return (
    <svg className="orbcurve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Paper accounts vs NIFTY 50">
      {[lo, (lo + hi) / 2, hi].map((v) => <g key={v}><line x1={L} x2={W - 8} y1={y(v)} y2={y(v)} className="grid" />
        <text x={L - 6} y={y(v) + 3} textAnchor="end">{lakh(v)}</text></g>)}
      <text x={L} y={H - 5}>{day(c0.dates[0])}</text><text x={W - 8} y={H - 5} textAnchor="end">{day(c0.dates[n - 1])}</text>
      <polyline points={line(c0.nifty)} className="nifty" />
      {keys.map((k) => <polyline key={k} points={line(accts[k].curve.net)} className={`var${k}`} />)}
    </svg>
  );
}

function Account({ k, a, maxPos, phone }) {
  const A = a.account;
  const cmp = a.compare || {};
  const buys = a.orders.filter((o) => o.action === 'BUY');
  const sells = a.orders.filter((o) => o.action === 'SELL');
  const free = Math.max(0, (a.allowed ? a.cap : 0) - a.positions.length + sells.length);
  return (
    <div className="panel">
      <h3><span className={`v${k}`}>{a.name}</span> <FilterBadge k={k} f={a.filter} /></h3>
      <table className="orbvs"><tbody>
        <tr><td>Account value</td><td className="num"><b>{inr(A.value)}</b> <span className="muted small">cash {inr(A.cash)}</span></td></tr>
        <tr><td>Before costs · costs · after costs</td><td className="num">{sgn(A.gross)} · −{inr(A.costs)} · <b className={cls(A.net)}>{sgn(A.net)} ({pct(A.return)})</b></td></tr>
        <tr><td className="muted">&nbsp;&nbsp;costs split</td><td className="num muted">charges {inr(A.charges)} · slippage {inr(A.slippage)}</td></tr>
        <tr><td>Realised · unrealised</td><td className="num">{sgn(A.realised)} · {sgn(A.unrealised)}</td></tr>
        <tr><td>Won · profit factor · max DD</td><td className="num">{a.won} of {a.closed} · {a.profit_factor ?? '—'} · <span className="down">{pct(-a.drawdown)}</span></td></tr>
        <tr><td>Paper vs backtest (same days)</td><td className="num">{pct(A.return)} vs {pct(cmp.return)}</td></tr>
      </tbody></table>

      <h4>Open ({a.positions.length} of {a.allowed ? a.cap : maxPos}{a.filter?.state === 'HALF' ? ' · HALF' : ''})</h4>
      {!a.positions.length ? <p className="muted small">None.</p> : (
        <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Bought</th><th className="num">Stop</th>
          {!phone && <th className="num">SMA50</th>}<th className="num">Last</th><th className="num">After costs, if sold</th></tr></thead>
          <tbody>{a.positions.map((x) => (
            <tr key={x.symbol}><td><b>{x.symbol}</b> <span className="muted small">{x.qty} sh</span>{x.warning && <div className="warn small">⚠️ {x.warning}</div>}</td>
              <td>{day(x.in)} · {rs(x.buy)}</td><td className="num">{rs(x.stop)}</td>{!phone && <td className="num">{rs(x.sma)}</td>}
              <td className="num">{rs(x.last)}</td>
              <td className={`num ${cls(x.net)}`} title={`before costs ${sgn(x.gross)} · costs ${inr(x.costs)}`}><b>{sgn(x.net)}</b>
                <div className="muted small">{sgn(x.gross)} − {inr(x.costs)}</div></td></tr>))}</tbody></table></div>)}

      <h4>Orders for the next open <span className="muted small">· decided {day(a.decided)}</span></h4>
      {!a.orders.length ? <p className="muted small">None.</p> : <ul className="orderlist">
        {sells.map((o) => <li key={`s${o.symbol}`}><b className="down">SELL</b> <b>{o.symbol}</b> {o.qty} · <span className="muted">{o.why}</span></li>)}
        {buys.slice(0, Math.max(free, 0)).map((o) => <li key={`b${o.symbol}`}><b className="up">BUY</b> <b>{o.symbol}</b> ~{o.qty_est} · stop {rs(o.stop)}
          {' '}· risk {inr(o.risk_est)}</li>)}
        {buys.length > free && <li className="muted small">{buys.length - free} more breakout(s): {free ? 'no further' : 'no'} free slot{a.allowed ? '' : ' (filter off)'}</li>}
      </ul>}

      <h4>Trade log</h4>
      {!a.trades.length ? <p className="muted small">None yet.</p> : (
        <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Bought · sold</th><th className="num">Days</th>
          <th className="num">Before</th><th className="num">Costs</th><th className="num">After</th><th>Exit</th></tr></thead>
          <tbody>{a.trades.map((t) => (
            <tr key={t.symbol + t.in}><td><b>{t.symbol}</b></td><td>{day(t.in)} · {day(t.out)}</td><td className="num">{t.days}</td>
              <td className={`num ${cls(t.gross)}`}>{sgn(t.gross)}</td><td className="num muted" title={`charges ${inr(t.charges)} + slippage ${inr(t.slippage)}`}>{inr(t.costs)}</td>
              <td className={`num ${cls(t.net)}`}><b>{sgn(t.net)}</b></td><td>{EXIT[t.why] || t.why}</td></tr>))}
            <tr><td colSpan={3} className="muted">Total</td><td className={`num ${cls(a.trade_totals.gross)}`}>{sgn(a.trade_totals.gross)}</td>
              <td className="num muted">{inr(a.trade_totals.costs)}</td><td className={`num ${cls(a.trade_totals.net)}`}><b>{sgn(a.trade_totals.net)}</b></td><td /></tr></tbody></table></div>)}
    </div>);
}

// The 📝 Paper trading tab: accounts A and E side by side, from the same start day. Simulated: no real orders.
export default function PaperPage() {
  const phone = useIsPhone();
  const [d, setD] = useState(null);
  const [rules, setRules] = useState(null);
  const [err, setErr] = useState('');
  const [cap, setCap] = useState('');
  const [tick, setTick] = useState(0);

  useEffect(() => {
    api.getPaper().then((x) => { setD(x); setErr(''); if (!x.started) setCap(String(x.capital)); }).catch((e) => setErr(e.message));
    api.getBreakout().then((b) => setRules(b.rules)).catch(() => {});
  }, [tick]);

  const start = () => api.startPaper(Number(cap)).then(() => setTick((t) => t + 1)).catch((e) => setErr(e.message));
  const reset = () => {
    if (window.confirm('Reset paper trading? Both paper accounts and all their trades are deleted.')) {
      api.resetPaper().then(() => setTick((t) => t + 1)).catch((e) => setErr(e.message));
    }
  };
  const banner = (extra) => (
    <div className="paperbanner"><span>📝 PAPER TRADING · simulated with the Trend Breakout rules · no real orders are placed</span>{extra}</div>);
  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;
  if (!d.started) {
    return (
      <>
        {banner(null)}
        <div className="panel"><h3>Start paper trading</h3>
          <p>Two virtual accounts start the same day, <b className="vA">A · 200-day filter</b> and <b className="vE">E · half filter</b>, each with
            {' '}this capital. Every evening the breakouts are "filled" at the next open from the daily candle, with the same size, stop, exit
            {' '}and Zerodha delivery costs as the backtest. Nothing is sent to Kite.</p>
          <p>Capital per account <input className="input" type="number" min="10000" step="10000" value={cap} onChange={(e) => setCap(e.target.value)} style={{ width: 130 }} />
            {' '}<button className="btn primary" onClick={start} disabled={!(Number(cap) > 0)}>Start</button></p>
          <p className="muted small">Position size = account value ÷ {d.max_pos}; quantities follow from the capital. The default is the paper capital setting.</p>
          {err && <div className="warn small">{err}</div>}
        </div>
        <HowItWorks r={rules} paper />
      </>);
  }
  const accts = d.accounts;
  const keys = Object.keys(accts);
  const a0 = accts[keys[0]];
  return (
    <div className="orb">
      {banner(<span className="muted small">Started {day(a0.start_date)} · {inr(a0.account.capital)} each · data to {day(a0.as_of)} · updated {stamp(a0.updated_at)}
        {' '}<button className="btn" onClick={reset}>Reset…</button></span>)}
      <AttentionStrip tab="paper" />
      {err && <div className="warn small">{err}</div>}
      <div className="panel">Today's market filter ({day(a0.decided)} close): {keys.map((k) => <FilterBadge key={k} k={k} f={accts[k].filter} />)}</div>
      <div className="panel"><h3>Since the start <span className="muted small">· after costs</span></h3>
        <Curve accts={accts} capital={a0.account.capital} />
        <div className="small">{keys.map((k) => <span key={k} className={`v${k}`}><b>━ {k} {pct(accts[k].account.return)}</b> &nbsp; </span>)}
          <span className="muted">╍ NIFTY 50 {pct(a0.account.nifty)}</span></div></div>
      <div className="paperacc">{keys.map((k) => <Account key={k} k={k} a={accts[k]} maxPos={d.max_pos} phone={phone} />)}</div>
      <p className="muted small">Fills use the day's open from the daily candle (+{d.slippage_pct}% slippage a fill) and Zerodha delivery charges (STT, exchange,
        SEBI, GST, stamp, DP). Stops are checked on the day's low after the close (no live intraday price), as in the backtest. Past results don't guarantee future returns.</p>
      <HowItWorks r={rules} paper />
    </div>
  );
}
