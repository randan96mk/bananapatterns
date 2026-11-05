import React, { useEffect, useMemo, useState } from 'react';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { Kpi, Spark, Acct, StopMeter } from './ui';
import { inr, pct, cls, signed } from '../fmt';
import { useIsPhone } from '../useMedia';

// 💼 Portfolio: every account in one place (the app's own: Real via Tradetron, swing paper, intraday paper), filtered
// by account: positions, trade history (CSV), orders, a P&L calendar, the curves.
const TABS = [['positions', 'Positions'], ['trades', 'Trade history'], ['orders', 'Orders'], ['calendar', 'Calendar'], ['curves', 'Curves']];
const COLORS = { real: 'var(--ok)', paper_A: '#6c8cff', paper_E: '#b39dff', intraday: '#f2b84b' };

function csv(rows) {
  const cols = ['account', 'strategy', 'symbol', 'side', 'qty', 'opened', 'closed', 'entry', 'exit', 'gross', 'costs', 'net', 'pct', 'reason'];
  const esc = (v) => (v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v);
  const body = [cols.join(','), ...rows.map((r) => cols.map((c) => esc(r[c])).join(','))].join('\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([body], { type: 'text/csv' }));
  a.download = 'trades.csv';
  a.click();
}

export default function PortfolioPage({ onStock }) {
  const o = useLive('overview');                           // positions update live; history loads once
  const [d, setD] = useState(null);
  const [book, setBook] = useState('all');
  const [tab, setTab] = useState('positions');
  const [err, setErr] = useState('');
  const phone = useIsPhone();
  useEffect(() => { api.getPortfolio().then(setD).catch((e) => setErr(e.message)); }, []);
  const names = d?.names || {};
  const books = useMemo(() => ['all', ...Object.keys(names).filter((b) => (d?.curves?.[b] || []).length || (o?.positions || []).some((p) => p.book === b))], [d, o]);
  const pick = (xs) => (book === 'all' ? xs : xs.filter((x) => x.book === book));
  const pos = pick(o?.positions || d?.positions || []);
  const trades = pick(d?.trades || []);
  const orders = pick(d?.orders || []);
  const month = new Date().toISOString().slice(0, 7);
  const mt = trades.filter((t) => (t.closed || '').slice(0, 7) === month);
  const won = mt.filter((t) => t.net > 0).length;
  const openPnl = pos.reduce((s, p) => s + (p.pnl || 0), 0);
  const daily = {};
  Object.entries(d?.daily || {}).forEach(([b, m]) => { if (book === 'all' || b === book) Object.entries(m).forEach(([day, v]) => { daily[day] = (daily[day] || 0) + v; }); });
  const days = Object.keys(daily).sort().slice(-42);
  if (err) return <div className="warn">{err}</div>;
  if (!d) return <div className="v2card muted">Loading…</div>;
  return <>
    <div className="bttools" style={{ marginBottom: 12 }}>
      <span className="seg">{books.map((b) => <button key={b} className={book === b ? 'active' : ''} onClick={() => setBook(b)}>{b === 'all' ? 'All' : names[b]}</button>)}</span>
      <span className="muted small">the app's own accounts · no broker holdings</span>
    </div>
    <div className="kpis">
      <Kpi label="Open P&L" value={signed(openPnl)} cls={cls(openPnl)} sub={`${pos.length} open positions · after exit costs`} />
      <Kpi label="Realised this month" value={signed(mt.reduce((s, t) => s + (t.net || 0), 0))} cls={cls(mt.reduce((s, t) => s + (t.net || 0), 0))}
        sub={`${mt.length} trades · ${mt.length ? Math.round(100 * won / mt.length) : 0}% won`} />
      <Kpi label="Costs this month" value={inr(mt.reduce((s, t) => s + (t.costs || 0), 0))} sub="charges + slippage" />
      <Kpi label="Closed trades (1 year)" value={trades.length} sub={`net ${signed(trades.reduce((s, t) => s + (t.net || 0), 0))}`} />
    </div>
    <nav className="subnav" style={{ padding: 0, marginBottom: 12 }}>{TABS.map(([k, l]) => <button key={k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>
      {l}{k === 'positions' ? ` ${pos.length}` : ''}</button>)}</nav>

    {tab === 'positions' && <div className="v2card">{!pos.length ? <p className="muted">No open positions.</p>
      : <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Account</th>{!phone && <th>Strategy</th>}<th className="num">Qty</th>
        <th className="num">Entry</th><th className="num">Now{pos.some((p) => p.live) ? <span className="up small" title="last traded price, market hours"> ●</span> : <span className="muted small" title="last close; live in market hours"> ·</span>}</th><th className="num">Value</th><th className="num">P&amp;L</th><th className="num">%</th>
        {!phone && <><th className="num">Stop</th><th>Distance</th><th>Opened</th></>}</tr></thead>
        <tbody>{pos.map((p) => <tr key={p.book + p.symbol} style={{ cursor: 'pointer' }} onClick={() => onStock(p.symbol)}>
          <td><b>{p.symbol}</b> <span className={`small ${p.side === 'short' ? 'down' : 'up'}`}>{p.side}</span></td><td><Acct book={p.book} name={p.account} /></td>
          {!phone && <td className="muted small">{p.strategy}</td>}<td className="num">{p.qty}</td><td className="num">{p.entry}</td><td className="num">{p.now ?? '—'}</td>
          <td className="num">{inr((p.now || p.entry) * p.qty)}</td><td className={`num ${cls(p.pnl)}`}>{signed(p.pnl)}</td><td className={`num ${cls(p.pct)}`}>{pct(p.pct)}</td>
          {!phone && <><td className="num">{p.stop ?? '—'}</td><td>{p.now && p.stop ? <StopMeter last={p.side === 'short' ? -p.now : p.now} stop={p.side === 'short' ? -p.stop : p.stop} /> : '—'}</td>
            <td className="muted small">{p.opened}</td></>}</tr>)}</tbody></table></div>}</div>}

    {tab === 'trades' && (() => { const m = {}; const books = [...new Set(trades.map((t) => t.book))];
      trades.forEach((t) => { const k = t.closed.slice(0, 7); m[k] = m[k] || {}; m[k][t.book] = (m[k][t.book] || 0) + (t.net || 0); });
      const months = Object.keys(m).sort().reverse();
      return months.length > 0 && <div className="v2card"><h3>By month <span className="r">net after costs · every book</span>
        <a className="btn" style={{ float: 'right' }} href="/api/export/trades.csv" download>⬇ CSV · every trade, every book</a></h3>
        <div className="tablewrap"><table className="grid small"><thead><tr><th>Month</th>{books.map((b) => <th key={b} className="num">{names[b] || b}</th>)}<th className="num">All</th></tr></thead>
          <tbody>{months.map((k) => { const tot = books.reduce((a, b) => a + (m[k][b] || 0), 0); return <tr key={k}><td>{k}</td>{books.map((b) => <td key={b} className={`num ${cls(m[k][b])}`} style={m[k][b] ? { background: m[k][b] > 0 ? 'rgba(38,166,154,.12)' : 'rgba(239,83,80,.12)' } : {}}>{m[k][b] != null ? inr(m[k][b]) : '—'}</td>)}<td className={`num ${cls(tot)}`}><b>{inr(tot)}</b></td></tr>; })}</tbody></table></div></div>; })()}
    {tab === 'trades' && <div className="v2card"><h3>Trade history <span className="r">{trades.length} · last year · newest first
      <button className="btn" style={{ marginLeft: 8 }} onClick={() => csv(trades)}>⬇ CSV</button></span></h3>
      <div className="tablewrap"><table className="grid"><thead><tr><th>Closed</th><th>Stock</th><th>Account</th><th className="num">Qty</th><th className="num">Entry</th>
        <th className="num">Exit</th><th className="num">Gross</th><th className="num">Costs</th><th className="num">Net</th><th className="num">%</th><th>Reason</th></tr></thead>
        <tbody>{trades.slice(0, 400).map((t, i) => <tr key={i}><td className="small">{t.closed}</td><td><b>{t.symbol}</b></td><td><Acct book={t.book} name={t.account} /></td>
          <td className="num">{t.qty}</td><td className="num">{t.entry}</td><td className="num">{t.exit}</td><td className={`num ${cls(t.gross)}`}>{inr(t.gross)}</td>
          <td className="num down">{inr(-(t.costs || 0))}</td><td className={`num ${cls(t.net)}`}><b>{signed(t.net)}</b></td><td className={`num ${cls(t.pct)}`}>{pct(t.pct)}</td>
          <td className="small">{t.reason}</td></tr>)}</tbody></table></div></div>}

    {tab === 'orders' && <div className="v2card"><div className="tablewrap"><table className="grid"><thead><tr><th>When</th><th>Account</th><th>Order</th><th>Stock</th>
      <th className="num">Qty</th><th className="num">Price</th><th>Status</th><th>Note</th></tr></thead>
      <tbody>{orders.slice(0, 300).map((x, i) => <tr key={i}><td className="small">{(x.at || '').replace('T', ' ').slice(0, 16)}</td><td><Acct book={x.book} name={x.account} /></td>
        <td className={/SELL|EXIT/.test(x.action) ? 'down' : 'up'}>{x.action}</td><td><b>{x.symbol}</b></td><td className="num">{x.qty ?? ''}</td>
        <td className="num">{x.price ?? '—'}</td><td className="small">{x.status}</td><td className="muted small">{x.note}</td></tr>)}</tbody></table></div></div>}

    {tab === 'calendar' && <div className="v2card"><h3>Daily P&amp;L <span className="r">last {days.length} sessions · after costs</span></h3>
      {!days.length ? <p className="muted">No sessions yet.</p> : <div className="calgrid">{days.map((day) => {
        const v = daily[day], a = Math.min(1, Math.abs(v) / (Math.max(...days.map((x) => Math.abs(daily[x]))) || 1));
        return <div key={day} title={`${day}: ${signed(v)}`} style={{ background: `color-mix(in srgb, ${v >= 0 ? 'var(--ok)' : 'var(--bad)'} ${Math.round(15 + 55 * a)}%, transparent)` }}>
          {day.slice(5)}<br /><b className="mono">{signed(v)}</b></div>; })}</div>}</div>}

    {tab === 'curves' && <div className="cards2">{Object.entries(d.curves).filter(([b]) => book === 'all' || b === book).map(([b, pts]) => (
      <div key={b} className="v2card"><h3>{names[b] || b}<span className="r">{pts.length} sessions · {inr(pts[pts.length - 1]?.[1])}</span></h3>
        <Spark values={pts.map((x) => x[1])} h={120} color={COLORS[b]} /></div>))}</div>}
  </>;
}
