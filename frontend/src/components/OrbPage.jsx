import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { ampm, stamp } from '../timefmt';
import { useIsPhone } from '../useMedia';

const PERIODS = [['1m', '1M'], ['3m', '3M'], ['6m', '6M'], ['1y', '1Y'], ['2y', '2Y'], ['y1', 'Year 1'], ['y2', 'Year 2']];
const SETS = [['all', 'All trades'], ['long', '▲ Momentum longs'], ['short', '▼ Momentum shorts'], ['weak', '▼ 52-week-low shorts']];
const STATUSES = [['closed', 'Trades'], ['won', 'Won'], ['lost', 'Lost'], ['no_entry', 'No entry'], ['all', 'All']];
const YEAR = { y1: 'Year 1 (older)', y2: 'Year 2 (latest)' };
const PAGE = 100;

const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const rr = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}R`);
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down');
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const lakh = (v) => (v == null ? '—' : `₹${(v / 1e5).toFixed(2).replace(/\.00$/, '')}L`);
const lakhs = (v) => (v == null ? '—' : `${(v / 1e5).toFixed(1)}L`);

function Chips({ items, value, onChange }) {
  return items.map(([id, label]) => (
    <button key={id} className={`spill ${value === id ? 'on' : ''}`} aria-pressed={value === id}
            onClick={() => onChange(id)}>{label}</button>));
}

// Account value (%) under the Momentum ORB rules vs Nifty 500 buy and hold.
function Curve({ p }) {
  if (!p?.dates?.length) return <p className="muted">No trades in this period yet.</p>;
  const W = 560, H = 190, L = 40, T = 14, B = 24;
  const all = [...p.strategy, ...(p.nifty || []), 0];
  let lo = Math.min(...all), hi = Math.max(...all);
  if (hi - lo < 4) { hi += 2; lo -= 2; }
  const n = p.dates.length;
  const x = (i) => L + ((W - L - 8) * i) / Math.max(1, n - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const line = (vals) => vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const raw = (hi - lo) / 6, mag = 10 ** Math.floor(Math.log10(raw));     // ~6 gridlines at 1 / 2 / 5 x 10^n
  const step = [1, 2, 5, 10].map((m) => m * mag).find((v) => v >= raw);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v);
  const months = [];
  p.dates.forEach((d, i) => { if (i === 0 || d.slice(5, 7) !== p.dates[i - 1].slice(5, 7)) months.push([i, d]); });
  const every = Math.ceil(months.length / 8);
  const sl = p.strategy[n - 1], nl = p.nifty?.length ? p.nifty[p.nifty.length - 1] : null;
  return (
    <svg className="orbcurve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Account value vs Nifty 500">
      {ticks.map((v) => <g key={v}><line x1={L} x2={W - 8} y1={y(v)} y2={y(v)} className={v === 0 ? 'zero' : 'grid'} />
        <text x={L - 6} y={y(v) + 3} textAnchor="end">{v > 0 ? '+' : ''}{v}%</text></g>)}
      {months.filter((_, k) => k % every === 0).map(([i, d]) => (
        <text key={d} x={x(i)} y={H - 6} textAnchor="middle">{day(d).split(' ')[1]}</text>))}
      <polygon points={`${x(0)},${y(0)} ${line(p.strategy)} ${x(n - 1)},${y(0)}`} className="area" />
      {p.nifty?.length > 1 && <polyline points={line(p.nifty)} className="nifty" />}
      <polyline points={line(p.strategy)} className="strat" />
      <circle cx={x(n - 1)} cy={y(sl)} r="3.5" className="dot" />
      {nl != null && <circle cx={x(n - 1)} cy={y(nl)} r="3" className="ndot" />}
    </svg>
  );
}

// "🎯 50% at ₹1,025.00 (10:20 AM) → 📉 ATR trail +1.8% (1:45 PM)"
function timeline(t) {
  if (t.status === 'no_entry' || t.status === 'pending') return t.reason;
  return (t.events || []).slice(1).map((e) => {
    const r = pct((e.price / t.entry - 1) * 100);
    if (e.kind === 'half') return `🎯 ${Math.round((e.frac ?? 0.5) * 100)}% at ${rs(e.price)} (${ampm(e.time)})`;
    const icon = e.why?.includes('stop') ? '🛑' : e.why?.includes('trail') ? '📉' : '⏱';
    return `${icon} ${e.why} ${r} (${ampm(e.time)})`;
  }).join(' → ');
}

// The 🚀 Momentum ORB tab: two evening screens, the next session's 30-minute breakout (long or short), and how it did.
export default function OrbPage() {
  const phone = useIsPhone();
  const [period, setPeriod] = useState('1y');
  const [which, setWhich] = useState('all');
  const [status, setStatus] = useState('closed');
  const [d, setD] = useState(null);
  const [trades, setTrades] = useState({ total: 0, rows: [] });
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true, timer;
    setLoading(true);
    const load = () => Promise.all([api.getOrb(period, which), api.getOrbTrades(period, status, PAGE, 0, which)])
      .then(([s, t]) => {
        if (!alive) return;
        setD(s); setTrades(t); setErr(''); setLoading(false);
        if (s.progress?.state !== 'idle') timer = setTimeout(load, 3000);   // running: refresh
      })
      .catch((e) => { if (alive) { setErr(e.message); setLoading(false); } });
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, [period, status, which]);

  const more = () => api.getOrbTrades(period, status, PAGE, trades.rows.length, which)
    .then((t) => setTrades({ total: t.total, rows: [...trades.rows, ...t.rows] })).catch((e) => setErr(e.message));
  const run = (rebuild = false) => api.buildOrb(rebuild)
    .then(() => setD({ ...d, progress: { state: 'building', done: 0, total: 0 } })).then(() => setPeriod(period))
    .catch((e) => setErr(e.message));

  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;
  const prog = d.progress || {};
  const R = d.rules || {};
  const rulesText = (
    <p className="muted small">Evening screens (Nifty 500, close over ₹{R.min_price}, volume over {lakhs(R.min_volume)} shares):
      <b> momentum</b> RSI({R.rsi_len}) over {R.rsi_min}, close over the {R.ema_len}-day EMA, a new 52-week high; <b>52-week low</b> RSI
      under {R.weak_rsi_max}, close under the EMA, a new 52-week low. Next session: the first 5-minute close beyond the
      9:15–9:45 AM range (until {ampm(R.entry_end)}) decides: over it = buy (momentum list), under it = sell short (both lists).
      Stop {R.stop_pct}% away; {Math.round((R.book_frac ?? 0.5) * 100)}% closed at {R.target_pct}% profit (the stop then moves to the
      entry); the rest on a 5-minute close past the best price ∓ {R.atr_mult} × ATR({R.atr_len}); everything closed by {ampm(R.exit_time)}.
      A candle touching both the stop and the target counts as the stop. After costs: {R.cost_pct}% a round trip (intraday) +
      {' '}{R.slippage_pct}% slippage a fill.</p>);

  if (!d.built) {
    const running = prog.state && prog.state !== 'idle';
    return (
      <div className="panel"><h3>🚀 Momentum ORB</h3>
        <p>A 2-year backtest of the two screens plus the next morning's 30-minute breakout (long or short), before any live alerts.</p>
        {rulesText}
        <p className="muted small">The 5-minute candles are fetched from Kite for the screened days only: a few thousand requests
          (about 25–30 minutes), once; candles already stored are re-used. After that the evening job adds each day and the
          next session's list.</p>
        {running ? <p><b>Running…</b> {prog.total ? `${prog.done} of ${prog.total} stocks` : ''} <span className="muted small">{prog.message}</span></p>
          : <><button className="btn primary" disabled={!d.kite} onClick={() => run(false)}>Run the backtest</button>
            {!d.kite && <span className="muted small"> &nbsp;Log in to Kite first (it needs 5-minute candles).</span>}
            {prog.message && <p className="muted small">{prog.message}</p>}</>}
        {err && <div className="warn small">{err}</div>}
      </div>);
  }
  const c = d.counts || {};
  const p = d.portfolio || {};
  const set$ = p.settings || {};
  const watch = d.watchlist || [];

  return (
    <div className={`orb ${loading ? 'busy' : ''}`}>
      <div className="orbfilters"><Chips items={SETS} value={which} onChange={setWhich} /></div>
      <div className="orbfilters"><span className="muted small">Period</span><Chips items={PERIODS} value={period} onChange={setPeriod} /></div>
      {err && <div className="warn small">{err}</div>}

      <div className="orbstatus">
        <span className="s"><b>{c.screened ?? 0}</b> screened</span><span className="ar">→</span>
        <span className="s"><b>{c.entered ?? 0}</b> entered</span>
        <span className="s"><b>{c.no_entry ?? 0}</b> ⛔ no breakout</span><span className="ar">→</span>
        <span className="s"><span className="up">{c.won ?? 0} won</span> · <span className="down">{c.lost ?? 0} lost</span> <span className="muted small">(after costs)</span></span>
        <span className="muted small orbmeta">Data to {day(d.as_of)} · updated {stamp(d.updated_at)}
          {prog.state !== 'idle' ? ` · ${prog.state}…` : <> · <button className="linkbtn" onClick={() => run(true)}>Re-run</button></>}</span>
      </div>

      <div className="orbkpis">
        <div className="k"><div className={`v ${cls(d.avg_net)}`}>{pct(d.avg_net, 2)}</div><div className="l">per trade after costs · {pct(d.avg_gross, 2)} before</div></div>
        <div className="k"><div className="v">{d.win_rate != null ? `${d.win_rate.toFixed(0)}%` : '—'}</div><div className="l">won after costs</div></div>
        <div className="k"><div className="v">{d.target_rate != null ? `${d.target_rate.toFixed(0)}%` : '—'}</div><div className="l">reached the {R.target_pct}% target</div></div>
        <div className="k"><div className="v up">{pct(d.avg_win, 2)}</div><div className="l">avg winner · <span className="down">{pct(d.avg_loss, 2)}</span> avg loser</div></div>
        <div className="k"><div className={`v ${cls(d.expectancy_r)}`}>{rr(d.expectancy_r)}</div><div className="l">expectancy (before costs)</div></div>
        <div className="k"><div className={`v ${d.profit_factor >= 1 ? 'up' : 'down'}`}>{d.profit_factor ?? '—'}</div><div className="l">profit factor (after costs)</div></div>
      </div>

      <div className="orbcmp">
        <div className="panel"><h3>₹10L on these rules vs Nifty 500</h3>
          <div className="muted small">{lakh(set$.capital)} · {((set$.risk_pct ?? 0.01) * 100).toFixed(1)}% risk per trade sized by the stop,
            max {Math.round((set$.position_cap ?? 0.2) * 100)}% per position, max {set$.max_positions} at once, after costs</div>
          <Curve p={p} />
          <div className="muted small"><span className="up"><b>━ Momentum ORB {pct(p.return)}</b></span> &nbsp; ╍ Nifty 500 {pct(p.nifty_return)} (buy and hold)
            {p.skipped ? ` · ${p.skipped} entries skipped (positions full or no cash)` : ''}</div></div>
        <div className="panel"><h3>Year 1 vs Year 2</h3>
          <table className="orbvs"><thead><tr><th /><th>{YEAR.y1}</th><th>{YEAR.y2}</th></tr></thead><tbody>
            {[['Trades', (y) => y.counts?.entered], ['Won after costs', (y) => (y.win_rate != null ? `${y.win_rate.toFixed(0)}%` : '—')],
              ['Reached target', (y) => (y.target_rate != null ? `${y.target_rate.toFixed(0)}%` : '—')],
              ['Per trade after costs', (y) => <b className={cls(y.avg_net)}>{pct(y.avg_net, 2)}</b>],
              ['Profit factor', (y) => y.profit_factor ?? '—']].map(([label, f]) => (
              <tr key={label}><td>{label}</td>{(d.by_year || []).map((y) => <td key={y.year}>{f(y)}</td>)}</tr>))}
          </tbody></table>
          <div className="muted small orbnote">A rule that only works in one year is luck. Both columns should be positive after costs.</div>
          <table className="orbvs"><tbody>
            <tr><td>{lakh(set$.capital)} became</td><td><b>{lakh(p.value)}</b></td></tr>
            <tr><td>Worst drawdown</td><td className="down">{pct(p.drawdown)} <span className="muted small">(Nifty 500 {pct(p.nifty_drawdown)})</span></td></tr>
            <tr><td>Months up</td><td>{p.months_up?.[1] ? `${p.months_up[0]} of ${p.months_up[1]}` : '—'}</td></tr>
          </tbody></table></div>
      </div>

      <div className="orbcmp">
        <div className="panel"><h3>By opening-range size <span className="muted small">· 9:15–9:45 AM range, % of its high</span></h3>
          <div className="tablewrap"><table className="grid"><thead><tr><th>Range</th><th className="num">Trades</th><th className="num">Won</th>
            <th className="num">Target</th><th className="num">Per trade after costs</th></tr></thead>
            <tbody>{(d.by_range || []).map((x) => (
              <tr key={x.bucket}><td>{x.bucket}</td><td className="num">{x.entered}</td>
                <td className="num">{x.win_rate != null ? `${x.win_rate.toFixed(0)}%` : '—'}</td>
                <td className="num">{x.target_rate != null ? `${x.target_rate.toFixed(0)}%` : '—'}</td>
                <td className={`num ${cls(x.avg_net)}`}><b>{pct(x.avg_net, 2)}</b></td></tr>))}</tbody></table></div></div>
        <div className="panel"><h3>How trades ended</h3>
          <table className="orbvs"><tbody>{(d.exits || []).map((x) => (
            <tr key={x.reason}><td>{x.reason}</td><td>{x.trades} <span className="muted small">({x.pct.toFixed(0)}%)</span></td></tr>))}
          </tbody></table></div>
      </div>

      {d.by_set?.length > 0 && (
        <div className="panel"><h3>Longs vs shorts <span className="muted small">· this period · tap a row to show it</span></h3>
          <div className="tablewrap"><table className="grid orbsets"><thead><tr><th>Set</th><th className="num">Trades</th>
            <th className="num">Won</th>{!phone && <th className="num">Target</th>}<th className="num">Per trade after costs</th>
            {!phone && <><th className="num">Year 1</th><th className="num">Year 2</th><th className="num">Profit factor</th></>}</tr></thead>
            <tbody>{d.by_set.map((x) => (
              <tr key={x.set} className={x.set === which ? 'cur' : ''} onClick={() => setWhich(x.set)}><td>{x.name}</td>
                <td className="num">{x.entered}</td><td className="num">{x.win_rate != null ? `${x.win_rate.toFixed(0)}%` : '—'}</td>
                {!phone && <td className="num">{x.target_rate != null ? `${x.target_rate.toFixed(0)}%` : '—'}</td>}
                <td className={`num ${cls(x.avg_net)}`}><b>{pct(x.avg_net, 2)}</b></td>
                {!phone && <><td className={`num ${cls(x.y1_net)}`}>{pct(x.y1_net, 2)} <span className="muted small">({x.y1_trades})</span></td>
                  <td className={`num ${cls(x.y2_net)}`}>{pct(x.y2_net, 2)} <span className="muted small">({x.y2_trades})</span></td>
                  <td className="num">{x.profit_factor ?? '—'}</td></>}</tr>))}</tbody></table></div></div>)}

      <div className="panel"><h3>Next session's lists <span className="muted small">· screened {day(d.as_of)} · 9:15–9:45 AM range, then the first 5-minute close beyond it</span></h3>
        {!watch.length ? <p className="muted">Nothing passed the screens.</p> : [['momentum', '▲▼ Momentum (long over the range, short under it)'], ['weak', '▼ 52-week lows (short under the range)']].map(([l, title]) => {
          const ws = watch.filter((w) => w.list === l);
          return ws.length ? <div key={l} className="orbwatchgrp"><div className="muted small">{title}</div><div className="orbwatch">{ws.map((w) => (
            <span key={w.symbol} className="spill"><b>{w.symbol}</b> {rs(w.close)} · RSI {w.rsi?.toFixed(0)} · {lakhs(w.volume)}</span>))}</div></div> : null;
        })}
      </div>

      <div className="panel"><h3>Trades <span className="muted small">· newest first</span></h3>
        <div className="orbfilters"><Chips items={STATUSES} value={status} onChange={setStatus} /></div>
        <div className="tablewrap"><table className="grid orbtrades"><thead><tr><th>Day</th><th>Stock</th>
          {!phone && <th className="num">30-min range</th>}<th className="num">Entry</th>{!phone && <th className="num">Stop · target</th>}
          <th className="num">After costs</th>{!phone && <th className="num">Before · R</th>}</tr></thead>
          <tbody>{trades.rows.map((t) => (
            <React.Fragment key={`${t.symbol}${t.signal_date}`}>
              <tr><td className="muted">{day(t.trade_date || t.signal_date)}</td><td><b>{t.symbol}</b> {t.side && <span className={t.side === 'long' ? 'up' : 'down'}>{t.side === 'long' ? '▲' : '▼'}</span>}{t.list === 'weak' && <span className="muted small"> 52w low</span>}</td>
                {!phone && <td className="num">{t.or_high ? <>{rs(t.or_low)}–{rs(t.or_high)}<div className="muted small">{pct(t.or_pct)}</div></> : '—'}</td>}
                <td className="num">{t.entry ? <>{rs(t.entry)}<div className="muted small">{ampm(t.entry_time)}</div></> : '—'}</td>
                {!phone && <td className="num">{t.stop ? <>{rs(t.stop)} · {rs(t.target)}</> : '—'}</td>}
                <td className={`num ${cls(t.net_pct)}`}><b>{t.net_pct != null ? pct(t.net_pct, 2) : '—'}</b></td>
                {!phone && <td className="num">{t.result_pct != null ? `${pct(t.result_pct, 2)} · ${rr(t.r)}` : '—'}</td>}</tr>
              <tr className="tl"><td /><td colSpan={phone ? 3 : 6}>{timeline(t)}</td></tr>
            </React.Fragment>))}</tbody></table></div>
        {trades.total > trades.rows.length && (
          <p className="orbmore"><button className="btn" onClick={more}>Show {Math.min(PAGE, trades.total - trades.rows.length)} more</button>
            <span className="muted small"> &nbsp;{trades.rows.length} of {trades.total}</span></p>)}
        {rulesText}
      </div>
    </div>
  );
}
