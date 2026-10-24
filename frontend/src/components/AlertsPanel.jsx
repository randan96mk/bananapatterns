import React, { useCallback, useEffect, useState } from 'react';
import * as api from '../services/api';

const GROUPS = [
  ['Stage alerts', [['breakout', '🚀 Fresh breakout'], ['below_pivot', '🔻 Back below pivot'], ['played_out', '🏁 Played out'],
    ['forming', '🔵 Newly forming'], ['near_pivot', '🎯 Near pivot'], ['low_volume_breakout', '⚠️ Low-volume breakout'],
    ['left', '▫️ Left a card (noisy)']]],
  ['Delivery', [['enabled', 'Alerts on'], ['digest', '📋 Daily digest'], ['chart_images', '🖼 Chart images'],
    ['system', '⚙️ System alerts (login, job failures, index changes)'], ['intraday', '⚡ Intraday pivot heads-up'],
    ['job_enabled', '🕡 Automatic evening job']]],
];
const FIELDS = [['near_pivot_pct', 'Near pivot within (0.03 = 3%)'], ['job_time', 'Evening job (IST)'],
  ['reminder_time', 'Login reminder (IST)'], ['intraday_start', 'Intraday from'], ['intraday_end', 'Intraday until'],
  ['intraday_interval_min', 'Intraday every (min)'], ['universe_refresh_weekday', 'Weekly refresh day (0=Mon … 6=Sun)']];
const strip = (h) => h.replace(/<[^>]+>/g, '');

// Settings + activity for Telegram alerts and the automatic jobs.
export default function AlertsPanel() {
  const [st, setSt] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [jobs, setJobs] = useState(null);
  const [log, setLog] = useState([]);
  const [msg, setMsg] = useState('');

  const load = useCallback(async () => {
    try {
      const [s, c, j, l] = await Promise.all([api.alertsStatus(), api.getConfig('alerts'), api.getJobs(), api.alertsLog()]);
      setSt(s); setCfg(c); setJobs(j); setLog(l);
    } catch (e) { setMsg(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async (patch) => {
    try { const c = await api.putConfig('alerts', patch); setCfg(c); setMsg('Saved'); setSt(await api.alertsStatus()); }
    catch (e) { setMsg(e.message); }
  };
  const act = async (fn, okText) => {
    setMsg('…');
    try { const r = await fn(); setMsg(okText(r)); setTimeout(load, 1500); } catch (e) { setMsg(e.message); }
  };

  if (!st || !cfg) return <div className="panel">{msg || 'Loading…'}</div>;
  return (
    <>
      <div className="panel">
        <h3>Telegram</h3>
        <div className="row">
          <span className={`pill ${st.bot_configured ? 'ok' : 'bad'}`}>Bot token {st.bot_configured ? 'set' : 'missing'}</span>
          <span className={`pill ${st.chat_id ? 'ok' : 'bad'}`}>Chat {st.chat_id || 'not set'}</span>
          <span className={`pill ${st.ready ? 'ok' : 'bad'}`}>{st.ready ? 'Ready' : 'Not ready'}</span>
          <button className="btn ghost" disabled={!st.bot_configured}
                  onClick={() => act(api.detectChat, (r) => (r.saved ? `Chat ${r.saved} saved`
                    : r.chats.length ? `Several chats found: ${r.chats.map((c) => `${c.name} (${c.chat_id})`).join(', ')} — set chat_id below`
                      : r.hint))}>Detect chat ID</button>
          <button className="btn" disabled={!st.ready} onClick={() => act(api.testAlert, () => 'Test message sent — check Telegram')}>
            Send test message</button>
          <span className="msg">{msg}</span>
        </div>
        {!st.bot_configured && (
          <ol className="muted small">
            <li>In Telegram, message <b>@BotFather</b> → <code>/newbot</code> → copy the token.</li>
            <li>Add <code>TELEGRAM_BOT_TOKEN=…</code> to <code>.env</code> and restart (<code>./start.sh</code>).</li>
            <li>Send your bot any message, then press <b>Detect chat ID</b> and <b>Send test message</b>.</li>
          </ol>)}
        <div className="row">
          <label className="muted">chat_id <input value={cfg.chat_id} onChange={(e) => setCfg({ ...cfg, chat_id: e.target.value })} /></label>
          <label className="muted">screens <select value={cfg.screens.join(',')}
            onChange={(e) => save({ screens: e.target.value.split(',') })}>
            <option value="vcp">VCP</option><option value="bases">Bases</option><option value="vcp,bases">VCP + Bases</option>
          </select></label>
          <button className="btn ghost" onClick={() => save({ chat_id: cfg.chat_id })}>Save chat</button>
          <span className="muted">Today {st.today}: {st.trading_day ? 'trading day' : `no trading (${st.holiday_today || 'weekend'})`}
            {st.upcoming_holidays.length > 0 && ` · next holidays ${st.upcoming_holidays.map((h) => `${h.date} ${h.name}`).join(', ')}`}</span>
        </div>
      </div>

      <div className="panel">
        <h3>What to send</h3>
        <div className="toggles">
          {GROUPS.map(([title, items]) => (
            <div key={title}><h4>{title}</h4>
              {items.map(([k, label]) => (
                <label key={k} className="toggle"><input type="checkbox" checked={!!cfg[k]}
                  onChange={(e) => save({ [k]: e.target.checked })} /> {label}</label>))}
            </div>))}
          <div><h4>Timing</h4>
            {FIELDS.map(([k, label]) => (
              <label key={k} className="toggle field">{label}
                <input value={String(cfg[k])} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value })}
                       onBlur={(e) => save({ [k]: e.target.value })} /></label>))}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="row"><h3>Automatic jobs</h3>
          <button className="btn" onClick={() => act(() => api.runJob('eod'), () => 'Evening job started (sync → scan → alerts)')}>Run evening job now</button>
          <button className="btn ghost" onClick={() => act(() => api.runJob('intraday'), () => 'Intraday check started')}>Intraday check now</button>
          <button className="btn ghost" onClick={() => act(() => api.runJob('weekly'), () => 'Weekly refresh started')}>Weekly refresh now</button>
          <button className="btn ghost" onClick={load}>Refresh</button>
        </div>
        <p className="muted small">Jobs run only while this app is open and the Mac is awake (see README: pmset wake schedule).</p>
        <table className="grid small">
          <thead><tr><th>Job</th><th>Date</th><th>Started</th><th>Status</th><th>Message</th></tr></thead>
          <tbody>{(jobs?.runs || []).map((r) => (
            <tr key={r.id}><td>{r.job}</td><td>{r.run_date}</td><td>{r.started_at.replace('T', ' ').slice(0, 16)}</td>
              <td className={`s-${r.status}`}>{r.status}</td><td className="wrap">{r.message}</td></tr>))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>Sent alerts</h3>
        <table className="grid small">
          <thead><tr><th>Time</th><th>Kind</th><th>Symbol</th><th>OK</th><th>Text</th></tr></thead>
          <tbody>{log.map((r) => (
            <tr key={r.key}><td>{r.sent_at.replace('T', ' ').slice(0, 16)}</td><td>{r.kind}</td><td>{r.symbol}</td>
              <td className={r.ok ? 'up' : 'down'}>{r.ok ? '✓' : `✗ ${r.error || ''}`}</td>
              <td className="wrap">{strip(r.text).slice(0, 220)}</td></tr>))}
          </tbody>
        </table>
      </div>
    </>
  );
}
