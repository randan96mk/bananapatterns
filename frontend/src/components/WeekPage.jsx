import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { inr } from '../fmt';
import { useIsPhone } from '../useMedia';
import { Panel, FixedTable, AiTag } from './ui2';

// 📖 Portfolio › Week: the week as a story (GET /api/week?week=2026-W46). Every number is code from the trade and
// equity rows: the headline, the curve (all books against NIFTY on the same scale), Mon–Fri, the numbers, by book.
// Up to three observations come from the local model once a week (Sunday evening), each checked by code against the
// rows; each carries the AI tag and a numbered pin on the day it is about. A past week without them shows none
// (never back-filled by itself): "Write now" queues the job for that week, and nothing else happens.
const money = (v) => (v == null ? '—' : v > 0 ? `+${inr(v)}` : inr(v));
const tone = (v) => (v == null || v === 0 ? '' : v > 0 ? 'up' : 'down');
const pts = (v) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)} pts`);
const pctOf = (v) => (v == null ? null : Math.abs(v) <= 1 ? Math.round(v * 100) : Math.round(v));
const toDate = (d) => (d ? new Date(`${String(d).slice(0, 10)}T00:00:00`) : null);
// "Mon 16" / "Fri 20 Nov" / "20 Nov" (no locale comma)
const dLabel = (d, opts = { weekday: 'short', day: 'numeric' }) => {
  const x = toDate(d);
  if (!x || isNaN(x)) return String(d || '');
  const part = (o) => x.toLocaleDateString('en-IN', o);
  return [opts.weekday && part({ weekday: 'short' }), opts.day && part({ day: 'numeric' }), opts.month && part({ month: 'short' })].filter(Boolean).join(' ');
};
const wday = (d) => dLabel(d, { weekday: 'short' });
const textOf = (x) => (x == null ? '' : typeof x === 'string' ? x : x.text || '');
const endOf = (t) => (t?.ended == null ? null : /^\d{4}-\d\d-\d\d/.test(String(t.ended)) ? `closed ${dLabel(t.ended, { day: 'numeric', month: 'short' })}` : t.ended);
const weekNo = (w) => (/W(\d+)/.exec(w || '') || [])[1];
const BOOKS = { real: ['💰', 'Real'], paper: ['📝', 'Paper E+'], paper_E: ['📝', 'Paper E+'], futures: ['📐', 'Futures · paper'], options: ['🧩', 'Options · paper'], signals: ['⚡', 'Signal track'], signal: ['⚡', 'Signal track'] };

// which by_day index an observation is about (a date, a weekday name, or "week" = the last day)
function dayIndex(o, days) {
  const d = String(o.day || '').trim();
  if (!d || !days.length) return -1;
  if (/^week$/i.test(d)) return days.length - 1;
  let i = days.findIndex((x) => String(x.day).slice(0, 10) === d.slice(0, 10));
  if (i < 0) i = days.findIndex((x) => wday(x.day).toLowerCase() === d.slice(0, 3).toLowerCase());
  return i;
}

function Curve({ curve, days, obs, h, phone, label }) {
  const pts0 = (curve || []).filter((p) => p && (p.all_books != null || p.nifty != null));
  if (pts0.length < 2) return <p className="muted small rempty">The curve needs at least two daily points in this week.</p>;
  const W = 1000, vals = pts0.flatMap((p) => [p.all_books, p.nifty]).filter((v) => v != null);
  const mn = Math.min(...vals, 0), mx = Math.max(...vals, 0), pad = (mx - mn || 1) * 0.08;
  const y = (v) => (h - 24 - ((v - mn + pad) / (mx - mn + 2 * pad)) * (h - 44)).toFixed(1);
  // the x axis is the week's days (Mon–Fri), plus a start slot when the curve begins before Monday (the week's base)
  const dayKeys = days.map((x) => String(x.day || '').slice(0, 10));
  const first = String(pts0[0].t || '').slice(0, 10);
  const off = dayKeys.length && first && first < dayKeys[0] ? 1 : 0;
  const slots = Math.max(dayKeys.length + off, pts0.length, 2);
  const slotOf = (p, j) => { const k = dayKeys.indexOf(String(p.t || '').slice(0, 10)); return k >= 0 ? k + off : j; };
  const frac = (idx) => idx / (slots - 1);
  const x = (j) => (frac(slotOf(pts0[j], j)) * W).toFixed(1);
  const line = (k) => pts0.map((p, i) => (p[k] == null ? null : `${x(i)},${y(p[k])}`)).filter(Boolean).join(' ');
  const xOfDay = (i) => frac(i + off) * 100;                            // percent across, for a day of the strip
  const lastX = pts0.length ? x(pts0.length - 1) : W;
  const allLine = line('all_books'), nLine = line('nifty');
  return <div className="wk-curve" style={{ height: h }}>
    <svg viewBox={`0 0 ${W} ${h}`} preserveAspectRatio="none" aria-hidden="true">
      {days.slice(1).map((_, i) => { const bx = ((xOfDay(i) + xOfDay(i + 1)) / 2) * 10; return <line key={i} x1={bx} x2={bx} y1="0" y2={h} className="vgrid" />; })}
      <line x1="0" x2={W} y1={y(0)} y2={y(0)} className="zero" />
      {allLine && <polygon className="area" points={`${x(0)},${y(0)} ${allLine} ${lastX},${y(0)}`} />}
      {nLine && <polyline fill="none" className="nifty" vectorEffect="non-scaling-stroke" points={nLine} />}
      {allLine && <polyline fill="none" className="all" vectorEffect="non-scaling-stroke" points={allLine} />}
    </svg>
    {days.map((d, i) => <span key={d.day || i} className="dl" style={{ left: `clamp(26px, ${xOfDay(i)}%, calc(100% - 26px))` }}>{dLabel(d.day)}</span>)}
    <span className="lg">{label}</span>
    {!phone && obs.map((o, k) => {
      const i = dayIndex(o, days);
      if (i < 0) return null;
      const left = xOfDay(i), top = k % 2 === 0;
      return <div key={k} className={`wk-pin ${top ? 'top' : 'bot'}`} style={{ left: `clamp(104px, ${left}%, calc(100% - 104px))` }} title={textOf(o)}>
        <div className="b"><span className="pn">{k + 1}</span><span className="tx"><AiTag of={o} /> {i >= 0 && !/^week$/i.test(o.day || '') ? `${wday(days[i].day)}: ` : ''}{textOf(o)}</span></div></div>;
    })}
  </div>;
}

export default function WeekPage({ onGo }) {
  const phone = useIsPhone();
  const [week, setWeek] = useState(null);                          // null = the current ISO week
  const [d, setD] = useState(undefined);
  const [err, setErr] = useState('');
  const [writing, setWriting] = useState(false);
  const [msg, setMsg] = useState(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    let alive = true;
    setErr('');
    api.getWeek(week || undefined).then((r) => { if (alive) setD(r || null); }).catch((e) => { if (alive) { setErr(e.message); setD(null); } });
    return () => { alive = false; };
  }, [week, n]);
  if (d === undefined) return <div className="rd"><div className="rbanner">Loading the week…</div></div>;
  if (!d) return <div className="rd"><div className="rbanner bad">The week could not be loaded{err ? `: ${err}` : ''}.</div></div>;

  const T = d.totals || {}, days = (d.by_day || []).slice(0, 5), obs = (d.observations || []).filter((o) => textOf(o));
  const best = d.best, worst = d.worst, S = d.suggestions, V = d.ai_view, B = d.backtest_hint;
  const wk = weekNo(d.week);
  const span = `${d.from ? dLabel(d.from, { weekday: 'short', day: 'numeric' }) : ''}${d.to ? ` → ${dLabel(d.to, { weekday: 'short', day: 'numeric', month: 'short' })}` : ''}`;
  const meta = d.story || d.observations_meta || {};
  const writtenAt = meta.at || d.observed_at || d.written_at;
  const rowsChecked = meta.rows_checked ?? meta.rows ?? T.trades;
  const running = !!(meta.running || d.writing);
  const pinOf = (i) => obs.map((o, k) => (dayIndex(o, days) === i ? k + 1 : null)).filter(Boolean);
  const nav = (w) => { if (w) { setWeek(w); setMsg(null); } };

  const copy = () => {
    const lines = [`Week ${wk || d.week} · ${span}`, d.headline && textOf(d.headline), `${money(T.net)} · ${T.trades ?? 0} trades · ${T.won ?? 0} won · ${pts(T.vs_nifty_pts)} vs NIFTY`,
      ...days.map((x) => `${dLabel(x.day)}: ${money(x.net)} · ${x.trades ?? 0} trades`),
      ...(d.by_book || []).map((b) => `${(BOOKS[b.book] || ['', b.book])[1]}: ${money(b.net)} · ${b.trades ?? 0} trades · ${b.won ?? 0} won`),
      ...obs.map((o, k) => `${k + 1}. [AI] ${textOf(o)}`)].filter(Boolean).join('\n');
    const done = () => setMsg({ text: '✓ summary copied' });
    const fail = () => setMsg({ text: 'copy failed: the browser blocked the clipboard', bad: true });
    try {
      if (navigator.clipboard?.writeText) navigator.clipboard.writeText(lines).then(done, fail);
      else { const t = document.createElement('textarea'); t.value = lines; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); done(); }
    } catch { fail(); }
  };
  const write = () => {
    if (writing) return;
    setWriting(true); setMsg(null);
    api.writeWeek(d.week).then(() => { setMsg({ text: 'queued · the night model writes them in a minute or two; this page re-reads in 60 s' }); setTimeout(() => setN((x) => x + 1), 60000); })
      .catch((e) => setMsg({ text: e.status === 409 ? 'already running for this week · check back in a minute' : e.message, bad: e.status !== 409 }))
      .finally(() => setWriting(false));
  };
  const prefill = () => {
    if (!B) return;
    const route = B.route || (B.prefill?.strategy === 'breakout' ? 'strategies/breakout' : 'strategies/futures');
    const q = B.prefill ? new URLSearchParams(Object.entries(B.prefill).map(([k, v]) => [k, String(v)])).toString() : '';
    onGo && onGo(route.split('/').pop());
    if (q) window.history.replaceState(null, '', `${window.location.pathname}?${q}${window.location.hash}`);
  };

  const curveLabel = <><span className="up">—</span> all books {money(T.net)} · <span className="muted">- -</span> NIFTY 50{d.nifty_pct != null ? ` ${d.nifty_pct > 0 ? '+' : ''}${d.nifty_pct}%` : ''} · ₹ on the same scale</>;
  const bookRows = (d.by_book || []).map((b) => { const [ic, nm] = BOOKS[b.book] || [b.icon || '•', b.book];
    return { key: b.book, cells: [<span>{b.icon || ic} {b.label || nm}</span>, <span className={tone(b.net)}>{money(b.net)}</span>, b.trades ?? 0, b.won ?? 0] }; });
  if (bookRows.length) bookRows.push({ key: 'all', cls: 'wk-total', cells: [<b>All books</b>, <b className={tone(T.net)}>{money(T.net)}</b>, <b>{T.trades ?? 0}</b>, <b>{T.won ?? 0}</b>] });

  const obsPanel = <Panel title={<>Three observations <AiTag title="written by the local model from the week's trade rows · every figure checked by code" /></>}
    right={obs.length && writtenAt ? [stamp(writtenAt), meta.model, meta.ms != null ? `${Math.round(meta.ms / 1000)} s` : null].filter(Boolean).join(' · ') : 'not yet'}>
    {obs.length ? <div className="wk-obs">{obs.map((o, k) => { const i = dayIndex(o, days);
      return <div key={k} className="ob"><span className="pn">{k + 1}</span><div style={{ minWidth: 0 }}><div className="tx">{textOf(o)}</div>
        <div className="m"><AiTag of={o} /><span>{/^week$/i.test(o.day || '') ? 'week' : i >= 0 ? dLabel(days[i].day) : o.day || 'week'}{i >= 0 && days[i].trades != null && !/^week$/i.test(o.day || '') ? ` · ${days[i].trades} trades` : ''}</span>
          {o.checked !== false && <span>✓ figures found in the rows</span>}</div></div></div>; })}
      <div className="muted wk-fine">each observation is checked by code against the trade rows before it is shown · a figure not in the rows is stripped · a line with no claim left is dropped</div></div>
      : <div className="wk-none"><p className="muted small" style={{ margin: 0 }}>no observations for this week</p>
        <button type="button" className="btn ghost small" disabled={writing || running} onClick={write} title="queues the week job for this week (the night model, one call); nothing else changes">{writing || running ? 'writing…' : 'Write now'}</button></div>}
  </Panel>;

  return <div className="rd wk">
    <div className="wk-hero">
      <div style={{ minWidth: 0 }}>
        <div className="wk-k">📖 Week {wk || d.week}{span ? ` · ${span}` : ''}</div>
        <h1 title={textOf(d.title || d.headline)}>{textOf(d.title || d.headline) || 'A quiet week'}</h1>
        <div className="sub"><span>headline by code from the week's rows · </span>
          <span className="rchip">{obs.length ? <><AiTag /> observations written {writtenAt ? stamp(writtenAt) : ''}{rowsChecked != null ? ` · checked against ${rowsChecked} trade rows ✓` : ''}</> : <><AiTag /> observations not yet</>}</span></div>
      </div>
      <div className="wk-right">
        <div className={`big mono ${tone(T.net)}`}>{money(T.net)}</div>
        <div className="muted small">{T.trades ?? 0} trades · {T.won ?? 0} won · <b className={tone(T.vs_nifty_pts)}>{pts(T.vs_nifty_pts)}</b> vs NIFTY</div>
        <div className="wk-nav">
          <button type="button" className="btn ghost small" disabled={!d.prev_week} onClick={() => nav(d.prev_week)}>◀ {d.prev_week ? `week ${weekNo(d.prev_week) || ''}` : 'week'}</button>
          <button type="button" className="btn ghost small" disabled={!d.next_week} onClick={() => nav(d.next_week)}>{d.next_week ? `week ${weekNo(d.next_week) || ''}` : 'week'} ▶</button>
          <button type="button" className="btn ghost small" onClick={copy} title="copies a text summary of this week to the clipboard">⤓ copy summary</button></div>
        {msg && <div className={`small ${msg.bad ? 'down' : 'up'}`} style={{ marginTop: 4 }}>{msg.text}</div>}
      </div>
    </div>
    <div className="wk-story">
      <div className="wk-col">
        <Panel title="The week" right="daily points · all books · pins = the observations">
          <Curve curve={d.curve} days={days} obs={obs} h={phone ? 200 : 250} phone={phone} label={curveLabel} />
          {phone && obs.length > 0 && <div className="wk-pinlist">{obs.map((o, k) => { const i = dayIndex(o, days);
            return <div key={k}><span className="pn">{k + 1}</span><span className="muted">{i >= 0 ? dLabel(days[i].day) : 'week'}</span></div>; })}</div>}
          <div className="wk-days">{(days.length ? days : []).map((x, i) => { const pins = pinOf(i);
            return <div key={x.day || i} className="day" title={[dLabel(x.day), money(x.net), `${x.trades ?? 0} trades · ${x.won ?? 0} won`, x.nifty_pct != null ? `NIFTY ${x.nifty_pct > 0 ? '+' : ''}${x.nifty_pct}%` : null, x.note].filter(Boolean).join(' · ')}>
              <div className="d">{dLabel(x.day)}</div><div className={`v mono ${tone(x.net)}`}>{money(x.net)}</div>
              <div className="s">{x.note || `${x.trades ?? 0} trade${x.trades === 1 ? '' : 's'}${x.won != null ? ` · ${x.won} won` : ''}`}</div>
              {pins.length > 0 && <span className="pns">{pins.map((p) => <span key={p} className="pn">{p}</span>)}</span>}</div>; })}
            {!days.length && <p className="muted small" style={{ margin: 0 }}>no trading days in this week</p>}</div>
        </Panel>
        <Panel title="The numbers" right="by code">
          <div className="wk-strip">
            <div><div className="kk">Best trade</div><b className={best ? tone(best.net) : ''}>{best ? `${best.symbol} ${money(best.net)}` : '—'}</b>
              <div className="dim">{best ? [best.score != null ? `rated ${best.score}` : null, endOf(best), best.days != null ? `${best.days} days` : null, best.why].filter(Boolean).join(' · ') || ' ' : 'no closed trade'}</div></div>
            <div><div className="kk">Worst trade</div><b className={worst ? tone(worst.net) : ''}>{worst ? `${worst.symbol} ${money(worst.net)}` : '—'}</b>
              <div className="dim">{worst ? [worst.score != null ? `rated ${worst.score}` : null, endOf(worst), worst.days != null ? `${worst.days} days` : null, worst.why].filter(Boolean).join(' · ') || ' ' : 'no closed trade'}</div></div>
            <div><div className="kk">Suggestions</div><b>{S ? `${S.made ?? 0} · ${S.accepted ?? 0} accepted` : '—'}</b>
              <div className="dim">{S ? `helped ${S.helped ?? 0} of ${S.accepted ?? 0}${S.expired != null ? ` · ${S.expired} expired` : ''}` : 'none this week'}</div></div>
            <div><div className="kk">AI view · this week</div><b>{V && V.buy_hit != null ? `buy ${pctOf(V.buy_hit)}% hit@${V.horizon || 20}` : '—'}</b>
              <div className="dim">{V && V.baseline != null ? `plan baseline ${pctOf(V.baseline)}%${(V.rated ?? V.votes) != null ? ` · ${V.rated ?? V.votes} rated` : ''}` : 'not scored yet'}</div></div>
            <div><div className="kk">One backtest</div><b title={B ? textOf(B.title || B.label) : ''}>{B ? textOf(B.title || B.label) || 'a settings idea' : '—'}</b>
              <div className="dim">{B ? <button type="button" className="btn ghost small" onClick={prefill} title="opens the backtest with the fields prefilled · nothing runs until you press Run">prefill · you press Run</button> : 'none born this week'}</div></div>
          </div>
        </Panel>
      </div>
      <div className="wk-col">
        {obsPanel}
        <Panel title="By book">
          <FixedTable cols={[{ h: 'Book', ell: true }, { h: 'Week', w: 104, n: true }, { h: 'Trades', w: 64, n: true }, { h: 'Won', w: 56, n: true }]} rows={bookRows} empty="no trades in this week" />
        </Panel>
      </div>
    </div>
  </div>;
}
