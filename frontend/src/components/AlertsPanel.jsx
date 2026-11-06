import React, { useCallback, useEffect, useState } from 'react';
import { ampm, clock, stamp } from '../timefmt';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { Head, Chip, Panel, FixedTable, Switch, Tag, KV } from './ui2';

const GROUPS = [
  ['Stage alerts', [['breakout', '🚀 Fresh breakout'], ['below_pivot', '🔻 Back below pivot'], ['played_out', '🏁 Played out'],
    ['forming', '🔵 Newly forming'], ['near_pivot', '🎯 Near pivot'], ['low_volume_breakout', '⚠️ Low-volume breakout'],
    ['left', '▫️ Left a card (noisy)']]],
  ['Delivery', [['enabled', 'Alerts on'], ['digest', '📋 Daily digest'], ['chart_images', '🖼 Chart images'],
    ['system', '⚙️ System alerts (login, job failures, index changes)'], ['intraday', '⚡ Intraday pivot heads-up + failed breakout'],
    ['close_check', '🕒 Act-before-close check (3:15 PM)'],
    ['job_enabled', '🕡 Automatic evening job']]],
  ['Priority', [['tiering', '🅰️ A/B priority (A at once, B per setting)'], ['market_line', '📊 Market line on alerts'],
    ['market_sizing', '⚖️ Market-aware size (½ under pressure, ¼ in correction)']]],
  ['Daily plan', [['top_setups', '🎯 Top setups after the evening scan'], ['morning_plan', '☀️ Morning plan'],
    ['gap_check', '🔔 Opening (gap) check'],
    ['alert_buttons', '🔘 Buttons under alerts (Watch · Mute · Chart · I bought)']]],
  ['Market hours', [['live_guard', '🔻 Live position guard (under stop, heavy selling, 9:10 AM and 3:15 PM checks)'],
    ['hard_stop', '🛑 Hard stop: under-stop alert says "exit now" (off: unconfirmed until the close)'],
    ['entry_checklist', '✅ Entry checklist on heads-ups (zone · VWAP · volume · vs Nifty · opening range)']]],
];
const FIELDS = [['near_pivot_pct', 'Near pivot within (0.03 = 3%)'], ['job_time', 'Evening job (IST)'],
  ['reminder_time', 'Login reminder (IST)'], ['intraday_start', 'Intraday from'], ['intraday_end', 'Intraday until'],
  ['intraday_interval_min', 'Intraday every (min)'], ['close_check_time', 'Close check at'],
  ['intraday_volume_mult', 'Volume needed (× 50-day avg)'], ['failed_buffer_pct', 'Failed below pivot by (0.01 = 1%)'],
  ['close_min_range', 'Close check: min place in day range (0–1)'], ['top_setups_time', 'Top setups at'],
  ['plan_top_n', 'How many top setups'], ['profit_zone_pct', 'Guard: profit zone at (+%)'],
  ['extended_pct', 'Guard: extended above 50-day (%)'], ['failed_days', 'Guard: failed if under pivot within (days)'], ['morning_plan_time', 'Morning plan at'], ['gap_check_time', 'Opening check at'], ['universe_refresh_weekday', 'Weekly refresh day (0=Mon … 6=Sun)'],
  ['live_drop_pct', 'Live guard: heavy selling at (−% today)'], ['live_drop_volume', 'Live guard: … on volume (× normal)'],
  ['opening_range_min', 'Checklist: opening range (first N min)'], ['today_near_pct', 'Today page: near pivot within (%)']];
const CHART_KINDS = [['breakout', '🚀 Breakout'], ['breakout_low_volume', '⚠️ Low-volume breakout'],
  ['near_pivot', '🎯 Near pivot'], ['forming', '🔵 Newly forming'], ['below_pivot', '🔻 Below pivot'],
  ['played_out', '🏁 Played out'], ['climbing', '📈 Climbing'], ['intraday', '⚡ Intraday + 3:15 PM']];
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
  ['intraday', '⚡ Intraday heads-up'], ['failed', '⚠️ Failed breakout'], ['close_check', '🕒 3:15 PM close check'],
  ['digest', '📋 Digest'], ['report', '🌙 Evening report'], ['system', '⚙️ System']];

// Which What-to-send toggle switches a scheduled job on or off (the schedule lists only jobs that are on).
const JOB_TOGGLE = { intraday: 'intraday', close_check: 'close_check', eod: 'job_enabled', top_setups: 'top_setups',
  morning_plan: 'morning_plan', gap_check: 'gap_check', reminder: 'system', filings: 'filings' };
const RUN = [['eod', 'Run evening job now', 'Evening job started (sync → scan → alerts)'], ['intraday', 'Intraday check now', 'Intraday check started'],
  ['close_check', 'Close check now', 'Close check started'], ['top_setups', 'Top setups now', 'Top setups: ranking and sending'],
  ['weekly', 'Weekly refresh now', 'Weekly refresh started']];
const STATUS = { ok: 'ok', failed: 'bad', aborted: 'bad', waiting: 'w', retry: 'w', running: 'w', partial: 'w' };
const split = (label) => { const i = label.indexOf(' ('); return i > 0 && label.endsWith(')') ? [label.slice(0, i), label.slice(i + 2, -1)] : [label, '']; };
const FR = ({ label, desc, children }) => <div className="fr"><div>{label}{desc ? <small>{desc}</small> : null}</div><div style={{ textAlign: 'right' }}>{children}</div></div>;
const Grp = ({ children }) => <div style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.04em', color: 'var(--muted)', padding: '12px 0 2px' }}>{children}</div>;

// Settings + activity for Telegram alerts and the automatic jobs. Every toggle and field saves as it changes.
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
  const sch = useLive('schedule');

  const load = useCallback(async () => {
    try {
      const [s, c, j, l] = await Promise.all([api.alertsStatus(), api.getConfig('alerts'), api.getJobs(), api.alertsLog()]);
      setSt(s); setCfg(c); setJobs(j); setLog(l.rows || l);
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

  if (!st || !cfg) return <div className="rd"><Head title="Alerts & schedule" /><Panel><p className="muted small rempty">{msg || 'Loading…'}</p></Panel></div>;
  const field = (k) => <input {...inputKind(k)} className="mono" value={String(cfg[k])} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value })} onBlur={(e) => save({ [k]: e.target.value })} />;
  const timeHint = (k) => (inputKind(k).type === 'time' ? `= ${ampm(String(cfg[k]))}` : '');
  const sw = (k) => <Switch on={!!cfg[k]} onChange={(v) => save({ [k]: v })} />;
  const jobRows = (sch?.jobs || []).map((j) => ({ key: j.key, title: j.desc, cls: j.skip ? 'dim' : '', cells: [
    `${j.icon} ${j.name}`, j.every ? `every ${j.every} min` : clock(j.at), j.skip ? j.skip : stamp(j.at), j.last ? `${stamp(j.last.at)} · ${j.last.status}` : '—',
    JOB_TOGGLE[j.key] ? <Switch on={!!cfg[JOB_TOGGLE[j.key]]} onChange={(v) => save({ [JOB_TOGGLE[j.key]]: v })} /> : <span className="muted">—</span>] }));
  const sentToday = log.filter((r) => (r.sent_at || '').slice(0, 10) === st.today).length;

  return (
    <div className="rd">
      <Head title="Alerts & schedule" right={<>
        {msg && <span className="msg small">{msg}</span>}
        <button className="btn ghost" disabled={!st.bot_configured} onClick={check}>Check bot</button>
        <button className="btn" disabled={!st.ready} onClick={() => act(api.testAlert, () => 'Test message sent — check Telegram')}>Send test message</button>
        <button className="btn ghost" onClick={load}>Refresh</button></>}>
        <Chip c={st.ready ? 'ok' : 'bad'}>Telegram <b className={st.ready ? 'up' : 'down'}>{st.ready ? 'linked' : 'not set up'}</b> · {sentToday} sent today</Chip>
        <Chip>Today {st.today}: <b>{st.trading_day ? 'trading day' : `no trading (${st.holiday_today || 'weekend'})`}</b></Chip>
        {st.upcoming_holidays.length > 0 && <Chip title={st.upcoming_holidays.map((h) => `${h.date} ${h.name}`).join(', ')}>next holidays {st.upcoming_holidays.map((h) => `${h.date} ${h.name}`).join(', ')}</Chip>}
      </Head>
      <div className="two"><div className="cols">
        <Panel title="Automatic jobs · timing" meta={jobRows.length || null} right={st.scheduler_running ? <span className="up">scheduler running</span> : <span className="down">scheduler stopped</span>}>
          <FixedTable empty="No jobs scheduled (the schedule arrives with the live stream)." rows={jobRows}
            cols={[{ h: 'Job' }, { h: 'When', w: 130, lo: true }, { h: 'Next', w: 150 }, { h: 'Last run', w: 170, lo: true }, { h: 'On', w: 60 }]} />
          <div className="rwrap">{RUN.map(([k, label, ok]) => <button key={k} className={`btn small ${k === 'eod' ? '' : 'ghost'}`} onClick={() => act(() => api.runJob(k), () => ok)}>{label}</button>)}</div>
          <div className="rnote">Jobs run only while this app is open and the Mac is awake (see README: pmset wake schedule).</div>
        </Panel>
        <Panel title="Timing" right="saves as you leave a field">
          <div className="rform">{FIELDS.map(([k, label]) => <FR key={k} label={label} desc={timeHint(k)}>{field(k)}</FR>)}</div>
        </Panel>
        <Panel title="What to send" right={cfg.alert_style === 'summary' ? 'one evening report' : 'every alert as its own message'}>
          <div className="rform">
            <FR label="Alert style" desc={cfg.alert_style === 'summary'
              ? 'The evening scan, guard and top setups arrive as one silent report with 📈 chart and 🧾 details buttons. Market-hours alerts are unchanged.'
              : 'Each stage move, near-pivot name, guard result and top setup is sent as its own message with its chart.'}>
              <select value={cfg.alert_style} onChange={(e) => save({ alert_style: e.target.value })}>
                <option value="summary">Summary: one 7 PM evening report, charts on tap</option>
                <option value="every">Every alert as its own message</option></select></FR>
            {GROUPS.map(([title, items]) => <React.Fragment key={title}><Grp>{title}</Grp>
              {items.map(([k, label]) => { const [l, d] = split(label); return <FR key={k} label={l} desc={d}>{sw(k)}</FR>; })}</React.Fragment>)}
            <Grp>Decision charts</Grp>
            <FR label="Chart image with numbered markers + verdict panel for:" desc={cfg.chart_images ? '' : 'turn Chart images on first'} />
            {CHART_KINDS.map(([k, label]) => <FR key={k} label={label}>
              <Switch on={cfg.chart_kinds.includes(k)} disabled={!cfg.chart_images}
                onChange={(v) => save({ chart_kinds: v ? [...cfg.chart_kinds, k] : cfg.chart_kinds.filter((x) => x !== k) })} /></FR>)}
            {SIZE_FIELDS.map(([k, label]) => { const [l, d] = split(label); return <FR key={k} label={l} desc={d}>{field(k)}</FR>; })}
            <Grp>A/B priority</Grp>
            {PRIORITY_FIELDS.map(([k, label]) => { const [l, d] = split(label); return <FR key={k} label={l} desc={d}>{field(k)}</FR>; })}
            <FR label="B setups">
              <select value={cfg.b_alerts} onChange={(e) => save({ b_alerts: e.target.value })}>
                <option value="digest">digest only</option><option value="push">send (tagged 🅱️)</option><option value="off">off</option></select></FR>
            <FR label="In a correction" desc="Sell-side alerts (below pivot, played out, failed) are always sent.">
              <select value={cfg.correction_mode} onChange={(e) => save({ correction_mode: e.target.value })}>
                <option value="caution">send A with a caution</option><option value="mute_buys">mute new-buy alerts</option></select></FR>
          </div>
        </Panel>
        <Panel title="Job runs" meta={jobs?.runs?.length || null} right="latest first">
          <FixedTable empty="No job has run yet." cols={[{ h: 'Job' }, { h: 'Date', w: 110, lo: true }, { h: 'Started', w: 150 }, { h: 'Status', w: 90 }, { h: 'Message', w: 320 }]}
            rows={(jobs?.runs || []).map((r) => ({ key: r.id, title: r.message, cells: [r.job, r.run_date, stamp(r.started_at), <Tag c={STATUS[r.status] || ''}>{r.status}</Tag>, r.message] }))} />
        </Panel>
        <Panel title="Sent alerts" meta={log.length || null} right="the latest · the Sent alerts page has them all">
          <FixedTable empty="Nothing sent yet." cols={[{ h: 'Text' }, { h: 'Time', w: 150 }, { h: 'Kind', w: 100, lo: true }, { h: 'Stock', w: 110 }, { h: 'OK', w: 60 }]}
            rows={log.map((r) => ({ key: r.key, title: strip(r.text).slice(0, 220), cells: [strip(r.text).slice(0, 220), stamp(r.sent_at), r.kind, r.symbol,
              <span className={r.ok ? 'up' : 'down'} title={r.error || ''}>{r.ok ? '✓' : `✗ ${r.error || ''}`}</span>] }))} />
        </Panel>
      </div><div className="cols">
        <Panel title="Telegram" right={<span className={st.ready ? 'up' : 'down'}>{st.ready ? 'Ready' : 'Not ready'}</span>}>
          <KV pairs={[['Bot token', <span className={st.bot_configured ? 'up' : 'down'}>{st.bot_configured ? 'set' : 'missing'}</span>],
            ['Chat', st.chat_id || <span className="down">not set</span>], ['Alerts', cfg.enabled ? 'on' : 'off'],
            ['Scheduler', st.scheduler_running ? 'running' : 'stopped'],
            ['Chart images', st.charts_available === false ? <span className="warn">matplotlib missing</span> : cfg.chart_images ? 'on' : 'off']]} />
          <div className="rwrap">
            <button className="btn ghost small" disabled={!st.bot_configured}
              onClick={() => act(api.detectChat, (r) => (r.saved ? `Chat ${r.saved} saved`
                : r.chats.length ? `Several chats found: ${r.chats.map((c) => `${c.name} (${c.chat_id})`).join(', ')} — set chat_id below`
                  : r.hint))}>Detect chat ID</button>
            <button className="btn small" disabled={!st.ready} onClick={() => act(api.testAlert, () => 'Test message sent — check Telegram')}>Send test message</button>
            <button className="btn ghost small" disabled={!st.bot_configured} onClick={check}>Check bot</button>
            {bot && (bot.checking ? <span className="muted">checking…</span>
              : bot.ok ? <span className="up">@{bot.username} ✓ token valid{bot.chat_id ? ` · chat ${bot.chat_id}` : ' · no chat yet'}</span>
                : <span className="down">✗ {bot.error}</span>)}
          </div>
          {st.charts_available === false && (
            <div className="rnote warn">Chart images are off: matplotlib isn't installed. Run <code>.venv/bin/pip install -r requirements.txt</code> and restart; alerts are sent as text meanwhile.</div>)}
          {!st.bot_configured && (
            <ol className="muted small" style={{ margin: '4px 14px 8px', paddingLeft: 18 }}>
              <li>In Telegram, message <b>@BotFather</b> → <code>/newbot</code> → copy the token.</li>
              <li>Add <code>TELEGRAM_BOT_TOKEN=…</code> to <code>.env</code> and restart (<code>./start.sh</code>).</li>
              <li>Send your bot any message, then press <b>Detect chat ID</b> and <b>Send test message</b>.</li>
            </ol>)}
          <div className="rform">
            <FR label="chat_id" desc="the chat the bot sends to">
              <span style={{ display: 'inline-flex', gap: 6 }}><input {...inputKind('chat_id')} className="mono" value={cfg.chat_id} onChange={(e) => setCfg({ ...cfg, chat_id: e.target.value })} />
                <button className="btn ghost small" onClick={() => save({ chat_id: cfg.chat_id })}>Save chat</button></span></FR>
            <Grp>Screens alerted</Grp>
            {[['vcp', 'VCP'], ['flag', 'Bull flag'], ['bases', 'Bases']].map(([k, label]) => <FR key={k} label={label}>
              <Switch on={cfg.screens.includes(k)} onChange={(v) => { const next = v ? [...cfg.screens, k] : cfg.screens.filter((x) => x !== k); if (next.length) save({ screens: next }); }} /></FR>)}
          </div>
        </Panel>
        {kite && (
          <Panel title="Kite login link" right={<span className={kite.has_token ? 'up' : 'down'}>{kite.has_token ? `Session until ${kite.expires_at ? stamp(kite.expires_at) : '6:00 AM'}` : 'No session'}</span>}>
            <div className="rnote">Kite sessions end at 6:00 AM IST. The bot sends a one-time <b>Log in to Kite</b> button (works once, 15 min) at each time below when
              Kite doesn't accept a session, checks every 30 min for a logged-out session, and answers <code>/login</code> and <code>/status</code>.</div>
            <div className="rwrap"><button className="btn small" disabled={!st.ready || !kite.api_key_configured}
              onClick={() => act(api.sendLoginLink, () => 'Login link sent: check Telegram')}>Send login link now</button></div>
            <div className="rform">
              <FR label="🔑 Login links on">{sw('login_links')}</FR>
              <FR label="Also weekends / holidays">{sw('login_every_day')}</FR>
              <FR label='⏰ "Ends at 6:00 AM" heads-up'>{sw('login_heads_up')}</FR>
              <FR label="Link times" desc={`IST, from 6:00 AM; 24-hour HH:MM, comma-separated · = ${cfg.login_times.map((t) => ampm(t)).join(', ')}`}>
                <input className="mono" value={timesDraft ?? cfg.login_times.join(', ')} onChange={(e) => setTimesDraft(e.target.value)}
                  onBlur={(e) => { setTimesDraft(null); save({ login_times: e.target.value.split(',').map((x) => x.trim()).filter(Boolean) }); }} /></FR>
              <FR label="Heads-up at" desc={`= ${ampm(cfg.login_heads_up_time)}`}>
                <input className="mono" value={cfg.login_heads_up_time} onChange={(e) => setCfg({ ...cfg, login_heads_up_time: e.target.value })}
                  onBlur={(e) => save({ login_heads_up_time: e.target.value })} /></FR>
            </div>
            <KV pairs={[['Dashboard password', kite.password_set ? 'on' : 'off']]} />
            <div className="rnote">Kite console redirect URL: <code>{kite.redirect_url}</code>. Links open on your phone only if this address is reachable from it (<code>PUBLIC_BASE_URL</code>: Tailscale or cloud).
              {!kite.password_set && <> Set <code>BP_DASHBOARD_PASSWORD</code> before exposing the app.</>}</div>
          </Panel>)}
        <Panel title="Sample alert" right='marked "🧪 TEST"'>
          <div className="rwrap">
            <select value={sampleKind} onChange={(e) => setSampleKind(e.target.value)}>
              {SAMPLES.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
            </select>
            <button className="btn small" disabled={!st.bot_configured || !st.chat_id || busy} onClick={sendSample}>{busy ? 'Sending…' : 'Send sample'}</button>
          </div>
          <div className="rnote">Built from your latest scan (or a built-in example). Works any day, no Kite needed.</div>
          {sampleRes && (sampleRes.error ? <div className="rtg down">✗ {sampleRes.error}</div> : (
            <div className="rtg">🧪 <b>{sampleRes.sent} sent{sampleRes.failed ? `, ${sampleRes.failed} failed` : ''}</b> to chat {sampleRes.chat_id}<br />
              {sampleRes.results.map((r) => <Tag key={r.kind} c={r.ok ? 'ok' : 'w'} title={r.error || ''}>{r.ok ? '✓' : '✗'} {r.kind}{r.chart ? ' 🖼' : ''}</Tag>)}
              {sampleRes.results.find((r) => r.error) && <div className="down small">{sampleRes.results.find((r) => r.error).error}</div>}
            </div>))}
        </Panel>
      </div></div>
    </div>
  );
}
