import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { inr, cls } from '../fmt';

// 🧭 One stock across the app: stage on each screen, positions and trades in every book, Follow rows, alerts.
// Clicking a position or a Follow row draws its entry / stop on the chart (onOverlay).
const NAMES = { bases: 'Bases', vcp: 'VCP', flag: 'Flag' };
const day = (d) => (d ? new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' }) : '—');
const when = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true }) : '');
export default function StockEverywhere({ symbol, onOverlay }) {
  const [d, setD] = useState(null);
  useEffect(() => { setD(null); api.stockEverywhere(symbol).then(setD).catch(() => setD({ error: true })); }, [symbol]);
  if (!d) return <p className="muted small">Loading…</p>;
  if (d.error) return <p className="muted small">Not available.</p>;
  const scr = Object.entries(d.screens);
  const row = (label, p, extra) => <div key={label} className="it" style={{ cursor: onOverlay ? 'pointer' : 'default' }}
    onClick={() => onOverlay && onOverlay({ entry: p.entry, stop: p.stop, events: [] })}>
    <div style={{ flex: 1 }}><b>{label}</b> <span className="muted small">{extra}</span></div>
    <b className={cls(p.net)}>{inr(p.net)}</b></div>;
  return <div className="small">
    <div><b>Screens:</b> {scr.length ? scr.map(([k, s]) => <span key={k} style={{ marginRight: 10 }}><b>{NAMES[k] || k}</b> {s.live_stage || s.stage || <span className="muted">not on a card</span>}{s.pivot ? ` · pivot ${s.pivot}` : ''}{!s.live_stage && s.why ? <span className="muted"> · why not: {s.why}</span> : ''}</span>) : 'no data'}</div>
    {d.positions.length > 0 && <><div style={{ marginTop: 6 }}><b>Open now</b> <span className="muted">· click to draw entry / stop</span></div>
      <div className="v2list">{d.positions.map((p) => row(p.account, p, `${p.side} ${p.qty} @ ${p.entry} · stop ${p.stop ?? '—'} · since ${p.opened}`))}</div></>}
    {d.follow.length > 0 && <><div style={{ marginTop: 6 }}><b>Follow</b> <span className="muted">· every breakout bought blindly</span></div>
      <div className="v2list">{d.follow.map((p) => row(`${day(p.breakout_date)} · ${(p.screens || []).map((k) => NAMES[k] || k).join('+')}`, p,
        `${p.status}${p.entry ? ` · ${p.qty} @ ${p.entry}` : ''}${p.exit_date ? ` → ${day(p.exit_date)} @ ${p.exit} · ${p.reason}` : ''}`))}</div></>}
    {d.trades.length > 0 && <><div style={{ marginTop: 6 }}><b>Past trades</b></div>
      <div className="v2list">{d.trades.map((t, i) => row(`${t.account} · ${day(t.opened)} → ${day(t.closed)}`, t, `${t.qty} @ ${t.entry} → ${t.exit} · ${t.reason || ''}`))}</div></>}
    {d.alerts.length > 0 && <><div style={{ marginTop: 6 }}><b>Alerts sent</b></div>
      <div className="v2list">{d.alerts.map((a, i) => <div key={i} className="it"><span className="muted" style={{ width: 110 }}>{when(a.sent_at)}</span>
        <span className="chip small">{a.kind}</span><span style={{ flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{a.text.replace(/<[^>]+>/g, '')}</span></div>)}</div></>}
    {!d.positions.length && !d.follow.length && !d.trades.length && !d.alerts.length && <div className="muted" style={{ marginTop: 4 }}>No positions, trades or alerts for this stock.</div>}
  </div>;
}
