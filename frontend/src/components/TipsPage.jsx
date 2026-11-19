import React, { useEffect, useMemo, useRef, useState } from 'react';
import * as api from '../services/api';
import { useLiveOr } from '../services/live';
import { clock, ampm } from '../timefmt';
import { inr } from '../fmt';
import { useIsPhone } from '../useMedia';
import { Card, Cards, Panel, FixedTable, Tag, AiTag, Chip, Head, Seg, Switch } from './ui2';
import { DRY_WARN, DRY_NOTE, KMODES, kmodeNote, hintName, modeConfirm, killConfirm, legLine, kiteTexts } from './tipsKite';

// 💡 Research › Tips: a Telegram channel's calls caught, filtered, decoded and scored. NOT linked to any strategy:
// every take is the owner's, each through a confirm, into a separate Tips PAPER book (nothing is sent to the broker).
// ⚡ Tips → Kite orders (tips.kite_mode click): on top of the paper book, a hint's legs go to Kite by the owner's click,
// each through a confirm; "Place all" = the entry now, the stop + T1 the moment it fills (GET /api/tips/kite behind it).
// Dry run (kite_mode dry): the same buttons and legs through a simulator that fills from live quotes; every badge, status
// and Telegram line carries DRY and nothing reaches Kite. "Test connection…" proves the session, the static IP and the
// order permission on the real account (one LIMIT buy at half the price, cancelled 5 s later).
// PR C: an entry with a level rests on Kite (SL-M trigger / LIMIT) or is armed and watched by the app (an index level);
// wait words with no level arm it until the channel says active ("Send now" skips that). Auto (kite_mode auto) arms
// placeable hints by itself and follows the channel; every Kite event is checked by code (red / amber lines) and read
// by the AI ([AI] lines); the Kite day check (3:35 PM) sits by the orders table.
// Data: the stream topic `tips` (5 s while a hint is open or waiting, else 60 s), GET /api/tips behind it.
// Every field the local model filled (decoded[field] === 'ai', a post classified_by 'ai') carries the AI tag.

const arr = (x) => (Array.isArray(x) ? x : []);
const isNum = (v) => v != null && v !== '' && !Number.isNaN(Number(v));
export const px = (v) => (isNum(v) ? Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—');
export const money = (v) => (!isNum(v) ? '—' : Number(v) > 0 ? `+${inr(Number(v))}` : inr(Number(v)));
const tone = (v) => (!isNum(v) || Number(v) === 0 ? '' : Number(v) > 0 ? 'up' : 'down');
const plural = (n, w) => `${n} ${w}${Number(n) === 1 ? '' : 's'}`;
const IST = 'Asia/Kolkata';
const toD = (iso) => new Date(/[zZ]$|[+-]\d\d:?\d\d$/.test(String(iso)) ? iso : `${String(iso).replace(' ', 'T')}+05:30`);
const dayKey = (dt) => dt.toLocaleDateString('en-CA', { timeZone: IST });
// "9:43 AM" today, "Mon 2:00 PM" another day (IST, 12-hour)
export function when(iso) {
  if (!iso) return '';
  const dt = toD(iso);
  if (Number.isNaN(dt.getTime())) return String(iso);
  if (dayKey(dt) === dayKey(new Date())) return clock(iso);
  return `${dt.toLocaleDateString('en-IN', { timeZone: IST, weekday: 'short' })} ${clock(iso)}`;
}
// "2026-11-19" -> "Wed 19 Nov"
const dayLabel = (d) => {
  if (!d) return '—';
  const dt = new Date(`${String(d).slice(0, 10)}T12:00:00+05:30`);
  if (Number.isNaN(dt.getTime())) return String(d);
  return ['weekday', 'day', 'month'].map((k) => dt.toLocaleDateString('en-IN', { timeZone: IST, [k]: k === 'day' ? 'numeric' : 'short' })).join(' ');
};
const hm12 = (v) => (v && /^\d{1,2}:\d{2}/.test(String(v)) ? ampm(String(v)) : v || '—');

export const OPEN = ['new', 'waiting', 'active', 'paused'];
export { hintName };
const isAi = (h, ...fields) => fields.some((f) => h?.decoded?.[f] === 'ai');

export function entryText(e, ltp, status, noKind) {
  if (!e || typeof e !== 'object') return '—';
  const lo = px(e.lo), hi = px(e.hi);
  const base0 = e.mode === 'zone' ? `zone ${lo}–${hi}` : e.mode === 'above' ? `above ${lo}` : e.mode === 'below' ? `below ${lo}`
    : e.mode === 'market' ? 'at market' : e.mode === 'at' ? `at ${lo}` : isNum(e.lo) ? `at ${lo}` : '—';
  // PR C: the level's kind: the option's own price, or the index ("SENSEX above 82,400 (index)")
  const lv = e.mode && e.mode !== 'market' ? e.level_kind : null;
  const base = lv === 'spot' ? `${e.index || 'spot'} ${base0}${noKind ? '' : ' (index)'}` : lv === 'premium' && !noKind ? `${base0} (premium)` : base0;
  return (status === 'new' || status === 'waiting') && isNum(ltp) && lv !== 'spot' ? `${base} · now ${px(ltp)}` : base;
}
export function stopText(s) {
  if (!s || typeof s !== 'object' || s.kind === 'none' || !isNum(s.value)) return 'no usable stop';
  return s.kind === 'spot' ? `spot ${px(s.value)} (15-min close)` : px(s.value);
}
const zeroTarget = (h) => arr(h?.targets).length === 1 && Number(h.targets[0]) === 0;

function statusTag(h) {
  const s = h?.status;
  if (s === 'new' || s === 'waiting') return <Tag c="w">{s === 'new' ? 'new · waiting' : 'waiting'}</Tag>;
  if (s === 'active') return <Tag c="ok">active</Tag>;
  if (s === 'paused') return <Tag c="w">paused</Tag>;
  if (s === 'skipped') return <Tag>skipped · scored</Tag>;
  if (s === 'expired') return <Tag>expired{h.ended ? ` · ${h.ended.replace('_', ' ')}` : ''}</Tag>;
  if (s === 'closed') return <Tag c={tone(h.pnl) === 'down' ? 'bad' : tone(h.pnl) === 'up' ? 'ok' : ''}>closed{h.ended ? ` · ${String(h.ended).replace('_', ' ')}` : ''}</Tag>;
  return s ? <Tag>{s}</Tag> : null;
}
const SRC = { channel: '📣', owner: '👤', code: '⚙', ai: '🤖', kite: '⚡' };

// The decoded grid: six fixed cells (3 columns on a phone); each value ellipsizes, never wraps.
export function Decoded({ h }) {
  const sh = h?.size_hint;
  const lotSize = h?.plan?.lot_size ?? h?.lot_size;
  const size = h?.taken && isNum(h.lots) ? `${plural(px(h.lots), 'lot')}${isNum(lotSize) ? ` · ${px(Number(h.lots) * Number(lotSize))}` : ''}`
    : sh && typeof sh === 'object' ? (isNum(sh.lots) ? plural(px(sh.lots), 'lot') : isNum(sh.qty) ? `qty ${px(sh.qty)}` : String(sh.text || 'stated'))
      : OPEN.includes(h?.status) ? 'not stated · you choose' : 'not stated';
  const exp = h?.expiry ? `${h?.expiry_label || dayLabel(h.expiry)}${h?.plan?.expiry_kind ? ` · ${h.plan.expiry_kind}` : ''}` : h?.instrument === 'cash' ? 'cash' : '—';
  const last = zeroTarget(h) ? ['target', '0 · hold to expiry', ['targets']] : ['time stop', h?.plan?.time_stop === null ? 'none' : hm12(h?.plan?.time_stop || '15:20'), ['time_stop']];
  const cells = [
    ['side · instrument', [h?.side, h?.instrument].filter(Boolean).join(' · ') || '—', ['side', 'instrument']],
    [h?.entry?.mode && h.entry.mode !== 'market' && h.entry.level_kind ? `entry · ${h.entry.level_kind === 'spot' ? 'index' : 'premium'}` : 'entry',
      entryText(h?.entry, h?.ltp, h?.status, true), ['entry']],
    ['stop', stopText(h?.stop), ['stop']],
    ['expiry', exp, ['expiry']],
    ['size', size, ['size', 'size_hint']],
    last,
  ];
  return <div className="tp-dec">{cells.map(([k, v, f]) => <div key={k} title={typeof v === 'string' ? v : undefined}>
    <div className="kk">{k}</div><b className={k === 'stop' && v === 'no usable stop' ? 'warn' : ''}><span className="v">{v}</span>{isAi(h, ...f) && <AiTag title="filled by the local model from the post's own text" />}</b></div>)}</div>;
}

export function TargetBar({ h }) {
  const t = arr(h?.targets);
  if (!t.length) return <div className="tp-tgt"><span className="muted">targets</span> <span className="dim">none stated</span></div>;
  const hit = Number(h?.targets_hit ?? h?.plan?.targets_hit ?? 0) || 0;
  return <div className="tp-tgt"><span className="muted">targets</span>{t.map((x, i) => <React.Fragment key={i}>
    <i className={i < hit ? 'ok' : ''} /><span className={`tv ${i < hit ? 'up' : 'dim'}`}>{px(x)}{h?.open_ended && i === t.length - 1 ? '++' : ''}</span></React.Fragment>)}
    {isAi(h, 'targets') && <AiTag title="targets filled by the local model from the post's own text" />}</div>;
}

function Timeline({ events }) {
  const ev = arr(events);
  if (!ev.length) return null;
  return <div className="tp-tl">{ev.map((e, i) => <div key={i}><span className="tt">{when(e?.t)}</span>
    {e?.src && e.src !== 'ai' && SRC[e.src] && <span className="src" title={e.src}>{SRC[e.src]}</span>}
    <span className="tx">{e?.text || ''}</span>{e?.src === 'ai' && <AiTag of={e} />}</div>)}</div>;
}

// ⚡ Tips → Kite orders ----------------------------------------------------------------------------------------------
const LEG_IC = { entry: '🟢', stop: '🛑', t1: '🎯', t2: '🎯', exit: '⏏' };
const LIVE_ST = ['placed', 'open'];
const legTag = (lg) => {
  const s = lg?.status;
  const c = s === 'complete' ? 'ok' : s === 'open' || s === 'placed' ? 'a' : s === 'armed' ? 'w' : s === 'rejected' || s === 'failed' ? 'bad' : lg?.after_t1 && s === 'none' ? 'w' : '';
  const text = s === 'none' && lg?.after_t1 ? 'placed after T1' : s === 'placed' && lg?.leg !== 'entry' ? 'placed' : lg?.status_text || 'not placed';
  return <Tag c={c} title={lg?.note || undefined}>{lg?.dry && s && s !== 'none' ? 'DRY · ' : ''}{text}</Tag>;
};
export const kiteCaps = (k) => {
  const c = k?.caps || {};
  return `today ${px(c.entries ?? 0)} of ${px(c.max_entries ?? 0)} entries · lots cap ${px(c.max_lots ?? 0)} · loss cap ${isNum(c.loss_cap) ? inr(Number(c.loss_cap)) : '—'} · used ${isNum(c.used_loss) ? (Number(c.used_loss) < 0 ? `−${inr(Math.abs(Number(c.used_loss)))}` : `−${inr(0)}`) : '—'}`;
};

// The bar under the Head: the mode (Off / By my click / Auto), the kill switch, the caps, "Orders today".
function KiteBar({ k, onDone, toOrders, onTest, testing }) {
  const [msg, setMsg] = useState('');
  const mode = k?.mode || 'off';
  const kill = !!k?.kill;
  const dry = mode === 'dry';
  const setMode = (m) => {
    if (m === mode) return;
    if (!window.confirm(modeConfirm(m))) return;
    api.putTipsSettings({ kite_mode: m }).then(() => { setMsg(''); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  const setKill = (on) => {
    if (!window.confirm(killConfirm(on))) return;
    api.tipsKiteKill(on).then((r) => { setMsg(on ? `kill switch on · ${px(r?.cancelled ?? 0)} cancelled · ${px(r?.exits ?? 0)} exits` : ''); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  const clearDry = () => {
    if (!window.confirm(`Clear the dry run?\n\nToday's dry orders are voided (kept in the record, never deleted), the simulator's book is emptied and the dry hints' legs are reset, so you can rehearse again. A hint traded dry today still is not traded real today.\n\n${DRY_WARN}`)) return;
    api.tipsKiteDryClear().then((r) => { setMsg(`dry run cleared · ${px(r?.voided ?? 0)} orders voided`); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  const rehearse = () => {
    if (!window.confirm('Add a rehearsal hint?\n\nA made-up BUY NIFTY CE call near the money at its last price (SL 20 % under, targets 20 % and 40 % over, 1 lot) appears as a hint in channel "rehearsal". Press Place all on it and watch the DRY legs. It is never pushed to Telegram as a call and never counts in the channel\'s record. Nothing reaches Kite.')) return;
    api.tipsKiteRehearse().then((r) => { setMsg(`rehearsal hint #${r?.hint_id} added`); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  const test = () => {
    if (!window.confirm('Test the Kite connection?\n\nOne LIMIT buy of a far out-of-the-money NIFTY put (1 lot) at half its last price goes to your REAL Kite account: it cannot fill at that price. It is cancelled 5 s later and read back from the order book. This proves the session, the static IP and the order permission on the app key.\n\nAt most one test per 5 minutes. Outside 9:15 AM – 3:30 PM Kite refuses it (market closed).')) return;
    onTest && onTest();
  };
  const note = msg ? <span className={msg.startsWith('⚠') ? 'down' : 'up'}>{msg}</span>
    : dry ? <span className="warn">{DRY_NOTE}{k?.market_open === false ? ' · market closed: a MARKET order fills at the last price' : ''}</span>
      : mode === 'auto' ? <span className="up">{k?.auto?.line || kmodeNote(mode)}</span> : kmodeNote(mode);
  return <div className={`tp-kbar ${mode === 'off' ? 'off' : ''} ${dry ? 'dry' : ''} ${kill ? 'killed' : ''}`}>
    <b className="ttl">⚡ Kite orders</b>
    <Seg small opts={KMODES.map(([v, l, t]) => [v, l, t])} v={mode} set={setMode} />
    {mode !== 'off' && <Switch red on={kill} onChange={setKill} label="kill switch" title="cancels every working Tips order on Kite and exits at market" />}
    <span className="sp" />
    {mode === 'off' ? <span className="cap">paper book only · nothing reaches Kite</span>
      : <><span className="cap" title={kiteCaps(k)}>{dry ? 'dry · ' : ''}{kiteCaps(k)}{k?.window ? ` · entries ${k.window}` : ''}</span>
        <span className={`cap note ${dry ? 'dry' : ''}`} title={dry ? DRY_NOTE : undefined}>{note}</span></>}
    {mode !== 'off' && <button type="button" className="btn ghost small" disabled={testing} title="one LIMIT buy at half the price on your real Kite account, cancelled 5 s later" onClick={test}>{testing ? 'Testing… ~6 s' : 'Test connection…'}</button>}
    {dry && <button type="button" className="btn small" onClick={rehearse} title="a made-up NIFTY CE hint to press Place all on · nothing reaches Kite">+ Rehearsal hint…</button>}
    {dry && <button type="button" className="btn ghost small" onClick={clearDry} title="void today's dry orders and rehearse again">Clear dry run…</button>}
    {(mode !== 'off' || Number(k?.open) > 0 || arr(k?.orders).length > 0) && <button type="button" className="btn ghost small" onClick={toOrders}>Orders today</button>}
  </div>;
}

// The connection test's result (POST /api/tips/kite/test; the last one comes with GET /api/tips/kite): passed / failed /
// market closed, each step ✅ / ⛔ / ·, Kite's own words under a failure.
function KiteTest({ t, err, onClose }) {
  if (!t && !err) return null;
  const tag = !t ? <Tag c="bad">not run</Tag> : t.passed === true ? <Tag c="ok">passed · {t.at_label}</Tag> : t.passed === false ? <Tag c="bad">failed · {t.at_label}</Tag> : <Tag c="w">market closed · {t.at_label}</Tag>;
  return <Panel title={<>Kite connection test {tag}</>} className="tp-ktest"
    right={<><span className="ell">{err ? <span className="down">{err}</span> : 'one LIMIT buy at half the price · cancelled 5 s later · nothing was bought'}</span>
      <button type="button" className="btn ghost small" onClick={onClose} aria-label="Close">✕</button></>}>
    {t && <div className="rbody">
      <div className="steps">{arr(t.steps).map((x, i) => <div key={i} className={`st ${x.ok === false ? 'no' : x.ok == null ? 'mid' : ''}`}>
        <span className="gi">{x.ok === true ? '✅' : x.ok === false ? '⛔' : '·'}</span>
        <span>{x.text}{x.detail && x.ok !== true ? <span className="kw"> · Kite: “{x.detail}”</span> : ''}</span></div>)}</div>
      {t.summary && <div className={`sum ${t.passed === false ? 'down' : t.passed ? 'up' : 'warn'}`}>{t.summary}</div>}
    </div>}
  </Panel>;
}

// The legs of one hint on Kite (entry / stop / target 1 / target 2), each with its status and its buttons; the row of
// whole-hint actions under them. Every button confirms first; the numbers come from the server (GET /api/tips).
function KiteLegs({ h, kinfo, onDone }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const k = h?.kite || {};
  const T = kiteTexts(h, kinfo);                        // the confirms' words (shared with Home · Tips · Kite)
  const { W, to, L, id, head } = T;
  const acts = arr(k.actions);
  const legs = arr(k.legs);
  const caps = kinfo?.caps || {};
  const capsAfter = `${px((caps.entries ?? 0) + 1)} of ${px(caps.max_entries ?? 0)} entries · ${px(k.lots)} of ${px(caps.max_lots ?? 0)} lots · loss cap ${isNum(caps.loss_cap) ? inr(Number(caps.loss_cap)) : '—'}${isNum(L.stop?.max_loss) ? ` · this trade's max loss ${money(L.stop.max_loss)}` : ''}`;
  const stopTxt = L.stop ? (L.stop.placeable === false ? 'spot stop: watched by the app, exit by hand' : `${legLine(L.stop)}${isNum(L.stop.max_loss) ? ` (${money(L.stop.max_loss)})` : ''}`) : '—';
  const run = (action, body, text, okText) => {
    if (!window.confirm(text)) return;
    setBusy(true); setMsg('');
    api.tipsKite(id, action, body).then(() => { setMsg(okText); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`)).finally(() => setBusy(false));
  };
  // PR C: what the entry does, in words from the server ("a BUY stop order rests on Kite at 370", "the app watches SENSEX
  // and buys at market when it trades 82,400 or above", "nothing is sent until the channel says active")
  const ew = k.needs_activation ? `armed: nothing is sent until the channel says active · then ${k.entry_what || 'at market'}` : (k.entry_what || `${L.entry?.side} at market now`);
  const tsLine = k.hold_to_expiry ? 'held to expiry (target 0): no time-stop exit' : k.time_stop ? `a Kite position still open at ${hm12(k.time_stop)} is exited at market (the time stop)` : '';
  const unfilled = k.entry_what && !/at market now/.test(k.entry_what) ? `An entry that never fills is cancelled at the end of the entry window${k.time_stop ? ` / at ${hm12(k.time_stop)}` : ''}, or when the channel says avoid.` : '';
  const all = () => run('all', {}, `${head('Place all on Kite')}premium ≈   ${isNum(L.entry?.premium) ? inr(Number(L.entry.premium)) : '—'}\n1 · entry   ${L.entry?.side} ${px(L.entry?.qty)} ${L.entry?.type_text || 'MARKET'} · ${ew}\n2 · the moment it fills   ${stopTxt}${L.t1 ? ` · T1 ${legLine(L.t1)}` : ''}\n${L.t2 ? `3 · after T1 fills   T2 ${legLine(L.t2)} · SL moved to cost\n` : ''}caps after this   ${capsAfter}\n\nSteps 2${L.t2 ? ' and 3' : ''} are the app's, as the consequence of this click; the leftovers are cancelled when the position closes.${k.beyond_t2 ? ` Targets past the second stay in the paper record only.` : ''}${unfilled ? ` ${unfilled}` : ''}${tsLine ? `\n${tsLine}.` : ''}\n\n${W}`, `entry ${k.needs_activation || L.entry?.arm_kind === 'watch' ? 'armed' : 'sent'} · ${to} · SL + T1 on the fill`);
  const entry = () => run('entry', {}, `${head('Place the entry on Kite')}premium ≈   ${isNum(L.entry?.premium) ? inr(Number(L.entry.premium)) : '—'}\norder   ${L.entry?.side} ${px(L.entry?.qty)} ${L.entry?.type_text || 'MARKET'} · ${ew}${tsLine ? `\n${tsLine}` : ''}\ncaps after this   ${capsAfter}\n\nOnly the entry: the stop and the targets are yours to place once it fills.\n\n${W}`, `entry sent to ${to}`);
  const rest = () => run('rest', {}, `${head('Place the rest on Kite')}now   ${L.stop?.status && LIVE_ST.includes(L.stop.status) ? '' : `SL ${stopTxt}`}${L.t1 && !['placed', 'open', 'complete'].includes(L.t1.status) ? ` · T1 ${legLine(L.t1)}` : ''}${L.t1?.status === 'complete' && L.t2 ? ` · T2 ${legLine(L.t2)}` : ''}\n${L.t2 && L.t1?.status !== 'complete' ? `after T1 fills   T2 ${legLine(L.t2)} · SL moved to cost\n` : ''}\nWhat follows a fill is the app's, as the consequence of this click.\n\n${W}`, `the rest sent to ${to}`);
  const stop = () => run('stop', {}, `${head('Place the stop on Kite')}order   ${L.stop?.side} ${stopTxt}\n\n${W}`, `stop sent to ${to}`);
  const tgts = (which) => run('targets', {}, `${head(`Place ${which === 't2' ? 'target 2' : 'target 1'} on Kite`)}order   ${L[which]?.side} ${legLine(L[which])}${which === 't1' && isNum(L.t1?.if_filled) ? ` (${money(L.t1.if_filled)} if filled)` : ''}\n\n${W}`, `${which === 't2' ? 'T2' : 'T1'} sent to ${to}`);
  const exit = () => run('exit', {}, T.exit, `exit sent to ${to}`);
  const cancel = (leg) => run('cancel', { leg }, T.cancel(leg), `${L[leg]?.name || leg} cancelled`);
  const sendNow = () => run('send_now', {}, T.sendNow, `entry sent to ${to}`);
  const toCost = () => run('modify_stop', { to: 'cost' }, T.moveStop('cost'), 'stop moved to cost');
  const resync = () => { if (window.confirm('Re-sync with Kite?\n\nReads the Kite order book once (only the app\'s own Tips orders) and applies what changed.')) { setBusy(true); api.tipsKiteResync().then(() => { setMsg('re-synced'); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`)).finally(() => setBusy(false)); } };
  const live = (leg) => LIVE_ST.includes(L[leg]?.status);
  const btn = (label, fn, extra = {}) => <button type="button" className={`btn ghost small ${extra.cls || ''}`} disabled={busy || extra.off} title={extra.title} onClick={fn}>{label}</button>;
  const legBtns = (lg) => {
    const out = [];
    if (lg.leg === 'entry') {
      if (acts.includes('entry')) out.push(btn('Place entry…', entry, { key: 'e' }));
      if (acts.includes('send_now')) out.push(btn('Send now…', sendNow, { title: "skip the channel's activation and send the entry now" }));
      if (acts.includes('cancel:entry')) out.push(btn('Cancel', () => cancel('entry')));
      if (lg.status === 'complete') out.push(<span key="d" className="dim small">done · by {lg.by === 'click' ? 'your click' : lg.by === 'dry' ? 'your click (dry run)' : lg.by}</span>);
    } else if (lg.leg === 'stop') {
      if (live('stop') && isNum(k.entry_px) && Number(lg.trigger) !== Number(k.entry_px) && acts.includes('modify_stop')) out.push(btn(`Modify → cost ${px(k.entry_px)}`, toCost));
      if (acts.includes('cancel:stop')) out.push(btn('Cancel', () => cancel('stop')));
      if (!live('stop') && lg.status !== 'complete' && lg.placeable !== false) out.push(btn('Place SL…', stop, { off: !acts.includes('stop'), title: acts.includes('stop') ? undefined : 'after the entry fills on Kite' }));
    } else if (lg.leg === 't1') {
      if (acts.includes('cancel:t1')) out.push(btn('Cancel', () => cancel('t1')));
      if (!live('t1') && lg.status !== 'complete') out.push(btn('Place T1…', () => tgts('t1'), { off: !acts.includes('targets'), title: acts.includes('targets') ? undefined : 'after the entry fills on Kite' }));
    } else if (lg.leg === 't2') {
      if (acts.includes('cancel:t2')) out.push(btn('Cancel', () => cancel('t2')));
      if (!live('t2') && lg.status !== 'complete') out.push(btn('Place now…', () => tgts('t2'), { off: !(acts.includes('targets') && L.t1?.status === 'complete'), title: 'after target 1 fills (a sell for more than you hold is refused)' }));
    }
    return out.map((x, i) => <React.Fragment key={i}>{x}</React.Fragment>);
  };
  const note = msg ? <span className={msg.startsWith('⚠') ? 'down' : 'up'}>{msg}</span>
    : k.state === 'closed' ? `closed on Kite${k.reason ? ` · ${k.reason}` : ''} · ${money(k.realised)}`
      : acts.includes('all') ? (L.entry?.arm_kind && L.entry.arm_kind !== 'market' ? `Place all = ${L.entry.arm_kind === 'watch' ? 'the app watches the index' : 'the entry rests on Kite'}${k.needs_activation ? " (after the channel's activation)" : ''}, SL and targets the moment it fills` : 'Place all = entry now, SL and targets the moment it fills · or each leg on its own')
        : k.state === 'armed_watch' ? `armed · the app watches ${k.arm?.index || 'the index'} · buys at market on the cross${k.auto_rest ? ' · then SL + T1' : ''}`
        : k.state === 'armed_wait' ? `armed · waiting for the channel's activation${k.auto_rest ? ' · then SL + T1' : ''}`
        : k.state === 'armed' ? `entry ${k.arm?.kind === 'slm' || k.arm?.kind === 'limit' ? 'resting on Kite' : 'on Kite'} · waiting for the fill${k.auto_rest ? ' · then SL + T1 by the app' : ''}`
          : k.state === 'entered' ? `${px(k.qty_open)} open ${k.dry ? 'in the dry run' : 'on Kite'}${k.beyond_t2 ? ' · targets past T2: paper record only' : ''}`
            : k.sim ? 'dry run · every button opens a confirm · nothing reaches Kite' : 'every button opens a confirm · the paper book stays as it is';
  return <div className="tp-kite">
    <div className="tp-legs">{legs.map((lg) => <div key={lg.leg} className={`tp-leg ${lg.status}`}>
      <div className="t"><span className="ic">{LEG_IC[lg.leg]}</span><b>{lg.name}</b><span className="ln">· {lg.side} {px(lg.qty)} · {lg.type_text}</span>{legTag(lg)}</div>
      <div className="d" title={lg.note || undefined}>{[lg.at, lg.order_id ? `order ${lg.order_id}` : '', lg.detail].filter(Boolean).join(' · ')}</div>
      {lg.now_text && <div className="d nw" title={lg.now_text}>{lg.now_text}</div>}
      <div className="a">{legBtns(lg)}</div></div>)}</div>
    <div className="acts tp-kacts">
      {acts.includes('all') && <button type="button" className="btn small" disabled={busy} onClick={all}>⚡ Place all on Kite · entry + SL + targets…</button>}
      {acts.includes('rest') && <button type="button" className="btn small" disabled={busy} onClick={rest}>⚡ Place rest…</button>}
      {acts.includes('exit') && <button type="button" className="btn ghost small tp-danger" disabled={busy} onClick={exit}>Exit on Kite now…</button>}
      <button type="button" className="btn ghost small" disabled={busy} onClick={resync}>Re-sync with Kite</button>
      <span className="note">{note}</span>
    </div>
    <KiteCheck k={k} />
  </div>;
}

// PR C: the order check under the legs: a red / amber line per item (code), then the AI read ([AI], 3 lines).
function KiteCheck({ k }) {
  const items = arr(k?.check?.items);
  const ai = k?.ai || null;
  const lines = arr(ai?.lines).filter((x) => typeof x === 'string' && /[A-Za-z]{3}/.test(x));
  if (!items.length && !lines.length && ai?.state !== 'pending' && !(k?.check && k.state && k.state !== 'none')) return null;
  return <div className="tp-kcheck">
    {k?.check && !items.length && <div className="ck ok"><span className="gi">✓</span><span>order check · all clear{k.check.at ? ` · ${when(k.check.at)}` : ''}</span></div>}
    {items.map((x, i) => <div key={i} className={`ck ${x.level === 'red' ? 'red' : 'amber'}`}><span className="gi">{x.level === 'red' ? '⛔' : '⚠'}</span>
      <span>{x.text}{x.hand ? <b> · {x.hand}</b> : ''}</span></div>)}
    {lines.map((x, i) => <div key={`a${i}`} className="ck ai"><AiTag title="the AI read of these orders · every figure checked against the post, the plan and the order rows" /><span>{x}</span></div>)}
    {ai?.state === 'pending' && <div className="ck ai dim"><AiTag title="the AI read waits for the local model" /><span>AI read pending</span></div>}
    {ai?.state === 'empty' && <div className="ck ai dim"><AiTag title="the model answered with nothing usable; the code check above still stands" /><span>AI read: nothing usable from the model</span></div>}
  </div>;
}

// 📷 the channel's screenshot read on the server (RapidOCR, rules only): agrees (quiet), a strike mismatch (red, two
// buttons: the owner picks), resolved, or "screenshot not read: <why>" (dim). Nothing when the post had no image.
function shotTag(s) {
  if (!s || s.state === 'error' || !s.label) return null;
  const c = s.state === 'agrees' ? 'ok' : s.state === 'mismatch' ? 'bad' : '';
  const short = s.state === 'agrees' ? '📷 ✓' : s.state === 'mismatch' ? '📷 ✗ strike' : '📷';
  return <Tag c={c} title={s.state === 'mismatch' ? s.text : s.label}><span className="tp-shtag">{short}</span></Tag>;
}
function ShotLine({ h, busy, onUse, onKeep }) {
  const s = h?.shot;
  if (!s) return null;
  if (s.state === 'error') return <div className="tp-shot dim">{s.label}</div>;
  const at = s.time ? ` at ${s.time}` : '';
  if (s.state === 'mismatch') {
    return <div className="tp-shot bad"><span className="tx"><span className="gi">⛔</span>{s.text}</span>
      <span className="bt"><button type="button" className="btn small" disabled={busy} onClick={onUse}>{s.use_label}</button>
        <button type="button" className="btn ghost small" disabled={busy} onClick={onKeep}>{s.keep_label}</button></span></div>;
  }
  const shown = isNum(s.price) ? ` · channel showed ${Number(s.price).toFixed(2)}${at}` : '';
  const live = isNum(h?.ltp) ? ` · live ${px(h.ltp)}` : '';
  if (s.state === 'agrees') {
    return <div className="tp-shot ok"><Tag c="ok" title={s.quiet}>{s.label}</Tag><span className="tx">{s.quiet || '✓ screenshot agrees'}{shown}{live}</span></div>;
  }
  return <div className="tp-shot dim"><Tag title={s.label}>{s.label}</Tag><span className="tx">{shown.replace(/^ · /, '')}</span></div>;
}

// PR C: the Kite day check (3:35 PM on trading days, or "Check now"): today's orders against the hints.
function KiteDayCheck({ kd, onDone }) {
  const [msg, setMsg] = useState('');
  const d = kd?.day_check;
  const today = d?.day && d.day === new Date().toLocaleDateString('en-CA', { timeZone: IST });
  const run = () => { setMsg('checking…'); api.tipsKiteDayCheck().then(() => { setMsg(''); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`)); };
  const items = today ? arr(d.items) : [];
  return <Panel title={<>Kite day check {today ? (d.red ? <Tag c="bad">{px(d.red)} red</Tag> : <Tag c="ok">all clear</Tag>) : <Tag>3:35 PM</Tag>}</>} className="tp-kday"
    right={<><span className="ell">{msg ? <span className={msg.startsWith('⚠') ? 'down' : ''}>{msg}</span> : today ? `checked ${d.at_label}` : 'runs at 3:35 PM on trading days'}</span>
      <button type="button" className="btn ghost small" onClick={run}>Check now</button></>}>
    <div className="rbody">
      {today ? <div className="sum">{d.summary}</div> : <p className="muted small" style={{ margin: 0 }}>Not run yet today: every Tips order of today against the hints (a fill with no hint, a fill with no stop, an open quantity after the time stop, a resting order still open, the caps).</p>}
      {today && d.caps_text && <div className="muted small">{d.caps_text}</div>}
      {items.map((x, i) => <div key={i} className={`ck ${x.level === 'red' ? 'red' : 'amber'}`}><span className="gi">{x.level === 'red' ? '⛔' : '⚠'}</span><span>{x.text}{x.hand ? <b> · {x.hand}</b> : ''}</span></div>)}
    </div></Panel>;
}

// The guards every Kite entry passes (GET /api/tips/kite), ✅ / ⛔.
function KiteGuards({ kd }) {
  const gs = arr(kd?.guards);
  return <Panel title="Guards" right={<span>Settings › tips › kite</span>} className="tp-guards">
    <div className="rbody">
      {!gs.length && <p className="muted small" style={{ margin: 0 }}>Loading…</p>}
      {gs.map((g) => <div key={g.key} className={`g ${g.ok ? '' : 'no'}`}><span className="gi">{g.ok ? '✅' : '⛔'}</span><span>{g.text}</span></div>)}
      <div className="g dim"><span className="gi">·</span><span>each hint also needs a complete plan (stop + target), a contract and a lot size · one Kite position per hint · never averaged · never re-entered after a stop</span></div>
      <div className="g dim"><span className="gi">·</span><span>the kill switch cancels every working order and exits at market · targets past the second stay in the paper record</span></div>
      <div className="g info"><span className="gi">ℹ</span><span>server public IP: <b className="ip">{kd?.public_ip || 'unknown'}</b> · whitelist this one in Kite Connect</span></div>
    </div></Panel>;
}

// Today's Kite orders of the Tips (ours only). Phones keep Time / Hint / Leg / Side / Qty / Status.
function KiteOrders({ kd, onDone }) {
  const [msg, setMsg] = useState('');
  const rows = arr(kd?.orders).map((o) => ({ key: `k${o.id}`, title: o.note || undefined, cells: [o.label, o.time, o.leg === 'test' ? 'test' : `#${o.hint_id}`,
    <span className={o.side === 'BUY' ? 'up' : 'down'}>{o.side}</span>, px(o.qty), o.type_text, px(o.price),
    <Tag c={o.status === 'complete' ? 'ok' : o.status === 'rejected' || o.status === 'failed' ? 'bad' : o.status === 'cancelled' ? '' : 'a'} title={o.note || undefined}>{o.dry ? 'DRY · ' : ''}{o.status_text}</Tag>,
    o.order_id || '—', o.by || '—'] }));
  const resync = () => { if (window.confirm('Re-sync with Kite?\n\nReads the Kite order book once (only the app\'s own Tips orders) and applies what changed.')) api.tipsKiteResync().then(() => { setMsg(''); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`)); };
  return <Panel id="tp-kite-orders" title="Kite orders today" meta={String(rows.length)} className="tp-korders"
    right={<>{msg ? <span className="down">{msg}</span> : <span>net <b className={tone(kd?.net)}>{money(kd?.net ?? 0)}</b> realised{kd?.last_sync_label ? ` · synced ${kd.last_sync_label}` : ''}</span>}
      <button type="button" className="btn ghost small" onClick={resync}>Re-sync</button></>}>
    <FixedTable cols={[{ h: 'Leg', ell: true }, { h: 'Time', w: 88 }, { h: 'Hint', w: 54 }, { h: 'Side', w: 50 }, { h: 'Qty', w: 54, n: true },
      { h: 'Type', w: 100, lo: true }, { h: 'Price', w: 80, n: true, lo: true }, { h: 'Status', w: 128, ell: true }, { h: 'Order id', w: 136, lo: true }, { h: 'By', w: 52, lo: true }]}
      rows={rows} empty="No Kite orders today. Every one starts from a card's button, through a confirm; only the app's own Tips orders are shown." />
  </Panel>;
}

// One hint: the raw post (kept as written), the decoded grid, the target bar, the timeline, the actions (each confirms).
function HintCard({ h, defLots, onDone, kinfo }) {
  const [lots, setLots] = useState('');
  const [trail, setTrail] = useState(null);           // null = closed, else the field's text
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const id = h?.id;
  const name = hintName(h);
  const open = OPEN.includes(h?.status);
  const acts = Array.isArray(h?.actions) ? h.actions : null;      // the server's allowed actions, when it sends them
  const can = (a) => (acts ? acts.includes(a) : a === 'exit' || a === 'trail' ? !!h?.taken && open : a === 'resume' ? h?.status === 'paused' : a === 'pause' ? open && !h?.taken && h?.status !== 'paused' : open && !h?.taken);
  const holding = can('exit') || can('trail');
  const offer = can('take') || can('take_half') || can('skip') || can('pause') || can('resume');
  const stated = h?.size_hint && isNum(h.size_hint.lots) ? Number(h.size_hint.lots) : null;
  const n = isNum(lots) && Number(lots) > 0 ? Number(lots) : stated ?? (isNum(defLots) ? Number(defLots) : 1);
  const tgts = arr(h?.targets).map(px).join(' / ') || 'none';
  const planLine = `entry ${entryText(h?.entry)} · stop ${stopText(h?.stop)} · targets ${tgts}`;
  const run = (p, okText) => {
    setBusy(true); setMsg('');
    p.then(() => { setMsg(okText); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`)).finally(() => setBusy(false));
  };
  const take = (half) => {
    const what = half ? `half of ${plural(n, 'lot')}` : plural(n, 'lot');
    if (!window.confirm(`Take #${id} · ${name} · ${what}?\n\n${planLine}\n${h?.entry?.mode && h.entry.mode !== 'market' ? 'The paper book waits for the entry rule, then follows the plan by code.' : 'Filled at the live quote, then the plan is followed by code.'}\n\nTips paper book only: nothing is sent to the broker.`)) return;
    run(api.tipsTake(id, half ? { lots: n, half: true } : { lots: n }), `taken · ${what}`);
  };
  const skip = () => {
    if (!window.confirm(`Skip #${id} · ${name}?\n\nIt is still scored for the channel's record. Nothing is sent to the broker.`)) return;
    run(api.tipsSkip(id), 'skipped');
  };
  const exit = () => {
    if (!window.confirm(`Exit #${id} · ${name} now?\n\nThe Tips paper position closes at the live quote${isNum(h?.ltp) ? ` (now ${px(h.ltp)})` : ''}. Nothing is sent to the broker.`)) return;
    run(api.tipsExit(id), 'exit sent to the paper book');
  };
  const doTrail = () => {
    const raw = String(trail || '').trim();
    const to = /^cost$/i.test(raw) ? 'cost' : isNum(raw) ? Number(raw) : null;
    if (to == null) { setMsg('⚠ a price, or "cost"'); return; }
    if (!window.confirm(`Trail #${id} · ${name} stop to ${to === 'cost' ? `cost${isNum(h?.entry_px) ? ` (${px(h.entry_px)})` : ''}` : px(to)}?\n\nThe paper book follows the new stop. Nothing is sent to the broker.`)) return;
    run(api.tipsTrail(id, to), `stop trailed to ${to}`); setTrail(null);
  };
  const pause = () => { if (window.confirm(`Pause #${id} · ${name}?\n\nNo entry until you resume it.`)) run(api.tipsPause(id), 'paused'); };
  const symInferred = h?.decoded?.symbol === 'strike';
  const canSym = open && !h?.taken && (!h?.symbol || symInferred) && h?.strike && h?.opt_type;
  const setSym = (e) => {
    const sym = e.target.value;
    if (!sym) return;
    e.target.value = '';
    if (!window.confirm(`Set #${id} to ${sym} ${px(h.strike)} ${h.opt_type}?\n\nThe post named no index. The contract is resolved, the plan rebuilt and tracking starts. Check the channel's screenshot first.`)) return;
    run(api.tipsSymbol(id, sym), `symbol set to ${sym}`);
  };
  const resume = () => { if (window.confirm(`Resume #${id} · ${name}?\n\nThe entry rule is watched again.`)) run(api.tipsResume(id), 'resumed'); };
  const shot = h?.shot;
  const useShot = () => {
    const u = shot?.use || {};
    if (!window.confirm(`${shot?.use_label} for #${id}?\n\n${shot?.text}\n\nThe contract is resolved again and the plan rebuilt; an armed Kite entry on the old strike is cancelled first. Logged.`)) return;
    run(api.tipsStrike(id, { strike: u.strike, opt_type: u.opt_type, symbol: u.symbol }), `strike set from the screenshot`);
  };
  const keepText = () => {
    if (!window.confirm(`${shot?.keep_label} for #${id}?\n\nThe text's strike stays; the mismatch is marked resolved. Logged.`)) return;
    run(api.tipsStrike(id, { keep: true }), "the text's strike kept");
  };
  const raw = h?.text ?? h?.raw ?? '';
  const live = open && h?.taken && isNum(h?.ltp) ? `live ${px(h.ltp)}${isNum(h?.pnl) ? ` · ${money(h.pnl)}` : ''}` : '';
  return <div className={`tp-hint ${h?.status === 'active' ? 'active' : ''} ${open ? '' : 'closed'}`} data-id={id}>
    <div className="h"><span className="id">#{id ?? '?'}</span><b className="nm" title={name}>{name}</b>{isAi(h, 'symbol', 'strike', 'opt_type') && <AiTag title="filled by the local model from the post's own text" />}
      {statusTag(h)}{h?.tracked === false && <Tag c="bad" title="no quotes for this instrument: it is not scored">{h.untracked_why || 'untracked · log in'}</Tag>}
      {symInferred && <Tag c="w" title="the post named no index: it was read from the strike level · set it by hand if the screenshot says otherwise">index from strike</Tag>}
      {h?.kite?.needs_activation && open && <Tag c="w" title="wait words and no level: the entry waits for the channel's active / activated">needs activation</Tag>}
      {h?.kite?.auto && <Tag c={h.kite.auto.state === 'not_armed' ? 'bad' : h.kite.auto.state === 'waiting' ? 'w' : 'ok'} title={h.kite.auto.text}><span className="tp-adot">●</span> {h.kite.auto.text}</Tag>}
      {shotTag(shot)}
      {canSym && <select className="tp-in" aria-label="set the index" defaultValue="" onChange={setSym} style={{ fontSize: 11, padding: '1px 4px' }} title="the post named no index: pick it after looking at the channel's screenshot">
        <option value="">{h?.symbol ? 'change index…' : 'set index…'}</option>{['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'].map((x) => <option key={x} value={x}>{x}</option>)}</select>}
      <span className="who">{[h?.channel, when(h?.created ?? h?.posted_at)].filter(Boolean).join(' · ')}</span></div>
    {raw ? <div className="raw">{raw}</div> : <div className="raw dim">(no text)</div>}
    <Decoded h={h} />
    <ShotLine h={h} busy={busy} onUse={useShot} onKeep={keepText} />
    <TargetBar h={h} />
    <Timeline events={arr(h?.kite?.log).length ? [...arr(h?.events), ...arr(h.kite.log)].sort((a, b) => String(a?.t || '').localeCompare(String(b?.t || ''))) : h?.events} />
    {kinfo && kinfo.mode !== 'off' && h?.kite && (open || (h.kite.state && h.kite.state !== 'none')) && <KiteLegs h={h} kinfo={kinfo} onDone={onDone} />}
    <div className="acts">
      {offer && <>
        <span className="lotf"><input inputMode="numeric" aria-label="lots" placeholder={String(n)} value={lots} onChange={(e) => setLots(e.target.value.replace(/[^\d]/g, '').slice(0, 3))} />lots</span>
        {can('take') && <button type="button" className="btn small" disabled={busy} onClick={() => take(false)}>Take #{id} · {plural(n, 'lot')}</button>}
        {can('take_half') && <button type="button" className="btn ghost small" disabled={busy} onClick={() => take(true)}>Take ½</button>}
        {can('skip') && <button type="button" className="btn ghost small" disabled={busy} onClick={skip}>Skip</button>}
        {can('resume') && <button type="button" className="btn ghost small" disabled={busy} onClick={resume}>Resume</button>}
        {can('pause') && <button type="button" className="btn ghost small" disabled={busy} onClick={pause}>Pause</button>}</>}
      {holding && <>
        {can('exit') && <button type="button" className="btn ghost small tp-danger" disabled={busy} onClick={exit}>Exit #{id} now…</button>}
        {!can('trail') ? null : trail == null ? <button type="button" className="btn ghost small" disabled={busy} onClick={() => setTrail('')}>Trail SL…</button>
          : <span className="lotf"><input aria-label="trail stop to" placeholder="price or cost" value={trail} onChange={(e) => setTrail(e.target.value.slice(0, 12))}
            onKeyDown={(e) => { if (e.key === 'Enter') doTrail(); if (e.key === 'Escape') setTrail(null); }} />
            <button type="button" className="btn small" disabled={busy} onClick={doTrail}>Trail</button>
            <button type="button" className="btn ghost small" onClick={() => setTrail(null)}>✕</button></span>}</>}
      <span className="note">{msg ? <span className={msg.startsWith('⚠') ? 'down' : 'up'}>{msg}</span>
        : live ? <span className={tone(h?.pnl)}>{live}</span>
          : open ? (!offer ? (h?.plan?.text || '') : `${!stated && !h?.taken ? 'size not stated · you choose · ' : ''}Telegram: ${h?.telegram || `take ${id} · take ${id} 2 · skip ${id}`}`)
            : h?.taken ? `${money(h?.pnl)} · closed${h?.exit_px != null ? ` at ${px(h.exit_px)}` : ''}` : "scored for the channel's record only · you did not take it"}</span>
    </div></div>;
}

// The channel feed: every post with its kind; chatter / promo / image dimmed and folded.
const QUIET = ['chatter', 'promo', 'image'];
function kindTag(p) {
  const k = p?.kind;
  if (k === 'hint') return <Tag c="ok">hint{p.hint_id != null ? ` #${p.hint_id}` : ''}</Tag>;
  if (k === 'follow_up') return <Tag c="a">follow-up{p.hint_id != null ? ` #${p.hint_id}` : ''}</Tag>;
  if (k === 'image' && p.hint_id != null) return <Tag>📷 screenshot #{p.hint_id}</Tag>;
  return <Tag>{k || 'post'}</Tag>;
}
function Feed({ feed, reader }) {
  const [all, setAll] = useState(false);
  const posts = arr(feed);
  const isQuiet = (p) => QUIET.includes(p?.kind) && !p?.ocr_line;      // 📷 a screenshot that was read is shown
  const quiet = posts.filter(isQuiet).length;
  const shown = all ? posts : posts.filter((p) => !isQuiet(p));
  const src = reader?.state === 'live' ? 'your account' : reader?.state === 'preview' ? 'public preview' : reader?.state === 'down' ? 'reader down' : 'not logged in';
  const lag = reader?.state === 'live' && isNum(reader?.lag_ms) && reader.lag_ms < 3600000 ? ` · ${Number(reader.lag_ms) < 1000 ? `${Math.round(reader.lag_ms)} ms` : `${Math.round(reader.lag_ms / 1000)} s`}` : '';
  return <Panel title="Channel feed" meta={reader?.state ? String(reader.state).replace('_', ' ') : null} right={<span>{src}{lag}</span>}>
    {!shown.length && <p className="muted small rempty">{posts.length ? 'Only chatter so far.' : 'No posts yet: they appear here about a second after the channel posts.'}</p>}
    <div className="tp-feed">{shown.map((p, i) => {
      const q = isQuiet(p);
      const text = p?.kind === 'image' && !p?.text ? (p?.ocr_line ? `📷 read: ${p.ocr_line}` : p?.ocr_error ? `📷 screenshot not read: ${p.ocr_error}` : '📷 image · ignored')
        : p?.text || (p?.has_media ? (p?.ocr_line ? `📷 read: ${p.ocr_line}` : '📷 media') : '');
      return <div key={p?.id ?? i} className={`fmsg ${q ? 'chat' : ''} ${p?.deleted_at || p?.deleted ? 'del' : ''}`}>
        <span className="t">{when(p?.posted_at)}</span>
        <span className="raw" title={p?.deleted_at ? `deleted ${when(p.deleted_at)} · kept` : p?.edited_at ? `edited ${when(p.edited_at)}` : undefined}>{text}{(p?.edited_at || p?.edited) && <span className="dim"> · edited</span>}{(p?.deleted_at || p?.deleted) && <span className="dim"> · deleted</span>}</span>
        <span className="fk">{kindTag(p)}{(p?.classified_by === 'ai' || p?.ai === true) && <AiTag of={p} />}</span></div>;
    })}</div>
    <div className="rnote">code drops posts with no number and no trade word · the model only sorts the rest into hint / follow-up / chatter and links a follow-up to an open hint · the raw text is always kept
      {quiet > 0 && <> · <button type="button" className="btn ghost small" onClick={() => setAll(!all)}>{all ? 'hide chatter' : `show chatter (${quiet})`}</button></>}</div>
  </Panel>;
}

const rLabel = (r) => r?.label ?? r?.key ?? r?.by ?? r?.instrument ?? r?.hour ?? '—';
const avgR = (v) => (isNum(v) ? `${Number(v) > 0 ? '+' : ''}${Number(v).toFixed(1)}` : '—');
function Record({ record, channel, days = 30 }) {
  const rec = record || {};
  const row = (r, k) => ({ key: k, cells: [rLabel(r), px(r?.hints), <span className="up">{px(r?.t1_first)}</span>, <span className="down">{px(r?.stop_first)}</span>, px(r?.no_fill), <span className={tone(r?.avg_r)}>{avgR(r?.avg_r)}</span>] });
  const rows = [];
  if (arr(rec.by_instrument).length) { rows.push('by instrument'); arr(rec.by_instrument).forEach((r, i) => rows.push(row(r, `i${i}`))); }
  if (arr(rec.by_hour).length) { rows.push('by hour posted'); arr(rec.by_hour).forEach((r, i) => rows.push(row(r, `h${i}`))); }
  if (rec.totals && typeof rec.totals === 'object') { rows.push('all'); rows.push({ ...row({ label: 'every hint', ...rec.totals }, 'tot'), cls: 'tp-tot' }); }
  const un = isNum(rec.untracked) ? Number(rec.untracked) : isNum(rec.untracked?.count) ? Number(rec.untracked.count) : isNum(rec.untracked?.n) ? Number(rec.untracked.n) : 0;
  return <Panel title={`Record${channel ? ` · ${channel}` : ''}`} meta={`${rec.days ?? days} d`}>
    <FixedTable cols={[{ h: 'By', ell: true }, { h: 'Hints', w: 62, n: true }, { h: 'T1 first', w: 70, n: true }, { h: 'Stop first', w: 88, n: true }, { h: 'No fill', w: 64, n: true, lo: true }, { h: 'Avg R', w: 64, n: true }]}
      rows={rows} empty="No scored hints yet: every hint is scored by code from stored quotes, taken or not." />
    <div className="rnote">scored by code from stored quotes: did T1 or the stop come first, time to it, best and worst move · "no fill" when a "when comes" zone never came
      {un > 0 && <> · <b className="warn">{plural(un, 'hint')} untracked</b> (no quotes: log in) · not in these numbers</>}</div>
  </Panel>;
}

const endLabel = (e) => (!e ? 'closed' : /^t\d$/i.test(e) ? String(e).toUpperCase() : String(e).replace(/_/g, ' '));
function Book({ book }) {
  const b = book || {};
  const pos = arr(b.positions), tr = arr(b.trades);
  const nm = (x) => <><b className="mono">#{x?.hint_id ?? x?.id ?? '?'}</b> {String(x?.title || x?.name || (x?.strike == null ? x?.symbol : '') || '').replace(/^(BUY|SELL) /, '') || [x?.symbol, x?.strike, x?.opt_type].filter((y) => y != null && y !== '').join(' ') || x?.tradingsymbol || ''}</>;
  const lotsOf = (x) => (x?.half ? `½ of ${px(x?.lots)}` : isNum(x?.qty_open) && isNum(x?.qty) && Number(x.qty_open) < Number(x.qty) ? `${px(x.qty_open)}/${px(x.qty)} qty` : px(x?.lots ?? x?.qty_lots));
  const rows = [
    ...pos.map((x, i) => ({ key: `p${i}`, cells: [nm(x), lotsOf(x), px(x?.entry_px ?? x?.entry), <span className={tone(x?.pnl)}>{money(x?.pnl)}</span>, <Tag c="ok">{x?.state || 'open'}{x?.trail ? ' · trail' : ''}</Tag>] })),
    ...tr.map((x, i) => ({ key: `t${i}`, cells: [nm(x), lotsOf(x), px(x?.entry_px ?? x?.entry), <span className={tone(x?.net ?? x?.pnl)}>{money(x?.net ?? x?.pnl)}</span>,
      <Tag c={(x?.ended ?? x?.why) === 'stop' ? 'bad' : /^t\d/.test(String(x?.ended ?? x?.why ?? '')) ? 'ok' : ''}>{endLabel(x?.ended ?? x?.why)}</Tag>] })),
  ];
  return <Panel title="Tips · paper book" meta={`${pos.length} open · ${tr.length} closed`} right={isNum(b.capital) ? <span>capital <b className="mono">{inr(Number(b.capital))}</b></span> : null}>
    <FixedTable cols={[{ h: 'Hint', ell: true }, { h: 'Lots', w: 92, n: true }, { h: 'Entry', w: 70, n: true }, { h: 'P&L', w: 88, n: true }, { h: 'Ended', w: 104 }]}
      rows={rows} empty="Nothing taken yet. Take a hint from its card (or reply take N on Telegram)." />
  </Panel>;
}

// A small modal in the page's style; Esc or the backdrop closes it.
function Dialog({ title, onClose, children, wide }) {
  useEffect(() => { const k = (e) => { if (e.key === 'Escape') onClose(); }; window.addEventListener('keydown', k); return () => window.removeEventListener('keydown', k); }, [onClose]);
  return <div className="tp-back" onClick={onClose}><div className={`tp-dlg ${wide ? 'wide' : ''}`} role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
    <div className="ph"><b>{title}</b><span className="r"><button type="button" className="btn ghost small" onClick={onClose} aria-label="Close">✕</button></span></div>
    <div className="rbody">{children}</div></div></div>;
}

function ChannelsDialog({ current, onClose, onSaved }) {
  const [v, setV] = useState((current || '').split(' · ').join('\n'));
  const [msg, setMsg] = useState('');
  const save = () => {
    const list = v.split(/[;\n]+/).map((x) => x.replace(/^@|^https?:\/\/t\.me\/(s\/)?/i, '').trim()).filter(Boolean);
    if (!list.length) { setMsg('⚠ at least one channel'); return; }
    if (!window.confirm(`Read these channels?\n\n${list.join('\n')}\n\nThe reader follows them from the next post. Nothing is traded.`)) return;
    api.putTipsSettings({ channels: list.join(';') }).then(() => { onSaved && onSaved(); onClose(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  return <Dialog title="Channels" onClose={onClose}>
    <p className="small muted" style={{ marginTop: 0 }}>One channel per line: a channel's @username, or for a private channel its full title exactly as Telegram shows it (spaces and commas included) or its numeric id. Any channel your account is a member of; public ones also work through the preview.</p>
    <textarea className="tp-in mono" rows={3} value={v} onChange={(e) => setV(e.target.value)} placeholder={'Index Options Buying with Aakanksha\na_public_username'} autoFocus style={{ resize: 'vertical', width: '100%' }} />
    <div className="tp-row"><span className={msg.startsWith('⚠') ? 'down small' : 'small'}>{msg}</span><button type="button" className="btn small" onClick={save}>Save…</button></div>
  </Dialog>;
}

function ImportDialog({ onClose, onDone }) {
  const [files, setFiles] = useState([]);
  const [prog, setProg] = useState(null);
  const [res, setRes] = useState('');
  const go = () => {
    if (!files.length) return;
    if (!window.confirm(`Import ${plural(files.length, 'file')} of the channel export?\n\nPosts are stored with their kind, hints decoded and scored where quotes allow. No alert is sent and nothing is traded.`)) return;
    setProg(0); setRes('');
    api.tipsImport(files, setProg).then((r) => {
      setProg(null);
      const parts = r && typeof r === 'object' ? [isNum(r.posts) && `${px(r.posts)} posts`, isNum(r.hints) && `${px(r.hints)} hints`, isNum(r.follow_ups) && `${px(r.follow_ups)} follow-ups`,
        isNum(r.scored) && `${px(r.scored)} scored`, isNum(r.skipped) && `${px(r.skipped)} already there`].filter(Boolean) : [];
      setRes(`✓ imported${parts.length ? ` · ${parts.join(' · ')}` : ''}`); onDone && onDone();
    }).catch((e) => { setProg(null); setRes(`⚠ ${e.message}`); });
  };
  return <Dialog title="Import export" onClose={onClose}>
    <p className="small muted" style={{ marginTop: 0 }}>The channel's Telegram Desktop export (Export chat history → HTML): pick its messages*.html files. Optional: the record otherwise starts today.</p>
    <input type="file" accept=".html,.htm,text/html" multiple onChange={(e) => { setFiles([...(e.target.files || [])]); setRes(''); }} />
    {files.length > 0 && <p className="small mono tp-files">{files.map((f) => f.name).join(' · ')}</p>}
    {prog != null && <div className="tp-prog"><i style={{ width: `${Math.round(prog * 100)}%` }} /><span className="small">{prog < 1 ? `uploading ${Math.round(prog * 100)}%` : 'reading the posts…'}</span></div>}
    <div className="tp-row"><span className={res.startsWith('⚠') ? 'down small' : 'up small'}>{res}</span>
      <button type="button" className="btn small" disabled={!files.length || prog != null} onClick={go}>Import…</button></div>
  </Dialog>;
}

// "Try a post": a dry decode of pasted text (POST /api/tips/test-post) · nothing saved, nothing pushed.
function TryPost({ onClose }) {
  const [text, setText] = useState('');
  const [out, setOut] = useState(null);
  const [msg, setMsg] = useState('');
  const go = () => {
    if (!text.trim()) return;
    setMsg('decoding…');
    api.tipsTestPost(text).then((r) => { setOut(r); setMsg(''); }).catch((e) => { setOut(null); setMsg(`⚠ ${e.message}`); });
  };
  const h = out?.hint ?? out?.decoded_hint ?? (out?.decoded && (out.decoded.symbol || out.decoded.entry) ? { ...out.decoded, kind: out.kind, classified_by: out.by } : null) ?? (out?.symbol || out?.entry ? out : null);
  return <Panel title="Try a post" meta="nothing saved" right={<button type="button" className="btn ghost small" onClick={onClose}>✕</button>}>
    <div className="rbody">
      <textarea className="tp-in mono" rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder={'Buy bank nifty 50300 PE near 250-270 when comes\nSl 190\nTgt 320-350-400++'} />
      <div className="tp-row"><span className={msg.startsWith('⚠') ? 'down small' : 'small muted'}>{msg || (out ? <>kind <b>{out.kind || (h ? 'hint' : '—')}</b>{out.classified_by === 'ai' && <AiTag of={out} />}{out.why ? ` · ${out.why}` : ''}</> : 'the decoder runs on the text; the model only fills gaps from it')}</span>
        <button type="button" className="btn small" disabled={!text.trim()} onClick={go}>Decode</button></div>
      {h && <div className="tp-try"><div className="h"><b>{hintName(h)}</b></div><Decoded h={h} /><TargetBar h={h} />
        {h?.stop?.kind === 'none' && <p className="small warn">stop missing → no plan</p>}</div>}
      {out && !h && <p className="small muted">Not a hint: {out.kind || 'no trade found'}.</p>}
    </div></Panel>;
}

// Login: phone → code (+ the 2-step password when the account has one) → done. The code is never echoed back.
function LoginPanel({ reader, onDone }) {
  const [step, setStep] = useState('phone');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [pw, setPw] = useState('');
  const [needPw, setNeedPw] = useState(false);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [acct, setAcct] = useState('');
  const sendPhone = () => {
    if (!phone.trim()) return;
    setBusy(true); setMsg('');
    api.tipsLogin(phone.trim()).then(() => { setStep('code'); setMsg('Telegram sent a code to your app.'); }).catch((e) => setMsg(`⚠ ${e.message}`)).finally(() => setBusy(false));
  };
  const sendCode = () => {
    if (!code.trim()) return;
    setBusy(true); setMsg('');
    const c = code.trim(), p = pw;
    setCode(''); setPw('');                               // never kept on screen
    api.tipsLoginCode(c, p || undefined).then((r) => {
      if (r?.need_password || r?.state === 'password') { setNeedPw(true); setMsg('This account has a 2-step password: enter the code again with it.'); return; }
      setAcct(r?.account_masked || ''); setStep('done'); setMsg(''); onDone && onDone();
    }).catch((e) => { if (/password/i.test(e.message)) setNeedPw(true); setMsg(`⚠ ${e.message}`); }).finally(() => setBusy(false));
  };
  return <Panel title="Log the reader in" meta={step === 'done' ? 'done' : step === 'code' ? 'step 2 of 2' : 'step 1 of 2'} className="tp-login">
    <div className="rbody">
      {step === 'phone' && <><p className="small muted" style={{ marginTop: 0 }}>The reader uses your own Telegram account (read only) so a new post arrives in about a second. The session is saved on the server, never shown or logged.{reader?.account_masked ? ` Last account: ${reader.account_masked}.` : ''}</p>
        <div className="tp-row"><input className="tp-in mono" inputMode="tel" autoComplete="tel" placeholder="+91 98xxxxxxxx" value={phone} onChange={(e) => setPhone(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && sendPhone()} />
          <button type="button" className="btn small" disabled={busy || !phone.trim()} onClick={sendPhone}>Send code</button></div></>}
      {step === 'code' && <><div className="tp-row"><input className="tp-in mono" inputMode="numeric" autoComplete="one-time-code" placeholder="code" value={code} onChange={(e) => setCode(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && sendCode()} />
        {needPw && <input className="tp-in" type="password" autoComplete="current-password" placeholder="2-step password" value={pw} onChange={(e) => setPw(e.target.value)} />}
        <button type="button" className="btn small" disabled={busy || !code.trim()} onClick={sendCode}>Log in</button>
        <button type="button" className="btn ghost small" onClick={() => { setStep('phone'); setMsg(''); }}>Back</button></div></>}
      {step === 'done' && <p className="small up" style={{ margin: 0 }}>✓ Logged in{acct ? <> as <b className="mono">{acct}</b></> : ''} · the reader starts on the next refresh.</p>}
      {msg && <p className={`small ${msg.startsWith('⚠') ? 'down' : 'muted'}`} style={{ marginBottom: 0 }}>{msg}</p>}
    </div></Panel>;
}

const STATE = { live: ['ok', 'reader live'], preview: ['warn', 'reader preview'], down: ['bad', 'reader down'], logged_out: ['bad', 'reader logged out'] };
const channelsOf = (d) => {
  const c = d?.settings?.channels ?? d?.channels ?? d?.reader?.channels;
  return Array.isArray(c) ? c.join(' · ') : typeof c === 'string' ? c.split(/[;\n]+/).map((x) => x.trim()).filter(Boolean).join(' · ') : '';
};

export default function TipsPage() {
  const [d, reload0, err] = useLiveOr('tips', api.getTips, 5000);
  const [kd, setKd] = useState(null);                    // ⚡ GET /api/tips/kite: today's orders, the guards, the caps
  const [ktest, setKtest] = useState({ busy: false, err: '', hidden: null });  // the connection test panel (today's last test; ✕ hides it)
  const phone = useIsPhone();
  const [dlg, setDlg] = useState(null);
  const [tryOpen, setTryOpen] = useState(false);
  const [older, setOlder] = useState(false);
  const [msg, setMsg] = useState('');
  const topRef = useRef(null);
  const data = d && typeof d === 'object' && !d.error ? d : null;
  const loadKite = () => { api.getTipsKite().then((r) => setKd(r && typeof r === 'object' ? r : null)).catch(() => {}); };
  const reload = () => { reload0(); loadKite(); };
  useEffect(() => { loadKite(); }, [data?.at]);          // again whenever the summary changes (5 s while a hint is open)
  const hints = useMemo(() => {
    const h = arr(data?.hints).filter(Boolean);
    const rank = (x) => (x.status === 'active' ? 0 : OPEN.includes(x.status) ? 1 : 2);
    return [...h].sort((a, b) => rank(a) - rank(b) || String(b.created ?? b.posted_at ?? '').localeCompare(String(a.created ?? a.posted_at ?? '')) || (Number(b.id) || 0) - (Number(a.id) || 0));
  }, [data]);
  if (!data) {
    return <div className="rd tips"><Head title="💡 Tips" />
      <Panel><p className="muted small rempty">{err ? `Tips is not answering yet (${err}).` : d?.error ? `Tips: ${d.error}` : 'Loading…'}</p></Panel></div>;
  }
  const reader = data.reader || {};
  const st = STATE[reader.state] || ['', reader.state ? `reader ${reader.state}` : 'reader —'];
  const chans = channelsOf(data);
  const settings = data.settings || {};
  const defLots = settings.lots ?? data.book?.lots ?? 1;
  const C = data.cards || {};
  const book = data.book || {};
  const capital = book.capital ?? settings.capital;
  const openH = hints.filter((h) => OPEN.includes(h.status));
  const nAct = C.open?.active ?? openH.filter((h) => h.status === 'active').length;
  const nNew = C.open?.new ?? C.open?.waiting ?? openH.filter((h) => h.status !== 'active').length;
  const firstOpen = openH.slice(0, 2).map((h) => `#${h.id} ${h.status === 'active' ? [h.symbol, h.strike, h.opt_type].filter(Boolean).join(' ') : 'waiting'}`).join(' · ');
  const O = C.open || {}, B = C.book || {}, H = C.channel30 || {}, Y = C.you_vs_all || {};
  const equity = B.equity ?? B.value ?? (isNum(capital) ? Number(capital) + (Number(B.net) || 0) : null);
  const shownHints = older ? hints : hints.slice(0, Math.max(6, openH.length));
  const runTest = () => {
    setKtest({ busy: true, err: '', hidden: null });
    api.tipsKiteTest().then((r) => { setKd(r && typeof r === 'object' ? r : null); setKtest({ busy: false, err: '', hidden: null }); reload0(); })
      .catch((e) => setKtest({ busy: false, err: `⚠ ${e.message}`, hidden: null }));
  };
  const enable = () => {
    if (!window.confirm('Turn Tips on?\n\nThe reader catches the channel posts, decodes them and pushes hints to Telegram. Nothing is ever taken without you, and nothing is sent to the broker.')) return;
    api.putTipsSettings({ enabled: true }).then(() => { setMsg('Tips is on'); reload(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  return <div className="rd tips" ref={topRef}>
    <Head title={`💡 Tips${chans ? ` · ${chans}` : ''}`} right={<>{msg && <span className={msg.startsWith('⚠') ? 'down small' : 'up small'}>{msg}</span>}
      <button type="button" className="btn ghost small" onClick={() => setTryOpen(!tryOpen)}>Try a post</button>
      <button type="button" className="btn ghost small" onClick={() => setDlg('channels')}>Channels…</button>
      <button type="button" className="btn ghost small" onClick={() => setDlg('import')}>Import export</button></>}>
      <Chip dot={st[0]} title={[reader.account_masked ? `account ${reader.account_masked}` : '', reader.reason ? `why: ${reader.reason}` : ''].filter(Boolean).join(' · ') || undefined}>{st[1]}{reader.state !== 'live' && reader.reason ? <> · <span className="down">{String(reader.reason).slice(0, 90)}</span></> : ''}{reader.last_post_at ? <> · last post {when(reader.last_post_at)}</> : ''}</Chip>
      {isNum(capital) && <Chip>paper book <b>{inr(Number(capital))}</b> · separate</Chip>}
      <Chip>not linked to any strategy · you decide every take</Chip>
    </Head>
    <KiteBar k={{ ...(data.kite || {}), ...(kd || {}) }} onDone={reload} toOrders={() => document.getElementById('tp-kite-orders')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
      testing={ktest.busy} onTest={runTest} />
    {(data.kite?.mode || 'off') !== 'off' && (ktest.err || (kd?.test?.at && dayKey(toD(kd.test.at)) === dayKey(new Date()) && kd.test.at !== ktest.hidden)) &&
      <KiteTest t={ktest.err ? null : kd?.test} err={ktest.err} onClose={() => setKtest({ busy: false, err: '', hidden: kd?.test?.at || null })} />}
    {data.enabled === false && <div className="rbanner warn tp-banner"><span>💡 Tips is <b>off</b>: nothing is read or pushed. {reader.state === 'logged_out' ? 'Log the reader in below first, then turn it on.' : ''}</span>
      <button type="button" className="btn small" onClick={enable}>Turn on…</button></div>}
    <Cards n={4}>
      <Card unit="n" label="Open hints" value={`${nAct} active · ${nNew} new`} num={nAct * 100 + nNew} live={openH.length > 0}
        sub={O.line || firstOpen || 'none open'} fact={<>today <b>{px(O.today?.hints ?? O.today_hints ?? 0)}</b> hints · {px(O.today?.posts ?? O.today_posts ?? 0)} posts · {px(O.today?.chatter ?? O.today_chatter ?? 0)} chatter</>} />
      <Card label="Tips · paper" value={isNum(equity) ? inr(Number(equity)) : '—'} num={equity} k={isNum(equity) && isNum(capital) ? tone(Number(equity) - Number(capital)) : ''}
        sub={<><span className={tone(B.today)}>{money(B.today ?? 0)}</span> today · {plural(px(B.lots_open ?? B.lots ?? 0), 'lot')} open</>}
        fact={<>{B.since ? `since ${dayLabel(B.since).split(' ').slice(1).join(' ')} · ` : ''}{px(B.taken ?? 0)} taken · {px(B.won ?? 0)} won</>} />
      <Card unit="n" label="Channel · 30 d" value={plural(px(H.hints ?? 0), 'hint')} num={H.hints}
        sub={`T1 first ${px(H.t1_first ?? 0)} · stop first ${px(H.stop_first ?? 0)} · no fill ${px(H.no_fill ?? 0)}`}
        fact={<>avg <b className={tone(H.avg_r)}>{avgR(H.avg_r)} R</b>{H.best_time || H.best_hour ? <> · best {H.best_time || hm12(H.best_hour)}</> : ''}</>} />
      <Card label="Your takes vs all" value={isNum(Y.you) || isNum(Y.all) ? (phone ? money(Y.you ?? 0) : `${money(Y.you ?? 0)} vs ${money(Y.all ?? 0)}`) : '—'} num={Y.you} k={tone(Y.you)}
        sub={`${phone && isNum(Y.all) ? `all ${money(Y.all)} · ` : ''}you took ${px(Y.took ?? 0)} of ${px(Y.of ?? H.hints ?? 0)}`} fact={isNum(Y.skips) ? `skips were right ${px(Y.skips_right ?? 0)} of ${px(Y.skips)}` : 'every hint scored, taken or not'} />
    </Cards>
    <div className="tp-two">
      <div className="tp-col">
        {reader.state === 'logged_out' && <LoginPanel reader={reader} onDone={reload} />}
        {tryOpen && <TryPost onClose={() => setTryOpen(false)} />}
        {!hints.length && <Panel title="Hints"><p className="muted small rempty">No hints yet. A call in the channel becomes a card here with its decoded plan; nothing is taken without you.</p></Panel>}
        {shownHints.map((h) => <HintCard key={h.id} h={h} defLots={defLots} onDone={reload} kinfo={data.kite ? { ...data.kite, ...(kd || {}) } : null} />)}
        {hints.length > shownHints.length && <button type="button" className="btn ghost small tp-more" onClick={() => setOlder(true)}>show {hints.length - shownHints.length} older</button>}
      </div>
      <div className="tp-col">
        {(data.kite?.mode || 'off') !== 'off' && <KiteGuards kd={kd} />}
        <Feed feed={data.feed} reader={reader} />
        <Record record={data.record} channel={chans.split(' · ')[0]} />
        <Book book={book} />
      </div>
    </div>
    {((data.kite?.mode || 'off') !== 'off' || arr(kd?.orders).length > 0) && <KiteOrders kd={kd} onDone={reload} />}
    {((data.kite?.mode || 'off') !== 'off' || arr(kd?.orders).length > 0) && <KiteDayCheck kd={kd} onDone={reload} />}
    {dlg === 'channels' && <ChannelsDialog current={chans} onClose={() => setDlg(null)} onSaved={() => { setMsg('channels saved'); reload(); }} />}
    {dlg === 'import' && <ImportDialog onClose={() => setDlg(null)} onDone={reload} />}
    {phone && <span className="tp-phone-pad" />}
  </div>;
}
