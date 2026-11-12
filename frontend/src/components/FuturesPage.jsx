import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp, ampm, clock } from '../timefmt';
import { inr, rs, signed, pct, cls, lakh } from '../fmt';
import { useLive, push } from '../services/live';
import AttentionStrip from './AttentionStrip';
import NextUp from './NextUp';
import ReplanPanel from './ReplanPanel';
import FuturesBacktest from './FuturesBacktest';
import { StopMeter } from './ui';
import { Head, Chip, Switch, Tag, Panel, FixedTable, KV, Act, Cards, Card, Bar, Seg } from './ui2';
import { useNarrow, useIsPhone } from '../useMedia';

// 📐 Stock futures: one book, two sides (long = E+ breakout, short = E− breakdown), each with a Paper and a Real switch.
// Paper fills from the daily futures candles; Real goes through the futures deployment's sender (a stub until step 4)
// and keeps the confirmations, the enable switch and the kill switch. Nothing here places an order in Kite.
const VIEWS = [['book', 'Book'], ['backtest', 'Backtest F']];
const STATUS = { sent: 'ok', filled: 'ok', failed: 'bad', skipped: 'w', added: 'a', removed: 'w', edited: '' };
const SIDE_ACT = { BUY: 'up', SELL: 'down', EXIT: 'down', ROLL: 'warn' };
const WHY = { stop: '🛑 stop', trail: '🪜 late trail', rule: '📉 trend exit', exit: '📉 exit', time: '⏱ time stop', roll: '↻ roll', roll_exit: '↻ roll · rule fired', expiry: '⏹ expiry' };
const n2 = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const wday = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { weekday: 'short' }) : '');
const todayIso = () => new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
const mmss = (s) => { const t = Math.max(0, s); const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), x = t % 60; return `${h ? `${h}:${String(m).padStart(2, '0')}` : m}:${String(x).padStart(2, '0')}`; };
const COL = { gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' };
const IN = { fontSize: 12, padding: '3px 6px', textAlign: 'right' };
const B = { fontSize: 11.5, padding: '3px 9px', whiteSpace: 'nowrap' };
const PEN = { fontSize: 11.5, padding: '2px 6px' };
const F = { display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', fontSize: 12, color: 'var(--muted)' };
const EFF = { fontSize: 11.5, color: 'var(--muted)', marginTop: 6, overflowWrap: 'anywhere' };
const Lbl = ({ children }) => <span style={{ display: 'inline-flex', gap: 4, alignItems: 'center', whiteSpace: 'nowrap' }}>{children}</span>;
const Meta = ({ children }) => <span style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{children}</span>;
const ROW = { display: 'flex', gap: 14, alignItems: 'center', fontSize: 12.5, minHeight: 34, padding: '0 14px', borderBottom: '1px solid var(--rowline)', minWidth: 0 };
const TILE = { background: 'var(--bg)', borderRadius: 8, padding: '7px 10px', height: 56, overflow: 'hidden', minWidth: 0 };
const TL = { fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '.04em', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
const TV = { display: 'block', fontSize: 14.5, fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', lineHeight: '24px' };
const Fact = ({ l, v, c, phone }) => <div style={TILE}><div style={TL}>{l}</div><b className={`mono ${c || ''}`} style={phone ? { ...TV, fontSize: 12.5 } : TV}>{v}</b></div>;
const Sw = ({ children }) => <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap' }}>{children}</span>;
const scrollTo = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
const sideTag = (s) => <Tag c={s === 'long' ? 'ok' : 'bad'}>{s === 'long' ? 'LONG' : 'SHORT'}</Tag>;
const Edited = ({ edits }) => (edits?.length ? <Tag title={edits.map((e) => `${e.field} ${e.from == null ? '' : `${typeof e.from === 'number' ? n2(e.from) : e.from} → `}${typeof e.to === 'number' ? n2(e.to) : e.to} · ${day(e.at)}${e.note ? ` · ${e.note}` : ''} · by you`).join('\n')}>edited</Tag> : null);

// ✎ Correct an open lot (lots · entry · stop · note): the effects come from a dry run and are repeated in the confirm.
function PositionEditor({ x, onDone, onErr }) {
  const [f, setF] = useState({ lots: String(x.lots), entry: n2(x.entry).replace(/,/g, ''), stop: n2(x.stop).replace(/,/g, ''), note: '' });
  const [eff, setEff] = useState(null);
  const [busy, setBusy] = useState(false);
  const body = { account: x.account };
  if (Number(f.lots) !== x.lots) body.lots = Number(f.lots);
  if (Math.abs(Number(f.entry) - x.entry) >= 0.005) body.entry = Number(f.entry);
  if (Math.abs(Number(f.stop) - x.stop) >= 0.005) body.stop = Number(f.stop);
  const changed = Object.keys(body).length > 1;
  const key = JSON.stringify(body);
  useEffect(() => {
    if (!changed) { setEff(null); return undefined; }
    const t = setTimeout(() => api.futuresEditPosition(x.symbol, { ...body, note: f.note }, true).then(setEff).catch((e) => setEff({ error: e.message })), 300);
    return () => clearTimeout(t);
  }, [key, changed, x.symbol]);
  const e = eff?.effects;
  const lines = e ? [body.entry != null && `entry ${n2(x.entry)} → ${n2(body.entry)}${e.est_cleared ? ' (est. cleared)' : ''}`, body.lots != null && `lots ${x.lots} → ${body.lots}`,
    body.stop != null && `stop ${n2(x.stop)} → ${n2(body.stop)}`, `MTM ${signed(e.pnl_before)} → ${signed(e.pnl_after)}`, `margin ${inr(e.margin)} · cash ${inr(e.cash)}`, `EXIT will send ${e.exit_qty} lot${e.exit_qty === 1 ? '' : 's'}`,
    `stop check at ${n2(eff.position?.stop)} (${e.stop_pct}% away)`, 'logged as "edited by you"'].filter(Boolean) : [];
  const save = () => {
    if (!changed || !e || !window.confirm(`Save the correction to ${x.symbol} (${x.tag})?\n\n${lines.join('\n')}\n\nNothing is sent to the deployment.`)) return;
    setBusy(true);
    api.futuresEditPosition(x.symbol, { ...body, note: f.note }).then(() => onDone(true)).catch((err) => onErr(err.message)).finally(() => setBusy(false));
  };
  const remove = () => {
    if (!window.confirm(`Remove ${x.symbol} (${x.tag}) from tracking?\n\nOnly when the entry never filled at the deployment: the margin (${inr(x.margin_est)}) and the charges come back as if never opened. No order is sent.`)) return;
    setBusy(true);
    api.futuresRemovePosition(x.symbol, f.note || 'never filled', x.account).then(() => onDone(true)).catch((err) => onErr(err.message)).finally(() => setBusy(false));
  };
  const set = (k) => (ev) => setF({ ...f, [k]: ev.target.value });
  return <div>
    <div style={F}><b style={{ color: 'var(--text)' }}>Edit {x.symbol} · {x.tag}</b>
      <Lbl>Lots <input className="input" type="number" min="1" step="1" value={f.lots} onChange={set('lots')} style={{ ...IN, width: 56 }} /></Lbl>
      <Lbl>Entry ₹ <input className="input" type="number" min="0" step="0.05" value={f.entry} onChange={set('entry')} style={{ ...IN, width: 96 }} /></Lbl>
      <Lbl>Stop ₹ <input className="input" type="number" min="0" step="0.05" value={f.stop} onChange={set('stop')} style={{ ...IN, width: 96 }} /></Lbl>
      <Lbl>Note <input className="input" type="text" value={f.note} onChange={set('note')} placeholder="filled by hand" style={{ ...IN, textAlign: 'left', width: 180 }} /></Lbl>
      <button type="button" className="btn primary" style={B} disabled={busy || !changed || !e} onClick={save}>Save…</button>
      <button type="button" className="btn ghost" style={B} disabled={busy} onClick={() => onDone(false)}>Cancel</button>
      <button type="button" className="btn ghost" style={{ ...B, color: 'var(--bad)', borderColor: 'var(--bad)', marginLeft: 'auto' }} disabled={busy} onClick={remove}>Remove from tracking…</button></div>
    <div style={EFF}>{eff?.error ? <span className="down">{eff.error}</span> : e ? <>Effects: {lines.map((l, i) => <span key={i}>{i ? ' · ' : ''}{l}</span>)}</>
      : changed ? 'Checking…' : x.est ? 'Estimated entry: the price seen when sending, corrected to the futures open in the evening unless you set it here.' : 'Change a value to see the effects before saving.'}</div></div>;
}

// + A lot opened by hand that the book should track and exit by its rules (one of the four accounts). Nothing is sent.
function AddPosition({ d, onDone, onErr }) {
  const [f, setF] = useState({ symbol: '', lots: '1', entry: '', day: todayIso(), side: 'long', book: 'paper', stop: '', note: '' });
  const [busy, setBusy] = useState(false);
  const set = (k) => (ev) => setF({ ...f, [k]: k === 'symbol' ? ev.target.value.toUpperCase() : ev.target.value });
  const ok = f.symbol.trim() && Number(f.lots) >= 1 && Number(f.entry) > 0 && /^\d{4}-\d{2}-\d{2}$/.test(f.day);
  const n = (d.positions || []).filter((p) => p.account === `${f.book}_${f.side}`).length, cap = d.settings?.max_pos;
  const add = () => {
    if (!ok) return;
    const stop = f.stop ? `stop ${n2(Number(f.stop))}` : 'the stop from the ATR rule (capped at 5%)';
    if (!window.confirm(`Track ${f.symbol} ${f.side.toUpperCase()} · ${Number(f.lots)} lot opened ${day(f.day)} at ${n2(Number(f.entry))} in ${f.book} · ${f.side}, ${stop}?\n\nIt takes a slot (${n + 1} of ${cap}) and the margin estimate from the cash. Nothing is sent; the book exits it by its rules.`)) return;
    setBusy(true);
    api.futuresAddPosition({ symbol: f.symbol.trim(), lots: Number(f.lots), entry: Number(f.entry), day: f.day, side: f.side, book: f.book, stop: f.stop ? Number(f.stop) : null, note: f.note })
      .then(() => onDone(true)).catch((err) => onErr(err.message)).finally(() => setBusy(false));
  };
  return <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--rowline)' }}>
    <div style={F}><b style={{ color: 'var(--text)' }}>Add a lot</b>
      <Lbl>Symbol <input className="input" type="text" value={f.symbol} onChange={set('symbol')} placeholder="HDFCBANK" style={{ ...IN, textAlign: 'left', width: 110 }} /></Lbl>
      <Lbl>Side <select className="input" value={f.side} onChange={set('side')} style={{ ...IN, textAlign: 'left' }}><option value="long">long</option><option value="short">short</option></select></Lbl>
      <Lbl>Book <select className="input" value={f.book} onChange={set('book')} style={{ ...IN, textAlign: 'left' }}><option value="paper">paper</option><option value="real">real</option></select></Lbl>
      <Lbl>Lots <input className="input" type="number" min="1" step="1" value={f.lots} onChange={set('lots')} style={{ ...IN, width: 56 }} /></Lbl>
      <Lbl>Entry ₹ <input className="input" type="number" min="0" step="0.05" value={f.entry} onChange={set('entry')} style={{ ...IN, width: 96 }} /></Lbl>
      <Lbl>Opened <input className="input" type="date" value={f.day} max={todayIso()} onChange={set('day')} style={{ ...IN, textAlign: 'left', width: 136 }} /></Lbl>
      <Lbl>Stop ₹ <input className="input" type="number" min="0" step="0.05" value={f.stop} onChange={set('stop')} placeholder="rule" style={{ ...IN, width: 96 }} /></Lbl>
      <Lbl>Note <input className="input" type="text" value={f.note} onChange={set('note')} style={{ ...IN, textAlign: 'left', width: 140 }} /></Lbl>
      <button type="button" className="btn primary" style={B} disabled={busy || !ok} onClick={add}>Add…</button>
      <button type="button" className="btn ghost" style={B} disabled={busy} onClick={() => onDone(false)}>Cancel</button></div>
    <div style={EFF}>The near contract comes from the stored contracts (lot size, expiry); the stop defaults to the ATR rule capped at {d.settings?.max_stop_pct}%; margin {d.settings?.margin_pct}% of the lot value · uses a slot ({n + 1} of {cap}) · nothing is sent.</div></div>;
}

// ☀️ The futures dry run (the same shapes as the Real page's): the plan of both sides with lots and margin, a skip
// switch per entry (EXITs always go), hold all. After the send it is the day's order log.
function MorningPanel({ onReplan, replanning, onErr }) {
  const streamed = useLive('futures_morning');
  const [m, setM] = useState(null);
  const [got, setGot] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState('');
  const phone = useIsPhone();
  useEffect(() => { api.futuresMorning().then((x) => { setM(x); setGot(Date.now()); }).catch(() => {}); }, []);
  useEffect(() => { if (streamed) { setM(streamed); setGot(Date.now()); } }, [streamed]);
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  if (!m || !m.visible) return null;
  const gone = Math.round((now - got) / 1000);
  const left = m.seconds_to_send - gone;
  const before = m.phase === 'before', after = m.phase === 'after';
  const send = ampm(m.send_at.slice(11, 16)), until = ampm(m.send_until.slice(11, 16));
  const set = (x) => { setM(x); setGot(Date.now()); push('futures_morning', x); };
  const act = (p, key) => { setBusy(key); p.then(set).catch((e) => onErr(e.message)).finally(() => setBusy('')); };
  const go = m.orders.filter((o) => !o.skip && o.status !== 'skipped');
  const entries = go.filter((o) => o.action !== 'EXIT'), exits = go.filter((o) => o.action === 'EXIT');
  const line = (o) => `${o.action} ${o.symbol} ${o.lots} lot${o.action !== 'EXIT' ? ` · stop ${rs(o.stop)} · margin ${inr(o.margin)}` : ''} · ${(o.books || []).join(' + ')} · ${o.side}`;
  const hold = () => {
    if (!entries.length) return;
    if (window.confirm(`Hold every futures entry today?\n\n${entries.map(line).join('\n')}\n\n${exits.length ? `The ${exits.length} EXIT${exits.length > 1 ? 's' : ''} still go${exits.length > 1 ? '' : 'es'}. ` : ''}A hold is for today only (paper fill and real send alike). The kill switch and the enable switch stay as they are.`)) act(api.futuresHold(), 'hold');
  };
  const real = (m.guard?.real_sides || []).length > 0 && m.guard.enabled && !m.guard.killed;
  const cols = [{ h: 'Contract' }, { h: 'Side', w: 62 }, { h: 'Books', w: 96, lo: true }, { h: 'Lots', w: 50, n: true }, { h: '≈ Price', w: 90, n: true, lo: true }, { h: 'Margin', w: 96, n: true },
    { h: 'Stop', w: 90, n: true, lo: true }, { h: 'Results', w: 80 }, { h: 'Why', w: 160, lo: true, ell: true }, { h: after ? 'Sent' : 'Take', w: 150 }];
  const cell = (o) => {
    if (o.status || after) return o.status ? <><Tag c={STATUS[o.status] || ''}>{o.status}</Tag>{o.at && <span className="mono small"> {clock(o.at, true)}</span>}</> : <span className="small">{o.books?.includes('real') ? 'not sent' : 'paper'}</span>;
    if (o.action === 'EXIT') return <Sw><Switch on disabled title="an exit always goes out" /><span className="small">exits always</span></Sw>;
    return <Sw><Switch on={!o.skip} disabled={!!busy} title={o.skip ? `skipped today by ${o.skip.by}${o.skip.note ? ` · ${o.skip.note}` : ''} · switch on to take it after all` : 'switch off to skip this entry today (paper and real)'} onChange={(on) => act(api.futuresSkip(o.symbol, !on, on ? '' : 'Futures page'), o.symbol)} />
      {o.skip ? <span className="small" style={{ color: 'var(--warn)' }}>{o.skip.by === 'you' ? 'skipped today' : 'results guard'}</span> : null}</Sw>;
  };
  const rows = m.orders.map((o) => ({ key: o.action + o.symbol, cells: [<><b>{o.symbol}</b> <small className="muted">{o.contract}</small></>, <Tag c={o.action === 'BUY' ? 'ok' : 'bad'}>{o.action}</Tag>,
    <span className="muted">{(o.books || []).join(' · ') || 'off'} · {o.side}</span>, o.lots, o.close ? n2(o.close) : '—', inr(o.margin), o.stop ? n2(o.stop) : '—',
    o.results_date ? <Tag c="w" title={`results ${day(o.results_date)} · in ${o.results_in} session${o.results_in === 1 ? '' : 's'}`}>⚠ {wday(o.results_date)}</Tag> : '—', <span className="muted">{o.why}</span>, cell(o)] }));
  rows.push(after ? `${m.counts.sent} sent · ${m.counts.failed} failed · ${m.counts.skipped} skipped${m.first_sent ? ` · ${clock(m.first_sent, true)} → ${clock(m.last_sent, true)}` : ''}`
    : `going out: ${exits.length} EXIT${exits.length === 1 ? '' : 's'} · ${entries.length} entr${entries.length === 1 ? 'y' : 'ies'} · margin needed ${inr(m.going_out.margin)} · ≈ ${inr(m.going_out.value)} notional${m.skipped.length ? ` · ${m.skipped.length} skipped today (${m.skipped.join(', ')})` : ''}${m.waiting ? ` · ${m.waiting} waiting` : ''}`);
  const right = after ? <Meta>{send} · <b className="mono">{m.counts.sent}</b> sent · <b className="mono">{m.counts.failed}</b> failed · <b className="mono">{m.counts.skipped}</b> skipped</Meta>
    : before ? <><Meta>{real ? 'real sides send' : 'paper fills at the open · real off'} · in <b className="mono" style={{ fontSize: 14, color: 'var(--text)' }}>{mmss(left)}</b></Meta><Switch on disabled title="always: the 9:00 reminder lists the orders; nothing is auto-retried" label="confirm before sending" /></>
    : <Meta>sending now · <b className="mono">{m.counts.sent}</b> sent · window closes {until}</Meta>;
  return <Panel id="morning" title={`This morning · futures orders at ${send}`} meta={after ? `${m.counts.sent} sent` : `${go.length} go`} right={right} className={before ? 'morning' : ''}>
    <div style={{ display: 'grid', gridTemplateColumns: `repeat(${phone ? 2 : 5}, minmax(0, 1fr))`, gap: 8, padding: '10px 14px' }}>
      {phone ? <><Fact phone l="Capital" v={inr(m.capital)} /><Fact phone l="Cash" v={inr(m.cash)} /></> : <Fact l="Capital · cash" v={`${inr(m.capital)} · ${inr(m.cash)}`} />}
      <Fact phone={phone} l="Slots" v={`${m.slots_used} of ${m.slots} · ${m.free} free`} />
      <Fact phone={phone} l="Filter · long / short" v={`${m.filter || '—'} / ${m.short_filter || '—'}`} c={m.filter === 'OFF' ? 'down' : m.filter ? 'up' : ''} />
      <Fact phone={phone} l="Margin used · needed" v={`${inr(m.margin_used)} · ${inr(m.going_out.margin)}`} />
      <Fact phone={phone} l="Guard" v={m.guard.killed ? 'kill switch ON' : !m.guard.enabled ? 'disabled' : real ? `real: ${m.guard.real_sides.join(', ')}` : 'paper only'} c={m.guard.killed || !m.guard.enabled ? 'down' : 'up'} />
    </div>
    <FixedTable cols={cols} rows={rows} empty="No orders today." />
    {!after && <div style={{ display: 'flex', gap: 8, padding: '10px 14px', alignItems: 'center', flexWrap: 'wrap' }}>
      <button type="button" className="btn ghost small" disabled={replanning || !!busy} onClick={onReplan} title="rebuild the plan from the evening's data and the accounts as they are now · nothing is sent">{replanning ? 'Planning…' : '↻ Re-evaluate plan'}</button>
      <button type="button" className="btn ghost small" style={{ color: 'var(--bad)', borderColor: 'var(--bad)' }} disabled={!!busy || !entries.length} onClick={hold}>{busy === 'hold' ? 'Holding…' : 'Hold all today…'}</button>
      <span className="muted" style={{ fontSize: 11, marginLeft: 'auto', minWidth: 0 }}>a skip is for today only (paper and real) · the kill switch and enable switch stay as they are · after {send} this panel becomes the order log</span></div>}
    {after && <div className="rnote">the day's send{m.first_sent ? ` · ${m.gap_s} s apart · ${clock(m.first_sent, true)} → ${clock(m.last_sent, true)}` : ''} · a skipped entry was never taken and is not retried today · tomorrow's plan comes at the evening run</div>}
  </Panel>;
}

const ALERTS = [['☀️', '9:00', 'futures orders with lots, margin needed, skip switches · reply fut skip SYMBOL / fut hold'], ['↻', 'T-3 / T-2', 'roll notice, then the roll lines'],
  ['🛑', 'live', 'stop hit (futures price) · daily loss limit reached · margin > 70%'], ['⚠', 'daily', 'MWPL ban on a held name · results within 3 sessions · lot size change'], ['📝', 'paper', 'short-side signals tagged “paper · short”']];

export default function FuturesPage() {
  const [view, setView] = useState(() => (window.location.hash.split('/')[3] === 'backtest' ? 'backtest' : 'book'));   // #/strategies/futures/backtest opens the report
  const live = useLive('futures');
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [tick, setTick] = useState(0);
  const [edit, setEdit] = useState(null);
  const [adding, setAdding] = useState(false);
  const [cap, setCap] = useState('');
  const [rp, setRp] = useState(null);
  const [replanning, setReplanning] = useState(false);
  const narrow = useNarrow();
  const phone = useIsPhone();
  const reload = () => setTick((t) => t + 1);
  useEffect(() => { api.getFutures().then((x) => { setD(x); setErr(''); setCap(String(x.capital)); }).catch((e) => setErr(e.message)); }, [tick]);
  useEffect(() => { if (live && !live.error) { setD(live); setCap((c) => c || String(live.capital)); } }, [live]);
  const replan = () => { setReplanning(true); api.futuresReplan().then((x) => { setRp(x); reload(); }).catch((e) => setErr(e.message)).finally(() => setReplanning(false)); };
  const setSw = (body) => api.futuresSettings(body).then(reload).catch((e) => setErr(e.message));
  const flipReal = (side, on) => {
    if (!on || window.confirm(`Turn the ${side} side's REAL switch on?\n\nIts plan then goes to the futures deployment at ${ampm(d.send.send_time)} (one lot a name), with the 9:00 dry run, the skip switches, the kill switch and the daily loss limit. Paper keeps running from the same plan. The sender is not wired yet (step 4): orders are logged as failed until it is.`)) setSw({ [`${side}_real`]: on });
  };
  const kill = (on) => {
    if (!on || window.confirm('Stop ALL futures orders now?\n\nWith a real side on, the deployment is told kill_switch=1 once (it closes everything and stops entries). Nothing is sent until you turn the kill switch off; paper keeps its marks.')) api.futuresKill(on).then(reload).catch((e) => setErr(e.message));
  };
  const saveCap = () => {
    const v = Number(cap), now = d.capital;
    if (!(v >= 10000) || v === now) return;
    const text = d.started ? `Change the futures capital from ${inr(now)} to ${inr(v)}? This counts as ${v > now ? 'adding' : 'withdrawing'} ${inr(Math.abs(v - now))} on every account (paper and real of both sides): cash changes, the profit so far stays.` : `Start the futures book with ${inr(v)}?`;
    if (window.confirm(text)) api.futuresCapital(v).then((r) => { if (r.replan?.status === 'ok') setRp(r.replan); reload(); }).catch((e) => setErr(e.message));
  };
  if (!d) return <div className="rd"><Panel>{err ? <div className="rbanner bad">{err}</div> : <p className="muted small rempty">Loading…</p>}</Panel></div>;
  const S = d.sides, G = d.guards, st = d.settings || {}, sp = d.short_paper || {}, roll = d.rolls || {};
  const long = S.long, short = S.short;
  const realOn = (long.real || short.real) && G.enabled && !G.kill;
  const active = (d.positions || []).filter((p) => p.account === d.primary || (!d.primary && p.book === 'paper'));
  const waiting = d.waiting || [];
  const marginPct = G.margin?.pct || 0;
  const lossPct = G.daily_loss?.limit ? Math.min(100, (100 * G.daily_loss.used) / G.daily_loss.limit) : 0;
  const biggest = active.reduce((a, p) => (p.risk > (a?.risk || 0) ? p : a), null);
  const done = (changed) => { setEdit(null); setAdding(false); if (changed) reload(); };
  const vsNifty = d.curve?.nifty ? null : null;
  const todayLong = (d.accounts?.[long.real ? 'real_long' : 'paper_long']?.mtm_today) ?? 0, todayShort = d.accounts?.paper_short?.mtm_today ?? 0;
  const posCols = [{ h: 'Contract', ell: true }, { h: 'Side', w: 62 }, { h: 'Book', w: 62, lo: true }, { h: 'Lots', w: 50, n: true }, { h: 'Entry', w: 90, n: true, lo: true }, { h: 'Now', w: 90, n: true },
    { h: 'MTM', w: 96, n: true }, { h: 'Stop · distance', w: narrow ? 160 : 200, ell: true }, { h: 'Margin', w: 100, n: true, lo: true, nw: true }, { h: 'Expiry', w: 120, lo: true }, { h: '', w: 44, key: 'edit' }];
  const posRows = (d.positions || []).flatMap((x) => [{ key: x.account + x.symbol, title: `${x.tag} · ${x.days} days · opened ${day(x.in)} · lot ${x.lot_size} · costs ${inr(x.costs)} · after costs ${signed(x.net)}${x.best ? ` · best close ${rs(x.best)}` : ''}${x.manual_stop ? ' · stop set by you (the trail may only raise it)' : ''}${x.rolls ? ` · rolled ${x.rolls}×` : ''}`,
    cells: [<><b>{x.symbol}</b> <small className="muted">{x.contract} · lot {x.lot_size}</small>{x.est && <Tag c="w" title="estimated: the price seen when sending, corrected to the futures open in the evening">est.</Tag>}<Edited edits={x.edits} />{x.manual && <Tag c="a">by hand</Tag>}{x.stop_pct != null && x.stop_pct < 2 && <Tag c="w">near stop</Tag>}{x.trailed && <Tag c="ok" title={`late trail: 25% under the best close once +100% · best ${rs(x.best)}`}>🪜 trail</Tag>}</>,
      sideTag(x.side), <Tag c={x.book === 'real' ? 'a' : ''}>{x.book}</Tag>, x.lots, n2(x.entry), <>{n2(x.last)}{x.live && <span className="livedot" title="live" />}</>, <b className={cls(x.mtm)}>{signed(x.mtm)}</b>,
      <><StopMeter last={x.side === 'short' ? -x.last : x.last} stop={x.side === 'short' ? -x.stop : x.stop} /> <span className="mono">{n2(x.stop)}</span></>, inr(x.margin_est),
      <>{day(x.expiry)}{x.roll_due ? <span className="warn"> · roll T-{st.roll_days}</span> : x.sessions_to_expiry != null ? <span className="muted"> · {x.sessions_to_expiry} sessions</span> : ''}</>,
      <button type="button" className={`btn ${edit === x.account + x.symbol ? 'primary' : 'ghost'}`} style={PEN} title="correct lots / entry / stop · remove from tracking" onClick={() => { setEdit(edit === x.account + x.symbol ? null : x.account + x.symbol); setAdding(false); }}>✎</button>] },
  ...(edit === x.account + x.symbol ? [{ key: `${x.account}${x.symbol}-edit`, span: <PositionEditor x={x} onDone={done} onErr={setErr} /> }] : [])]);
  if (waiting.length) posRows.push(`waiting · ${waiting.slice(0, 4).map((w) => `${w.symbol} ${w.side} (${w.why})`).join(' · ')}${waiting.length > 4 ? ` · +${waiting.length - 4} more` : ''}`);
  const shortCols = [{ h: 'Contract', ell: true }, { h: 'Signal', w: 80 }, { h: 'Entry', w: 90, n: true }, { h: 'Exit', w: 90, n: true, lo: true }, { h: 'Paper P&L', w: 100, n: true }, { h: 'Why', w: 130, lo: true, ell: true }, { h: 'Status', w: 150 }];
  const shortOpen = (d.positions || []).filter((p) => p.account === 'paper_short');
  const shortRows = [...shortOpen.map((p) => ({ key: `o${p.symbol}`, cells: [<b>{p.symbol}</b>, sideTag('short'), n2(p.entry), '—', <b className={cls(p.mtm)}>{signed(p.mtm)}</b>, <span className="muted">E− breakdown</span>, <Tag c="a">open · {p.days} days</Tag>] })),
    ...(sp.trades || []).map((t) => ({ key: `t${t.id}`, cells: [<b>{t.symbol}</b>, sideTag('short'), n2(t.entry), n2(t.exit), <b className={cls(t.net)}>{signed(t.net)}</b>, <span className="muted">{WHY[t.why] || t.why}</span>, <Tag c={t.why === 'stop' ? 'bad' : ''}>closed · {WHY[t.why] || t.why}</Tag>] }))];
  const tradeCols = [{ h: 'Contract', ell: true }, { h: 'Book', w: 110, lo: true }, { h: 'Opened · closed', w: 150, lo: true }, { h: 'Lots', w: 50, n: true }, { h: 'Entry', w: 90, n: true, lo: true, nw: true }, { h: 'Exit', w: 90, n: true, lo: true }, { h: 'Costs', w: 80, n: true, lo: true, nw: true }, { h: 'After', w: 104, n: true }, { h: 'Why', w: 130 }];
  const tradeRows = (d.trades || []).map((t) => ({ key: t.id, cells: [<><b>{t.symbol}</b> <small className="muted">{t.contract}</small></>, <span className="muted">{t.tag}</span>, `${day(t.in)} · ${day(t.out)}`, t.lots, n2(t.entry), n2(t.exit), <span className="muted">{inr(t.costs)}</span>, <b className={cls(t.net)}>{signed(t.net)}</b>, WHY[t.why] || t.why] }));
  const planRows = d.plan || [];
  const expiryChip = d.next_expiry ? <Chip title="the near month's expiry and the roll session (T-2)">{new Date(`${d.next_expiry}T00:00:00`).toLocaleDateString('en-IN', { month: 'short' })} expiry <b>{day(d.next_expiry)}</b>{d.roll_day && <> · roll day <b>{wday(d.roll_day)} {day(d.roll_day)}</b> (T-{st.roll_days})</>}</Chip> : <Chip>no contracts yet · run the futures data step</Chip>;
  const capRow = <div className="rform"><div className="fr"><span>Capital<small>one lot a name · margin {st.margin_pct}% of the lot value · a change after the start is money added / withdrawn on every account</small></span>
    <span style={{ whiteSpace: 'nowrap' }}>₹<input className="input" type="number" min="10000" step="10000" value={cap} onChange={(e) => setCap(e.target.value)} style={{ width: 110 }} />{' '}
      <button type="button" className="btn small" onClick={saveCap} disabled={!(Number(cap) >= 10000) || Number(cap) === d.capital}>Save</button></span></div></div>;

  return <div className="rd">
    <AttentionStrip tab="futures" />
    {err && <div className="rbanner bad">{err}</div>}
    <Head title="📐 Stock Futures · E+ on the near month" right={<>
      <Seg opts={VIEWS} v={view} set={setView} small />
      {view === 'book' && <><button type="button" className="btn primary small" disabled={replanning || !d.started} onClick={replan} title="rebuild the plan from the evening's data and the accounts as they are now · nothing is sent">{replanning ? 'Planning…' : '↻ Re-evaluate plan'}</button>
        <button type="button" className="btn small" onClick={() => scrollTo('fut-orderlog')}>Order log</button>
        <button type="button" className="btn small" onClick={() => setView('backtest')}>Backtest F</button></>}</>}>
      <Chip>capital <b>{inr(d.capital)}</b> · separate book</Chip>
      {expiryChip}
      <Chip dot={G.kill ? 'bad' : realOn ? 'ok' : 'warn'} c={G.kill ? 'bad' : ''}>{G.kill ? 'kill switch ON · nothing is sent' : !G.enabled ? 'futures OFF · paper only' : <>● <b>{active.length} of {st.max_pos}</b> active · {waiting.length} waiting{waiting.length ? ` (${waiting[0].why})` : ''}</>}</Chip>
      <Chip>{d.started ? <>since {day(d.start_date)} · updated {stamp(d.updated_at)}</> : 'starts at the next evening run'} · {realOn ? <b className="up">{[long.real && 'long real', short.real && 'short real'].filter(Boolean).join(' · ')}</b> : <b className="warn">PAPER</b>}</Chip>
    </Head>
    {view === 'backtest' ? <FuturesBacktest onBook={() => setView('book')} /> : <>
      <div className="cols c4">
        <Panel title="Long side" meta="E+ breakout">
          <div style={ROW}><Switch on={long.paper} onChange={(on) => setSw({ long_paper: on })} /><b>Paper</b><Meta><span className="muted">simulated · lots and margin as real · fills at the futures open</span></Meta></div>
          <div style={ROW}><Switch red on={long.real} onChange={(on) => flipReal('long', on)} /><b>Real</b><Meta><span className="muted">sends to the futures deployment · confirm · kill switch</span></Meta></div>
          <div className="rnote">backtest {long.verdict == null ? 'not run' : long.verdict ? '✅' : '❌'}{long.robust?.of ? ` ${long.robust.pass} of ${long.robust.of} robust` : ''} · tags “paper · long” / “real · long”</div>
        </Panel>
        <Panel title="Short side" meta="E− breakdown">
          <div style={ROW}><Switch on={short.paper} onChange={(on) => setSw({ short_paper: on })} /><b>Paper</b><Meta><span className="muted">on by default · tagged “paper · short”</span></Meta></div>
          <div style={ROW}><Switch red on={short.real} disabled={!short.real_allowed} title={short.real_allowed ? 'the short backtest passed: real is allowed' : 'locked until Backtest F passes the short side (dev / val / OOS and robustness)'} onChange={(on) => flipReal('short', on)} /><b>Real</b><Meta><span className="muted">{short.real_allowed ? 'allowed · the backtest passed' : 'locked until the backtest passes'}</span></Meta></div>
          <div className="rnote">backtest {short.verdict == null ? 'not run' : short.verdict ? '✅' : '❌'}{short.robust?.of ? ` ${short.robust.pass} of ${short.robust.of} robust` : ''} · {short.real_allowed ? 'real allowed' : 'paper only'}</div>
        </Panel>
        <Panel title="Guards">
          <div style={ROW}><Meta><span className="muted">Kill switch · futures</span></Meta><Switch red on={G.kill} onChange={(on) => kill(on)} /></div>
          <div style={ROW}><Meta><span className="muted">Enabled</span></Meta><Switch on={G.enabled} onChange={(on) => setSw({ enabled: on })} title="OFF: nothing is sent; paper keeps its marks" /></div>
          <KV pairs={[['Daily loss limit', <span className={G.daily_loss?.hit ? 'down' : ''}>{inr(G.daily_loss?.used)} / {inr(G.daily_loss?.limit)}</span>],
            ['Notional cap', `${lakh(G.notional?.used)} / ${lakh(G.notional?.cap)}`],
            ['Free cash floor', <span className={G.cash_floor?.ok ? 'up' : 'down'}>{inr(G.cash_floor?.free)} {G.cash_floor?.ok ? '≥' : '<'} {inr(G.cash_floor?.floor)}</span>],
            ['Heat', <span className={G.heat?.pct > G.heat?.cap ? 'down' : ''}>{pct(G.heat?.pct)} / {pct(G.heat?.cap)}</span>]]} />
        </Panel>
        <Panel title="Margin" meta={live ? 'live' : 'estimate'}>
          <div style={{ padding: '10px 14px 0', fontSize: 12.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>used <b className="mono">{inr(d.margin_used)}</b> · <span className="mono">{marginPct.toFixed(0)}%</span> · MTM <b className={`mono ${cls(d.mtm_today)}`}>{signed(d.mtm_today)}</b></div>
          <div style={{ height: 10, borderRadius: 5, background: 'var(--hover)', overflow: 'hidden', margin: '6px 14px' }}><i style={{ display: 'block', height: '100%', width: `${Math.min(100, marginPct)}%`, background: marginPct >= 80 ? 'var(--bad)' : marginPct >= 70 ? 'var(--warn)' : 'var(--ok)' }} /></div>
          <div className="rnote" style={{ paddingTop: 0 }}>warn at {G.margin?.warn_at}% · no new lots at {G.margin?.block_at}%{G.margin?.warn ? ' · ⚠️ over the warning line' : ''}</div>
          {capRow}
        </Panel>
      </div>
      <Cards n={6}>
        <Card label="Futures · today" value={signed(d.mtm_today)} num={d.mtm_today} k={cls(d.mtm_today)} spark={d.curve?.net?.slice(-30)} live={!!live}
          sub={<>long {long.real ? 'real' : 'paper'} <span className={cls(todayLong)}>{signed(todayLong)}</span> · short paper <span className={cls(todayShort)}>{signed(todayShort)}</span></>}
          fact={<>daily loss <Bar pct={lossPct} bad={G.daily_loss?.hit} /> {lossPct.toFixed(0)}%</>} title={`MTM today on the ${d.primary || 'paper_long'} account · daily loss ${inr(G.daily_loss?.used)} of ${inr(G.daily_loss?.limit)}`} />
        <Card label={`Book · since ${day(d.start_date) || 'start'}`} value={inr(d.value)} num={d.value} k={cls(d.net)} spark={d.curve?.net?.slice(-30)}
          sub={`${pct(d.return)} · ${d.closed ?? 0} trades · won ${d.closed ? Math.round((100 * (d.won || 0)) / d.closed) : 0}% · DD ${pct(-(d.drawdown || 0))}`}
          fact={<>net <b className={cls(d.net)}>{signed(d.net)}</b></>} title={`value ${inr(d.value)} · realised ${signed(d.realised)} · unrealised ${signed(d.unrealised)}${vsNifty ? '' : ''}`} />
        <Card unit="n" label="Active lots" value={`${active.length} of ${st.max_pos}`} num={active.length} sub={`${active.map((p) => `${p.symbol} ${p.lots}`).join(' · ') || 'none'}${waiting.length ? ` · ${waiting.length} waiting` : ''}`}
          fact={<>eligible <Bar pct={d.universe_count ? (100 * d.eligible_count) / d.universe_count : 0} /> {d.eligible_count} / {d.universe_count}</>} title={`lot value ≤ ${lakh(st.max_lot_value)} · ${d.eligible_count} of ${d.universe_count} futures names eligible`} />
        <Card label="Risk at the stops" value={signed(-(d.risk || 0))} num={d.risk} k="down" sub={`${pct((100 * (d.risk || 0)) / d.capital)} of capital · heat cap ${pct(st.heat_pct)}`}
          fact={biggest ? <>biggest {biggest.symbol} <b>{inr(biggest.risk)}</b></> : <span className="muted">no open lot</span>} title={`risk to the stops ${inr(d.risk)} · heat cap ${pct(st.heat_pct)}`} />
        <Card unit="n" label="Next roll" value={roll.day ? `${wday(roll.day)} ${day(roll.day)}` : '—'} num={roll.items?.filter((i) => i.due).length || 0} k={roll.items?.some((i) => i.due) ? 'warn' : ''}
          sub={`T-${st.roll_days} · ${(roll.items || []).length} contract${(roll.items || []).length === 1 ? '' : 's'} · est. cost ${inr((roll.items || []).reduce((a, i) => a + (i.est_cost || 0), 0))}`}
          fact="rule holds: roll · else exit" title="roll if the exit rule holds, else exit · never held into expiry" />
        <Card unit="n" label="Short paper" value={`${sp.open ?? 0} open`} num={sp.open} k={sp.open ? '' : 'muted'} sub={`${sp.signals_month ?? 0} signals · 30 d · ${signed(sp.net)}`}
          fact={<>robust <Bar pct={sp.robust?.of ? (100 * sp.robust.pass) / sp.robust.of : 0} bad={sp.verdict === false} ok={sp.verdict === true} /> {sp.robust?.of ? `${sp.robust.pass} / ${sp.robust.of}` : 'not run'}</>} />
      </Cards>
      <div className="two">
        <div className="rd" style={COL}>
          {d.started && <MorningPanel onReplan={replan} replanning={replanning} onErr={setErr} />}
          <Panel title="Open futures positions" meta={(d.positions || []).length} right={<><Meta>stop check every {d.send?.stop_check_min} min on the futures price · trail 25% under the best close once +100%</Meta>
            {d.started && <button type="button" className={`btn small ${adding ? 'primary' : ''}`} title="track a lot opened by hand · nothing is sent" onClick={() => { setAdding(!adding); setEdit(null); }}>+ Add a lot</button>}</>}>
            {adding && <AddPosition d={d} onDone={done} onErr={setErr} />}
            <FixedTable cols={posCols} rows={posRows} empty={d.started ? 'No open lots.' : 'The book starts at the next evening run: the first plan is decided at the close and fills at the next futures open (paper), or goes out at 9:16 (real).'} />
            {d.warnings?.ban?.length > 0 && <div className="rbanner warn" style={{ margin: '8px 14px' }}>⛔ in the MWPL ban list: {d.warnings.ban.join(', ')} · no new positions; exits still allowed</div>}
            {d.notes?.length > 0 && <div className="rnote">{d.notes.slice(-3).map((n) => `📌 ${n.day}: ${n.text}`).join(' · ')}</div>}
            <div className="rnote">One lot a name · the stop is ATR × {`mult`} in futures points, capped at {st.max_stop_pct}% · stops are checked on the futures quote every {d.send?.stop_check_min} min (needs the Kite login) and on the day's low / high in the evening · rolls at T-{st.roll_days}: EXIT the near month, then the next (never held into expiry) · ✎ corrects lots / entry / stop, “Remove from tracking” drops an entry that never filled, “+ Add a lot” tracks one opened by hand. Nothing here sends an order.</div>
          </Panel>
          <ReplanPanel r={rp} send={d.send} busy={replanning} onRaise={() => {}} onKeep={() => setRp(null)} />
          <Panel title="Short side · paper" meta={`${sp.open ?? 0} open · ${sp.signals_month ?? 0} signals this month`} right={<Meta>real stays locked until dev / val / OOS and robustness pass</Meta>}>
            <FixedTable cols={shortCols} rows={shortRows} empty="No short paper trades yet: E− fires on a close under the 20-day low with lower highs, under the 50- and 200-day, with NIFTY under its 50-day." />
          </Panel>
          <Panel id="fut-tradelog" title="Trade log" meta={(d.trades || []).length}><FixedTable cols={tradeCols} rows={tradeRows} empty="None yet." /></Panel>
        </div>
        <div className="rd" style={COL}>
          <Panel title="Plan · next open" meta={planRows.length} right={d.decided ? <Meta>decided {day(d.decided)} · filter {d.filter?.state || '—'}</Meta> : null}>
            {!planRows.length ? <p className="muted small rempty">{d.started ? 'No futures orders for the next open.' : 'After the first evening run.'}</p> : planRows.map((o) => <Act key={o.action + o.symbol} icon={o.action === 'EXIT' ? '↓' : o.action === 'BUY' ? '↑' : '↓'} cls={o.action === 'BUY' ? 'up' : 'down'}
              text={<><b>{o.symbol}</b> {o.side?.toUpperCase()} · {o.action} · {o.lots} lot ({(o.lot_size || 0).toLocaleString('en-IN')}){o.action !== 'EXIT' && <> · ≈ {lakh(o.lot_value)} · stop {n2(o.stop)} ({o.stop_pct}%) · risk {inr(o.risk)} ({o.risk_pct}%)</>}</>}
              meta={o.action === 'EXIT' ? o.why : `${o.why} · margin needed ${inr(o.margin_est)} · ${[S[o.side]?.paper && 'paper', S[o.side]?.real && 'real'].filter(Boolean).join(' and ') || 'side off'} planned${o.sent ? ` · ${o.sent}` : ''}`} />)}
            <div className="rnote">real sides sent at {ampm(d.send?.send_time)} with the {d.send?.gap_s} s gap · {ampm(d.send?.preview_time)} dry run with skip switches, like the cash book · paper fills at the futures open</div>
          </Panel>
          <Panel title="Roll queue" meta={roll.day ? `${wday(roll.day)} ${day(roll.day)}` : '—'} right={<Meta>T-{st.roll_days} · {ampm(d.send?.roll_time || '09:20')}</Meta>}>
            {!(roll.items || []).length ? <p className="muted small rempty">No open lot to roll.</p> : roll.items.map((it) => <Act key={it.account + it.symbol} icon="↻" cls={it.action === 'roll' ? 'warn' : 'down'}
              text={<><b>{it.symbol}</b> · {it.why} → {it.action === 'roll' ? <>EXIT {it.contract} + {it.side === 'long' ? 'BUY' : 'SELL'} {it.next}</> : <>EXIT {it.contract}</>} · est. cost {inr(it.est_cost)}</>}
              meta={`${it.book} · ${it.side} · expiry ${day(it.expiry)}${it.sessions != null ? ` · ${it.sessions} session${it.sessions === 1 ? '' : 's'}` : ''} · sent as a pair, ${d.send?.gap_s} s apart · never held into expiry`} />)}
            {(d.roll_log || []).length > 0 && <div className="rnote">last rolls: {d.roll_log.slice(0, 3).map((r) => `${day(r.day)} ${r.symbol} ${r.from} → ${r.to} · cost ${inr(r.cost)}`).join(' · ')}</div>}
          </Panel>
          <Panel title="Alerts · futures">
            {ALERTS.map(([i, t, x]) => <Act key={t} icon={i} text={<><b>{t}</b> · {x}</>} />)}
          </Panel>
          <Panel id="fut-orderlog" title="Order log" meta={(d.log || []).length} right={<Meta>every order, fill, roll and correction · never deleted</Meta>}>
            {!d.log?.length ? <p className="muted small rempty">Nothing yet.</p> : d.log.slice(0, 60).map((o, k) => <Act key={k} icon={o.status === 'sent' || o.status === 'filled' ? '✓' : o.status === 'failed' ? '!' : SIDE_ACT[o.action] ? '·' : '✎'} cls={o.status === 'sent' || o.status === 'filled' ? 'up' : o.status === 'failed' ? 'down' : SIDE_ACT[o.action] ? 'warn' : ''}
              text={<><b className={SIDE_ACT[o.action] || 'muted'}>{o.action}</b> <b>{o.symbol}</b> {o.lots ?? ''}{o.price ? <span className="muted small"> ~{rs(o.price)}</span> : null} <Tag c={STATUS[o.status] || ''}>{o.status}</Tag> <span className="muted small">{o.tag}</span></>}
              meta={`${stamp(o.created_at)}${o.reason ? ` · ${o.reason}` : ''}${o.reply ? ` · ${o.reply}` : ''}`} />)}
          </Panel>
          {!phone && <NextUp />}
        </div>
      </div>
      <div className="rnote">Signals on the cash candles; execution, stops, MTM and the trail on the near-month future. Orders go by webhook to the futures deployment (the sender is step 4; until then every real order is logged as failed). The app never places an order in Kite; Kite is used only for quotes. Past results don't guarantee future returns.</div>
    </>}
  </div>;
}
