import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { THEMES, getMode, applyMode } from '../theme';

// ⚙️ System › Connections: Kite (login and market data only), Tradetron (real orders), Telegram, the evening job,
// the live feed, and the theme. The detailed settings are on the other System pages.
const Row = ({ title, ok, state, note, children }) => (
  <div className="v2card" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
    <div style={{ minWidth: 200 }}><b>{title}</b><div className="muted small">{note}</div></div>
    <span className={`achip ${ok ? 'g' : ''}`} style={ok ? {} : { color: 'var(--bad)' }}>{state}</span>
    <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>{children}</span></div>);

export default function SystemHome({ onGo, onKite }) {
  const kite = useLive('kite');
  const sch = useLive('schedule');
  const flow = useLive('flow');
  const [real, setReal] = useState(null);
  const [tg, setTg] = useState(null);
  const [mode, setMode] = useState(getMode);
  useEffect(() => { api.getReal().then(setReal).catch(() => {}); api.alertsStatus().then(setTg).catch(() => {}); }, []);
  const eod = (sch?.jobs || []).find((j) => j.key === 'eod');
  const f = flow?.feed || {};
  return <>
    <Row title="Kite" ok={kite?.has_token} state={kite?.has_token ? 'logged in' : 'log in needed'}
      note={`login and market data only (prices, candles, live feed) · no holdings or broker positions${kite?.expires_at ? ` · expires ${new Date(kite.expires_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit', hour12: true })}` : ''}`}>
      <button className="btn" onClick={onKite}>{kite?.has_token ? 'Log in again' : 'Log in'}</button></Row>
    <Row title="Tradetron (real orders)" ok={real?.token_set && real?.enabled && !real?.killed}
      state={real ? (real.killed ? 'kill switch ON' : real.enabled ? 'sending orders' : 'off') : '…'}
      note={real ? `strategy ${real.variant === 'E' ? 'E+' : real.variant} · capital ₹${(real.settings?.capital || 0).toLocaleString('en-IN')} · orders ${real.settings?.send_time}–${real.settings?.send_until} · token ${real.token_set ? 'set' : 'missing'}` : ''}>
      <button className="btn ghost" onClick={() => onGo('real')}>Open Real</button></Row>
    <Row title="Telegram" ok={tg?.ready && tg?.enabled} state={tg ? (tg.ready ? (tg.enabled ? 'on' : 'paused') : 'not set up') : '…'}
      note={tg ? `alerts ${tg.enabled ? 'on' : 'off'} · scheduler ${tg.scheduler_running ? 'running' : 'stopped'}${tg.holiday_today ? ` · holiday: ${tg.holiday_today}` : ''}` : ''}>
      <button className="btn ghost" onClick={() => onGo('alerts')}>Alerts & schedule</button></Row>
    <Row title="Evening sync + scan" ok={!eod?.last || eod.last.status === 'ok'} state={eod?.last ? (eod.last.status === 'ok' ? 'ok' : eod.last.status) : 'not run yet'}
      note={eod?.last?.at ? `last ${new Date(eod.last.at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true })}` : 'daily candles, scans, paper, real orders for tomorrow'}>
      <button className="btn ghost" onClick={() => onGo('data')}>Data</button></Row>
    <Row title="Intraday live feed" ok={f.state === 'live' || f.state === 'off'} state={f.state || '…'}
      note={f.state === 'live' ? `${f.tokens} instruments · last tick ${f.last_tick?.slice(11, 19) || ''}` : 'starts 9:10 AM in Signals or Paper mode'}>
      <button className="btn ghost" onClick={() => onGo('flow')}>Intraday Flow</button></Row>
    <Row title="Appearance" ok state={THEMES[mode]?.[0]} note="Auto follows the device (light by day settings, dark by night settings)">
      <span className="seg">{Object.entries(THEMES).map(([id, [label]]) => <button key={id} className={mode === id ? 'active' : ''}
        onClick={() => { setMode(id); applyMode(id); }}>{label}</button>)}</span></Row>
  </>;
}
