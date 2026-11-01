import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
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

function Report({ r, color }) {
  return <>
    <div className="summary" style={{ marginBottom: 12 }}>
      <div className="k"><div className={`v ${cls(r.net)}`}>{inr(r.net)}</div><div className="l">net after costs on {inr(r.capital)} · {pct(r.return)}</div></div>
      <div className="k"><div className="v">{pct(r.cagr)}</div><div className="l">CAGR · stressed {pct(r.stress.cagr)} · NIFTY {pct(r.nifty)}</div></div>
      <div className="k"><div className="v down">{pct(-r.drawdown)}</div><div className="l">worst drawdown</div></div>
      <div className="k"><div className="v">{r.trades} · {r.win_rate ?? '—'}%</div><div className="l">trades · won</div></div>
      <div className="k"><div className="v">{r.profit_factor ?? '—'}</div><div className="l">profit factor · avg {r.avg_r ?? '—'} R</div></div>
      <div className="k"><div className="v">{inr(r.costs)}</div><div className="l">costs · {inr(r.trades ? r.costs / r.trades : 0)} a trade</div></div>
    </div>
    <div className="panel"><b>Account</b> <span className="muted small">· after costs · stressed (+0.10% a fill) · dashed NIFTY 50 · {day(r.from)} – {day(r.to)}</span>
      <Lines series={[{ color: '#8b98a5', values: r.curve.nifty, dash: true }, { color, values: r.curve.stress, dash: true }, { color, values: r.curve.net }]} />
      <div className="small">{r.splits.map((s) => <span key={s.name} style={{ marginRight: 14 }}>{s.name} <b className={cls(s.net)}>{pct(s.net)}</b>
        <span className="muted"> (stressed {pct(s.stress)})</span></span>)} {r.ok_periods ? '✅ all periods positive' : '❌ not every period positive'}</div></div>
    <div className="flowcols">
      <div className="panel"><b>By side</b><Agg rows={r.by_side} /><b>By entry time</b><Agg rows={r.by_time} /></div>
      <div className="panel"><b>By month</b> <span className="muted small">· net</span><div>{Object.entries(r.by_month).map(([m, a]) => (
        <span key={m} className={`chip ${cls(a.net)}`}>{m} {inr(a.net)}</span>))}</div>
        <p className="small"><b>Exits</b> {Object.entries(r.exits).map(([k, n]) => `${EXIT[k] || k} ${n}`).join(' · ')}</p>
        <p className="muted small">{r.covered} of {r.sessions} sessions had saved 1-minute data{r.missing ? ` · ${r.missing} stock-days missing (log in to Kite and re-run to fetch them)` : ''} · ran {r.ran_at?.slice(0, 16).replace("T", " ")}</p></div>
    </div>
    <div className="panel"><b>Trades</b> <span className="muted small">· newest first{r.rows.length < r.trades ? ` · latest ${r.rows.length}` : ''}</span>
      <div className="tablewrap"><table className="grid"><thead><tr><th>Day</th><th>Stock</th><th>Side</th><th>In → out</th><th className="num">Net</th><th className="num">R</th><th>Exit</th></tr></thead>
        <tbody>{r.rows.slice(0, 300).map((t, i) => <tr key={i}><td>{day(t.day)}</td><td><b>{t.symbol}</b></td><td className={t.side === 'long' ? 'up' : 'down'}>{t.side}</td>
          <td className="small">{t.entry_t} @ {t.entry} → {t.exit_t} @ {t.exit}</td><td className={`num ${cls(t.net)}`}>{inr(t.net)}</td>
          <td className="num">{t.r ?? '—'}</td><td>{EXIT[t.reason] || t.reason}</td></tr>)}</tbody></table></div></div>
  </>;
}

const ROWS = [
  ['Net after costs', (r) => r.net, (r) => inr(r.net)], ['CAGR · stressed', (r) => r.cagr, (r) => `${pct(r.cagr)} · ${pct(r.stress.cagr)}`],
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
  const load = () => api.flowBacktest(period).then((x) => {
    setD(x);
    setSel((s) => (s.length ? s.filter((n) => n in x.setups) : Object.keys(x.setups).slice(0, 1)));
  }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [period]);
  useEffect(() => {
    if (d?.status.state !== 'running') return undefined;
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [d?.status.state, period]);
  if (!d) return <div className="panel muted">Loading…</div>;
  const names = Object.keys(d.setups);
  const colors = Object.fromEntries(names.map((n, i) => [n, COLORS[i % COLORS.length]]));
  const results = d.results.filter((r) => sel.includes(r.setup));
  const shown = focus && results.find((r) => r.setup === focus);
  const running = d.status.state === 'running';
  const act = (p) => p.then(load).catch((e) => setErr(e.message));
  const save = () => { const n = window.prompt('Name this setup (a copy of every box\'s settings on the canvas):'); if (n) act(api.flowSetupSave(n)); };

  return <>
    <div className="panel flowtools">
      <span className="seg">{PERIODS.map(([k, l]) => <button key={k} className={period === k ? 'active' : ''} onClick={() => setPeriod(k)}>{l}</button>)}</span>
      <button className="btn primary" disabled={running || !sel.length} onClick={() => act(api.flowBacktestRun(sel, period))}>
        {running ? 'Running…' : `▶ Run ${sel.length > 1 ? `selected (${sel.length})` : 'backtest'}`}</button>
      <button className="btn" onClick={save}>💾 Save canvas as setup…</button>
      <div style={{ width: '100%' }}>{names.map((n) => (
        <label key={n} className="chip" style={{ borderColor: colors[n] }}>
          <input type="checkbox" checked={sel.includes(n)} onChange={(e) => setSel(e.target.checked ? [...sel, n] : sel.filter((x) => x !== n))} />
          {' '}<b style={{ color: colors[n] }}>{n}</b>
          {d.setups[n] && <> <button className="linkbtn small" onClick={() => act(api.flowSetupLoad(n))}>load on canvas</button>
            <button className="linkbtn small" onClick={() => window.confirm(`Delete setup "${n}" and its results?`) && act(api.flowSetupDelete(n))}>delete</button></>}
        </label>))}</div>
    </div>
    {err && <div className="warn small">{err}</div>}
    {(running || d.status.message) && <div className="panel small">{running ? <b>Running… </b> : null}{d.status.message}
      {running && <div className="bar" style={{ marginTop: 6 }}><i style={{ width: `${d.status.pct}%` }} /></div>}</div>}
    {!results.length ? <p className="muted panel">No results for the ticked setup(s) over {PERIODS.find((p) => p[0] === period)[1]} yet: press Run. Days not saved
      yet are fetched from Kite first (once, kept for next time). A run replays the same boxes as live, minute by minute, with paper fills and Zerodha costs.</p>
      : shown || results.length === 1 ? <>{results.length > 1 && <button className="linkbtn" onClick={() => setFocus(null)}>← back to side by side</button>}
        <h3 style={{ color: colors[(shown || results[0]).setup] }}>● {(shown || results[0]).setup}</h3><Report r={shown || results[0]} color={colors[(shown || results[0]).setup]} /></>
        : <Compare rs={results} colors={colors} onOpen={setFocus} />}
    <p className="muted small">⚠️ Today's Nifty 500 list (survivorship bias) and modelled fills: real results will be somewhat lower. Nothing here places orders or touches the live paper positions.</p>
  </>;
}
