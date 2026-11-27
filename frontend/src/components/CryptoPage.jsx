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

// outcome · grade · forecast (every key optional: an older server renders the page as before)
const OUT_C = { up: 'ok', down: 'bad', warn: 'w', dim: '' };
const outcomeTag = (o) => (o?.chip ? <Tag c={OUT_C[o.tone] ?? ''} title={o.r != null ? `${o.r > 0 ? '+' : ''}${o.r} R${o.mins != null ? ` · ${o.mins} min` : ''}` : ''}>{o.chip}</Tag> : <span className="muted">—</span>);
const gradeBadge = (g) => {
  if (!g) return <span className="muted">—</span>;
  const step = g.model_step ? ` · candle model ${g.model_step > 0 ? '+1' : '−1'}` : '';
  if (g.few || !g.g) return <span className="btc-g gN" title={g.why || `too few like this${g.n != null ? ` (${g.n})` : ''}`}>—</span>;
  return <span className={`btc-g g${g.g}`} title={`${g.why || ''}${step}`}>{g.g}</span>;
};
const fcTag = (f) => (f?.chip ? <Tag c={f.p != null && Math.abs(f.p - 0.5) < 0.05 ? '' : 'a'} title={f.exp_px != null ? `${px(f.exp_px)} ± ${px(f.exp_range)} · stop first ${Math.round((f.touch_stop ?? 0) * 100)} % · 1R first ${Math.round((f.touch_r1 ?? 0) * 100)} %` : ''}>{f.chip}</Tag> : <span className="muted">—</span>);
const isFc = (s) => s?.kind === 'forecast';
const shadowRow = (s) => isFc(s) && s.state === 'quiet';
const pctS = (v) => (v == null ? '—' : `${Math.round(v)} %`);
const rS = (v) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(+v).toFixed(1)} R`);
const coneWord = (f) => (f ? [f.horizon_min ? `${f.horizon_min} min` : null, f.p != null ? `${Math.round(f.p * 100)} % ${f.dir === 'long' ? 'up' : 'down'}` : null,
  f.exp_range != null ? `±${px(f.exp_range)}` : null].filter(Boolean).join(' · ') : '');

// The forecast cone (candle model, shadow) from the signal candle: the p10–p90 and p25–p75 bands as filled polygons
// (a series primitive on the candles); the median is a dashed line series whose points also put the future times on
// the time scale, so the bands can be placed past the last candle. Fades toward the horizon.
class ConePrim {
  constructor() { this.c = null; this.chart = null; this.series = null; this.req = null; const me = this; this.view = { zOrder: () => 'bottom', renderer: () => ({ draw: (t) => me.draw(t, false) }) };
    this.top = { zOrder: () => 'top', renderer: () => ({ draw: (t) => me.draw(t, true) }) }; }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.req = requestUpdate; }
  detached() { this.chart = null; this.series = null; this.req = null; }
  set(c) { this.c = c; if (this.req) this.req(); }
  updateAllViews() {}
  paneViews() { return [this.view, this.top]; }
  draw(target, label) {   // the bands under the candles, the label over them
    const c = this.c;
    if (!c || !this.chart || !this.series) return;
    const ts = this.chart.timeScale();
    const x = (t) => ts.timeToCoordinate(t);
    const y = (v) => this.series.priceToCoordinate(v);
    const x0 = x(c.t0), y0 = y(c.anchor);
    if (x0 == null || y0 == null) return;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      const xs = c.ts.map(x);
      if (xs.some((v) => v == null)) return;
      const xe = xs[xs.length - 1];
      const band = (lo, hi, a) => {
        if (!lo || !hi) return;
        const g = ctx.createLinearGradient(x0, 0, Math.max(x0 + 1, xe), 0);
        g.addColorStop(0, `rgba(91,108,255,${a})`); g.addColorStop(1, `rgba(91,108,255,${a * 0.45})`);
        ctx.beginPath(); ctx.moveTo(x0, y0);
        hi.forEach((v, i) => ctx.lineTo(xs[i], y(v)));
        for (let i = lo.length - 1; i >= 0; i--) ctx.lineTo(xs[i], y(lo[i]));
        ctx.closePath(); ctx.fillStyle = g; ctx.fill();
      };
      if (!label) { band(c.p10, c.p90, 0.12); band(c.p25, c.p75, 0.2); return; }
      if (c.label) {
        const lows = [c.p10, c.p25, c.med].find(Boolean);
        const yl = y(Math.min(...lows)) ?? y0;
        ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace'; ctx.fillStyle = '#5b6cff'; ctx.textAlign = 'right';
        ctx.fillText(c.label, xe, yl + 16);
      }
    });
  }
}
// One cone from a read's paths, its start candle (t0, chart time) and its step times (chart times; else from horizon_min):
// the detection cone and the live "forecast now" cone both go through here.
function coneFrom({ paths, t0, times, horizonMin, anchor, label }, step) {
  const med = paths?.median;
  if (!Array.isArray(med) || !med.length || t0 == null) return null;
  const n = med.length;
  const dt = horizonMin ? Math.max(1, Math.round((+horizonMin * 60) / n)) : step;
  const ts0 = Array.isArray(times) && times.length === n ? times.map(Number) : med.map((_, i) => t0 + (i + 1) * dt);
  const keep = ts0.map((_, i) => i).filter((i) => (ts0[i] - t0) % step === 0);   // on a coarser chart only the points on its candle grid
  if (!keep.length) return null;
  const arr = (k) => (Array.isArray(paths[k]) && paths[k].length === n ? keep.map((i) => +paths[k][i]) : null);
  return { t0, anchor: +(anchor ?? med[0]), ts: keep.map((i) => ts0[i]), med: keep.map((i) => +med[i]),
    p25: arr('p25'), p75: arr('p75'), p10: arr('p10'), p90: arr('p90'), label };
}
const coneOf = (s, off, step) => (s?.forecast && s.t != null ? coneFrom({ paths: s.forecast.paths, t0: s.t + off, times: s.forecast.times,
  horizonMin: s.forecast.horizon_min, anchor: s.entry, label: coneWord(s.forecast) }, step) : null);
// the live cone (no entry / stop / 1R: only the cone and the pivots) from GET /api/crypto/forecast_now
const liveCone = (f, step) => (f?.paths ? coneFrom({ paths: f.paths, t0: f.t0, times: f.times, horizonMin: f.horizon_min, anchor: f.last,
  label: coneWord(f) }, step) : null);
const fcNowText = (f) => {
  if (!f || f.empty) return null;
  const pv = (x) => (x ? `${x.name} ${px(x.px)}` : '—');
  const pc = f.p_up != null ? Math.round(Math.max(f.p_up, 1 - f.p_up) * 100) : null;
  return { head: `📈 now · ${f.candle_label || '—'} candle · ${f.horizon_min} min: `,
    odds: pc == null ? '—' : Math.abs(f.p_up - 0.5) < 0.02 ? `${pc} % · flat` : `${pc} % ${f.p_up > 0.5 ? 'up' : 'down'}`,
    tail: ` · ${px(f.exp_px)} ± ${px(f.exp_range)} · next pivot ${pv(f.next_pivots?.above)} above · ${pv(f.next_pivots?.below)} below` };
};

function palette() {
  return { bg: cssVar('--panel'), text: cssVar('--text'), grid: cssVar('--grid'), border: cssVar('--border'), up: cssVar('--ok') || '#1f9d61',
    down: cssVar('--bad') || '#d64545', blue: cssVar('--blue') || '#2a63d4', muted: cssVar('--muted') || '#9aa3ad', level: '#e08a2e', ema: '#5b6cff' };
}

// The chart: candles, the EMA, the pivot levels per period (a gap at each new period), the markers, the selected
// detection's entry / stop / 1R lines. Fixed height; the tooltip shows the detection under the cursor.
function BtcChart({ data, sel, alt, live, onPick }) {
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
    const cone = new ConePrim();
    candles.attachPrimitive(cone);
    const median = chart.addSeries(LineSeries, { color: '#5b6cff', lineWidth: 2, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    h.current = { chart, candles, ema, levels, markers, pal, lines: [], LineStyle, sigs: [], cone, median };
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
    const mk = [...best.values()].sort((a, b) => a.mt - b.mt).map((s) => ({
      time: s.mt, position: s.dir === 'long' ? 'belowBar' : 'aboveBar', shape: 'circle',
      size: s.id === sel ? 1.6 : s.state === 'dropped' ? 0.7 : 1,
      color: s.state === 'dropped' ? `${c.pal.muted}99` : muted(s) ? `${s.dir === 'long' ? c.pal.up : c.pal.down}66` : (s.dir === 'long' ? c.pal.up : c.pal.down) }));
    const s0 = data.signals.find((x) => x.id === sel);
    const s = s0 && alt && alt.id === s0.id ? { ...alt, ...s0, forecast: s0.forecast?.paths ? s0.forecast : alt.forecast, outcome: s0.outcome || alt.outcome } : s0;
    const step0 = data.candles.length > 1 ? data.candles[1].time - data.candles[0].time : 300;
    // the selected detection's cone (when its model read carries paths) and its scored outcome written at the 1R / stop candle
    const cn = s ? coneOf(s, off, step0) : liveCone(live, step0);   // a selected detection's cone takes over from the live one
    c.cone.set(cn);
    c.median.setData(cn ? [{ time: cn.t0, value: cn.anchor }, ...cn.ts.map((t, i) => ({ time: t, value: cn.med[i] }))] : []);
    const o = s?.outcome;
    if (s && o && o.state !== 'open' && o.mins != null && o.chip) {
      const at = s.t + off + o.mins * 60;
      const bar = data.candles.find((b) => b.time >= at) || data.candles[data.candles.length - 1];
      if (bar && bar.time >= s.t + off) {
        const above = (s.dir === 'long') === (o.state === 'won');
        mk.push({ time: bar.time, position: above ? 'aboveBar' : 'belowBar', shape: 'square', size: 0.6, text: o.chip,
          color: o.tone === 'up' ? c.pal.up : o.tone === 'down' ? c.pal.down : o.tone === 'warn' ? c.pal.level : c.pal.muted });
      }
    }
    c.markers.setMarkers(mk.sort((a, b) => a.time - b.time));
    if (s) {
      [[s.entry, c.pal.blue, 'entry'], [s.stop, c.pal.down, 'stop'], [s.r1, c.pal.up, '1R']].forEach(([p, color, title]) => c.lines.push(c.candles.createPriceLine({ price: p, color, lineWidth: 1, lineStyle: c.LineStyle.Solid, axisLabelVisible: true, title })));
      const step = data.candles.length > 1 ? data.candles[1].time - data.candles[0].time : 300;
      try { c.chart.timeScale().setVisibleRange({ from: s.t + off - 45 * step, to: s.t + off + 25 * step }); } catch { /* outside the data */ }
    } else if (data.candles.length) {
      c.chart.timeScale().fitContent();
    }
  }, [data, sel, alt, live, lw, theme]);

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
    {'score_after_min' in draft && <>
      <G>Outcomes · grade · forecast alerts</G>
      <K l="score outcomes after (min)" title="each detection is scored by code this long after it: 1R first won, stop first lost, else the R at the end"><Seg small opts={[[30, '30'], [60, '60'], [120, '120']]} v={+draft.score_after_min} set={(v) => set('score_after_min', v)} /></K>
      {'min_alert_grade' in draft && <K l="min alert grade" title="the grade shows on every alert either way · an alert under it is stored quiet"><Seg small opts={[['all', 'all'], ['C', 'C'], ['B', 'B'], ['A', 'A']]} v={draft.min_alert_grade} set={(v) => set('min_alert_grade', v)} /></K>}
      {'forecast_alerts' in draft && <K l="forecast alerts" title={d.scores?.forecast?.gate?.why || 'send needs the gate: 40 scored · 55 % won'}><Seg small opts={[['shadow', 'shadow'], ['send', 'send']]} v={draft.forecast_alerts} set={(v) => set('forecast_alerts', v)} /></K>}
      {draft.forecast_alerts === 'send' && d.scores?.forecast?.gate && !d.scores.forecast.gate.ok && <div className="btc-gate warn small" title={d.scores.forecast.gate.why}>{d.scores.forecast.gate.why || 'held in shadow until the gate passes'}</div>}
      {'forecast_p' in draft && <K l="forecast odds threshold" title="the candle model's odds one way, at least"><Seg small opts={[[58, '58 %'], [62, '62 %'], [66, '66 %']]} v={+draft.forecast_p} set={(v) => set('forecast_p', v)} /></K>}
      {'forecast_move_atr' in draft && <K l="forecast move ≥" title="the expected range against the ATR"><Seg small opts={[[0.6, '0.6 × ATR'], [1, '1.0 × ATR']]} v={+draft.forecast_move_atr} set={(v) => set('forecast_move_atr', v)} /></K>}
      {'forecast_cone' in draft && <K l="forecast cone on alerts" title="the cone on the Telegram snapshot (the page chart always shows it for a selected detection)">{sw('forecast_cone')}</K>}
    </>}
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
  // the scores (outcomes scored by code): the summary carries 30 d; the Direction tile's toggle reads 1 d / 7 d
  const [sdays, setSdays] = useState(30);
  const [sc1, setSc1] = useState(null);
  useEffect(() => {
    if (sdays === 30) { setSc1(null); return undefined; }
    let alive = true;
    api.cryptoScores(sdays).then((x) => { if (alive) setSc1(x); }).catch(() => { if (alive) setSc1(null); });
    return () => { alive = false; };
  }, [sdays, lastT]);
  const SC = d?.scores || null;
  const SD = (sdays !== 30 && sc1) || SC;
  const modelChip = SC?.model_chip || d?.ai?.model_chip || 'candle model · off';
  const H = d?.health || {};
  const P = d?.pivots?.levels || {};
  const T = d?.today || {};
  const hours = d?.hours || {};
  const quiet = async (hours_) => { try { await api.cryptoQuiet(hours_); load(); } catch (e) { setErr(e.message); } };
  const askQuiet = () => { const v = window.prompt('Quiet the BTC alerts for how many hours? (detections are still stored and listed in the digest)', '2'); if (v && +v > 0) quiet(+v); };
  const refresh = async () => { try { const r = await api.cryptoRefresh(); if (!r.ok) setErr(r.why); load(); } catch (e) { setErr(e.message); } };
  const scan = async () => { try { const r = await api.cryptoScan(); if (!r.ok) setErr(r.why); else setErr(`history scan: ${r.marked} marked over ${r.scanned} candles (${r.days} d) · nothing sent`); load(); } catch (e) { setErr(e.message); } };
  const pick = (id) => { setSel(id === sel ? null : id); };
  // 📈 Forecast now: the candle model's latest per-candle read (cached by the service; this only reads it) as a live cone
  // from the last closed candle. Off by default; the toggle and the horizon are remembered.
  const lsGet = (k, dflt) => { try { return localStorage.getItem(k) ?? dflt; } catch { return dflt; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } };
  const [fcOn, setFcOn] = useState(() => lsGet('bp.btc.fcnow', '0') === '1');
  const [fcH, setFcH] = useState(() => (lsGet('bp.btc.fcnow.h', '30') === '60' ? 60 : 30));
  const toggleFc = () => setFcOn((v) => { lsSet('bp.btc.fcnow', v ? '0' : '1'); return !v; });
  const pickFcH = (v) => { lsSet('bp.btc.fcnow.h', String(v)); setFcH(v); };
  const [fcNow, setFcNow] = useState(null);
  const sumFc = d?.forecast_now;
  useEffect(() => { if (fcH === 30 && sumFc && !fcNow) setFcNow(sumFc); }, [sumFc, fcH]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!fcOn) return undefined;
    let alive = true;
    const get = () => api.cryptoForecastNow(fcH).then((x) => { if (alive) setFcNow(x); }).catch(() => {});
    get();
    const id = setInterval(get, 30000);
    return () => { alive = false; clearInterval(id); };
  }, [fcOn, fcH, lastT]);
  const fcCur = fcNow && fcNow.horizon_min === fcH ? fcNow : null;
  const fcDraw = fcOn && fcCur && !fcCur.empty && fcCur.worker === 'ok' && !fcCur.stale && fcCur.paths && fcCur.tf === vtf ? fcCur : null;
  const fcKey = fcDraw ? `${fcDraw.t}|${fcDraw.horizon_min}` : '';
  const liveFc = useMemo(() => fcDraw, [fcKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const fcTxt = fcNowText(fcCur);
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

  // the grade / forecast / outcome columns appear once the server sends those keys (an older server: the table as before)
  const hasG = sigs.some((x) => 'grade' in x), hasF = sigs.some((x) => 'forecast' in x), hasO = sigs.some((x) => 'outcome' in x);
  const cols = [{ h: 'When (IST)' }, { h: 'Pattern', w: hasF ? 150 : 112, ell: true }, { h: 'Dir', w: 54 }, { h: 'Level', w: 104, ell: true },
    { h: 'Entry · stop · 1R', w: 176, ell: true }, hasG && { h: 'Grade', w: 66 }, hasF && { h: 'Forecast', w: 180, ell: true },
    !hasO && { h: 'Filters', w: 118, ell: true, lo: true }, { h: 'State', w: 150, ell: true }, hasO && { h: 'Outcome', w: 190, ell: true },
    { h: 'AI', w: 90, ell: true, lo: true }, { h: '', w: 100, lo: hasO }].filter(Boolean);
  const rows = sigs.map((x) => ({ key: x.id, onClick: () => pick(x.id), cls: `${x.id === sel ? 'btc-sel-row' : ''} ${x.state === 'dropped' ? 'dim' : ''}`, title: x.text,
    cells: [x.when, isFc(x) ? <span><Tag c="a">forecast</Tag> {x.label}</span> : x.label, <span className={x.dir === 'long' ? 'up' : 'down'}>{x.dir}</span>, `${x.level} ${px(x.level_px)}`,
      `${px(x.entry)} · ${px(x.stop)}${x.capped ? ' (cap)' : ''} · ${px(x.r1)}`, ...(hasG ? [gradeBadge(x.grade)] : []), ...(hasF ? [fcTag(x.forecast)] : []), ...(hasO ? [] : [filters(x)]),
      shadowRow(x) ? <Tag c="w" title={x.why || 'forecast alerts are held in shadow until their gate passes'}>shadow · not sent</Tag> : <Tag c={STATE_C[x.state] || ''} title={[x.why, hasO ? filters(x) : ''].filter(Boolean).join(' · ')}>{stateWord(x)}</Tag>,
      ...(hasO ? [outcomeTag(x.outcome)] : []),
      x.ai?.line ? <span title={x.ai.line}><AiTag /> {x.ai.verdict || ''}</span> : <span className="muted">{x.ai?.verdict || '—'}</span>,
      x.png ? <a className="btn ghost small" href={`/api/crypto/snap/${x.id}.png`} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>Snapshot</a> : <span className="muted">—</span>] }));

  const bpCols = [{ h: 'Pattern', ell: true }, { h: 'Dir', w: 60 }, { h: 'Signals', w: 80, n: true }, { h: 'Won', w: 100, n: true }, { h: 'Avg R', w: 80, n: true },
    { h: 'At R1', w: 100, n: true, lo: true }, { h: 'At S1', w: 100, n: true, lo: true }, { h: 'At P', w: 100, n: true, lo: true }];
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
          {s?.forecast && !phone && <Chip c="fc" title="the candle model's read for this candle · shadow: never gates an alert">{modelChip}</Chip>}
          <button type="button" className={`btn ghost small btc-fcbtn ${fcOn ? 'on' : ''}`} onClick={toggleFc} aria-pressed={fcOn}
            title="the candle model's read on the last closed candle, drawn as a cone · shadow · never an alert">Forecast now · {fcOn ? 'on' : 'off'}</button>
          {fcOn && <Seg small opts={[[30, '30 min'], [60, '60 min']]} v={fcH} set={pickFcH} />}
          {full && <><Seg small opts={TFS} v={vtf} set={setTf} /><Seg small opts={RANGES} v={range} set={setRange} /></>}
          <button type="button" className="btn ghost small" onClick={() => setFull(!full)} title={full ? 'exit full screen (Esc)' : 'full screen'}>{full ? '✕ Exit full screen' : '⛶ Full screen'}</button></>}>
          <BtcChart data={ch} sel={sel} alt={s} live={liveFc} onPick={pick} />
          {fcOn && <div className="btc-fcnow">
            {!fcCur || fcCur.empty ? <Chip>📈 now · no read yet{fcCur?.next_at_label ? ` · ${fcCur.next_at_label} next` : ''}</Chip> : <>
              <Chip c="fc" title={fcCur.when ? `the read on the ${fcCur.when} candle · paths p10–p90 · shadow` : ''}><span>{fcTxt.head}<b>{fcTxt.odds}</b>{fcTxt.tail}</span></Chip>
              <Chip c="warn" title="the candle model's BTC scorecard · shadow: never gates or sends an alert">{fcCur.model_chip || 'shadow'} · not an alert</Chip></>}
            {fcCur && !fcCur.empty && fcCur.worker === 'down' ? <Chip c="warn" title="the candle-model worker did not answer on the last closed candle">worker down · {fcCur.next_at_label} next</Chip>
              : fcCur && fcCur.stale ? <Chip c="warn" title="older than 2 candles">stale · {fcCur.next_at_label} next</Chip>
                : <Chip>refreshes at each closed candle · {fcCur?.next_at_label || '—'} next</Chip>}
            {fcCur && !fcCur.empty && fcCur.tf && fcCur.tf !== vtf && <Chip>cone on the {TF_WORD[fcCur.tf] || fcCur.tf} chart</Chip>}
          </div>}
          <div className="btc-leg"><span><i style={{ background: '#e08a2e' }} />pivots R1–R5 / S1–S5</span><span><i style={{ background: '#9aa3ad' }} />P</span>
            <span><i style={{ background: '#5b6cff' }} />EMA {d?.settings?.ema_len ?? 20}</span><span><i className="dotm up" />bullish</span><span><i className="dotm down" />bearish (faded: quiet / mark only)</span><span><i className="dotm grey" />formed but filtered (EMA · volume · day · cooldown)</span>
            <span><i style={{ background: 'var(--blue)' }} />entry</span><span><i style={{ background: 'var(--bad)' }} />stop</span><span><i style={{ background: 'var(--ok)' }} />1R</span>
            {s?.forecast?.paths && <span><i className="cone" />forecast cone · median dashed · 50 % / 80 % bands (shadow)</span>}
            {!s?.forecast?.paths && (fcOn || liveFc) && <span className="fc">▰ forecast cone (candle model · shadow)</span>}
            <span className="muted">{d?.symbol || 'BTCUSD'} · Delta Exchange · {TF_WORD[vtf]} · IST</span></div>
        </Panel>
        </div>
        <Cards n={4}>
          <Card unit="n" label="Feed · Delta" value={H.last_label || (d?.enabled ? '—' : 'off')} sub={H.lag_candles != null ? `lag ${H.lag_candles} candle${H.lag_candles === 1 ? '' : 's'} · ${H.errors || 0} errors` : 'no candles yet'}
            fact={`${d?.symbol || 'BTCUSD'} · ${TF_WORD[d?.tf] || '5 min'} · 24×7`} k={H.behind ? 'down' : ''} title={H.last_error || 'last closed candle stored'} />
          <Card unit="n" label="Today's detections" value={`${T.sent || 0} sent`} sub={`${T.quiet || 0} quiet · ${T.mark || 0} mark · ${T.dropped || 0} dropped`}
            fact={`${T.confirm_wait || 0} waiting for confirmation`} />
          <Card unit="n" label={`Pivots · ${/^\d{4}-\d\d-\d\d$/.test(d?.pivots?.from || '') ? `${+d.pivots.from.slice(8)} ${MON[+d.pivots.from.slice(5, 7) - 1]}` : (d?.pivots?.from || 'today')}`} value={`P ${px(P.P)}`} sub={`R1 ${px(P.R1)} · R2 ${px(P.R2)}`} fact={`S1 ${px(P.S1)} · S2 ${px(P.S2)}`} />
          <Card unit="n" label="AI" value={d?.ai?.read ? 'read on' : 'off'} sub={modelChip}
            fact={d?.ai?.last ? `${d.ai.last.when} · ${d.ai.last.line}` : (d?.ai?.local_ai ? 'no read yet' : 'local AI off · code facts only')} title={d?.ai?.last?.line || ''} />
        </Cards>
        {SC && <div className="btc-scores"><Cards n={3}>
          <Card unit="n" label={<>Direction · {sdays} d <span className="btc-dd">{[1, 7, 30].map((k) => <button key={k} type="button" className={sdays === k ? 'on' : ''}
            onClick={(e) => { e.stopPropagation(); setSdays(k); }}>{k} d</button>)}</span></>}
            value={`long ${rS(SD?.direction?.long?.r)} · short ${rS(SD?.direction?.short?.r)}`}
            sub={`long ${SD?.direction?.long?.n ?? 0} signals · ${SD?.direction?.long?.won ?? 0} won · short ${SD?.direction?.short?.n ?? 0} · ${SD?.direction?.short?.won ?? 0} won`}
            fact="sent · quiet · mark · dropped all scored · R only, no money" title="every detection scored by code after crypto.score_after_min: 1R first +1 R, stop first −1 R, else the R at the end" />
          <Card unit="n" label={`By grade · ${SC.days || 30} d`} value={['A', 'B', 'C', 'D'].map((g) => `${g} ${pctS(SC.by_grade?.[g]?.pct)}`).join(' · ')}
            sub={`${['A', 'B', 'C', 'D'].map((g, i) => `${g} ${SC.by_grade?.[g]?.n ?? 0}${i ? '' : ' signals'}`).join(' · ')} · min grade to send: ${d?.settings?.min_alert_grade ?? 'all'}`}
            title={['A', 'B', 'C', 'D'].map((g) => `${g} ${pctS(SC.by_grade?.[g]?.pct)} won of ${SC.by_grade?.[g]?.n ?? 0}`).join(' · ')}
            fact={(d?.settings?.min_alert_grade ?? 'all') === 'all' ? 'set crypto.min_alert_grade to B to quiet the C and D alerts' : `alerts under grade ${d.settings.min_alert_grade} are stored quiet`} />
          <Card unit="n" label={`Forecast alerts · ${d?.settings?.forecast_alerts === 'send' && SC.forecast?.gate?.ok ? 'sending' : 'shadow'}`}
            value={`${SC.forecast?.scored ?? 0} scored · ${pctS(SC.forecast?.pct)} won`}
            sub={(SC.forecast?.buckets || []).length ? SC.forecast.buckets.map((b) => `${b.lo}${b.hi != null ? `–${b.hi}` : '+'} %: ${b.n}${b.pct != null ? ` · ${Math.round(b.pct)} % won` : ''}`).join(' · ') : 'no forecast candidate scored yet'}
            fact={`${SC.forecast?.gate?.why || (SC.forecast?.gate?.ok ? 'gate passed' : 'held in shadow')} · crypto.forecast_alerts ${d?.settings?.forecast_alerts ?? 'shadow'}`}
            k={SC.forecast?.gate?.ok ? 'up' : ''} title={SC.forecast?.gate?.why || ''} />
        </Cards></div>}
        <div className="btc-ai">
          <div className="t"><AiTag /><b>Market read{s ? ` · ${s.label} ${s.when}` : ''}</b><Chip c={SC?.model_chip ? 'fc' : ''}>{modelChip}</Chip><Chip>never gates an alert</Chip></div>
          <div className="l"><span>shape</span><div title={s?.ai?.facts || ''}>{s ? (s.ai?.facts || s.text) : 'pick a detection: its code facts, the model read (shadow) and the one [AI] line'}</div></div>
          <div className="l"><span>model</span><div title={s?.forecast?.chip || ''}>{s?.forecast?.chip
            ? <>{s.forecast.chip}{s.forecast.exp_px != null ? ` · ${px(s.forecast.exp_px)} ± ${px(s.forecast.exp_range)}` : ''}{s.forecast.touch_stop != null ? ` · stop first ${Math.round(s.forecast.touch_stop * 100)} %` : ''} <span className="muted">· shadow</span></>
            : s?.ai?.model ? 'stored in shadow · scored before it shows' : 'no model read'}</div></div>
          <div className="l"><span>read</span><div title={s?.ai?.line || ''}>{s?.ai?.line ? <><AiTag /> {s.ai.line} </> : <span className="muted">{s ? (s.ai?.state === 'off' ? 'the local AI is off' : 'no line yet') : '—'} </span>}
            {s?.ai?.verdict && <span className={/supports/.test(s.ai.verdict) ? 'up' : /against/.test(s.ai.verdict) ? 'down' : 'muted'}>· {s.ai.verdict}</span>}</div></div>
        </div>
        <Panel title="Detections" meta={sigs.length || null} right={`newest first · every row stays${phone ? '' : ' · click a row: the chart jumps to it'}`}>
          <div className="btc-table"><FixedTable cols={cols} rows={rows} empty="No detections yet: they appear here as the candles close." /></div>
        </Panel>
        {Array.isArray(SC?.by_pattern) && <Panel title={`By pattern and level · ${SC.days || 30} d`} right="R and win rate · code-scored">
          <FixedTable cols={bpCols} rows={SC.by_pattern.map((b, i) => ({ key: `${b.pattern}${b.dir}${i}`, cells: [
            (d?.patterns || []).find((p) => p.key === b.pattern)?.label || b.label || b.pattern, <span className={b.dir === 'long' ? 'up' : 'down'}>{b.dir}</span>,
            b.n ?? 0, `${b.won ?? 0} · ${pctS(b.pct)}`, <span className={b.avg_r > 0 ? 'up' : b.avg_r < 0 ? 'down' : ''}>{b.avg_r == null ? '—' : `${b.avg_r > 0 ? '+' : b.avg_r < 0 ? '−' : ''}${Math.abs(b.avg_r).toFixed(1)}`}</span>,
            ...['R1', 'S1', 'P'].map((k) => (b.at?.[k]?.n ? `${b.at[k].n} · ${pctS(b.at[k].pct)}` : '—'))] }))}
            empty="Nothing scored yet: each detection is scored by code after crypto.score_after_min." />
        </Panel>}
      </div>
      {inputsOpen && <Inputs d={d} onSaved={load} />}
    </div>
  </div>;
}
