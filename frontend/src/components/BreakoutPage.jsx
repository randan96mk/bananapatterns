import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { useIsPhone } from '../useMedia';
import HowItWorks from './HowItWorks';
import AttentionStrip from './AttentionStrip';

const SCEN = [['normal', 'Normal slippage'], ['stress', 'Stressed slippage']];
const VARS = [['A', 'A · 200-day filter'], ['E', 'E · half filter']];
const EXIT = { stop: '🛑 stop', exit: '📉 trend exit', end: '⏹ end of test' };
const SKIP = { no_slot: 'no free slot', market_off: 'market filter off', too_small: 'too small (under 1 share)' };
const STATE = { ON: ['✅', 'up'], FULL: ['✅', 'up'], HALF: ['🟡', 'warn'], OFF: ['⛔', 'down'] };
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

export function FilterBadge({ k, f }) {
  if (!f) return null;
  const [icon, c] = STATE[f.state] || ['', ''];
  return (
    <span className="filterbadge"><b className={`v${k}`}>{k}</b> <b className={c}>{icon} {f.state}</b>
      {f.state === 'HALF' && <span className="muted small"> ({f.slots} slots)</span>}
      {f.changed && <span className="muted small"> · was {f.prev}</span>}</span>);
}

// Account value (₹): A (blue), E (purple), NIFTY 50 with the same capital (dashed).
function Curve({ dates, lines, nifty, capital }) {
  if (!dates?.length) return <p className="muted">No curve yet.</p>;
  const W = 560, H = 200, L = 46, T = 14, B = 24;
  const all = [...lines.flatMap((l) => l.vals), ...nifty, capital];
  const lo = Math.min(...all), hi = Math.max(...all);
  const n = dates.length;
  const x = (i) => L + ((W - L - 8) * i) / Math.max(1, n - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
  const line = (vals) => vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const raw = (hi - lo) / 6, mag = 10 ** Math.floor(Math.log10(raw || 1));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((v) => v >= raw);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v);
  const years = [];
  dates.forEach((d, i) => { if (i === 0 || d.slice(0, 4) !== dates[i - 1].slice(0, 4)) years.push([i, d]); });
  return (
    <svg className="orbcurve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Account value, A and E vs NIFTY 50">
      {ticks.map((v) => <g key={v}><line x1={L} x2={W - 8} y1={y(v)} y2={y(v)} className="grid" />
        <text x={L - 6} y={y(v) + 3} textAnchor="end">{lakh(v)}</text></g>)}
      {years.map(([i, d]) => <text key={d} x={x(i)} y={H - 6} textAnchor="middle">{d.slice(0, 4)}</text>)}
      <polyline points={line(nifty)} className="nifty" />
      {lines.map((l) => <polyline key={l.k} points={line(l.vals)} className={`var${l.k}`} />)}
    </svg>
  );
}

// The 📈 Trend Breakout tab: A (200-day filter) and E (half filter) side by side; the evening scan. Signals only.
export default function BreakoutPage() {
  const phone = useIsPhone();
  const [scen, setScen] = useState('normal');
  const [tv, setTv] = useState('A');
  const [d, setD] = useState(null);
  const [trades, setTrades] = useState({ total: 0, rows: [] });
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true, timer;
    setLoading(true);
    const load = () => Promise.all([api.getBreakout(), api.getBreakoutTrades(tv, scen, PAGE, 0)])
      .then(([s, t]) => {
        if (!alive) return;
        setD(s); setTrades(t); setErr(''); setLoading(false);
        if (s.progress?.state !== 'idle') timer = setTimeout(load, 3000);   // running: refresh
      })
      .catch((e) => { if (alive) { setErr(e.message); setLoading(false); } });
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, [scen, tv, tick]);

  const more = () => api.getBreakoutTrades(tv, scen, PAGE, trades.rows.length)
    .then((t) => setTrades({ total: t.total, rows: [...trades.rows, ...t.rows] })).catch((e) => setErr(e.message));
  const run = () => api.runBreakout().then(() => setTick((x) => x + 1)).catch((e) => setErr(e.message));

  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;
  const prog = d.progress || {};
  const R = d.rules || {};
  const running = prog.state && prog.state !== 'idle';
  const runLine = running ? <p><b>Running…</b> <span className="muted small">{prog.message}</span></p>
    : <><button className="btn primary" onClick={run}>{d.built ? 'Re-run' : 'Run the backtest'}</button>
      {prog.message && <span className="muted small"> &nbsp;{prog.message}</span>}</>;

  if (!d.built) {
    return (
      <>
        <AttentionStrip tab="breakout" />
        <div className="panel"><h3>📈 Trend Breakout · A vs E</h3>
          <p>A 6-year swing backtest of two variants side by side (A: 200-day market filter, E: half filter), from the stored daily candles
            (no 5-minute data, no Kite requests), plus the evening scan. Re-run once to see both.</p>
          {runLine}{err && <div className="warn small">{err}</div>}
        </div>
        <HowItWorks r={R} />
      </>);
  }
  const r = d.run;
  const V = r.variants;
  const keys = Object.keys(V);
  const S = (k) => V[k][scen];
  const sc = d.scan;
  const row = (label, f, extra) => (
    <tr key={label}><td>{label}</td>{keys.map((k) => <td key={k} className="num">{f(V[k], k)}</td>)}<td className="num muted">{extra ?? ''}</td></tr>);

  return (
    <div className={`orb ${loading ? 'busy' : ''}`}>
      <AttentionStrip tab="breakout" />
      <div className="orbfilters"><Chips items={SCEN} value={scen} onChange={setScen} />
        <span className="muted small">{scen === 'stress' ? R.stress_slippage_pct : R.slippage_pct}% a fill</span>
        <span className="muted small orbmeta">{day(r.from)} → {day(r.to)} · {r.sessions} sessions · {r.stocks} stocks · updated {stamp(r.updated_at)}
          {running ? <> · <span className="pulse running" /> running…</> : <> · <button className="linkbtn" onClick={run}>Re-run</button></>}</span></div>
      {err && <div className="warn small">{err}</div>}

      <div className="panel">
        <b>{keys.map((k) => `${V[k].verdict ? '✅' : '❌'} ${k}`).join(' · ')}</b>
        <span className="muted"> · ✅ = profitable after costs in all three periods (stressed too) and beats NIFTY 50</span>
        <div className="caveat small">⚠️ Universe is today's Nifty 500 list (survivorship bias): real results will be somewhat lower.
          {' '}E's out-of-sample gain comes mostly from 2026. Past results don't guarantee future returns.</div>
      </div>

      <div className="orbcmp">
        <div className="panel"><h3>Results <span className="muted small">· {scen === 'stress' ? 'stressed' : 'normal'} slippage</span></h3>
          <div className="tablewrap"><table className="orbvs"><thead><tr><th />{keys.map((k) => <th key={k} className={`num v${k}`}>{V[k].name}</th>)}
            <th className="num">NIFTY 50</th></tr></thead><tbody>
            {row('Net after costs', (v) => <b className={cls(v[scen].net)}>{inr(v[scen].net)}</b>, inr(r.nifty.return / 100 * R.capital))}
            {row('Before costs · costs', (v) => <span className="muted">{inr(v[scen].gross)} · −{inr(v[scen].costs)}</span>)}
            {row('CAGR after costs', (v) => <b>{pct(v[scen].cagr)}</b>, pct(r.nifty.cagr))}
            {row('CAGR stressed', (v) => pct(v.stress.cagr))}
            {row('Worst drawdown', (v) => <span className="down">{pct(-v[scen].drawdown, 0)}</span>, pct(-r.nifty.drawdown, 0))}
            {row('Trades · won', (v) => `${v[scen].trades} · ${v[scen].win_rate.toFixed(0)}%`)}
            {row('Profit factor', (v) => v[scen].profit_factor ?? '—')}
            {row('Average hold', (v) => `${v[scen].avg_hold.toFixed(0)} days`)}
            {row('Costs a trade', (v) => inr(v[scen].cost_per_trade))}
            {row('New buys allowed', (v) => `${v.allowed_pct.toFixed(0)}% of days`)}
            {row('Robustness', (v) => `${v.robust_pass} of ${v.robust.length} pass`)}
          </tbody></table></div></div>
        <div className="panel"><h3>{lakh(R.capital)} on each <span className="muted small">· {scen === 'stress' ? 'stressed' : 'normal'} slippage</span></h3>
          <Curve dates={r.curve.dates} nifty={r.curve.nifty} capital={R.capital}
                 lines={keys.map((k) => ({ k, vals: V[k].curve[scen] }))} />
          <div className="small">{keys.map((k) => <span key={k} className={`v${k}`}><b>━ {k} {pct(S(k).cagr)}</b> &nbsp; </span>)}
            <span className="muted">╍ NIFTY 50 {pct(r.nifty.cagr)} (CAGR)</span></div></div>
      </div>

      <div className="orbcmp">
        <div className="panel"><h3>Development · validation · out-of-sample</h3>
          <div className="tablewrap"><table className="orbvs"><thead><tr><th />{V[keys[0]].splits.map((x) => (
            <th key={x.name} className="num">{x.name}<div className="muted small">{day(x.from)} – {day(x.to)}</div></th>))}</tr></thead><tbody>
            {keys.map((k) => (
              <React.Fragment key={k}>
                <tr><td><b className={`v${k}`}>{k}</b> after costs</td>{V[k].splits.map((x) => <td key={x.name} className="num"><b className={cls(x.net)}>{pct(x.net)}</b></td>)}</tr>
                <tr><td className="muted">&nbsp;&nbsp;before · costs · stressed</td>{V[k].splits.map((x) => (
                  <td key={x.name} className="num muted small">{pct(x.gross)} · {pct(-x.costs)} · {pct(x.stress)}</td>))}</tr>
              </React.Fragment>))}
            <tr><td>NIFTY 50</td>{V[keys[0]].splits.map((x) => <td key={x.name} className="num muted">{pct(x.nifty)}</td>)}</tr>
          </tbody></table></div>
          <div className="muted small orbnote">The first 50% / next 25% / last 25% of the test sessions. An edge must hold in all three, at stressed slippage too.</div></div>
        <div className="panel"><h3>Year by year <span className="muted small">· after costs</span></h3>
          <table className="orbvs"><thead><tr><th>Year</th>{keys.map((k) => <th key={k} className={`num v${k}`}>{k}</th>)}<th className="num">NIFTY 50</th></tr></thead><tbody>
            {V[keys[0]].years.map((y, i) => <tr key={y.year}><td>{y.year}</td>
              {keys.map((k) => <td key={k} className={`num ${cls(V[k].years[i].net)}`}>{pct(V[k].years[i].net)}</td>)}
              <td className={`num ${cls(y.nifty)}`}>{pct(y.nifty)}</td></tr>)}</tbody></table></div>
      </div>

      <div className="panel"><h3>Robustness <span className="muted small">· one setting changed at a time ·
        {keys.map((k) => <span key={k} className={`v${k}`}> {k}: {V[k].robust_pass} of {V[k].robust.length} pass</span>)}</span></h3>
        <p className="muted small">✅ = profitable after costs in all three periods, stressed too, and beats NIFTY 50. Under 75% passing means a modest,
          lumpy edge: expect real results to vary.</p>
        <div className="tablewrap"><table className="grid"><thead><tr><th>Changed</th>
          {keys.map((k) => <React.Fragment key={k}><th className={`num v${k}`}>{k} trades</th><th className={`num v${k}`}>{k} CAGR</th>
            {!phone && <th className={`num v${k}`}>{k} Dev / Val / OOS</th>}<th className={`v${k}`}>{k}</th></React.Fragment>)}</tr></thead>
          <tbody>{V[keys[0]].robust.map((x, i) => (
            <tr key={x.label}><td>{x.label}</td>
              {keys.map((k) => { const y = V[k].robust[i]; return (
                <React.Fragment key={k}><td className="num">{y.trades}</td><td className={`num ${cls(y.cagr)}`}>{pct(y.cagr)}</td>
                  {!phone && <td className="num">{y.periods.map((v) => pct(v, 0)).join(' / ')}</td>}<td>{y.ok ? '✅' : '❌'}</td></React.Fragment>); })}
            </tr>))}</tbody></table></div>
      </div>

      <div className="panel"><h3>How trades ended · signals not taken</h3>
        <table className="orbvs"><thead><tr><th />{keys.map((k) => <th key={k} className={`num v${k}`}>{k}</th>)}</tr></thead><tbody>
          {Object.keys(EXIT).map((e) => <tr key={e}><td>{EXIT[e]}</td>{keys.map((k) => <td key={k} className="num">{S(k).exits?.[e] ?? 0}</td>)}</tr>)}
          {Object.keys(SKIP).map((e) => <tr key={e}><td className="muted">⛔ {SKIP[e]}</td>{keys.map((k) => <td key={k} className="num">{S(k).skipped?.[e] ?? 0}</td>)}</tr>)}
        </tbody></table></div>

      <div className="panel"><h3>Tonight's scan <span className="muted small">· signals only, no orders{sc ? ` · ${day(sc.as_of)} · ${stamp(sc.computed_at)}` : ''}</span></h3>
        {!sc ? <p className="muted">Runs after the evening sync (and with Re-run).</p> : <>
          <p>Market filter: {Object.entries(sc.filters || {}).map(([k, f]) => <FilterBadge key={k} k={k} f={f} />)}
            <span className="muted small"> · NIFTY 50 {sc.nifty?.toLocaleString('en-IN')} · 50-day {sc.nifty_sma50?.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
              {' '}· 200-day {sc.nifty_sma200?.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span></p>
          {sc.buys?.length ? <div className="tablewrap"><table className="grid"><thead><tr><th>#</th><th>Breakout (next open)</th><th className="num">Close</th>
            <th className="num">Stop</th><th className="num">Qty · value</th><th className="num">₹ at risk</th></tr></thead>
            <tbody>{sc.buys.map((b, k) => <tr key={b.symbol}><td className="muted">{k + 1}</td><td><b>{b.symbol}</b>
              {!phone && <span className="muted small"> 6M {pct(b.ret126, 0)}</span>}</td><td className="num">{rs(b.close)}</td>
              <td className="num">{rs(b.stop)} <span className="muted small">{b.stop_pct.toFixed(1)}%</span></td>
              <td className="num">{b.qty} · {inr(b.value)}</td><td className="num down">{inr(b.risk)}</td></tr>)}</tbody></table></div>
            : <p className="muted">No breakouts today.</p>}
          {sc.buys?.length > 0 && <p className="muted small">{Object.entries(sc.filters || {}).map(([k, f]) => `${k}: ${f.state === 'OFF' ? 'not bought (filter off)'
            : f.state === 'HALF' ? `buy only while under ${f.slots} positions` : 'buy at the next open'}`).join(' · ')}</p>}
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
        <div className="orbfilters"><Chips items={VARS} value={tv} onChange={setTv} /></div>
        <div className="tablewrap"><table className="grid orbtrades"><thead><tr><th>Stock</th><th>Entry</th><th>Exit</th>
          <th className="num">Days</th>{!phone && <th className="num">Before costs</th>}{!phone && <th className="num">Costs</th>}<th className="num">After costs</th><th>Exit reason</th></tr></thead>
          <tbody>{trades.rows.map((t) => (
            <tr key={`${t.sym}${t.in}`}><td><b>{t.sym}</b> <span className="muted small">{t.qty} sh</span></td>
              <td>{day(t.in)}<div className="muted small">{rs(t.buy)}</div></td>
              <td>{day(t.out)}<div className="muted small">{rs(t.sell)}</div></td>
              <td className="num">{t.days}</td>
              {!phone && <td className={`num ${cls(t.gross)}`}>{inr(t.gross)}</td>}
              {!phone && <td className="num muted">{inr(t.costs)}</td>}
              <td className={`num ${cls(t.net)}`}><b>{inr(t.net)}</b></td><td>{EXIT[t.why] || t.why}</td></tr>))}</tbody></table></div>
        {trades.total > trades.rows.length && (
          <p className="orbmore"><button className="btn" onClick={more}>Show {Math.min(PAGE, trades.total - trades.rows.length)} more</button>
            <span className="muted small"> &nbsp;{trades.rows.length} of {trades.total}</span></p>)}
      </div>
      <HowItWorks r={R} />
    </div>
  );
}
