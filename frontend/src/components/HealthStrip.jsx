import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

// 🩺 One dot per source: green fresh, amber stale, red failing. Compact on Home, full list with details on Data.
const when = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true }) : '');
export default function HealthStrip({ full = false, onGo }) {
  const [h, setH] = useState(null);
  useEffect(() => { const load = () => api.health().then(setH).catch(() => {}); load(); const id = setInterval(load, 120000); return () => clearInterval(id); }, []);
  if (!h) return null;
  const dot = (c) => <i style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 4, marginRight: 5, background: !c.ok ? 'var(--bad)' : c.warn ? 'var(--warn, #f5c518)' : 'var(--ok)' }} />;
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
