import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { clock, stamp } from '../timefmt';
import { inr } from '../fmt';
import { Panel, Tag, AiTag } from './ui2';
import { useIsPhone } from '../useMedia';

// 🤖 Home › "AI · Suggestions · for your review": what the local AI proposes for the next session (GET
// /api/ai/suggestions?status=open). Code made each one from last night's notes; the model wrote only the one-line why.
// Nothing happens until you tap: ✓ runs the SAME code path (and the same confirm text) as the hand action, ✗ records a
// reason, Later moves the expiry four hours once. A row's height never depends on a number: every line is one line
// with an ellipsis (the why may take two). Stream topic "ai" (the open count) → refetch.
const BOOK = { real: 'Real', paper: 'Paper', futures: 'Futures', options: 'Options' };
const SUB = {                                                      // what the row's small text says, per kind
  skip: (s) => `${BOOK[s.book] || 'the'} plan${s.value ? ` · ≈ ${inr(s.value)}` : ''}`,
  exit: (s) => `held in ${BOOK[s.book] || 'a book'}${s.value ? ` · ≈ ${inr(s.value)}` : ''}`,
  flag: (s) => `${BOOK[s.book] || 'the'} plan${s.value ? ` · ≈ ${inr(s.value)}` : ''} · the 9:00 dry run`,
  hold_options: () => 'Options · the day\'s entry window',
  roll_or_exit: (s) => `Futures · roll day${s.value ? ` · ≈ ${inr(s.value)}` : ''}`,
  backtest: () => 'Backtest F · a settings idea',
};
const CONF = { high: 'w', medium: '', low: '' };
const wday = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { weekday: 'short' }) : '');
// "accept → …": what the ✓ button really does, in plain words (the API path is in the tooltip)
const pathText = (s) => {
  const a = s.action;
  if (!a) return 'note only · nothing to run';
  if (a.path === 'POST /api/real/skip') return `the morning skip switch · only for ${wday(s.for_day) || 'the next send'}`;
  if (a.path === 'POST /api/options/hold') return 'the options hold for the day · no entry';
  if (a.path === 'ui') return `opens ${a.route || 'the page'}${a.hint ? ` · ${a.hint}` : ''}${a.prefill ? ' · you press Run' : ''}`;
  return a.path;
};
// the symbol in bold inside the title ("Skip GLENMARK tomorrow")
const Title = ({ s }) => {
  const t = s.title || '', sym = s.symbol;
  const i = sym ? t.indexOf(sym) : -1;
  if (i < 0) return <span>{t}</span>;
  return <span>{t.slice(0, i)}<b>{sym}</b>{t.slice(i + sym.length)}</span>;
};
const ELL = { minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
const LINE = { ...ELL, display: 'block', fontSize: 11.5, color: 'var(--muted)', marginTop: 2 };

function Row({ s, open, onOpen, onGo, onChanged }) {
  const phone = useIsPhone();
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState(null);                            // {text, bad} under the buttons
  const a = s.action;
  const act = (key, p, after) => {
    setBusy(key);
    p.then((r) => { setMsg({ text: after(r) }); onChanged(); }).catch((e) => setMsg({ text: e.message, bad: true })).finally(() => setBusy(''));
  };
  const accept = () => {
    if (!window.confirm(a?.confirm_text || `Accept #${s.id} · ${s.title}?`)) return;
    act('accept', api.aiSuggestionAccept(s.id), (r) => {
      if (a?.path === 'ui') {                                      // navigate; the prefill rides on the query string for the page to read
        const q = a.prefill ? new URLSearchParams(Object.entries(a.prefill).map(([k, v]) => [k, String(v)])).toString() : '';
        onGo((a.route || '').split('/').pop());
        if (q) window.history.replaceState(null, '', `${window.location.pathname}?${q}${window.location.hash}`);
        return `✓ accepted · opened ${a.route}`;
      }
      const res = r?.action_result;
      const text = res?.result ? Object.entries(res.result).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : typeof v === 'object' && v ? JSON.stringify(v) : String(v)}`).join(' · ') : '';
      return `✓ accepted${res?.ran ? ` · ran ${res.path}` : ' · recorded'}${text ? ` · ${text}` : ''}`;
    });
  };
  const dismiss = (reason) => act(reason, api.aiSuggestionDismiss(s.id, reason), () => `✗ dismissed · ${reason === 'knew' ? 'you knew' : reason}`);
  const later = () => act('later', api.aiSuggestionLater(s.id), (r) => `later · now until ${clock(r.expires_at)}`);
  const until = s.expires_at ? <Tag title={`expires ${stamp(s.expires_at)}${s.later ? ' · pushed once already' : ''}`}>until {clock(s.expires_at)}</Tag> : <Tag title="a settings idea keeps until you decide">no expiry</Tag>;
  const conf = <span style={s.confidence === 'low' ? { opacity: 0.6 } : undefined}><Tag c={CONF[s.confidence] || ''} title="set by code from the kind of evidence, never by the model">{s.confidence} confidence</Tag></span>;
  return <div style={{ borderBottom: '1px solid var(--rowline)', background: open ? 'var(--hover)' : undefined }}>
    <div className="click" role="button" tabIndex={0} onClick={() => onOpen(open ? null : s.id)} onKeyDown={(e) => { if (e.key === 'Enter') onOpen(open ? null : s.id); }}
      style={{ display: 'flex', gap: 10, padding: '9px 14px', minWidth: 0, alignItems: 'flex-start', cursor: 'pointer' }} title={open ? 'click to fold' : 'click for the evidence and the buttons'}>
      <span style={{ width: 24, height: 24, borderRadius: 6, background: 'var(--panel)', border: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, flex: 'none', marginTop: 1 }}>{s.icon || '🤖'}</span>
      <div style={{ flex: 1, minWidth: 0, fontSize: 12.5 }}>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', minWidth: 0, flexWrap: phone ? 'wrap' : 'nowrap' }}>
          <AiTag of={s} /><span className="mono muted" style={{ flex: 'none' }}>#{s.id}</span>
          <span style={{ ...ELL, flex: phone ? '1 1 100%' : '0 1 auto' }}><Title s={s} /></span>
          <span className="muted" style={{ ...ELL, flex: phone ? '1 1 100%' : '1 1 auto' }}>· {(SUB[s.kind] || (() => s.book || ''))(s)}</span>
          <span style={{ flex: 'none', display: 'flex', gap: 4 }}>{conf}{until}</span></div>
        <div style={{ color: 'var(--muted)', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', marginTop: 2 }} title={s.why}>{s.why}</div>
        {!open && <span style={LINE} title={a?.path ? `${a.path}${a.args ? ` ${JSON.stringify(a.args)}` : ''}` : 'no action: a note for you'}>accept → {pathText(s)}</span>}
      </div></div>
    {open && <div style={{ padding: '0 14px 10px 48px' }}>
      <div style={{ border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg)', padding: '8px 12px' }}>
        <div className="muted" style={{ fontSize: 10.5, letterSpacing: '.04em', textTransform: 'uppercase', marginBottom: 4 }}>Evidence · rows from the database</div>
        {(s.evidence || []).length ? (s.evidence || []).map((e, i) => <div key={i} style={{ ...ELL, fontSize: 12.5, lineHeight: '22px' }} title={e.text}>
          <span style={{ marginRight: 6 }}>{e.icon || '·'}</span>{e.url ? <a href={e.url} target="_blank" rel="noreferrer" style={{ color: 'var(--blue)' }}>{e.text}</a> : e.text}</div>)
          : <div className="muted" style={{ fontSize: 12 }}>no evidence rows kept</div>}
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 8, minWidth: 0 }}>
        {a && <button type="button" className="btn primary small" disabled={!!busy} onClick={accept} title={a.path === 'ui' ? `opens ${a.route}` : `${a.path} · the same confirm as the hand action`}>{busy === 'accept' ? 'Working…' : `✓ ${a.label}`}</button>}
        {!a && <button type="button" className="btn small" disabled title="a note-only suggestion: there is nothing to run">note only</button>}
        <button type="button" className="btn ghost small" disabled={!!busy} onClick={() => dismiss('wrong')} title="recorded as wrong · the scorecard counts it">{busy === 'wrong' ? '…' : '✗ Dismiss · wrong'}</button>
        <button type="button" className="btn ghost small" disabled={!!busy} onClick={() => dismiss('knew')} title="recorded as already known">{busy === 'knew' ? '…' : '✗ Already knew'}</button>
        <button type="button" className="btn ghost small" disabled={!!busy || s.later} onClick={later} title={s.later ? 'pushed once already' : 'move the expiry 4 hours on, once'}>{busy === 'later' ? '…' : 'Later'}</button>
        <span className="muted" style={{ ...ELL, fontSize: 11, marginLeft: phone ? 0 : 'auto', flex: phone ? '1 1 100%' : '0 1 auto' }} title={a?.path || ''}>accept → {pathText(s)}</span></div>
      {msg && <div className={msg.bad ? 'down' : 'up'} style={{ ...ELL, fontSize: 11.5, marginTop: 6 }} title={msg.text}>{msg.text}</div>}
    </div>}
  </div>;
}

export default function AiSuggestions({ onGo }) {
  const live = useLive('ai');
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState('');
  const [open, setOpen] = useState(null);
  const [tick, setTick] = useState(0);
  const sig = JSON.stringify(live?.suggestions || null);        // the open count / by-kind from the stream: refetch when it moves
  useEffect(() => {
    let on = true;
    api.getAiSuggestions({ status: 'open', limit: 50 }).then((x) => { if (on) { setRows(x.rows || []); setErr(''); } })
      .catch((e) => { if (on) { setRows([]); setErr(e.message); } });
    return () => { on = false; };
  }, [sig, tick]);
  const list = rows || [];
  const n = list.length;
  const goAi = (anchor) => { onGo('ai'); if (anchor) setTimeout(() => document.getElementById(anchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 350); };
  const right = <><button type="button" className="btn small" onClick={() => goAi('sugg-scorecard')} title="System › AI · how each kind has done">Scorecard</button>
    <button type="button" className="btn small" onClick={() => goAi('sugg-scorecard')} title="switch a kind off or on (System › AI › Scorecard)">Kinds…</button></>;
  return <Panel id="suggestions" title={<><AiTag title="suggestions written by the local model · nothing happens until you tap" /> Suggestions · for your review</>} meta={rows == null ? '…' : `${n} open · nothing happens until you tap`} right={right}>
    {err && <div className="rbanner bad" style={{ margin: '8px 14px' }}>{err}</div>}
    {rows == null ? <p className="muted small rempty">Loading…</p>
      : !n ? <p className="muted small rempty">No suggestions · they are born after the evening job from the night's notes</p>
        : list.map((s) => <Row key={s.id} s={s} open={open === s.id} onOpen={setOpen} onGo={onGo} onChanged={() => setTick((t) => t + 1)} />)}
    <div className="rnote" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>born from last night's notes · each one expires on its own · accept uses the same confirm dialogs as today · Telegram: <b>accept N</b> · <b>dismiss N</b> · <b>sugg</b></div>
  </Panel>;
}
