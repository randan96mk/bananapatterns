import React, { useEffect, useMemo, useRef, useState } from 'react';
import * as api from '../services/api';
import { inr, cls } from '../fmt';
import { Head, Chip, Cards, Card, Bar, Panel, FixedTable, Seg, Tag, KV, Bars } from './ui2';

// 📈 Follow: every fresh breakout on the Research screens bought blindly with ₹X. Everything on the page follows the
// filters (screens, status, result, max open at once): tiles, growth chart vs NIFTY 50, drawdown, the table.
const TAG = { bases: ['BASES', '#26a69a'], vcp: ['VCP', '#c58af9'], flag: ['FLAG', '#ff8a65'] };
const day = (d) => (d ? new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' }) : '—');
const n2 = (v) => (v == null ? '—' : (+v).toLocaleString('en-IN', { maximumFractionDigits: 2 }));
const pc = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${(+v).toFixed(1)}%`);
const Tags = ({ s }) => s.map((k) => <span key={k} className="rtag" style={{ color: TAG[k]?.[1], borderColor: TAG[k]?.[1] }}>{TAG[k]?.[0] || k}</span>);
const SCREEN_F = [['all', 'All'], ['vcp', 'VCP'], ['bases', 'Bases'], ['flag', 'Flag'], ['vcp+flag', 'VCP + Flag'], ['vcp+bases', 'VCP + Bases'], ['flag+bases', 'Flag + Bases'],
  ['bases-only', 'Bases only'], ['vcp-only', 'VCP only'], ['flag-only', 'Flag only']];
const STATUS_F = [['all', 'All'], ['open', 'Open'], ['closed', 'Closed']];
const RESULT_F = [['all', 'All'], ['profit', 'Profit'], ['loss', 'Loss']];
const MAX_F = [['0', 'All'], ['3', '3'], ['5', '5'], ['10', '10']];
const CAP_F = [['0', 'All'], ['500000', '5 L'], ['1000000', '10 L'], ['2000000', '20 L']];
const STOP_F = [['1', '1%'], ['2', '2%'], ['3', '3%'], ['4', '4%'], ['5', '5%'], ['6', '6%'], ['7', '7%'], ['8', '8%']];
const BASIS_F = [['entry', 'under entry'], ['pivot', 'under pivot']];
const MKT_F = [['0', 'Off'], ['1', 'NIFTY above its 50-day line']];
const TIME_F = [['0', 'Off'], ['5', '5'], ['6', '6'], ['7', '7'], ['8', '8'], ['9', '9'], ['10', '10']];
const live = (p) => p.status !== 'closed';
const has = (p, k) => p.screens.includes(k);
const screenOk = (p, f) => f === 'all' || (f === 'vcp+flag' ? has(p, 'vcp') || has(p, 'flag') : f === 'vcp+bases' ? has(p, 'vcp') && has(p, 'bases') : f === 'flag+bases' ? has(p, 'flag') && has(p, 'bases')
  : f.endsWith('-only') ? p.screens.length === 1 && has(p, f.slice(0, -5)) : has(p, f));

// Delivery charges, as the backend counts them (STT on the sell, exchange, SEBI, GST on those, stamp duty on the buy).
const charges = (value, sell) => { const exch = value * 0.00297 / 100, sebi = value * 10 / 1e7; return value * (sell ? 0.1 : 0) / 100 + exch + sebi + (exch + sebi) * 0.18 + (sell ? 15.93 : value * 0.015 / 100); };

// The position's exits re-run on its daily path under a stop of stopPct under the pivot and a time stop of timeN
// sessions (sold at the next open if not in profit by then). Both can only sell earlier than the stored exit, so the
// stored path (entry day .. exit day) holds every day needed. pl: [[date, P&L]] day by day for the growth chart.
function applyRules(p, stopPct, basis, timeN) {
  const path = p.path || [];
  if (!path.length || !p.qty) return { ...p, pl: [] };
  const cin = p.charges_in || 0, q = p.qty, e = p.entry;
  const stop = Math.round((basis === 'entry' ? e : p.pivot) * (1 - stopPct / 100) * 100) / 100;
  const last = path.length - 1, closedOrig = p.status === 'closed';
  const mark = (c) => Math.round(((c - e) * q - cin - charges(c * q, true)) * 100) / 100;
  let exitIdx = closedOrig ? last : -1, reason = closedOrig ? p.reason : p.status === 'closing' ? p.reason : null, closing = p.status === 'closing';
  let trig = closedOrig && last > 0 ? last - 1 : closing ? last : -1;      // the day whose close fired the exit
  for (let i = 0; i < (closedOrig ? last : path.length); i += 1) {        // a closed one: never the exit day itself
    const c = path[i][2];
    const why = c <= stop ? `fell ${stopPct}% below the ${basis}` : timeN && i >= timeN && c <= e ? `not in profit after ${timeN} sessions` : null;
    if (why) { reason = why; trig = i; if (i < last) exitIdx = i + 1; else { exitIdx = -1; closing = true; } break; }
  }
  const trigger = trig >= 0 ? { date: path[trig][0], close: path[trig][2], gap: exitIdx >= 0 ? Math.round((path[exitIdx][1] / path[trig][2] - 1) * 1000) / 10 : null } : null;
  if (exitIdx >= 0) {
    const exit = path[exitIdx][1], gross = (exit - e) * q, ch = cin + charges(exit * q, true);
    const pl = path.slice(0, exitIdx).map(([d, , c]) => [d, mark(c)]).concat([[path[exitIdx][0], Math.round((gross - ch) * 100) / 100]]);
    return { ...p, stop, status: 'closed', reason, trigger, exit_date: path[exitIdx][0], exit: Math.round(exit * 100) / 100, days: exitIdx,
      gross: Math.round(gross * 100) / 100, net: Math.round((gross - ch) * 100) / 100, pct: Math.round(gross / p.value * 10000) / 100, pl };
  }
  const c = path[last][2];
  return { ...p, stop, status: closing ? 'closing' : p.status, reason: closing ? reason : null, trigger: closing ? trigger : null, exit_date: null, exit: null, days: last,
    net: mark(c), pct: Math.round((c - e) / e * 10000) / 100, last: c, pl: path.map(([d, , x]) => [d, mark(x)]) };
}
const Why = ({ p }) => <>{p.reason}{p.trigger && <span className="muted"> · {day(p.trigger.date)} close {n2(p.trigger.close)}{p.trigger.gap != null ? `, next open ${p.trigger.gap > 0 ? '+' : ''}${p.trigger.gap}%` : ''}</span>}</>;

// At most N open at once: the breakouts are taken in entry order, one that finds no free slot is skipped.
// …and / or a fixed capital: a breakout is taken only if the money already in open positions plus this trade's
// value (qty × entry) fits in it; a sale gives its money back for later breakouts (closed profit / loss does not
// change the capital). skipped: 'slot' | 'cash' | false.
function capped(rows, n, capital, mktOk) {
  const ordered = rows.filter((p) => (p.qty > 0 || live(p)) && !p.corp_action).map((p) => (mktOk && !mktOk(p.entry_date || p.signal_date) ? { ...p, skipped: 'market' } : p));
  if (!n && !capital) return ordered.map((p) => ({ ...p, skipped: p.skipped || false }));
  const open = [];                                                   // [exit date, value] of the positions taken
  return [...ordered].sort((a, b) => (a.entry_date || a.signal_date).localeCompare(b.entry_date || b.signal_date)).map((p) => {
    const at = p.entry_date || p.signal_date;
    if (p.skipped === 'market') return p;
    for (let i = open.length - 1; i >= 0; i -= 1) if (open[i][0] <= at) open.splice(i, 1);   // exits before this entry free their money
    if (n && open.length >= n) return { ...p, skipped: 'slot' };
    if (capital && open.reduce((a, [, v]) => a + v, 0) + p.value > capital) return { ...p, skipped: 'cash' };
    open.push([p.exit_date || '9999', p.value]);
    return { ...p, skipped: false };
  }).sort((a, b) => b.signal_date.localeCompare(a.signal_date));
}

// The growth curve of a set of positions, day by day: open ones at the close (after the charges a sale would take),
// closed ones at their net from the exit day on.
function curve(rows) {
  const by = {}, realised = {};
  rows.forEach((p) => {
    (p.pl || []).forEach(([d, v]) => { by[d] = (by[d] || 0) + v; });
    if (p.status === 'closed' && p.exit_date) realised[p.exit_date] = (realised[p.exit_date] || 0) + p.net;
  });
  let carry = 0;
  return Object.keys(by).sort().map((d) => { const v = by[d] + carry; carry += realised[d] || 0; return [d, Math.round(v)]; });
}

function drawdown(cv) {
  let peak = 0, from = cv[0]?.[0], sessions = 0;
  let worst = { value: 0, from: null, to: null, recovered: null, sessions: 0, peak: 0 };
  cv.forEach(([d, v]) => {
    if (v >= peak) { peak = v; from = d; sessions = 0; if (worst.to && !worst.recovered && v >= worst.peak) worst.recovered = d; }
    else { sessions += 1; if (peak - v > worst.value) worst = { value: peak - v, from, to: d, recovered: null, sessions, peak }; }
  });
  return worst;
}

function Chart({ cv, nifty, peakValue }) {
  if (!cv.length) return <p className="muted small">No held sessions yet.</p>;
  const dates = cv.map(([d]) => d);
  const nx = Object.fromEntries(nifty);
  const first = nifty.find(([d]) => d >= dates[0])?.[1];
  let lastB = null;
  const bench = first && peakValue ? dates.map((d) => { if (nx[d] != null) lastB = Math.round(peakValue * (nx[d] / first - 1)); return lastB; }) : [];
  const vals = cv.map(([, v]) => v);
  const all = [...vals, ...bench.filter((v) => v != null), 0];
  const lo = Math.min(...all), hi = Math.max(...all), h = 170, n = vals.length;
  const y = (v) => h - 8 - ((v - lo) / (hi - lo || 1)) * (h - 16);
  const x = (i) => (i / Math.max(n - 1, 1)) * 600;
  const pts = (arr) => arr.map((v, i) => (v == null ? '' : `${x(i)},${y(v)}`)).join(' ');
  return <>
    <svg width="100%" height={h} viewBox={`0 0 600 ${h}`} preserveAspectRatio="none">
      <line x1="0" x2="600" y1={y(0)} y2={y(0)} stroke="#334259" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      {bench.length > 0 && <polyline fill="none" stroke="#8b98a5" strokeWidth="1" strokeDasharray="4 3" vectorEffect="non-scaling-stroke" points={pts(bench)} />}
      <polyline fill="none" stroke="#f5c518" strokeWidth="2" vectorEffect="non-scaling-stroke" points={pts(vals)} />
    </svg>
    <div className="muted small">{day(dates[0])} – {day(dates[n - 1])} · <b style={{ color: '#f5c518' }}>yellow</b>: profit / loss of the shown positions, day by day
      {bench.length > 0 && <> · <b style={{ color: '#8b98a5' }}>dashed</b>: the same peak money ({inr(peakValue)}) held in NIFTY 50 instead</>}</div></>;
}

function csv(rows) {
  const cols = ['stock', 'screens', 'breakout', 'bought', 'entry', 'qty', 'value', 'pivot', 'stop', 'sold', 'exit_or_now', 'profit_loss', 'pct', 'days', 'status', 'why'];
  const lines = rows.map((p) => [p.symbol, p.screens.join('+'), p.breakout_date, p.entry_date || '', p.entry ?? '', p.qty, p.value, p.pivot, p.stop,
    p.exit_date || '', p.exit ?? p.last ?? '', p.net, p.pct ?? '', p.days, p.skipped ? 'skipped' : p.status, p.skipped === 'cash' ? 'no cash' : p.skipped ? 'no free slot' : p.reason || ''].join(','));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([[cols.join(','), ...lines].join('\n')], { type: 'text/csv' }));
  a.download = `follow_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

const agg = (ps) => {
  const closed = ps.filter((p) => !live(p) && p.qty), won = closed.filter((p) => p.net > 0), lost = closed.filter((p) => p.net <= 0);
  const avg = (xs) => (xs.length ? xs.reduce((a, p) => a + p.pct, 0) / xs.length : null);
  return { n: ps.filter((p) => p.qty).length, open: ps.filter(live).length, closed: closed.length,
    win_rate: closed.length ? Math.round(1000 * won.length / closed.length) / 10 : null, avg_win: avg(won), avg_loss: avg(lost),
    net: ps.reduce((a, p) => a + (p.qty ? p.net : 0), 0), open_net: ps.filter(live).reduce((a, p) => a + (p.net || 0), 0) };
};
const peakUse = (ps) => {
  const ev = [];
  ps.forEach((p) => { if (p.entry_date) { ev.push([p.entry_date, 1, p.value]); if (p.exit_date) ev.push([p.exit_date, 0, -p.value]); } });
  ev.sort((a, b) => a[0].localeCompare(b[0]) || a[1] - b[1]);
  let cur = 0, n = 0, best = { value: 0, positions: 0, date: null };
  ev.forEach(([d, k, v]) => { cur += v; n += k ? 1 : -1; if (cur > best.value) best = { value: cur, positions: n, date: d }; });
  return best;
};

// The "Try it as" selectors the board can save as the page's default (PUT /api/follow/settings {defaults}).
const DEFAULT_KEYS = ['scr', 'status', 'result', 'maxOpen', 'capital', 'stopPct', 'basis', 'timeN', 'mkt'];
const short = (v) => (v == null ? '—' : Math.abs(v) >= 1e5 ? `${v < 0 ? '−' : ''}₹${(Math.abs(v) / 1e5).toFixed(1)}L` : inr(v));

export default function FollowPage() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [scr, setScr] = useState('all');
  const [st, setSt] = useState('all');
  const [res, setRes] = useState('all');
  const [maxOpen, setMaxOpen] = useState('0');
  const [capital, setCapital] = useState('0');
  const [capBox, setCapBox] = useState('');
  const [mkt, setMkt] = useState('0');
  const [stopPct, setStopPct] = useState('8');
  const [basis, setBasis] = useState('entry');
  const [timeN, setTimeN] = useState('0');
  const [per, setPer] = useState('');
  const [max, setMax] = useState(100);
  const [saved, setSaved] = useState('');                    // "saved" / "cleared" flash after a defaults call
  const applied = useRef(false);                             // the saved defaults are applied once, on the first load
  const load = () => api.follow().then((x) => { setD(x); setErr(''); setPer((v) => (v === '' ? String(x.per_position) : v)); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  useEffect(() => {                                           // while a rebuild runs: progress every 2 s
    if (d?.status?.state !== 'running') return undefined;
    const id = setInterval(load, 2000);
    return () => clearInterval(id);
  }, [d?.status?.state]);
  const applyDefaults = (df) => {
    const S = { scr: setScr, status: setSt, result: setRes, maxOpen: setMaxOpen, capital: setCapital, stopPct: setStopPct, basis: setBasis, timeN: setTimeN, mkt: setMkt };
    DEFAULT_KEYS.forEach((k) => { if (df?.[k] != null) S[k](String(df[k])); });
    if (df?.capital != null && !CAP_F.some(([k]) => k === String(df.capital))) setCapBox(String(df.capital));
  };
  useEffect(() => {                                           // never overwrite the user's live changes on later reloads
    if (!d || applied.current) return;
    applied.current = true;
    if (Object.keys(d.defaults || {}).length) applyDefaults(d.defaults);
  }, [d]);
  const flash = (msg) => { setSaved(msg); setTimeout(() => setSaved(''), 2000); };
  const saveDefaults = () => api.followDefaults({ scr, status: st, result: res, maxOpen, capital, stopPct, basis, timeN, mkt })
    .then(() => { flash('saved'); load(); }).catch((e) => setErr(e.message));
  const clearDefaults = () => api.followDefaults({}).then(() => { flash('cleared · the page opens on the board defaults next time'); load(); }).catch((e) => setErr(e.message));
  const mktOk = useMemo(() => {                                         // NIFTY above its 50-day line on the entry day
    if (!d || mkt !== '1') return null;
    const m = {}; let lastOk = true;
    d.nifty.forEach(([dt, c, ma]) => { if (ma != null) lastOk = c >= ma; m[dt] = lastOk; });
    const dates = Object.keys(m).sort();
    return (dt) => { if (m[dt] != null) return m[dt]; let ok = true; for (const x of dates) { if (x > dt) break; ok = m[x]; } return ok; };
  }, [d, mkt]);
  const run = (sp, bs, tn) => capped(d.rows.filter((p) => screenOk(p, scr)).map((p) => applyRules(p, sp, bs, tn)), +maxOpen, +capital, mktOk);
  const base = useMemo(() => (d ? run(+stopPct, basis, +timeN) : []), [d, scr, maxOpen, capital, stopPct, basis, timeN, mktOk]);
  const grid = useMemo(() => (d ? STOP_F.map(([sp]) => TIME_F.map(([tn]) => { const t = run(+sp, basis, +tn).filter((p) => !p.skipped); const cl = t.filter((p) => !live(p) && p.qty);
    return { net: t.reduce((a, p) => a + (p.qty ? p.net : 0), 0), won: cl.length ? Math.round(100 * cl.filter((p) => p.net > 0).length / cl.length) : null, dd: drawdown(curve(t)).value }; })) : []),
  [d, scr, maxOpen, capital, basis, mktOk]);
  // "blind": every breakout taken, the stored exits only (8% under the pivot), no cap, no filter
  const blind = useMemo(() => (d ? capped(d.rows.map((p) => applyRules(p, 8, 'pivot', 0)), 0, 0, null).reduce((a, p) => a + (p.qty ? p.net : 0), 0) : 0), [d]);
  const corp = useMemo(() => (d ? d.rows.filter((p) => p.corp_action && screenOk(p, scr)) : []), [d, scr]);
  const taken = useMemo(() => base.filter((p) => !p.skipped), [base]);
  const rows = useMemo(() => base.filter((p) => !p.skipped || (st === 'all' && res === 'all'))
    .filter((p) => st === 'all' || (st === 'open' ? live(p) : !live(p)))
    .filter((p) => res === 'all' || (res === 'profit' ? p.net > 0 : p.net <= 0)), [base, st, res]);
  const cv = useMemo(() => curve(taken), [taken]);
  if (!d) return <div className="rd"><div className="rbanner">{err || 'Loading…'}</div></div>;
  const t = agg(taken), peak = peakUse(taken), dd = drawdown(cv), skipped = base.length - taken.length;
  const closedByExit = taken.filter((p) => !live(p) && p.qty).sort((a, b) => a.exit_date.localeCompare(b.exit_date));
  let streak = 0, worstStreak = 0;
  closedByExit.forEach((p) => { streak = p.net <= 0 ? streak + 1 : 0; worstStreak = Math.max(worstStreak, streak); });
  const worstTrade = closedByExit.reduce((w, p) => (!w || p.net < w.net ? p : w), null);
  const months = {};
  closedByExit.forEach((p) => { months[p.exit_date.slice(0, 7)] = (months[p.exit_date.slice(0, 7)] || 0) + p.net; });
  const worstMonth = Object.entries(months).sort((a, b) => a[1] - b[1])[0];
  const niftyPct = d.nifty.length > 1 && cv.length ? (d.nifty[d.nifty.length - 1][1] / (d.nifty.find(([x]) => x >= cv[0][0])?.[1] || d.nifty[0][1]) - 1) * 100 : null;
  const shownTaken = rows.filter((p) => !p.skipped);
  const sumShown = (f) => shownTaken.reduce((a, p) => a + (f(p) || 0), 0);
  const nOpen = shownTaken.filter(live).length, nClosed = shownTaken.length - nOpen, nWon = shownTaken.filter((p) => !live(p) && p.net > 0).length;
  const running = d.status.state === 'running';
  const act = (p) => p.then(load).catch((e) => setErr(e.message));
  const scope = `${SCREEN_F.find(([k]) => k === scr)[1]}${+maxOpen ? ` · max ${maxOpen} open` : ''}${+capital ? ` · capital ${inr(+capital)}` : ''} · stop ${stopPct}% ${basis === 'entry' ? 'under entry' : 'under pivot'}${+timeN ? ` · time stop ${timeN}` : ''}`;
  const nSlot = base.filter((p) => p.skipped === 'slot').length, nCash = base.filter((p) => p.skipped === 'cash').length, nMkt = base.filter((p) => p.skipped === 'market').length;
  const reasons = {};
  taken.forEach((p) => { const k = live(p) ? 'still open' : /below the (entry|pivot)/.test(p.reason || '') ? 'stop' : /not in profit/.test(p.reason || '') ? 'time stop' : /50-day/.test(p.reason || '') ? '50-day line' : p.reason || 'other';
    reasons[k] = reasons[k] || { n: 0, net: 0 }; reasons[k].n += 1; reasons[k].net += p.qty ? p.net : 0; });
  const bestNet = Math.max(...grid.flat().map((g) => g.net), -Infinity);
  const inUse = taken.filter((p) => live(p) && p.qty).reduce((a, p) => a + p.value, 0);
  // derived for the cards: CAGR on the peak money, payoff, expectancy, the drawdown series and where the curve sits now
  const days = cv.length > 1 ? (new Date(cv[cv.length - 1][0]) - new Date(cv[0][0])) / 86400000 : 0;
  const cagr = peak.value > 0 && days >= 30 && peak.value + t.net > 0 ? ((peak.value + t.net) / peak.value) ** (365 / days) - 1 : null;
  const payoff = t.avg_win != null && t.avg_loss ? Math.abs(t.avg_win / t.avg_loss) : null;
  const expect = t.win_rate != null && t.avg_win != null && t.avg_loss != null ? (t.win_rate / 100) * t.avg_win + (1 - t.win_rate / 100) * t.avg_loss : null;
  let runPeak = 0;
  const ddSeries = cv.map(([, v]) => { runPeak = Math.max(runPeak, v); return v - runPeak; });
  const ddNow = ddSeries.length ? ddSeries[ddSeries.length - 1] : 0;
  const nPending = taken.filter((p) => p.status === 'pending').length, nClosing = taken.filter((p) => p.status === 'closing').length;
  const defaultsOn = Object.keys(d.defaults || {}).length > 0;
  const statusTag = (p) => (p.skipped ? <Tag>skipped · {p.skipped === 'cash' ? 'no cash' : p.skipped === 'market' ? 'market filter' : 'no slot'}</Tag>
    : p.status === 'pending' ? <Tag c="a">pending · buys next open</Tag> : p.status === 'closing' ? <Tag c="w">sells next open</Tag>
      : live(p) ? <Tag c="ok">open · {p.days} d</Tag> : <Tag c={p.net > 0 ? 'ok' : 'bad'}>closed · {p.days} d</Tag>);
  const whyText = (p) => (p.skipped ? (p.skipped === 'cash' ? 'no cash left in the capital' : p.skipped === 'market' ? 'NIFTY under its 50-day line on the entry day' : 'no free slot')
    : `${p.reason || ''}${p.trigger ? ` · ${day(p.trigger.date)} close ${n2(p.trigger.close)}${p.trigger.gap != null ? `, next open ${p.trigger.gap > 0 ? '+' : ''}${p.trigger.gap}%` : ''}` : ''}`);
  const bCols = [{ h: 'Stock' }, { h: 'Breakout', w: 72 }, { h: 'Screens', w: 100 }, { h: 'Entry', w: 84, n: true, lo: true }, { h: 'Now / exit', w: 90, n: true },
    { h: 'Qty', w: 52, n: true, lo: true }, { h: 'Value', w: 92, n: true, lo: true }, { h: 'P&L', w: 96, n: true }, { h: '%', w: 66, n: true }, { h: 'Held', w: 56, n: true, lo: true }, { h: 'Status', w: 190 }];
  const total = `Total · ${shownTaken.length} taken${rows.length - shownTaken.length ? ` · ${rows.length - shownTaken.length} skipped` : ''} · value ${inr(sumShown((p) => p.value))} · P&L ${inr(sumShown((p) => p.net))} (${sumShown((p) => p.value) ? pc(sumShown((p) => p.net) / sumShown((p) => p.value) * 100) : '—'}) · held ${shownTaken.length ? Math.round(sumShown((p) => p.days) / shownTaken.length) : 0} d avg · ${nOpen} open · ${nClosed} closed · ${nWon} won`;
  const bRows = rows.slice(0, max).map((p) => ({ key: p.symbol + p.breakout_date, cls: p.skipped ? 'dim' : '', title: whyText(p), cells: [
    <><b>{p.symbol}</b> <small>{p.entry_date ? `bought ${day(p.entry_date)}` : 'not bought yet'} · pivot {n2(p.pivot)} · stop {n2(p.stop)}{p.exit_date ? ` · sold ${day(p.exit_date)}` : ''}</small></>,
    day(p.breakout_date), <Tags s={p.screens} />, n2(p.entry), n2(p.exit ?? p.last), p.qty || '—', inr(p.value),
    <b className={cls(p.net)}>{p.skipped ? '—' : inr(p.net)}</b>, <span className={cls(p.pct)}>{p.skipped ? '—' : pc(p.pct)}</span>, `${p.days} d`,
    <>{statusTag(p)} <small>{p.skipped ? '' : whyText(p)}</small></>] }));
  const cCols = [{ h: 'Stop ↓ · time stop →', w: undefined }, ...TIME_F.map(([k, l]) => ({ h: l, w: 118, n: true, key: k }))];
  const cRows = STOP_F.map(([sp, sl], i) => ({ key: sp, cells: [<b>{sl}</b>, ...TIME_F.map(([tn], j) => { const g = grid[i]?.[j]; if (!g) return '';
    const best = g.net === bestNet && bestNet !== 0, here = sp === stopPct && tn === timeN;
    return <span role="button" tabIndex={0} title={`stop ${sl} · time stop ${tn === '0' ? 'off' : tn} · net ${inr(g.net)} · won ${g.won ?? '—'}% · worst fall ${inr(-g.dd)} · click to set`}
      onClick={() => { setStopPct(sp); setTimeN(tn); }} className={cls(g.net)}
      style={{ cursor: 'pointer', display: 'inline-block', padding: '2px 6px', borderRadius: 4, background: best ? 'var(--ok-bg)' : here ? 'var(--hover)' : undefined, outline: here ? '1px solid var(--accent)' : undefined }}>
      <b>{short(g.net)}</b> <small>{g.won ?? '—'}%</small></span>; })] }));
  const sCols = [{ h: 'Screen' }, { h: 'Breakouts', w: 90, n: true }, { h: 'Open', w: 64, n: true, lo: true }, { h: 'Closed · won', w: 110, n: true }, { h: 'Avg win / loss', w: 130, n: true, lo: true }, { h: 'P&L', w: 110, n: true }];
  const sRows = Object.entries(d.by_screen).map(([k, a]) => ({ key: k, cells: [<>{TAG[k] ? <Tags s={[k]} /> : null} {d.names[k]}</>, a.breakouts, a.open, `${a.closed} · ${a.win_rate ?? '—'}%`,
    `${pc(a.avg_win)} / ${pc(a.avg_loss)}`, <span className={cls(a.net)}>{inr(a.net)}</span>] }));
  const col = { display: 'grid', gap: 12, minWidth: 0, alignContent: 'start' };
  return <div className="rd">
    <Head title="📈 Follow · every fresh breakout bought blind" right={<>
      <button type="button" className="btn ghost small" onClick={() => document.getElementById('follow-where')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>Where the result came from</button>
      <button type="button" className="btn small" disabled={running} onClick={() => window.confirm(`Rebuild the whole book from the saved daily candles (last ${d.months} months)? Takes a few minutes.`) && act(api.followRebuild())}>{running ? 'Rebuilding…' : '↻ Rebuild'}</button>
      <button type="button" className="btn ghost small" onClick={() => csv(rows)} disabled={!rows.length}>⬇ CSV</button></>}>
      <Chip title="One stock is one position at a time, tagged with every screen that flagged it · judged on each day's data only · updated every evening after the scan · nothing is ordered.">
        <b className="mono">{t.n}</b> breakouts · <b className="mono">{inr(d.per_position)}</b> a position · {d.as_of ? `as of ${day(d.as_of)}` : 'not built yet: press Rebuild'}{running ? ' · rebuilding' : ''}</Chip>
      <Chip>Buy: next day's open after the breakout day</Chip>
      <Chip>Sell: a close under the {d.rows[0]?.ma || 50}-day line, or {stopPct}% under the {basis}{+timeN ? `, or not in profit after ${timeN} sessions` : ''} → next open</Chip>
      <Chip>Delivery charges included</Chip>
    </Head>
    {(running || d.status.message) && <div className="rbanner">{d.status.message}{running && <div className="bar"><i style={{ width: `${d.status.pct}%` }} /></div>}</div>}
    {err && <div className="rbanner bad">{err}</div>}
    <Panel title="Try it as" meta={defaultsOn ? 'saved default' : null} right={<>
      {saved && <span className="up">{saved}</span>}
      <button type="button" className="btn ghost small" onClick={saveDefaults}>💾 Save as default</button>
      <button type="button" className="btn ghost small" disabled={!defaultsOn} onClick={() => applyDefaults(d.defaults)}>Reset to default</button>
      {defaultsOn && <button type="button" className="linkbtn small" style={{ padding: 0 }} onClick={clearDefaults}>Clear saved default</button>}</>}>
      <div className="rwrap">
        <span>Screens <Seg small opts={SCREEN_F} v={scr} set={setScr} /></span>
        <span>Status <Seg small opts={STATUS_F} v={st} set={setSt} /></span>
        <span>Result <Seg small opts={RESULT_F} v={res} set={setRes} /></span>
        <span>₹ a position <input className="input" style={{ width: 90, textAlign: 'right' }} inputMode="numeric" value={per} onChange={(e) => setPer(e.target.value.replace(/[^0-9]/g, ''))} />
          <button type="button" className="btn ghost small" onClick={() => act(api.followSettings(+per))} disabled={+per < 1000}>Save</button></span>
        <span>Capital <Seg small opts={CAP_F} v={CAP_F.some(([k]) => k === capital) ? capital : 'custom'} set={(k) => { setCapital(k); setCapBox(''); }} />
          <input className="input" style={{ width: 90, textAlign: 'right' }} inputMode="numeric" placeholder="custom ₹" value={capBox}
            onChange={(e) => { const v = e.target.value.replace(/[^0-9]/g, ''); setCapBox(v); setCapital(+v >= 10000 ? v : '0'); }} /></span>
        <span>Max open at once <Seg small opts={MAX_F} v={maxOpen} set={setMaxOpen} /></span>
        <span>Stop <Seg small opts={STOP_F} v={stopPct} set={setStopPct} /> <Seg small opts={BASIS_F} v={basis} set={setBasis} /></span>
        <span>New entries only while <Seg small opts={MKT_F} v={mkt} set={setMkt} /></span>
        <span>Time stop (sessions) <Seg small opts={TIME_F} v={timeN} set={setTimeN} /></span>
      </div>
      <div className="rnote">Every number below is for <b>{scope}</b>. "VCP + Flag" = flagged by either (a stock is still one position: a second screen adds its tag, never a second trade) · "VCP + Bases" = by both · "Bases only" = by no other screen.
        With a max or a capital, breakouts are taken in entry order; one with no free slot, or whose value does not fit in the capital beside the money already in open positions, is skipped (greyed, out of every number). A sale gives its money back; closed profit / loss does not change the capital. The stop (under your entry price, or under the pivot) and the time stop re-run every position's exits on its daily closes (sold at the next open); the 50-day-line exit and the stored 8%-under-pivot exit always stay.
        A new ₹ a position applies to new entries (Rebuild to apply it to the history) · nothing is ordered.
        {corp.length > 0 && <> <b>{corp.length} position{corp.length > 1 ? 's' : ''} left out</b> (a split / bonus moved the price 40%+ in a day; the daily candles are not adjusted): {corp.map((p) => p.symbol).join(', ')}.</>}</div>
    </Panel>
    <Cards n={6}>
      <Card label="Net · this setting" value={inr(t.net)} num={t.net} k={cls(t.net)} sub={`${t.n} taken · ${t.open} open · ${t.closed} closed`} spark={cv.map(([, v]) => v)}
        fact={<>vs blind <Bar pct={blind ? 100 * t.net / blind : 0} ok={t.net >= blind} /> <b>{inr(blind)}</b></>} />
      {cagr != null ? <Card unit="%" label="CAGR · vs NIFTY" value={pc(cagr * 100)} num={cagr} k={cls(cagr)} sub={`NIFTY 50 ${pc(niftyPct)} · same dates`} fact={<>on <b>{inr(peak.value)}</b> peak money · net {pc(peak.value ? t.net / peak.value * 100 : null)}</>} />
        : <Card unit="%" label="Net on the peak money" value={peak.value ? pc(t.net / peak.value * 100) : '—'} num={peak.value ? t.net / peak.value : 0} k={cls(t.net)} sub={`NIFTY 50 ${pc(niftyPct)} over the same dates`} fact={<>peak <b>{inr(peak.value)}</b> · {peak.positions} positions{peak.date ? ` on ${day(peak.date)}` : ''}</>} />}
      <Card unit="n" label="Win rate · payoff" value={`${t.win_rate ?? '—'}% · ${payoff != null ? payoff.toFixed(1) : '—'}`} num={t.win_rate} sub={`avg win ${pc(t.avg_win)} · avg loss ${pc(t.avg_loss)}`}
        fact={<>expectancy <b>{pc(expect)}</b> a trade</>} />
      <Card label="Max drawdown" value={inr(-dd.value)} num={-dd.value} k={dd.value ? 'down' : ''} spark={ddSeries.length > 1 ? ddSeries : undefined}
        sub={dd.from ? `${day(dd.from)} → ${day(dd.to)} · ${dd.sessions} sessions` : 'no fall yet'}
        fact={<>now <b className={ddNow < 0 ? 'down' : 'up'}>{inr(ddNow)}</b> from the high{dd.recovered ? ` · back at the peak ${day(dd.recovered)}` : dd.to ? ' · not back yet' : ''}</>} />
      <Card unit="n" label="Pending · open" value={`${nPending} · ${t.open}`} num={t.open} sub={`buy next open · open P&L ${inr(t.open_net)}`}
        fact={<><b>{nClosing}</b> selling · in use <b>{inr(inUse)}</b>{+capital ? ` of ${inr(+capital)}` : ''}</>} />
      <Card unit="n" label="Skipped" value={skipped} num={skipped} k={skipped ? 'warn' : ''} sub={`${nSlot} no slot · ${nCash} no cash · ${nMkt} market filter`} fact={<>left out <b>{corp.length}</b> corp action{corp.length === 1 ? '' : 's'}</>} />
    </Cards>
    <div className="two">
      <div style={col}>
        <Panel title="Growth" right={<span>{scope} · vs NIFTY 50</span>}><div className="rbody"><Chart cv={cv} nifty={d.nifty} peakValue={peak.value} /></div></Panel>
        <Panel title="Compare" meta="stop × time stop" right={<span>net · won % · best in green · click a cell to set it</span>}>
          <FixedTable cols={cCols} rows={cRows} />
          <div className="rnote">{SCREEN_F.find(([k]) => k === scr)[1]}{+maxOpen ? ` · max ${maxOpen}` : ''}{+capital ? ` · ${inr(+capital)}` : ''} · stop {basis === 'entry' ? 'under entry' : 'under pivot'} · hover a cell for its worst fall.</div>
        </Panel>
        <Panel title="By screen" right={<span>the whole book, no cap · a breakout counts under every screen that flagged it</span>}><FixedTable cols={sCols} rows={sRows} /></Panel>
        <Panel title="Breakouts" meta={rows.length} right={<>
          <Seg small opts={STATUS_F} v={st} set={setSt} /><Seg small opts={RESULT_F} v={res} set={setRes} /></>}>
          <FixedTable cols={bCols} rows={bRows.length ? [...bRows, total] : []} empty={`No breakouts match${d.as_of ? '' : ' (not built yet: press Rebuild, or wait for the evening scan)'}.`} />
          {rows.length > max && <div className="rnote"><button type="button" className="btn ghost small" onClick={() => setMax(rows.length)}>show all {rows.length}</button></div>}
          <div className="rnote">Newest first. The total line follows the filters: value = the sum of every shown taken position's buy value, % = profit / loss over it. Open rows are marked at the last close, after the charges a sale would take.</div>
        </Panel>
      </div>
      <div style={col}>
        <Panel title="Drawdown" right={<span>how deep and how long the profit curve fell</span>}>
          {ddSeries.length > 1 ? <Bars vals={ddSeries} h={90} titles={cv.map(([dt], i) => `${day(dt)}: ${inr(ddSeries[i])}`)} /> : null}
          <KV pairs={[['Worst fall from a peak', <span className="down">{inr(-dd.value)}{peak.value ? ` · ${pc(-dd.value / peak.value * 100)}` : ''}</span>],
            ['Peak → low', dd.from ? `${day(dd.from)} → ${day(dd.to)}` : 'no fall yet'],
            ['Sessions under water', `${dd.sessions}${dd.recovered ? ` · back ${day(dd.recovered)}` : dd.to ? ' · not back yet' : ''}`],
            ['Now from the high', <span className={ddNow < 0 ? 'down' : ''}>{inr(ddNow)}</span>],
            ['Losing trades in a row', worstStreak],
            ['Worst trade', worstTrade ? <span className="down" title={worstTrade.reason}>{inr(worstTrade.net)} · {worstTrade.symbol} {day(worstTrade.exit_date)}</span> : '—'],
            ['Worst month', worstMonth ? <span className={cls(worstMonth[1])}>{inr(worstMonth[1])} · {worstMonth[0]}</span> : '—']]} />
        </Panel>
        <Panel title="Where the result came from" id="follow-where" right={<span>by exit</span>}>
          {Object.keys(reasons).length ? <KV pairs={Object.entries(reasons).map(([k, r]) => [`${k} · ${r.n}`, <span className={cls(r.net)}>{inr(r.net)}</span>])} /> : <p className="muted small rempty">Nothing taken yet.</p>}
          <KV pairs={[['Entry', 'next open after breakout'], ['Exit', `${d.rows[0]?.ma || 50}-day · stop · time stop`],
            ['Skipped', `slots ${nSlot} · cash ${nCash} · filter ${nMkt}`], ['Left out', `${corp.length} · corp action`]]} />
          {corp.length > 0 && <div className="rnote">Left out: {corp.map((p) => p.symbol).join(', ')} — a split / bonus moved the price 40%+ in a day; the daily candles are not adjusted.</div>}
        </Panel>
      </div>
    </div>
  </div>;
}
