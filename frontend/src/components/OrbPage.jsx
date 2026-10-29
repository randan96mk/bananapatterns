import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { ampm, stamp } from '../timefmt';
import { useIsPhone } from '../useMedia';

const SCEN = [['normal', 'Normal slippage'], ['stress', 'Stressed slippage']];
const SPLITS = [['all', 'All'], ['dev', 'Development'], ['val', 'Validation'], ['oos', 'Out-of-sample']];
const SIDES = [['all', 'Both'], ['long', '▲ Longs'], ['short', '▼ Shorts']];
const EXIT = { stop: '🛑 stop', target: '🎯 target', close: '⏱ square-off' };
const PAGE = 100;

const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const rr = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}R`);
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down');
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const lakh = (v) => (v == null ? '—' : `₹${(v / 1e5).toFixed(2).replace(/\.00$/, '')}L`);
const inr = (v) => (v == null ? '—' : `${v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
const win = (v) => (v != null ? `${v.toFixed(0)}%` : '—');

function Chips({ items, value, onChange }) {
  return items.map(([id, label]) => (
    <button key={id} className={`spill ${value === id ? 'on' : ''}`} aria-pressed={value === id}
            onClick={() => onChange(id)}>{label}</button>));
}

// Account value (% of the capital) at normal (solid) and stressed (dashed) slippage.
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
    <svg className="orbcurve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Account value at normal and stressed slippage">
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

// The 🚀 ORB + VWAP tab: the 9:15-9:30 AM breakout with VWAP and relative volume, backtested on the most liquid
// Nifty 500 stocks through the account's limits; development / validation / out-of-sample at two slippages.
export default function OrbPage() {
  const phone = useIsPhone();
  const [scen, setScen] = useState('normal');
  const [split, setSplit] = useState('all');
  const [side, setSide] = useState('all');
  const [d, setD] = useState(null);
  const [trades, setTrades] = useState({ total: 0, rows: [] });
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true, timer;
    setLoading(true);
    const load = () => Promise.all([api.getOrb(), api.getOrbTrades(scen, split, side, PAGE, 0)])
      .then(([s, t]) => {
        if (!alive) return;
        setD(s); setTrades(t); setErr(''); setLoading(false);
        if (s.progress?.state !== 'idle') timer = setTimeout(load, 3000);   // running: refresh
      })
      .catch((e) => { if (alive) { setErr(e.message); setLoading(false); } });
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, [scen, split, side, tick]);

  const more = () => api.getOrbTrades(scen, split, side, PAGE, trades.rows.length)
    .then((t) => setTrades({ total: t.total, rows: [...trades.rows, ...t.rows] })).catch((e) => setErr(e.message));
  const run = (rebuild = false) => api.buildOrb(rebuild).then(() => setTick((x) => x + 1)).catch((e) => setErr(e.message));

  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;
  const prog = d.progress || {};
  const R = d.rules || {};
  const rulesText = (
    <p className="muted small">Universe: each day the {R.top_n} Nifty 500 stocks with the highest median daily turnover over the
      previous {R.turnover_days} sessions (previous close over ₹{R.min_price}). Opening range {ampm(R.range_start)}–{ampm(R.range_end)}.
      <b> Long</b>: a 5-minute close (by {ampm(R.entry_end)}) over the range high and over VWAP, VWAP higher than {R.vwap_slope_bars} candles
      earlier, volume at least {R.rvol_min}× the average of the previous {R.rvol_bars} candles. <b>Short</b>: the mirror. Entry at the next
      candle's open; stop the breakout candle's low − {R.stop_buffer_pct}% (high + for a short; at least one ₹{R.tick} tick); target {R.reward_r}R;
      a candle touching both counts as the stop; square-off at {ampm(R.exit_time)}. Account ₹{R.capital?.toLocaleString('en-IN')}:
      {' '}₹{R.risk_per_trade} risk a trade (charges included), at most ₹{R.allocated?.toLocaleString('en-IN')} in positions, {R.max_entries_per_day} entries
      a day, {R.max_concurrent} open, ₹{R.daily_loss_limit} daily loss limit. Zerodha intraday charges (as of {R.fees_as_of}) and {R.slippage_pct}%
      slippage a fill ({R.stress_slippage_pct}% stressed). Backtest only: no orders.</p>);

  if (!d.built) {
    const running = prog.state && prog.state !== 'idle';
    return (
      <div className="panel"><h3>🚀 ORB + VWAP</h3>
        <p>A 2-year backtest of the opening-range breakout with VWAP and relative volume, before any live alerts.</p>
        {rulesText}
        <p className="muted small">The 5-minute candles are fetched from Kite once (about a thousand requests, 10–15 minutes);
          candles already stored are re-used. After that the evening job adds each session.</p>
        {running ? <p><b>Running…</b> {prog.total ? `${prog.done} of ${prog.total} stocks` : ''} <span className="muted small">{prog.message}</span></p>
          : <><button className="btn primary" disabled={!d.kite} onClick={() => run(false)}>Run the backtest</button>
            {!d.kite && <span className="muted small"> &nbsp;Log in to Kite first (it needs 5-minute candles).</span>}
            {prog.message && <p className="muted small">{prog.message}</p>}</>}
        {err && <div className="warn small">{err}</div>}
      </div>);
  }
  const T = d.total?.[scen] || {};
  const other = scen === 'normal' ? 'stress' : 'normal';
  const cv = d.curve || {};
  const curve = { dates: cv.dates || [], strategy: cv[scen] || [], nifty: cv[other] || [] };
  const skipped = Object.entries(d.skipped?.[scen] || {});
  const row = (label, f) => <tr key={label}><td>{label}</td>{(d.splits || []).map((x) => <td key={x.split}>{f(x[scen])}</td>)}</tr>;

  return (
    <div className={`orb ${loading ? 'busy' : ''}`}>
      <div className="orbfilters"><Chips items={SCEN} value={scen} onChange={setScen} />
        <span className="muted small">{scen === 'stress' ? R.stress_slippage_pct : R.slippage_pct}% a fill</span></div>
      {err && <div className="warn small">{err}</div>}

      <div className="orbstatus">
        <span className="s"><b>{cv.dates?.length ?? 0}</b> sessions</span><span className="ar">→</span>
        <span className="s"><b>{d.scanned ?? 0}</b> stock-days</span><span className="ar">→</span>
        <span className="s"><b>{d.signals ?? 0}</b> signals</span><span className="ar">→</span>
        <span className="s"><b>{T.trades ?? 0}</b> trades <span className="muted small">({T.longs ?? 0} ▲ · {T.shorts ?? 0} ▼)</span></span>
        <span className="muted small orbmeta">Data to {day(d.as_of)} · updated {stamp(d.updated_at)}
          {prog.state !== 'idle' ? ` · ${prog.state}…` : <> · <button className="linkbtn" onClick={() => run(true)}>Re-run</button></>}</span>
      </div>

      <div className="orbkpis">
        <div className="k"><div className={`v ${cls(T.net)}`}>{inr(T.net)}</div><div className="l">net · {inr(T.gross)} gross − {inr((T.charges ?? 0) + (T.slippage ?? 0))} costs</div></div>
        <div className="k"><div className="v">{win(T.win_rate)}</div><div className="l">won after costs</div></div>
        <div className="k"><div className={`v ${T.profit_factor >= 1 ? 'up' : 'down'}`}>{T.profit_factor ?? '—'}</div><div className="l">profit factor</div></div>
        <div className="k"><div className={`v ${cls(T.expectancy)}`}>{inr(T.expectancy)}</div><div className="l">per trade · {rr(T.expectancy_r)}</div></div>
        <div className="k"><div className="v down">{inr(-(T.max_drawdown ?? 0))}</div><div className="l">worst drawdown · {pct(-(T.max_drawdown_pct ?? 0))}</div></div>
        <div className="k"><div className="v">{inr(T.avg_cost)}</div><div className="l">costs a trade (charges + slippage)</div></div>
      </div>

      <div className="orbcmp">
        <div className="panel"><h3>{lakh(R.capital)} on these rules</h3>
          <Curve p={curve} />
          <div className="muted small"><span className="up"><b>━ {SCEN.find((s) => s[0] === scen)[1]} {pct(T.return_pct)}</b></span>
            {' '}&nbsp; ╍ {SCEN.find((s) => s[0] === other)[1]} {pct(d.total?.[other]?.return_pct)}</div></div>
        <div className="panel"><h3>Development · validation · out-of-sample</h3>
          <div className="tablewrap"><table className="orbvs"><thead><tr><th />{(d.splits || []).map((x) => (
            <th key={x.split}>{x.name}<div className="muted small">{day(x.from)} – {day(x.to)}</div></th>))}</tr></thead><tbody>
            {row('Trades', (s) => s.trades)}
            {row('Won', (s) => win(s.win_rate))}
            {row('Net', (s) => <b className={cls(s.net)}>{inr(s.net)}</b>)}
            {row('Gross · costs', (s) => <span className="muted small">{inr(s.gross)} · {inr(s.charges + s.slippage)}</span>)}
            {row('Profit factor', (s) => s.profit_factor ?? '—')}
            {row('Per trade', (s) => <>{inr(s.expectancy)} <span className="muted small">{rr(s.expectancy_r)}</span></>)}
            {row('Worst drawdown', (s) => <span className="down">{inr(-s.max_drawdown)}</span>)}
            <tr><td>Net, {other === 'stress' ? 'stressed' : 'normal'} slippage</td>{(d.splits || []).map((x) => (
              <td key={x.split} className={cls(x[other].net)}>{inr(x[other].net)}</td>))}</tr>
          </tbody></table></div>
          <div className="muted small orbnote">The rules were fixed before the run (none tuned on this data). An edge must hold in all three columns, and still at stressed slippage.</div></div>
      </div>

      <div className="orbcmp">
        <div className="panel"><h3>Longs vs shorts</h3>
          <div className="tablewrap"><table className="grid"><thead><tr><th>Side</th><th className="num">Trades</th><th className="num">Won</th>
            <th className="num">Net</th><th className="num">Profit factor</th><th className="num">Net, stressed</th></tr></thead>
            <tbody>{(d.sides || []).map((x) => (
              <tr key={x.side}><td>{x.side === 'long' ? '▲ Longs' : '▼ Shorts'}</td><td className="num">{x[scen].trades}</td>
                <td className="num">{win(x[scen].win_rate)}</td><td className={`num ${cls(x[scen].net)}`}><b>{inr(x[scen].net)}</b></td>
                <td className="num">{x[scen].profit_factor ?? '—'}</td><td className={`num ${cls(x.stress.net)}`}>{inr(x.stress.net)}</td></tr>))}</tbody></table></div></div>
        <div className="panel"><h3>How trades ended · signals not taken</h3>
          <table className="orbvs"><tbody>
            {Object.entries(T.exits || {}).map(([k, v]) => <tr key={k}><td>{EXIT[k] || k}</td><td>{v}</td></tr>)}
            {skipped.map(([k, v]) => <tr key={k}><td className="muted">⛔ {k}</td><td>{v}</td></tr>)}
            {d.bad_days > 0 && <tr><td className="muted">⚠️ stock-days with bad or missing candles (no signal)</td><td>{d.bad_days}</td></tr>}
          </tbody></table></div>
      </div>

      <div className="panel"><h3>Trades <span className="muted small">· newest first</span></h3>
        <div className="orbfilters"><Chips items={SPLITS} value={split} onChange={setSplit} /><Chips items={SIDES} value={side} onChange={setSide} /></div>
        <div className="tablewrap"><table className="grid orbtrades"><thead><tr><th>Day</th><th>Stock</th>
          {!phone && <th className="num">Signal</th>}<th className="num">Entry</th>{!phone && <th className="num">Stop · target</th>}
          {!phone && <th className="num">Qty</th>}<th className="num">Net</th>{!phone && <th className="num">Gross · costs</th>}<th>Exit</th></tr></thead>
          <tbody>{trades.rows.map((t) => (
            <tr key={`${t.day}${t.symbol}${t.side}`}><td className="muted">{day(t.day)}</td>
              <td><b>{t.symbol}</b> <span className={t.side === 'long' ? 'up' : 'down'}>{t.side === 'long' ? '▲' : '▼'}</span></td>
              {!phone && <td className="num">{ampm(t.signal_time)}<div className="muted small">RVOL {t.rvol?.toFixed(1)}×</div></td>}
              <td className="num">{rs(t.entry_fill)}<div className="muted small">{ampm(t.entry_time)}</div></td>
              {!phone && <td className="num">{rs(t.stop)} · {rs(t.target)}</td>}
              {!phone && <td className="num">{t.qty}</td>}
              <td className={`num ${cls(t.net)}`}><b>{inr(t.net)}</b><div className="muted small">{rr(t.r)}</div></td>
              {!phone && <td className="num">{inr(t.gross)} · {inr(t.charges + t.slippage)}</td>}
              <td>{EXIT[t.reason] || t.reason} <span className="muted small">{ampm(t.exit_time)}</span></td></tr>))}</tbody></table></div>
        {trades.total > trades.rows.length && (
          <p className="orbmore"><button className="btn" onClick={more}>Show {Math.min(PAGE, trades.total - trades.rows.length)} more</button>
            <span className="muted small"> &nbsp;{trades.rows.length} of {trades.total}</span></p>)}
        {rulesText}
      </div>
    </div>
  );
}
