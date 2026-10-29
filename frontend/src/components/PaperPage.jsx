import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { useIsPhone } from '../useMedia';

const EXIT = { stop: '🛑 stop', exit: '📉 trend exit' };
const inr = (v) => (v == null ? '—' : `${v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
const sgn = (v) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down');
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const lakh = (v) => `₹${(v / 1e5).toFixed(2).replace(/\.00$/, '')}L`;

// Paper account after costs (solid), before costs (thin), NIFTY 50 with the same capital (dashed).
function Curve({ c, capital }) {
  if (!c?.dates?.length || c.dates.length < 2) return <p className="muted">The curve starts after the first session.</p>;
  const W = 560, H = 180, L = 46, T = 12, B = 22;
  const all = [...c.net, ...c.gross, ...c.nifty.filter((v) => v != null), capital];
  const lo = Math.min(...all), hi = Math.max(...all);
  const n = c.dates.length;
  const x = (i) => L + ((W - L - 8) * i) / Math.max(1, n - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
  const line = (vals) => vals.map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean).join(' ');
  const ticks = [lo, (lo + hi) / 2, hi];
  return (
    <svg className="orbcurve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Paper account vs NIFTY 50">
      {ticks.map((v) => <g key={v}><line x1={L} x2={W - 8} y1={y(v)} y2={y(v)} className="grid" />
        <text x={L - 6} y={y(v) + 3} textAnchor="end">{lakh(v)}</text></g>)}
      <text x={L} y={H - 5}>{day(c.dates[0])}</text><text x={W - 8} y={H - 5} textAnchor="end">{day(c.dates[n - 1])}</text>
      <polyline points={line(c.nifty)} className="nifty" />
      <polyline points={line(c.gross)} className="stress" />
      <polyline points={line(c.net)} className="strat" />
    </svg>
  );
}

// The 📝 Paper trading tab: the Trend Breakout rules run forward from the start day. Simulated: no real orders.
export default function PaperPage() {
  const phone = useIsPhone();
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [cap, setCap] = useState('');
  const [tick, setTick] = useState(0);

  useEffect(() => {
    api.getPaper().then((x) => { setD(x); setErr(''); if (!x.started) setCap(String(x.capital)); })
      .catch((e) => setErr(e.message));
  }, [tick]);

  const start = () => api.startPaper(Number(cap)).then(() => setTick((t) => t + 1)).catch((e) => setErr(e.message));
  const reset = () => {
    if (window.confirm('Reset paper trading? The paper account and all its trades are deleted.')) {
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
          <p>Each evening the Trend Breakout scan's buys are "filled" at the next open from the daily candle, with the same
            equal-weight size, fixed stop, trend exit and Zerodha delivery costs as the backtest. Nothing is sent to Kite.</p>
          <p>Capital <input className="input" type="number" min="10000" step="10000" value={cap} onChange={(e) => setCap(e.target.value)} style={{ width: 130 }} />
            {' '}<button className="btn primary" onClick={start} disabled={!(Number(cap) > 0)}>Start</button></p>
          <p className="muted small">Position size = account value ÷ {d.max_pos ?? 10}; quantities follow from the capital. The default comes from
            the paper capital setting.</p>
          {err && <div className="warn small">{err}</div>}
        </div>
      </>);
  }
  const a = d.account;
  const tt = d.trade_totals;
  const cmp = d.compare || {};
  return (
    <div className="orb">
      {banner(<span className="muted small">Started {day(d.start_date)} with {inr(a.capital)} · data to {day(d.as_of)} · updated {stamp(d.updated_at)}
        {' '}<button className="btn" onClick={reset}>Reset…</button></span>)}
      {err && <div className="warn small">{err}</div>}

      <div className="orbkpis">
        <div className="k"><div className={`v ${cls(a.net)}`}>{inr(a.value)}</div><div className="l">account value · after costs {pct(a.return)}</div></div>
        <div className="k"><div className={`v ${cls(a.gross)}`}>{sgn(a.gross)}</div><div className="l">profit before costs · {pct(a.return_gross)}</div></div>
        <div className="k"><div className="v down">−{inr(a.costs)}</div><div className="l">costs: charges {inr(a.charges)} + slippage {inr(a.slippage)}</div></div>
        <div className="k"><div className={`v ${cls(a.net)}`}>{sgn(a.net)}</div><div className="l">profit after costs · NIFTY 50 {pct(a.nifty)}</div></div>
        <div className="k"><div className="v">{d.won} of {d.closed}</div><div className="l">closed trades won (after costs)</div></div>
        <div className="k"><div className="v down">{pct(-d.drawdown)}</div><div className="l">worst drawdown</div></div>
      </div>

      <div className="orbcmp">
        <div className="panel"><h3>Paper account vs NIFTY 50</h3>
          <Curve c={d.curve} capital={a.capital} />
          <div className="muted small"><span className="up"><b>━ after costs {pct(a.return)}</b></span> &nbsp;
            <span style={{ color: 'var(--warn, #d08a00)' }}>━ before costs {pct(a.return_gross)}</span> &nbsp; ╍ NIFTY 50 {pct(a.nifty)}</div></div>
        <div className="panel"><h3>Paper vs backtest <span className="muted small">· same days, same rules, same capital</span></h3>
          <table className="orbvs"><thead><tr><th /><th>Paper</th><th>Backtest</th></tr></thead><tbody>
            <tr><td>Return before costs</td><td>{pct(a.return_gross)}</td><td>{pct(cmp.gross)}</td></tr>
            <tr><td>Costs</td><td>{inr(a.costs)}</td><td>{inr(cmp.costs)}</td></tr>
            <tr><td>Return after costs</td><td><b className={cls(a.return)}>{pct(a.return)}</b></td><td className={cls(cmp.return)}>{pct(cmp.return)}</td></tr>
            <tr><td>Trades closed</td><td>{d.closed}</td><td>{cmp.trades ?? '—'}</td></tr>
          </tbody></table>
          <div className="muted small orbnote">Paper records each fill when it happens and never rewrites it. A gap means the data
            (e.g. a split adjusted later) or the rules drifted.</div></div>
      </div>

      <div className="panel"><h3>Orders for the next open <span className="muted small">· decided {day(d.decided)} · market filter {d.market_on ? '✅ on' : '⛔ off: no new buys'}</span></h3>
        {!d.orders.length ? <p className="muted">No orders.</p> : (
          <div className="tablewrap"><table className="grid"><thead><tr><th>Action</th><th>Stock</th><th className="num">Last close</th>
            <th className="num">Stop</th><th>Why</th></tr></thead>
            <tbody>{d.orders.map((o) => (
              <tr key={o.action + o.symbol}><td><b className={o.action === 'BUY' ? 'up' : 'down'}>{o.action}</b></td><td><b>{o.symbol}</b></td>
                <td className="num">{rs(o.close)}</td><td className="num">{o.stop ? <>{rs(o.stop)} <span className="muted small">{((1 - o.stop / o.close) * 100).toFixed(1)}%</span></> : '—'}</td>
                <td className="muted">{o.action === 'BUY' ? `breakout #${o.rank} · 6M ${pct(o.ret126, 0)}` : o.why}</td></tr>))}</tbody></table></div>)}
        <p className="muted small">Quantity is set at the open: account value ÷ {d.max_pos}, capped by the cash left, whole shares.</p>
        {d.skipped?.length > 0 && <p className="muted small">Not filled today: {d.skipped.map((s) => `${s.symbol} (${s.why})`).join(' · ')}</p>}
      </div>

      <div className="panel"><h3>Open positions ({d.positions.length} of {d.max_pos}) <span className="muted small">· cash {inr(a.cash)}</span></h3>
        {!d.positions.length ? <p className="muted">None.</p> : (
          <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th className="num">Qty</th><th>Bought</th>
            <th className="num">Last close</th><th className="num">Stop</th><th className="num">Before costs</th><th className="num">Costs</th>
            <th className="num">After costs, if sold</th>{!phone && <th className="num">Days</th>}</tr></thead>
            <tbody>{d.positions.map((x) => (
              <tr key={x.symbol}><td><b>{x.symbol}</b>{x.warning && <div className="warn small">⚠️ {x.warning}</div>}</td><td className="num">{x.qty}</td>
                <td>{day(x.in)} · {rs(x.buy)}</td><td className="num">{rs(x.last)}</td><td className="num">{rs(x.stop)}</td>
                <td className={`num ${cls(x.gross)}`}>{sgn(x.gross)}</td><td className="num muted">{inr(x.costs)}</td>
                <td className={`num ${cls(x.net)}`}><b>{sgn(x.net)}</b></td>{!phone && <td className="num">{x.days}</td>}</tr>))}</tbody></table></div>)}
        <p className="muted small">Costs = paid so far (buy charges and slippage) + the estimated sale costs.</p>
      </div>

      <div className="panel"><h3>Closed trades <span className="muted small">· newest first</span></h3>
        {!d.trades.length ? <p className="muted">None yet.</p> : (
          <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Bought</th><th>Sold</th><th className="num">Days</th>
            <th className="num">Before costs</th><th className="num">Costs</th><th className="num">After costs</th><th>Exit</th></tr></thead>
            <tbody>{d.trades.map((t) => (
              <tr key={t.symbol + t.in}><td><b>{t.symbol}</b> <span className="muted small">{t.qty} sh</span></td>
                <td>{day(t.in)} · {rs(t.buy)}</td><td>{day(t.out)} · {rs(t.sell)}</td><td className="num">{t.days}</td>
                <td className={`num ${cls(t.gross)}`}>{sgn(t.gross)}</td>
                <td className="num muted" title={`charges ${inr(t.charges)} + slippage ${inr(t.slippage)}`}>{inr(t.costs)}</td>
                <td className={`num ${cls(t.net)}`}><b>{sgn(t.net)}</b></td><td>{EXIT[t.why] || t.why}</td></tr>))}
              <tr><td colSpan={4} className="muted">Total</td><td className={`num ${cls(tt.gross)}`}>{sgn(tt.gross)}</td>
                <td className="num muted">{inr(tt.costs)}</td><td className={`num ${cls(tt.net)}`}><b>{sgn(tt.net)}</b></td><td /></tr></tbody></table></div>)}
        <p className="muted small">Fills use the day's open from the daily candle (+{d.slippage_pct}% slippage a fill) and Zerodha delivery charges (STT, exchange,
          SEBI, GST, stamp, DP). Stops are checked on the day's low after the close (no live intraday price), as in the backtest.
          Past results don't guarantee future returns.</p>
      </div>
    </div>
  );
}
