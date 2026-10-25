import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

// Universe refresh + candle sync with live progress and run history.
export default function DataPanel({ hasToken, onDataChanged }) {
  const [st, setSt] = useState(null);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [evs, setEvs] = useState(null);
  const [fils, setFils] = useState(null);
  const loadEvents = () => {
    api.eventsStatus().then(setEvs).catch(() => setEvs(null));
    api.filingsStatus().then(setFils).catch(() => setFils(null));
  };
  useEffect(() => { loadEvents(); }, []);
  const refreshEvents = async () => {
    setBusy(true); setMsg('Fetching the NSE results calendar…');
    try { const r = await api.refreshEvents(); setMsg(`Results calendar: ${r.stored} events stored.`); await loadEvents(); }
    catch (e) { setMsg(e.message); }
    setBusy(false);
  };
  const refreshFilings = async () => {
    setBusy(true); setMsg('Fetching NSE filings…');
    try { const r = await api.refreshFilings(); setMsg(`Filings: ${r.new} new.`); await loadEvents(); }
    catch (e) { setMsg(e.message); }
    setBusy(false);
  };

  const load = async () => {
    try { setSt(await api.ingestStatus()); } catch (e) { setMsg(e.message); }
  };
  useEffect(() => { load(); }, []);

  const running = st?.job?.running;
  useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [running]);
  useEffect(() => { if (st && !running && st.job.status !== 'idle') onDataChanged?.(); }, [running]); // eslint-disable-line

  const act = async (fn, label) => {
    setBusy(true); setMsg(`${label}…`);
    try {
      const r = await fn();
      if (r && r.members !== undefined) {
        setMsg(`Universe ${r.snapshot_date}: ${r.members} members, ${r.matched} mapped to Kite` +
          (r.unmatched.length ? `; unmatched: ${r.unmatched.join(', ')}` : '') +
          (r.excluded.length ? `; excluded by series: ${r.excluded.join(', ')}` : '') +
          (r.missing_indices.length ? `; missing indices: ${r.missing_indices.join(', ')}` : ''));
        onDataChanged?.();
      } else setMsg('');
      await load();
    } catch (e) { setMsg(e.message); }
    setBusy(false);
  };

  const job = st?.job;
  const pct = job && job.total ? Math.round((100 * job.done) / job.total) : 0;

  return (
    <div className="panel">
      <h3>Data pipeline</h3>
      <div className="row">
        <button className="btn" disabled={!hasToken || busy || running}
                onClick={() => act(api.refreshUniverse, 'Refreshing Nifty 500 list + instruments')}>
          1. Refresh universe
        </button>
        <button className="btn" disabled={!hasToken || busy || running}
                onClick={() => act(() => api.startSync(false), 'Starting sync')}>
          2. Sync candles (backfill / incremental)
        </button>
        <button className="btn ghost" disabled={!hasToken || busy || running}
                onClick={() => window.confirm('Refetch full history for every symbol (~6-7 min)?') &&
                  act(() => api.startSync(true), 'Starting full refetch')}>
          Full refetch
        </button>
        {running && <button className="btn danger" onClick={() => act(api.cancelSync, 'Cancelling')}>Cancel</button>}
      </div>
      <div className="row small">
        <span className="muted">Results calendar (NSE board meetings): {evs?.last_fetch
          ? `${evs.upcoming_results} upcoming results · fetched ${evs.last_fetch.slice(0, 16).replace('T', ' ')}`
          : 'not fetched yet'} · refreshed by the evening job</span>
        <button className="btn ghost" disabled={busy} onClick={refreshEvents}>Refresh results calendar</button>
      </div>
      <div className="row small">
        <span className="muted">Exchange filings (NSE announcements): {fils?.last_fetch
          ? `${fils.important_today} important today · fetched ${fils.last_fetch.slice(0, 16).replace('T', ' ')}`
          : 'not fetched yet'} · every 15 min, 07:00–23:00</span>
        <button className="btn ghost" disabled={busy} onClick={refreshFilings}>Refresh filings</button>
      </div>
      {!hasToken && <p className="muted">Log in with Kite first.</p>}
      {msg && <p className="msg">{msg}</p>}

      {job && job.status !== 'idle' && (
        <div className="job">
          <div className="progress"><div style={{ width: `${pct}%` }} /></div>
          <div className="muted">
            {job.status} · {job.done}/{job.total} · failed {job.failed} · refetched {job.refetched}
            {job.current && ` · now ${job.current}`}
          </div>
          <div className="muted">{job.message}</div>
          {job.errors.length > 0 && (
            <details open><summary>Errors ({job.errors.length})</summary>
              <ul className="errors">{job.errors.map((e) => <li key={e.symbol}><b>{e.symbol}</b> {e.error}</li>)}</ul>
            </details>
          )}
        </div>
      )}

      <h4>Recent runs</h4>
      <div className="hscroll"><table className="grid small">
        <thead><tr><th>#</th><th>Kind</th><th>Status</th><th>Started</th><th>Done</th><th>Failed</th><th>Message</th></tr></thead>
        <tbody>
          {(st?.runs || []).map((r) => (
            <tr key={r.id}><td>{r.id}</td><td>{r.kind}</td><td className={`s-${r.status}`}>{r.status}</td>
              <td>{r.started_at?.replace('T', ' ').slice(0, 16)}</td><td>{r.done}/{r.total}</td>
              <td>{r.failed}</td><td>{r.message}</td></tr>
          ))}
        </tbody>
      </table></div>
    </div>
  );
}
