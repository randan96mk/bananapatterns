import React, { useEffect, useRef, useState } from 'react';
import * as api from '../services/api';
import { useIsPhone } from '../useMedia';
import { inr, pct, cls } from '../fmt';

// 🧪 Intraday Flow backtest: saved setups, a run over 6 months / 1 / 2 years, one setup's report or several side by side.
const COLORS = ['#f5c518', '#4ea1ff', '#c58af9', '#26a69a', '#ff8a65', '#90a4ae'];
const PERIODS = [['6m', '6 months'], ['1y', '1 year'], ['2y', '2 years']];
const EXIT = { stop: '🛑 stop', target: '🎯 target', 'trailing stop': '🪜 trail', 'time stop': '⏱ time stop', 'square-off': '🔔 square-off', 'end of day': '⏹ end of day' };
const day = (d) => (d ? new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' }) : '');

function Lines({ series, h = 150 }) {          // [{color, values, dash}] on one scale
  const all = series.flatMap((s) => s.values.filter((v) => v != null));
  if (!all.length) return null;
  const lo = Math.min(...all), hi = Math.max(...all), n = Math.max(...series.map((s) => s.values.length));
  const y = (v) => h - 6 - ((v - lo) / (hi - lo || 1)) * (h - 12);
  return <svg width="100%" height={h} viewBox={`0 0 600 ${h}`} preserveAspectRatio="none">{series.map((s, k) => (
    <polyline key={k} fill="none" stroke={s.color} strokeWidth={s.dash ? 1 : 2} strokeDasharray={s.dash ? '4 3' : ''} vectorEffect="non-scaling-stroke"
      points={s.values.map((v, i) => (v == null ? '' : `${(i / Math.max(n - 1, 1)) * 600},${y(v)}`)).join(' ')} />))}</svg>;
}

const Agg = ({ rows }) => (
  <table className="grid"><thead><tr><th /><th className="num">trades</th><th className="num">won</th><th className="num">net</th></tr></thead>
    <tbody>{Object.entries(rows || {}).map(([k, a]) => <tr key={k}><td>{k}</td><td className="num">{a.trades}</td>
      <td className="num">{a.win_rate ?? '—'}%</td><td className={`num ${cls(a.net)}`}>{inr(a.net)}</td></tr>)}</tbody></table>);

// Plain profit → slippage → charges → what is left: the same trades before and after costs.
function Charges({ r }) {
  const c = r.charges;
  if (!c) return null;
  const row = (label, v, per, note, strong) => <tr><td>{strong ? <b>{label}</b> : label}{note && <div className="muted small">{note}</div>}</td>
    <td className={`num ${cls(v)}`}>{strong ? <b>{inr(v)}</b> : inr(v)}</td><td className={`num ${cls(per)}`}>{inr(per, 2)}</td></tr>;
  return <div className="panel"><b>💸 Before vs after charges</b> <span className="muted small">· the same {r.trades} trades</span>
    <div className="tablewrap"><table className="grid"><thead><tr><th /><th className="num">total</th><th className="num">a trade</th></tr></thead><tbody>
      {row('Plain profit', c.plain, c.per_trade.plain, 'exact signal prices, no costs', true)}
      {row('− Slippage', -c.slip, -c.per_trade.slip, '0.05% on every fill')}
      {row('− Brokerage', -(c.brk || 0), -(c.per_trade.brk || 0), 'per order, with its 18% GST')}
      {row('− Govt charges', -(c.gov || 0), -(c.per_trade.gov || 0), 'STT, exchange, SEBI, GST on them, stamp duty')}
      {row('= Profit after costs', c.net, c.per_trade.net, `stressed slippage (+0.10% a fill): ${inr(r.stress.net)}`, true)}
    </tbody></table></div>
    <div className="small">Won <b>{c.win_rate_plain ?? '—'}%</b> before → <b>{r.win_rate ?? '—'}%</b> after · profit factor <b>{c.pf_plain ?? '—'}</b> → <b>{r.profit_factor ?? '—'}</b>
      {' '}· CAGR <b className={cls(c.cagr_plain)}>{pct(c.cagr_plain)}</b> → <b className={cls(r.cagr)}>{pct(r.cagr)}</b>
      {c.share != null && <> · costs take <b>{c.share.toFixed(0)}%</b> of the plain profit</>}
      {c.plain > 0 && c.net <= 0 && <div className="warn small">⚠️ Profitable before costs, a loss after: too many small trades for the edge they have.</div>}</div></div>;
}

const held = (a, b) => { const m = (t) => +t.slice(0, 2) * 60 + +t.slice(3, 5); const x = m(b) - m(a); return x >= 60 ? `${Math.floor(x / 60)}h ${x % 60}m` : `${x}m`; };
const n2 = (v) => (v == null ? '—' : (+v).toLocaleString('en-IN', { maximumFractionDigits: 2 }));
const Exits = ({ t }) => (t.exits || []).map((e, i) => <div key={i} className="small">{e.t} · {e.qty} @ {n2(e.price)}
  <span className="muted"> {i < t.exits.length - 1 ? 'partial' : (EXIT[e.why] || e.why)}</span></div>);
const COLS = [['Day', (t) => day(t.day)], ['Stock', (t) => <b>{t.symbol}</b>], ['Side', (t) => <span className={t.side === 'long' ? 'up' : 'down'}>{t.side}</span>],
  ['Setup', (t) => t.setup || '—'], ['Signal', (t) => t.signal_t || '—'], ['Signal ₹', (t) => n2(t.signal_price), 1], ['Entry', (t) => t.entry_t],
  ['Entry ₹', (t) => n2(t.entry), 1], ['Qty', (t) => t.qty, 1], ['Value', (t) => inr(t.entry * t.qty), 1], ['Stop', (t) => n2(t.sl), 1],
  ['Target', (t) => n2(t.target), 1], ['Exit(s)', (t) => <Exits t={t} />], ['Exit ₹ avg', (t) => n2(t.exit), 1],
  ['Exit reason', (t) => EXIT[t.reason] || t.reason], ['Held', (t) => held(t.entry_t, t.exit_t), 1],
  ['Plain', (t) => <span className={cls(t.plain)}>{inr(t.plain)}</span>, 1], ['Slippage', (t) => inr(-(t.slip || 0)), 1],
  ['Brokerage', (t) => inr(-(t.brk || 0)), 1], ['Govt', (t) => inr(-(t.gov || 0)), 1],
  ['Net', (t) => <b className={cls(t.net)}>{inr(t.net)}</b>, 1], ['R', (t) => t.r ?? '—', 1]];

function Trades({ r, csv }) {
  const phone = useIsPhone();
  const rows = r.rows.slice(0, 300);
  return <div className="panel"><b>📋 Trades</b> <span className="muted small">· {r.trades} · newest first{rows.length < r.trades ? ` · showing ${rows.length}, the CSV has all` : ''}
    {!phone && ' · scroll sideways for every column'}</span>
    {r.split_costs && <a className="btn" style={{ float: 'right' }} href={csv(r.setup, r.period)} download>⬇ CSV (all trades)</a>}
    {phone ? <div className="pcards">{rows.map((t, i) => <details key={i} className="pcard"><summary className="t"><span><b>{t.symbol}</b> <span className="muted small">{day(t.day)} · {t.side}</span></span>
      <b className={cls(t.net)}>{inr(t.net)}</b></summary>
      <table className="grid small"><tbody>{COLS.slice(3).map(([h, f]) => <tr key={h}><td className="muted">{h}</td><td className="num">{f(t)}</td></tr>)}</tbody></table></details>)}</div>
      : <div className="tablewrap"><table className="grid"><thead><tr>{COLS.map(([h, , n]) => <th key={h} className={n ? 'num' : ''}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((t, i) => <tr key={i}>{COLS.map(([h, f, n]) => <td key={h} className={n ? 'num' : ''} style={{ whiteSpace: 'nowrap' }}>{f(t)}</td>)}</tr>)}</tbody></table></div>}
  </div>;
}

// ⚙️ Report as if: capital, brokerage (% and / or ₹ per order; 0 or empty = none), slippage and govt charges on / off.
const COSTS0 = { capital: '', brk_pct: '0.03', brk_rs: '20', slip: true, gov: true };
function CostsBar({ c, set, runCap }) {
  const num = (k, w, ph) => <input className="input" style={{ width: w, textAlign: 'right' }} inputMode="decimal" placeholder={ph} value={c[k]}
    onChange={(e) => set({ ...c, [k]: e.target.value.replace(/[^0-9.]/g, '') })} />;
  return <div className="panel bttools small">
    <b>⚙️ Report as if</b>
    <span>Capital ₹ {num('capital', 110, runCap ? String(runCap) : 'as run')}</span>
    <span>Brokerage {num('brk_pct', 64, '0')} % and {num('brk_rs', 64, '0')} ₹ per order</span>
    <label><input type="checkbox" checked={c.slip} onChange={(e) => set({ ...c, slip: e.target.checked })} /> Slippage 0.05%</label>
    <label><input type="checkbox" checked={c.gov} onChange={(e) => set({ ...c, gov: e.target.checked })} /> Govt charges</label>
    <button className="linkbtn" onClick={() => set({ ...c, brk_pct: '', brk_rs: '' })}>₹0 brokerage</button>
    <button className="linkbtn" onClick={() => set(COSTS0)}>↺ reset</button>
    <div className="muted" style={{ width: '100%' }}>Both brokerage boxes set: the lower of the two an order · one set: that one · 0 or empty: none.
      {' '}Changes every number, chart and trade below, no re-run (another capital scales the quantities, whole shares). Remembered on this device.</div>
  </div>;
}

function Report({ r, color, csv }) {
  return <>
    <div className="summary" style={{ marginBottom: 12 }}>
      <div className="k"><div className={`v ${cls(r.net)}`}>{inr(r.net)}</div><div className="l">net after costs on {inr(r.capital)} · {pct(r.return)}</div></div>
      <div className="k"><div className="v">{pct(r.cagr)}</div><div className="l">CAGR · stressed {pct(r.stress.cagr)} · NIFTY {pct(r.nifty)}</div></div>
      <div className="k"><div className="v down">{pct(-r.drawdown)}</div><div className="l">worst drawdown</div></div>
      <div className="k"><div className="v">{r.trades} · {r.win_rate ?? '—'}%</div><div className="l">trades · won</div></div>
      <div className="k"><div className="v">{r.profit_factor ?? '—'}</div><div className="l">profit factor · avg {r.avg_r ?? '—'} R</div></div>
      <div className="k"><div className="v">{inr(r.costs)}</div><div className="l">costs · {inr(r.trades ? r.costs / r.trades : 0)} a trade</div></div>
    </div>
    <Charges r={r} />
    <div className="panel"><b>Account</b> <span className="muted small">· <b style={{ color }}>after costs</b> · green: plain (before costs) · dashed: stressed and NIFTY 50 · {day(r.from)} – {day(r.to)}</span>
      <Lines series={[{ color: '#8b98a5', values: r.curve.nifty, dash: true }, { color, values: r.curve.stress, dash: true },
        ...(r.curve.plain ? [{ color: '#26a69a', values: r.curve.plain }] : []), { color, values: r.curve.net }]} />
      <div className="small">{r.splits.map((s) => <span key={s.name} style={{ marginRight: 14 }}>{s.name} <b className={cls(s.net)}>{pct(s.net)}</b>
        <span className="muted"> (stressed {pct(s.stress)})</span></span>)} {r.ok_periods ? '✅ all periods positive' : '❌ not every period positive'}</div></div>
    <div className="flowcols">
      <div className="panel"><b>By side</b><Agg rows={r.by_side} /><b>By entry time</b><Agg rows={r.by_time} /></div>
      <div className="panel"><b>By month</b> <span className="muted small">· net</span><div>{Object.entries(r.by_month).map(([m, a]) => (
        <span key={m} className={`chip ${cls(a.net)}`}>{m} {inr(a.net)}</span>))}</div>
        <p className="small"><b>Exits</b> {Object.entries(r.exits).map(([k, n]) => `${EXIT[k] || k} ${n}`).join(' · ')}</p>
        <p className="muted small">{r.covered} of {r.sessions} sessions had saved 1-minute data{r.missing ? ` · ${r.missing} stock-days missing (log in to Kite and re-run to fetch them)` : ''} · ran {r.ran_at?.slice(0, 16).replace("T", " ")}</p></div>
    </div>
    <Trades r={r} csv={csv} />
  </>;
}

const ROWS = [
  ['Plain profit (before costs)', (r) => r.charges?.plain, (r) => inr(r.charges?.plain)],
  ['− Slippage − charges', null, (r) => (r.charges ? `${inr(-(r.charges.slip + r.charges.costs))} · ${inr(-(r.charges.per_trade.slip + r.charges.per_trade.costs), 0)} a trade` : '—')],
  ['Net after costs', (r) => r.net, (r) => <b>{inr(r.net)}</b>], ['CAGR · stressed', (r) => r.cagr, (r) => `${pct(r.cagr)} · ${pct(r.stress.cagr)}`],
  ['Worst drawdown', (r) => -r.drawdown, (r) => pct(-r.drawdown)], ['Trades · won', null, (r) => `${r.trades} · ${r.win_rate ?? '—'}%`],
  ['Profit factor · avg R', (r) => r.profit_factor, (r) => `${r.profit_factor ?? '—'} · ${r.avg_r ?? '—'}`],
  ['Long · short net', null, (r) => `${inr(r.by_side.long?.net)} · ${inr(r.by_side.short?.net)}`],
  ['Dev / Val / OOS', null, (r) => r.splits.map((s) => pct(s.net)).join(' / ')],
  ['Verdict', null, (r) => (r.ok_periods ? '✅ all periods +' : '❌ a period −')],
];

function Compare({ rs, colors, onOpen }) {
  return <div className="panel"><b>Side by side</b> <span className="muted small">· best in green · click a column to open its report</span>
    <div className="tablewrap"><table className="grid"><thead><tr><th />{rs.map((r) => <th key={r.setup} className="num" style={{ color: colors[r.setup], cursor: 'pointer' }}
      onClick={() => onOpen(r.setup)}>● {r.setup}</th>)}</tr></thead>
      <tbody>{ROWS.map(([label, val, show]) => {
        const best = val ? Math.max(...rs.map((r) => val(r) ?? -Infinity)) : null;
        return <tr key={label}><td>{label}</td>{rs.map((r) => <td key={r.setup} className="num" style={val && rs.length > 1 && val(r) === best ? { background: 'rgba(38,166,154,.14)' } : {}}>{show(r)}</td>)}</tr>;
      })}</tbody></table></div>
    <Lines series={rs.map((r) => ({ color: colors[r.setup], values: r.curve.net })).concat(rs[0] ? [{ color: '#8b98a5', values: rs[0].curve.nifty, dash: true }] : [])} h={130} />
  </div>;
}

export default function FlowBacktest() {
  const [d, setD] = useState(null);
  const [period, setPeriod] = useState('1y');
  const [sel, setSel] = useState([]);
  const [focus, setFocus] = useState(null);
  const [err, setErr] = useState('');
  const [costs, setCosts] = useState(() => { try { return { ...COSTS0, ...JSON.parse(localStorage.getItem('bt.costs') || '{}') }; } catch { return COSTS0; } });
  const qs = new URLSearchParams({ slip: costs.slip, gov: costs.gov, brk_pct: +costs.brk_pct || 0, brk_rs: +costs.brk_rs || 0,
    ...(+costs.capital >= 1000 ? { capital: +costs.capital } : {}) }).toString();
  const csv = (setup, per) => `/api/flow/backtest/csv?${qs}&${new URLSearchParams({ setup, period: per })}`;
  const was = useRef(null);
  const load = () => api.flowBacktest(qs).then((x) => {                 // every period's results; filtered below
    if (was.current === 'running' && x.status.state !== 'running' && x.status.period) setPeriod(x.status.period);   // a run ended: show it
    if (was.current === null && x.results.length) setPeriod(x.results[0].period);   // opened: the latest run's period
    was.current = x.status.state;
    setD(x);
    setSel((s) => (s.length ? s.filter((n) => n in x.setups) : Object.keys(x.setups).slice(0, 1)));
  }).catch((e) => setErr(e.message));
  useEffect(() => {
    try { localStorage.setItem('bt.costs', JSON.stringify(costs)); } catch { /* private mode */ }
    const id = setTimeout(load, 350);                        // as you type: the report is rebuilt for these costs
    return () => clearTimeout(id);
  }, [qs]);
  useEffect(() => {
    if (d?.status.state !== 'running') return undefined;
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [d?.status.state]);
  if (!d) return <div className="panel muted">Loading…</div>;
  const names = Object.keys(d.setups);
  const colors = Object.fromEntries(names.map((n, i) => [n, COLORS[i % COLORS.length]]));
  const results = d.results.filter((r) => sel.includes(r.setup) && r.period === period);
  const other = [...new Set(d.results.filter((r) => sel.includes(r.setup) && r.period !== period).map((r) => r.period))];
  const shown = focus && results.find((r) => r.setup === focus);
  const running = d.status.state === 'running';
  const act = (p) => p.then(load).catch((e) => setErr(e.message));
  const save = () => { const n = window.prompt('Name this setup (a copy of every box\'s settings on the canvas):'); if (n) act(api.flowSetupSave(n)); };

  return <>
    <div className="panel bttools">
      <span className="seg">{PERIODS.map(([k, l]) => <button key={k} className={period === k ? 'active' : ''} onClick={() => setPeriod(k)}>{l}</button>)}</span>
      <button className="btn primary" disabled={running || !sel.length} onClick={() => act(api.flowBacktestRun(sel, period))}>
        {running ? 'Running…' : `▶ Run ${sel.length > 1 ? `selected (${sel.length})` : 'backtest'}`}</button>
      <button className="btn" onClick={save}>💾 Save canvas as setup…</button>
      <div style={{ width: '100%' }}>{names.map((n) => (
        <label key={n} className="chip" style={{ borderColor: colors[n] }}>
          <input type="checkbox" checked={sel.includes(n)} aria-label={n} onChange={(e) => setSel(e.target.checked ? [...sel, n] : sel.filter((x) => x !== n))} />
          {' '}<b style={{ color: colors[n] }}>{n}</b>
          {d.setups[n] && <> <button className="linkbtn small" onClick={() => window.confirm(`Put "${n}" on the canvas? The live flow uses it from the next candle.`) && act(api.flowSetupLoad(n))}>load on canvas</button>
            {!d.setups[n].preset && <button className="linkbtn small" onClick={() => window.confirm(`Delete setup "${n}" and its results?`) && act(api.flowSetupDelete(n))}>delete</button>}</>}
        </label>))}</div>
    </div>
    {names.filter((n) => d.setups[n]?.preset && sel.includes(n)).map((n) => <p key={n} className="muted small panel">📚 <b>{n}</b>: {d.setups[n].desc}</p>)}
    {err && <div className="warn small">{err}</div>}
    {results.length > 0 && <CostsBar c={costs} set={setCosts} runCap={results[0].run_capital || results[0].capital} />}
    {results.some((r) => !r.split_costs) && <p className="warn small panel">A result from before the cost options: re-run it to change its costs or capital and to download its CSV.</p>}
    {(running || d.status.message) && <div className="panel small">{running ? <b>Running… </b> : null}{d.status.message}
      {running && <div className="bar" style={{ marginTop: 6 }}><i style={{ width: `${d.status.pct}%` }} /></div>}</div>}
    {!results.length && other.length > 0 && <div className="panel">Results saved for {other.map((k) => (
      <button key={k} className="btn primary" style={{ marginRight: 8 }} onClick={() => setPeriod(k)}>{PERIODS.find((p) => p[0] === k)[1]} →</button>))}</div>}
    {!results.length ? <p className="muted panel">No results for the ticked setup(s) over {PERIODS.find((p) => p[0] === period)[1]} yet: press Run. Days not saved
      yet are fetched from Kite first (once, kept for next time). A run replays the same boxes as live, minute by minute, with paper fills and intraday charges.</p>
      : shown || results.length === 1 ? <>{results.length > 1 && <button className="linkbtn" onClick={() => setFocus(null)}>← back to side by side</button>}
        <h3 style={{ color: colors[(shown || results[0]).setup] }}>● {(shown || results[0]).setup}</h3><Report r={shown || results[0]} color={colors[(shown || results[0]).setup]} csv={csv} /></>
        : <Compare rs={results} colors={colors} onOpen={setFocus} />}
    <p className="muted small">⚠️ Today's Nifty 500 list (survivorship bias) and modelled fills: real results will be somewhat lower. Nothing here places orders or touches the live paper positions.</p>
  </>;
}
