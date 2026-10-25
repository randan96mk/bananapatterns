import React, { useCallback, useEffect, useState } from 'react';
import * as api from '../services/api';

const GROUPS = [
  ['Stage alerts', [['breakout', '🚀 Fresh breakout'], ['below_pivot', '🔻 Back below pivot'], ['played_out', '🏁 Played out'],
    ['forming', '🔵 Newly forming'], ['near_pivot', '🎯 Near pivot'], ['low_volume_breakout', '⚠️ Low-volume breakout'],
    ['left', '▫️ Left a card (noisy)']]],
  ['Delivery', [['enabled', 'Alerts on'], ['digest', '📋 Daily digest'], ['chart_images', '🖼 Chart images'],
    ['system', '⚙️ System alerts (login, job failures, index changes)'], ['intraday', '⚡ Intraday pivot heads-up + failed breakout'],
    ['close_check', '🕒 Act-before-close check (15:15)'],
    ['job_enabled', '🕡 Automatic evening job']]],
  ['Priority', [['tiering', '🅰️ A/B priority (A at once, B per setting)'], ['market_line', '📊 Market line on alerts'],
    ['market_sizing', '⚖️ Market-aware size (½ under pressure, ¼ in correction)']]],
  ['Daily plan', [['top_setups', '🎯 Top setups after the evening scan'], ['morning_plan', '☀️ Morning plan'],
    ['gap_check', '🔔 Opening (gap) check'], ['position_guard', '📒 Position guard (Kite holdings, read-only)'],
    ['alert_buttons', '🔘 Buttons under alerts (Watch · Mute · Chart · I bought)']]],
];
const FIELDS = [['near_pivot_pct', 'Near pivot within (0.03 = 3%)'], ['job_time', 'Evening job (IST)'],
  ['reminder_time', 'Login reminder (IST)'], ['intraday_start', 'Intraday from'], ['intraday_end', 'Intraday until'],
  ['intraday_interval_min', 'Intraday every (min)'], ['close_check_time', 'Close check at'],
  ['intraday_volume_mult', 'Volume needed (× 50-day avg)'], ['failed_buffer_pct', 'Failed below pivot by (0.01 = 1%)'],
  ['close_min_range', 'Close check: min place in day range (0–1)'], ['top_setups_time', 'Top setups at'],
  ['plan_top_n', 'How many top setups'], ['profit_zone_pct', 'Guard: profit zone at (+%)'],
  ['extended_pct', 'Guard: extended above 50-day (%)'], ['failed_days', 'Guard: failed if under pivot within (days)'], ['morning_plan_time', 'Morning plan at'], ['gap_check_time', 'Opening check at'], ['universe_refresh_weekday', 'Weekly refresh day (0=Mon … 6=Sun)']];
const CHART_KINDS = [['breakout', '🚀 Breakout'], ['breakout_low_volume', '⚠️ Low-volume breakout'],
  ['near_pivot', '🎯 Near pivot'], ['forming', '🔵 Newly forming'], ['below_pivot', '🔻 Below pivot'],
  ['played_out', '🏁 Played out'], ['climbing', '📈 Climbing'], ['intraday', '⚡ Intraday + 15:15']];
const PRIORITY_FIELDS = [['a_min_rs', 'A needs RS ≥'], ['a_min_trend', 'A needs trend checks ≥ (of 8)'],
  ['a_top_industry_frac', 'A needs industry in top (0.33 = third)']];
const SIZE_FIELDS = [['buy_zone_pct', 'Buy zone above pivot (0.05 = 5%)'],
  ['capital', 'Capital ₹ for position size (0 = off)'], ['risk_pct', 'Risk per trade (0.01 = 1%)']];
// Phone keyboards: a time picker for HH:MM fields, the number pad for numbers.
const inputKind = (k) => (/(_time|intraday_start|intraday_end)$/.test(k) ? { type: 'time' }
  : k === 'chat_id' ? {} : { inputMode: 'decimal' });
const strip = (h) => h.replace(/<[^>]+>/g, '');
const SAMPLES = [['all', 'All types'], ['breakout', '🚀 Breakout'], ['near_pivot', '🎯 Near pivot'],
  ['forming', '🔵 Newly forming'], ['below_pivot', '🔻 Back below pivot'], ['played_out', '🏁 Played out'],
  ['intraday', '⚡ Intraday heads-up'], ['failed', '⚠️ Failed breakout'], ['close_check', '🕒 15:15 close check'],
  ['digest', '📋 Digest'], ['system', '⚙️ System']];

// Settings + activity for Telegram alerts and the automatic jobs.
export default function AlertsPanel() {
  const [st, setSt] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [jobs, setJobs] = useState(null);
  const [log, setLog] = useState([]);
  const [msg, setMsg] = useState('');
  const [bot, setBot] = useState(null);
  const [sampleKind, setSampleKind] = useState('all');
  const [sampleRes, setSampleRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [kite, setKite] = useState(null);
  const [timesDraft, setTimesDraft] = useState(null);

  const load = useCallback(async () => {
    try {
      const [s, c, j, l] = await Promise.all([api.alertsStatus(), api.getConfig('alerts'), api.getJobs(), api.alertsLog()]);
      setSt(s); setCfg(c); setJobs(j); setLog(l);
      api.kiteStatus().then(setKite).catch(() => {});
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

  const check = async () => { setBot({ checking: true }); try { setBot(await api.checkBot()); } catch (e) { setBot({ ok: false, error: e.message }); } };
  const sendSample = async () => {
    setBusy(true); setSampleRes(null);
    try { setSampleRes(await api.sampleAlert(sampleKind)); setTimeout(load, 500); }
    catch (e) { setSampleRes({ error: e.message }); }
    setBusy(false);
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
          <button className="btn ghost" disabled={!st.bot_configured} onClick={check}>Check bot</button>
          {bot && (bot.checking ? <span className="muted">checking…</span>
            : bot.ok ? <span className="up">@{bot.username} ✓ token valid{bot.chat_id ? ` · chat ${bot.chat_id}` : ' · no chat yet'}</span>
              : <span className="down">✗ {bot.error}</span>)}
          <span className="msg">{msg}</span>
        </div>
        <div className="row">
          <b>Sample alert</b>
          <select value={sampleKind} onChange={(e) => setSampleKind(e.target.value)}>
            {SAMPLES.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
          </select>
          <button className="btn" disabled={!st.bot_configured || !st.chat_id || busy} onClick={sendSample}>
            {busy ? 'Sending…' : 'Send sample'}</button>
          <span className="muted small">Marked "🧪 TEST", built from your latest scan (or a built-in example). Works any day, no Kite needed.</span>
        </div>
        {sampleRes && (sampleRes.error ? <p className="down">✗ {sampleRes.error}</p> : (
          <p className="small">{sampleRes.sent} sent{sampleRes.failed ? `, ${sampleRes.failed} failed` : ''} to chat {sampleRes.chat_id}: {' '}
            {sampleRes.results.map((r) => (
              <span key={r.kind} className={`tag ${r.ok ? 'ok' : 'warnbg'}`} title={r.error || ''}>
                {r.ok ? '✓' : '✗'} {r.kind}{r.chart ? ' 🖼' : ''}</span>))}
            {sampleRes.results.find((r) => r.error) && <span className="down"> {sampleRes.results.find((r) => r.error).error}</span>}
          </p>))}
        {st.charts_available === false && (
          <p className="warn small">Chart images are off: matplotlib isn't installed. Run{' '}
            <code>.venv/bin/pip install -r requirements.txt</code> and restart; alerts are sent as text meanwhile.</p>)}
        {!st.bot_configured && (
          <ol className="muted small">
            <li>In Telegram, message <b>@BotFather</b> → <code>/newbot</code> → copy the token.</li>
            <li>Add <code>TELEGRAM_BOT_TOKEN=…</code> to <code>.env</code> and restart (<code>./start.sh</code>).</li>
            <li>Send your bot any message, then press <b>Detect chat ID</b> and <b>Send test message</b>.</li>
          </ol>)}
        <div className="row">
          <label className="muted">chat_id <input value={cfg.chat_id} onChange={(e) => setCfg({ ...cfg, chat_id: e.target.value })} /></label>
          <span className="muted">screens</span>
          {[['vcp', 'VCP'], ['cup', 'Cup & handle'], ['flag', 'Bull flag'], ['bases', 'Bases']].map(([k, label]) => (
            <label key={k} className="muted"><input type="checkbox" checked={cfg.screens.includes(k)}
              onChange={(e) => {
                const next = e.target.checked ? [...cfg.screens, k] : cfg.screens.filter((x) => x !== k);
                if (next.length) save({ screens: next });
              }} /> {label}</label>))}
          <button className="btn ghost" onClick={() => save({ chat_id: cfg.chat_id })}>Save chat</button>
          <span className="muted">Today {st.today}: {st.trading_day ? 'trading day' : `no trading (${st.holiday_today || 'weekend'})`}
            {st.upcoming_holidays.length > 0 && ` · next holidays ${st.upcoming_holidays.map((h) => `${h.date} ${h.name}`).join(', ')}`}</span>
        </div>
      </div>

      {kite && (
        <div className="panel">
          <div className="row"><h3>Kite login link</h3>
            <button className="btn" disabled={!st.ready || !kite.api_key_configured}
                    onClick={() => act(api.sendLoginLink, () => 'Login link sent: check Telegram')}>Send login link now</button>
            <span className={`pill ${kite.has_token ? 'ok' : 'bad'}`}>{kite.has_token
              ? `Session until ${kite.expires_at ? kite.expires_at.slice(11, 16) + ' ' + kite.expires_at.slice(5, 10) : '06:00'}` : 'No session'}</span>
          </div>
          <p className="muted small">Kite sessions end at 06:00 IST. The bot sends a one-time <b>Log in to Kite</b> button
            (works once, 15 min) at each time below when Kite doesn't accept a session, checks every 30 min for a
            logged-out session, and answers <code>/login</code> and <code>/status</code>.</p>
          <div className="toggles">
            <div>
              <label className="toggle"><input type="checkbox" checked={cfg.login_links}
                onChange={(e) => save({ login_links: e.target.checked })} /> 🔑 Login links on</label>
              <label className="toggle"><input type="checkbox" checked={cfg.login_every_day}
                onChange={(e) => save({ login_every_day: e.target.checked })} /> Also weekends / holidays</label>
              <label className="toggle"><input type="checkbox" checked={cfg.login_heads_up}
                onChange={(e) => save({ login_heads_up: e.target.checked })} /> ⏰ "Ends at 06:00" heads-up</label>
            </div>
            <div>
              <label className="toggle field">Link times (IST, from 06:00)
                <input style={{ width: '9em' }} value={timesDraft ?? cfg.login_times.join(', ')} onChange={(e) => setTimesDraft(e.target.value)}
                       onBlur={(e) => { setTimesDraft(null); save({ login_times: e.target.value.split(',').map((x) => x.trim()).filter(Boolean) }); }} /></label>
              <label className="toggle field">Heads-up at
                <input value={cfg.login_heads_up_time} onChange={(e) => setCfg({ ...cfg, login_heads_up_time: e.target.value })}
                       onBlur={(e) => save({ login_heads_up_time: e.target.value })} /></label>
            </div>
            <div className="small">
              <div>Kite console redirect URL: <code>{kite.redirect_url}</code></div>
              <div className="muted">Links open on your phone only if this address is reachable from it
                (<code>PUBLIC_BASE_URL</code>: Tailscale or cloud).</div>
              <div>Dashboard password: {kite.password_set ? 'on' : <span className="muted">off (set{' '}
                <code>BP_DASHBOARD_PASSWORD</code> before exposing the app)</span>}</div>
            </div>
          </div>
        </div>)}

      <div className="panel">
        <h3>What to send</h3>
        <div className="toggles">
          {GROUPS.map(([title, items]) => (
            <div key={title}><h4>{title}</h4>
              {items.map(([k, label]) => (
                <label key={k} className="toggle"><input type="checkbox" checked={!!cfg[k]}
                  onChange={(e) => save({ [k]: e.target.checked })} /> {label}</label>))}
            </div>))}
          <div><h4>Decision charts</h4>
            <p className="muted small">Chart image with numbered markers + verdict panel for:</p>
            {CHART_KINDS.map(([k, label]) => (
              <label key={k} className="toggle"><input type="checkbox" checked={cfg.chart_kinds.includes(k)}
                disabled={!cfg.chart_images}
                onChange={(e) => save({ chart_kinds: e.target.checked ? [...cfg.chart_kinds, k]
                  : cfg.chart_kinds.filter((x) => x !== k) })} /> {label}</label>))}
            {SIZE_FIELDS.map(([k, label]) => (
              <label key={k} className="toggle field">{label}
                <input {...inputKind(k)} value={String(cfg[k])} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value })}
                       onBlur={(e) => save({ [k]: e.target.value })} /></label>))}
          </div>
          <div><h4>A/B priority</h4>
            {PRIORITY_FIELDS.map(([k, label]) => (
              <label key={k} className="toggle field">{label}
                <input {...inputKind(k)} value={String(cfg[k])} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value })}
                       onBlur={(e) => save({ [k]: e.target.value })} /></label>))}
            <label className="toggle field">B setups
              <select value={cfg.b_alerts} onChange={(e) => save({ b_alerts: e.target.value })}>
                <option value="digest">digest only</option><option value="push">send (tagged 🅱️)</option>
                <option value="off">off</option></select></label>
            <label className="toggle field">In a correction
              <select value={cfg.correction_mode} onChange={(e) => save({ correction_mode: e.target.value })}>
                <option value="caution">send A with a caution</option><option value="mute_buys">mute new-buy alerts</option>
              </select></label>
            <p className="muted small">Sell-side alerts (below pivot, played out, failed) are always sent.</p>
          </div>
          <div><h4>Timing</h4>
            {FIELDS.map(([k, label]) => (
              <label key={k} className="toggle field">{label}
                <input {...inputKind(k)} value={String(cfg[k])} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value })}
                       onBlur={(e) => save({ [k]: e.target.value })} /></label>))}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="row"><h3>Automatic jobs</h3>
          <button className="btn" onClick={() => act(() => api.runJob('eod'), () => 'Evening job started (sync → scan → alerts)')}>Run evening job now</button>
          <button className="btn ghost" onClick={() => act(() => api.runJob('intraday'), () => 'Intraday check started')}>Intraday check now</button>
          <button className="btn ghost" onClick={() => act(() => api.runJob('close_check'), () => '15:15 close check started')}>Close check now</button>
          <button className="btn ghost" onClick={() => act(() => api.runJob('top_setups'), () => 'Top setups: ranking and sending')}>Top setups now</button>
          <button className="btn ghost" onClick={() => act(() => api.runJob('guard'), () => 'Position guard: syncing holdings and checking')}>Run guard now</button>
          <button className="btn ghost" onClick={() => act(() => api.runJob('weekly'), () => 'Weekly refresh started')}>Weekly refresh now</button>
          <button className="btn ghost" onClick={load}>Refresh</button>
        </div>
        <p className="muted small">Jobs run only while this app is open and the Mac is awake (see README: pmset wake schedule).</p>
        <div className="hscroll"><table className="grid small">
          <thead><tr><th>Job</th><th>Date</th><th>Started</th><th>Status</th><th>Message</th></tr></thead>
          <tbody>{(jobs?.runs || []).map((r) => (
            <tr key={r.id}><td>{r.job}</td><td>{r.run_date}</td><td>{r.started_at.replace('T', ' ').slice(0, 16)}</td>
              <td className={`s-${r.status}`}>{r.status}</td><td className="wrap">{r.message}</td></tr>))}
          </tbody>
        </table></div>
      </div>

      <div className="panel">
        <h3>Sent alerts</h3>
        <div className="hscroll"><table className="grid small">
          <thead><tr><th>Time</th><th>Kind</th><th>Symbol</th><th>OK</th><th>Text</th></tr></thead>
          <tbody>{log.map((r) => (
            <tr key={r.key}><td>{r.sent_at.replace('T', ' ').slice(0, 16)}</td><td>{r.kind}</td><td>{r.symbol}</td>
              <td className={r.ok ? 'up' : 'down'}>{r.ok ? '✓' : `✗ ${r.error || ''}`}</td>
              <td className="wrap">{strip(r.text).slice(0, 220)}</td></tr>))}
          </tbody>
        </table></div>
      </div>
    </>
  );
}
