import React, { useEffect, useMemo, useRef, useState } from 'react';
import * as api from '../services/api';
import { useLiveOr } from '../services/live';
import { clock, ampm } from '../timefmt';
import { inr } from '../fmt';
import { useIsPhone } from '../useMedia';
import { Card, Cards, Panel, FixedTable, Tag, AiTag, Chip, Head } from './ui2';

// 💡 Research › Tips: a Telegram channel's calls caught, filtered, decoded and scored. NOT linked to any strategy:
// every take is the owner's, each through a confirm, into a separate Tips PAPER book (nothing is sent to the broker).
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
export const hintName = (h) => h?.title || [h?.side, h?.symbol, h?.strike, h?.opt_type].filter((x) => x != null && x !== '').join(' ') || h?.tradingsymbol || 'hint';
const isAi = (h, ...fields) => fields.some((f) => h?.decoded?.[f] === 'ai');

export function entryText(e, ltp, status) {
  if (!e || typeof e !== 'object') return '—';
  const lo = px(e.lo), hi = px(e.hi);
  const base = e.mode === 'zone' ? `zone ${lo}–${hi}` : e.mode === 'above' ? `above ${lo}` : e.mode === 'below' ? `below ${lo}`
    : e.mode === 'market' ? 'at market' : e.mode === 'at' ? `at ${lo}` : isNum(e.lo) ? `at ${lo}` : '—';
  return (status === 'new' || status === 'waiting') && isNum(ltp) ? `${base} · now ${px(ltp)}` : base;
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
const SRC = { channel: '📣', owner: '👤', code: '⚙', ai: '🤖' };

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
    ['entry', entryText(h?.entry, h?.ltp, h?.status), ['entry']],
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

// One hint: the raw post (kept as written), the decoded grid, the target bar, the timeline, the actions (each confirms).
function HintCard({ h, defLots, onDone }) {
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
  const resume = () => { if (window.confirm(`Resume #${id} · ${name}?\n\nThe entry rule is watched again.`)) run(api.tipsResume(id), 'resumed'); };
  const raw = h?.text ?? h?.raw ?? '';
  const live = open && h?.taken && isNum(h?.ltp) ? `live ${px(h.ltp)}${isNum(h?.pnl) ? ` · ${money(h.pnl)}` : ''}` : '';
  return <div className={`tp-hint ${h?.status === 'active' ? 'active' : ''} ${open ? '' : 'closed'}`} data-id={id}>
    <div className="h"><span className="id">#{id ?? '?'}</span><b className="nm" title={name}>{name}</b>{isAi(h, 'symbol', 'strike', 'opt_type') && <AiTag title="filled by the local model from the post's own text" />}
      {statusTag(h)}{h?.tracked === false && <Tag c="bad" title="no quotes for this instrument: it is not scored">{h.untracked_why || 'untracked · log in'}</Tag>}
      <span className="who">{[h?.channel, when(h?.created ?? h?.posted_at)].filter(Boolean).join(' · ')}</span></div>
    {raw ? <div className="raw">{raw}</div> : <div className="raw dim">(no text)</div>}
    <Decoded h={h} />
    <TargetBar h={h} />
    <Timeline events={h?.events} />
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
  return <Tag>{k || 'post'}</Tag>;
}
function Feed({ feed, reader }) {
  const [all, setAll] = useState(false);
  const posts = arr(feed);
  const quiet = posts.filter((p) => QUIET.includes(p?.kind)).length;
  const shown = all ? posts : posts.filter((p) => !QUIET.includes(p?.kind));
  const src = reader?.state === 'live' ? 'your account' : reader?.state === 'preview' ? 'public preview' : reader?.state === 'down' ? 'reader down' : 'not logged in';
  const lag = isNum(reader?.lag_ms) ? ` · ${Number(reader.lag_ms) < 1000 ? `${Math.round(reader.lag_ms)} ms` : `${Math.round(reader.lag_ms / 1000)} s`}` : '';
  return <Panel title="Channel feed" meta={reader?.state ? String(reader.state).replace('_', ' ') : null} right={<span>{src}{lag}</span>}>
    {!shown.length && <p className="muted small rempty">{posts.length ? 'Only chatter so far.' : 'No posts yet: they appear here about a second after the channel posts.'}</p>}
    <div className="tp-feed">{shown.map((p, i) => {
      const q = QUIET.includes(p?.kind);
      const text = p?.kind === 'image' && !p?.text ? '📷 image · ignored' : p?.text || (p?.has_media ? '📷 media' : '');
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
  const [v, setV] = useState(current || '');
  const [msg, setMsg] = useState('');
  const save = () => {
    const list = v.split(/[\s,]+/).map((x) => x.replace(/^@|^https?:\/\/t\.me\/(s\/)?/i, '').trim()).filter(Boolean);
    if (!list.length) { setMsg('⚠ at least one channel'); return; }
    if (!window.confirm(`Read these channels?\n\n${list.join('\n')}\n\nThe reader follows them from the next post. Nothing is traded.`)) return;
    api.putTipsSettings({ channels: list.join(',') }).then(() => { onSaved && onSaved(); onClose(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  return <Dialog title="Channels" onClose={onClose}>
    <p className="small muted" style={{ marginTop: 0 }}>Comma-separated channel names (any channel your account is a member of; public ones also work through the preview).</p>
    <input className="tp-in mono" value={v} onChange={(e) => setV(e.target.value)} placeholder="aakankshatrading, another_channel" autoFocus />
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
  return Array.isArray(c) ? c.join(', ') : typeof c === 'string' ? c.split(',').map((x) => x.trim()).filter(Boolean).join(', ') : '';
};

export default function TipsPage() {
  const [d, reload, err] = useLiveOr('tips', api.getTips, 5000);
  const phone = useIsPhone();
  const [dlg, setDlg] = useState(null);
  const [tryOpen, setTryOpen] = useState(false);
  const [older, setOlder] = useState(false);
  const [msg, setMsg] = useState('');
  const topRef = useRef(null);
  const data = d && typeof d === 'object' && !d.error ? d : null;
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
  const enable = () => {
    if (!window.confirm('Turn Tips on?\n\nThe reader catches the channel posts, decodes them and pushes hints to Telegram. Nothing is ever taken without you, and nothing is sent to the broker.')) return;
    api.putTipsSettings({ enabled: true }).then(() => { setMsg('Tips is on'); reload(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  return <div className="rd tips" ref={topRef}>
    <Head title={`💡 Tips${chans ? ` · ${chans}` : ''}`} right={<>{msg && <span className={msg.startsWith('⚠') ? 'down small' : 'up small'}>{msg}</span>}
      <button type="button" className="btn ghost small" onClick={() => setTryOpen(!tryOpen)}>Try a post</button>
      <button type="button" className="btn ghost small" onClick={() => setDlg('channels')}>Channels…</button>
      <button type="button" className="btn ghost small" onClick={() => setDlg('import')}>Import export</button></>}>
      <Chip dot={st[0]} title={reader.account_masked ? `account ${reader.account_masked}` : undefined}>{st[1]}{reader.last_post_at ? <> · last post {when(reader.last_post_at)}</> : ''}</Chip>
      {isNum(capital) && <Chip>paper book <b>{inr(Number(capital))}</b> · separate</Chip>}
      <Chip>not linked to any strategy · you decide every take</Chip>
    </Head>
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
        {shownHints.map((h) => <HintCard key={h.id} h={h} defLots={defLots} onDone={reload} />)}
        {hints.length > shownHints.length && <button type="button" className="btn ghost small tp-more" onClick={() => setOlder(true)}>show {hints.length - shownHints.length} older</button>}
      </div>
      <div className="tp-col">
        <Feed feed={data.feed} reader={reader} />
        <Record record={data.record} channel={chans.split(',')[0]} />
        <Book book={book} />
      </div>
    </div>
    {dlg === 'channels' && <ChannelsDialog current={chans} onClose={() => setDlg(null)} onSaved={() => { setMsg('channels saved'); reload(); }} />}
    {dlg === 'import' && <ImportDialog onClose={() => setDlg(null)} onDone={reload} />}
    {phone && <span className="tp-phone-pad" />}
  </div>;
}
