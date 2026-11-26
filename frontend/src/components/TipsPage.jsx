import React, { useEffect, useMemo, useRef, useState } from 'react';
import * as api from '../services/api';
import { useLiveOr } from '../services/live';
import { clock, ampm } from '../timefmt';
import { inr } from '../fmt';
import { useIsPhone } from '../useMedia';
import { Card, Cards, Panel, FixedTable, Tag, AiTag, Chip, Head, Seg, Switch } from './ui2';
import { DRY_WARN, DRY_NOTE, KMODES, kmodeNote, hintName, modeConfirm, killConfirm, legLine, kiteTexts, PLACE_ALL_NOTE, WATCH_NOTE } from './tipsKite';

// 💡 Research › Tips: a Telegram channel's calls caught, filtered, decoded and scored. NOT linked to any strategy:
// every take is the owner's, each through a confirm, into a separate Tips PAPER book (nothing is sent to the broker).
// ⚡ Tips → Kite orders (tips.kite_mode click): on top of the paper book, a hint's legs go to Kite by the owner's click,
// each through a confirm; "Place all" = the entry now, the stop the moment it fills (GET /api/tips/kite behind it). Only
// the stop rests on Kite: the targets are WATCHED legs (the app sells at market when the price reaches one).
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
  if (s === 'active') return h?.kite?.state === 'closed' ? <Tag c="w" title="your Kite position is closed; the channel's call is still scored for its record until its target, stop or time stop">record · active</Tag> : <Tag c="ok">active</Tag>;
  if (s === 'paused') return <Tag c="w">paused</Tag>;
  if (s === 'skipped') return <Tag>skipped · scored</Tag>;
  if (s === 'expired') return <Tag>expired{h.ended ? ` · ${h.ended.replace('_', ' ')}` : ''}</Tag>;
  if (s === 'closed') return <Tag c={tone(h.pnl) === 'down' ? 'bad' : tone(h.pnl) === 'up' ? 'ok' : ''}>closed{h.ended ? ` · ${String(h.ended).replace('_', ' ')}` : ''}</Tag>;
  return s ? <Tag>{s}</Tag> : null;
}
const SRC = { channel: '📣', owner: '👤', code: '⚙', ai: '🤖', kite: '⚡' };

// The decoded grid: six fixed cells (3 columns on a phone); each value ellipsizes, never wraps. An open hint (or an open
// Kite position) gets a seventh, NOW: the strike's last price (the 5 s quote poll), bold, green / red against the fill
// once entered (neutral before), with where it sits under it ("−5.6 % from 230" / "in the zone 191–198").
// 🛑 The stop tile: the stop with its source (provisional amber · yours blue · the channel's plain) and, on an open hint
// (not taken / armed / entered), "Set stop…": an inline price box (Enter sets, Esc cancels) and ONE confirm. The
// server re-checks the side of the price and refuses a stale set (the stop changed since this page loaded: `rev`).
function StopCell({ h, onDone }) {
  const [edit, setEdit] = useState(null);             // null = closed, else the box's text
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const t = h?.stop_tile || null;
  const val = t?.value_text || stopText(h?.stop);
  const can = !!(onDone && h?.id != null && t?.settable);
  const set = () => {
    const raw = String(edit || '').trim();
    if (!isNum(raw) || Number(raw) <= 0) { setErr('⚠ a price'); return; }
    const v = Number(raw);
    if (!window.confirm(`Set #${h.id}'s stop to ${px(v)}?\n\nBefore the fill it rewrites the plan and the SL the app places on the fill. After the fill it modifies the SL order on Kite. The channel's SL, when it comes, replaces it.`)) return;
    setBusy(true); setErr('');
    api.tipsSetStop(h.id, v, isNum(t?.rev) ? Number(t.rev) : undefined)
      .then(() => { setEdit(null); onDone(); }).catch((e) => setErr(`⚠ ${e.message}`)).finally(() => setBusy(false));
  };
  return <div className={`stc ${t?.cls || ''}`} title={[val, t?.sub].filter(Boolean).join('\n')}>
    <div className="kk"><span>stop{isAi(h, 'stop') && <AiTag title="filled by the local model from the post's own text" />}</span>
      {can && edit == null && <button type="button" className="btn ghost small sbtn" onClick={() => { setEdit(''); setErr(''); }}>Set stop…</button>}</div>
    <b className={val === 'no usable stop' ? 'warn' : ''}><span className="v">{val}</span></b>
    {edit == null ? (t?.sub && <div className="s1">{t.sub}</div>)
      : <span className="inl"><input aria-label={`set #${h.id}'s stop`} inputMode="decimal" placeholder="price" autoFocus value={edit}
        onChange={(e) => setEdit(e.target.value.replace(/[^\d.]/g, '').slice(0, 10))}
        onKeyDown={(e) => { if (e.key === 'Enter') set(); if (e.key === 'Escape') { setEdit(null); setErr(''); } }} />
        <button type="button" className="btn small" disabled={busy} onClick={set}>Set</button>
        <button type="button" className="btn ghost small" onClick={() => { setEdit(null); setErr(''); }} aria-label="cancel">✕</button></span>}
    {err && <div className="s1 down">{err}</div>}
  </div>;
}

export function Decoded({ h, onDone }) {
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
  const nw = h?.now && isNum(h.now.ltp) && (OPEN.includes(h?.status) || h?.kite?.state === 'entered' || h?.position?.state === 'open') ? h.now : null;
  const nt = h?.now_tile && h.now_tile.value_text ? h.now_tile : null;     // 💡 the Kite NOW tile (live · at exit · armed)
  const nowCell = nt ? <NowTile key="now" t={nt} />
    : nw && <div key="now" className={`nowc ${nw.tone || ''}`} title={`now ${px(nw.ltp)}${nw.sub ? ` · ${nw.sub}` : ''}`}>
      <div className="kk">now</div><b className={`nv ${nw.tone || ''}`}><span className="v">{px(nw.ltp)}</span></b>{nw.sub && <div className="ns">{nw.sub}</div>}</div>;
  const sz = h?.sizing && h.sizing.line1 ? h.sizing : null;     // 📏 the size cell (server): lots · qty, the reasoning, the rule
  const out = cells.map(([k, v, f]) => (k === 'stop' && (h?.stop_tile || onDone) ? <StopCell key={k} h={h} onDone={onDone} /> : k === 'size' && sz
    ? <div key={k} className={`szc ${sz.held ? 'held' : ''}`} title={[sz.line1, sz.line2, sz.line3].join('\n')}>
      <div className="kk">size{isAi(h, ...f) && <AiTag title="filled by the local model from the post's own text" />}</div>
      <b className={sz.held ? 'down' : ''}><span className="v">{sz.line1}</span></b>
      <div className="s1">{sz.line2}</div><div className={`s2 ${sz.held ? 'down' : ''}`}>{sz.line3}</div></div>
    : <div key={k} title={typeof v === 'string' ? v : undefined}>
      <div className="kk">{k}</div><b className={k === 'stop' && v === 'no usable stop' ? 'warn' : ''}><span className="v">{v}</span>{isAi(h, ...f) && <AiTag title="filled by the local model from the post's own text" />}</b></div>));
  if (nowCell) out.splice(2, 0, nowCell);                // NOW next to ENTRY / STOP / EXPIRY
  return <div className={`tp-dec${nt ? ' n8' : nowCell ? ' n7' : ''}${sz ? ' sz' : ''}`}>{out}</div>;
}

// 💡 The Kite card blocks (the server computes every figure; the page only renders). Every value sits on one line and
// ellipsizes, so a live tick never squeezes or grows the card.
// The big NOW tile: two cells wide, a 24 px price (green / red against the fill, grey before it), the tick's change,
// a pulse while open on Kite; "NOW · AT EXIT" once closed.
function NowTile({ t }) {
  return <div className={`nowt ${t.state} ${t.tone || ''}`} title={`${t.label} ${t.value_text}${t.sub ? ` · ${t.sub}` : ''}`}>
    <div className="kk">{t.label}</div>
    <div className="nb"><span className={`v ${t.tone || ''}`}>{t.value_text}</span>{t.sub && <span className={`s ${t.state === 'live' ? t.tone || '' : ''}`}>{t.sub}</span>}</div>
    {t.pulse && <span className="pulse" aria-label="live" />}</div>;
}

// The P&L strip under the card header (only when Kite has / had a position on the hint).
export function PnlStrip({ s }) {
  if (!s) return null;
  const mv = (x, c) => (x ? <><b className={c}>{x.text}</b>{x.at ? ` ${x.at}` : ''}</> : '—');
  return <div className={`tp-pnl ${s.tone === 'down' ? 'neg' : s.tone === 'up' ? 'pos' : ''}`}>
    <div className="pk"><span className="lbl" title={s.label}>{s.dry ? 'DRY · ' : ''}{s.label}</span>
      <div className={`big ${s.tone || ''}`}><span className="v">{s.pnl_text}</span><small>{s.pct_text}</small></div></div>
    <div className="pkv"><span className="lbl">net after charges</span><span className="vv" title="estimate: brokerage, STT, exchange, SEBI, stamp duty, GST at the broker's F&O option rates"><b>{s.net_text}</b></span></div>
    <div className="pkv"><span className="lbl">{s.time_k}</span><span className="vv" title={`${s.time_v}${s.time_sub ? ` · ${s.time_sub}` : ''}`}><b>{s.time_v}</b>{s.time_sub ? ` · ${s.time_sub}` : ''}</span></div>
    <div className="pkv"><span className="lbl">best · worst</span><span className="vv">{mv(s.best, 'up')} · {mv(s.worst, 'down')}</span></div>
    {(s.booked_text || s.rest_text || s.avg_text) && <div className="pkv parts"><span className="lbl">{s.avg_text ? 'after the add' : 'part booked'}</span>
      <span className="vv" title={[s.avg_text, s.booked_text, s.rest_text].filter(Boolean).join(' · ')}>{[s.avg_text, s.booked_text, s.rest_text].filter(Boolean).join(' · ')}</span></div>}
    {s.state === 'closed' && <div className="pkv"><span className="lbl">channel's call</span><span className="vv" title={s.channel ? `${s.channel.text}${s.channel.note ? ` · ${s.channel.note}` : ''}` : undefined}>
      {s.channel ? <>{s.channel.text}{s.channel.note ? <> · <b className={s.channel.tone || ''}>{s.channel.note}</b></> : null}</> : 'no result yet'}</span></div>}
    <span className="day" title={s.day?.text}>{s.day?.text || ''}</span>
  </div>;
}

// stop ◀──●──▶ target: the red zone near the stop, the green one near the last target, a marker at the fill, ticks at
// the targets before the last, the dot at the price (the exit once closed).
export function DistBar({ d }) {
  if (!d) return null;
  const tname = d.target?.name && d.target.name !== 'target' ? `target ${d.target.name}` : 'target';
  return <div className={`tp-dist ${d.closed ? 'closed' : ''} ${d.danger ? 'danger' : ''}`}>
    <div className="lbls">
      <span className="l">stop <b>{px(d.stop?.px)}</b> · <b className={d.stop?.tone || ''}>{d.stop?.pct_text}</b> {d.stop?.note}</span>
      <span className="m">fill <b>{px(d.fill?.px)}</b></span>
      <span className="r">{tname} <b>{px(d.target?.px)}</b>{!d.closed && <> · <b className={d.target?.tone || ''}>{d.target?.pct_text}</b></>}
        {d.target?.note ? <> · <b className={d.target?.tone || ''}>{d.target.note}</b></> : ''}</span></div>
    <div className="bar" role="img" aria-label={`price ${px(d.dot_px)} between the stop ${px(d.stop?.px)} and the target ${px(d.last)}`}>
      <span className="zs" style={{ width: `${d.zones?.[0] ?? 16}%` }} /><span className="zt" style={{ left: `${d.zones?.[1] ?? 84}%` }} />
      {arr(d.ticks).map((x) => <em key={x.label} className="tk" style={{ left: `${x.pos}%` }} title={`${x.label} ${px(x.px)}`} />)}
      <em className="fl" style={{ left: `${d.fill?.pos ?? 0}%` }} title={`fill ${px(d.fill?.px)}`} />
      <i className="dot" style={{ left: `${d.dot ?? 0}%` }} /></div>
  </div>;
}

// "channel said next": every linked follow-up and what the app did, post edits, your exit, the record, the AI reads.
export function SaidNext({ chips }) {
  const c = arr(chips);
  if (!c.length) return null;
  return <div className="tp-fu"><span className="lb">channel said next</span>{c.map((x, i) => <span key={i} className={`c ${x.kind || ''}`} title={`${x.at_label ? `${x.at_label} · ` : ''}${x.text}`}>
    {x.kind !== 'ai' && x.kind !== 'more' && x.at_label && <b>{x.at_label}</b>}<span className="tx">{x.text}</span></span>)}</div>;
}

// The trade facts row: post → fill · slippage · spread · index move · stop nearest · exit by.
export function FactsRow({ f }) {
  const c = arr(f);
  if (!c.length) return null;
  return <div className="tp-facts">{c.map((x) => <div key={x.k} title={`${x.k}: ${x.v}`}><span className="kk">{x.k}</span><span className={`v ${x.tone || ''}`}>{x.v}</span></div>)}</div>;
}

export function TargetBar({ h }) {
  const t = arr(h?.targets);
  if (!t.length) return <div className="tp-tgt"><span className="muted">targets</span> <span className="dim">none stated</span></div>;
  const hit = Number(h?.targets_hit ?? h?.plan?.targets_hit ?? 0) || 0;
  return <div className="tp-tgt"><span className="muted">targets</span>{t.map((x, i) => <React.Fragment key={i}>
    <i className={i < hit ? 'ok' : ''} /><span className={`tv ${i < hit ? 'up' : 'dim'}`}>{px(x)}{h?.open_ended && i === t.length - 1 ? '++' : ''}</span></React.Fragment>)}
    {isAi(h, 'targets') && <AiTag title="targets filled by the local model from the post's own text" />}</div>;
}

// The card's event log: folded to its last LOG_TAIL lines by default (open by default only while the hint is working
// today: the server's log_open), a fixed-height toggle row at the top. The choice is remembered per hint on this
// device. Folded lines stay in the DOM (hidden), nothing is dropped.
const LOG_TAIL = 3;
const LOG_KEY = (id) => `bp.tips.log.${id}`;
const readLog = (id) => { try { const v = localStorage.getItem(LOG_KEY(id)); return v === 'open' ? true : v === 'closed' ? false : null; } catch { return null; } };
const saveLog = (id, open) => { try { localStorage.setItem(LOG_KEY(id), open ? 'open' : 'closed'); } catch { /* private mode */ } };

function Timeline({ events, id, defOpen }) {
  const ev = arr(events);
  const [mine, setMine] = useState(() => (id != null ? readLog(id) : null));
  const open = mine ?? !!defOpen;
  if (!ev.length) return null;
  const more = ev.length - LOG_TAIL;
  const toggle = () => { const v = !open; setMine(v); if (id != null) saveLog(id, v); };
  return <div className="tp-tl">
    {more > 0 && <button type="button" className="tp-tlt" aria-expanded={open} onClick={toggle}>
      {open ? '▾ show less' : `▸ ${more} more line${more === 1 ? '' : 's'} · show all`}</button>}
    {ev.map((e, i) => <div key={i} hidden={!open && i < more}><span className="tt">{when(e?.t)}</span>
      {e?.src && e.src !== 'ai' && SRC[e.src] && <span className="src" title={e.src}>{SRC[e.src]}</span>}
      <span className="tx">{e?.text || ''}</span>{e?.src === 'ai' && <AiTag of={e} />}</div>)}</div>;
}

// ⚡ Tips → Kite orders ----------------------------------------------------------------------------------------------
const LEG_IC = { entry: '🟢', stop: '🛑', t1: '🎯', t2: '🎯', t3: '🎯', exit: '⏏', add: '➕' };
const LIVE_ST = ['placed', 'open'];
const legTag = (lg) => {
  const s = lg?.status;
  const c = s === 'complete' ? 'ok' : s === 'open' || s === 'placed' ? 'a' : s === 'armed' || s === 'watched' ? 'w' : s === 'rejected' || s === 'failed' ? 'bad' : '';
  const text = s === 'placed' && lg?.leg !== 'entry' ? 'placed' : lg?.runner && s === 'watched' && lg?.status_text === 'watched' ? 'watched · runner' : lg?.status_text || 'not placed';
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
  const mult = Number(k?.sizing?.lot_multiplier ?? 1) || 1;          // 📏 lots ×1…×5 (tips.kite_lot_multiplier)
  const dstop = Number(k?.sizing?.default_stop ?? 0) || 0;          // 🛑 no-SL stop: 0 = off, else −N % (tips.kite_default_stop)
  const stopOpts = [0, 15, 20, 25, 30, 40].concat([0, 15, 20, 25, 30, 40].includes(dstop) ? [] : [dstop]).sort((a, b) => a - b);
  const setMult = (e) => {
    const v = Number(e.target.value);
    if (v === mult) return;
    if (!window.confirm(`Lots ×${v}?\n\nEvery size the app decides (the post's lots, the fallback, fixed, the channel's "2-2 lots" and its add) is multiplied by ${v}. The risk mode's own figure is not. Lots you type are literal. kite_max_lots and the risk per trade apply after.`)) { e.target.value = String(mult); return; }
    api.putTipsSettings({ kite_lot_multiplier: v }).then(() => { setMsg(''); onDone && onDone(); }).catch((x) => setMsg(`⚠ ${x.message}`));
  };
  const setDstop = (e) => {
    const v = Number(e.target.value);
    if (v === dstop) return;
    if (!window.confirm(v ? `No-SL stop −${v} %?\n\nA call with no SL gets a provisional stop ${v} % under its entry level (premium levels only), so its plan is complete and auto can arm it. The channel's SL replaces it the moment it comes.` : 'No-SL stop off?\n\nA call with no SL waits for the channel\'s SL (no plan until then), as before.')) { e.target.value = String(dstop); return; }
    api.putTipsSettings({ kite_default_stop: v }).then(() => { setMsg(''); onDone && onDone(); }).catch((x) => setMsg(`⚠ ${x.message}`));
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
    {k?.sizing && <select className="tp-in tp-ksel" aria-label="lots multiplier" value={String(mult)} onChange={setMult} title="tips.kite_lot_multiplier: the post's lots (or the fallback) × this · caps and the risk guard after">
      {[1, 2, 3, 4, 5].map((x) => <option key={x} value={String(x)}>lots ×{x}</option>)}</select>}
    {k?.sizing && <select className="tp-in tp-ksel" aria-label="no-SL stop" value={String(dstop)} onChange={setDstop} title="tips.kite_default_stop: a call with no SL gets a provisional stop this far under its entry level until the channel's SL comes">
      {stopOpts.map((x) => <option key={x} value={String(x)}>{x ? `no-SL stop −${x} %` : 'no-SL stop: off'}</option>)}</select>}
    <span className="sp" />
    {mode === 'off' ? <span className="cap">paper book only · nothing reaches Kite</span>
      : <><span className="cap" title={kiteCaps(k)}>{dry ? 'dry · ' : ''}{kiteCaps(k)}{k?.window ? ` · entries ${k.window}` : ''}</span>
        <span className={`cap note ${dry ? 'dry' : ''}`} title={dry ? DRY_NOTE : undefined}>{note}</span>
        {(mult > 1 || dstop > 0) && <span className="cap warn">{[mult > 1 ? `lots ×${mult}: the post's lots (or ${px(k?.sizing?.lots ?? 1)}) ${mult === 2 ? 'doubled' : `× ${mult}`} · caps and the risk guard apply after` : '', dstop > 0 ? `no SL in the post: a −${dstop} % stop stands in until the channel's SL arrives` : ''].filter(Boolean).join(' · ')}</span>}
        {k?.day_closed && <span className="cap warn" title={`the channel closed the day${k.day_closed.text ? `: "${k.day_closed.text}"` : ''} · auto arms nothing new today · open positions and stops untouched · a new call re-opens it`}>📕 day closed {k.day_closed.at_label}</span>}</>}
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
  const SZ = h?.sizing || {};
  const sizeLine = `size   ${plural(px(k.lots), 'lot')} · ${px(k.qty)} qty · premium ≈ ${isNum(L.entry?.premium) ? inr(Number(L.entry.premium)) : '—'} · at the stop ${isNum(L.stop?.max_loss) ? money(L.stop.max_loss) : '—'}${SZ.line3 && !SZ.held ? ` · ${SZ.line3}` : ''}`;
  const held = !!SZ.held && (acts.includes('all') || acts.includes('entry'));
  const heldWhy = held ? SZ.held_text || SZ.line3 : '';
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
  const all = () => run('all', {}, `${head('Place all on Kite')}${PLACE_ALL_NOTE}\n\n${sizeLine}\npremium ≈   ${isNum(L.entry?.premium) ? inr(Number(L.entry.premium)) : '—'}\n1 · entry   ${L.entry?.side} ${px(L.entry?.qty)} ${L.entry?.type_text || 'MARKET'} · ${ew}\n2 · the moment it fills   ${stopTxt} rests on Kite\n${L.t1 ? `3 · watched by the app   T1 ${legLine(L.t1)}${L.t2 ? ' · then the stop to cost' : ''}\n` : ''}${L.t2 ? `4 · watched by the app   T2 ${legLine(L.t2)}${L.t3 ? ' · then the stop to T1' : ''}\n` : ''}${L.t3 ? `5 · watched by the app   T3 ${legLine(L.t3)} · the runner\n` : ''}caps after this   ${capsAfter}\n\nThe steps after the entry are the app's, as the consequence of this click; the leftovers are cancelled when the position closes.${k.beyond_t2 ? ` Targets past the ${L.t3 ? 'third' : 'second'} stay in the paper record only.` : ''}${unfilled ? ` ${unfilled}` : ''}${tsLine ? `\n${tsLine}.` : ''}\n\n${W}`, `entry ${k.needs_activation || L.entry?.arm_kind === 'watch' ? 'armed' : 'sent'} · ${to} · SL on the fill · targets watched`);
  const entry = () => run('entry', {}, `${head('Place the entry on Kite')}${sizeLine}\npremium ≈   ${isNum(L.entry?.premium) ? inr(Number(L.entry.premium)) : '—'}\norder   ${L.entry?.side} ${px(L.entry?.qty)} ${L.entry?.type_text || 'MARKET'} · ${ew}${tsLine ? `\n${tsLine}` : ''}\ncaps after this   ${capsAfter}\n\nOnly the entry: the stop is yours to place once it fills (Place SL… / Place rest…); the targets are watched by the app either way.\n\n${W}`, `entry sent to ${to}`);
  const rest = () => run('rest', {}, `${head('Place the rest on Kite')}now   SL ${stopTxt} rests on Kite\n${L.t1 && L.t1.status !== 'complete' ? `watched   T1 ${legLine(L.t1)}\n` : ''}${L.t2 && L.t2.status !== 'complete' ? `watched   T2 ${legLine(L.t2)}\n` : ''}${L.t3 && L.t3.status !== 'complete' ? `watched   T3 ${legLine(L.t3)} · the runner\n` : ''}\n${WATCH_NOTE}.\n\n${W}`, `the stop sent to ${to} · targets watched`);
  const stop = () => run('stop', {}, `${head('Place the stop on Kite')}order   ${L.stop?.side} ${stopTxt}\n\n${W}`, `stop sent to ${to}`);
  const exit = () => run('exit', {}, T.exit, `exit sent to ${to}`);
  const cancel = (leg) => run('cancel', { leg }, T.cancel(leg), `${L[leg]?.name || leg} cancelled`);
  const sendNow = () => run('send_now', {}, T.sendNow.replace('\n\nSkips', `\n${sizeLine}\n\nSkips`), `entry sent to ${to}`);
  const nOk = Number(SZ.max_ok) || 0;
  const takeN = () => run('all', { lots: nOk }, `${head(`Take ${plural(nOk, 'lot')} on Kite`)}the size is held: ${SZ.held_text || ''}\n\nThis places the largest size under kite_risk_per_trade: ${plural(nOk, 'lot')} · ${px(nOk * Number(h?.lot_size || 0))} qty · at the stop ${isNum(SZ.risk_rs) && Number(SZ.lots) ? money(-Number(SZ.risk_rs) / Number(SZ.lots) * nOk) : '—'}\n${PLACE_ALL_NOTE}\ncaps after this   ${capsAfter}\n\n${W}`, `entry ${plural(nOk, 'lot')} · ${to} · SL on the fill`);
  const ad = k.add_offer || {};
  const addQty = isNum(ad.lots) ? Number(ad.lots) * Number(h?.lot_size || 0) : null;
  const placeAdd = () => run('add', {}, `${head("Place the channel's add on Kite")}add   ${L.entry?.side || 'BUY'} LIMIT ${px(ad.level)}${addQty ? ` ×${px(addQty)}` : ''} · ${k.add_text || ''}\n\nONE add per call: after the fill the stop's quantity goes up to the total (same trigger) and the entry price becomes the average. Capped by kite_max_lots and kite_risk_per_trade (blended average); cancelled with the window end, the time stop, any exit or a stop hit.\n\nAveraging doubles a bad day.\n\n${W}`, 'add armed');
  const toCost = () => run('modify_stop', { to: 'cost' }, T.moveStop('cost'), 'stop moved to cost');
  const resync = () => { if (window.confirm('Re-sync with Kite?\n\nReads the Kite order book once (only the app\'s own Tips orders) and applies what changed.')) { setBusy(true); api.tipsKiteResync().then(() => { setMsg('re-synced'); onDone && onDone(); }).catch((e) => setMsg(`⚠ ${e.message}`)).finally(() => setBusy(false)); } };
  const live = (leg) => LIVE_ST.includes(L[leg]?.status);
  const btn = (label, fn, extra = {}) => <button type="button" className={`btn ghost small ${extra.cls || ''}`} disabled={busy || extra.off} title={extra.title} onClick={fn}>{label}</button>;
  const legBtns = (lg) => {
    const out = [];
    if (lg.leg === 'entry') {
      if (acts.includes('entry')) out.push(btn('Place entry…', entry, { key: 'e', off: held, title: held ? heldWhy : undefined }));
      if (acts.includes('send_now')) out.push(btn('Send now…', sendNow, { title: "skip the channel's activation and send the entry now" }));
      if (acts.includes('cancel:entry')) out.push(btn('Cancel', () => cancel('entry')));
      if (lg.status === 'complete') out.push(<span key="d" className="dim small">done · by {lg.by === 'click' ? 'your click' : lg.by === 'dry' ? 'your click (dry run)' : lg.by}</span>);
    } else if (lg.leg === 'stop') {
      if (live('stop') && isNum(k.entry_px) && Number(lg.trigger) !== Number(k.entry_px) && acts.includes('modify_stop')) out.push(btn(`Modify → cost ${px(k.entry_px)}`, toCost));
      if (acts.includes('cancel:stop')) out.push(btn('Cancel', () => cancel('stop')));
      if (!live('stop') && lg.status !== 'complete' && lg.placeable !== false && !lg.closed && k.state !== 'closed') out.push(btn('Place SL…', stop, { off: !acts.includes('stop'), title: acts.includes('stop') ? undefined : 'after the entry fills on Kite' }));
    } else if (lg.leg === 't1' || lg.leg === 't2' || lg.leg === 't3') {      // watched by the app: no order to place (a resting one from before can be cancelled)
      if (acts.includes(`cancel:${lg.leg}`)) out.push(btn('Cancel', () => cancel(lg.leg)));
    } else if (lg.leg === 'add') {
      if (acts.includes('cancel:add')) out.push(btn('Cancel', () => cancel('add')));
    }
    return out.map((x, i) => <React.Fragment key={i}>{x}</React.Fragment>);
  };
  const note = msg ? <span className={msg.startsWith('⚠') ? 'down' : 'up'}>{msg}</span>
    : k.state === 'closed' ? `closed on Kite${k.reason ? ` · ${k.reason}` : ''} · ${money(k.realised)}`
      : acts.includes('all') ? (L.entry?.arm_kind && L.entry.arm_kind !== 'market' ? `Place all = ${L.entry.arm_kind === 'watch' ? 'the app watches the index' : 'the entry rests on Kite'}${k.needs_activation ? " (after the channel's activation)" : ''} · SL rests the moment it fills · target watched by the app` : 'Place all = entry now · SL rests the moment it fills · target watched by the app')
        : k.state === 'armed_watch' ? `armed · the app watches ${k.arm?.index || 'the index'} · buys at market on the cross${k.auto_rest ? ' · then SL' : ''}`
        : k.state === 'armed_wait' ? `armed · waiting for the channel's activation${k.auto_rest ? ' · then SL' : ''}`
        : k.state === 'armed' ? `entry ${k.arm?.kind === 'slm' || k.arm?.kind === 'limit' ? 'resting on Kite' : 'on Kite'} · waiting for the fill${k.auto_rest ? ' · then SL by the app' : ''}`
          : k.state === 'entered' ? `${px(k.qty_open)} open ${k.dry ? 'in the dry run' : 'on Kite'} · only the stop rests · targets watched by the app${k.beyond_t2 ? ` · targets past ${L.t3 ? 'T3' : 'T2'}: paper record only` : ''}${k.footer ? ` · ${k.footer}` : ''}`
            : k.sim ? 'dry run · every button opens a confirm · nothing reaches Kite' : 'every button opens a confirm · the paper book stays as it is';
  return <div className="tp-kite">
    <div className="tp-legs">{legs.map((lg) => <div key={lg.leg} className={`tp-leg ${lg.status}`}>
      <div className="t"><span className="ic">{LEG_IC[lg.leg]}</span><b>{lg.name}</b><span className="ln">· {lg.side} {px(lg.qty)} · {lg.type_text}</span>{legTag(lg)}</div>
      <div className="d" title={lg.note || undefined}>{[lg.at, lg.order_id ? `order ${lg.order_id}` : '', lg.detail].filter(Boolean).join(' · ')}</div>
      {lg.now_text && <div className={`d nw ${lg.tone || ''}`} title={lg.now_text}>{lg.now_text}</div>}
      <div className="a">{legBtns(lg)}</div></div>)}</div>
    {held && <div className="tp-held" role="alert"><span className="tx">{SZ.held_text || SZ.line3}</span>
      {nOk > 0 && acts.includes('all') && <button type="button" className="btn small" disabled={busy} onClick={takeN}>Take {plural(nOk, 'lot')}…</button>}</div>}
    {k.add_text && !k.add && <div className="tp-addl" title={k.add_why || ''}>➕ {k.add_text} · {k.follow_adds ? (k.add_why ? `not armed: ${k.add_why}` : 'kite_follow_adds on · one add after the fill') : 'the app never adds unless kite_follow_adds is on'}</div>}
    <FactsRow f={h?.facts} />
    <div className="acts tp-kacts">
      {acts.includes('all') && <button type="button" className="btn small" disabled={busy || held} title={held ? heldWhy : undefined} onClick={all}>⚡ Place all on Kite · entry + SL…</button>}
      {acts.includes('add') && <button type="button" className="btn ghost small" disabled={busy} title={k.add_text || ''} onClick={placeAdd}>➕ Place add…</button>}
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

// 🤖 AI on Tips (the server computes every number; the model wrote only the [AI]-tagged lines; nothing here acts).
const GRADE_C = { ok: 'ok', w: 'w', bad: 'bad' };
function GradeChip({ g }) {
  if (!g) return null;
  return <span className={`tp-grade ${GRADE_C[g.tone] || ''}`} title={`call grade ${g.grade} by code · ${g.why || ''} · informational: never changes the plan`}>
    grade <b>{g.grade}</b>{g.at_label ? ` · at ${g.at_label}` : ''}</span>;
}
const aiLine = (x, i, tone) => <div key={i} className="l"><span className="lk">{x.label}</span><div className={tone || ''}><AiTag title="written by the local model · every figure checked against the numbers code gave it" /> {x.text}</div></div>;
function AiState({ v }) {
  if (!v || v.state === 'done') return null;
  return <div className="l"><span className="lk">read</span><div className="dim"><AiTag title="the local model's read" /> {v.state === 'empty' ? 'no usable read' : v.text || 'the read waits for the local model'}</div></div>;
}
export function ReviewBlock({ r }) {
  if (!r) return null;
  return <div className="tp-ai">
    <div className="t"><AiTag title="the evening review, written by the local model" /><b>Evening review{r.dry ? ' · DRY' : ''}</b>
      <span className="m">{r.state === 'pending' ? 'waits for the local model' : `written ${r.at_label || ''}`} from the stored ticks, orders and posts · the model wrote only the tagged lines · nothing here acts</span></div>
    {arr(r.lines).map((x, i) => aiLine(x, i))}
    <AiState v={r} />
    {r.numbers && <div className="l"><span className="lk">numbers</span><div className="dim">{r.numbers}</div></div>}
  </div>;
}
export function GradeBlock({ g }) {
  if (!g) return null;
  return <div className="tp-ai">
    <div className="t"><AiTag title="the read line is the local model's; the grade is code" /><b>Call grade {g.grade}</b>
      <span className="m">from the channel's own record for calls like this · computed by code, worded by the model · never changes the plan{g.regraded ? ' · re-graded after the plan changed' : ''}</span></div>
    <div className="l"><span className="lk">record</span><div>{g.record || '—'}</div></div>
    <div className="l"><span className="lk">shape</span><div>{g.shape || '—'}</div></div>
    {g.market && <div className="l"><span className="lk">market</span><div>{g.market}</div></div>}
    {arr(g.read?.lines).map((x, i) => aiLine(x, i))}
    <AiState v={g.read} />
  </div>;
}
export function PreflightBlock({ p }) {
  if (!p) return null;
  const lines = arr(p.lines);
  return <div className="tp-ai">
    <div className="t"><AiTag title="the pre-flight read, written by the local model" /><b>Pre-flight read</b><span className="tp-sw" aria-label="switch on"><i />{p.switch}</span>
      <span className="m">{p.at_label}{isNum(p.ms) ? ` · ${(Number(p.ms) / 1000).toFixed(1)} s` : ''} · queued before arming · a mismatch is told, never blocks</span></div>
    {lines.map((x, i) => aiLine(x, i, x.label === 'flag' && p.flag ? 'warn' : x.label === 'plan vs post' ? (p.agrees === false ? 'warn' : '') : ''))}
    {lines.length > 0 && p.agrees != null && <div className="l"><span className="lk">verdict</span><div className={p.agrees ? 'up' : 'warn'}>{p.agrees ? 'agrees' : `differs${p.why ? ` · ${p.why}` : ''} · armed anyway`}</div></div>}
    <AiState v={p} />
  </div>;
}

// 📈 the market read: code tiles (trend, levels, pattern, volatility) always; the candle model's numbers only when the
// server's gate says cards (shadow: the chip only); the local model's sentences carry the AI tag; nothing here acts.
const MK_IDX = ['NIFTY', 'BANKNIFTY', 'SENSEX'];
function MkTile({ t }) {
  if (!t) return <div />;
  const bar = t.state === 'shown' && isNum(t.p_up);
  return <div title={[t.label, t.head, t.chg, t.sub].filter(Boolean).join(' · ')}>
    <div className="k">{t.label}</div>
    <div className={`h ${t.tone || ''}`}>{t.head}{t.chg && <span className={`c ${t.tone || ''}`}>{t.chg}</span>}</div>
    {bar && <div className="mk-pbar" style={{ '--p': `${Math.round(Number(t.p_up) * 100)}%` }} />}
    <div className="s">{t.sub}</div></div>;
}
function mkLine(x, i) {
  const v = x.verdict;
  const body = v && String(x.text).endsWith(v) ? String(x.text).slice(0, -v.length) : x.text;
  return <div key={i} className="l"><span className="lk">{x.label}</span><div className="mk-cl" title={x.text}><AiTag title="written by the local model · every figure checked against the numbers code gave it" /> {body}
    {v && String(x.text).endsWith(v) && <span className={`vd ${v.startsWith('supports') ? 'up' : v.startsWith('against') ? 'down' : 'w'}`}>{v}</span>}</div></div>;
}
function MkWords({ v, label = 'read' }) {
  if (!v || v.state === 'done' || v.state === 'none') return null;
  return <div className="l"><span className="lk">{label}</span><div className="dim mk-one"><AiTag title="the local model's words" /> {v.state === 'empty' ? 'no usable read' : v.text || 'the words wait for the local model'}</div></div>;
}
function MkFan({ f }) {
  const hist = arr(f?.history).map(Number).filter((x) => !Number.isNaN(x));
  const q10 = arr(f?.q10).map(Number), q50 = arr(f?.q50).map(Number), q90 = arr(f?.q90).map(Number);
  if (!q50.length) return null;
  const marks = [f.stop, f.t1].filter(isNum).map(Number);
  const all = [...hist, ...q10, ...q90, ...marks];
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const y = (v) => (64 - ((v - lo) / span) * 58).toFixed(1);
  const hx = (i) => (hist.length > 1 ? (i / (hist.length - 1)) * 200 : 200).toFixed(1);
  const fx = (i) => (200 + ((i + 1) / q50.length) * 200).toFixed(1);
  const last = isNum(f.last) ? Number(f.last) : hist[hist.length - 1];
  const band = (a, b) => `200,${y(last)} ${a.map((v, i) => `${fx(i)},${y(v)}`).join(' ')} ${[...b].reverse().map((v, i) => `${fx(b.length - 1 - i)},${y(v)}`).join(' ')}`;
  return <div className="mk-fan" title="the candle model's sampled paths: the middle 80 % (q10–q90) and the median, after the premium's own last hour">
    <svg viewBox="0 0 400 70" preserveAspectRatio="none" aria-label="paths fan">
      {hist.length > 1 && <polyline fill="none" stroke="var(--muted)" strokeWidth="1.2" points={hist.map((v, i) => `${hx(i)},${y(v)}`).join(' ')} />}
      <polygon fill="color-mix(in srgb, var(--ok) 16%, transparent)" points={band(q90, q10)} />
      <polyline fill="none" stroke="var(--ok)" strokeWidth="1.4" strokeDasharray="4 3" points={`200,${y(last)} ${q50.map((v, i) => `${fx(i)},${y(v)}`).join(' ')}`} />
      <line x1="200" y1="0" x2="200" y2="70" stroke="var(--border)" />
      {isNum(f.stop) && <line x1="0" x2="400" y1={y(Number(f.stop))} y2={y(Number(f.stop))} stroke="var(--bad)" strokeDasharray="3 3" />}
      {isNum(f.t1) && <line x1="0" x2="400" y1={y(Number(f.t1))} y2={y(Number(f.t1))} stroke="var(--ok)" strokeDasharray="3 3" />}
    </svg>
    {isNum(f.stop) && <span className="fl down" style={{ top: `${Math.max(0, Number(y(Number(f.stop))) - 12)}px` }}>stop {px(f.stop)}</span>}
    {isNum(f.t1) && <span className="fl up" style={{ top: `${Math.min(56, Number(y(Number(f.t1))) + 1)}px` }}>T1 {px(f.t1)}</span>}
    <span className="fl now">now {f.at_label} · next {f.horizon} min</span></div>;
}
export function MarketBlock({ m }) {
  if (!m) return null;
  const t = m.tiles;
  return <div className="tp-ai mk-card">
    <div className="t"><AiTag title="the tagged lines are the local model's; every number is code or the candle model (with its version)" /><b>Market read</b>
      <span className={`mk-shadow ${m.cards ? 'on' : ''}`} title="the candle model's numbers show only while its scorecard earns it (Settings › market)">{m.chip}</span>
      <span className="m">{m.at_label ? `${m.at_label} · ` : ''}code: trend, levels, patterns · model: next-15-min odds{m.cards ? '' : ' (stored, not shown)'} · the local model wrote only the tagged lines · nothing here acts</span></div>
    {t ? <div className="mk-tiles">{['index', 'levels', 'pattern', 'model'].map((k) => <MkTile key={k} t={t[k]} />)}</div> : <div className="mk-empty">{m.empty || 'no index candles yet'}</div>}
    {m.fan && <MkFan f={m.fan} />}
    {arr(m.lines).map(mkLine)}
    {t && <MkWords v={{ state: m.state, text: m.words_text }} />}
    {m.numbers && <div className="l"><span className="lk">numbers</span><div className="dim mk-one" title={m.numbers}>{m.numbers}</div></div>}
  </div>;
}
export function EntryTiming({ e }) {
  if (!e) return null;
  return <div className="tp-ai mk-entry">
    <div className="t"><AiTag title="the pullback line is the local model's; the odds line is code" /><b>Entry timing</b><span className="m">{e.note}</span></div>
    <div className="l"><span className="lk">odds</span><div className="mk-one" title={e.code}>{e.code}</div></div>
    {arr(e.lines).map(mkLine)}
    <MkWords v={{ state: e.state, text: e.text }} label="pullback" />
  </div>;
}
function MarketPanel() {
  const [idx, setIdx] = useState(() => { try { return MK_IDX.includes(localStorage.getItem('bp.mkt.idx')) ? localStorage.getItem('bp.mkt.idx') : 'NIFTY'; } catch { return 'NIFTY'; } });
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    let live = true;
    const load = () => api.getMarketNow(idx).then((r) => { if (live) { setD(r && typeof r === 'object' ? r : null); setErr(''); } }).catch((e) => { if (live) setErr(e.message); });
    load();
    const t = setInterval(load, 30000);
    return () => { live = false; clearInterval(t); };
  }, [idx]);
  const pick = (v) => { setIdx(v); try { localStorage.setItem('bp.mkt.idx', v); } catch { /* private mode */ } };
  const tl = d?.symbol === idx ? d?.tiles : null;
  const bd = d?.symbol === idx ? d?.backdrop : null;
  const src = `code: candles + levels · model: Kronos-mini (4 M, on this server)${d?.worker?.state ? ` · ${d.worker.state}` : ''} · words: the local model · every ${isNum(d?.interval_s) ? d.interval_s : 60} s${d?.login && d?.tiles ? ` · ${d.login}` : ''}`;
  return <Panel className="mk-panel" title={`Market now · ${idx}`} right={d?.refreshed ? `refreshed ${d.refreshed}` : null}>
    <div className="mk-body">
      <div className="mk-bar0"><Seg small opts={MK_IDX.map((x) => [x, x])} v={idx} set={pick} />
        <span className={`mk-shadow ${d?.cards ? 'on' : ''}`} title={d?.gate || ''}>{d?.chip || 'candle model'}</span>
        <span className="mk-src" title={src}>{src}</span></div>
      {tl ? <div className="mk-tiles">{['trend', 'levels', 'volatility', 'model'].map((k) => <MkTile key={k} t={tl[k]} />)}</div>
        : <div className="mk-empty">{d?.off || d?.empty || d?.login || err || 'Loading…'}</div>}
      <div className="tp-ai">
        <div className="t"><AiTag title="the read and for-calls lines are the local model's; the numbers line is code" /><b>Backdrop</b><span className="m">three lines · nothing here acts</span></div>
        {arr(bd?.lines).map(mkLine)}
        {(!bd || bd.state !== 'done') && <div className="l"><span className="lk">read</span><div className="dim mk-one"><AiTag title="the local model's words" /> {bd?.state === 'empty' ? 'no usable read' : bd?.text || (tl ? 'the words wait for the local model' : 'no candles to read yet')}</div></div>}
        <div className="l"><span className="lk">numbers</span><div className="dim mk-one" title={bd?.numbers || ''}>{bd?.numbers || '—'}</div></div>
      </div>
    </div>
  </Panel>;
}

// 📊 the weekly channel profile (Research › Tips › Track): the four boxes with bars (widths from the numbers) and the
// four [AI] lines; a week picker for the last 8 weeks; "Write now" one per hour.
function Bars({ rows, right }) {
  const r = arr(rows);
  if (!r.length) return <div className="dim">none</div>;
  return <div className="tp-bars">{r.map((b) => <div key={b.label} title={`${b.label} · ${right(b)}`}><span className="bl">{b.label}</span>
    <span className="bt"><i className={b.tone === 'bad' ? 'bad' : ''} style={{ width: `${Math.max(0, Math.min(100, Number(b.w) || 0))}%` }} /></span><span className="br">{right(b)}</span></div>)}</div>;
}
function WeeklyPanel({ p, onWrite, busy, next }) {
  const n = p?.numbers || {};
  const tw = n.this_week || {}, f4 = n.four_weeks || {};
  const ai = p?.ai || {};
  const mins = (v) => (isNum(v) ? `${px(v)} min` : '—');
  return <Panel className="tp-week" title={`📊 Channel profile · ${p?.label || ''}`} meta={p?.channel}
    right={<span className="tp-wr"><span className="ell">{p?.written ? `${p.written} · from the record, ticks and orders · the model wrote only the tagged lines` : 'not written yet · Sunday 7:05 PM'}</span>
      {onWrite && <button type="button" className="btn ghost small" disabled={busy || !!next} title={next ? `one Write now per hour · next at ${next}` : 'write the profile now (one per hour)'} onClick={onWrite}>Write now</button>}</span>}>
    <div className="tp-wk">
      <div className="box"><b className="h">this week</b>
        <div><b>{plural(px(tw.calls), 'call')}</b> · T1 first {px(tw.t1_first)} · stop first {px(tw.stop_first)} · no fill {px(tw.no_fill)} · avg <b className={tone(tw.avg_r)}>{isNum(tw.avg_r) ? `${avgR(tw.avg_r)} R` : '—'}</b></div>
        <div>you took <b>{px(tw.took)}</b>{tw.dry ? ` (${px(tw.dry)} dry)` : ''} · yours <b className={tone(tw.yours)}>{money(tw.yours)}</b> · all calls 1 lot <b className={tone(tw.all_1lot)}>{money(tw.all_1lot)}</b></div>
        <div className="dim">4 weeks: {px(f4.calls)} calls · T1 first {isNum(f4.t1_pct) ? `${px(f4.t1_pct)} %` : '—'} · avg {isNum(f4.avg_r) ? `${avgR(f4.avg_r)} R` : '—'}</div></div>
      <div className="box"><b className="h">by index</b><Bars rows={n.by_index} right={(b) => `${px(b.n)} · ${px(b.won)} won`} /></div>
      <div className="box"><b className="h">by hour posted</b><Bars rows={n.by_hour} right={(b) => `${px(b.n)} · ${px(b.won)} won`} /></div>
      <div className="box"><b className="h">how calls end</b><Bars rows={n.ends} right={(b) => px(b.n)} />
        <div className="dim">median to T1 {mins(n.median_t1_min)} · to the stop {mins(n.median_stop_min)}</div></div>
    </div>
    <div className="tp-ai">
      <div className="t"><AiTag title="written by the local model from the numbers above" /><b>What the week says</b>
        <span className="m">{ai.state === 'done' ? `written ${ai.at_label || ''}` : ai.state === 'pending' ? 'waits for the local model' : ai.state === 'empty' ? 'no usable read' : 'not written yet'}</span></div>
      {arr(ai.lines).map((x, i) => aiLine(x, i))}
      {ai.state !== 'done' && <div className="l"><span className="lk">read</span><div className="dim">{ai.state === 'empty' ? <><AiTag title="the local model's read" /> no usable read</> : ai.text || 'not written yet'}</div></div>}
      <div className="l"><span className="lk">numbers</span><div className="dim">every figure above is from code · the model only wrote the sentences{arr(n.hand_exits).length ? ` · your hand exits: yours ${money(arr(n.hand_exits).reduce((a, x) => a + (Number(x.yours) || 0), 0))} · the watch ${money(arr(n.hand_exits).reduce((a, x) => a + (Number(x.watch) || 0), 0))}` : ''} · follow-ups {px(n.followups?.count)} · AI second opinions {px(n.followups?.read)} · agreed {px(n.followups?.agree)}{p?.model ? ` · ${p.model}` : ''}</div></div>
    </div>
  </Panel>;
}
function TrackTab({ record, channel, books }) {
  const [w, setW] = useState(null);
  const [week, setWeek] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const load = (wk) => api.getTipsWeekly(wk).then((r) => setW(r && typeof r === 'object' ? r : null)).catch((e) => setMsg(`⚠ ${e.message}`));
  useEffect(() => { load(week); }, [week]);
  const write = () => {
    if (!window.confirm('Write the channel profile for this week now?\n\nThe local model writes 4 sentences from numbers code computed. One per hour. Nothing here acts on an order.')) return;
    setBusy(true); setMsg('');
    api.tipsWeeklyWrite(w?.week).then(() => { setMsg('queued for the local model'); load(week); }).catch((e) => setMsg(`⚠ ${e.message}`)).finally(() => setBusy(false));
  };
  const panels = arr(w?.panels);
  return <>
    <div className="tp-trackbar"><label className="small">week <select className="tp-in" aria-label="week" value={week || w?.week || ''} onChange={(e) => setWeek(e.target.value)}>
      {arr(w?.weeks).map((x) => <option key={x.week} value={x.week}>{x.label}</option>)}</select></label>
      {msg && <span className={msg.startsWith('⚠') ? 'down small' : 'up small'}>{msg}</span>}
      {w && !w.on && <span className="small dim">the weekly profile is off · Settings › tips · ai_weekly</span>}</div>
    {panels.length ? panels.map((p) => <WeeklyPanel key={p.channel} p={p} onWrite={w?.on ? write : null} busy={busy} next={w?.write_next} />)
      : <Panel title="📊 Channel profile"><p className="muted small rempty">{w ? 'No calls in this week.' : 'Loading…'}</p></Panel>}
    {books}
    <KiteDays record={record} show={!!books} />
    <Record record={record} channel={channel} />
  </>;
}

// ⚡ Track › the Kite ledger by day (real fills only; a dry run is never part of the record).
function KiteDays({ record, show }) {
  const days = arr(record?.kite_days);
  if (!days.length && !show) return null;
  const rows = days.map((x, i) => ({ key: `d${i}`, cells: [x.label || x.day, px(x.n), <span className={tone(x.net)}>{money(x.net)}</span>, px(x.won)] }));
  return <Panel title="Kite trades by day" meta={`${record?.days ?? 30} d · real fills only`}>
    <FixedTable cols={[{ h: 'Day', ell: true }, { h: 'Trades', w: 70, n: true }, { h: 'Net', w: 100, n: true }, { h: 'Won', w: 60, n: true }]}
      rows={rows} empty="No Kite trade closed in these days." />
  </Panel>;
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
      { h: 'Type', w: 168, lo: true }, { h: 'Price', w: 80, n: true, lo: true }, { h: 'Status', w: 128, ell: true }, { h: 'Order id', w: 150, lo: true }, { h: 'By', w: 52, lo: true }]}
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
  const mult = Number(h?.sizing?.mult ?? 1) || 1;                 // 📏 lots ×N: the default shown and taken is multiplied; typed lots are literal
  const n = isNum(lots) && Number(lots) > 0 ? Number(lots) : (stated ?? (isNum(defLots) ? Number(defLots) : 1)) * mult;
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
      {h?.stop_tile?.chip && <Tag c={h.stop_tile.chip_tone === 'b' ? 'a' : h.stop_tile.chip_tone || ''} title={h.stop_tile.sub || undefined}>{h.stop_tile.chip}</Tag>}
      <GradeChip g={h?.ai4?.grade} />
      {canSym && <select className="tp-in" aria-label="set the index" defaultValue="" onChange={setSym} style={{ fontSize: 11, padding: '1px 4px' }} title="the post named no index: pick it after looking at the channel's screenshot">
        <option value="">{h?.symbol ? 'change index…' : 'set index…'}</option>{['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'].map((x) => <option key={x} value={x}>{x}</option>)}</select>}
      <span className="who">{[h?.channel, when(h?.created ?? h?.posted_at)].filter(Boolean).join(' · ')}</span></div>
    <PnlStrip s={h?.pnl_strip} />
    <ReviewBlock r={h?.ai4?.review} />
    {raw ? <div className="raw">{raw}</div> : <div className="raw dim">(no text)</div>}
    <Decoded h={h} onDone={onDone} />
    <DistBar d={h?.dist} />
    <ShotLine h={h} busy={busy} onUse={useShot} onKeep={keepText} />
    <TargetBar h={h} />
    <SaidNext chips={h?.said_next} />
    <Timeline id={id} defOpen={h?.log_open} events={arr(h?.kite?.log).length ? [...arr(h?.events), ...arr(h.kite.log)].sort((a, b) => String(a?.t || '').localeCompare(String(b?.t || ''))) : h?.events} />
    {kinfo && kinfo.mode !== 'off' && h?.kite && (open || (h.kite.state && h.kite.state !== 'none')) ? <KiteLegs h={h} kinfo={kinfo} onDone={onDone} /> : <FactsRow f={h?.facts} />}
    {/* the [AI] blocks sit below the trade: plan, bar, chips, log and legs come first */}
    <GradeBlock g={h?.ai4?.grade} />
    <PreflightBlock p={h?.ai4?.preflight} />
    <MarketBlock m={h?.market} />
    <EntryTiming e={h?.market?.entry} />
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
            : h?.taken ? `${money(h?.pnl)} · closed${h?.exit_px != null ? ` at ${px(h.exit_px)}` : ''}` : h?.kite?.footer ? `${h.kite.state === 'closed' ? `closed on Kite${h.kite.reason ? ` · ${h.kite.reason}` : ''} · ${money(h.kite.realised)} · ` : ''}${h.kite.footer}` : "scored for the channel's record only · you did not take it"}</span>
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

// ⚡ The Kite ledger beside the paper book: one row per closed Kite hint (the app's own orders only), charges per order
// like the paper book. A dry run's book says so and is never summed as real. At phone width the table scrolls sideways
// inside the panel, never the page.
const BY = { click: 'you', dry: 'you (dry)', auto: 'auto', app: 'app' };
function KiteBook({ kb }) {
  const b = kb || {};
  const dry = !!b.dry;
  const tr = arr(b.trades);
  const T = b.totals || {};
  const endTag = (w) => <Tag c={w === 'stop' ? 'bad' : /^target/.test(String(w || '')) ? 'ok' : /time stop/.test(String(w || '')) ? 'w' : ''}>{w || 'closed'}</Tag>;
  const rows = tr.map((x, i) => ({ key: `k${i}`, cells: [x.when_label || '—', <><b className="mono">#{x.hint_id}</b> {String(x.title || '').replace(/^(BUY|SELL) /, '')}{x.dry ? <> <Tag c="w">dry</Tag></> : ''}</>,
    px(x.lots), x.entry_exit || '—', money(x.gross), isNum(x.charges) ? money(-Number(x.charges)) : '—', <span className={tone(x.net)}>{money(x.net)}</span>, endTag(x.why), BY[x.by] || x.by || '—'] }));
  if (tr.length) rows.push({ key: 'tot', cls: 'tp-tot', cells: [`${plural(px(T.n ?? tr.length), 'trade')} · ${px(T.won ?? 0)} won`, '', '', '', money(T.gross), isNum(T.charges) ? money(-Number(T.charges)) : '—', <span className={tone(T.net)}>{money(T.net)}</span>, '', ''] });
  const since = b.since ? dayLabel(b.since).split(' ').slice(1).join(' ') : '';
  return <Panel title={<>{dry ? 'Tips · dry run book' : 'Tips · Kite book'}{dry ? <> <Tag c="w">dry</Tag></> : ''}</>} className="tp-kbook"
    meta={`${px(b.open ?? 0)} open · ${px(b.closed ?? tr.length)} closed · ${dry ? 'dry run fills · never summed as real' : 'real fills only'} · charges per order`}
    right={<span>today <b className={`mono ${tone(b.today)}`}>{money(b.today ?? 0)}</b>{since ? <> · since {since} <b className={`mono ${tone(b.net)}`}>{money(b.net ?? 0)}</b></> : ''}</span>}>
    <div className="kscroll">
      <FixedTable cols={[{ h: 'When', w: 150 }, { h: 'Hint', ell: true }, { h: 'Lots', w: 50, n: true }, { h: 'Entry → exit', w: 150, n: true }, { h: 'Gross', w: 84, n: true },
        { h: 'Charges', w: 78, n: true }, { h: 'Net', w: 88, n: true }, { h: 'Ended', w: 130 }, { h: 'By', w: 60 }]}
        rows={rows} empty="No Kite trade closed yet. Every one starts from a card, through the app's own orders." />
    </div>
    <div className="rnote">one row per closed hint · a partial booking shows every exit fill · backfilled once from the app's own order rows · never deleted · the Track tab lists the same rows by day</div>
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
  const [view, setView0] = useState(() => (/^#\/?research\/tips\/track/.test(window.location.hash) ? 'track' : 'calls'));   // 📊 Calls · Track
  const setView = (v) => { setView0(v); try { window.history.replaceState(null, '', `#/research/tips${v === 'track' ? '/track' : ''}`); } catch { /* ignore */ } };
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
  const relayN = String(settings.relay_to || '').split(/[\n;]+/).filter((x) => x.trim()).length;
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
  const K = C.kite || {};                                // ⚡ the Kite ledger's card (mode kite / dry; off = the paper card as before)
  const kOn = K.mode === 'kite' || K.mode === 'dry';
  const kDry = K.mode === 'dry';
  const showBooks = kOn || arr(data.kite_book?.trades).length > 0;
  const books = showBooks ? <div className="tp-books"><KiteBook kb={data.kite_book} /><Book book={book} /></div> : null;
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
      {settings.relay && relayN > 0 && <Chip title="every new post forwarded as-is from your Telegram account (Settings › tips)">↗ relay on · {relayN} {relayN === 1 ? 'person' : 'people'}{isNum(reader.relayed_today) ? ` · ${px(reader.relayed_today)} sent today` : ''}</Chip>}
    </Head>
    <KiteBar k={{ ...(data.kite || {}), ...(kd || {}) }} onDone={reload} toOrders={() => document.getElementById('tp-kite-orders')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
      testing={ktest.busy} onTest={runTest} />
    {(data.kite?.mode || 'off') !== 'off' && (ktest.err || (kd?.test?.at && dayKey(toD(kd.test.at)) === dayKey(new Date()) && kd.test.at !== ktest.hidden)) &&
      <KiteTest t={ktest.err ? null : kd?.test} err={ktest.err} onClose={() => setKtest({ busy: false, err: '', hidden: kd?.test?.at || null })} />}
    {data.enabled === false && <div className="rbanner warn tp-banner"><span>💡 Tips is <b>off</b>: nothing is read or pushed. {reader.state === 'logged_out' ? 'Log the reader in below first, then turn it on.' : ''}</span>
      <button type="button" className="btn small" onClick={enable}>Turn on…</button></div>}
    <div className="tp-tabs"><Seg opts={[['calls', 'Calls'], ['track', 'Track', 'the weekly channel profile and the record']]} v={view} set={setView} /></div>
    {view === 'track' ? <TrackTab record={data.record} channel={chans.split(' · ')[0]} books={books} /> : <>
    <MarketPanel />
    <Cards n={4}>
      <Card unit="n" label="Open hints" value={`${nAct} active · ${nNew} new`} num={nAct * 100 + nNew} live={openH.length > 0}
        sub={O.line || firstOpen || 'none open'} fact={<>today <b>{px(O.today?.hints ?? O.today_hints ?? 0)}</b> hints · {px(O.today?.posts ?? O.today_posts ?? 0)} posts · {px(O.today?.chatter ?? O.today_chatter ?? 0)} chatter</>} />
      {kOn ? <Card label={kDry ? <>Tips · dry run <Tag c="w">dry</Tag></> : 'Tips · Kite'} value={money(K.net ?? 0)} num={K.net} k={tone(K.net)}
        sub={<><span className={tone(K.today)}>{money(K.today ?? 0)}</span> today · {plural(px(K.lots_open ?? 0), 'lot')} open · {plural(px(K.trades_today ?? 0), 'trade')}</>}
        fact={<>{K.since ? `since ${dayLabel(K.since).split(' ').slice(1).join(' ')} · ` : ''}{px(K.taken ?? 0)} taken · {px(K.won ?? 0)} won · paper book {isNum(equity) ? inr(Number(equity)) : '—'}{isNum(equity) && isNum(capital) ? <> · <span className={tone(Number(equity) - Number(capital))}>{money(Number(equity) - Number(capital))}</span></> : ''}</>} />
      : <Card label="Tips · paper" value={isNum(equity) ? inr(Number(equity)) : '—'} num={equity} k={isNum(equity) && isNum(capital) ? tone(Number(equity) - Number(capital)) : ''}
        sub={<><span className={tone(B.today)}>{money(B.today ?? 0)}</span> today · {plural(px(B.lots_open ?? B.lots ?? 0), 'lot')} open</>}
        fact={<>{B.since ? `since ${dayLabel(B.since).split(' ').slice(1).join(' ')} · ` : ''}{px(B.taken ?? 0)} taken · {px(B.won ?? 0)} won</>} />}
      <Card unit="n" label="Channel · 30 d" value={plural(px(H.hints ?? 0), 'hint')} num={H.hints}
        sub={`T1 first ${px(H.t1_first ?? 0)} · stop first ${px(H.stop_first ?? 0)} · no fill ${px(H.no_fill ?? 0)}`}
        fact={<>avg <b className={tone(H.avg_r)}>{avgR(H.avg_r)} R</b>{H.best_time || H.best_hour ? <> · best {H.best_time || hm12(H.best_hour)}</> : ''}</>} />
      <Card label="Your takes vs all" value={isNum(Y.you) || isNum(Y.all) ? (phone ? money(Y.you ?? 0) : `${money(Y.you ?? 0)} vs ${money(Y.all ?? 0)}`) : '—'} num={Y.you} k={tone(Y.you)}
        sub={`${phone && isNum(Y.all) ? `all ${money(Y.all)} · ` : ''}you took ${px(Y.took ?? 0)} of ${px(Y.of ?? H.hints ?? 0)}${Number(Y.on_kite) > 0 ? ` · on Kite ${px(Y.on_kite)}${Number(Y.paper) > 0 ? ` · paper ${px(Y.paper)}` : ''}` : ''}`} fact={isNum(Y.skips) ? `skips were right ${px(Y.skips_right ?? 0)} of ${px(Y.skips)}` : 'every hint scored, taken or not'} />
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
        {!showBooks && <Book book={book} />}
      </div>
    </div>
    {books}
    {((data.kite?.mode || 'off') !== 'off' || arr(kd?.orders).length > 0) && <KiteOrders kd={kd} onDone={reload} />}
    {((data.kite?.mode || 'off') !== 'off' || arr(kd?.orders).length > 0) && <KiteDayCheck kd={kd} onDone={reload} />}
    </>}
    {dlg === 'channels' && <ChannelsDialog current={chans} onClose={() => setDlg(null)} onSaved={() => { setMsg('channels saved'); reload(); }} />}
    {dlg === 'import' && <ImportDialog onClose={() => setDlg(null)} onDone={reload} />}
    {phone && <span className="tp-phone-pad" />}
  </div>;
}
