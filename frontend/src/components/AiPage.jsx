import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp, ampm, clock } from '../timefmt';
import { useLive } from '../services/live';
import { inr, signed, cls } from '../fmt';
import { Head, Chip, Switch, Tag, AiTag, Panel, FixedTable, Act, Cards, Card, Seg, Bar } from './ui2';
import { useNarrow, useIsPhone } from '../useMedia';
import { AiViewRecord } from './AiView';

// 🤖 System › AI: a local model next to the app that reads and explains and never trades. Everything here reads
// GET /api/ai (stream topic "ai"); the switches, the full stop, the model downloads and the test prompt act through
// the /api/ai routes. PR 1 is the infrastructure: switches, server, models, recent jobs, a test prompt. PR 2 adds the
// filing and deal notes: tonight's queue, the quality test set, the notes list (GET /api/ai/notes) and two cards. PR 3 adds the
// suggestions' scorecard (per kind, with the mute switches) and six cards, the trade "why" and morning brief notes in the
// feed, the brief panel and "Ask the app" (POST /api/ai/ask: one read-only tool, the day model, any time).
const ROW = { display: 'flex', gap: 10, alignItems: 'center', fontSize: 12.5, minHeight: 34, padding: '0 14px', borderBottom: '1px solid var(--rowline)', minWidth: 0 };
const LINE = { padding: '10px 14px 0', fontSize: 12.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
const COL = { gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' };
const Meta = ({ children, title }) => <span title={title} style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{children}</span>;
const Ell = ({ children, title, c = '' }) => <span className={c} title={title} style={{ display: 'block', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}>{children}</span>;
const n1 = (v) => (v == null ? '—' : Number(v).toFixed(1));
const n0 = (v) => (v == null ? '—' : Math.round(Number(v)).toLocaleString('en-IN'));
const secs = (ms) => (ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
// "qwen2.5:7b-instruct" -> "Qwen 2.5 7B" on the chips; anything else is shown as it is
const pretty = (name) => { const m = /^qwen(\d(?:\.\d)?):(\d+)b/i.exec(name || ''); return m ? `Qwen ${m[1]} ${m[2]}B` : name || '—'; };
const STATUS_TAG = { done: 'ok', failed: 'bad', running: 'a', queued: '', skipped: 'w' };
const SEV_TAG = { high: 'bad', medium: 'w' };                                    // low / info stay muted
const KIND_ICON = { filing: '📰', deal: '💬', why: '🧾', brief: '☀️' };
const BOOKN = { real: 'Real', paper: 'Paper', futures: 'Futures', options: 'Options' };
// 🤖 suggestions: the kinds as the backend names them (backend/ai/suggest.py KINDS / ICON) with the scorecard's words
const SKINDS = [['skip', '⏸', 'Skip a planned name', 'pledge, deal, results miss'], ['exit', '🚪', 'Exit a held name early', 'bad filing'], ['flag', '🚩', 'Flag on the dry run', 'deal, results near'],
  ['hold_options', '🧩', 'Hold the options book', 'event day, VIX jump'], ['roll_or_exit', '📐', 'Roll vs exit at T-2', 'thin next month, basis, below entry'], ['backtest', '🧪', 'Backtest a setting', 'Sunday night, from the week\'s stops']];
const SLABEL = Object.fromEntries(SKINDS.map(([k, , l]) => [k, l]));
const HOW = [['A note is written (filing, deal, results, trade why, roll check)', 'from rows already stored · the night model · tagged AI'],
  ['Code asks: does this note touch a planned, held or watched name, or a rule?', 'no match → stays a note only'],
  ['A suggestion template is filled: kind, action, expiry, the evidence rows', 'the model writes only the "why" sentence'],
  ['It appears on Home, in the AI chip, on Telegram', 'tap ✓ → the existing confirm dialog · ✗ → recorded with a reason'],
  ['Five sessions later code scores it: helped, cost, neutral', 'the scorecard decides which kinds you keep on']];
const RULES = ['never acts on its own · a suggestion with no tap simply expires', 'accept = the same confirm dialog and the same switch as a hand action',
  'evidence must be rows in the database · no row, no suggestion', 'one suggestion per name per day · at most 6 open at a time (settable)',
  'an exit suggestion never lowers a stop · a skip is for one day only', 'any kind can be switched off · the full stop silences all'];
const ofN = (a, b) => (b ? `${a} of ${b}` : '—');
const mins = (s) => (s == null ? '—' : s < 90 ? `~${Math.max(1, Math.round(s / 60))} min` : `~${Math.round(s / 60)} min`);
// "held" / "in the plan #3" / "on your watchlist" from the note's scope (why / rank come from code, never the model)
const scopeText = (sc) => (!sc ? '' : sc.book === 'held' ? 'held' : sc.book === 'plan' ? `in the plan${sc.rank ? ` #${sc.rank}` : ''}` : sc.book === 'watch' ? 'on your watchlist' : sc.why || '');
// a deal's source time is a date only ("14 Nov"); a filing's is a timestamp ("14 Nov, 6:41 PM")
const day = (iso) => (!iso ? '' : iso.length <= 10 ? stamp(`${iso}T12:00:00`).split(',')[0] : stamp(iso));
const FILTER_PARAMS = { all: {}, high: { severity: 'high' }, filing: { kind: 'filing' }, deal: { kind: 'deal' }, why: { kind: 'why' }, brief: { kind: 'brief' } };
const CHIP = { display: 'inline-block', fontSize: 10.5, padding: '0 6px', borderRadius: 8, background: 'var(--hover)', color: 'var(--muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '100%' };

// One note: icon · AI tag · symbol (a link to the source) · kind · severity · scope · title / the line / the acts / meta.
// Every line is one line with an ellipsis (the text may take two), so a row's height never depends on a number.
function NoteRow({ n }) {
  if (n.kind === 'brief') return <BriefRow n={n} />;
  const sym = n.url ? <a href={n.url} target="_blank" rel="noreferrer" style={{ fontWeight: 700, color: 'var(--blue)' }} title="open the source in a new tab">{n.symbol}</a> : <b>{n.symbol}</b>;
  const meta = [stamp(n.created), n.source_at ? `${n.kind === 'deal' ? 'deals' : 'filing'} ${day(n.source_at)}` : null, pretty(n.model), secs(n.ms), n.prompt_version,
    n.numbers_checked === false ? 'numbers —' : 'numbers ✓'].filter(Boolean).join(' · ');
  return <div style={{ display: 'flex', gap: 10, padding: '9px 14px', borderBottom: '1px solid var(--rowline)', minWidth: 0, alignItems: 'flex-start' }}
    title={`${n.symbol} · ${n.kind} · ${n.severity}${n.severity_model && n.severity_model !== n.severity ? ` (the model said ${n.severity_model}; the code decides)` : ''} · ${n.category || ''} · ${n.direction || ''} · confidence ${n.confidence ?? '—'}`}>
    <span style={{ width: 24, height: 24, borderRadius: 6, background: 'var(--hover)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, flex: 'none', marginTop: 1 }}>{KIND_ICON[n.kind] || '🤖'}</span>
    <div style={{ flex: 1, minWidth: 0, fontSize: 12.5 }}>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', minWidth: 0, whiteSpace: 'nowrap' }}>
        <AiTag of={n} />{sym}<span className="muted">· {n.kind}</span><Tag c={SEV_TAG[n.severity] || ''}>{n.severity}</Tag>
        <span className="muted" style={{ flex: 'none' }}>· {n.kind === 'why' ? `${BOOKN[n.book] || n.book || ''} trade` : scopeText(n.scope)} ·</span><span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{n.title}</span></div>
      <div style={{ color: 'var(--muted)', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', marginTop: 2 }} title={n.text}>{n.text}</div>
      {n.acts?.length > 0 && <div style={{ display: 'flex', gap: 6, marginTop: 4, minWidth: 0, overflow: 'hidden' }}>{n.acts.map((a) => <span key={a} style={CHIP} title={a}>→ {a}</span>)}</div>}
      <div className="muted" style={{ fontSize: 11, marginTop: 3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={meta}>{meta}</div>
    </div></div>;
}

// ☀️ A morning brief in the feed: the title line, then its lines one under another (each one line, ellipsis).
function BriefRow({ n }) {
  const lines = (n.text || '').split('\n').filter(Boolean);
  const meta = [stamp(n.created), pretty(n.model), secs(n.ms), n.prompt_version, n.numbers_checked === false ? 'numbers —' : 'numbers ✓', 'sent on Telegram'].filter(Boolean).join(' · ');
  return <div style={{ display: 'flex', gap: 10, padding: '9px 14px', borderBottom: '1px solid var(--rowline)', minWidth: 0, alignItems: 'flex-start' }} title={`${n.title} · written from the plans and filings · no action taken`}>
    <span style={{ width: 24, height: 24, borderRadius: 6, background: 'var(--hover)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, flex: 'none', marginTop: 1 }}>☀️</span>
    <div style={{ flex: 1, minWidth: 0, fontSize: 12.5 }}>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', minWidth: 0, whiteSpace: 'nowrap' }}><AiTag of={n} /><b>{n.title}</b><span className="muted">· brief</span><span className="muted" style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>· {lines.length} line{lines.length === 1 ? '' : 's'}</span></div>
      {lines.map((l, i) => <Ell key={i} title={l}><span className="muted">· </span>{l}</Ell>)}
      <div className="muted" style={{ fontSize: 11, marginTop: 3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={meta}>{meta}</div>
    </div></div>;
}

// 💬 One ask: the question, the answer (two lines at most), the "read: tool(args) · n s · read-only" meta.
function AskRow({ a, big }) {
  const meta = [a.read ? `read: ${a.read}` : a.ok ? 'read: —' : null, a.ms != null ? secs(a.ms) : null, 'read-only', a.who === 'telegram' ? 'Telegram' : null, stamp(a.created), a.status && a.status !== 'done' ? a.status : null].filter(Boolean).join(' · ');
  return <div style={{ display: 'flex', gap: 10, padding: '9px 14px', borderBottom: '1px solid var(--rowline)', minWidth: 0, alignItems: 'flex-start' }}>
    <span style={{ width: 24, height: 24, borderRadius: 6, background: 'var(--hover)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, flex: 'none', marginTop: 1 }}>💬</span>
    <div style={{ flex: 1, minWidth: 0, fontSize: 12.5 }}>
      <Ell title={a.question}><b>{a.question}</b></Ell>
      <div style={{ color: a.ok ? 'var(--text)' : 'var(--bad)', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: big ? 4 : 2, WebkitBoxOrient: 'vertical', marginTop: 2 }} title={a.answer || a.error || ''}>
        {a.answer && <><AiTag of={a} /> </>}{a.answer || (a.error ? `✗ ${a.error}` : a.status === 'queued' || a.status === 'running' ? 'thinking…' : '—')}</div>
      <div className="muted" style={{ fontSize: 11, marginTop: 3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={meta}>{meta}</div>
    </div></div>;
}

// A test-set score: label · bar · "n of 30" · the best run's n when it differs.
const Score = ({ label, n, best, total }) => <div style={{ display: 'grid', gridTemplateColumns: '78px minmax(0, 1fr) 64px 52px', gap: 8, alignItems: 'center', padding: '0 14px', minHeight: 30, fontSize: 12.5 }}>
  <span>{label}</span>
  <span style={{ height: 8, borderRadius: 4, background: 'var(--hover)', overflow: 'hidden', display: 'block' }}><i style={{ display: 'block', height: '100%', width: `${total ? (100 * (n || 0)) / total : 0}%`, background: total && n / total >= 0.9 ? 'var(--ok)' : total && n / total >= 0.75 ? 'var(--warn)' : 'var(--bad)' }} /></span>
  <span className="mono" style={{ textAlign: 'right' }}>{n ?? '—'} of {total ?? '—'}</span>
  <small className="muted mono" style={{ textAlign: 'right' }} title="the best run so far">{best != null && best !== n ? `best ${best}` : ''}</small></div>;

// A two-part meter (app · AI) out of the box's total; a fixed height, never wider than its panel.
const Gauge = ({ a, b, total, ca = 'var(--blue)', cb = 'var(--ok)' }) => {
  const p = (v) => (total ? Math.max(0, Math.min(100, (100 * (v || 0)) / total)) : 0);
  return <div style={{ height: 10, borderRadius: 5, background: 'var(--hover)', overflow: 'hidden', margin: '6px 14px 0', display: 'flex' }}>
    <i style={{ display: 'block', height: '100%', width: `${p(a)}%`, background: ca }} /><i style={{ display: 'block', height: '100%', width: `${p(b)}%`, background: cb }} /></div>;
};

export default function AiPage() {
  const live = useLive('ai');
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState('');
  const [rows, setRows] = useState(false);
  const [filt, setFilt] = useState('all');                                       // the notes filter: all / high / filing / deal
  const [notes, setNotes] = useState({ rows: [], total: 0, loading: true });
  const [sc, setSc] = useState(null);                                           // 🤖 the suggestions' scorecard (this month)
  const [q, setQ] = useState('');                                               // 💬 ask the app
  const [ans, setAns] = useState(null);
  const [asks, setAsks] = useState([]);
  const [earlier, setEarlier] = useState(false);
  const narrow = useNarrow();
  const phone = useIsPhone();
  const reload = () => setTick((t) => t + 1);
  useEffect(() => { api.getAi().then((x) => { setD(x); setErr(''); }).catch((e) => setErr(e.message)); }, [tick]);
  useEffect(() => { if (live && !live.error) setD(live); }, [live]);
  const notesToday = d?.notes?.today, tsRunning = !!d?.testset?.running, tsDone = d?.testset?.progress?.done;
  const sugSig = JSON.stringify(d?.suggestions || null), askSig = JSON.stringify(d?.asks || null);
  useEffect(() => { api.getAiScorecard().then(setSc).catch((e) => setErr(e.message)); }, [sugSig, tick]);     // the scorecard moves with the open count and after a mute
  useEffect(() => { api.getAiAsks(10).then((x) => setAsks(x.rows || [])).catch(() => {}); }, [askSig, tick]);
  useEffect(() => {                                                              // the first page of notes; again when a new note lands
    let on = true;
    api.getAiNotes({ limit: 20, offset: 0, ...FILTER_PARAMS[filt] }).then((x) => on && setNotes({ rows: x.rows || [], total: x.total || 0, loading: false }))
      .catch((e) => { if (on) { setNotes({ rows: [], total: 0, loading: false }); setErr(e.message); } });
    return () => { on = false; };
  }, [filt, notesToday, tick]);
  useEffect(() => {                                                              // while the test set runs, refresh the progress
    if (!tsRunning) return undefined;
    const t = setInterval(reload, 5000);
    return () => clearInterval(t);
  }, [tsRunning, tsDone]);
  const act = (key, p) => { setBusy(key); p.then(() => { setErr(''); reload(); }).catch((e) => setErr(e.message)).finally(() => setBusy('')); };
  const mute = (kind, on) => { if (window.confirm(on ? `Switch ${SLABEL[kind]} suggestions off?\n\nNone of this kind is born while it is off; the open ones stay until they expire or you decide. Nothing else changes.` : `Switch ${SLABEL[kind]} suggestions back on?\n\nThey are born again from tonight's notes.`)) act(`mute-${kind}`, api.aiSuggestionMute(kind, on)); };
  const ask = () => {                                                            // 💬 one question → one read-only tool → the answer; waits up to 60 s
    const text = q.trim();
    if (!text) return;
    setBusy('ask'); setAns({ question: text, status: 'running', ok: false });
    api.aiAsk(text).then((r) => { setAns(r); setQ(''); setErr(''); setAsks((xs) => [r, ...xs.filter((x) => x.id !== r.id)].slice(0, 10)); }).catch((e) => setAns({ question: text, error: e.message, ok: false, status: 'failed' })).finally(() => setBusy(''));
  };
  const setSw = (body, text) => { if (!text || window.confirm(text)) act('sw', api.putAiSettings(body)); };
  const STOP_TEXT = 'Full stop now?\n\nEvery loaded model is unloaded (the RAM and CPU are free at once) and every queued job is marked skipped. The container itself keeps idling at ~50 MB. Nothing runs again until you start it here or with Telegram “ai start”.';
  const START_TEXT = 'Clear the full stop?\n\nThe switches decide what runs from here; no model is loaded until the next job.';
  const fullStop = (on) => { if (window.confirm(on ? STOP_TEXT : START_TEXT)) act('stop', on ? api.aiStop() : api.aiStart()); };
  const pull = () => { if (window.confirm(`Download the missing models?\n\n${[d.models.night, d.models.day].filter((m) => !m.present).map((m) => m.name).join(' and ')} are pulled into data/ai in the background (a few GB; some minutes on the server). The page shows the progress; nothing else changes.`)) act('pull', api.aiPull()); };
  const test = () => act('test', api.aiTest());
  const rerun = () => { if (window.confirm('Queue tonight\'s batch again?\n\nWarm-up, notes, why lines, suggestions, ratings and the week\'s story are queued once more. What is already done is not repeated; the guard decides when each job runs. Nothing acts on its own.')) act('rerun', api.aiRerun()); };
  const runNotes = () => { if (window.confirm("Queue tonight's notes now?\n\nEvery in-scope filing and deal of the last days not noted yet goes into the queue (held names first, then the plan, then the watchlist). The guard still decides when each job runs; nothing is sent anywhere.")) act('notes', api.aiNotesRun()); };
  const runTestset = () => { if (window.confirm('Run the test set?\n\n30 hand-labelled filings go through the night model one by one (a few minutes); the score is kept next to the best run so far. No note or alert comes out of it.')) act('testset', api.aiTestsetRun()); };
  const more = () => { setBusy('more'); api.getAiNotes({ limit: 20, offset: notes.rows.length, ...FILTER_PARAMS[filt] }).then((x) => setNotes((s) => ({ ...s, rows: [...s.rows, ...(x.rows || [])], total: x.total ?? s.total }))).catch((e) => setErr(e.message)).finally(() => setBusy('')); };
  if (!d) return <div className="rd"><Panel>{err ? <div className="rbanner bad">{err}</div> : <p className="muted small rempty">Loading…</p>}</Panel></div>;

  const sw = d.switches || {}, S = d.server || {}, M = d.models || {}, Q = d.queue || {}, B = d.breaker || {}, st = d.settings || {}, L = d.limits || {}, P = M.pull || {}, T = d.last_test;
  const state = sw.stopped ? 'stopped' : !d.url_ok ? 'service unreachable' : Q.job ? 'running' : !sw.enabled ? 'off' : 'idle';
  const dot = sw.stopped || !d.url_ok ? 'bad' : Q.job ? 'ok' : sw.enabled ? 'ok' : 'warn';
  const aiGb = (S.ai_ram_mb || 0) / 1024;
  const used = S.ram_total_gb != null && S.ram_free_gb != null ? Math.max(0, S.ram_total_gb - S.ram_free_gb) : null;
  const appGb = used != null ? Math.max(0, used - aiGb) : null;
  const loaded = M.loaded || [];
  const bothPresent = !!(M.night?.present && M.day?.present);
  const diskGb = (M.night?.size_gb || 0) + (M.day?.size_gb || 0);
  const nPresent = [M.night, M.day].filter((m) => m?.present).length;
  const jobs = d.jobs || [];
  const N = d.notes || {}, LN = N.last_night, TS = d.testset || {}, TL = TS.last, TB = TS.best, PR = TS.progress || {};
  const tonight = Q.tonight || [], tonightN = tonight.reduce((a, r) => a + (r.n || 0), 0), tonightQueued = tonight.reduce((a, r) => a + (r.queued || 0), 0);
  const tonightS = tonight.reduce((a, r) => a + (r.est_s || 0), 0);
  const runWhy = sw.stopped ? 'full stop is on' : !Q.night_allowed ? Q.night_note || 'not allowed now' : tonightQueued ? `${tonightQueued} already queued` : !tonightN ? 'nothing to read' : '';
  const byKind = LN ? Object.entries(LN.by_kind || {}).map(([k, v]) => `${v} ${k}${v === 1 ? '' : 's'}`).join(' · ') : '';

  // 🤖 suggestions: the open count from the page, the month from the scorecard
  const SG = d.suggestions || {}, SK = sc?.kinds || {}, ST = sc?.totals || SG.scorecard_month || {};
  const sumK = (f) => Object.values(SK).reduce((a, k) => a + (k[f] || 0), 0);
  const helpedAcc = sumK('helped_when_accepted'), wouldHave = sumK('would_have_helped'), scoredAcc = Object.values(SK).reduce((a, k) => a + Math.min(k.accepted || 0, k.scored || 0), 0);
  const dismissedScored = Math.max(0, sumK('scored') - scoredAcc);
  const muted = SG.muted || [];
  const byKindText = Object.entries(SG.by_kind || {}).map(([k, v]) => `${k} ${v}`).join(' · ');
  const monthName = ST.month ? new Date(`${ST.month}-01T00:00:00`).toLocaleDateString('en-IN', { month: 'short' }) : '';
  const bestK = ST.best_kind ? SK[ST.best_kind] : null;
  const scCols = [{ h: 'Kind · what it proposes', ell: true }, { h: 'Made', w: 64, n: true }, { h: 'Accepted', w: 84, n: true, lo: true }, { h: 'Dismissed', w: 86, n: true, lo: true },
    { h: 'Helped', w: 84, n: true, title: 'accepted ones whose stock moved the way the suggestion said within 5 sessions' }, { h: 'Would have', w: 96, n: true, lo: true, title: 'dismissed or expired ones that would have' }, { h: 'Switch', w: 70, key: 'sw' }];
  const scRows = SKINDS.map(([k, icon, label, what]) => { const r = SK[k] || {}; const off = muted.includes(k); return { key: k, cls: off ? 'dim' : '', title: `${label} (${what}) · ${r.made || 0} made this month · ${r.open || 0} open · ${r.expired || 0} expired · ${r.scored || 0} scored${off ? ' · switched off' : ''}`,
    cells: [<><span style={{ marginRight: 6 }}>{icon}</span><b>{label}</b> <small className="muted">({what})</small></>, r.made || 0, r.accepted || 0, r.dismissed || 0,
      <span className="mono">{ofN(r.helped_when_accepted || 0, Math.min(r.accepted || 0, r.scored || 0))}</span>, <span className="mono">{ofN(r.would_have_helped || 0, Math.max(0, (r.scored || 0) - Math.min(r.accepted || 0, r.scored || 0)))}</span>,
      <Switch on={!off} disabled={!!busy} title={off ? 'switched off · none born · click to switch on' : 'on · click to switch this kind off'} onChange={(on) => mute(k, !on)} />] }; });
  const B2 = d.brief || {}, askOff = !sw.enabled ? 'the AI is switched off' : !st.ask ? 'ask is off (Settings → Local AI → ask)' : sw.stopped ? 'full stop is on' : '';
  const jobCols = [{ h: 'Job', ell: true }, { h: 'Time', w: 142 }, { h: 'Model', w: 150, lo: true }, { h: 'Took', w: 70, n: true }, { h: 'Status', w: 78 }, { h: 'Error', w: narrow ? 200 : 320, ell: true, lo: true }];
  const jobRows = jobs.map((j) => ({ key: j.id, title: `#${j.id} · ${j.kind} · ${j.key} · ${j.status}${j.note ? ` · ${j.note}` : ''}${j.tokens ? ` · ${j.tokens} tokens` : ''}${j.error ? ` · ${j.error}` : ''}`,
    cells: [<><b>{j.kind}</b> <small className="muted">· {j.key}</small></>, <span className="mono">{stamp(j.finished || j.started || j.created)}</span>, <span className="muted">{j.model || '—'}</span>,
      <span className="mono">{secs(j.ms)}</span>, <Tag c={STATUS_TAG[j.status] ?? ''}>{j.status}</Tag>, <Ell title={j.error || ''} c={j.error ? 'down' : 'muted'}>{j.error || (j.note || '—')}</Ell>] }));

  return <div className="rd">
    {err && <div className="rbanner bad">{err}</div>}
    {!d.url_ok && !err && <div className="rbanner warn">AI service unreachable at <span className="mono">{d.url}</span> · it is the <span className="mono">ollama</span> service in docker compose (<span className="mono">./docker.sh update</span> starts it); nothing on this page runs until it answers. {M.error}</div>}
    <Head title={phone ? '🤖 Local AI' : '🤖 Local AI · reads and explains · never trades'}>
      <Chip>runs on this server · nothing leaves it · free</Chip>
      <Chip title={`${M.night?.name || st.night_model} (night) · ${M.day?.name || st.day_model} (day) · settings → Local AI`}>night model <b>{pretty(M.night?.name || st.night_model)}</b> · day model <b>{pretty(M.day?.name || st.day_model)}</b></Chip>
      <Chip dot={dot} c={dot === 'bad' ? 'bad' : ''} title={d.allowed_note}>{state} · {loaded.length ? `${loaded.length} model${loaded.length === 1 ? '' : 's'} loaded` : 'model unloaded'} · <b className="mono">{n0(S.ai_ram_mb || 0)} MB</b> in use</Chip>
      <Chip title="the AI container's limits in docker-compose.yml · the app keeps the rest">limits <b>{L.cpus} cores · {L.mem_gb} GB</b> · low CPU priority next to the app</Chip>
      {d.url_ok && d.version && <Chip title={d.url}>service <b>v{d.version}</b></Chip>}
    </Head>
    <div className="cols c4">
      <Panel title="Switches" meta="all AI work">
        <div style={ROW}><Switch on={sw.enabled} disabled={!!busy || (!sw.enabled && !d.can_enable)} title={!sw.enabled && !d.can_enable ? d.enable_note : undefined}
          onChange={(on) => setSw({ enabled: on }, on ? 'Turn the AI on?\n\nNight notes run after the evening job with the night model; day answers use the day model only. It reads and explains: it never places an order, never changes a setting and never sends anything off this server.' : 'Turn the AI off?\n\nNothing new runs; a running job finishes. The models stay on disk.')} />
          <b className={!sw.enabled && !d.can_enable ? 'muted' : ''}>AI enabled</b><Meta title={d.enable_note || ''}><span className="muted">{d.can_enable || sw.enabled ? 'night notes · day answers' : d.enable_note}</span></Meta></div>
        <div style={ROW}><Switch on={sw.night_batch} disabled={!!busy} onChange={(on) => setSw({ night_batch: on }, on ? `Turn the night batch on?\n\nNight work runs after the evening job until ${ampm(st.night_end)} with the night model (${st.night_model}).` : 'Turn the night batch off?\n\nNo night work; day answers are not affected.')} />
          <b>Night batch</b><Meta><span className="muted">after the evening job → {ampm(st.night_end)} · {pretty(st.night_model)}</span></Meta></div>
        <div style={ROW}><Switch on={sw.day_answers} disabled={!!busy} onChange={(on) => setSw({ day_answers: on }, on ? `Turn day answers on?\n\n9:00 AM–3:40 PM the day model (${st.day_model}) only; never on the trading path.` : 'Turn day answers off?\n\nNo answers while the market is open; the night batch is not affected.')} />
          <b>Day answers</b><Meta title={`${st.day_model} only · 9:00 AM–3:40 PM · never on the trading path`}><span className="muted">{pretty(st.day_model)} only · 9:00 AM–3:40 PM · never on the trading path</span></Meta></div>
        <div style={ROW}><Switch red on={!!sw.stopped} disabled={!!busy} onChange={fullStop} /><b>Full stop</b><Meta title="unloads the model, empties the queue, frees RAM and CPU at once · nothing else changes"><span className="muted">unloads the model, empties the queue, frees RAM and CPU at once · nothing else changes</span></Meta></div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 14px 4px', minWidth: 0 }}>
          <button type="button" className="btn small" style={sw.stopped ? undefined : { color: 'var(--bad)', borderColor: 'var(--bad)', background: 'var(--bad-bg)' }} disabled={!!busy} onClick={() => fullStop(!sw.stopped)}>
            {busy === 'stop' ? 'Working…' : sw.stopped ? '▶ Start again…' : '⏹ Full stop now…'}</button>
          <Meta>asks to confirm · Telegram: <b>ai stop</b> / <b>ai start</b></Meta></div>
        <div className="rnote">{d.container_note}</div>
      </Panel>
      <Panel title="Server" meta={d.url_ok ? 'live' : 'the box'} right={<span className={d.allowed ? 'up' : 'muted'}>{d.allowed ? '✓ AI work allowed now' : `✗ ${d.allowed_note || 'not allowed now'}`}</span>}>
        <div style={LINE} title={`${n1(used)} GB used of ${n1(S.ram_total_gb)} GB · free ${n1(S.ram_free_gb)} GB · AI ${n0(S.ai_ram_mb || 0)} MB · app + system ${n1(appGb)} GB`}>
          RAM <b className="mono">{n1(used)} GB</b> of {n1(S.ram_total_gb)} GB · AI <b className="mono">{n0(S.ai_ram_mb || 0)} MB</b> {loaded.length ? '' : '(unloaded)'} · app <b className="mono">{n1(appGb)} GB</b></div>
        <Gauge a={appGb} b={aiGb} total={S.ram_total_gb} />
        <div style={LINE} title={`CPU ${S.cpu_pct == null ? 'not measured yet (needs two reads)' : `${S.cpu_pct}%`} · load (1 min) ${S.load1 ?? '—'} · the scheduler's last tick ${S.app_tick_ms == null ? 'not measured yet' : `${S.app_tick_ms} ms`}`}>
          CPU <b className="mono">{S.cpu_pct == null ? '—' : `${S.cpu_pct}%`}</b> · load <b className="mono">{S.load1 ?? '—'}</b> · app tick <b className="mono">{S.app_tick_ms == null ? '—' : `${n0(S.app_tick_ms)} ms`}</b></div>
        <Gauge a={S.cpu_pct} b={0} total={100} ca={S.cpu_pct >= 80 ? 'var(--bad)' : 'var(--blue)'} />
        <div className="rnote" style={{ paddingTop: 8 }}>guard: AI pauses itself if the app's tick takes &gt; {st.slow_tick_s} s or free RAM &lt; {st.min_free_ram_gb} GB · breaker: {st.breaker_failures} failures → off for {st.breaker_minutes} min · breaker now <b className={B.open ? 'down' : ''}>{B.text || 'closed'}</b>{B.failures ? ` · ${B.failures} failure${B.failures === 1 ? '' : 's'} in a row` : ''} · one call at most {st.timeout_s} s · the model stays loaded {st.keep_alive} after a call</div>
      </Panel>
      <Panel title="Queue · tonight" meta={`${Q.running || 0} running · ${Q.queued || 0} waiting`} right={<Meta title={Q.night_note || ''}>{Q.night_allowed ? <span className="up">✓ may run now</span> : <span className="muted">✗ {Q.night_note || 'not now'}</span>}</Meta>}>
        {tonightN ? tonight.filter((r) => r.n).map((r) => <div key={r.kind} style={ROW} title={`${r.label}: ${r.n} to read (${r.queued || 0} already queued) · about ${r.est_s} s`}>
          <span style={{ flex: 'none' }}>{KIND_ICON[r.kind] || '🤖'}</span><Meta><b>{r.label}</b> · {r.n} {r.kind === 'deal' ? `deal day${r.n === 1 ? '' : 's'}` : `filing${r.n === 1 ? '' : 's'}`}{r.queued ? <span className="muted"> · {r.queued} queued</span> : ''}</Meta>
          <span className="mono muted" style={{ flex: 'none' }}>{mins(r.est_s)}</span></div>)
          : <p className="muted small rempty">nothing to read · every in-scope filing and deal of the last {st.note_days} days has its note</p>}
        <div style={{ ...LINE, whiteSpace: 'normal' }} className="muted">{Q.night_allowed ? <>starts after the evening job · done in {mins(tonightS)} · order: held names first, then the plan, then the watchlist</>
          : <>the jobs wait for the night window: <b className="down">{Q.night_note || 'not allowed now'}</b> · at most {st.notes_per_night} a night · held names first</>}</div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 14px 4px', minWidth: 0 }}>
          <button type="button" className="btn small" disabled={!!busy || !!runWhy} onClick={runNotes} title={runWhy || 'queue tonight\'s notes now · the guard still decides when they run'}>{busy === 'notes' ? 'Queueing…' : 'Run tonight\'s notes now'}</button>
          <Meta title={runWhy}>{runWhy ? <span className="muted">{runWhy}</span> : <span className="muted">asks to confirm · {tonightN} job{tonightN === 1 ? '' : 's'}</span>}</Meta></div>
        <div className="rnote">one job per filing / deal day · the estimate is the average of the done jobs of that kind</div>
      </Panel>
      <Panel title="Quality" meta="test set" right={<Meta title={TL ? `last run ${stamp(TL.at)}` : ''}>{TS.running ? <span className="up">running · {PR.done ?? 0} of {PR.n ?? 30}</span> : TL ? `last ${stamp(TL.at)}` : 'not run yet'}</Meta>}>
        {TL ? <div style={{ padding: '6px 0 2px' }}>
          <Score label="Category" n={TL.category_ok} best={TB?.category_ok} total={TL.n} />
          <Score label="Severity" n={TL.severity_ok} best={TB?.severity_ok} total={TL.n} />
          <Score label="Direction" n={TL.direction_ok} best={TB?.direction_ok} total={TL.n} />
          <div style={LINE} className="muted" title={`${TL.model} · ${TL.prompt_version} · ${secs(TL.ms_avg)} a filing · ${TL.failed || 0} failed${TB && TB.at !== TL.at ? ` · best run ${stamp(TB.at)} (${TB.model} · ${TB.prompt_version})` : ''}`}>
            <b>{pretty(TL.model)}</b> · {TL.prompt_version} · <span className="mono">{secs(TL.ms_avg)}</span> · {TL.failed ? <span className="down">{TL.failed} failed</span> : 'none failed'}{TB && TB.at !== TL.at ? ` · best ${TB.category_ok + TB.severity_ok + TB.direction_ok}/${3 * TB.n}` : ' · the best run'}</div></div>
          : <p className="muted small rempty">not run yet · 30 hand-labelled filings through the night model; a prompt or model change must not drop below the best score</p>}
        {TS.running && <div style={{ padding: '6px 14px 0' }}><Gauge a={PR.done || 0} b={0} total={PR.n || 30} /></div>}
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 14px 4px', minWidth: 0 }}>
          <button type="button" className="btn small" disabled={!!busy || TS.running || !d.url_ok || !!sw.stopped} onClick={runTestset}
            title={TS.running ? 'running now' : !d.url_ok ? 'the service does not answer' : sw.stopped ? 'full stop is on' : 'send the 30 filings through the night model now'}>{TS.running ? `Running… ${PR.done ?? 0}/${PR.n ?? 30}` : busy === 'testset' ? 'Starting…' : 'Run test set'}</button>
          <Meta title={PR.error || ''}>{PR.error ? <span className="down">last run failed: {PR.error}</span> : <span className="muted">kind "test" · allowed outside the night window · a few minutes</span>}</Meta></div>
        {TL?.rows?.length ? <div style={{ padding: '0 14px 6px' }}>
          <button type="button" className="btn small" onClick={() => setRows(!rows)}>{rows ? 'Hide rows' : `Show rows · ${TL.rows.filter((r) => !(r.ok?.category && r.ok?.severity && r.ok?.direction)).length} missed`}</button>
          {rows && <div style={{ marginTop: 8, maxHeight: 260, overflowY: 'auto', fontSize: 11.5 }}>
            {TL.rows.map((r) => <div key={r.id} style={{ display: 'grid', gridTemplateColumns: '28px 92px minmax(0, 1fr)', gap: 8, padding: '4px 0', borderBottom: '1px solid var(--line)' }} title={r.error || ''}>
              <span className="mono muted">{r.id}</span><b className="mono">{r.symbol}</b>
              <span style={{ minWidth: 0 }}>{r.status && r.status !== 'done' ? <span className="down">{r.status}{r.error ? ` · ${r.error}` : ''}</span>
                : ['category', 'severity', 'direction'].map((k) => <span key={k} style={{ marginRight: 8 }} className={r.ok?.[k] ? 'muted' : 'down'}>{r.ok?.[k] ? '✓' : '✗'} {k.slice(0, 3)} {r.got?.[k] || '—'}{r.ok?.[k] ? '' : ` (want ${r.expected?.[k]})`}</span>)}
                {r.ms ? <span className="mono muted"> · {secs(r.ms)}</span> : null}</span></div>)}
          </div>}
        </div> : null}
        <div className="rnote">category · severity · direction against the hand labels · the severity on screen is set by code anyway</div>
      </Panel>
    </div>
    <Cards n={6}>
      <Card unit="n" label="Notes · last night" value={LN ? n0(LN.n) : '—'} num={LN?.n || 0} k={LN?.n ? '' : 'muted'} sub={LN ? byKind || 'no notes' : 'no night yet'}
        fact={LN ? <>took <b className="mono">{Math.max(1, Math.round((LN.ms_total || 0) / 60000))} min</b> · last at {clock(LN.at)}</> : <span className="muted">the first ones come after the evening job</span>}
        title={LN ? `night of ${LN.day} · ${LN.n} notes · ${byKind} · model time ${secs(LN.ms_total)} · ${LN.high} high` : 'no notes yet'} />
      <Card unit="n" label="Flagged for you" value={n0(N.high || 0)} num={N.high || 0} k={N.high ? 'down' : 'muted'} sub={N.high ? 'high today · on Home needs-a-look' : 'nothing high today'}
        fact={<>{n0(N.today || 0)} today{LN?.high ? <> · <b className="down">{LN.high}</b> high last night</> : ' · none high last night'}</>}
        title="high = pledge up, a results miss on a held name, legal, a rating cut, promoter selling · the code sets it, not the model · high notes go to Home and Telegram" />
      <Card unit="n" label="Jobs · today" value={n0(Q.done_today)} num={Q.done_today} k={Q.done_today ? '' : 'muted'} sub={`${Q.queued || 0} queued · ${Q.running || 0} running`}
        fact={Q.job ? <>running <b>{Q.job.kind}</b> · {Q.job.s} s</> : <span className="muted">nothing running</span>} title={`done today ${Q.done_today} · failed today ${Q.failed_today} · queue ${Q.queued} · running ${Q.running}`} />
      <Card unit="n" label="Failures · today" value={n0(Q.failed_today)} num={Q.failed_today} k={Q.failed_today ? 'down' : 'muted'} sub={`breaker ${B.text || 'closed'}`}
        fact={<>{B.open ? <b className="down">open</b> : 'closed'} · {B.failures || 0} in a row · trips at {st.breaker_failures}</>} title={`the breaker opens for ${st.breaker_minutes} min after ${st.breaker_failures} failures in a row; one success closes it`} />
      <Card unit="n" label="Model RAM in use" value={`${n0(S.ai_ram_mb || 0)} MB`} num={S.ai_ram_mb || 0} k={S.ai_ram_mb ? '' : 'muted'} sub={loaded.length ? loaded.map((m) => pretty(m.name)).join(' · ') : 'model unloaded'}
        fact={<>free RAM <b className="mono">{n1(S.ram_free_gb)} GB</b> · floor {st.min_free_ram_gb} GB</>} title={`the loaded models' RAM (from the service) · free RAM ${n1(S.ram_free_gb)} GB of ${n1(S.ram_total_gb)} GB`} />
      <Card unit="n" label="Models on disk" value={nPresent ? `${n1(diskGb)} GB` : '—'} num={diskGb} k={nPresent ? '' : 'muted'} sub={`${nPresent} of 2 downloaded`}
        fact={<span className="muted">{d.notes_count ? `notes ${n0(d.notes_count)} · never deleted` : 'data/ai · survive rebuilds'}</span>} title={`${M.night?.name}: ${M.night?.present ? `${n1(M.night.size_gb)} GB` : 'missing'} · ${M.day?.name}: ${M.day?.present ? `${n1(M.day.size_gb)} GB` : 'missing'}`} />
    </Cards>
    <Cards n={6}>
      <Card unit="n" label="Suggestions · open" value={n0(SG.open || 0)} num={SG.open || 0} k={SG.open ? '' : 'muted'} sub={byKindText || 'none open · they are born after the evening job'}
        fact={<>at most <b className="mono">{SG.max_open ?? 6}</b> open · on Home for your review</>} title={`open now ${SG.open || 0}${byKindText ? ` · ${byKindText}` : ''} · cap ${SG.max_open} · each one expires on its own`} />
      <Card unit="n" label={`This month${monthName ? ` · ${monthName}` : ''}`} value={n0(ST.made || 0)} num={ST.made || 0} k={ST.made ? '' : 'muted'} sub={`accepted ${ST.accepted || 0} · dismissed ${ST.dismissed || 0} · expired ${ST.expired || 0}`}
        fact={<>accepted <Bar pct={ST.made ? (100 * (ST.accepted || 0)) / ST.made : 0} ok /> <b className="mono">{ST.made ? Math.round((100 * (ST.accepted || 0)) / ST.made) : 0}%</b></>} title={`made ${ST.made || 0} this month · accepted ${ST.accepted || 0} · dismissed ${ST.dismissed || 0} · expired ${ST.expired || 0}`} />
      <Card label="Accepted · outcome" value={signed(ST.accepted_outcome_rs || 0)} num={ST.accepted_outcome_rs || 0} k={cls(ST.accepted_outcome_rs || 0)} sub={scoredAcc ? `${helpedAcc} of ${scoredAcc} helped · ${scoredAcc - helpedAcc} cost` : 'nothing scored yet · 5 sessions after'}
        fact={<>helped <Bar pct={scoredAcc ? (100 * helpedAcc) / scoredAcc : 0} ok /> <b className="mono">{scoredAcc ? Math.round((100 * helpedAcc) / scoredAcc) : 0}%</b></>} title="rupees the accepted suggestions saved (or cost): the 5-session move × the planned / held value · scored by code" />
      <Card label="Dismissed · outcome" value={signed(ST.dismissed_outcome_rs || 0)} num={ST.dismissed_outcome_rs || 0} k={cls(-(ST.dismissed_outcome_rs || 0))} sub={dismissedScored ? `would have helped ${wouldHave} of ${dismissedScored}` : 'nothing scored yet'}
        fact={<>you were right <Bar pct={dismissedScored ? (100 * (dismissedScored - wouldHave)) / dismissedScored : 0} ok /> <b className="mono">{dismissedScored ? Math.round((100 * (dismissedScored - wouldHave)) / dismissedScored) : 0}%</b></>} title="what the dismissed suggestions would have saved had you accepted them · a positive figure = they were right" />
      <Card unit="n" label="Best kind" value={ST.best_kind || '—'} num={bestK ? bestK.scored : 0} k={ST.best_kind ? 'up' : 'muted'}
        sub={bestK ? `${(bestK.helped_when_accepted || 0) + (bestK.would_have_helped || 0)} of ${bestK.scored} helped` : 'needs scored suggestions'} fact={<span className="muted">the highest helped share this month</span>} title={bestK ? `${SLABEL[ST.best_kind]} · ${(bestK.helped_when_accepted || 0) + (bestK.would_have_helped || 0)} of ${bestK.scored} scored helped` : 'no kind scored yet'} />
      <Card unit="n" label="Switched off" value={`${muted.length} kind${muted.length === 1 ? '' : 's'}`} num={muted.length} k={muted.length ? 'warn' : 'muted'} sub={muted.length ? muted.map((k) => SLABEL[k] || k).join(' · ') : 'any kind can be switched off'}
        fact={<span className="muted">the switches in the scorecard below</span>} title={muted.length ? `off: ${muted.join(', ')} · settings ai.suggest_muted` : 'every kind is on'} />
    </Cards>
    <AiViewRecord view={d.view} phone={phone} onChanged={reload} />
    <div className="rd">
      <Panel id="sugg-scorecard" title="Scorecard · how each kind has done" meta={ST.month ? `since 1 ${monthName}` : null} right={phone ? null : <Meta title="helped = the stock moved the way the suggestion said within 5 sessions · judged by code, not by the model">"helped" = the stock moved the way the suggestion said within 5 sessions · judged by code, not by the model</Meta>}>
        <FixedTable cols={scCols} rows={scRows} empty="Loading…" />
        <div className="rnote">"helped" = the stock moved the way the suggestion said within 5 sessions · judged by code, not by the model · a switch off stops that kind from being born (ai.suggest_muted) · open ones stay</div>
      </Panel>
    </div>
    <div className="two">
      <div className="rd" style={COL}>
        <Panel title={phone ? 'Notes' : 'Notes · tonight'} meta={notes.total ? `${notes.total} · newest first` : null} right={<Seg small v={filt} set={setFilt} opts={[['all', 'All'], ['high', 'High'], ['filing', 'Filings'], ['deal', 'Deals'], ['why', 'Why'], ['brief', 'Brief']]} />}>
          {notes.rows.length ? notes.rows.map((n) => <NoteRow key={n.id} n={n} />)
            : <p className="muted small rempty">{notes.loading ? 'Loading…' : filt === 'all' ? 'No notes yet · the first ones come after the evening job' : 'No notes match this filter'}</p>}
          {notes.rows.length > 0 && <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '8px 14px 10px', minWidth: 0 }}>
            <button type="button" className="btn small" disabled={!!busy || notes.rows.length >= notes.total} onClick={more}>{busy === 'more' ? 'Loading…' : notes.rows.length >= notes.total ? 'All shown' : 'More'}</button>
            <Meta>{notes.rows.length} of {notes.total} · click a symbol to open the filing · numbers come from code, never the model</Meta></div>}
        </Panel>
        <Panel id="ask" title="Ask the app" meta={phone ? 'read-only' : `answers from your data · ${pretty(st.day_model)} · read-only`} right={d.asks?.today ? <Meta>{d.asks.today} asked today</Meta> : null}>
          <div style={{ display: 'flex', gap: 8, padding: '10px 14px 4px', alignItems: 'center', minWidth: 0 }}>
            <input className="input" style={{ flex: 1, minWidth: 0 }} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !busy && !askOff) ask(); }} disabled={!!askOff}
              placeholder={askOff || 'why did we skip NMDC on Thursday? · what is near its stop? · how did futures do this week?'} aria-label="ask the app" />
            <button type="button" className="btn primary small" disabled={!!busy || !!askOff || !q.trim()} onClick={ask} title={askOff || 'one question → one read-only tool → the answer · up to 60 s'}>{busy === 'ask' ? 'Thinking…' : 'Ask'}</button></div>
          {ans && <AskRow a={ans} big />}
          {!ans && !asks.length && <p className="muted small rempty">Nothing asked yet · it answers from the app's own data: plans, positions, trades, filings, notes, backtests.</p>}
          {asks.length > 0 && <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '8px 14px 4px', minWidth: 0 }}>
            <button type="button" className="btn small" onClick={() => setEarlier(!earlier)}>{earlier ? 'Hide earlier' : `Earlier · ${asks.length}`}</button>
            <Meta>{d.asks?.last ? `last asked ${stamp(d.asks.last.created)}` : ''}</Meta></div>}
          {earlier && asks.filter((a) => a.id !== ans?.id).map((a) => <AskRow key={a.id} a={a} />)}
          <div className="rnote">it can only call a fixed list of read-only tools · it cannot switch, send, edit or delete anything · the same works on Telegram: <b>ask why did we skip NMDC</b></div>
        </Panel>
        <Panel title="Recent jobs" meta={jobs.length || null} right={<><Meta>every job stays in the database · newest first</Meta><button type="button" className="btn small" disabled={!!busy || !sw.enabled || !!sw.stopped} onClick={rerun} title="queue the night batch again (after a fix or a deploy); what is done is not repeated">{busy === 'rerun' ? 'Queuing…' : 'Re-run tonight'}</button></>}>
          <FixedTable cols={jobCols} rows={jobRows} empty="No AI job yet · the first comes from a test prompt or the night batch." />
        </Panel>
      </div>
      <div className="rd" style={COL}>
        <Panel title="Morning brief" meta={B2.day ? `${day(B2.day)} · ${clock(B2.last_at)}` : 'not written yet'} right={<Meta>{st.brief ? `at ${ampm(st.brief_time)} on a trading day` : 'off (Settings → Local AI → brief)'}</Meta>}>
          {B2.lines?.length ? B2.lines.map((l, i) => <Act key={i} icon="☀️" text={<Ell title={l}><AiTag of={B2} /> {l}</Ell>} />)
            : <p className="muted small rempty">not written yet · at {ampm(st.brief_time)} on a trading day · from the plans, the books and the night's notes · one Telegram message</p>}
          <div className="rnote">written from the plans and filings · no action taken · also in the Notes feed (Brief) and on System → Alerts as sent</div>
        </Panel>
        <Panel title="Models" meta={`${nPresent} of 2 on disk`} right={<Meta>{d.url_ok ? `service v${d.version || '?'}` : 'service unreachable'}</Meta>}>
        {[['Night', M.night, 'the evening and night work'], ['Day', M.day, 'quick answers while the market is open']].map(([label, m, what]) => <Act key={label} icon={m?.present ? '✓' : '○'} cls={m?.present ? 'up' : 'warn'}
          text={<><b>{label}</b> · {m?.name || '—'} <small className="muted">· {what}</small></>} meta={m?.present ? `present · ${n1(m.size_gb)} GB` : 'not downloaded'} />)}
        <div style={LINE} title={loaded.length ? loaded.map((m) => `${m.name} · ${n0(m.ram_mb)} MB`).join(' · ') : 'no model loaded now'}>
          loaded now: {loaded.length ? loaded.map((m) => <span key={m.name}><b>{pretty(m.name)}</b> <span className="mono">{n0(m.ram_mb)} MB</span> </span>) : <span className="muted">none · a model loads on the first job and stays {st.keep_alive}</span>}</div>
        {P.running && <div style={{ padding: '10px 14px 0' }}>
          <div style={{ fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>downloading <b>{P.model || '…'}</b> · <span className="mono">{Math.round(P.pct || 0)}%</span>{P.done?.length ? ` · done: ${P.done.join(', ')}` : ''}</div>
          <Gauge a={P.pct} b={0} total={100} /></div>}
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 14px 4px', minWidth: 0 }}>
          <button type="button" className="btn small" disabled={!!busy || P.running || bothPresent || !d.url_ok} onClick={pull}
            title={bothPresent ? 'both models are on disk' : !d.url_ok ? 'the service does not answer' : 'download the missing models into data/ai in the background'}>{P.running ? 'Downloading…' : bothPresent ? 'Both present ✓' : 'Download models'}</button>
          <Meta title={P.error || ''}>{P.error ? <span className="down">download failed: {P.error}</span> : P.at && !P.running ? <span className="muted">last download {stamp(P.at)}{P.done?.length ? ` · ${P.done.length} model${P.done.length === 1 ? '' : 's'}` : ''}</span> : <span className="muted">models live in data/ai · they survive rebuilds</span>}</Meta></div>
        <div className="rnote">{M.error ? <span className="down">{M.error}</span> : `${bothPresent ? 'both present · the AI enabled switch may go on' : d.enable_note || ''} · model names in Settings → Local AI`}</div>
        </Panel>
        <Panel title="Test prompt" meta={pretty(st.day_model)} right={<Meta>a tiny JSON prompt through the queue</Meta>}>
          {T ? <Act icon={T.ok ? '✓' : '✗'} cls={T.ok ? 'up' : 'down'} text={<><Tag c={T.ok ? 'ok' : 'bad'}>{T.ok ? 'ok' : T.status || 'failed'}</Tag> <span className="mono" title={T.text || ''}>{T.text || '—'}</span></>}
            meta={`${stamp(T.at)} · ${secs(T.ms)} · ${T.model || st.day_model}${T.error ? ` · ${T.error}` : ''}`} />
            : <p className="muted small rempty">No test yet · the day model answers {'{"ok": true, "word": "banana"}'} in 5–10 s once it is loaded.</p>}
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '8px 14px 10px', minWidth: 0 }}>
            <button type="button" className="btn small" disabled={!!busy || !d.url_ok || !!sw.stopped} onClick={test} title={!d.url_ok ? 'the service does not answer' : sw.stopped ? 'full stop is on' : 'send the test prompt now'}>{busy === 'test' ? 'Asking…' : 'Send a test prompt'}</button>
            <Meta>runs under the same guard as every job</Meta></div>
        </Panel>
        <Panel title="What it never does">
          {(d.never || []).map((x) => <Act key={x} icon="✗" cls="down" text={x} />)}
        </Panel>
      </div>
    </div>
    <div className="two">
      <Panel title="How a suggestion is made" meta="every night, after the notes">
        {HOW.map(([t, m], i) => <Act key={t} icon={i + 1} cls="act" text={t} meta={m} />)}
      </Panel>
      <Panel title="Rules">
        {RULES.map((t) => <Act key={t} icon="•" cls="act" text={t} />)}
      </Panel>
    </div>
    <div className="rnote">A local model in the container next to the app: it reads what the app already has and writes notes; it never decides or acts. Nothing leaves this server. Times are IST.</div>
  </div>;
}
