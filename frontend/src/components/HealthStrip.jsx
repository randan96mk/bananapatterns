import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { Chip } from './ui2';

// 🩺 One dot per source: green fresh, amber stale, red failing. Compact on Home, full list with details on Data,
// `chips` = one chip per check for a redesigned (.rd) panel; `onLoad` hands the whole health answer to the parent.
const when = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true }) : '');
const colour = (c) => (!c.ok ? 'var(--bad)' : c.warn ? 'var(--warn, #f5c518)' : 'var(--ok)');
export default function HealthStrip({ full = false, compact = false, chips = false, onGo, onLoad }) {   // compact: Home's chip line
  const [h, setH] = useState(null);
  useEffect(() => {
    const load = () => api.health().then((x) => { setH(x); onLoad && onLoad(x); }).catch(() => {});
    load(); const id = setInterval(load, 120000); return () => clearInterval(id);
  }, []); // eslint-disable-line
  if (!h) return null;
  const dot = (c) => <i style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 4, marginRight: 5, background: colour(c) }} />;
  if (chips) {
    return <div className="rwrap">
      <Chip c={h.red ? 'bad' : h.amber ? 'warn' : 'ok'}>{h.red ? `${h.red} failing` : h.amber ? `${h.amber} stale` : 'all fresh'} · checked {when(h.at)} · every 2 min</Chip>
      {h.checks.map((c) => <Chip key={c.key} title={`${c.detail}${c.at ? ` · ${when(c.at)}` : ''}`}>{dot(c)}{c.name} · <b>{c.state}</b></Chip>)}
    </div>;
  }
  if (compact) {                                           // Home: one line of chips; a chip's detail only on hover (title), never inline
    return <div className="hhealth" aria-label="Health">
      <Chip c={h.red ? 'bad' : h.amber ? 'warn' : 'ok'} dot={h.red ? 'bad' : h.amber ? 'warn' : 'ok'} title="Open System › Data" onClick={() => onGo && onGo('data')}>
        🩺 {h.red ? `${h.red} failing` : h.amber ? `${h.amber} stale` : 'all fresh'} · checked {when(h.at)}</Chip>
      {h.checks.map((c) => <Chip key={c.key} title={`${c.name}: ${c.state}${c.detail ? ` · ${c.detail}` : ''}${c.at ? ` · ${when(c.at)}` : ''}`}>{dot(c)}{c.name} · <b>{c.state}</b></Chip>)}
    </div>;
  }
  if (!full) {
    return <div className="v2card" style={{ padding: '8px 12px', display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center', cursor: onGo ? 'pointer' : 'default' }} onClick={() => onGo && onGo('data')} title="Open System › Data">
      <b className="small">🩺 {h.red ? <span className="down">{h.red} failing</span> : h.amber ? <span className="warn">{h.amber} stale</span> : <span className="up">all fresh</span>}</b>
      {h.checks.map((c) => <span key={c.key} className="small" title={`${c.detail}${c.at ? ` · ${when(c.at)}` : ''}`}>{dot(c)}{c.name} <span className="muted">{c.state}</span></span>)}
    </div>;
  }
  return <div className="panel"><h3>🩺 Health <span className="muted small">· checked {when(h.at)} · every 2 min</span></h3>
    <div className="v2list">{h.checks.map((c) => <div key={c.key} className="it"><span style={{ width: 14 }}>{dot(c)}</span>
      <div style={{ flex: 1 }}><b>{c.name}</b> <span className={!c.ok ? 'down' : c.warn ? 'warn' : 'muted'}>{c.state}</span>
        <div className="muted small">{c.detail}{c.at ? ` · ${when(c.at)}` : ''}</div></div></div>)}</div></div>;
}
