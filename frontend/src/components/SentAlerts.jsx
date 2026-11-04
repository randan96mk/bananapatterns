import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

// 📨 Every alert the app sent (screens, intraday, guard, real orders, system): search, filter, open the stock.
const when = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true }) : '');
const strip = (html) => html.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
export default function SentAlerts({ onStock }) {
  const [d, setD] = useState(null);
  const [kind, setKind] = useState('');
  const [q, setQ] = useState('');
  const [days, setDays] = useState('7');
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(null);
  useEffect(() => {
    const id = setTimeout(() => api.alertsLog(new URLSearchParams({ limit: 300, kind, q, days, failed }).toString()).then(setD).catch(() => {}), 250);
    return () => clearTimeout(id);
  }, [kind, q, days, failed]);
  return <div className="panel">
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 8 }}><b>📨 Sent alerts</b>
      <input className="input" style={{ width: 200 }} placeholder="search text or stock" value={q} onChange={(e) => setQ(e.target.value)} />
      <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}><option value="">every kind</option>{(d?.kinds || []).map((k) => <option key={k} value={k}>{k}</option>)}</select>
      <span className="seg">{[['1', 'today'], ['7', '7 days'], ['30', '30 days'], ['0', 'all']].map(([k, l]) => <button key={k} className={days === k ? 'active' : ''} onClick={() => setDays(k)}>{l}</button>)}</span>
      <label className="small"><input type="checkbox" checked={failed} onChange={(e) => setFailed(e.target.checked)} /> failed only</label>
      <span className="muted small" style={{ marginLeft: 'auto' }}>{d ? `${d.rows.length} shown` : 'Loading…'}</span></div>
    {d && !d.rows.length && <p className="muted">Nothing sent for these filters.</p>}
    {d && d.rows.length > 0 && <div className="v2list">{d.rows.map((r) => <div key={r.key} className="it" style={{ cursor: 'pointer' }} onClick={() => setOpen(open === r.key ? null : r.key)}>
      <span className="muted small" style={{ width: 118 }}>{when(r.sent_at)}</span>
      <span className="chip small" style={{ width: 70, textAlign: 'center' }}>{r.kind}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        {r.symbol && <b style={{ marginRight: 6, cursor: 'pointer' }} onClick={(e) => { e.stopPropagation(); onStock && onStock(r.symbol); }}>{r.symbol}</b>}
        <span className={open === r.key ? '' : 'small'} style={open === r.key ? { whiteSpace: 'pre-wrap' } : { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', display: 'inline-block', maxWidth: '100%', verticalAlign: 'bottom' }}>{strip(r.text)}</span>
        {!r.ok && <div className="down small">failed: {r.error}</div>}{r.ok && r.error && <div className="muted small">{r.error}</div>}</div></div>)}</div>}
  </div>;
}
