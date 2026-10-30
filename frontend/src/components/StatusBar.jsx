import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useAttention } from './AttentionStrip';
import { THEMES, getMode, applyMode } from '../theme';

// Shared schedule (GET /api/schedule), polled every minute; the clock and countdowns tick every second locally.
let cache = { at: 0, data: null };
export function useSchedule() {
  const [data, setData] = useState(cache.data);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    let alive = true;
    const load = () => {
      if (Date.now() - cache.at < 20000 && cache.data) { setData(cache.data); return; }
      api.getSchedule().then((x) => { cache = { at: Date.now(), data: x }; if (alive) setData(x); }).catch(() => {});
    };
    load();
    const t1 = setInterval(load, 60000), t2 = setInterval(() => setNow(Date.now()), 1000);
    return () => { alive = false; clearInterval(t1); clearInterval(t2); };
  }, []);
  return { data, now };
}

export const left = (iso, now) => {
  const s = Math.max(0, Math.round((new Date(iso).getTime() - now) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}:${String(x).padStart(2, '0')}`;
};
export const istClock = (now) => new Date(now).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false });
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
  const loggedIn = kite?.has_token;
  return (
    <div className="statusbar" role="status">
      <button className={`sb ${loggedIn ? '' : 'bad'}`} onClick={onKite} title="Kite login">
        <i className={`dot ${loggedIn ? 'ok' : 'bad'}`} />{loggedIn ? 'Kite' : 'Kite: log in'}</button>
      {m && <span className="sb">{m.open ? `NSE open · closes ${at12(m.closes_at)}` : m.opens_at ? `NSE closed · opens ${new Date(m.opens_at).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', weekday: 'short' })} ${at12(m.opens_at)}` : 'NSE closed'}</span>}
      {eod?.at && <span className="sb hide-phone">Evening run {at12(eod.at)} {eod.status === 'ok' ? '✓' : '⚠'}</span>}
      {real && <button className="sb" onClick={() => onGo('real')}>💰 Real <b className={real.enabled && !real.killed ? 'up' : 'down'}>{real.killed ? 'KILLED' : real.enabled ? 'ON' : 'OFF'}</b> · {real.variant}</button>}
      {next && <span className="sb">⏱ Next: <b>{next.name}</b> in <b className="mono">{left(next.at, now)}</b>
        <span className="hide-phone muted">{later.map((j) => ` · ${j.name.toLowerCase()} ${left(j.at, now)}`).join('')}</span></span>}
      <span className="sbfill" />
      {needs.length > 0 && <button className="sb warn" onClick={() => onGo(needs[0][0])}><i className="dot warn blinkdot" />{needs.length} needs attention</button>}
      <label className="sb themepick" title="Theme">🎨
        <select value={mode} onChange={(e) => { setMode(e.target.value); applyMode(e.target.value); }} aria-label="Theme">
          {Object.entries(THEMES).map(([id, [label]]) => <option key={id} value={id}>{label}</option>)}
        </select></label>
      <span className="sb mono hide-phone">{istClock(now)}</span>
    </div>);
}
