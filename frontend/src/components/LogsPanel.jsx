import React, { useEffect, useMemo, useState } from 'react';
import { stamp, clock } from '../timefmt';
import * as api from '../services/api';
import { Head, Chip, Seg, Cards, Card, Panel } from './ui2';

// Warnings and errors persisted by the backend logger: level and module filters, a text filter, four counts, the lines.
const today = () => new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
export default function LogsPanel() {
  const [logs, setLogs] = useState([]);
  const [level, setLevel] = useState('');
  const [src, setSrc] = useState('');
  const [q, setQ] = useState('');
  const [at, setAt] = useState(null);
  const load = () => api.getLogs(level).then((x) => { setLogs(x); setAt(new Date().toISOString()); }).catch(() => {});
  useEffect(() => { load(); }, [level]); // eslint-disable-line

  const sources = useMemo(() => [...new Set(logs.map((l) => l.source).filter(Boolean))].sort(), [logs]);
  const shown = logs.filter((l) => (!src || l.source === src) && (!q || `${l.source} ${l.message}`.toLowerCase().includes(q.toLowerCase())));
  const d = today();
  const todayN = logs.filter((l) => (l.timestamp || '').slice(0, 10) === d).length;
  const warnN = logs.filter((l) => l.level === 'WARNING').length;
  const errN = logs.filter((l) => l.level === 'ERROR').length;
  const lastErr = logs.find((l) => l.level === 'ERROR');
  const lv = (l) => (l === 'ERROR' ? 'down' : l === 'WARNING' ? 'warn' : 'muted');

  return <div className="rd">
    <Head title="Logs" right={<>
      <Seg opts={[['', 'Warn + Error'], ['WARNING', 'Warn'], ['ERROR', 'Error']]} v={level} set={setLevel} />
      {sources.length > 0 && <Seg small opts={[['', 'All'], ...sources.map((s) => [s, s])]} v={src} set={setSrc} />}
      <input className="input" style={{ width: 180 }} placeholder="⌕ filter…" value={q} onChange={(e) => setQ(e.target.value)} />
      <button className="btn ghost small" onClick={load}>Refresh</button></>}>
      <Chip>{at ? <>loaded <b>{clock(at, true)}</b></> : 'Loading…'} · last {logs.length} lines</Chip>
      <Chip title="The INFO log is on disk, not in the database">full INFO log: <b>logs/bananapatterns.log</b></Chip>
    </Head>
    <Cards n={4}>
      <Card label="Today" value={todayN} num={todayN} sub={`lines today · of ${logs.length} loaded`} fact={level ? `${level} only` : 'warnings + errors'} />
      <Card label="Warnings" value={warnN} num={warnN} k={warnN ? 'warn' : ''} sub="in the loaded lines" fact={sources.length ? `${sources.length} modules` : '—'} />
      <Card label="Errors" value={errN} num={errN} k={errN ? 'down' : ''} sub="in the loaded lines" fact={errN ? 'see the last error →' : 'none'} />
      <Card label="Last error" value={lastErr ? clock(lastErr.timestamp) : '—'} sub={lastErr ? `${lastErr.source} · ${lastErr.message}` : 'no error on record'} fact={lastErr ? stamp(lastErr.timestamp) : ''} />
    </Cards>
    <Panel title="Lines" meta={shown.length} right={src || q ? `filtered from ${logs.length}` : null}>
      {!shown.length ? <p className="muted small rempty">{logs.length ? 'Nothing matches the filter.' : 'No warnings or errors on record.'}</p>
        : <div className="rlog">{shown.map((l) => <div key={l.id} title={l.details || l.message}>
          <span className="ts">{stamp(l.timestamp, true)}</span> <span className={`lv ${lv(l.level)}`}>{l.level === 'WARNING' ? 'WARN' : l.level}</span>
          <span className="muted">{l.source}</span> · {l.message}</div>)}</div>}
    </Panel>
  </div>;
}
