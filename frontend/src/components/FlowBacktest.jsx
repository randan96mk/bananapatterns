import React, { useEffect, useRef, useState } from 'react';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { stamp } from '../timefmt';
import { inr, pct, cls } from '../fmt';
import { Cards, Card, Bar, Panel, FixedTable, Seg, LineChart, Heat, KV, Tag } from './ui2';

// 🧪 Intraday Flow backtest: saved setups, a run over 6 months / 1 / 2 years, one setup's report or several side by side.
const COLORS = ['#f5c518', '#4ea1ff', '#c58af9', '#26a69a', '#ff8a65', '#90a4ae'];
const PERIODS = [['6m', '6 months'], ['1y', '1 year'], ['2y', '2 years']];
const EXIT = { stop: '🛑 stop', target: '🎯 target', 'trailing stop': '🪜 trail', 'time stop': '⏱ time stop', 'square-off': '🔔 square-off', 'end of day': '⏹ end of day' };
const day = (d) => (d ? new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' }) : '');
const AGG = [{ h: '' }, { h: 'Trades', w: 70, n: true }, { h: 'Won', w: 60, n: true }, { h: 'Net', w: 100, n: true }];
const aggRows = (rows) => Object.entries(rows || {}).map(([k, a]) => ({ key: k, cells: [k, a.trades, `${a.win_rate ?? '—'}%`, <span className={cls(a.net)}>{inr(a.net)}</span>] }));

// Plain profit → slippage → charges → what is left: the same trades before and after costs.
function Charges({ r }) {
  const c = r.charges;
  if (!c) return null;
  const cols = [{ h: '' }, { h: 'Total', w: 100, n: true }, { h: 'A trade', w: 90, n: true }];
  const row = (label, v, per, strong) => ({ key: label, cells: [strong ? <b>{label}</b> : label, <span className={cls(v)}>{strong ? <b>{inr(v)}</b> : inr(v)}</span>, <span className={cls(per)}>{inr(per, 2)}</span>] });
  return <Panel title="💸 Before vs after charges" right={`the same ${r.trades} trades`}>
    <FixedTable cols={cols} rows={[row('Plain profit', c.plain, c.per_trade.plain, true), row('− Slippage (0.05% a fill)', -c.slip, -c.per_trade.slip),
      row('− Brokerage (+18% GST)', -(c.brk || 0), -(c.per_trade.brk || 0)), row('− Govt charges', -(c.gov || 0), -(c.per_trade.gov || 0)),
      row('= Profit after costs', c.net, c.per_trade.net, true), row('Stressed (+0.10% a fill)', r.stress.net, r.trades ? r.stress.net / r.trades : 0)]} />
    <div className="rnote">Exact signal prices first; govt charges are STT, exchange, SEBI, GST on them and stamp duty. Won <b>{c.win_rate_plain ?? '—'}%</b> before → <b>{r.win_rate ?? '—'}%</b> after
      {' '}· profit factor <b>{c.pf_plain ?? '—'}</b> → <b>{r.profit_factor ?? '—'}</b> · CAGR <b className={cls(c.cagr_plain)}>{pct(c.cagr_plain)}</b> → <b className={cls(r.cagr)}>{pct(r.cagr)}</b>
      {c.share != null && <> · costs take <b>{c.share.toFixed(0)}%</b> of the plain profit</>}
      {c.plain > 0 && c.net <= 0 && <div className="warn">⚠️ Profitable before costs, a loss after: too many small trades for the edge they have.</div>}</div></Panel>;
}

const held = (a, b) => { const m = (t) => +t.slice(0, 2) * 60 + +t.slice(3, 5); const x = m(b) - m(a); return x >= 60 ? `${Math.floor(x / 60)}h ${x % 60}m` : `${x}m`; };
const n2 = (v) => (v == null ? '—' : (+v).toLocaleString('en-IN', { maximumFractionDigits: 2 }));
const exits = (t) => (t.exits || []).map((e, i) => `${e.t} · ${e.qty} @ ${n2(e.price)} ${i < t.exits.length - 1 ? 'partial' : (EXIT[e.why] || e.why)}`).join(' · ');
// one exit in full; several as a count and the last one (the whole list is the tooltip)
const brief = (t) => { const x = t.exits || []; if (!x.length) return '—'; const l = x[x.length - 1]; return x.length === 1 ? exits(t) : `${x.length} fills · last ${l.t} ${EXIT[l.why] || l.why}`; };
// [header, cell, number?, width, low priority (hidden on a phone)?, view]: the entry side and the exit & costs side of each trade
const COLS = [['Stock', (t) => <b>{t.symbol}</b>], ['Day', (t) => day(t.day), 0, 80, 0, 'in'], ['Side', (t) => <span className={t.side === 'long' ? 'up' : 'down'}>{t.side}</span>, 0, 60, 0, 'in'],
  ['Setup', (t) => t.setup || '—', 0, 110, 1, 'in'], ['Signal', (t) => t.signal_t || '—', 0, 70, 1, 'in'], ['Signal ₹', (t) => n2(t.signal_price), 1, 90, 1, 'in'], ['Entry', (t) => t.entry_t, 0, 70, 0, 'in'],
  ['Entry ₹', (t) => n2(t.entry), 1, 90, 0, 'in'], ['Qty', (t) => t.qty, 1, 60, 1, 'in'], ['Value', (t) => inr(t.entry * t.qty), 1, 100, 1, 'in'], ['Stop', (t) => n2(t.sl), 1, 90, 0, 'in'],
  ['Target', (t) => n2(t.target), 1, 90, 1, 'in'], ['Exit(s)', (t) => <span title={exits(t)}>{brief(t)}</span>, 0, 200, 1, 'out'], ['Exit ₹', (t) => n2(t.exit), 1, 90, 0, 'out'],
  ['Exit reason', (t) => EXIT[t.reason] || t.reason, 0, 110, 0, 'out'], ['Held', (t) => held(t.entry_t, t.exit_t), 1, 70, 1, 'out'],
  ['Plain', (t) => <span className={cls(t.plain)}>{inr(t.plain)}</span>, 1, 90, 1, 'out'], ['Slippage', (t) => inr(-(t.slip || 0)), 1, 90, 1, 'out'],
  ['Brokerage', (t) => inr(-(t.brk || 0)), 1, 90, 1, 'out'], ['Govt', (t) => inr(-(t.gov || 0)), 1, 80, 1, 'out'],
  ['Net', (t) => <b className={cls(t.net)}>{inr(t.net)}</b>, 1, 90, 0, 'both'], ['R', (t) => t.r ?? '—', 1, 50, 1, 'out']];

function Trades({ r, csv }) {
  const [max, setMax] = useState(100);
  const [view, setView] = useState('in');
  const rows = r.rows.slice(0, max);
  const cols = COLS.filter(([, , , , , v]) => !v || v === view || v === 'both');
  return <Panel title="📋 Trades" meta={r.trades} right={<>
    <span>newest first{rows.length < r.trades ? ` · showing ${rows.length}, the CSV has all` : ''}</span>
    <Seg small opts={[['in', 'Entry'], ['out', 'Exit & costs']]} v={view} set={setView} />
    {r.rows.length > max && <button type="button" className="btn ghost small" onClick={() => setMax(r.rows.length)}>show all {r.rows.length}</button>}
    {r.split_costs && <a className="btn ghost small" href={csv(r.setup, r.period)} download>⬇ CSV (all trades)</a>}</>}>
    <FixedTable cols={cols.map(([h, , n, w, lo]) => ({ h, w, n: !!n, lo: !!lo }))} rows={rows.map((t, i) => ({ key: i, cells: cols.map(([, f]) => f(t)) }))} empty="No trades." />
  </Panel>;
}

// ⚙️ Report as if: capital, brokerage (% and / or ₹ per order; 0 or empty = none), slippage and govt charges on / off.
const COSTS0 = { capital: '', brk_pct: '0.03', brk_rs: '20', slip: true, gov: true };
function CostsBar({ c, set, runCap }) {
  const num = (k, w, ph) => <input className="input" style={{ width: w, textAlign: 'right' }} inputMode="decimal" placeholder={ph} value={c[k]}
    onChange={(e) => set({ ...c, [k]: e.target.value.replace(/[^0-9.]/g, '') })} />;
  return <Panel title="⚙️ Report as if" right={<><button type="button" className="btn ghost small" onClick={() => set({ ...c, brk_pct: '', brk_rs: '' })}>₹0 brokerage</button>
    <button type="button" className="btn ghost small" onClick={() => set(COSTS0)}>↺ reset</button></>}>
    <div className="rwrap">
      <span>Capital ₹ {num('capital', 110, runCap ? String(runCap) : 'as run')}</span>
      <span>Brokerage {num('brk_pct', 64, '0')} % and {num('brk_rs', 64, '0')} ₹ per order</span>
      <label><input type="checkbox" checked={c.slip} onChange={(e) => set({ ...c, slip: e.target.checked })} /> Slippage 0.05%</label>
      <label><input type="checkbox" checked={c.gov} onChange={(e) => set({ ...c, gov: e.target.checked })} /> Govt charges</label>
    </div>
    <div className="rnote">Both brokerage boxes set: the lower of the two an order · one set: that one · 0 or empty: none.
      {' '}Changes every number, chart and trade below, no re-run (another capital scales the quantities, whole shares). Remembered on this device.</div>
  </Panel>;
}

// r.by_month keys like 'YYYY-MM' → one heat row per year, net in ₹ thousands.
function monthRows(byMonth) {
  const years = {};
  Object.entries(byMonth || {}).forEach(([k, a]) => { const [y, m] = k.split('-'); (years[y] = years[y] || Array(12).fill(null))[+m - 1] = Math.round(a.net / 100) / 10; });
  return Object.keys(years).sort().reverse().map((y) => [y, years[y]]);
}

function Report({ r, color, csv }) {
  const months = monthRows(r.by_month);
  const scale = Math.max(1, ...months.flatMap(([, v]) => v.filter((x) => x != null).map(Math.abs)));
  const okAll = r.ok_periods;
  return <>
    <Cards n={4}>
      <Card label="Net after costs" value={inr(r.net)} num={r.net} k={cls(r.net)} sub={`on ${inr(r.capital)} · ${pct(r.return)}`} spark={r.curve?.net} fact={`${day(r.from)} – ${day(r.to)}`} />
      <Card label="Stressed net" value={inr(r.stress.net)} num={r.stress.net} k={cls(r.stress.net)} sub="+0.10% slippage a fill" fact={`stressed CAGR ${pct(r.stress.cagr)}`} />
      <Card unit="%" label="CAGR" value={pct(r.cagr)} num={r.cagr} k={cls(r.cagr)} sub={`NIFTY ${pct(r.nifty)} · same period`} fact={`${r.covered} of ${r.sessions} sessions covered`} />
      <Card unit="%" label="Worst drawdown" value={pct(-r.drawdown)} num={r.drawdown} k="down" sub="peak to trough, after costs" />
      <Card unit="n" label="Trades · won" value={`${r.trades} · ${r.win_rate ?? '—'}%`} num={r.trades} sub={`${r.sessions ? (r.trades / r.sessions).toFixed(1) : '—'} a session`} />
      <Card unit="n" label="Profit factor" value={r.profit_factor ?? '—'} num={r.profit_factor} sub={`average ${r.avg_r ?? '—'} R`} />
      <Card label="Costs" value={inr(r.costs)} num={r.costs} k="down" sub={`${inr(r.trades ? r.costs / r.trades : 0)} a trade`} fact={r.charges?.share != null ? <><Bar pct={r.charges.share} bad={r.charges.share > 50} /> {r.charges.share.toFixed(0)}% of plain profit</> : null} />
      <Card unit="n" label="Dev · val · out-of-sample" value={`${r.splits.filter((s) => s.net > 0).length} of ${r.splits.length} +`} num={r.splits[0]?.net} k={okAll ? 'up' : 'down'}
        sub={r.splits.map((s) => pct(s.net, 0)).join(' / ')} fact={okAll ? '✅ all periods positive' : '❌ not every period positive'} />
    </Cards>
    <div className="two">
      <div className="rd">
        <Panel title="Equity" right={`${inr(r.capital)} start · ${day(r.from)} – ${day(r.to)}`}>
          <LineChart series={[{ name: 'after costs', pts: r.curve.net, color, fill: true }, ...(r.curve.plain ? [{ name: 'plain (before costs)', pts: r.curve.plain, color: 'var(--ok)' }] : []),
            { name: 'stressed', pts: r.curve.stress, color, dash: true }, { name: 'NIFTY 50', pts: r.curve.nifty, color: 'var(--muted)', dash: true }]} labels={[day(r.from), day(r.to)]} /></Panel>
        <Panel title="By month" right="net after costs">
          {months.length ? <Heat rows={months} scale={scale} fmt={(v) => inr(v * 1000)} /> : <p className="muted small rempty">No months yet.</p>}</Panel>
        <div className="cols c3">
          <Panel title="By side"><FixedTable cols={AGG} rows={aggRows(r.by_side)} empty="—" /></Panel>
          <Panel title="By entry time"><FixedTable cols={AGG} rows={aggRows(r.by_time)} empty="—" /></Panel>
          {r.by_setup && <Panel title="By setup" right="which entry setup(s) fired"><FixedTable cols={AGG} rows={aggRows(r.by_setup)} empty="—" /></Panel>}
          <Panel title="Exits"><FixedTable cols={[{ h: 'How' }, { h: 'Trades', w: 80, n: true }, { h: 'Share', w: 70, n: true }]}
            rows={Object.entries(r.exits || {}).map(([k, n]) => ({ key: k, cells: [EXIT[k] || k, n, `${r.trades ? Math.round((100 * n) / r.trades) : 0}%`] }))} empty="—" /></Panel>
        </div>
      </div>
      <div className="rd">
        {r.by_quarter && <Panel title="By quarter" right="walk-forward: does the edge hold?"><FixedTable cols={AGG} rows={aggRows(r.by_quarter)} empty="—" /></Panel>}
        <Charges r={r} />
        <Panel title="Data"><div className="rnote">{r.covered} of {r.sessions} sessions had saved 1-minute data{r.missing ? ` · ${r.missing} stock-days missing (log in to Kite and re-run to fetch them)` : ''} · ran {stamp(r.ran_at)}</div></Panel>
      </div>
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
  const cols = [{ h: '' }, ...rs.map((r) => ({ h: r.setup, key: r.setup, w: 170, n: true }))];
  const rows = [{ key: 'open', cells: ['Report', ...rs.map((r) => <button type="button" className="btn ghost small" style={{ color: colors[r.setup] }} onClick={() => onOpen(r.setup)}>● open →</button>)] }, ...ROWS.map(([label, val, show]) => {
    const best = val ? Math.max(...rs.map((r) => val(r) ?? -Infinity)) : null;
    return { key: label, cells: [label, ...rs.map((r) => <span style={val && rs.length > 1 && val(r) === best ? { color: 'var(--ok)' } : undefined}>{show(r)}</span>)] };
  })];
  return <Panel title="Side by side" right="best in green · open a column's report">
    <FixedTable cols={cols} rows={rows} />
    <LineChart h={130} series={rs.map((r) => ({ name: r.setup, pts: r.curve.net, color: colors[r.setup] })).concat(rs[0] ? [{ name: 'NIFTY 50', pts: rs[0].curve.nifty, color: 'var(--muted)', dash: true }] : [])} />
  </Panel>;
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
  const want = useRef(period);                                             // the period the page shows (only its results are fetched)
  want.current = period;
  const csv = (setup, per) => `/api/flow/backtest/csv?${qs}&${new URLSearchParams({ setup, period: per })}`;
  const was = useRef(null);
  const load = () => api.flowBacktest(`${qs}&period=${want.current}`).then((x) => {   // this period's results only
    if (was.current === 'running' && x.status.state !== 'running' && x.status.period) setPeriod(x.status.period);   // a run ended: show it
    if (was.current === null && !x.results.length && x.saved?.latest) setPeriod(x.saved.latest);   // opened: the latest run's period
    was.current = x.status.state;
    setErr('');
    setD(x);
    setSel((s) => (s.length ? s.filter((n) => n in x.setups) : Object.keys(x.setups).slice(0, 1)));
  }).catch((e) => setErr(/load failed|failed to fetch|network/i.test(e.message) ? 'Could not reach the app just now (busy or a weak connection): pull to refresh or wait a moment.' : e.message));
  useEffect(() => {
    try { localStorage.setItem('bt.costs', JSON.stringify(costs)); } catch { /* private mode */ }
    const id = setTimeout(load, 350);                        // as you type: the report is rebuilt for these costs
    return () => clearTimeout(id);
  }, [qs, period]);
  const bst = useLive('backtest');                        // 📡 progress from the live connection
  useEffect(() => {
    if (!bst || !d) return;
    if (d.status.state === 'running' && bst.state !== 'running') load();   // finished: the full results once
    else if (bst.state !== d.status.state || bst.pct !== d.status.pct || bst.message !== d.status.message) setD((x) => ({ ...x, status: bst }));
  }, [bst]);
  if (!d) return <Panel title="🧪 Backtest"><p className="muted small rempty">Loading…</p></Panel>;
  const names = Object.keys(d.setups);
  const colors = Object.fromEntries(names.map((n, i) => [n, COLORS[i % COLORS.length]]));
  const results = d.results.filter((r) => sel.includes(r.setup) && r.period === period);
  const other = [...new Set(sel.flatMap((n) => d.saved?.periods?.[n] || []).filter((p) => p !== period))];
  const shown = focus && results.find((r) => r.setup === focus);
  const running = d.status.state === 'running';
  const act = (p) => p.then(load).catch((e) => setErr(e.message));
  const save = () => { const n = window.prompt('Name this setup (a copy of every box\'s settings on the canvas):'); if (n) act(api.flowSetupSave(n)); };
  const one = shown || results[0];

  return <>
    <Panel title="🧪 Backtest" meta={names.length} right={<>
      <Seg opts={PERIODS} v={period} set={setPeriod} />
      {running ? <button type="button" className="btn small" style={{ background: 'var(--bad)', color: '#fff' }}
        onClick={() => window.confirm('Stop the running backtest? Nothing from this run is saved.') && act(api.flowBacktestStop())}>⏹ Stop backtest</button>
        : <button type="button" className="btn primary small" disabled={!sel.length} onClick={() => act(api.flowBacktestRun(sel, period))}>
          {`▶ Run ${sel.length > 1 ? `selected (${sel.length})` : 'backtest'}`}</button>}
      <button type="button" className="btn ghost small" onClick={save}>💾 Save canvas as setup…</button></>}>
      <div className="rwrap">{names.map((n) => (
        <label key={n} className="chip" style={{ borderColor: colors[n] }}>
          <input type="checkbox" checked={sel.includes(n)} aria-label={n} onChange={(e) => setSel(e.target.checked ? [...sel, n] : sel.filter((x) => x !== n))} />
          {' '}<b style={{ color: colors[n] }}>{n}</b>
          {d.setups[n] && <> <button type="button" className="linkbtn small" style={{ padding: 0 }} onClick={() => window.confirm(`Put "${n}" on the canvas? The live flow uses it from the next candle.`) && act(api.flowSetupLoad(n))}>load on canvas</button>
            {!d.setups[n].preset && <button type="button" className="linkbtn small" style={{ padding: 0 }} onClick={() => window.confirm(`Delete setup "${n}" and its results?`) && act(api.flowSetupDelete(n))}>delete</button>}</>}
        </label>))}</div>
      {names.filter((n) => d.setups[n]?.preset && sel.includes(n)).map((n) => <div key={n} className="rnote">📚 <b>{n}</b>: {d.setups[n].desc}</div>)}
      {(running || d.status.message) && <div className="rnote">{running ? <b>Running… </b> : null}{d.status.message}
        {running && <div style={{ marginTop: 6 }}><Bar pct={d.status.pct} /> <span className="mono">{d.status.pct}%</span></div>}</div>}
    </Panel>
    {err && <div className="rbanner warn">{err}</div>}
    {results.length > 0 && <CostsBar c={costs} set={setCosts} runCap={results[0].run_capital || results[0].capital} />}
    {results.some((r) => !r.split_costs) && <div className="rbanner warn">A result from before the cost options: re-run it to change its costs or capital and to download its CSV.</div>}
    {!results.length && other.length > 0 && <div className="rbanner">Results saved for {other.map((k) => (
      <button key={k} type="button" className="btn primary small" style={{ marginLeft: 8 }} onClick={() => setPeriod(k)}>{PERIODS.find((p) => p[0] === k)[1]} →</button>))}</div>}
    {!results.length ? <div className="rbanner">No results for the ticked setup(s) over {PERIODS.find((p) => p[0] === period)[1]} yet: press Run. Days not saved
      yet are fetched from Kite first (once, kept for next time). A run replays the same boxes as live, minute by minute, with paper fills and intraday charges.</div>
      : shown || results.length === 1 ? <>
        <div className="rhead"><h1 style={{ color: colors[one.setup] }}>● {one.setup}</h1><Tag>{PERIODS.find((p) => p[0] === one.period)?.[1] || one.period}</Tag>
          {results.length > 1 && <span className="r"><button type="button" className="btn ghost small" onClick={() => setFocus(null)}>← back to side by side</button></span>}</div>
        <Report r={one} color={colors[one.setup]} csv={csv} /></>
        : <Compare rs={results} colors={colors} onOpen={setFocus} />}
    <div className="rnote">⚠️ Today's Nifty 500 list (survivorship bias) and modelled fills: real results will be somewhat lower. Nothing here places orders or touches the live paper positions.</div>
  </>;
}
