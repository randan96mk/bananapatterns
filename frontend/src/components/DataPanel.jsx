import HealthStrip from './HealthStrip';
import React, { useEffect, useState } from 'react';
import { stamp } from '../timefmt';
import * as api from '../services/api';
import { Head, Chip, Panel, Box, FixedTable, Tag, Act, KV } from './ui2';

// Universe refresh + candle sync with live progress and run history, the NSE sources, backups, health.
const mb = (b) => `${(b / 1e6).toFixed(1)} MB`;
const STATUS = { ok: 'ok', partial: 'w', running: 'w', waiting: 'w', retry: 'w', failed: 'bad', aborted: 'bad' };
export default function DataPanel({ hasToken, onDataChanged }) {
  const [st, setSt] = useState(null);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [evs, setEvs] = useState(null);
  const [fils, setFils] = useState(null);
  const [deals, setDeals] = useState(null);
  const [bk, setBk] = useState(null);
  const [h, setH] = useState(null);
  const [sel, setSel] = useState('candles');
  useEffect(() => { api.backups().then(setBk).catch(() => {}); }, []);
  const loadEvents = () => {
    api.eventsStatus().then(setEvs).catch(() => setEvs(null));
    api.filingsStatus().then(setFils).catch(() => setFils(null));
    api.dealsStatus().then(setDeals).catch(() => setDeals(null));
  };
  useEffect(() => { loadEvents(); }, []);
  const refreshEvents = async () => {
    setBusy(true); setMsg('Fetching the NSE results calendar…');
    try { const r = await api.refreshEvents(); setMsg(`Results calendar: ${r.stored} events stored.`); await loadEvents(); }
    catch (e) { setMsg(e.message); }
    setBusy(false);
  };
  const refreshDeals = async () => {
    setBusy(true); setMsg('Fetching bulk / block deals and holder disclosures…');
    try { const r = await api.refreshDeals(); setMsg(`Deals: ${r.new} new.`); await loadEvents(); }
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
  const backupNow = () => api.backupsRun().then(() => api.backups().then(setBk)).catch((e) => setMsg(e.message));

  const job = st?.job;
  const pct = job && job.total ? Math.round((100 * job.done) / job.total) : 0;
  const chk = (k) => (h?.checks || []).find((c) => c.key === k);
  const stOf = (c) => (c ? [!c.ok ? '● failing' : c.warn ? '● stale' : '● ok', !c.ok ? 'err' : c.warn ? '' : 'ok'] : null);
  const lastRun = st?.runs?.[0];
  const off = !hasToken || busy || running;
  const disk = chk('disk');

  // The pipeline, in the order the evening job runs it. Each box shows the health check's state; the selected one
  // shows its buttons underneath.
  const steps = [
    { k: 'universe', icon: '🌐', title: 'Universe', c: chk('universe'), val: chk('universe')?.state, text: 'Nifty 500 · Kite tokens', long: 'Nifty 500 list and Kite instrument tokens',
      btns: <button className="btn small" disabled={off} onClick={() => act(api.refreshUniverse, 'Refreshing Nifty 500 list + instruments')}>Refresh universe</button> },
    { k: 'candles', icon: '🕯', title: 'Candles', c: chk('candles'), val: chk('candles')?.detail, text: chk('candles')?.state || 'daily · new days only',
      long: lastRun ? `last ${lastRun.kind} · ${stamp(lastRun.started_at)}` : 'daily candles · only the missing days are fetched',
      btns: <><button className="btn small" disabled={off} onClick={() => act(() => api.startSync(false), 'Starting sync')}>Sync candles (backfill / incremental)</button>
        <button className="btn ghost small" disabled={off} onClick={() => window.confirm('Refetch full history for every symbol (~6-7 min)?') && act(() => api.startSync(true), 'Starting full refetch')}>Full refetch</button>
        {running && <button className="btn danger small" onClick={() => act(api.cancelSync, 'Cancelling')}>Cancel</button>}</> },
    { k: 'corp', icon: '✂', title: 'Corp actions', c: chk('corp'), val: chk('corp')?.state, text: 'splits · bonuses', long: chk('corp')?.detail || 'found in the candles after each sync' },
    { k: 'filings', icon: '📰', title: 'Filings', c: chk('filings'), val: chk('filings')?.state, text: 'NSE · every 15 min', long: fils?.last_fetch ? `fetched ${stamp(fils.last_fetch)} · results calendar and deals come with them` : 'results calendar · filings · deals · 7:00 AM – 11:00 PM',
      btns: <><button className="btn ghost small" disabled={busy} onClick={refreshEvents}>Refresh results calendar</button>
        <button className="btn ghost small" disabled={busy} onClick={refreshFilings}>Refresh filings</button>
        <button className="btn ghost small" disabled={busy} onClick={refreshDeals}>Refresh deals</button></> },
    { k: 'scan', icon: '⌕', title: 'Scan', c: chk('scan'), val: chk('scan')?.state, text: 'VCP · Bases · Flag', long: 'runs in the evening job (Alerts & schedule → Run evening job now)' },
    { k: 'backup', icon: '💾', title: 'Backup', c: null, val: bk?.backups[0] ? mb(bk.backups[0].bytes) : '—', text: bk ? `${bk.backups.length} kept` : 'after the job', long: 'a copy of the database after the evening job',
      btns: <button className="btn small" onClick={backupNow}>Back up now</button> },
  ];
  const cur = steps.find((s) => s.k === sel);
  // a box shows the short form of a health state: "snapshot 2026-10-24" → "24 Oct"
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const shortVal = (v) => (typeof v === 'string' ? v.replace(/^(snapshot|as of|to)\s+/, '').replace(/(\d{4})-(\d{2})-(\d{2})/, (m, y, mo, d) => `${+d} ${MON[+mo - 1]}`) : v);

  return <div className="rd">
    <Head title="Data" right={<>
      <button className="btn" disabled={off} onClick={() => act(api.refreshUniverse, 'Refreshing Nifty 500 list + instruments')}>Refresh universe</button>
      <button className="btn" disabled={off} onClick={() => act(() => api.startSync(false), 'Starting sync')}>Sync candles</button>
      <button className="btn ghost" disabled={off} onClick={() => window.confirm('Refetch full history for every symbol (~6-7 min)?') && act(() => api.startSync(true), 'Starting full refetch')}>Full refetch</button>
      {running && <button className="btn danger" onClick={() => act(api.cancelSync, 'Cancelling')}>Cancel</button>}</>}>
      {disk && <Chip title={disk.detail}>{disk.detail} · <b>{disk.state}</b></Chip>}
      {!hasToken && <Chip c="warn">Log in with Kite first</Chip>}
      {running && <Chip c="ok">sync running · {pct}%</Chip>}
    </Head>
    <Panel title="Health" meta={h ? `${h.red} red · ${h.amber} amber` : null}><HealthStrip chips onLoad={setH} /></Panel>
    <div className="two"><div className="cols">
      <Panel title="Data pipeline" meta="evening job" right={msg ? <span className="msg">{msg}</span> : 'in the order the evening job runs them'}>
        <div className="rpipe" style={{ gridTemplateColumns: `repeat(${steps.length}, minmax(0, 1fr))` }}>
          {steps.map((s) => <Box key={s.k} icon={s.icon} title={s.title} outN={shortVal(s.val ?? '—')} text={s.text} status={stOf(s.c)} selected={sel === s.k} onClick={() => setSel(s.k)} />)}
        </div>
        <div className="rsteps">{steps.map((s, i) => <div key={s.k} className={`step ${sel === s.k ? 'sel' : ''}`} onClick={() => setSel(s.k)}><span className="c">{i + 1}</span><span>{s.title}</span><span className="n mono">{s.val ?? '—'}</span></div>)}</div>
        {cur && <div className="rwrap"><b style={{ color: 'var(--text)' }}>{cur.icon} {cur.title}</b><span>{cur.long}</span>{cur.btns}</div>}
        {job && job.status !== 'idle' && (
          <div className="rbody">
            <div className="progress"><div style={{ width: `${pct}%` }} /></div>
            <div className="muted small mono">{job.status} · {job.done}/{job.total} · failed {job.failed} · refetched {job.refetched}{job.current && ` · now ${job.current}`}</div>
            <div className="muted small">{job.message}</div>
            {job.errors.length > 0 && (
              <details open><summary>Errors ({job.errors.length})</summary>
                <ul className="errors">{job.errors.map((e) => <li key={e.symbol}><b>{e.symbol}</b> {e.error}</li>)}</ul>
              </details>)}
          </div>)}
      </Panel>
      <Panel title="Recent runs" meta={st?.runs?.length || null}>
        <FixedTable empty="No sync has run yet." cols={[{ h: 'When' }, { h: 'Kind', w: 110 }, { h: 'Done', w: 110, n: true }, { h: 'Failed', w: 70, n: true, lo: true }, { h: 'Message', w: 300, lo: true }, { h: 'Status', w: 80 }]}
          rows={(st?.runs || []).map((r) => ({ key: r.id, title: r.message, cells: [stamp(r.started_at), r.kind, `${r.done}/${r.total}`, r.failed, r.message, <Tag c={STATUS[r.status] || ''}>{r.status}</Tag>] }))} />
      </Panel>
    </div><div className="cols">
      <Panel title="💾 Backups" meta={bk ? `${bk.backups.length} kept` : null} right={bk ? `last ${bk.keep} kept` : ''}>
        {bk && !bk.backups.length && <p className="muted small rempty">None yet.</p>}
        {(bk?.backups || []).map((b) => <Act key={b.name} icon="💾" text={stamp(b.at)} meta={`${mb(b.bytes)} · ${b.name}`} />)}
        <div className="rwrap"><button className="btn small" onClick={backupNow}>Back up now</button><span>a copy every evening after the job</span></div>
        <div className="rnote">Folder {bk?.folder}. To restore: stop the app, copy a backup over data/bananapatterns.db, start it again.</div>
      </Panel>
      <Panel title="NSE sources">
        <KV pairs={[['Results calendar', evs?.last_fetch ? `${evs.upcoming_results} upcoming · ${stamp(evs.last_fetch)}` : 'not fetched yet'],
          ['Exchange filings', fils?.last_fetch ? `${fils.important_today} important today · ${stamp(fils.last_fetch)}` : 'not fetched yet'],
          ['Bulk / block deals', deals?.last_fetch ? `fetched ${stamp(deals.last_fetch)}` : 'not fetched yet']]} />
        <div className="rnote">Results calendar: NSE board meetings, refreshed by the evening job. Filings: every 15 min, 7:00 AM – 11:00 PM; deals come with the filings.</div>
      </Panel>
      {h && <Panel title="Storage">
        <KV pairs={[['Database', disk?.detail?.replace(/^database /, '') || '—'], ['Free disk', disk?.state || '—'],
          ['Daily candles', chk('candles')?.detail || '—'], ['1-minute candles', chk('intraday')?.state || '—'], ['Splits / bonuses', chk('corp')?.state || '—']]} />
      </Panel>}
    </div></div>
  </div>;
}
