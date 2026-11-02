import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { signed } from '../fmt';

// 🧭 The flow explained: the latest candle (why a trade or not, where stocks stopped, the closest to the trigger),
// the rules in plain words, one stock checked through every box, the open trades' exits.
const n2 = (v) => (v == null ? '—' : (+v).toLocaleString('en-IN', { maximumFractionDigits: 2 }));
const ICON = { pass: ['✓', 'up'], drop: ['✗', 'down'], wait: ['…', 'warn'], skip: ['·', 'muted'] };
const VAL = { price: 'price', side: 'side', sides: 'sides', vwap: 'VWAP', rsi: 'RSI', rvol: 'RVOL', or_high: 'range high',
  or_low: 'range low', entry: 'entry', score: 'score', sl: 'stop', target: 'target', qty: 'qty', risk_rs: 'risk ₹', stop_pct: 'stop %' };
export const vals = (x) => Object.entries(VAL).filter(([k]) => x?.[k] != null && x[k] !== '')
  .map(([k, l]) => `${l} ${Array.isArray(x[k]) ? x[k].join('/') : typeof x[k] === 'string' ? x[k] : n2(x[k])}`).join(' · ');

export function StockCheck({ symbol: initial }) {
  const [q, setQ] = useState(initial || '');
  const [c, setC] = useState(null);
  const run = (s) => s && api.flowCheck(s).then(setC).catch((e) => setC({ error: e.message }));
  useEffect(() => { if (initial) { setQ(initial); run(initial); } }, [initial]);
  const f = c?.facts || {};
  return <div>
    <form onSubmit={(e) => { e.preventDefault(); run(q); }} style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
      <input className="input" style={{ flex: 1 }} value={q} onChange={(e) => setQ(e.target.value.toUpperCase())} placeholder="Stock, e.g. TCS" />
      <button className="btn">Check</button></form>
    {c?.error && <p className="warn small">{c.error}</p>}
    {c?.steps && <>
      <div className="hlbox"><b>{c.symbol}</b>: {c.verdict} <span className="muted small">· candle {c.at}</span></div>
      <div className="v2list">{c.steps.map((s) => { const [i, k] = ICON[s.state] || ['·', 'muted']; return (
        <div key={s.id} className="it"><b className={k} style={{ width: 14 }}>{i}</b><div style={{ flex: 1 }}><b>{s.title}</b>
          {s.why && <span className="muted small"> · {s.why}</span>}{s.values && <div className="muted small">{vals(s.values)}</div>}</div></div>); })}</div>
      <div className="muted small" style={{ marginTop: 8 }}>Facts: price {n2(f.price)} · VWAP {n2(f.vwap)} · RSI {n2(f.rsi)} · RVOL {n2(f.rvol)}× ·
        {' '}50/200-day {n2(f.sma50)} / {n2(f.sma200)} · opening range {n2(f.or_low)}–{n2(f.or_high)} · NIFTY {n2(f.nifty)} (VWAP {n2(f.nifty_vwap)}) · VIX {n2(f.vix)}</div></>}
  </div>;
}

export default function JourneyPanel({ onClose, symbol, onSymbol }) {
  const j = useLive('journey');
  const lv = useLive('flow_live');
  const [tab, setTab] = useState(symbol ? 'check' : 'candle');
  useEffect(() => { if (symbol) setTab('check'); }, [symbol]);
  const open = Object.entries(lv?.rows || {}).filter(([, r]) => r.status === 'open');
  return <aside className="drawer" aria-label="Strategy journey">
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}><b style={{ fontSize: 16 }}>🧭 Strategy journey</b>
      {j?.at && <span className="achip">candle {j.at}</span>}<button className="linkbtn" style={{ marginLeft: 'auto' }} onClick={onClose}>✕</button></div>
    <nav className="subnav" style={{ padding: 0, margin: '8px 0 12px' }}>{[['candle', 'Latest candle'], ['rules', 'Rules'], ['check', 'Check a stock'], ['open', `Open trades ${open.length}`]]
      .map(([k, l]) => <button key={k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>{l}</button>)}</nav>
    {!j ? <p className="muted">Loading…</p> : <>
      {tab === 'candle' && <>
        <div className="hlbox">{j.headline}</div>
        {j.funnel?.length > 0 && <div className="v2card"><h3>Where stocks stopped <span className="r">this candle</span></h3>
          <div className="v2list">{j.funnel.map((x) => { const info = x.kind === 'execute' && x.drops?.['signals only (mode)']; return <div key={x.id} className="it">
            <b className={info ? 'muted' : x.out ? 'up' : 'down'} style={{ width: 14 }}>{info ? '·' : x.out ? '✓' : '✗'}</b>
            <div style={{ flex: 1 }}><b>{x.title}</b> <span className="mono" style={{ float: 'right' }}>{x.in} → {x.out}</span>
              {Object.keys(x.drops || {}).length > 0 && <div className="muted small">out: {Object.entries(x.drops).map(([k, v]) => `${k} ${v}`).join(' · ')}</div>}
              {info && <div className="muted small">Signals mode: nothing is traded, not even on paper (switch to 📝 Paper to trade on paper)</div>}
              <div className="bar" style={{ marginTop: 4 }}><i style={{ width: `${x.in ? Math.round(100 * x.out / x.in) : 0}%` }} /></div></div></div>; })}</div></div>}
        {j.closest?.length > 0 && <div className="v2card"><h3>Closest to a trade <span className="r">reached the trigger · distance to it</span></h3>
          <table className="grid"><thead><tr><th>Stock</th><th>Side</th><th className="num">Last close</th><th className="num">Trigger</th><th className="num">Away</th><th className="num">RVOL</th></tr></thead>
            <tbody>{j.closest.map((x) => <tr key={x.symbol + x.side} style={{ cursor: 'pointer' }} onClick={() => onSymbol(x.symbol)}>
              <td><b>{x.symbol}</b></td><td className={x.side === 'long' ? 'up' : 'down'}>{x.side}</td><td className="num">{n2(x.last)}</td>
              <td className="num">{n2(x.trigger)}</td><td className="num">{x.away_pct <= 0 ? 'beyond' : `${x.away_pct.toFixed(2)}%`}</td><td className="num">{x.rvol ? `${x.rvol}×` : '—'}</td></tr>)}</tbody></table></div>}
      </>}
      {tab === 'rules' && <>{j.rules.map((r) => <div key={r.title} className="v2card"><h3>{r.title}</h3>
        <ol style={{ margin: 0, paddingLeft: 18, lineHeight: 1.6 }}>{r.lines.map((l) => <li key={l}>{l}</li>)}</ol></div>)}
        <p className="muted small">Written from the boxes' settings: change a box and this changes.</p></>}
      {tab === 'check' && <StockCheck symbol={symbol} />}
      {tab === 'open' && (!open.length ? <p className="muted">No open paper trades.</p>
        : <div className="v2list">{open.map(([s, r]) => <div key={s} className="it"><div style={{ flex: 1 }}><b>{s}</b> <span className="muted small">{r.qty} @ {n2(r.entry)} · now {n2(r.now)}</span>
          <div className="muted small">stop {n2(r.stop)}{r.trailed ? ' (trailed)' : ''} · {r.target ? `target ${n2(r.target)}` : 'no target'} · out by square-off</div></div>
          <b className={r.pnl >= 0 ? 'up' : 'down'}>{signed(r.pnl)}</b></div>)}</div>)}
    </>}
  </aside>;
}

// Click a box: who came in, who passed (and where each is now), who dropped and why.
export function NodeStocks({ nid, onSymbol, stamp }) {
  const [d, setD] = useState(null);
  const [tab, setTab] = useState('passed');
  useEffect(() => { setD(null); api.flowNodeStocks(nid).then(setD).catch(() => setD({ error: 'not available' })); }, [nid, stamp]);
  if (!d) return <p className="muted small">Loading the stocks…</p>;
  if (d.error) return <p className="muted small">No live run yet: stocks show after the first candle close (or ▶ Run now).</p>;
  const groups = {};
  d.dropped.forEach((x) => { (groups[x.reason] = groups[x.reason] || []).push(x); });
  return <div>
    <h4>Stocks in this box <span className="muted small">· candle {d.at}</span></h4>
    <nav className="subnav" style={{ padding: 0, margin: '4px 0 8px' }}>{[['passed', `Passed ${d.passed.length}`], ['dropped', `Dropped / waiting ${d.dropped.length}`]]
      .map(([k, l]) => <button key={k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>{l}</button>)}
      <span className="muted small" style={{ alignSelf: 'center', marginLeft: 'auto' }}>in {d.in}</span></nav>
    {tab === 'passed' && (d.passed.length ? <div className="v2list" style={{ maxHeight: 320, overflowY: 'auto' }}>{d.passed.map((x) => (
      <div key={x.symbol} className="it" style={{ cursor: 'pointer' }} onClick={() => onSymbol(x.symbol)}><div style={{ flex: 1 }}><b>{x.symbol}</b>
        <div className="muted small">{vals(x)}</div></div><span className="small muted" style={{ textAlign: 'right' }}>{x.now}</span></div>))}</div>
      : <p className="muted small">None passed.</p>)}
    {tab === 'dropped' && (d.dropped.length ? Object.entries(groups).map(([why, xs]) => <div key={why} style={{ marginBottom: 8 }}>
      <div className="small"><b className={xs[0].waiting ? 'warn' : 'down'}>{xs[0].waiting ? '… waiting' : '✗'}</b> <b>{why}</b> <span className="muted">· {xs.length}</span></div>
      <div className="flowchips">{xs.slice(0, 40).map((x) => <span key={x.symbol} className="spill" style={{ cursor: 'pointer' }} onClick={() => onSymbol(x.symbol)}>{x.symbol}</span>)}
        {xs.length > 40 && <span className="muted small">+{xs.length - 40}</span>}</div></div>) : <p className="muted small">None dropped.</p>)}
  </div>;
}
