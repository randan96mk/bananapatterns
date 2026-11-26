import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useLiveOr } from '../services/live';
import { inr, cls } from '../fmt';
import { Card, Seg, Switch, Tag, AiTag } from './ui2';
import { KMODES, modeConfirm, killConfirm, kiteTexts, kiteMoney } from './tipsKite';

// 🏠 Home · "💡 Tips · Kite": two cards (Tips · Kite · today, Channel · 30 d) and the block under them: the strip (mode,
// kill switch, caps, window, the reader, Open Tips), one row per working Kite hint (open: Trail SL… / Exit now…; armed:
// Send now… / Cancel: the same API calls and the same confirms as Research › Tips), today's calls as chips with their
// outcome, the order-check lines, the channel's 30-day record. Data: GET /api/home/tips (stream topic `home_tips`),
// every 5 s while anything is armed or open, else every 30 s. Nothing here calls Kite or a quote by itself.

const isNum = (v) => v != null && v !== '' && !Number.isNaN(Number(v));
const R = (v) => (isNum(v) ? `${Number(v) > 0 ? '+' : Number(v) < 0 ? '−' : ''}${Math.abs(Number(v)).toFixed(1).replace(/\.0$/, '')} R` : '—');
const ids = (xs) => { const t = xs.map((x) => `#${x}`); return t.length > 1 ? `${t.slice(0, -1).join(', ')} and ${t[t.length - 1]}` : t[0] || ''; };
const READER = { live: 'up', preview: 'warn' };
// 🧭 the card's state word (the same badge as Research › Tips and the Telegram line); the old tag when the payload has none
const KINDS = ['live', 'wait', 'won', 'lost', 'off'];
function StateWord({ s, tag, c, title }) {
  if (s && s.word) {
    const k = KINDS.includes(s.kind) ? s.kind : 'off';
    return <span className={`tp-state sm ${k}`} title={[s.why, title].filter(Boolean).join(' · ') || s.word}>{k === 'live' && <i className="pd" aria-hidden="true">●</i>}{s.word}</span>;
  }
  return <Tag c={c} title={title}>{tag}</Tag>;
}

export function useHomeTips() {
  const [ms, setMs] = useState(30000);
  const [d, reload] = useLiveOr('home_tips', api.getHomeTips, ms);
  const live = !!(d && d.live);
  useEffect(() => { setMs(live ? 5000 : 30000); }, [live]);
  const ok = d && !d.error && typeof d.mode === 'string' && (d.enabled || d.mode !== 'off');
  return [ok ? d : null, reload];
}

export function TipsKiteCards({ d, onGo }) {
  const off = d.mode === 'off';
  const c = d.card || {}, h = d.channel30 || {};
  return <>
    <Card label={<>Tips <span className="muted">· Kite · today</span>{d.kill && !off && <span className="down"> · killed</span>}</>} value={off ? 'off' : kiteMoney(c.pnl ?? 0)} num={c.pnl ?? 0}
      k={off ? 'muted' : cls(c.pnl)} live={!!d.live} sub={c.line} title="today's realised + open Kite P&L of the Tips · Research › Tips" onClick={() => onGo('tips')} />
    <Card unit="n" label="Channel · 30 d" value={`${h.hints ?? 0} hint${h.hints === 1 ? '' : 's'}`} num={h.hints ?? 0}
      sub={`T1 first ${h.t1_first ?? 0} · stop first ${h.stop_first ?? 0} · avg ${R(h.avg_r)}`} title="the channel's calls of the last 30 days, scored by code" onClick={() => onGo('tips')} />
  </>;
}

// "now 217 · stop 175 is −19 %": the "now 217" bold, green / red against the fill (neutral before the fill).
function Facts({ r }) {
  const f = String(r.facts || '');
  const m = /^now ([^·]+?)(?= ·|$)/.exec(f);
  if (!m) return f;
  const tone = r.now?.tone || '';
  return <><b className={`nowv ${tone}`} title={r.now?.sub || undefined}>now {m[1]}</b>{f.slice(m[0].length)}</>;
}

function Row({ r, reload, setMsg }) {
  const [busy, setBusy] = useState(false);
  const [trail, setTrail] = useState(null);           // null = closed, else the field's text
  const T = kiteTexts(r, { product: r.product });
  const run = (action, body, text, okText) => {
    if (!window.confirm(text)) return;
    setBusy(true); setMsg('');
    api.tipsKite(r.id, action, body).then(() => { setMsg(okText); setTrail(null); reload(); }).catch((e) => setMsg(`⚠ ${e.message}`)).finally(() => setBusy(false));
  };
  const doTrail = () => {
    const raw = String(trail || '').trim();
    const to = /^cost$/i.test(raw) ? 'cost' : isNum(raw) && Number(raw) > 0 ? Number(raw) : null;
    if (to == null) { setMsg('⚠ a price, or "cost"'); return; }
    run('modify_stop', { to }, T.moveStop(to), `#${r.id} stop moved to ${to}`);
  };
  const b = r.buttons || [];
  return <div className="hk-row">
    <div className="l"><span className="id">#{r.id}</span><b>{r.title}</b><StateWord s={r.state_word || (r.state && r.state.word ? r.state : null)} tag={r.tag} c={r.tag_c} />{r.grade && <Tag c={r.grade.c} title="call grade by code · informational: auto ignores it">grade {r.grade.g}</Tag>}
      {r.pnl != null && <span className={`pnl ${cls(r.pnl)}`}>{kiteMoney(r.pnl)}</span>}{r.facts ? <span className="f"> · <Facts r={r} /></span> : null}</div>
    <div className="m" title={[r.detail, r.ai ? `[AI] ${r.ai}` : ''].filter(Boolean).join(' · ')}>{r.detail}{r.ai && <> · <AiTag title="the AI read of these orders · every figure checked by code" /> {r.ai}</>}</div>
    <div className="a">
      {b.includes('trail') && (trail == null ? <button type="button" className="btn ghost small" disabled={busy} onClick={() => setTrail('')}>Trail SL…</button>
        : <span className="lotf"><input aria-label="trail the Kite stop to" placeholder="price or cost" value={trail} onChange={(e) => setTrail(e.target.value.slice(0, 12))}
          onKeyDown={(e) => { if (e.key === 'Enter') doTrail(); if (e.key === 'Escape') setTrail(null); }} />
          <button type="button" className="btn small" disabled={busy} onClick={doTrail}>Trail</button>
          <button type="button" className="btn ghost small" onClick={() => setTrail(null)} aria-label="Close">✕</button></span>)}
      {b.includes('exit') && r.state === 'entered' && <button type="button" className="btn ghost small tp-danger" disabled={busy} onClick={() => run('exit', {}, T.exit, `exit sent to ${T.to}`)}>Exit now…</button>}
      {b.includes('send_now') && <button type="button" className="btn ghost small" disabled={busy || !r.send_ok}
        title={r.send_ok ? "skip the channel's activation and send the entry now" : 'the entry already rests on Kite: it fills on the level'}
        onClick={() => run('send_now', {}, T.sendNow, `entry sent to ${T.to}`)}>Send now…</button>}
      {b.includes('cancel') && <button type="button" className="btn ghost small" disabled={busy} onClick={() => run('cancel', { leg: 'entry' }, T.cancel('entry'), 'entry cancelled')}>Cancel</button>}
      {b.includes('exit') && r.state !== 'entered' && !b.includes('cancel') && <button type="button" className="btn ghost small" disabled={busy} onClick={() => run('exit', {}, T.exit, `exit sent to ${T.to}`)}>Cancel</button>}
    </div>
  </div>;
}

export function TipsKiteBlock({ d, onGo, reload }) {
  const [msg, setMsg] = useState('');
  const mode = d.mode || 'off';
  const off = mode === 'off';
  const caps = d.caps || {}, rec = d.record || {}, ck = d.checks || {}, rd = d.reader || {};
  const setMode = (m) => {
    if (m === mode || !window.confirm(modeConfirm(m))) return;
    api.putTipsSettings({ kite_mode: m }).then(() => { setMsg(''); reload(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  const setKill = (on) => {
    if (!window.confirm(killConfirm(on))) return;
    api.tipsKiteKill(on).then((r) => { setMsg(on ? `kill switch on · ${r?.cancelled ?? 0} cancelled · ${r?.exits ?? 0} exits` : ''); reload(); }).catch((e) => setMsg(`⚠ ${e.message}`));
  };
  const rows = Array.isArray(d.rows) ? d.rows : [], today = Array.isArray(d.today) ? d.today : [], items = Array.isArray(ck.items) ? ck.items : [];
  const clear = (ck.clear || []).filter((x) => !items.some((y) => y.id === x));
  return <div className={`rpanel hk ${off ? 'off' : ''} ${d.kill ? 'killed' : ''} ${d.dry ? 'dry' : ''}`}>
    <div className="hk-strip">
      <b className="ttl">💡 Tips · Kite</b>
      <Seg small opts={KMODES} v={mode} set={setMode} />
      {!off && <Switch red on={!!d.kill} onChange={setKill} label="kill switch" title="cancels every working Tips order on Kite and exits at market" />}
      <span className="sp" />
      {off ? <span className="cap">paper book only · nothing reaches Kite</span>
        : <span className="cap" title={`entries ${caps.entries} of ${caps.max_entries} · loss ${inr(caps.loss_used || 0)} of ${inr(caps.loss_cap || 0)} · window ${d.window}`}>{d.dry ? 'DRY · ' : ''}entries <b>{caps.entries ?? 0}</b> of {caps.max_entries ?? 0} · loss <b>{inr(caps.loss_used || 0)}</b> of {inr(caps.loss_cap || 0)} · window {d.window}</span>}
      <span className="cap">reader <b className={READER[rd.state] || 'down'}>{String(rd.state || 'down').replace('_', ' ')}</b>{rd.last_post_label ? ` · last post ${rd.last_post_label}` : ''}</span>
      <button type="button" className="btn ghost small" onClick={() => onGo('tips')}>Open Tips</button>
    </div>
    {msg && <div className={`hk-msg ${msg.startsWith('⚠') ? 'down' : 'up'}`}>{msg}</div>}
    {!off && rows.map((r) => <Row key={r.id} r={r} reload={reload} setMsg={setMsg} />)}
    {!off && today.length > 0 && <div className="hk-today"><span className="dim">today</span>
      {today.map((t) => <span key={t.id} className="hk-call" title={`#${t.id} ${t.name} · ${t.text}`}><span className="id">#{t.id}</span><span className="nm">{t.name}</span>{t.grade && <Tag c={t.grade.c} title="call grade by code · informational">{t.grade.g}</Tag>}<StateWord s={t.state} tag={t.text} c={t.c} title={t.state?.word ? t.text : undefined} /></span>)}</div>}
    {!off && (clear.length > 0 || items.length > 0) && <div className="hk-checks">
      {clear.length > 0 && <div className="ck"><span className="gi up">✓</span><span>order check · all clear on {ids(clear)}{ck.at_label ? ` · ${ck.at_label}` : ''}</span></div>}
      {items.map((x, i) => <div key={i} className={`ck ${x.level === 'red' ? 'red' : 'amber'}`}><span className="gi">{x.level === 'red' ? '⛔' : '⚠'}</span>
        <span>#{x.id} · {x.text}{x.hand ? ` · ${x.hand}` : ''}</span></div>)}
    </div>}
    {!off && <div className="hk-rec">
      <span>channel 30 d: <b>{rec.hints ?? 0} hint{rec.hints === 1 ? '' : 's'}</b></span><span>T1 first <b>{rec.t1_first ?? 0}</b></span><span>stop first <b>{rec.stop_first ?? 0}</b></span>
      <span>no fill <b>{rec.no_fill ?? 0}</b></span><span>avg <b>{R(rec.avg_r)}</b></span><span>you took {rec.took ?? 0} of {rec.of ?? 0}</span>
      <span>yours <b className={cls(rec.you)}>{kiteMoney(rec.you ?? 0)}</b> vs all {kiteMoney(rec.all ?? 0)}</span>
      <span>{rec.day_check ? <>day check: <span className={rec.day_check.red ? 'down' : ''}>{rec.day_check.summary}</span></> : `day check ${rec.day_check_at || '3:35 PM'}`}</span>
    </div>}
  </div>;
}
