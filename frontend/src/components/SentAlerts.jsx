import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { Head, Chip, Seg, Switch, Panel, Act } from './ui2';

// 📨 Every alert the app sent (screens, intraday, guard, real orders, system): search, filter, open the stock.
const strip = (html) => html.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
const ICON = { intraday: '⚡', real: '₹', system: '⚙', digest: '📋', report: '🌙', breakout: '🚀', near_pivot: '🎯', forming: '🔵',
  below_pivot: '🔻', played_out: '🏁', guard: '🛡', failed: '⚠️', close_check: '🕒', plan: '☀', login: '🔑' };
const today = () => new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
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
  const rows = d?.rows || [];
  const t = today();
  const todayRows = rows.filter((r) => (r.sent_at || '').slice(0, 10) === t);
  const label = { 1: 'Today', 7: 'Last 7 days', 30: 'Last 30 days', 0: 'All time' }[days];
  return <div className="rd">
    <Head title="📨 Sent alerts" right={<>
      <Seg opts={[['1', 'Today'], ['7', '7 d'], ['30', '30 d'], ['0', 'All']]} v={days} set={setDays} />
      <Seg small opts={[['', 'All'], ...(d?.kinds || []).map((k) => [k, k])]} v={kind} set={setKind} />
      <input className="input" style={{ width: 180 }} placeholder="⌕ text or stock…" value={q} onChange={(e) => setQ(e.target.value)} />
      <Switch on={failed} onChange={setFailed} label="failed only" /></>}>
      <Chip>today <b>{todayRows.length}</b> · {todayRows.filter((r) => !r.ok).length} failed</Chip>
    </Head>
    <Panel title={label} meta={d ? rows.length : null} right={d ? `${rows.length} shown · newest first` : 'Loading…'}>
      {d && !rows.length && <p className="muted small rempty">Nothing sent for these filters.</p>}
      {rows.map((r) => <Act key={r.key} icon={ICON[r.kind] || '✉'} cls={r.ok ? 'up' : 'down'} onClick={() => setOpen(open === r.key ? null : r.key)}
        text={<span style={open === r.key ? { whiteSpace: 'pre-wrap' } : { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', display: 'block' }}>
          {r.symbol && <b style={{ marginRight: 6 }}>{r.symbol}</b>}{strip(r.text)}</span>}
        meta={<>{stamp(r.sent_at)} · {r.kind} · {r.ok ? 'delivered' : <span className="down">failed: {r.error}</span>}{r.ok && r.error ? ` · ${r.error}` : ''}</>}
        btn={r.symbol && onStock ? 'open' : null} onBtn={() => onStock(r.symbol)} />)}
    </Panel>
  </div>;
}
