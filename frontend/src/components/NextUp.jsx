import React, { useState } from 'react';
import { useSchedule, left, istClock } from './StatusBar';
import { useIsPhone } from '../useMedia';

const at12 = (iso) => new Date(iso).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit', hour12: true });
const dayOf = (iso) => new Date(iso).toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
const wday = (iso) => new Date(iso).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', weekday: 'short' });

// ⏱ Next up: every scheduled job with a live countdown (GET /api/schedule; the times come from the scheduler's settings).
export default function NextUp() {
  const { data, now } = useSchedule();
  const phone = useIsPhone();
  const [all, setAll] = useState(false);
  if (!data) return null;
  const jobs = data.jobs || [];
  const live = jobs.filter((j) => !j.skip);
  const next = live[0];
  const today = new Date(now).toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  const shown = all ? jobs : jobs.slice(0, phone ? 3 : 8);
  const m = data.market;
  return (
    <div className="panel nextup">
      <div className="nuhead"><b>⏱ Next up</b><span className="mono nuclock">{istClock(now)} <span className="muted small">IST</span></span></div>
      <div className="muted small">{m?.open ? `NSE open · closes in ${left(m.closes_at, now)}` : m?.opens_at ? `NSE opens in ${left(m.opens_at, now)}` : 'NSE closed'}</div>
      {next && (
        <div className="nunext">
          <svg viewBox="0 0 54 54" className="nuring" aria-hidden="true"><circle cx="27" cy="27" r="23" className="nutrack" />
            {next.every && <circle cx="27" cy="27" r="23" className="nuarc" transform="rotate(-90 27 27)"
              strokeDasharray={`${144 * (1 - Math.min(1, (new Date(next.at).getTime() - now) / (next.every * 60000)))} 144`} />}</svg>
          <div><div className="muted small">NEXT · in</div><div className="mono nubig">{left(next.at, now)}</div>
            <b>{next.icon} {next.name}</b>{next.real && <span className="tagreal">💰 real</span>}</div>
          {next.last?.at && <div className="muted small nulast">last {next.last.at.slice(11, 16)} {next.last.status === 'ok' ? '✓' : '⚠'}</div>}
        </div>)}
      <div className="nulist">{shown.map((j) => (
        <div key={j.key} className={`nurow ${j.skip ? 'skip' : ''}`}>
          <span aria-hidden="true">{j.icon}</span>
          <div><b>{j.name}</b>{j.real && <span className="tagreal">💰 real</span>}{j.skip && <span className="tagskip">{j.skip}</span>}
            {!phone && <div className="muted small">{j.desc}</div>}
            {j.every && !j.skip && <div className="nubar"><i style={{ width: `${100 * (1 - Math.min(1, (new Date(j.at).getTime() - now) / (j.every * 60000)))}%` }} /></div>}</div>
          {!phone && <span className="muted small nuat">{dayOf(j.at) === today ? '' : `${wday(j.at)} `}{at12(j.at)}</span>}
          <span className="mono nuleft">{j.skip ? '—' : left(j.at, now)}</span>
        </div>))}</div>
      {jobs.length > shown.length || all ? <button className="linkbtn nuall" onClick={() => setAll(!all)}>{all ? 'Show less ▴' : `Show all ${jobs.length} ▾`}</button> : null}
    </div>);
}
