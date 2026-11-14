import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useLive, useLiveStamp } from '../services/live';
import { useSchedule, left } from './StatusBar';
import { useAttention } from './AttentionStrip';
import { StopMeter } from './ui';
import { inr, pct, cls, signed } from '../fmt';
import { useIsPhone } from '../useMedia';
import { useFlashKeys, useStale, useUnread, useNewIds } from '../heartbeat';
import * as api from '../services/api';
import HealthStrip from './HealthStrip';
import PhonePositions from './PhonePositions';
import { Card, Cards, Bar, Panel, FixedTable, Seg, Tag, Chip } from './ui2';

// 🏠 Home: the day on one screen. Timeline (click a dot: Activity filters to it), account cards with sparklines (Real
// and Paper E+ against NIFTY), the stats strip, today's live P&L with who made the day, the positions map (one tile
// per name), the market pulse, positions by book with filters and sortable headers, the next open's buys, what needs
// a look, risk, next up, activity. Every block pulses when its own data changes (heartbeat.js).
const at12 = (s) => {
  if (!s) return '';
  const d = new Date(s.length > 10 ? s : `${s}T00:00:00`);
  return isNaN(d) ? s : d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true });
};
const hm = (s) => (s ? new Date(s).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit', hour12: true }) : '');
const hm24of = (s) => (s && s.length > 10 ? new Date(s).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' }) : '');
const dayName = (s) => (s ? new Date(s.length > 10 ? s : `${s}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const weekday = (s) => (s ? new Date(s.length > 10 ? s : `${s}T00:00:00`).toLocaleDateString('en-IN', { weekday: 'short' }) : '');
const sgn = (v) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
const shortName = (s) => (s && s.length > 9 ? `${s.slice(0, 8)}…` : s || '');       // the same shortening everywhere on the map
const BOOK = { real: 'Real', paper_E: 'Paper E+', intraday: 'Intraday', futures: 'Futures', options: 'Options' };
const mins = (t) => (t ? +t.slice(0, 2) * 60 + +t.slice(3, 5) : 0);
const pctOfDay = (t) => 3 + Math.max(0, Math.min(1, (mins(t) - 555) / 375)) * 94;   // 9:15 → 15:30, with a margin
const evId = (e) => `${e.t}|${e.text}`;
const remembered = (key, dflt) => { try { return localStorage.getItem(key) || dflt; } catch { return dflt; } };
const remember = (key, v) => { try { localStorage.setItem(key, v); } catch { /* private mode */ } };

function Timeline({ events, jobs, now, feed, phone, picked, onPick }) {
  const nowT = new Date(now).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' });
  const upcoming = (jobs || []).map((j) => ({ t: new Date(j.at).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' }), kind: 'job', text: j.name }))
    .filter((j) => j.t >= '09:15' && j.t <= '15:30');
  const all = [...(events || []), ...upcoming].filter((e) => e.t && e.t >= '09:00' && e.t <= '15:45').sort((a, b) => a.t.localeCompare(b.t));
  const fresh = useNewIds('bp.tl', (events || []).map(evId));
  const col = { order: 'var(--ok)', failed: 'var(--bad)', signal: 'var(--blue)', exit: 'var(--bad)', trail: 'var(--purple)', job: 'var(--muted)' };
  const clusters = [];                                        // events closer than 1.5% of the day share one dot
  all.forEach((e) => { const x = pctOfDay(e.t); const c = clusters[clusters.length - 1]; if (c && x - c.x < 1.5) { c.items.push(e); c.x2 = x; } else clusters.push({ x, x2: x, items: [e] }); });
  const show = feed === 'live' || (nowT >= '09:15' && nowT <= '15:30');
  const isOn = (c) => picked && c.items.some((e) => picked.includes(evId(e)));
  const pick = (c) => onPick(isOn(c) ? null : c.items.filter((e) => e.kind !== 'job'));
  return <div className={`htl ${phone ? 'phone' : ''}`} aria-label="Today's timeline">
    <div className="past" style={{ width: `${pctOfDay(nowT)}%` }} />
    {(phone ? ['09:15', '12:00', '15:30'] : ['09:15', '10:30', '12:00', '13:30', '15:30']).map((t) => <span key={t} className={`h ${t === '09:15' ? 'first' : t === '15:30' ? 'last' : ''}`} style={{ left: `${pctOfDay(t)}%` }}>{t}</span>)}
    {clusters.map((c, i) => { const tip = c.items.map((e) => `${e.t} · ${e.text}`).join('\n'); const ping = c.items.some((e) => fresh.includes(evId(e)));
      const kind = c.items.find((e) => e.kind !== 'job')?.kind || 'job';
      return c.items.length > 1
        ? <span key={i} className={`cl ${ping ? 'ping' : ''} ${isOn(c) ? 'on' : ''}`} style={{ left: `${(c.x + c.x2) / 2}%` }} title={`${tip}\nclick: show these in Activity`} onClick={() => pick(c)}>{c.items.length}</span>
        : <span key={i} className={`ev ${ping ? 'ping' : ''} ${isOn(c) ? 'on' : ''}`} style={{ left: `${c.x}%` }} title={`${tip}\nclick: show it in Activity`} onClick={() => pick(c)}><i style={{ background: col[kind] || 'var(--muted)' }} /></span>; })}
    {show && <div className="now" style={{ left: `${pctOfDay(nowT)}%` }}>{!phone && <span className={pctOfDay(nowT) > 85 ? 'flip' : ''}>now {hm(now)}</span>}</div>}
    {picked && !phone && <span className="tlf" onClick={() => onPick(null)}>showing {picked.length} event{picked.length === 1 ? '' : 's'} in Activity · ✕</span>}
  </div>;
}

function TodayCurve({ points, today }) {
  const v = (points || []).map((p) => p[1]);
  if (v.length < 2) return <div className="hcurve-box"><p className="muted small">The live curve draws from the open, one point a minute{today != null ? ` · now ${signed(today)}` : ''}.</p></div>;
  const lo = Math.min(0, ...v), hi = Math.max(0, ...v), w = 600, h = 110;
  const y = (x) => h - 6 - ((x - lo) / (hi - lo || 1)) * (h - 12);
  const peakI = v.indexOf(Math.max(...v));
  const seg = (a, b) => v.slice(a, b + 1).map((x, i) => `${((a + i) / (v.length - 1)) * w},${y(x)}`).join(' ');
  return <>
    <svg className="hcurve" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"><line x1="0" x2={w} y1={y(0)} y2={y(0)} stroke="var(--border)" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      <polyline fill="none" stroke="var(--ok)" strokeWidth="2" vectorEffect="non-scaling-stroke" points={seg(0, peakI)} />
      {peakI < v.length - 1 && <polyline fill="none" stroke="var(--bad)" strokeWidth="2" vectorEffect="non-scaling-stroke" points={seg(peakI, v.length - 1)} />}</svg>
    <div className="hlegend">{hm(points[0][0])} → {hm(points[points.length - 1][0])} · peak <b className={cls(v[peakI])}>{signed(v[peakI])}</b> at {hm(points[peakI][0])} · now <b className={cls(v[v.length - 1])}>{signed(v[v.length - 1])}</b></div></>;
}

// Who made the day: one segment per position by |P&L| (green / red), then the top winners and the losers. Fixed height.
function Contribution({ positions }) {
  const rows = (positions || []).filter((p) => p.pnl).sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl));
  const total = rows.reduce((a, p) => a + Math.abs(p.pnl), 0);
  const byName = new Map();                                 // the legend is per name: a name held in two books is one entry
  rows.forEach((p) => { const m = byName.get(p.symbol) || { symbol: p.symbol, pnl: 0, books: [] }; m.pnl += p.pnl; m.books.push(p.account); byName.set(p.symbol, m); });
  const names = [...byName.values()];
  const win = names.filter((p) => p.pnl > 0).sort((a, b) => b.pnl - a.pnl), lose = names.filter((p) => p.pnl < 0).sort((a, b) => a.pnl - b.pnl);
  const item = (p) => <span key={p.symbol} title={`${p.symbol} · ${p.books.join(' + ')} · ${sgn(p.pnl)}`}><b>{shortName(p.symbol)}</b> <span className={cls(p.pnl)}>{sgn(p.pnl)}</span></span>;
  return <>
    <div className="hcontrib" aria-label="Contribution to today's P&L">{rows.filter((p) => p.pnl > 0).concat(rows.filter((p) => p.pnl < 0)).map((p) => <i key={p.book + p.symbol} className={p.pnl > 0 ? 'g' : 'r'} style={{ width: `${total ? (100 * Math.abs(p.pnl)) / total : 0}%` }} title={`${p.symbol} · ${signed(p.pnl)}`} />)}</div>
    <div className="hcl">{win.length ? <>{win.slice(0, 4).map(item)}{win.length > 4 && <span>+{win.length - 4} more</span>}</> : <span>no winners yet</span>}</div>
    <div className="hcl">{lose.length ? <>{lose.slice(0, 4).map(item)}{lose.length > 4 && <span>+{lose.length - 4} more</span>}</> : <span>no losers</span>}</div></>;
}

// One tile per name. A name in both Real and Paper E+ is one tile tagged R·P (value = the sum, P&L % = Real's).
function PositionsMap({ positions, onStock, view }) {
  const tagOf = { real: 'R', paper_E: 'P', intraday: 'I', futures: 'F', options: 'O' };
  const merged = new Map();
  (positions || []).filter((p) => p.entry && p.qty && (view === 'both' || (view === 'real' ? p.book === 'real' : p.book === 'paper_E'))).forEach((p) => {
    const m = merged.get(p.symbol) || { symbol: p.symbol, value: 0, books: [], pct: null, accounts: [] };
    m.value += p.entry * p.qty; m.books.push(p.book); m.accounts.push(`${p.account} ${pct(p.pct)}`);
    if (m.pct == null || p.book === 'real') m.pct = p.pct;
    merged.set(p.symbol, m);
  });
  const rows = [...merged.values()].sort((a, b) => b.value - a.value);
  if (!rows.length) return <div className="hmap"><p className="muted small">No open positions{view === 'both' ? '' : ` in ${view === 'real' ? 'Real' : 'Paper E+'}`}.</p></div>;
  const shade = (x) => { const a = Math.min(1, Math.abs(x || 0) / 6); return x >= 0 ? `color-mix(in srgb, var(--ok) ${25 + 65 * a}%, var(--panel))` : `color-mix(in srgb, var(--bad) ${25 + 65 * a}%, var(--panel))`; };
  const order = ['real', 'paper_E', 'futures', 'options', 'intraday'];
  const MAX = 14, more = rows.length - MAX;
  return <div className="hmap">{rows.slice(0, more > 0 ? MAX : rows.length).map((p) => <div key={p.symbol} className="tile" style={{ background: shade(p.pct) }} title={`${p.symbol} · ${p.accounts.join(' · ')} · ${inr(p.value)}`} onClick={() => onStock(p.symbol)}>
      <b>{shortName(p.symbol)}</b><small>{pct(p.pct)}</small><em>{order.filter((b) => p.books.includes(b)).map((b) => tagOf[b]).join('·')}</em></div>)}
    {more > 0 && <div className="tile more" title={rows.slice(MAX).map((p) => `${p.symbol} ${pct(p.pct)}`).join('\n')}><b>+{more} more</b></div>}</div>;
}

// The card opens to the right of the pointer (to its left at the window's edge) and under the hovered row, or
// above the row when there is no room below: never over the row under the pointer.
const shortStrategy = (s) => (/E\+/.test(s || '') ? 'E+' : /flow/i.test(s || '') ? 'Flow' : /spread/i.test(s || '') ? 'Opt' : s || '');
function HoverCard({ p, at }) {
  const [c, setC] = useState(null);
  const ref = useRef(null);
  const [h, setH] = useState(190);
  useEffect(() => { let on = true; api.getCandles(p.symbol, 60).then((d) => on && setC(d.candles || [])).catch(() => on && setC([])); return () => { on = false; }; }, [p.symbol]);
  useLayoutEffect(() => { if (ref.current) setH(ref.current.offsetHeight); });
  const v = (c || []).map((x) => x.close);
  const all = [...v, p.entry, p.stop].filter((x) => x != null);
  const lo = Math.min(...all), hi = Math.max(...all), w = 280, h80 = 80;
  const y = (x) => h80 - 4 - ((x - lo) / (hi - lo || 1)) * (h80 - 8);
  const top = at.below + 6 + h <= window.innerHeight ? at.below + 6 : Math.max(4, at.above - 6 - h);
  return <div ref={ref} className="hhover" style={{ left: at.x, top }}>
    <div className="t"><b>{p.symbol}</b> <span className="muted small">{p.account}</span><span className={`mono ${cls(p.pnl)}`} style={{ marginLeft: 'auto' }}>{signed(p.pnl)}</span></div>
    <div className="muted small">{p.side} {p.qty} @ {p.entry} · since {p.opened} · stop {p.stop ?? '—'}{p.stop_dist != null ? ` (${p.stop_dist}% away)` : ''}</div>
    {v.length > 1 ? <svg viewBox={`0 0 ${w} ${h80}`} preserveAspectRatio="none"><polyline fill="none" stroke="var(--muted)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" points={v.map((x, i) => `${(i / (v.length - 1)) * w},${y(x)}`).join(' ')} />
      {p.entry && <><line x1="0" x2={w} y1={y(p.entry)} y2={y(p.entry)} stroke="var(--blue)" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" /><text x="4" y={y(p.entry) - 3} fill="var(--blue)" fontSize="9">entry {p.entry}</text></>}
      {p.stop && <><line x1="0" x2={w} y1={y(p.stop)} y2={y(p.stop)} stroke="var(--bad)" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" /><text x="4" y={y(p.stop) + 10} fill="var(--bad)" fontSize="9">stop {p.stop}</text></>}</svg>
      : <div className="muted small" style={{ height: 80 }}>{c ? 'no candles' : 'loading…'}</div>}
    <div className="muted small">last 60 sessions · click the row for the full chart</div></div>;
}

// The trail column: not armed yet → how far to arming; armed → active, and whether the stop was raised.
const trailCell = (p) => {
  const t = p.trail;
  if (!t) return <span className="trail">—</span>;
  if (t.active) return <span className="trail on" title={`E+ late trail active${t.raised ? ' · the stop was raised' : ''}${t.best != null ? ` · best ${t.best}` : ''}`}>🪜 active{t.raised ? ' · raised' : ''}</span>;
  return <span className="trail" title={`${pct(p.pct)} so far · the trail arms at +${Math.round((p.pct || 0) + (t.pct_to_arm || 0))}%`}>🪜 arms at +{Math.round((p.pct || 0) + (t.pct_to_arm || 0))}%</span>;
};
const FILTERS = { all: () => true, near: (p) => p.stop_dist != null && p.stop_dist <= 5, trail: (p) => p.trail?.active, results: (p) => p.results_in != null && p.results_in <= 5 };
const SORTS = { pnl: (p) => p.pnl ?? -Infinity, pct: (p) => p.pct ?? -Infinity, stop: (p) => p.stop_dist ?? Infinity };

export default function HomePage({ onGo, onStock }) {
  const o = useLive('overview');
  const stamp = useLiveStamp('overview');
  const { data: sch, now } = useSchedule();
  const att = useAttention();
  const phone = useIsPhone();
  const [hover, setHover] = useState(null);
  const [toasts, setToasts] = useState([]);
  const [mapView, setMapViewRaw] = useState(() => remembered('bp.home.map', 'both'));
  const setMapView = (v) => { setMapViewRaw(v); remember('bp.home.map', v); };
  const [filter, setFilter] = useState('all');
  const [view, setView] = useState('all');
  const [sort, setSort] = useState({ key: 'pnl', dir: 'desc' });
  const [picked, setPicked] = useState(null);                 // timeline events chosen for the Activity panel
  const stale = useStale(stamp, 180000, o?.feed === 'live');
  const flashMap = useMemo(() => Object.fromEntries((o?.positions || []).map((p) => [p.book + p.symbol, p.pnl])), [o]);
  const flash = useFlashKeys(flashMap);
  const needs = att ? Object.entries(att).filter(([, a]) => a?.level && a.level !== 'running') : [];
  const needRows = useMemo(() => { const m = new Map();                 // one line per message, every page it belongs to
    needs.forEach(([tab, a]) => (a.items || []).forEach((x) => { const r = m.get(x.text) || { text: x.text, level: x.level, tabs: [] }; if (!r.tabs.includes(tab)) r.tabs.push(tab); m.set(x.text, r); }));
    return [...m.values()]; }, [att]);
  const needIds = needRows.map((x) => x.text);
  const freshNeeds = useNewIds('bp.needs', needIds, 600000);
  const actIds = (o?.activity || []).map((a) => `${a.at}|${a.text}`);
  const freshAct = useNewIds('bp.act', actIds, 30000);
  useUnread(freshNeeds.length > 0);
  const seenToast = useRef(new Set());
  useEffect(() => {                                        // money events only: an order sent, a stop hit, a failure
    (o?.activity || []).forEach((a) => {
      const id = `${a.at}|${a.text}`;
      if (!freshAct.includes(id) || seenToast.current.has(id) || !/sent|failed|stop|EXIT|SELL/i.test(a.text)) return;
      seenToast.current.add(id);
      setToasts((t) => [...t, { id, text: `${a.account} · ${a.text}`, at: a.at }]);
    });
  }, [freshAct, o]);
  if (!o) return <div className="rd"><Panel><p className="muted small rempty">Loading…</p></Panel></div>;
  const A = o.accounts || {}, it = A.intraday || {}, R = o.risk || {}, S = o.stats || {}, P = o.pulse || {}, F = o.facts || {}, D = o.drift, N = o.next_open;
  const hm24 = (t) => (t ? new Date(`2000-01-01T${t}:00`).toLocaleTimeString('en-IN', { hour: 'numeric', minute: '2-digit', hour12: true }) : '');
  const ptsOf = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v} pts`);
  const niftyPts = (acc) => {                                 // return since start against NIFTY rebased to the same start, in points
    if (acc.nifty != null) return +(acc.return - acc.nifty).toFixed(1);
    const n = acc.spark_nifty || [];
    return n.length > 1 && acc.return != null ? +(acc.return - ((n[n.length - 1] / n[0]) - 1) * 100).toFixed(1) : null;
  };
  const jobs = (sch?.jobs || []).filter((j) => !j.skip);
  const positions = o.positions || [];
  const net = positions.reduce((a, p) => a + (p.pnl || 0), 0);
  const liveNow = positions.some((p) => p.live) || o.feed === 'live';
  const swing = (o.today || 0) - (it.day_pnl || 0);

  // the positions table: view (which books), quick filter, sort inside each book
  const realSyms = new Map(positions.filter((p) => p.book === 'real').map((p) => [p.symbol, p.qty]));
  const paperSyms = new Map(positions.filter((p) => p.book === 'paper_E').map((p) => [p.symbol, p.qty]));
  const differs = (p) => (p.book === 'real' ? !paperSyms.has(p.symbol) || paperSyms.get(p.symbol) !== p.qty : p.book === 'paper_E' ? !realSyms.has(p.symbol) || realSyms.get(p.symbol) !== p.qty : false);
  const inView = (p) => (view === 'all' ? true : view === 'real' ? p.book === 'real' : view === 'paper' ? p.book === 'paper_E' : differs(p));
  const counts = { near: positions.filter(FILTERS.near).length, trail: positions.filter(FILTERS.trail).length, results: positions.filter(FILTERS.results).length };
  const shown = positions.filter(inView).filter(FILTERS[filter] || FILTERS.all);
  const sorter = (a, b) => { const d = SORTS[sort.key](a) - SORTS[sort.key](b); return sort.dir === 'desc' ? -d : d; };
  const groups = ['real', 'paper_E', 'futures', 'options', 'intraday'].map((b) => [b, shown.filter((p) => p.book === b).sort(sorter)]).filter(([, ps]) => ps.length);
  const flip = (key) => setSort((s) => ({ key, dir: s.key === key && s.dir === 'desc' ? 'asc' : 'desc' }));
  const arrow = (key) => (sort.key === key ? (sort.dir === 'desc' ? ' ▾' : ' ▴') : '');
  // Strategy is the short tag at 60 px; under 1440 px Entry and Trail are dropped (nw) so the Stock column keeps 140 px+
  const POS_COLS = [{ h: 'Stock', ell: true }, { h: 'Strat.', w: 60, lo: true, title: 'strategy' }, { h: 'Qty', w: 56, n: true }, { h: 'Entry', w: 80, n: true, lo: true, nw: true }, { h: 'Now', w: 84, n: true },
    { h: `P&L${arrow('pnl')}`, key: 'pnl', w: 86, n: true, onClick: () => flip('pnl'), title: 'sort by P&L' }, { h: `%${arrow('pct')}`, key: 'pct', w: 60, n: true, onClick: () => flip('pct'), title: 'sort by %' },
    { h: `Stop · distance${arrow('stop')}`, key: 'stop', w: 150, onClick: () => flip('stop'), title: 'sort by the distance to the stop' }, { h: 'Trail · events', w: 150, lo: true, nw: true }];
  const posRows = groups.flatMap(([b, ps]) => [
    `${BOOK[b] || b} · ${ps.length} · ${signed(ps.reduce((a, p) => a + (p.pnl || 0), 0))} · ${inr(ps.reduce((a, p) => a + (p.entry || 0) * (p.qty || 0), 0))} ${b === 'futures' ? 'notional' : b === 'options' ? 'premium' : 'in use'}`,
    ...ps.map((p) => { const f = flash[p.book + p.symbol]; return { key: p.book + p.symbol, cls: f ? `row-${f}` : '', onClick: () => onStock(p.symbol), cells: [
      <><b>{p.symbol}</b>{p.stop_dist != null && p.stop_dist < 2 && <Tag c="w">near stop</Tag>}{p.results_in != null && p.results_in <= 3 && <Tag c="w" title={`results on ${dayName(p.results_date)} (${p.results_in} session${p.results_in === 1 ? '' : 's'})`}>⚠ results {weekday(p.results_date)}</Tag>}{f === 'new' && <Tag c="a">new</Tag>}</>,
      <span className={/E\+/.test(p.strategy || '') ? 'vE' : 'muted'} title={p.strategy}>{shortStrategy(p.strategy)}</span>, p.qty, p.entry,
      <><span className={f && f !== 'new' ? `flash-${f}` : ''}>{p.now ?? '—'}</span><span className={`tick ${f === 'up' ? 'up' : f === 'down' ? 'down' : ''}`}>{f === 'up' ? '▲' : f === 'down' ? '▼' : ''}</span></>,
      <span className={`${cls(p.pnl)} ${f && f !== 'new' ? `flash-${f}` : ''}`}>{signed(p.pnl)}</span>, <span className={cls(p.pct)}>{pct(p.pct)}</span>,
      p.book === 'options' ? <span className="small mono" title={p.stop ? `the sold leg's premium stop · ${p.stop_dist?.toFixed(0)}% of the stop premium still to go` : 'the hedge exits with the sold leg'}>{p.stop ? `stop ${p.stop} · ${p.stop_dist?.toFixed(0)}%` : 'with the sold leg'}</span>
        : p.now && p.stop ? <StopMeter last={p.side === 'short' ? -p.now : p.now} stop={p.side === 'short' ? -p.stop : p.stop} /> : '—',
      trailCell(p)] }; })]);
  const hoverOn = (e, p, tr) => { if (phone) return; const r = tr.getBoundingClientRect();
    setHover({ p, at: { x: e.clientX + 16 + 300 <= window.innerWidth ? e.clientX + 16 : Math.max(4, e.clientX - 16 - 300), below: r.bottom, above: r.top } }); };

  // activity, optionally filtered to the timeline's picked events (same minute, or the same text)
  const pickedTimes = picked ? picked.map((e) => e.t) : null, pickedTexts = picked ? picked.map((e) => e.text) : null;
  const activity = picked ? (o.activity || []).filter((a) => pickedTimes.includes(hm24of(a.at)) || pickedTexts.some((t) => a.text.includes(t) || t.includes(a.text))) : (o.activity || []);
  const heldResults = S.results_held || [];
  const closeDate = P.close_date || P.as_of;
  const pulseWhen = P.live ? 'live' : closeDate ? `${dayName(closeDate)} close` : '';
  const vsNifty = (f) => (f?.vs_nifty != null ? ptsOf(f.vs_nifty) : '—');
  const cardTip = (parts) => parts.filter(Boolean).join(' · ');

  return <div className="rd">
    <HealthStrip onGo={onGo} compact />
    <Timeline events={o.timeline} jobs={jobs} now={now} feed={o.feed} phone={phone} picked={picked ? picked.map(evId) : null} onPick={(items) => setPicked(items && items.length ? items : null)} />
    <Cards n={4 + (A.follow && A.follow.breakouts > 0 ? 1 : 0) + (A.futures ? 1 : 0) + (A.options ? 1 : 0)}>
      <Card label="Today · all books" value={signed(o.today)} num={o.today} k={cls(o.today)} spark={(o.today_curve || []).map((p) => p[1])} stale={stale} live={liveNow}
        sub={<>intraday <span className={cls(it.day_pnl)}>{sgn(it.day_pnl)}</span> · swing <span className={cls(swing)}>{sgn(swing)}</span></>}
        fact={F.today ? <>high <b className="up">{sgn(F.today.high)}</b> {hm24(F.today.high_at)} · low <b className="down">{sgn(F.today.low)}</b></> : <span className="muted">high / low from the open</span>}
        title={cardTip([`intraday ${signed(it.day_pnl)}`, `swing last session ${signed(swing)}`, F.today && `high ${signed(F.today.high)} at ${hm24(F.today.high_at)}`, F.today && `low ${signed(F.today.low)} at ${hm24(F.today.low_at)}`])} />
      {A.real && <Card label={<>Real <span className={A.real.on ? 'up' : 'muted'}>· {A.real.on ? 'ON' : 'OFF'}{A.real.variant ? ` · ${A.real.variant === 'E' ? 'E+' : A.real.variant}` : ''}</span></>} value={inr(A.real.value)} num={A.real.value}
        sub={<><span className={cls(A.real.day_pnl)}>{sgn(A.real.day_pnl)}</span> last session{F.real ? <> · {F.real.slots} of {F.real.max_pos} slots</> : <> · {A.real.positions} positions</>}</>} spark={A.real.spark} spark2={A.real.spark_nifty} stale={stale}
        fact={<>vs NIFTY <b className={cls(niftyPts(A.real))}>{ptsOf(niftyPts(A.real))}</b> since start</>}
        title={cardTip([`${A.real.positions} positions`, `${pct(A.real.return)} since start`, F.real && `${inr(F.real.in_use)} in use of ${inr(F.real.capital)}`, F.real && `${F.real.slots} of ${F.real.max_pos} slots`, 'corner chart: Real (solid) against NIFTY (dashed) since start'])} />}
      {A.paper_E && <Card label="Paper E+ · swing" value={pct(A.paper_E.return)} num={A.paper_E.value} k={cls(A.paper_E.return)} spark={A.paper_E.spark} spark2={A.paper_E.spark_nifty}
        sub={<>{inr(A.paper_E.value)} · <span className={cls(A.paper_E.day_pnl)}>{sgn(A.paper_E.day_pnl)}</span> last session</>}
        fact={<>filter <b className={/ON|FULL/.test(F.paper_E?.filter || '') ? 'up' : 'muted'}>{F.paper_E?.filter || '—'}</b> · vs NIFTY <b className={cls(F.paper_E?.vs_nifty)}>{vsNifty(F.paper_E)}</b></>}
        title={cardTip([`${inr(A.paper_E.value)}`, `${signed(A.paper_E.day_pnl)} last session`, `${A.paper_E.positions} open`, F.paper_E?.filter && `filter ${F.paper_E.filter}`, `vs NIFTY ${vsNifty(F.paper_E)} since start`, 'corner chart: Paper E+ (solid) against NIFTY (dashed)'])} />}
      {A.real && A.paper_E && <Card label="Real vs Paper E+" value={D && D.names ? signed(D.diff) : '—'} num={D?.diff} k={D && D.names ? cls(D.diff) : ''} spark={D?.spark}
        sub={D && D.names ? `same ${D.names} name${D.names === 1 ? '' : 's'} · fills and slippage` : 'no name held in both books yet'}
        fact={D && D.names && D.pct_of_paper != null ? <>drift <Bar pct={Math.abs(D.pct_of_paper)} bad={D.diff < 0} ok={D.diff >= 0} /> <b className={cls(D.diff)}>{pct(D.pct_of_paper)}</b> of paper P&amp;L</> : <span className="muted">—</span>}
        title={D && D.names ? cardTip([`Real ${signed(D.real_pnl)} vs Paper E+ ${signed(D.paper_pnl)} on ${D.symbols.join(', ')}`, `difference ${signed(D.diff)}`, D.pct_of_paper != null && `${pct(D.pct_of_paper)} of the paper P&L`]) : 'What live execution costs: Real against Paper E+ on the names both books hold'} />}
      {A.futures && <Card label={<>Futures <span className={A.futures.mode === 'real' ? 'up' : 'muted'}>· {A.futures.mode}</span>{A.futures.killed && <span className="down"> · killed</span>}</>} value={inr(A.futures.value)} num={A.futures.value} k={cls(A.futures.net)} spark={A.futures.spark} spark2={A.futures.spark_nifty} stale={stale}
        sub={<><span className={cls(A.futures.day_pnl)}>{sgn(A.futures.day_pnl)}</span> today · {F.futures ? <>{F.futures.lots} of {F.futures.max_pos} lots</> : <>{A.futures.positions} lots</>}</>}
        fact={<>margin <Bar pct={A.futures.margin_pct || 0} bad={(A.futures.margin_pct || 0) >= 80} ok={(A.futures.margin_pct || 0) < 70} /> <b className={(A.futures.margin_pct || 0) >= 70 ? 'warn' : ''}>{Math.round(A.futures.margin_pct || 0)}%</b> · {pct(A.futures.return)}</>}
        title={cardTip([`Futures · ${A.futures.mode}`, `${A.futures.positions} lots`, `${pct(A.futures.return)} since start`, `margin ${Math.round(A.futures.margin_pct || 0)}% of capital`, 'corner chart: the book (solid) against NIFTY (dashed)'])} onClick={() => onGo('futures')} />}
      {A.options && <Card label={<>Options <span className="muted">· paper</span>{A.options.killed && <span className="down"> · killed</span>}</>} value={inr(A.options.value)} num={A.options.value} k={cls(A.options.net)} spark={A.options.spark} stale={stale} live={A.options.positions > 0}
        sub={<><span className={cls(A.options.day_pnl)}>{sgn(A.options.day_pnl)}</span> today · {A.options.positions ? <>1 spread open · {F.options?.lots ?? A.options.lots} lot{(F.options?.lots ?? A.options.lots) === 1 ? '' : 's'}</> : F.options?.status === 'no_trade' ? 'no trade today' : 'no spread open'}</>}
        fact={<>margin <Bar pct={A.options.margin_pct || 0} ok={(A.options.margin_pct || 0) < 50} /> <b>{Math.round(A.options.margin_pct || 0)}%</b> · {A.options.bias ? <span className={A.options.bias === 'BULLISH' ? 'up' : 'down'}>{A.options.bias === 'BULLISH' ? '↑ bullish' : '↓ bearish'}</span> : 'no bias'} · {pct(A.options.return)}</>}
        title={cardTip(['Options · paper · NIFTY weekly credit spreads', A.options.positions ? `1 spread open · ${A.options.lots} lots` : 'no spread open', A.options.bias && `bias ${A.options.bias}`, `${pct(A.options.return)} since start`, `margin estimate ${Math.round(A.options.margin_pct || 0)}% of capital`])} onClick={() => onGo('options')} />}
      {A.follow && A.follow.breakouts > 0 && <Card label="Follow (blind)" value={signed(A.follow.net)} num={A.follow.net} k={cls(A.follow.net)} sub={`${A.follow.breakouts} breakouts · won ${A.follow.win_rate ?? '—'}%`}
        fact={F.follow ? <><b>{F.follow.open}</b> open · <b className={cls(F.follow.open_pnl)}>{sgn(F.follow.open_pnl)}</b> · {F.follow.pending ?? 0} pending</> : <span className="muted">—</span>}
        title={cardTip([`${A.follow.breakouts} breakouts`, `won ${A.follow.win_rate ?? '—'}%`, `${inr(A.follow.per_position)} each`, F.follow && `${F.follow.open} open at ${signed(F.follow.open_pnl)}`, F.follow && `${F.follow.pending ?? 0} buys at the next open`, A.follow.as_of && `as of ${dayName(A.follow.as_of)}`])} />}
    </Cards>
    {!S.error && <div className={`hstats ${phone ? 'phone' : ''}`}>
      <div title={cardTip([`intraday · ${it.mode === 'paper' ? 'paper' : it.mode || 'off'}`, `${it.trades || 0} trades`, F.intraday && `loss limit ${inr(F.intraday.loss_used)} / ${inr(F.intraday.loss_limit)}`])}><span className="l">Intraday · open · risk</span><b className={cls(it.day_pnl)}>{sgn(it.day_pnl)}<span className="muted" style={{ fontWeight: 400, fontSize: 12 }}> · {it.open || 0} open · {inr(it.at_risk)}</span></b></div>
      <div><span className="l">Win streak</span><b className={S.win_streak ? 'up' : ''}>{S.win_streak || 0} day{S.win_streak === 1 ? '' : 's'}</b></div>
      <div><span className="l">Best day · 30 d</span><b className="up">{S.best_day ? `${signed(S.best_day.net)} · ${dayName(S.best_day.day)}` : '—'}</b></div>
      <div><span className="l">Worst day · 30 d</span><b className="down">{S.worst_day ? `${signed(S.worst_day.net)} · ${dayName(S.worst_day.day)}` : '—'}</b></div>
      <div><span className="l">Days since a loss</span><b>{S.days_since_loss ?? '—'}</b></div>
      <div><span className="l">Signals today · hit rate 30 d</span><b>{S.signals_today ?? 0} · {S.hit_rate_30d != null ? `${S.hit_rate_30d}%` : '—'}</b></div>
      <div><span className="l">Real orders this week</span><b className={S.orders_failed_week ? 'down' : ''}>{S.orders_week ?? 0} · {S.orders_failed_week ?? 0} failed</b></div>
      <div title={`intraday · ${it.mode === 'paper' ? 'paper' : it.mode || 'off'} · ${it.trades || 0} trades today`}><span className="l">Intraday loss limit · signals</span><b>{inr(F.intraday?.loss_used ?? R.loss_used)} / {inr(F.intraday?.loss_limit ?? it.loss_limit)}{F.intraday?.per_day ? <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}> · {F.intraday.signals} of {F.intraday.per_day}</span> : null}</b></div>
    </div>}
    {!phone && <div className="hthree">
      <Panel title="Today, live" right="all books · one point a minute"><div className="hbody"><TodayCurve points={o.today_curve} today={o.today} /><Contribution positions={positions} /></div></Panel>
      <Panel title="Positions map" right={<Seg small opts={[['both', 'Both'], ['real', 'Real'], ['paper', 'Paper']]} v={mapView} set={setMapView} />}>
        <div className="hbody"><PositionsMap positions={positions} onStock={onStock} view={mapView} /><div className="hlegend" style={{ marginTop: 6 }}>biggest first · colour = P&amp;L · R·P = held in Real and Paper E+</div></div></Panel>
      <Panel title="Market pulse" right={pulseWhen} id="pulse">
        <div className="hbody"><div className="hmini">
          <div title={P.proxy ? 'from the daily candles (a proxy for the live feed)' : undefined}><span className="l">Regime</span><b className={P.regime === 'correction' ? 'down' : P.regime === 'uptrend' ? 'up' : ''}>{P.regime || '—'}</b><small>{P.dist_days != null ? `${P.dist_days} distribution day${P.dist_days === 1 ? '' : 's'}` : 'distribution days —'}</small></div>
          <div><span className="l">Breadth · above 50-day</span><b>{P.breadth_50 != null ? `${P.breadth_50}%` : '—'}</b><small>{P.breadth_50 != null ? 'share of the universe' : 'needs the evening scan'}</small></div>
          <div><span className="l">Breakouts working</span><b>{P.breakouts?.rate != null ? `${P.breakouts.working ?? ''} / ${P.breakouts.total ?? ''} · ${Math.round(P.breakouts.rate * 100)}%` : '—'}</b><small>last sessions</small></div>
          <div title={P.proxy ? 'proxy: from the last close, not the live feed' : undefined}><span className="l">Advancing · declining</span><b>{P.advancing != null ? <><span className="up">{P.advancing}</span> · <span className="down">{P.declining}</span></> : <span className="muted">{P.live ? '—' : 'feed off'}</span>}</b><small>{P.live ? 'live' : P.as_of_close && closeDate ? `at close ${dayName(closeDate)}` : ''}</small></div>
          <div title={P.proxy ? 'proxy: from the last close, not the live feed' : undefined}><span className="l">Above VWAP · NIFTY</span><b>{P.above_vwap_pct != null ? `${P.above_vwap_pct}% · ` : ''}{P.nifty_vs_vwap ? <span className={P.nifty_vs_vwap === 'above' ? 'up' : 'down'}>{P.nifty_vs_vwap}</span> : <span className="muted">—</span>}</b><small>{P.live ? 'live' : P.as_of_close && closeDate ? `at close ${dayName(closeDate)}` : ''}</small></div>
          <div title={heldResults.map((r) => `${r.symbol} · ${dayName(r.date)}`).join('\n') || 'no held name reports this week'}><span className="l">Results this week · held</span><b className={heldResults.length ? 'warn' : ''}>{heldResults.length}</b><small>{heldResults.length ? `${heldResults.slice(0, 2).map((r) => `${r.symbol} ${weekday(r.date)}`).join(' · ')}${heldResults.length > 2 ? ` · +${heldResults.length - 2}` : ''}` : 'none of the held names'}</small></div>
        </div></div></Panel>
    </div>}
    <div className="two">
      <Panel title="Open positions" meta={positions.length} right={phone ? <>{liveNow && <span className="livedot" />}<select value={view} onChange={(e) => setView(e.target.value)} style={{ fontSize: 11.5, padding: '2px 6px', marginLeft: 'auto' }} title="which books">
          <option value="all">Real + Paper</option><option value="real">Real</option><option value="paper">Paper</option><option value="diff">Differences</option></select></>
        : <>{liveNow && <span className="livedot" />}net <b className={`mono ${cls(net)}`}>{signed(net)}</b>{R.worst_case != null && <> · worst case <b className="mono down">{signed(R.worst_case)}</b></>}{R.near_stop?.length ? <> · <span className="warn">{R.near_stop.length} within 2% of a stop</span></> : null} · <button className="linkbtn" style={{ padding: 0 }} onClick={() => onGo('portfolio')}>Portfolio →</button></>}>
        {phone ? <PhonePositions positions={positions} groups={groups} counts={counts} filter={filter} setFilter={setFilter} sort={sort} setSort={setSort} net={net} worstCase={R.worst_case} flash={flash} onStock={onStock} onGo={onGo} differs={differs} /> : <>
          <div className="hfilters">
            <Seg small opts={[['all', 'All'], ['near', `Within 5% of stop (${counts.near})`], ['trail', `Trail armed (${counts.trail})`], ['results', `Results this week (${counts.results})`]]} v={filter} set={setFilter} />
            <Seg small opts={[['all', 'Real + Paper'], ['real', 'Real'], ['paper', 'Paper'], ['diff', 'Differences', 'names held in one book but not the other, or with a different quantity']]} v={view} set={setView} />
          </div>
          <div onMouseLeave={() => setHover(null)} onMouseOver={(e) => { const tr = e.target.closest('tr'); const key = tr?.dataset?.key; const p = key && positions.find((x) => x.book + x.symbol === key); if (p) hoverOn(e, p, tr); }}>
            <FixedTable className="hpos" cols={POS_COLS} rows={posRows} empty={positions.length ? 'Nothing matches this filter.' : 'No open positions.'} /></div></>}
      </Panel>
      <div className="rd">
        <Panel title="Needs a look" meta={needIds.length || null} right={freshNeeds.length > 0 ? <span className="warn">● {freshNeeds.length} new</span> : null}>
          {needRows.length ? <div className="rbody"><div className="v2list">{needRows.map((x) => <div key={x.text} className={`it hact ${freshNeeds.includes(x.text) ? 'fresh' : ''}`}>
            <span className={`i ${x.level === 'warn' || x.level === 'sell' ? 'warn' : ''}`}>{x.level === 'sell' ? '↓' : x.level === 'buy' ? '↑' : x.level === 'warn' ? '!' : 'i'}</span><span style={{ flex: 1 }}>{x.text}</span>
            {x.tabs.map((tab) => <button key={tab} className="btn ghost small" onClick={() => onGo(tab)}>{tab}</button>)}</div>)}</div></div> : <p className="muted small rempty">Nothing right now.</p>}</Panel>
        {!R.error && <Panel title="Risk now" right="all books"><div className="rbody">
          <div className="hrisk"><div><b className="mono">{inr(R.in_use)}</b><div className="muted small">in open positions{R.capital ? ` · ${Math.round(100 * R.in_use / R.capital)}% of ${inr(R.capital)} capital` : ''}</div></div>
            <div className="bar"><i style={{ width: `${R.capital ? Math.min(100, 100 * R.in_use / R.capital) : 0}%` }} /></div></div>
          <div className="hmini">
            <div><span className="l">Worst case to the stops</span><b className="down mono">{signed(R.worst_case)}</b></div>
            <div><span className="l">Biggest position</span><b className="mono">{R.biggest ? `${R.biggest.symbol} ${inr(R.biggest.value)}` : '—'}</b></div>
            <div><span className="l">Real orders today</span><b className={R.orders_today?.failed ? 'down' : 'up'}>{R.orders_today?.sent ?? 0} sent · {R.orders_today?.failed ?? 0} failed</b></div>
            <div><span className="l">Intraday loss limit</span><b className="mono">{inr(R.loss_used)} / {inr(R.loss_limit)}</b></div></div></div></Panel>}
        <Panel title="Next up"><div className="rbody"><div className="v2list">{jobs.slice(0, 5).map((j) => <div key={j.key} className="it"><span>{j.icon}</span><span className="ell1">{j.name}</span><span className="mono" style={{ marginLeft: 'auto' }}>{left(j.at, now)}</span></div>)}</div></div></Panel>
        {N && <Panel title={`Next open · ${(N.buys || []).length} breakout${(N.buys || []).length === 1 ? '' : 's'}`} right={N.as_of ? `plan of ${dayName(N.as_of)}` : null}>
          <FixedTable cols={[{ h: 'Stock', ell: true }, { h: 'Qty', w: 60, n: true }, { h: 'Capital', w: 100, n: true }, { h: 'Stop', w: 76, n: true, title: 'stop below the entry' }]}
            rows={[...(N.buys || []).slice(0, 5).map((b) => ({ key: b.symbol, onClick: () => onStock(b.symbol), cells: [<b>{b.symbol}</b>, b.qty, inr(b.value), b.stop_pct != null ? `${(+b.stop_pct).toFixed(1)}%` : '—'] })),
              `${(N.buys || []).length > 5 ? `+${(N.buys || []).length - 5} more · ` : ''}total ${inr(N.total_value)} · fits ${N.fits ?? 0} of ${N.slots_free ?? 0} slot${N.slots_free === 1 ? '' : 's'}${N.filter ? ` · filter ${N.filter}` : ''}`]} empty="No buys for the next open." />
          <div className="rnote">{N.fits === 0 ? 'E+ is full: these fire only if a position exits at the open' : `${N.fits} of ${(N.buys || []).length} fit the free slots at the open`}<button className="linkbtn" onClick={() => onGo('breakout')}>Breakout page →</button></div></Panel>}
        <Panel title="Activity" meta={picked ? `${activity.length} of ${(o.activity || []).length}` : (o.activity || []).length || null} right={picked ? <Chip c="" title="click to show every event" onClick={() => setPicked(null)}>filtered by the timeline · ✕</Chip> : null}>
          {!activity.length ? <p className="muted small rempty">{picked ? 'No activity matches these events.' : 'Nothing yet.'}</p>
            : <div className="rbody"><div className="v2list">{activity.map((a, i) => { const trail = /stop raised|trail/i.test(a.text); return <div key={i} className={`it ${freshAct.includes(`${a.at}|${a.text}`) ? 'row-new' : ''}`}><span className="t">{at12(a.at)}</span><span><b>{a.account}</b> · {a.text}{trail && <span style={{ color: 'var(--purple)', marginLeft: 6 }} title="trailing stop">🪜</span>}</span></div>; })}</div></div>}</Panel>
      </div>
    </div>
    {hover && !phone && <HoverCard p={hover.p} at={hover.at} />}
    {toasts.length > 0 && <div className="htoasts">{toasts.slice(-3).map((t) => <div key={t.id} className="htoast"><b>{t.text}</b><span className="muted small">{at12(t.at)}</span><button className="linkbtn" onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}>dismiss</button></div>)}</div>}
  </div>;
}
