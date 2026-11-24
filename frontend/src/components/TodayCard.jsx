import React, { useState } from 'react';
import * as api from '../services/api';
import { useLive, useLiveOr, push } from '../services/live';
import { clock } from '../timefmt';
import { inr, signed, cls } from '../fmt';
import { Tag, AiTag } from './ui2';
import { AiView } from './AiView';

// ☀️ Home › the Today card: one band under the health strip that says where the day is (the phase ribbon), the one
// thing to decide, the orders / what to watch / the books, and the morning brief's lines (the ONLY model text here,
// each with the AI tag). Built by code (GET /api/today-card, stream topic `today_card`, also joined into `overview`).
// Nothing on it acts by itself: "Skip SYM…" asks first (the same words as the morning skip) and then calls the same
// POST /api/real/skip as the Real page; the other buttons only navigate.
const STEPS = [['brief', '9:00 AM brief'], ['orders', '9:16 AM orders'], ['close', '3:30 PM close'], ['summary', '3:40 PM day summary'], ['night', '6:34 PM night batch']];
const NOW = { morning: 1, session: 2, close: 3, evening: 4 };      // the lit step per phase (the ones before are done)
const ICON = { morning: '☀️', session: '📈', close: '🌇', evening: '🌙' };
const FOOT = {
  morning: 'after 9:16 AM this card becomes the session view · after 3:40 PM the day summary · nothing here acts on its own',
  session: 'the session view · after 3:40 PM the day summary · nothing here acts on its own',
  close: 'the day summary · after the evening job, tonight\'s queue and tomorrow · nothing here acts on its own',
  evening: 'tonight\'s queue and tomorrow · at 9:00 AM the brief, at 9:16 AM the orders · nothing here acts on its own',
};
const ROWS = 4;                                                  // rows a column shows (fixed height); the rest go to "+n"
const dayLabel = (d) => { const x = d ? new Date(`${String(d).slice(0, 10)}T00:00:00`) : null; return x && !isNaN(x) ? ['weekday', 'day', 'month'].map((k) => x.toLocaleDateString('en-IN', { [k]: k === 'day' ? 'numeric' : 'short' })).join(' ') : ''; };
const txt = (x) => (x == null ? '' : typeof x === 'string' ? x : x.text || '');
// The same words the morning skip uses (the suggestion's confirm text, for today's send); the payload may bring its own.
export const skipConfirmText = (sym, when = 'today\'s') => `Skip ${sym} on ${when} send?\n\nThe BUY stays out for the day; nothing else changes. Tomorrow's plan is not affected, and you can switch it back on from the Real page until the send.`;

const SIDE = { EXIT: 'bad', SELL: 'bad', BUY: 'ok' };
const STATUS = { filled: 'ok', complete: 'ok', sent: 'a', failed: 'bad', rejected: 'bad', skipped: '', pending: '' };
function OrderRow({ o }) {
  const side = (o.action || o.side || '').toUpperCase();
  const what = [o.qty != null ? o.qty : null, o.text || o.note || o.why, o.price != null ? `@ ₹${o.price}` : null, o.at ? `at ${o.at}` : null].filter((x) => x != null && x !== '').join(' · ');
  const st = o.skip ? 'skipped' : o.status === 'pending' ? null : o.status;
  return <div className="tc-row" title={[side, o.symbol, what, o.status].filter(Boolean).join(' · ')}>
    {side && <Tag c={SIDE[side] || ''}>{side}</Tag>}<b>{o.symbol}</b><span className="f muted">{what}</span>
    {st && <Tag c={STATUS[st] ?? ''}>{st}</Tag>}
    {o.score != null ? <AiView compact v={{ score: o.score, verdict: o.verdict || '' }} /> : o.value != null ? <span className="mono muted">{inr(o.value)}</span> : null}</div>;
}
const WICON = { exit_watch: ['●', 'down'], suggestion: ['⚠', 'warn'], skip: ['⚠', 'warn'], flag: ['⚠', 'warn'], deal: ['⚠', 'warn'], 'exit-watch': ['●', 'down'], exit: ['●', 'down'], results: ['📅', 'muted'] };
const CONF = { high: 'w', medium: '', low: '' };
function WatchRow({ w }) {
  const [ic, k] = WICON[w.kind] || ['•', 'muted'];
  const raw = w.text || w.title || '';
  const what = w.symbol && raw.startsWith(`${w.symbol} `) ? raw.slice(w.symbol.length + 1).replace(/^·\s*/, '') : raw;   // the symbol is already in bold
  return <div className="tc-row" title={[w.symbol, what].filter(Boolean).join(' · ')}>
    <span className={k} style={{ width: 14, textAlign: 'center' }}>{w.icon || ic}</span>{w.symbol && <b>{w.symbol}</b>}<span className="f muted">{what}{w.until ? ` · until ${clock(w.until)}` : ''}</span>
    {w.score != null ? <AiView compact v={{ score: w.score, verdict: w.verdict || '' }} /> : w.confidence ? <Tag c={CONF[w.confidence] || ''}>{w.confidence}</Tag> : w.tag ? <Tag>{w.tag}</Tag> : null}</div>;
}
function BookRow({ b }) {
  const pnl = b.pnl ?? b.day_pnl ?? b.today;
  return <div className="tc-row" title={[b.label || b.book, b.text, b.value != null && inr(b.value), pnl != null && signed(pnl)].filter(Boolean).join(' · ')}>
    <span style={{ width: 18, textAlign: 'center' }}>{b.icon || '•'}</span><span className="f">{b.label || b.book}{b.text ? <span className="muted"> · {b.text}</span> : null}</span>
    {b.value != null && <span className="mono">{inr(b.value)}</span>}{pnl != null && <span className={`mono ${cls(pnl)}`}>{pnl > 0 ? '+' : ''}{inr(pnl)}</span>}</div>;
}
function Col({ title, meta, rows, Row, empty, note }) {
  const list = rows || [];
  const more = list.length - ROWS;
  return <div className="tc-col">
    <div className="kk"><span>{title}</span><span className="m">{meta ?? (list.length || '')}{more > 0 ? ` · +${more} more` : ''}</span></div>
    {list.length ? list.slice(0, ROWS).map((x, i) => <Row key={x.symbol || x.book || x.label || i} {...{ o: x, w: x, b: x }} />) : <div className="tc-row muted">{empty}</div>}
    {note && <div className="tc-row tc-note" title={txt(note)}>{note.ai ? <AiTag /> : null}<span className="f">{txt(note)}</span></div>}</div>;
}

// ✨ the phase-aware tiles (payload `panel`, backend/ai/today_tiles.py): each row is one fixed-height line (ellipsis), a
// sub line under it when it has one; [AI] rows carry the AI tag (text the model already wrote, stored); a tile that
// shows the last session's rows is greyed (.last) with the date in its header. Markers: .tt-tile[data-tile], .tt-row.
const TROWS = 8;                                                 // rows a tile shows (fixed height); the rest go to "+n"
const PICON = { evening: '🌙', premarket: '☀️', market: '📈' };
const ST = { done: ['✓', ''], run: ['…', 'run'], queued: ['·', 'q'], wait: ['·', 'q'], skipped: ['–', 'q'], failed: ['!', 'bad'] };
function Spark({ r }) {
  const pts = (r.pts || []).map(Number).filter((x) => Number.isFinite(x));
  if (pts.length < 2) return null;
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
  const xy = pts.map((v, i) => `${((i / (pts.length - 1)) * 200).toFixed(1)},${(26 - ((v - lo) / span) * 24).toFixed(1)}`).join(' ');
  return <div className="tt-spark"><svg viewBox="0 0 200 28" preserveAspectRatio="none" aria-hidden="true"><polyline className={r.c || ''} fill="none" strokeWidth="1.6" points={xy} /></svg></div>;
}
function TRow({ r }) {
  if (r.kind === 'spark') return <Spark r={r} />;
  if (r.kind === 'step') {
    const [ic, k] = ST[r.st] || ST.wait;
    return <div className="tt-step" title={[r.t, r.at].filter(Boolean).join(' · ')}><i className={k}>{ic}</i><span className="f">{r.t}</span><span className="s">{r.at}</span></div>;
  }
  const v = (r.v || []).filter((x) => x && x.t);
  return <div className={`tt-row${r.s || r.s_ai ? ' tt2' : ''}${r.dim ? ' dim' : ''}`} title={[r.icon, r.b, r.n, ...v.map((x) => x.t), r.s, r.s_ai].filter(Boolean).join(' ')}>
    <div className="l"><span className="n">{r.ai ? <AiTag /> : null}{r.icon ? <span className="ic">{r.icon}</span> : null}{r.b ? <b>{r.b}</b> : null}{r.n}</span>
      {(v.length > 0 || r.tag) && <span className="v">{v.map((x, i) => <span key={i} className={x.c || ''}>{i ? ' · ' : ''}{x.t}</span>)}{r.tag ? <Tag c={r.tag.c || ''}>{r.tag.t}</Tag> : null}</span>}</div>
    {(r.s || r.s_ai) && <div className="s">{r.s}{r.s && r.s_ai ? ' · ' : ''}{r.s_ai ? <><AiTag /> {r.s_ai}</> : null}</div>}</div>;
}
function Tile({ t }) {
  const list = t.rows || [];
  const more = list.length - TROWS;
  return <div className={`tc-col tt-tile${t.last ? ' last' : ''}`} data-tile={t.key}>
    <div className="kk"><span>{t.title}</span><span className="m">{t.meta}{more > 0 ? ` · +${more} more` : ''}</span></div>
    {list.length ? list.slice(0, TROWS).map((r, i) => <TRow key={i} r={r} />) : <div className="tc-row muted">{t.empty}</div>}</div>;
}

export default function TodayCard({ onGo }) {
  const ov = useLive('overview');
  const [fetched, reload] = useLiveOr('today_card', api.getTodayCard, 60000);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const c = ov?.today_card || fetched;
  if (!c || c.error || typeof c !== 'object') return null;
  const phase = NOW[c.phase] != null ? c.phase : 'morning';
  const pn = c.panel && Array.isArray(c.panel.tiles) ? c.panel : null;   // the phase-aware tiles (evening · pre-market · market hours)
  const lit = pn && Number.isInteger(pn.step) ? pn.step : NOW[phase];
  const cols = c.columns || {}, counts = c.counts || {};
  const one = c.the_one_thing ? (typeof c.the_one_thing === 'string' ? { text: c.the_one_thing } : c.the_one_thing) : null;
  const given = Object.fromEntries((Array.isArray(c.steps) ? c.steps : []).filter((x) => x && x.key).map((x) => [x.key, x.label]));
  const steps = STEPS.map(([k, l], i) => { const lab = given[k] || l;
    return [k === 'orders' && phase === 'morning' && counts.minutes_to_send != null ? `${lab} · in ${counts.minutes_to_send} min` : lab, i < lit ? 'done' : i === lit ? 'now' : '']; });
  const nSugg = counts.suggestions ?? counts.suggestions_open;

  // the buttons: what the payload asks for, plus the three that only navigate (never anything automatic)
  const acts = [...(c.actions || []), ...((pn && pn.buttons) || [])];
  const has = (kind, arg) => acts.some((a) => a.kind === kind && (arg == null || String(a.arg || '').includes(arg)));
  if (!acts.some((a) => a.label === 'Open the dry run')) acts.push({ label: 'Open the dry run', kind: 'go', arg: 'portfolio/real' });
  if (!has('open', 'suggestions') && !has('scroll')) acts.push({ label: `Suggestions${nSugg != null ? ` (${nSugg})` : ''}`, kind: 'open', arg: 'suggestions' });
  if (!has('go', 'plan')) acts.push({ label: 'Plan', kind: 'go', arg: 'research/plan' });
  const oneSkip = one && acts.find((a) => a.kind === 'skip' && a.arg && (one.symbol ? a.arg === one.symbol : txt(one).includes(a.arg)));

  const run = (a) => {
    if (a.kind === 'skip') {
      const sym = a.arg;
      if (!sym || busy) return;
      if (!window.confirm(a.confirm || a.confirm_text || skipConfirmText(sym))) return;
      setBusy(true); setMsg(null);
      api.realSkip(sym, true, 'Today card').then((r) => {
        if (r && Array.isArray(r.orders)) push('real_morning', r);
        setMsg({ text: `✓ ${sym} skipped for today · switch it back on from the Real page before the send` }); reload();
      }).catch((e) => setMsg({ text: e.message, bad: true })).finally(() => setBusy(false));
    } else if (a.kind === 'open' || a.kind === 'scroll') {
      const el = document.getElementById(a.arg || 'suggestions');
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if (a.arg) onGo && onGo(String(a.arg).split('/').pop());
  };
  const label = (a) => (a.kind === 'skip' ? `${(a.label || `Skip ${a.arg}`).replace(/…$/, '')}…` : a.label || a.arg);
  const ordersTitle = phase === 'morning' ? 'Orders at 9:16 AM' : phase === 'session' ? 'Orders today' : 'Orders';
  const ordersMeta = [cols.orders?.length || null, counts.cash_after != null && `cash after ${inr(counts.cash_after)}`, counts.free_slots != null && `${counts.free_slots} slots free`].filter(Boolean).join(' · ');

  return <div className={`tc-hero ph-${phase}`}>
    <div className="tc-hh"><span className="ttl">{pn ? PICON[pn.phase] || ICON[phase] : ICON[phase]} Today · {dayLabel(c.day) || 'today'}</span>
      <span className="dim">{c.at ? `${clock(c.at)} · ` : ''}from the brief, the plan, the books and the night batch · the model wrote only the tagged lines</span>
      <span className="tc-chip">{pn ? pn.chip : phase}</span></div>
    <div className="tc-ribbon">{steps.map(([l, st], i) => <span key={i} className={st || ''}><i />{l}</span>)}</div>
    {(pn?.one || c.headline) && <div className="tc-headline" title={txt(pn?.one || c.headline)}>{txt(pn?.one || c.headline)}</div>}
    {one && txt(one) && <div className="tc-one"><span className="k">The one thing</span>
      <span className="t" title={[txt(one), one.sub].filter(Boolean).join(' · ')}>{txt(one)}{one.sub ? <small> · {one.sub}</small> : null}</span>
      {oneSkip && <button type="button" className="btn primary small" disabled={busy} onClick={() => run(oneSkip)}>{label(oneSkip)}</button>}</div>}
    {pn ? <div className="tc-cols tt-cols">{pn.tiles.slice(0, 3).map((t) => <Tile key={t.key} t={t} />)}</div> : <div className="tc-cols">
      <Col title={ordersTitle} meta={ordersMeta} rows={cols.orders} Row={OrderRow} note={cols.orders_note}
        empty={phase === 'close' || phase === 'evening' ? 'no orders now · the next send is 9:16 AM on the next trading day' : 'no orders for this send'} />
      <Col title="Watch" rows={cols.watch} Row={WatchRow} empty="nothing to watch right now" />
      <Col title="Books" meta="last session" rows={cols.books} Row={BookRow} empty="no book figures yet" />
    </div>}
    {(c.brief_lines || []).length > 0 && pn?.phase !== 'premarket' && <div className="tc-brief">{c.brief_lines.map((l, i) => <div key={i} title={txt(l)}><AiTag of={l} /><span>{txt(l)}</span></div>)}</div>}
    <div className="tc-acts">
      {acts.map((a, i) => <button key={`${a.kind}${a.arg}${i}`} type="button" className={`btn small ${a.kind === 'skip' ? 'primary' : 'ghost'}`} disabled={a.kind === 'skip' && busy}
        title={a.kind === 'skip' ? 'asks first · then the same morning skip as the Real page (today only)' : undefined} onClick={() => run(a)}>{label(a)}</button>)}
      <span className="dim">{msg ? <span className={msg.bad ? 'down' : 'up'}>{msg.text}</span> : pn?.footer || c.footer || FOOT[phase]}</span>
    </div>
  </div>;
}
