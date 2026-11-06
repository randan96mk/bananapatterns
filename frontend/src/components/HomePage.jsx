import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useLive, useLiveStamp } from '../services/live';
import { useSchedule, left } from './StatusBar';
import { useAttention } from './AttentionStrip';
import { StopMeter } from './ui';
import { inr, pct, cls, signed } from '../fmt';
import { useIsPhone } from '../useMedia';
import { useBeat, useFlashKeys, useStale, useUnread, useNewIds } from '../heartbeat';
import * as api from '../services/api';
import HealthStrip from './HealthStrip';
import { Card, Spark, Bar } from './ui2';

// 🏠 Home: the day on one screen. Timeline, account cards with sparklines, the stats strip, today's live P&L, the
// positions map, the market pulse, positions by book, what needs a look, risk, next up, activity. Every block
// pulses when its own data changes (heartbeat.js).
const at12 = (s) => {
  if (!s) return '';
  const d = new Date(s.length > 10 ? s : `${s}T00:00:00`);
  return isNaN(d) ? s : d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true });
};
const hm = (s) => (s ? new Date(s).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit', hour12: true }) : '');
const dayName = (s) => (s ? new Date(s).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const BOOK = { real: 'Real', paper_A: 'Paper A', paper_E: 'Paper E+', intraday: 'Intraday' };
const mins = (t) => (t ? +t.slice(0, 2) * 60 + +t.slice(3, 5) : 0);
const pctOfDay = (t) => 3 + Math.max(0, Math.min(1, (mins(t) - 555) / 375)) * 94;   // 9:15 → 15:30, with a margin

function Timeline({ events, jobs, now, feed, phone }) {
  const nowT = new Date(now).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' });
  const upcoming = (jobs || []).map((j) => ({ t: new Date(j.at).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' }), kind: 'job', text: j.name }))
    .filter((j) => j.t >= '09:15' && j.t <= '15:30');
  const all = [...(events || []), ...upcoming].filter((e) => e.t && e.t >= '09:00' && e.t <= '15:45').sort((a, b) => a.t.localeCompare(b.t));
  const fresh = useNewIds('bp.tl', (events || []).map((e) => `${e.t}|${e.text}`));
  const col = { order: 'var(--ok)', failed: 'var(--bad)', signal: 'var(--blue)', exit: 'var(--bad)', job: 'var(--muted)' };
  const clusters = [];                                        // events closer than 1.5% of the day share one dot
  all.forEach((e) => { const x = pctOfDay(e.t); const c = clusters[clusters.length - 1]; if (c && x - c.x < 1.5) { c.items.push(e); c.x2 = x; } else clusters.push({ x, x2: x, items: [e] }); });
  const show = feed === 'live' || (nowT >= '09:15' && nowT <= '15:30');
  return <div className={`htl ${phone ? 'phone' : ''}`} aria-label="Today's timeline">
    <div className="past" style={{ width: `${pctOfDay(nowT)}%` }} />
    {(phone ? ['09:15', '12:00', '15:30'] : ['09:15', '10:30', '12:00', '13:30', '15:30']).map((t) => <span key={t} className={`h ${t === '09:15' ? 'first' : t === '15:30' ? 'last' : ''}`} style={{ left: `${pctOfDay(t)}%` }}>{t}</span>)}
    {clusters.map((c, i) => { const tip = c.items.map((e) => `${e.t} · ${e.text}`).join('\n'); const ping = c.items.some((e) => fresh.includes(`${e.t}|${e.text}`));
      const kind = c.items.find((e) => e.kind !== 'job')?.kind || 'job';
      return c.items.length > 1
        ? <span key={i} className={`cl ${ping ? 'ping' : ''}`} style={{ left: `${(c.x + c.x2) / 2}%` }} title={tip}>{c.items.length}</span>
        : <span key={i} className={`ev ${ping ? 'ping' : ''}`} style={{ left: `${c.x}%` }} title={tip}><i style={{ background: col[kind] || 'var(--muted)' }} /></span>; })}
    {show && <div className="now" style={{ left: `${pctOfDay(nowT)}%` }}>{!phone && <span>now {hm(now)}</span>}</div>}
  </div>;
}

function TodayCurve({ points, today }) {
  const v = (points || []).map((p) => p[1]);
  if (v.length < 2) return <p className="muted small">The live curve draws from the open, one point a minute{today != null ? ` · now ${signed(today)}` : ''}.</p>;
  const lo = Math.min(0, ...v), hi = Math.max(0, ...v), w = 600, h = 110;
  const y = (x) => h - 6 - ((x - lo) / (hi - lo || 1)) * (h - 12);
  const peakI = v.indexOf(Math.max(...v));
  const seg = (a, b) => v.slice(a, b + 1).map((x, i) => `${((a + i) / (v.length - 1)) * w},${y(x)}`).join(' ');
  return <>
    <svg className="hcurve" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"><line x1="0" x2={w} y1={y(0)} y2={y(0)} stroke="var(--border)" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      <polyline fill="none" stroke="var(--ok)" strokeWidth="2" vectorEffect="non-scaling-stroke" points={seg(0, peakI)} />
      {peakI < v.length - 1 && <polyline fill="none" stroke="var(--bad)" strokeWidth="2" vectorEffect="non-scaling-stroke" points={seg(peakI, v.length - 1)} />}</svg>
    <div className="muted small">{hm(points[0][0])} → {hm(points[points.length - 1][0])} · peak <b className={cls(v[peakI])}>{signed(v[peakI])}</b> at {hm(points[peakI][0])} · now <b className={cls(v[v.length - 1])}>{signed(v[v.length - 1])}</b></div></>;
}

function PositionsMap({ positions, onStock }) {
  const rows = (positions || []).filter((p) => p.entry && p.qty).map((p) => ({ ...p, value: p.entry * p.qty })).sort((a, b) => b.value - a.value);
  if (!rows.length) return <div className="hmap"><p className="muted small">No open positions.</p></div>;
  const total = rows.reduce((a, p) => a + p.value, 0), nRows = Math.max(1, Math.min(4, Math.ceil(rows.length / 5)));
  const lanes = [];                                           // rows of similar total value, at most ~5 tiles each, biggest first
  let lane = [], sum = 0;
  rows.forEach((p) => { lane.push(p); sum += p.value; if ((sum >= total / nRows || lane.length >= Math.ceil(rows.length / nRows)) && lanes.length < nRows - 1) { lanes.push(lane); lane = []; sum = 0; } });
  if (lane.length) lanes.push(lane);
  const shade = (x) => { const a = Math.min(1, Math.abs(x || 0) / 6); return x >= 0 ? `color-mix(in srgb, var(--ok) ${25 + 65 * a}%, var(--panel))` : `color-mix(in srgb, var(--bad) ${25 + 65 * a}%, var(--panel))`; };
  const short = { real: 'Real', paper_A: 'Paper A', paper_E: 'Paper E+', intraday: 'Intraday' };
  return <div className="hmap">{lanes.map((ps, i) => <div key={i} className="lane" style={{ flex: `${ps.reduce((a, p) => a + p.value, 0)} 1 0%` }}>
    {ps.map((p) => <div key={p.book + p.symbol} className="tile" style={{ flex: `${p.value} 1 0%`, background: shade(p.pct) }} title={`${p.symbol} · ${p.account} · ${inr(p.value)} · ${pct(p.pct)}`} onClick={() => onStock(p.symbol)}>
      <b>{p.symbol}</b><small>{pct(p.pct)}</small><em>{short[p.book] || p.book}</em></div>)}</div>)}</div>;
}

function HoverCard({ p, at }) {
  const [c, setC] = useState(null);
  useEffect(() => { let on = true; api.getCandles(p.symbol, 60).then((d) => on && setC(d.candles || [])).catch(() => on && setC([])); return () => { on = false; }; }, [p.symbol]);
  const v = (c || []).map((x) => x.close);
  const all = [...v, p.entry, p.stop].filter((x) => x != null);
  const lo = Math.min(...all), hi = Math.max(...all), w = 280, h = 80;
  const y = (x) => h - 4 - ((x - lo) / (hi - lo || 1)) * (h - 8);
  return <div className="hhover" style={{ left: at.x, top: at.y }}>
    <div className="t"><b>{p.symbol}</b> <span className="muted small">{p.account}</span><span className={`mono ${cls(p.pnl)}`} style={{ marginLeft: 'auto' }}>{signed(p.pnl)}</span></div>
    <div className="muted small">{p.side} {p.qty} @ {p.entry} · since {p.opened} · stop {p.stop ?? '—'}{p.stop_dist != null ? ` (${p.stop_dist}% away)` : ''}</div>
    {v.length > 1 ? <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"><polyline fill="none" stroke="var(--muted)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" points={v.map((x, i) => `${(i / (v.length - 1)) * w},${y(x)}`).join(' ')} />
      {p.entry && <><line x1="0" x2={w} y1={y(p.entry)} y2={y(p.entry)} stroke="var(--blue)" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" /><text x="4" y={y(p.entry) - 3} fill="var(--blue)" fontSize="9">entry {p.entry}</text></>}
      {p.stop && <><line x1="0" x2={w} y1={y(p.stop)} y2={y(p.stop)} stroke="var(--bad)" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" /><text x="4" y={y(p.stop) + 10} fill="var(--bad)" fontSize="9">stop {p.stop}</text></>}</svg>
      : <div className="muted small" style={{ height: 80 }}>{c ? 'no candles' : 'loading…'}</div>}
    <div className="muted small">last 60 sessions · click the row for the full chart</div></div>;
}

export default function HomePage({ onGo, onStock }) {
  const o = useLive('overview');
  const stamp = useLiveStamp('overview');
  const { data: sch, now } = useSchedule();
  const att = useAttention();
  const phone = useIsPhone();
  const [hover, setHover] = useState(null);
  const [toasts, setToasts] = useState([]);
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
  if (!o) return <div className="v2card muted">Loading…</div>;
  const A = o.accounts || {}, it = A.intraday || {}, R = o.risk || {}, S = o.stats || {}, P = o.pulse || {}, F = o.facts || {};
  const hm24 = (t) => (t ? new Date(`2000-01-01T${t}:00`).toLocaleTimeString('en-IN', { hour: 'numeric', minute: '2-digit', hour12: true }) : '');
  const factPaper = (f) => f && <>vs NIFTY <b className={cls(f.vs_nifty)}>{f.vs_nifty != null ? `${f.vs_nifty > 0 ? '+' : ''}${f.vs_nifty} pts` : '—'}</b>{f.filter ? <> · filter <b className={/ON|FULL/.test(f.filter) ? 'up' : 'muted'}>{f.filter}</b></> : null}</>;
  const jobs = (sch?.jobs || []).filter((j) => !j.skip);
  const groups = ['real', 'paper_A', 'paper_E', 'intraday'].map((b) => [b, o.positions.filter((p) => p.book === b)]).filter(([, ps]) => ps.length);
  const net = o.positions.reduce((a, p) => a + (p.pnl || 0), 0);
  const liveNow = o.positions.some((p) => p.live) || o.feed === 'live';
  return <>
    <HealthStrip onGo={onGo} compact={phone} />
    <Timeline events={o.timeline} jobs={jobs} now={now} feed={o.feed} phone={phone} />
    <div className="kpis">
      <Card label="Today · all books" value={signed(o.today)} num={o.today} k={cls(o.today)} sub={`intraday ${signed(it.day_pnl)} · swing last session ${signed((o.today || 0) - (it.day_pnl || 0))}`} spark={(o.today_curve || []).map((p) => p[1])} stale={stale} live={liveNow}
        fact={F.today ? <>high <b className="up">{signed(F.today.high)}</b> {hm24(F.today.high_at)} · low <b className="down">{signed(F.today.low)}</b> {hm24(F.today.low_at)}</> : <span className="muted">high / low from the open</span>} />
      {A.real && <Card label={<>Real <span className={A.real.on ? 'up' : 'muted'}>· {A.real.on ? 'ON' : 'OFF'}{A.real.variant ? ` · ${A.real.variant === 'E' ? 'E+' : A.real.variant}` : ''}</span></>} value={inr(A.real.value)} num={A.real.value}
        sub={<><span className={cls(A.real.day_pnl)}>{signed(A.real.day_pnl)}</span> last session · {A.real.positions} positions · {pct(A.real.return)} since start</>} spark={A.real.spark} stale={stale}
        fact={F.real && <><b>{inr(F.real.in_use)}</b> in use · {F.real.slots} of {F.real.max_pos} slots<Bar pct={F.real.capital ? 100 * F.real.in_use / F.real.capital : 0} /></>} />}
      {A.paper_A && <Card label="Paper A · swing" value={pct(A.paper_A.return)} num={A.paper_A.value} k={cls(A.paper_A.return)} sub={<>{inr(A.paper_A.value)} · <span className={cls(A.paper_A.day_pnl)}>{signed(A.paper_A.day_pnl)}</span> last session · {A.paper_A.positions} open</>} spark={A.paper_A.spark} fact={factPaper(F.paper_A)} />}
      {A.paper_E && <Card label="Paper E+ · swing" value={pct(A.paper_E.return)} num={A.paper_E.value} k={cls(A.paper_E.return)} sub={<>{inr(A.paper_E.value)} · <span className={cls(A.paper_E.day_pnl)}>{signed(A.paper_E.day_pnl)}</span> last session · {A.paper_E.positions} open</>} spark={A.paper_E.spark} fact={factPaper(F.paper_E)} />}
      <Card label={`Intraday · ${it.mode === 'paper' ? 'paper' : it.mode || 'off'}`} value={signed(it.day_pnl)} num={it.day_pnl} k={cls(it.day_pnl)} sub={`${it.open || 0} open · ${it.trades || 0} trades · risk ${inr(it.at_risk)}`} spark={it.spark} stale={stale} live={o.feed === 'live'}
        fact={F.intraday && <>loss limit <b>{inr(F.intraday.loss_used)}</b> / {inr(F.intraday.loss_limit)}{F.intraday.per_day ? <> · signals <b>{F.intraday.signals}</b> of {F.intraday.per_day}</> : null}<Bar pct={F.intraday.loss_limit ? 100 * F.intraday.loss_used / F.intraday.loss_limit : 0} bad /></>} />
      {A.follow && A.follow.breakouts > 0 && <Card label="Screens · Follow (blind)" value={signed(A.follow.net)} num={A.follow.net} k={cls(A.follow.net)} sub={`${A.follow.breakouts} breakouts · won ${A.follow.win_rate ?? '—'}% · ${inr(A.follow.per_position)} each`}
        fact={F.follow && <><b>{F.follow.open}</b> open · <b className={cls(F.follow.open_pnl)}>{signed(F.follow.open_pnl)}</b> · buys next open <b>{F.follow.pending ?? 0}</b></>} />}
    </div>
    {!S.error && <div className={`hstats ${phone ? 'phone' : ''}`}>
      <div><span className="l">Win streak</span><b className={S.win_streak ? 'up' : ''}>{S.win_streak || 0} day{S.win_streak === 1 ? '' : 's'}</b></div>
      <div><span className="l">Best day · 30 d</span><b className="up">{S.best_day ? `${signed(S.best_day.net)} · ${dayName(S.best_day.day)}` : '—'}</b></div>
      <div><span className="l">Worst day · 30 d</span><b className="down">{S.worst_day ? `${signed(S.worst_day.net)} · ${dayName(S.worst_day.day)}` : '—'}</b></div>
      <div><span className="l">Days since a loss</span><b>{S.days_since_loss ?? '—'}</b></div>
      <div><span className="l">Signals today · hit rate 30 d</span><b>{S.signals_today ?? 0} · {S.hit_rate_30d != null ? `${S.hit_rate_30d}%` : '—'}</b></div>
      <div><span className="l">Real orders this week</span><b className={S.orders_failed_week ? 'down' : ''}>{S.orders_week ?? 0} · {S.orders_failed_week ?? 0} failed</b></div>
    </div>}
    {!phone && <div className="hthree">
      <div className="v2card"><h3>Today, live <span className="r">all books · one point a minute</span></h3><TodayCurve points={o.today_curve} today={o.today} /></div>
      <div className="v2card"><h3>Positions map <span className="r">size = value · colour = P&amp;L</span></h3><PositionsMap positions={o.positions} onStock={onStock} /></div>
      <div className="v2card"><h3>Market pulse <span className="r">{P.live ? 'live' : P.as_of ? `${dayName(P.as_of)} close` : ''}</span></h3>
        <div className="hmini">
          <div><span className="l">Regime</span><b className={P.regime === 'correction' ? 'down' : P.regime === 'uptrend' ? 'up' : ''}>{P.regime || '—'}</b></div>
          <div><span className="l">Breadth · above 50-day</span><b>{P.breadth_50 != null ? `${P.breadth_50}%` : '—'}</b></div>
          <div><span className="l">Distribution days</span><b className={P.dist_days >= 5 ? 'down' : ''}>{P.dist_days ?? '—'}</b></div>
          <div><span className="l">Breakouts working</span><b>{P.breakouts?.rate != null ? `${P.breakouts.working ?? ''}/${P.breakouts.total ?? ''} · ${Math.round(P.breakouts.rate * 100)}%` : '—'}</b></div>
          <div><span className="l">Advancing · declining</span><b>{P.advancing != null ? <><span className="up">{P.advancing}</span> · <span className="down">{P.declining}</span></> : <span className="muted">feed off</span>}</b></div>
          <div><span className="l">Above VWAP · NIFTY</span><b>{P.above_vwap_pct != null ? `${P.above_vwap_pct}% · ` : ''}{P.nifty_vs_vwap ? <span className={P.nifty_vs_vwap === 'above' ? 'up' : 'down'}>{P.nifty_vs_vwap} VWAP</span> : <span className="muted">—</span>}</b></div>
        </div></div>
    </div>}
    <div className="v2grid">
      <div className="v2card"><h3>Open positions <span className="achip">{o.positions.length}</span>{liveNow && <span className="livedot" />}
        <span className="r">net <b className={cls(net)}>{signed(net)}</b>{!phone && R.worst_case != null && <> · worst case to the stops <b className="down">{signed(R.worst_case)}</b></>}{!phone && R.near_stop?.length ? <> · <span className="warn">{R.near_stop.length} within 2% of a stop</span></> : null} · <button className="linkbtn" onClick={() => onGo('portfolio')}>Portfolio →</button></span></h3>
        {!o.positions.length ? <p className="muted">No open positions.</p> : phone
          ? <div className="hprows">{groups.map(([b, ps]) => <React.Fragment key={b}><div className="grp">{BOOK[b] || b} · {ps.length} · <span className={cls(ps.reduce((a, p) => a + (p.pnl || 0), 0))}>{signed(ps.reduce((a, p) => a + (p.pnl || 0), 0))}</span></div>
            {ps.map((p) => { const f = flash[p.book + p.symbol]; return <div key={p.book + p.symbol} className={`prow ${f ? `row-${f}` : ''}`} onClick={() => onStock(p.symbol)}>
              <div className="n"><b>{p.symbol}</b>{p.stop_dist != null && p.stop_dist < 2 && <span className="tag warn" style={{ marginLeft: 6 }}>near stop</span>}<small>{p.qty} @ {p.entry} · now <span className={f && f !== 'new' ? `flash-${f}` : ''}>{p.now ?? '—'}</span>{f === 'up' ? ' ▲' : f === 'down' ? ' ▼' : ''}</small></div>
              <div className="p"><b className={`mono ${cls(p.pnl)}`}>{signed(p.pnl)}</b><small className={cls(p.pct)}>{pct(p.pct)}</small></div>
              <div>{p.now && p.stop ? <StopMeter last={p.side === 'short' ? -p.now : p.now} stop={p.side === 'short' ? -p.stop : p.stop} /> : <span className="muted small">—</span>}</div></div>; })}</React.Fragment>)}</div>
          : <div className="tablewrap"><table className="grid hpos"><colgroup><col style={{ width: 124 }} /><col style={{ width: 136 }} /><col style={{ width: 52 }} /><col style={{ width: 84 }} /><col style={{ width: 96 }} /><col style={{ width: 92 }} /><col style={{ width: 66 }} /><col /></colgroup><thead><tr><th>Stock</th><th>Strategy</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Now</th><th className="num">P&amp;L</th><th className="num">%</th><th>Stop · distance</th></tr></thead>
            <tbody>{groups.map(([b, ps]) => <React.Fragment key={b}>
              <tr className="grp"><td colSpan={8}>{BOOK[b] || b} · {ps.length} · <span className={cls(ps.reduce((a, p) => a + (p.pnl || 0), 0))}>{signed(ps.reduce((a, p) => a + (p.pnl || 0), 0))}</span> · {inr(ps.reduce((a, p) => a + (p.entry || 0) * (p.qty || 0), 0))} in use</td></tr>
              {ps.map((p) => { const f = flash[p.book + p.symbol]; return <tr key={p.book + p.symbol} className={f ? `row-${f}` : ''} style={{ cursor: 'pointer' }} onClick={() => onStock(p.symbol)}
                onMouseEnter={(e) => setHover({ p, at: { x: Math.min(e.clientX + 12, window.innerWidth - 320), y: e.clientY + 12 } })} onMouseLeave={() => setHover(null)}>
                <td><b>{p.symbol}</b>{p.stop_dist != null && p.stop_dist < 2 && <span className="tag warn" style={{ marginLeft: 6 }}>near stop</span>}{f === 'new' && <span className="tag" style={{ marginLeft: 6, color: 'var(--blue)' }}>new</span>}</td>
                <td className="muted">{p.strategy}</td><td className="num mono">{p.qty}</td><td className="num mono">{p.entry}</td>
                <td className="num mono"><span className={f && f !== 'new' ? `flash-${f}` : ''}>{p.now ?? '—'}</span><span className={`tick ${f === 'up' ? 'up' : f === 'down' ? 'down' : ''}`}>{f === 'up' ? '▲' : f === 'down' ? '▼' : ''}</span></td>
                <td className={`num mono ${cls(p.pnl)}`}><span className={f && f !== 'new' ? `flash-${f}` : ''}>{signed(p.pnl)}</span></td><td className={`num mono ${cls(p.pct)}`}>{pct(p.pct)}</td>
                <td>{p.now && p.stop ? <StopMeter last={p.side === 'short' ? -p.now : p.now} stop={p.side === 'short' ? -p.stop : p.stop} /> : '—'}</td></tr>; })}
            </React.Fragment>)}</tbody></table></div>}
      </div>
      <div>
        <div className="v2card"><h3>Needs a look {needIds.length > 0 && <span className="achip">{needIds.length}</span>}{freshNeeds.length > 0 && <span className="r warn">● {freshNeeds.length} new</span>}</h3>
          {needRows.length ? <div className="v2list">{needRows.map((x) => <div key={x.text} className={`it hact ${freshNeeds.includes(x.text) ? 'fresh' : ''}`}>
            <span className={`i ${x.level === 'warn' || x.level === 'sell' ? 'warn' : ''}`}>{x.level === 'sell' ? '↓' : x.level === 'buy' ? '↑' : x.level === 'warn' ? '!' : 'i'}</span><span style={{ flex: 1 }}>{x.text}</span>
            {x.tabs.map((tab) => <button key={tab} className="btn ghost small" onClick={() => onGo(tab)}>{tab}</button>)}</div>)}</div> : <p className="muted small">Nothing right now.</p>}</div>
        {!R.error && <div className="v2card"><h3>Risk now <span className="r">all books</span></h3>
          <div className="hrisk"><div><b className="mono">{inr(R.in_use)}</b><div className="muted small">in open positions{R.capital ? ` · ${Math.round(100 * R.in_use / R.capital)}% of ${inr(R.capital)} capital` : ''}</div></div>
            <div className="bar"><i style={{ width: `${R.capital ? Math.min(100, 100 * R.in_use / R.capital) : 0}%` }} /></div></div>
          <div className="hmini">
            <div><span className="l">Worst case to the stops</span><b className="down mono">{signed(R.worst_case)}</b></div>
            <div><span className="l">Biggest position</span><b className="mono">{R.biggest ? `${R.biggest.symbol} ${inr(R.biggest.value)}` : '—'}</b></div>
            <div><span className="l">Real orders today</span><b className={R.orders_today?.failed ? 'down' : 'up'}>{R.orders_today?.sent ?? 0} sent · {R.orders_today?.failed ?? 0} failed</b></div>
            <div><span className="l">Intraday loss limit</span><b className="mono">{inr(R.loss_used)} / {inr(R.loss_limit)}</b></div></div></div>}
        <div className="v2card"><h3>Next up</h3><div className="v2list">{jobs.slice(0, 5).map((j) => <div key={j.key} className="it"><span>{j.icon}</span><span>{j.name}</span><span className="mono" style={{ marginLeft: 'auto' }}>{left(j.at, now)}</span></div>)}</div></div>
        <div className="v2card"><h3>Activity</h3>{!o.activity.length ? <p className="muted small">Nothing yet.</p>
          : <div className="v2list">{o.activity.map((a, i) => <div key={i} className={`it ${freshAct.includes(`${a.at}|${a.text}`) ? 'row-new' : ''}`}><span className="t">{at12(a.at)}</span><span><b>{a.account}</b> · {a.text}</span></div>)}</div>}</div>
      </div>
    </div>
    {hover && !phone && <HoverCard p={hover.p} at={hover.at} />}
    {toasts.length > 0 && <div className="htoasts">{toasts.slice(-3).map((t) => <div key={t.id} className="htoast"><b>{t.text}</b><span className="muted small">{at12(t.at)}</span><button className="linkbtn" onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}>dismiss</button></div>)}</div>}
  </>;
}
