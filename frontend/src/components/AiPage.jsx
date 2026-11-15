import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp, ampm } from '../timefmt';
import { useLive } from '../services/live';
import { Head, Chip, Switch, Tag, Panel, FixedTable, Act, Cards, Card } from './ui2';
import { useNarrow, useIsPhone } from '../useMedia';

// 🤖 System › AI: a local model next to the app that reads and explains and never trades. Everything here reads
// GET /api/ai (stream topic "ai"); the switches, the full stop, the model downloads and the test prompt act through
// the /api/ai routes. PR 1 is the infrastructure only: switches, server, models, recent jobs, a test prompt.
const ROW = { display: 'flex', gap: 10, alignItems: 'center', fontSize: 12.5, minHeight: 34, padding: '0 14px', borderBottom: '1px solid var(--rowline)', minWidth: 0 };
const LINE = { padding: '10px 14px 0', fontSize: 12.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
const COL = { gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' };
const Meta = ({ children, title }) => <span title={title} style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{children}</span>;
const Ell = ({ children, title, c = '' }) => <span className={c} title={title} style={{ display: 'block', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}>{children}</span>;
const n1 = (v) => (v == null ? '—' : Number(v).toFixed(1));
const n0 = (v) => (v == null ? '—' : Math.round(Number(v)).toLocaleString('en-IN'));
const secs = (ms) => (ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
// "qwen2.5:7b-instruct" -> "Qwen 2.5 7B" on the chips; anything else is shown as it is
const pretty = (name) => { const m = /^qwen(\d(?:\.\d)?):(\d+)b/i.exec(name || ''); return m ? `Qwen ${m[1]} ${m[2]}B` : name || '—'; };
const STATUS_TAG = { done: 'ok', failed: 'bad', running: 'a', queued: '', skipped: 'w' };

// A two-part meter (app · AI) out of the box's total; a fixed height, never wider than its panel.
const Gauge = ({ a, b, total, ca = 'var(--blue)', cb = 'var(--ok)' }) => {
  const p = (v) => (total ? Math.max(0, Math.min(100, (100 * (v || 0)) / total)) : 0);
  return <div style={{ height: 10, borderRadius: 5, background: 'var(--hover)', overflow: 'hidden', margin: '6px 14px 0', display: 'flex' }}>
    <i style={{ display: 'block', height: '100%', width: `${p(a)}%`, background: ca }} /><i style={{ display: 'block', height: '100%', width: `${p(b)}%`, background: cb }} /></div>;
};

export default function AiPage() {
  const live = useLive('ai');
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState('');
  const narrow = useNarrow();
  const phone = useIsPhone();
  const reload = () => setTick((t) => t + 1);
  useEffect(() => { api.getAi().then((x) => { setD(x); setErr(''); }).catch((e) => setErr(e.message)); }, [tick]);
  useEffect(() => { if (live && !live.error) setD(live); }, [live]);
  const act = (key, p) => { setBusy(key); p.then(() => { setErr(''); reload(); }).catch((e) => setErr(e.message)).finally(() => setBusy('')); };
  const setSw = (body, text) => { if (!text || window.confirm(text)) act('sw', api.putAiSettings(body)); };
  const STOP_TEXT = 'Full stop now?\n\nEvery loaded model is unloaded (the RAM and CPU are free at once) and every queued job is marked skipped. The container itself keeps idling at ~50 MB. Nothing runs again until you start it here or with Telegram “ai start”.';
  const START_TEXT = 'Clear the full stop?\n\nThe switches decide what runs from here; no model is loaded until the next job.';
  const fullStop = (on) => { if (window.confirm(on ? STOP_TEXT : START_TEXT)) act('stop', on ? api.aiStop() : api.aiStart()); };
  const pull = () => { if (window.confirm(`Download the missing models?\n\n${[d.models.night, d.models.day].filter((m) => !m.present).map((m) => m.name).join(' and ')} are pulled into data/ai in the background (a few GB; some minutes on the server). The page shows the progress; nothing else changes.`)) act('pull', api.aiPull()); };
  const test = () => act('test', api.aiTest());
  if (!d) return <div className="rd"><Panel>{err ? <div className="rbanner bad">{err}</div> : <p className="muted small rempty">Loading…</p>}</Panel></div>;

  const sw = d.switches || {}, S = d.server || {}, M = d.models || {}, Q = d.queue || {}, B = d.breaker || {}, st = d.settings || {}, L = d.limits || {}, P = M.pull || {}, T = d.last_test;
  const state = sw.stopped ? 'stopped' : !d.url_ok ? 'service unreachable' : Q.job ? 'running' : !sw.enabled ? 'off' : 'idle';
  const dot = sw.stopped || !d.url_ok ? 'bad' : Q.job ? 'ok' : sw.enabled ? 'ok' : 'warn';
  const aiGb = (S.ai_ram_mb || 0) / 1024;
  const used = S.ram_total_gb != null && S.ram_free_gb != null ? Math.max(0, S.ram_total_gb - S.ram_free_gb) : null;
  const appGb = used != null ? Math.max(0, used - aiGb) : null;
  const loaded = M.loaded || [];
  const bothPresent = !!(M.night?.present && M.day?.present);
  const diskGb = (M.night?.size_gb || 0) + (M.day?.size_gb || 0);
  const nPresent = [M.night, M.day].filter((m) => m?.present).length;
  const jobs = d.jobs || [];

  const jobCols = [{ h: 'Job', ell: true }, { h: 'Time', w: 142 }, { h: 'Model', w: 150, lo: true }, { h: 'Took', w: 70, n: true }, { h: 'Status', w: 78 }, { h: 'Error', w: narrow ? 200 : 320, ell: true, lo: true }];
  const jobRows = jobs.map((j) => ({ key: j.id, title: `#${j.id} · ${j.kind} · ${j.key} · ${j.status}${j.note ? ` · ${j.note}` : ''}${j.tokens ? ` · ${j.tokens} tokens` : ''}${j.error ? ` · ${j.error}` : ''}`,
    cells: [<><b>{j.kind}</b> <small className="muted">· {j.key}</small></>, <span className="mono">{stamp(j.finished || j.started || j.created)}</span>, <span className="muted">{j.model || '—'}</span>,
      <span className="mono">{secs(j.ms)}</span>, <Tag c={STATUS_TAG[j.status] ?? ''}>{j.status}</Tag>, <Ell title={j.error || ''} c={j.error ? 'down' : 'muted'}>{j.error || (j.note || '—')}</Ell>] }));

  return <div className="rd">
    {err && <div className="rbanner bad">{err}</div>}
    {!d.url_ok && !err && <div className="rbanner warn">AI service unreachable at <span className="mono">{d.url}</span> · it is the <span className="mono">ollama</span> service in docker compose (<span className="mono">./docker.sh update</span> starts it); nothing on this page runs until it answers. {M.error}</div>}
    <Head title={phone ? '🤖 Local AI' : '🤖 Local AI · reads and explains · never trades'}>
      <Chip>runs on this server · nothing leaves it · free</Chip>
      <Chip title={`${M.night?.name || st.night_model} (night) · ${M.day?.name || st.day_model} (day) · settings → Local AI`}>night model <b>{pretty(M.night?.name || st.night_model)}</b> · day model <b>{pretty(M.day?.name || st.day_model)}</b></Chip>
      <Chip dot={dot} c={dot === 'bad' ? 'bad' : ''} title={d.allowed_note}>{state} · {loaded.length ? `${loaded.length} model${loaded.length === 1 ? '' : 's'} loaded` : 'model unloaded'} · <b className="mono">{n0(S.ai_ram_mb || 0)} MB</b> in use</Chip>
      <Chip title="the AI container's limits in docker-compose.yml · the app keeps the rest">limits <b>{L.cpus} cores · {L.mem_gb} GB</b> · low CPU priority next to the app</Chip>
      {d.url_ok && d.version && <Chip title={d.url}>service <b>v{d.version}</b></Chip>}
    </Head>
    <div className="cols c3">
      <Panel title="Switches" meta="all AI work">
        <div style={ROW}><Switch on={sw.enabled} disabled={!!busy || (!sw.enabled && !d.can_enable)} title={!sw.enabled && !d.can_enable ? d.enable_note : undefined}
          onChange={(on) => setSw({ enabled: on }, on ? 'Turn the AI on?\n\nNight notes run after the evening job with the night model; day answers use the day model only. It reads and explains: it never places an order, never changes a setting and never sends anything off this server.' : 'Turn the AI off?\n\nNothing new runs; a running job finishes. The models stay on disk.')} />
          <b className={!sw.enabled && !d.can_enable ? 'muted' : ''}>AI enabled</b><Meta title={d.enable_note || ''}><span className="muted">{d.can_enable || sw.enabled ? 'night notes · day answers' : d.enable_note}</span></Meta></div>
        <div style={ROW}><Switch on={sw.night_batch} disabled={!!busy} onChange={(on) => setSw({ night_batch: on }, on ? `Turn the night batch on?\n\nNight work runs after the evening job until ${ampm(st.night_end)} with the night model (${st.night_model}).` : 'Turn the night batch off?\n\nNo night work; day answers are not affected.')} />
          <b>Night batch</b><Meta><span className="muted">after the evening job → {ampm(st.night_end)} · {pretty(st.night_model)}</span></Meta></div>
        <div style={ROW}><Switch on={sw.day_answers} disabled={!!busy} onChange={(on) => setSw({ day_answers: on }, on ? `Turn day answers on?\n\n9:00 AM–3:40 PM the day model (${st.day_model}) only; never on the trading path.` : 'Turn day answers off?\n\nNo answers while the market is open; the night batch is not affected.')} />
          <b>Day answers</b><Meta title={`${st.day_model} only · 9:00 AM–3:40 PM · never on the trading path`}><span className="muted">{pretty(st.day_model)} only · 9:00 AM–3:40 PM · never on the trading path</span></Meta></div>
        <div style={ROW}><Switch red on={!!sw.stopped} disabled={!!busy} onChange={fullStop} /><b>Full stop</b><Meta title="unloads the model, empties the queue, frees RAM and CPU at once · nothing else changes"><span className="muted">unloads the model, empties the queue, frees RAM and CPU at once · nothing else changes</span></Meta></div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 14px 4px', minWidth: 0 }}>
          <button type="button" className="btn small" style={sw.stopped ? undefined : { color: 'var(--bad)', borderColor: 'var(--bad)', background: 'var(--bad-bg)' }} disabled={!!busy} onClick={() => fullStop(!sw.stopped)}>
            {busy === 'stop' ? 'Working…' : sw.stopped ? '▶ Start again…' : '⏹ Full stop now…'}</button>
          <Meta>asks to confirm · Telegram: <b>ai stop</b> / <b>ai start</b></Meta></div>
        <div className="rnote">{d.container_note}</div>
      </Panel>
      <Panel title="Server" meta={d.url_ok ? 'live' : 'the box'} right={<span className={d.allowed ? 'up' : 'muted'}>{d.allowed ? '✓ AI work allowed now' : `✗ ${d.allowed_note || 'not allowed now'}`}</span>}>
        <div style={LINE} title={`${n1(used)} GB used of ${n1(S.ram_total_gb)} GB · free ${n1(S.ram_free_gb)} GB · AI ${n0(S.ai_ram_mb || 0)} MB · app + system ${n1(appGb)} GB`}>
          RAM <b className="mono">{n1(used)} GB</b> of {n1(S.ram_total_gb)} GB · AI <b className="mono">{n0(S.ai_ram_mb || 0)} MB</b> {loaded.length ? '' : '(unloaded)'} · app <b className="mono">{n1(appGb)} GB</b></div>
        <Gauge a={appGb} b={aiGb} total={S.ram_total_gb} />
        <div style={LINE} title={`CPU ${S.cpu_pct == null ? 'not measured yet (needs two reads)' : `${S.cpu_pct}%`} · load (1 min) ${S.load1 ?? '—'} · the scheduler's last tick ${S.app_tick_ms == null ? 'not measured yet' : `${S.app_tick_ms} ms`}`}>
          CPU <b className="mono">{S.cpu_pct == null ? '—' : `${S.cpu_pct}%`}</b> · load <b className="mono">{S.load1 ?? '—'}</b> · app tick <b className="mono">{S.app_tick_ms == null ? '—' : `${n0(S.app_tick_ms)} ms`}</b></div>
        <Gauge a={S.cpu_pct} b={0} total={100} ca={S.cpu_pct >= 80 ? 'var(--bad)' : 'var(--blue)'} />
        <div className="rnote" style={{ paddingTop: 8 }}>guard: AI pauses itself if the app's tick takes &gt; {st.slow_tick_s} s or free RAM &lt; {st.min_free_ram_gb} GB · breaker: {st.breaker_failures} failures → off for {st.breaker_minutes} min · breaker now <b className={B.open ? 'down' : ''}>{B.text || 'closed'}</b>{B.failures ? ` · ${B.failures} failure${B.failures === 1 ? '' : 's'} in a row` : ''} · one call at most {st.timeout_s} s · the model stays loaded {st.keep_alive} after a call</div>
      </Panel>
      <Panel title="Models" meta={`${nPresent} of 2 on disk`} right={<Meta>{d.url_ok ? `service v${d.version || '?'}` : 'service unreachable'}</Meta>}>
        {[['Night', M.night, 'the evening and night work'], ['Day', M.day, 'quick answers while the market is open']].map(([label, m, what]) => <Act key={label} icon={m?.present ? '✓' : '○'} cls={m?.present ? 'up' : 'warn'}
          text={<><b>{label}</b> · {m?.name || '—'} <small className="muted">· {what}</small></>} meta={m?.present ? `present · ${n1(m.size_gb)} GB` : 'not downloaded'} />)}
        <div style={LINE} title={loaded.length ? loaded.map((m) => `${m.name} · ${n0(m.ram_mb)} MB`).join(' · ') : 'no model loaded now'}>
          loaded now: {loaded.length ? loaded.map((m) => <span key={m.name}><b>{pretty(m.name)}</b> <span className="mono">{n0(m.ram_mb)} MB</span> </span>) : <span className="muted">none · a model loads on the first job and stays {st.keep_alive}</span>}</div>
        {P.running && <div style={{ padding: '10px 14px 0' }}>
          <div style={{ fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>downloading <b>{P.model || '…'}</b> · <span className="mono">{Math.round(P.pct || 0)}%</span>{P.done?.length ? ` · done: ${P.done.join(', ')}` : ''}</div>
          <Gauge a={P.pct} b={0} total={100} /></div>}
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 14px 4px', minWidth: 0 }}>
          <button type="button" className="btn small" disabled={!!busy || P.running || bothPresent || !d.url_ok} onClick={pull}
            title={bothPresent ? 'both models are on disk' : !d.url_ok ? 'the service does not answer' : 'download the missing models into data/ai in the background'}>{P.running ? 'Downloading…' : bothPresent ? 'Both present ✓' : 'Download models'}</button>
          <Meta title={P.error || ''}>{P.error ? <span className="down">download failed: {P.error}</span> : P.at && !P.running ? <span className="muted">last download {stamp(P.at)}{P.done?.length ? ` · ${P.done.length} model${P.done.length === 1 ? '' : 's'}` : ''}</span> : <span className="muted">models live in data/ai · they survive rebuilds</span>}</Meta></div>
        <div className="rnote">{M.error ? <span className="down">{M.error}</span> : `${bothPresent ? 'both present · the AI enabled switch may go on' : d.enable_note || ''} · model names in Settings → Local AI`}</div>
      </Panel>
    </div>
    <Cards n={4}>
      <Card unit="n" label="Jobs · today" value={n0(Q.done_today)} num={Q.done_today} k={Q.done_today ? '' : 'muted'} sub={`${Q.queued || 0} queued · ${Q.running || 0} running`}
        fact={Q.job ? <>running <b>{Q.job.kind}</b> · {Q.job.s} s</> : <span className="muted">nothing running</span>} title={`done today ${Q.done_today} · failed today ${Q.failed_today} · queue ${Q.queued} · running ${Q.running}`} />
      <Card unit="n" label="Failures · today" value={n0(Q.failed_today)} num={Q.failed_today} k={Q.failed_today ? 'down' : 'muted'} sub={`breaker ${B.text || 'closed'}`}
        fact={<>{B.open ? <b className="down">open</b> : 'closed'} · {B.failures || 0} in a row · trips at {st.breaker_failures}</>} title={`the breaker opens for ${st.breaker_minutes} min after ${st.breaker_failures} failures in a row; one success closes it`} />
      <Card unit="n" label="Model RAM in use" value={`${n0(S.ai_ram_mb || 0)} MB`} num={S.ai_ram_mb || 0} k={S.ai_ram_mb ? '' : 'muted'} sub={loaded.length ? loaded.map((m) => pretty(m.name)).join(' · ') : 'model unloaded'}
        fact={<>free RAM <b className="mono">{n1(S.ram_free_gb)} GB</b> · floor {st.min_free_ram_gb} GB</>} title={`the loaded models' RAM (from the service) · free RAM ${n1(S.ram_free_gb)} GB of ${n1(S.ram_total_gb)} GB`} />
      <Card unit="n" label="Models on disk" value={nPresent ? `${n1(diskGb)} GB` : '—'} num={diskGb} k={nPresent ? '' : 'muted'} sub={`${nPresent} of 2 downloaded`}
        fact={<span className="muted">{d.notes_count ? `notes ${n0(d.notes_count)} · never deleted` : 'data/ai · survive rebuilds'}</span>} title={`${M.night?.name}: ${M.night?.present ? `${n1(M.night.size_gb)} GB` : 'missing'} · ${M.day?.name}: ${M.day?.present ? `${n1(M.day.size_gb)} GB` : 'missing'}`} />
    </Cards>
    <div className="two">
      <div className="rd" style={COL}>
        <Panel title="Recent jobs" meta={jobs.length || null} right={<Meta>every job stays in the database · newest first</Meta>}>
          <FixedTable cols={jobCols} rows={jobRows} empty="No AI job yet · the first comes from a test prompt or the night batch." />
        </Panel>
      </div>
      <div className="rd" style={COL}>
        <Panel title="Test prompt" meta={pretty(st.day_model)} right={<Meta>a tiny JSON prompt through the queue</Meta>}>
          {T ? <Act icon={T.ok ? '✓' : '✗'} cls={T.ok ? 'up' : 'down'} text={<><Tag c={T.ok ? 'ok' : 'bad'}>{T.ok ? 'ok' : T.status || 'failed'}</Tag> <span className="mono" title={T.text || ''}>{T.text || '—'}</span></>}
            meta={`${stamp(T.at)} · ${secs(T.ms)} · ${T.model || st.day_model}${T.error ? ` · ${T.error}` : ''}`} />
            : <p className="muted small rempty">No test yet · the day model answers {'{"ok": true, "word": "banana"}'} in 5–10 s once it is loaded.</p>}
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '8px 14px 10px', minWidth: 0 }}>
            <button type="button" className="btn small" disabled={!!busy || !d.url_ok || !!sw.stopped} onClick={test} title={!d.url_ok ? 'the service does not answer' : sw.stopped ? 'full stop is on' : 'send the test prompt now'}>{busy === 'test' ? 'Asking…' : 'Send a test prompt'}</button>
            <Meta>runs under the same guard as every job</Meta></div>
        </Panel>
        <Panel title="What it never does">
          {(d.never || []).map((x) => <Act key={x} icon="✗" cls="down" text={x} />)}
        </Panel>
      </div>
    </div>
    <div className="rnote">A local model in the container next to the app: it reads what the app already has and writes notes; it never decides or acts. Nothing leaves this server. Times are IST.</div>
  </div>;
}
