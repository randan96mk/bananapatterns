import React, { useEffect, useState } from 'react';
import { useLive } from '../services/live';
import { useAttention } from './AttentionStrip';
import { THEMES, getMode, applyMode } from '../theme';

// The schedule comes from the live connection (📡 services/live.js); the clock and countdowns tick every second here.
export function useSchedule() {
  const data = useLive('schedule');
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  return { data, now };
}

export const left = (iso, now) => {
  const s = Math.max(0, Math.round((new Date(iso).getTime() - now) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}:${String(x).padStart(2, '0')}`;
};
export const istClock = (now) => new Date(now).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false });
const TAB = { breakout: 'Breakout', paper: 'Paper', real: 'Real', flow: 'Intraday', screens: 'Screens', plan: 'Plan' };
const at12 = (iso) => new Date(iso).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit', hour12: true });

// The status bar on every page: Kite, market, the last evening run, real trading, the next job, attention, theme.
export default function StatusBar({ kite, onKite, onGo }) {
  const { data, now } = useSchedule();
  const att = useAttention();
  const [mode, setMode] = useState(getMode);
  const jobs = (data?.jobs || []).filter((j) => !j.skip);
  const next = jobs[0];
  const later = jobs.slice(1, 3);
  const eod = (data?.jobs || []).find((j) => j.key === 'eod')?.last;
  const m = data?.market;
  const real = data?.real;
  const needs = att ? Object.entries(att).filter(([, a]) => a?.level && a.level !== 'running') : [];
  const [open, setOpen] = useState(false);                // the needs-a-look list under the chip
  useEffect(() => { if (!open) return undefined; const close = (e) => { if (!e.target.closest?.('.needwrap')) setOpen(false); }; document.addEventListener('click', close); return () => document.removeEventListener('click', close); }, [open]);
  const rows = [];                                        // one line per message, with every page it belongs to
  needs.forEach(([tab, a]) => (a.items || []).forEach((x) => { const r = rows.find((y) => y.text === x.text); if (r) { if (!r.tabs.includes(tab)) r.tabs.push(tab); } else rows.push({ text: x.text, level: x.level, tabs: [tab] }); }));
  const loggedIn = kite?.has_token;
  return (
    <div className="statusbar" role="status">
      <button className={`sb ${loggedIn ? '' : 'bad'}`} onClick={onKite} title="Kite login">
        <i className={`dot ${loggedIn ? 'ok' : 'bad'}`} />{loggedIn ? 'Kite' : 'Kite: log in'}</button>
      {m && <span className="sb">{m.open ? `NSE open · closes ${at12(m.closes_at)}` : m.opens_at ? `NSE closed · opens ${new Date(m.opens_at).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', weekday: 'short' })} ${at12(m.opens_at)}` : 'NSE closed'}</span>}
      {eod?.at && <span className="sb hide-phone">Evening run {at12(eod.at)} {eod.status === 'ok' ? '✓' : '⚠'}</span>}
      {real && <button className="sb" onClick={() => onGo('real')}>💰 Real <b className={real.enabled && !real.killed ? 'up' : 'down'}>{real.killed ? 'KILLED' : real.enabled ? 'ON' : 'OFF'}</b> · {real.variant === 'E' ? 'E+' : real.variant}</button>}
      {next && <span className="sb">⏱ Next: <b>{next.name}</b> in <b className="mono">{left(next.at, now)}</b>
        <span className="hide-phone muted">{later.map((j) => ` · ${j.name.toLowerCase()} ${left(j.at, now)}`).join('')}</span></span>}
      <span className="sbfill" />
      {needs.length > 0 && <span className="needwrap"><button className="sb warn" aria-expanded={open} onClick={() => setOpen(!open)}><i className="dot warn blinkdot" />{rows.length} need{rows.length === 1 ? 's' : ''} a look</button>
        {open && <div className="sysdrop needdrop" role="menu">{rows.map((r) => <div key={r.text} className="needrow"><span className={`i ${r.level === 'buy' ? 'up' : r.level === 'sell' ? 'down' : 'warn'}`}>{r.level === 'buy' ? '↑' : r.level === 'sell' ? '↓' : '!'}</span>
          <span className="t">{r.text}</span><span className="b">{r.tabs.map((t) => <button key={t} type="button" className="btn ghost small" onClick={() => { setOpen(false); onGo(t); }}>{TAB[t] || t}</button>)}</span></div>)}</div>}</span>}
      <label className="sb themepick" title="Theme">🎨
        <select value={mode} onChange={(e) => { setMode(e.target.value); applyMode(e.target.value); }} aria-label="Theme">
          {Object.entries(THEMES).map(([id, [label]]) => <option key={id} value={id}>{label}</option>)}
        </select></label>
      <span className="sb mono hide-phone">{istClock(now)}</span>
    </div>);
}
