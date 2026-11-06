import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { THEMES, getMode, applyMode } from '../theme';
import { ampm, clock, stamp } from '../timefmt';
import { inr } from '../fmt';
import { Head, Chip, Panel, KV, Seg } from './ui2';

// ⚙️ System › Connections: Kite (login and market data only), real orders, Telegram, the evening job, the live
// feed, and the theme — one panel each with a status dot, a one-line state and a few facts. Details live on the
// other System pages.
const Dot = ({ ok, warn }) => <i style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 4, marginRight: 7, verticalAlign: 1,
  background: ok == null ? 'var(--muted)' : ok ? 'var(--ok)' : warn ? 'var(--warn)' : 'var(--bad)' }} />;
const Conn = ({ title, ok, warn, state, pairs, children }) => (
  <Panel title={<><Dot ok={ok} warn={warn} />{title}</>} right={<span className={ok === false && !warn ? 'down' : ''}>{state}</span>}>
    <KV pairs={pairs} />
    <div style={{ padding: '8px 14px', display: 'flex', gap: 6, flexWrap: 'wrap' }}>{children}</div>
  </Panel>);

export default function SystemHome({ onGo, onKite }) {
  const kite = useLive('kite');
  const sch = useLive('schedule');
  const flow = useLive('flow');
  const [real, setReal] = useState(null);
  const [tg, setTg] = useState(null);
  const [h, setH] = useState(null);
  const [mode, setMode] = useState(getMode);
  useEffect(() => {
    api.getReal().then(setReal).catch(() => {}); api.alertsStatus().then(setTg).catch(() => {}); api.health().then(setH).catch(() => {});
  }, []);
  const eod = (sch?.jobs || []).find((j) => j.key === 'eod');
  const f = flow?.feed || {};
  const realOn = real ? real.token_set && real.enabled && !real.killed : null;
  return <div className="rd">
    <Head title="System · Connections">
      {h && <Chip c={h.red ? 'bad' : h.amber ? 'warn' : 'ok'}><Dot ok={!h.red} warn={!!h.amber} />{h.amber} amber · {h.red} red · checked {clock(h.at)}</Chip>}
      <Chip><Dot ok={!!kite?.has_token} />Kite <b>{kite?.has_token ? 'logged in' : 'log in needed'}</b></Chip>
    </Head>
    <div className="cols c3">
      <Conn title="Kite · login + market data" ok={kite ? !!kite.has_token : null} state={kite?.has_token ? 'logged in' : 'log in needed'}
        pairs={[['Session', kite?.has_token ? `to ${kite.expires_at ? clock(kite.expires_at) : '6:00 AM'}` : 'none'],
          ['Used for', 'prices · candles · feed'], ['Holdings · positions', 'never read']]}>
        <button className="btn" onClick={onKite}>{kite?.has_token ? 'Log in again' : 'Log in'}</button></Conn>
      <Conn title="Real orders" ok={realOn} state={real ? (real.killed ? 'kill switch ON' : real.enabled ? 'sending orders' : 'off') : '…'}
        pairs={real ? [['Strategy', real.variant === 'E' ? 'E+' : real.variant], ['Capital', inr(real.settings?.capital || 0)],
          ['Orders', `${ampm(real.settings?.send_time)} – ${ampm(real.settings?.send_until)}`], ['Token', real.token_set ? 'set' : 'missing']] : [['Status', '…']]}>
        <button className="btn ghost" onClick={() => onGo('real')}>Open Real</button></Conn>
      <Conn title="Telegram" ok={tg ? !!(tg.ready && tg.enabled) : null} warn={tg?.ready && !tg.enabled}
        state={tg ? (tg.ready ? (tg.enabled ? 'on' : 'paused') : 'not set up') : '…'}
        pairs={tg ? [['Alerts', tg.enabled ? 'on' : 'off'], ['Scheduler', tg.scheduler_running ? 'running' : 'stopped'],
          ['Today', tg.trading_day ? 'trading day' : `no trading · ${tg.holiday_today || 'weekend'}`]] : [['Status', '…']]}>
        <button className="btn ghost" onClick={() => onGo('alerts')}>Alerts & schedule</button></Conn>
      <Conn title="Intraday live feed" ok={f.state ? f.state === 'live' || f.state === 'off' : null} state={f.state || '…'}
        pairs={[['Instruments', f.state === 'live' ? f.tokens : '—'], ['Last tick', f.last_tick ? clock(f.last_tick, true) : '—'],
          ['Starts', '9:10 AM']]}>
        <button className="btn ghost" onClick={() => onGo('flow')}>Intraday Flow</button></Conn>
      <Conn title="Evening sync + scan" ok={!eod?.last || eod.last.status === 'ok'} state={eod?.last ? (eod.last.status === 'ok' ? 'ok' : eod.last.status) : 'not run yet'}
        pairs={[['Last run', eod?.last?.at ? stamp(eod.last.at) : 'never'], ['Next', eod?.at ? stamp(eod.at) : '—'],
          ['Does', 'candles · scans · orders']]}>
        <button className="btn ghost" onClick={() => onGo('data')}>Data</button></Conn>
      <Conn title="Appearance" ok state={THEMES[mode]?.[0]}
        pairs={[['Theme', THEMES[mode]?.[0]], ['Auto', 'follows the device'], ['Times', '12-hour IST']]}>
        <Seg opts={Object.entries(THEMES).map(([id, [label]]) => [id, label])} v={mode} set={(id) => { setMode(id); applyMode(id); }} /></Conn>
    </div>
  </div>;
}
