import React, { useEffect, useMemo, useRef, useState } from 'react';
import * as api from '../services/api';
import { useLive, push } from '../services/live';
import { cssVar, useTheme } from '../theme';
import { useIsPhone } from '../useMedia';
import { ampm } from '../timefmt';
import { Head, Chip, Panel, FixedTable, Cards, Card, Seg, Switch, Tag, AiTag } from './ui2';

// ₿ BTC pivot patterns · ALERTS ONLY (nothing here places, sizes or books an order). Delta Exchange candles (public,
// no key), the previous day's pivot levels, 16 candle patterns at S1–S3 / R1–R3, a Telegram line with a chart
// snapshot. Everything reads GET /api/crypto (stream topic "crypto") and GET /api/crypto/candles; the inputs save to
// the config section "crypto" (Settings › crypto shows the same keys). Times are 12-hour IST.

let LW = null;
const loadLW = () => import('lightweight-charts').then((m) => { LW = m; return m; });

const TFS = [['1m', '1m'], ['5m', '5m'], ['15m', '15m'], ['1h', '1h']];
const RANGES = [['1d', '1 d'], ['3d', '3 d'], ['2w', '2 w']];
const TF_WORD = { '1m': '1 min', '5m': '5 min', '15m': '15 min', '1h': '1 h' };
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const WEEK = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const SHOWN = ['P', 'R1', 'R2', 'R3', 'R4', 'R5', 'S1', 'S2', 'S3', 'S4', 'S5'];
const px = (v) => (v == null ? '—' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 }));
// the chart's times are unix + the IST offset (the axis reads IST); read them back with the UTC getters
const t12 = (d) => ampm(`${d.getUTCHours()}:${String(d.getUTCMinutes()).padStart(2, '0')}`);
const tickFmt = (time, type) => { const d = new Date(time * 1000); return type < 3 ? `${d.getUTCDate()} ${MON[d.getUTCMonth()]}` : t12(d); };
const fullFmt = (time) => { const d = new Date(time * 1000); return `${DAY[d.getUTCDay()]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${t12(d)}`; };
const stateWord = (s) => (s.state === 'dropped' ? `dropped: ${s.why || ''}` : s.state === 'sent' ? (s.sent_label ? `sent ${s.sent_label}` : `sent · ${s.why || 'not delivered'}`) : s.state_word);
const STATE_C = { sent: 'ok', quiet: 'w', mark: '', confirm_wait: 'a', dropped: 'bad' };
const muted = (s) => s.state !== 'sent' && s.state !== 'confirm_wait';
const filters = (s) => [s.ema_ok == null ? 'EMA —' : `EMA ${s.ema_len || ''} ${s.ema_ok ? '✓' : '✗'}`, s.vol_x != null ? `vol ${s.vol_x}× ${s.vol_ok ? '✓' : '✗'}` : null].filter(Boolean).join(' · ');

function palette() {
  return { bg: cssVar('--panel'), text: cssVar('--text'), grid: cssVar('--grid'), border: cssVar('--border'), up: cssVar('--ok') || '#1f9d61',
    down: cssVar('--bad') || '#d64545', blue: cssVar('--blue') || '#2a63d4', muted: cssVar('--muted') || '#9aa3ad', level: '#e08a2e', ema: '#5b6cff' };
}

// The chart: candles, the EMA, the pivot levels per period (a gap at each new period), the markers, the selected
// detection's entry / stop / 1R lines. Fixed height; the tooltip shows the detection under the cursor.
function BtcChart({ data, sel, onPick }) {
  const box = useRef(null);
  const h = useRef(null);
  const theme = useTheme();
  const [lw, setLw] = useState(LW);
  const [tip, setTip] = useState(null);
  useEffect(() => { if (!lw) loadLW().then(setLw).catch(() => {}); }, [lw]);
  useEffect(() => {
    if (!lw || !box.current) return undefined;
    const { createChart, createSeriesMarkers, CandlestickSeries, LineSeries, LineStyle } = lw;
    const pal = palette();
    const chart = createChart(box.current, {
      autoSize: true, layout: { background: { color: pal.bg }, textColor: pal.text, fontSize: 11 },
      grid: { vertLines: { color: pal.grid }, horzLines: { color: pal.grid } },
      timeScale: { borderColor: pal.border, timeVisible: true, secondsVisible: false, tickMarkFormatter: tickFmt, rightOffset: 4 },
      rightPriceScale: { borderColor: pal.border }, localization: { timeFormatter: fullFmt },
    });
    const candles = chart.addSeries(CandlestickSeries, { upColor: pal.up, downColor: pal.down, borderVisible: false, wickUpColor: pal.up, wickDownColor: pal.down });
    const ema = chart.addSeries(LineSeries, { color: pal.ema, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    const levels = Object.fromEntries(SHOWN.map((n) => [n, chart.addSeries(LineSeries, {
      color: n === 'P' ? pal.muted : pal.level, lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      autoscaleInfoProvider: () => null })]));                          // the price scale follows the candles, not R5 / S5
    const markers = createSeriesMarkers(candles, []);
    h.current = { chart, candles, ema, levels, markers, pal, lines: [], LineStyle, sigs: [] };
    chart.subscribeCrosshairMove((p) => {
      const s = p?.time != null ? (h.current?.sigs || []).find((x) => x.mt === p.time) : null;
      setTip(s && p.point ? { x: p.point.x, y: p.point.y, s } : null);
    });
    chart.subscribeClick((p) => { const s = p?.time != null ? (h.current?.sigs || []).find((x) => x.mt === p.time) : null; if (s) onPick(s.id); });
    return () => { h.current = null; chart.remove(); };
  }, [lw, theme]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const c = h.current;
    if (!c || !data) return;
    const off = data.offset || 0;
    c.candles.setData(data.candles);
    c.ema.setData(data.ema);
    const segOf = (t) => { let k = -1; data.pivots.forEach((s, i) => { if (t >= s.start) k = i; }); return k; };
    const firsts = new Set(data.pivots.map((s) => s.start));
    SHOWN.forEach((n) => c.levels[n].setData(data.candles.map((b, i) => {
      const k = segOf(b.time);
      const v = k >= 0 ? data.pivots[k].levels[n] : undefined;
      const nxt = data.candles[i + 1];                                   // the line into a new period is drawn clear (no vertical jump)
      return v == null ? { time: b.time } : { time: b.time, value: v, ...(nxt && firsts.has(nxt.time) ? { color: 'rgba(0,0,0,0)' } : {}) };
    })));
    c.lines.forEach((l) => c.candles.removePriceLine(l));
    c.lines = [];
    const lastSeg = data.pivots[data.pivots.length - 1];
    if (lastSeg) ['P', 'R1', 'R2', 'R3', 'S1', 'S2', 'S3'].forEach((n) => { if (lastSeg.levels[n] != null) c.lines.push(c.candles.createPriceLine({ price: lastSeg.levels[n], color: n === 'P' ? c.pal.muted : c.pal.level, lineVisible: false, axisLabelVisible: true, title: n })); });
    const times = new Set(data.candles.map((b) => b.time));
    // every formed pattern gets a marker: sent / confirm wait full colour, quiet / mark-only faded, filtered (dropped) grey
    c.sigs = data.signals.map((s) => ({ ...s, mt: s.t + off })).filter((s) => times.has(s.mt));
    const rank = (s) => (s.id === sel ? 0 : s.state === 'dropped' ? 3 : muted(s) ? 2 : 1);
    const best = new Map();
    c.sigs.forEach((s) => { const k = `${s.mt}${s.dir}`; const o = best.get(k); if (!o || rank(s) < rank(o)) best.set(k, s); });
    c.markers.setMarkers([...best.values()].sort((a, b) => a.mt - b.mt).map((s) => ({
      time: s.mt, position: s.dir === 'long' ? 'belowBar' : 'aboveBar', shape: 'circle',
      size: s.id === sel ? 1.6 : s.state === 'dropped' ? 0.7 : 1,
      color: s.state === 'dropped' ? `${c.pal.muted}99` : muted(s) ? `${s.dir === 'long' ? c.pal.up : c.pal.down}66` : (s.dir === 'long' ? c.pal.up : c.pal.down) })));
    const s = data.signals.find((x) => x.id === sel);
    if (s) {
      [[s.entry, c.pal.blue, 'entry'], [s.stop, c.pal.down, 'stop'], [s.r1, c.pal.up, '1R']].forEach(([p, color, title]) => c.lines.push(c.candles.createPriceLine({ price: p, color, lineWidth: 1, lineStyle: c.LineStyle.Solid, axisLabelVisible: true, title })));
      const step = data.candles.length > 1 ? data.candles[1].time - data.candles[0].time : 300;
      try { c.chart.timeScale().setVisibleRange({ from: s.t + off - 45 * step, to: s.t + off + 25 * step }); } catch { /* outside the data */ }
    } else if (data.candles.length) {
      c.chart.timeScale().fitContent();
    }
  }, [data, sel, lw, theme]);

  return <div className="btc-chart" ref={box}>
    {data && !data.candles.length && <div className="btc-empty muted small">No {TF_WORD[data.tf] || data.tf} candles stored for this range yet.</div>}
    {tip && <div className="btc-tip" style={{ left: Math.min(tip.x + 12, (box.current?.clientWidth || 300) - 250), top: Math.max(4, tip.y - 44) }}>
      <b>{tip.s.label}</b> · {tip.s.level} {px(tip.s.level_px)} · stop {px(tip.s.stop)} · 1R {px(tip.s.r1)}<br /><span className="muted">{tip.s.when} · {stateWord(tip.s)}</span></div>}
  </div>;
}

// The inputs (right; stacked under on a phone). Save → PUT /api/config/crypto; Reset fills the defaults (then Save).
const G = ({ children }) => <div className="g">{children}</div>;
const K = ({ l, title, children }) => <div className="k" title={title}><span>{l}</span><span className="c">{children}</span></div>;

function Inputs({ d, onSaved }) {
  const [draft, setDraft] = useState(null);
  const [msg, setMsg] = useState('');
  const saved = d?.settings;
  useEffect(() => { if (saved && !draft) setDraft(saved); }, [saved, draft]);
  if (!draft) return <Panel title="Inputs"><p className="muted small rempty">Loading…</p></Panel>;
  const set = (k, v) => setDraft({ ...draft, [k]: v });
  const changed = Object.keys(draft).filter((k) => JSON.stringify(draft[k]) !== JSON.stringify(saved[k]));
  const num = (k, w) => <input className="btc-in" style={w ? { width: w } : undefined} value={String(draft[k])} onChange={(e) => set(k, e.target.value)} />;
  const sw = (k) => <Switch on={!!draft[k]} onChange={(v) => set(k, v)} />;
  const days = (k) => <span className="btc-days">{WEEK.map((x) => <button key={x} type="button" className={draft[k].includes(x) ? 'on' : ''}
    onClick={() => set(k, draft[k].includes(x) ? draft[k].filter((y) => y !== x) : WEEK.filter((y) => y === x || draft[k].includes(y)))}>{x[0].toUpperCase() + x[1]}</button>)}</span>;
  const save = async () => {
    const vals = Object.fromEntries(changed.map((k) => [k, draft[k]]));
    try { const v = await api.putConfig('crypto', vals); setDraft(v); setMsg('Saved'); onSaved(); } catch (e) { setMsg(e.message); }
  };
  const pats = d.patterns || [];
  return <Panel title="Inputs" right={changed.length ? <span className="warn">{changed.length} unsaved</span> : 'saved to config · crypto'} className="btc-set">
    <div className="rnote">alerts only · each change applies from the next closed candle · Settings › crypto has the same keys</div>
    <G>Data</G>
    <K l="feed on" title="the Delta Exchange feed runs 24×7 (crypto.enabled)">{sw('enabled')}</K>
    <K l="symbol">{num('symbol')}</K>
    <K l="timeframe"><Seg small opts={TFS} v={draft.tf} set={(v) => set('tf', v)} /></K>
    <K l="history (days)" title="backfill depth at the first start">{num('history_days')}</K>
    <K l="scan history (days)" title="patterns of the last N days marked on the chart after the backfill and on Scan history · nothing sent">{num('scan_days')}</K>
    <K l="archived after (days)" title="older candles and detections MOVE to data/archive.db; nothing is deleted">{num('archive_days')}</K>
    <G>Pivots</G>
    <K l="type"><select className="btc-sel" value={draft.pivot_type} onChange={(e) => set('pivot_type', e.target.value)}>
      {['traditional', 'fibonacci', 'woodie', 'classic', 'camarilla', 'dm'].map((x) => <option key={x} value={x}>{x === 'dm' ? 'DM' : x[0].toUpperCase() + x.slice(1)}</option>)}</select></K>
    <K l="pivots timeframe" title="auto: daily for 15 min and under, weekly above"><select className="btc-sel" value={draft.pivot_tf} onChange={(e) => set('pivot_tf', e.target.value)}>
      {['auto', 'daily', 'weekly', 'monthly'].map((x) => <option key={x} value={x}>{x}</option>)}</select></K>
    <K l="day boundary" title="utc: Delta's day (5:30 AM IST), as TradingView · ist: midnight IST"><Seg small opts={[['utc', 'UTC'], ['ist', 'IST']]} v={draft.day_boundary} set={(v) => set('day_boundary', v)} /></K>
    <K l="use P as a level">{sw('use_p')}</K>
    <G>Patterns · off | mark | alert · confirm</G>
    {pats.map((p) => <div key={p.key} className="k pat"><span><i className={p.dir === 'long' ? 'up' : 'down'}>●</i> {p.label}</span>
      <span className="c"><Seg small opts={[['off', 'off'], ['mark', 'mark'], ['alert', 'alert']]} v={draft[p.key]} set={(v) => set(p.key, v)} />
        <span className="cf">{p.confirm ? <Switch on={!!draft[`confirm_${p.key}`]} onChange={(v) => set(`confirm_${p.key}`, v)} title="wait for the next candle to close in the direction" /> : null}</span></span></div>)}
    <G>Thresholds</G>
    <K l="long wick vs body (×)">{num('wick_mult')}</K>
    <K l="long wick min share of range">{num('wick_min')}</K>
    <K l="opposite wick max share">{num('opp_wick_max')}</K>
    <K l="body zone">{num('body_zone')}</K>
    <K l="doji body max share">{num('doji_body')}</K>
    <K l="doji long wick min share">{num('doji_wick')}</K>
    <K l="star middle body (× first)">{num('star_body')}</K>
    <K l="equal-low tolerance %">{num('eq_tol_pct')}</K>
    <K l="touch (points)">{num('touch_points')}</K>
    <G>Filters</G>
    <K l="trend filter · EMA">{sw('ema_on')}</K>
    <K l="EMA length">{num('ema_len')}</K>
    <K l="volume filter">{sw('vol_on')}</K>
    <K l="volume avg length">{num('vol_len')}</K>
    <K l="volume × average">{num('vol_mult')}</K>
    <K l="prior 3-candle trend">{sw('trend_on')}</K>
    <K l="days (IST)">{days('days')}</K>
    <G>Alerts</G>
    <K l="alert from (IST)" title={`24-hour HH:MM · ${ampm(String(draft.alert_from))}`}>{num('alert_from')}</K>
    <K l="alert until (IST)" title={`24-hour HH:MM · ${ampm(String(draft.alert_until))}`}>{num('alert_until')}</K>
    <K l="alert days">{days('alert_days')}</K>
    <K l="Telegram">{sw('telegram')}</K>
    <K l="chart snapshot">{sw('snapshot')}</K>
    <K l="forming heads-up">{sw('forming')}</K>
    <K l="quiet-hours digest">{sw('digest')}</K>
    <K l="cooldown (candles)">{num('cooldown_candles')}</K>
    <K l="stop pad (points)">{num('stop_pad')}</K>
    <K l="max stop (points)">{num('max_stop')}</K>
    <G>AI</G>
    <K l="market read at a detection" title="code facts + the candle model (shadow) + one [AI] line; never gates an alert">{sw('ai_read')}</K>
    <div className="btc-save"><button type="button" className="btn small" disabled={!changed.length} onClick={save}>Save</button>
      <button type="button" className="btn ghost small" onClick={() => { setDraft({ ...d.defaults }); setMsg('defaults filled · Save to keep'); }}>Reset to defaults</button>
      <span className={`msg small ${msg && msg !== 'Saved' && !msg.startsWith('defaults') ? 'down' : 'muted'}`}>{msg}</span></div>
  </Panel>;
}

export default function CryptoPage() {
  const live = useLive('crypto');
  const phone = useIsPhone();
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [tf, setTf] = useState(null);
  const [range, setRange] = useState('3d');
  const [ch, setCh] = useState(null);
  const [sel, setSel] = useState(null);
  const load = () => api.getCrypto().then((x) => { setD(x); push('crypto', x); setErr(''); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  useEffect(() => { if (live && !live.error) setD(live); }, [live]);
  const vtf = tf || d?.tf || '5m';
  const lastT = d?.health?.last_t;
  const sigs = useMemo(() => d?.signals || [], [d]);
  const s = useMemo(() => sigs.find((x) => x.id === sel) || null, [sigs, sel]);
  // a selected detection outside the chosen range: the chart loads a window around it and jumps there
  const win = useMemo(() => {
    if (!s) return null;
    const step = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600 }[vtf] || 300;
    const span = { '1d': 86400, '3d': 3 * 86400, '2w': 14 * 86400 }[range] || 3 * 86400;
    const now = Math.floor(Date.now() / 1000);
    if (s.t >= now - span) return null;
    return { from: s.t - 80 * step, to: s.t + 40 * step };
  }, [s, vtf, range]);
  useEffect(() => {
    let alive = true;
    api.cryptoCandles(vtf, range, win).then((x) => { if (alive) setCh(x); }).catch((e) => alive && setErr(e.message));
    return () => { alive = false; };
  }, [vtf, range, lastT, win]);
  const H = d?.health || {};
  const P = d?.pivots?.levels || {};
  const T = d?.today || {};
  const hours = d?.hours || {};
  const quiet = async (hours_) => { try { await api.cryptoQuiet(hours_); load(); } catch (e) { setErr(e.message); } };
  const askQuiet = () => { const v = window.prompt('Quiet the BTC alerts for how many hours? (detections are still stored and listed in the digest)', '2'); if (v && +v > 0) quiet(+v); };
  const refresh = async () => { try { const r = await api.cryptoRefresh(); if (!r.ok) setErr(r.why); load(); } catch (e) { setErr(e.message); } };
  const scan = async () => { try { const r = await api.cryptoScan(); if (!r.ok) setErr(r.why); else setErr(`history scan: ${r.marked} marked over ${r.scanned} candles (${r.days} d) · nothing sent`); load(); } catch (e) { setErr(e.message); } };
  const pick = (id) => { setSel(id === sel ? null : id); };
  // ⛶ full screen: the chart panel fills the window (the browser's fullscreen when it allows it, else a fixed overlay); Esc leaves
  const [full, setFull] = useState(false);
  // ⚙ Inputs: a drawer beside the chart (closed by default so the chart takes the whole row); the choice is remembered
  const [inputsOpen, setInputsOpen] = useState(() => { try { return localStorage.getItem('bp.btc.inputs') === '1'; } catch { return false; } });
  const toggleInputs = () => setInputsOpen((v) => { try { localStorage.setItem('bp.btc.inputs', v ? '0' : '1'); } catch { /* private mode */ } return !v; });
  useEffect(() => {
    if (!inputsOpen) return undefined;
    const onKey = (e) => { if (e.key === 'Escape' && !full) toggleInputs(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [inputsOpen, full]);
  const fullRef = useRef(null);
  useEffect(() => {
    if (!full) return undefined;
    const el = fullRef.current;
    const onKey = (e) => { if (e.key === 'Escape') setFull(false); };
    const onFs = () => { if (!document.fullscreenElement) setFull(false); };
    window.addEventListener('keydown', onKey);
    document.addEventListener('fullscreenchange', onFs);
    try { el?.requestFullscreen?.().catch(() => {}); } catch { /* overlay only */ }
    document.body.classList.add('btc-full-open');
    return () => {
      window.removeEventListener('keydown', onKey);
      document.removeEventListener('fullscreenchange', onFs);
      document.body.classList.remove('btc-full-open');
      try { if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {}); } catch { /* ignore */ }
    };
  }, [full]);

  const cols = [{ h: 'When (IST)' }, { h: 'Pattern', w: 112, ell: true }, { h: 'Dir', w: 54 }, { h: 'Level', w: 104, ell: true },
    { h: 'Entry · stop · 1R', w: 176, ell: true }, { h: 'Filters', w: 118, ell: true, lo: true }, { h: 'State', w: 150, ell: true }, { h: 'AI', w: 90, ell: true, lo: true }, { h: '', w: 100 }];
  const rows = sigs.map((x) => ({ key: x.id, onClick: () => pick(x.id), cls: `${x.id === sel ? 'btc-sel-row' : ''} ${x.state === 'dropped' ? 'dim' : ''}`, title: x.text,
    cells: [x.when, x.label, <span className={x.dir === 'long' ? 'up' : 'down'}>{x.dir}</span>, `${x.level} ${px(x.level_px)}`,
      `${px(x.entry)} · ${px(x.stop)}${x.capped ? ' (cap)' : ''} · ${px(x.r1)}`, filters(x),
      <Tag c={STATE_C[x.state] || ''} title={x.why || ''}>{stateWord(x)}</Tag>,
      x.ai?.line ? <span title={x.ai.line}><AiTag /> {x.ai.verdict || ''}</span> : <span className="muted">{x.ai?.verdict || '—'}</span>,
      x.png ? <a className="btn ghost small" href={`/api/crypto/snap/${x.id}.png`} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>Snapshot</a> : <span className="muted">—</span>] }));

  const stateChip = !d ? <Chip>loading…</Chip> : d.enabled
    ? <Chip dot={H.behind ? 'warn' : 'ok'} c={H.behind ? 'warn' : 'ok'} title={H.last_error || ''}>{d.running ? 'running' : 'starting'} · {TF_WORD[d.tf]} · Delta · {H.last_label ? `last candle ${H.last_label}` : 'no candles yet'}</Chip>
    : <Chip title="Inputs › feed on (crypto.enabled)">off · {TF_WORD[d.tf]} · Delta{H.last_label ? ` · last candle ${H.last_label}` : ''}</Chip>;
  return <div className="rd btc">
    <Head title="₿ BTC patterns" right={<>
      <Seg small opts={TFS} v={vtf} set={setTf} />
      <Seg small opts={RANGES} v={range} set={setRange} />
      {hours.quiet_until ? <button type="button" className="btn ghost small" onClick={() => quiet(null)}>Resume</button>
        : <button type="button" className="btn ghost small" onClick={askQuiet}>Quiet 2 h…</button>}
      <button type="button" className="btn ghost small" onClick={refresh}>Refresh</button>
      <button type="button" className="btn ghost small" onClick={scan} title="mark the patterns of the last scan_days on the chart · nothing is sent">Scan history</button>
      <button type="button" className={`btn small ${inputsOpen ? '' : 'ghost'}`} onClick={toggleInputs} aria-expanded={inputsOpen} title={inputsOpen ? 'close the inputs (Esc)' : 'open the inputs beside the chart'}>⚙ Inputs{inputsOpen ? ' ✕' : ''}</button></>}>
      {stateChip}
      <Chip>pivots {d?.pivots?.period || 'daily'} · {d?.pivots?.type === 'dm' ? 'DM' : (d?.pivots?.type || 'traditional').replace(/^./, (x) => x.toUpperCase())}</Chip>
      <Chip c={hours.quiet_until ? 'warn' : ''}>{hours.text || 'alerts …'}</Chip>
      <Chip c="warn" title="alerts only: nothing here places an order">alerts only · no orders</Chip>
    </Head>
    {err && <div className="rbanner bad">{err}</div>}
    <div className={`btc-grid ${inputsOpen ? 'open' : ''}`}>
      <div className="btc-left">
        <div className={full ? 'btc-full' : undefined} ref={fullRef}>
        <Panel title="Chart" right={<>
          {s ? <Chip title={s.text}>{s.label} {s.when.split(' ').slice(-2).join(' ')} on {s.level} {px(s.level_px)}</Chip> : <Chip>click a marker or a row</Chip>}
          {s && <Chip>entry {px(s.entry)} · stop {px(s.stop)}{s.capped ? ' (cap)' : ''} · 1R {px(s.r1)}</Chip>}
          {full && <><Seg small opts={TFS} v={vtf} set={setTf} /><Seg small opts={RANGES} v={range} set={setRange} /></>}
          <button type="button" className="btn ghost small" onClick={() => setFull(!full)} title={full ? 'exit full screen (Esc)' : 'full screen'}>{full ? '✕ Exit full screen' : '⛶ Full screen'}</button></>}>
          <BtcChart data={ch} sel={sel} onPick={pick} />
          <div className="btc-leg"><span><i style={{ background: '#e08a2e' }} />pivots R1–R5 / S1–S5</span><span><i style={{ background: '#9aa3ad' }} />P</span>
            <span><i style={{ background: '#5b6cff' }} />EMA {d?.settings?.ema_len ?? 20}</span><span><i className="dotm up" />bullish</span><span><i className="dotm down" />bearish (faded: quiet / mark only)</span><span><i className="dotm grey" />formed but filtered (EMA · volume · day · cooldown)</span>
            <span><i style={{ background: 'var(--blue)' }} />entry</span><span><i style={{ background: 'var(--bad)' }} />stop</span><span><i style={{ background: 'var(--ok)' }} />1R</span>
            <span className="muted">{d?.symbol || 'BTCUSD'} · Delta Exchange · {TF_WORD[vtf]} · IST</span></div>
        </Panel>
        </div>
        <Cards n={4}>
          <Card unit="n" label="Feed · Delta" value={H.last_label || (d?.enabled ? '—' : 'off')} sub={H.lag_candles != null ? `lag ${H.lag_candles} candle${H.lag_candles === 1 ? '' : 's'} · ${H.errors || 0} errors` : 'no candles yet'}
            fact={`${d?.symbol || 'BTCUSD'} · ${TF_WORD[d?.tf] || '5 min'} · 24×7`} k={H.behind ? 'down' : ''} title={H.last_error || 'last closed candle stored'} />
          <Card unit="n" label="Today's detections" value={`${T.sent || 0} sent`} sub={`${T.quiet || 0} quiet · ${T.mark || 0} mark · ${T.dropped || 0} dropped`}
            fact={`${T.confirm_wait || 0} waiting for confirmation`} />
          <Card unit="n" label={`Pivots · ${/^\d{4}-\d\d-\d\d$/.test(d?.pivots?.from || '') ? `${+d.pivots.from.slice(8)} ${MON[+d.pivots.from.slice(5, 7) - 1]}` : (d?.pivots?.from || 'today')}`} value={`P ${px(P.P)}`} sub={`R1 ${px(P.R1)} · R2 ${px(P.R2)}`} fact={`S1 ${px(P.S1)} · S2 ${px(P.S2)}`} />
          <Card unit="n" label="AI" value={d?.ai?.read ? 'read on' : 'off'} sub={d?.ai?.model_chip || 'candle model · off'}
            fact={d?.ai?.last ? `${d.ai.last.when} · ${d.ai.last.line}` : (d?.ai?.local_ai ? 'no read yet' : 'local AI off · code facts only')} title={d?.ai?.last?.line || ''} />
        </Cards>
        <div className="btc-ai">
          <div className="t"><AiTag /><b>Market read{s ? ` · ${s.label} ${s.when}` : ''}</b><Chip>{d?.ai?.model_chip || 'candle model · off'}</Chip><Chip>never gates an alert</Chip></div>
          <div className="l"><span>shape</span><div title={s?.ai?.facts || ''}>{s ? (s.ai?.facts || s.text) : 'pick a detection: its code facts, the model read (shadow) and the one [AI] line'}</div></div>
          <div className="l"><span>model</span><div>{s?.ai?.model ? 'stored in shadow · scored before it shows' : 'no model read'}</div></div>
          <div className="l"><span>read</span><div title={s?.ai?.line || ''}>{s?.ai?.line ? <><AiTag /> {s.ai.line} </> : <span className="muted">{s ? (s.ai?.state === 'off' ? 'the local AI is off' : 'no line yet') : '—'} </span>}
            {s?.ai?.verdict && <span className={/supports/.test(s.ai.verdict) ? 'up' : /against/.test(s.ai.verdict) ? 'down' : 'muted'}>· {s.ai.verdict}</span>}</div></div>
        </div>
        <Panel title="Detections" meta={sigs.length || null} right={`newest first · every row stays${phone ? '' : ' · click a row: the chart jumps to it'}`}>
          <div className="btc-table"><FixedTable cols={cols} rows={rows} empty="No detections yet: they appear here as the candles close." /></div>
        </Panel>
      </div>
      {inputsOpen && <Inputs d={d} onSaved={load} />}
    </div>
  </div>;
}
