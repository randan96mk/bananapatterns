import React, { useEffect, useMemo, useState } from 'react';
import * as api from '../services/api';
import { useIsPhone } from '../useMedia';
import { inr, cls } from '../fmt';

// 📈 Follow: every fresh breakout on the Research screens bought blindly with ₹X. Everything on the page follows the
// filters (screens, status, result, max open at once): tiles, growth chart vs NIFTY 50, drawdown, the table.
const TAG = { bases: ['BASES', '#26a69a'], vcp: ['VCP', '#c58af9'], flag: ['FLAG', '#ff8a65'] };
const day = (d) => (d ? new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' }) : '—');
const n2 = (v) => (v == null ? '—' : (+v).toLocaleString('en-IN', { maximumFractionDigits: 2 }));
const pc = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${(+v).toFixed(1)}%`);
const Tags = ({ s }) => s.map((k) => <span key={k} className="chip small" style={{ color: TAG[k]?.[1], borderColor: TAG[k]?.[1], marginRight: 3 }}>{TAG[k]?.[0] || k}</span>);
const SCREEN_F = [['all', 'All'], ['vcp', 'VCP'], ['bases', 'Bases'], ['flag', 'Flag'], ['vcp+flag', 'VCP + Flag'], ['vcp+bases', 'VCP + Bases'], ['flag+bases', 'Flag + Bases'],
  ['bases-only', 'Bases only'], ['vcp-only', 'VCP only'], ['flag-only', 'Flag only']];
const STATUS_F = [['all', 'All'], ['open', 'Open'], ['closed', 'Closed']];
const RESULT_F = [['all', 'All'], ['profit', 'Profit'], ['loss', 'Loss']];
const MAX_F = [['0', 'All'], ['3', '3'], ['5', '5'], ['10', '10']];
const STOP_F = [['5', '5%'], ['6', '6%'], ['7', '7%'], ['8', '8%']];
const TIME_F = [['0', 'Off'], ['5', '5'], ['6', '6'], ['7', '7'], ['8', '8'], ['9', '9'], ['10', '10']];
const Seg = ({ opts, v, set }) => <span className="seg">{opts.map(([k, l]) => <button key={k} className={v === k ? 'active' : ''} onClick={() => set(k)}>{l}</button>)}</span>;
const live = (p) => p.status !== 'closed';
const has = (p, k) => p.screens.includes(k);
const screenOk = (p, f) => f === 'all' || (f === 'vcp+flag' ? has(p, 'vcp') || has(p, 'flag') : f === 'vcp+bases' ? has(p, 'vcp') && has(p, 'bases') : f === 'flag+bases' ? has(p, 'flag') && has(p, 'bases')
  : f.endsWith('-only') ? p.screens.length === 1 && has(p, f.slice(0, -5)) : has(p, f));

// Delivery charges, as the backend counts them (STT on the sell, exchange, SEBI, GST on those, stamp duty on the buy).
const charges = (value, sell) => { const exch = value * 0.00297 / 100, sebi = value * 10 / 1e7; return value * (sell ? 0.1 : 0) / 100 + exch + sebi + (exch + sebi) * 0.18 + (sell ? 15.93 : value * 0.015 / 100); };

// The position's exits re-run on its daily path under a stop of stopPct under the pivot and a time stop of timeN
// sessions (sold at the next open if not in profit by then). Both can only sell earlier than the stored exit, so the
// stored path (entry day .. exit day) holds every day needed. pl: [[date, P&L]] day by day for the growth chart.
function applyRules(p, stopPct, timeN) {
  const path = p.path || [];
  if (!path.length || !p.qty) return { ...p, pl: [] };
  const stop = Math.round(p.pivot * (1 - stopPct / 100) * 100) / 100, cin = p.charges_in || 0, q = p.qty, e = p.entry;
  const last = path.length - 1, closedOrig = p.status === 'closed';
  const mark = (c) => Math.round(((c - e) * q - cin - charges(c * q, true)) * 100) / 100;
  let exitIdx = closedOrig ? last : -1, reason = closedOrig ? p.reason : p.status === 'closing' ? p.reason : null, closing = p.status === 'closing';
  for (let i = 0; i < path.length; i += 1) {
    if (closedOrig && i >= last - 1) break;                       // the stored exit fires on the day before the exit day
    const c = path[i][2];
    const why = c <= stop ? `fell ${stopPct}% below the pivot` : timeN && i >= timeN && c <= e ? `not in profit after ${timeN} sessions` : null;
    if (why) { reason = why; if (i < last) exitIdx = i + 1; else { exitIdx = -1; closing = true; } break; }
  }
  if (exitIdx >= 0) {
    const exit = path[exitIdx][1], gross = (exit - e) * q, ch = cin + charges(exit * q, true);
    const pl = path.slice(0, exitIdx).map(([d, , c]) => [d, mark(c)]).concat([[path[exitIdx][0], Math.round((gross - ch) * 100) / 100]]);
    return { ...p, stop, status: 'closed', reason, exit_date: path[exitIdx][0], exit: Math.round(exit * 100) / 100, days: exitIdx,
      gross: Math.round(gross * 100) / 100, net: Math.round((gross - ch) * 100) / 100, pct: Math.round(gross / p.value * 10000) / 100, pl };
  }
  const c = path[last][2];
  return { ...p, stop, status: closing ? 'closing' : p.status, reason: closing ? reason : null, exit_date: null, exit: null, days: last,
    net: mark(c), pct: Math.round((c - e) / e * 10000) / 100, last: c, pl: path.map(([d, , x]) => [d, mark(x)]) };
}

// At most N open at once: the breakouts are taken in entry order, one that finds no free slot is skipped.
function capped(rows, n) {
  const ordered = rows.filter((p) => p.qty > 0 || live(p));
  if (!n) return ordered.map((p) => ({ ...p, skipped: false }));
  const open = [];
  return [...ordered].sort((a, b) => (a.entry_date || a.signal_date).localeCompare(b.entry_date || b.signal_date)).map((p) => {
    const at = p.entry_date || p.signal_date;
    for (let i = open.length - 1; i >= 0; i -= 1) if (open[i] <= at) open.splice(i, 1);   // exits before this entry free a slot
    if (open.length >= n) return { ...p, skipped: true };
    open.push(p.exit_date || '9999');
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
    p.exit_date || '', p.exit ?? p.last ?? '', p.net, p.pct ?? '', p.days, p.skipped ? 'skipped' : p.status, p.skipped ? 'no free slot' : p.reason || ''].join(','));
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

export default function FollowPage() {
  const phone = useIsPhone();
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [scr, setScr] = useState('all');
  const [st, setSt] = useState('all');
  const [res, setRes] = useState('all');
  const [maxOpen, setMaxOpen] = useState('0');
  const [stopPct, setStopPct] = useState('8');
  const [timeN, setTimeN] = useState('0');
  const [per, setPer] = useState('');
  const [max, setMax] = useState(100);
  const load = () => api.follow().then((x) => { setD(x); setErr(''); setPer((v) => (v === '' ? String(x.per_position) : v)); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  useEffect(() => {                                           // while a rebuild runs: progress every 2 s
    if (d?.status?.state !== 'running') return undefined;
    const id = setInterval(load, 2000);
    return () => clearInterval(id);
  }, [d?.status?.state]);
  const base = useMemo(() => (d ? capped(d.rows.filter((p) => screenOk(p, scr)).map((p) => applyRules(p, +stopPct, +timeN)), +maxOpen) : []),
    [d, scr, maxOpen, stopPct, timeN]);
  const taken = useMemo(() => base.filter((p) => !p.skipped), [base]);
  const rows = useMemo(() => base.filter((p) => !p.skipped || (st === 'all' && res === 'all'))
    .filter((p) => st === 'all' || (st === 'open' ? live(p) : !live(p)))
    .filter((p) => res === 'all' || (res === 'profit' ? p.net > 0 : p.net <= 0)), [base, st, res]);
  const cv = useMemo(() => curve(taken), [taken]);
  if (!d) return <div className="panel muted">{err || 'Loading…'}</div>;
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
  const scope = `${SCREEN_F.find(([k]) => k === scr)[1]}${+maxOpen ? ` · max ${maxOpen} open` : ''} · stop ${stopPct}%${+timeN ? ` · time stop ${timeN}` : ''}`;
  const Tile = ({ v, l, k }) => <div className="k"><div className={`v ${k || ''}`}>{v}</div><div className="l">{l}</div></div>;
  return <>
    <div className="panel">
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <b>📈 Every fresh breakout bought with {inr(d.per_position)}{+maxOpen ? `, at most ${maxOpen} open at once` : ', nothing skipped'}</b>
        <span className="chip small">Buy: next day's open after the breakout day</span>
        <span className="chip small">Sell: a close under the {d.rows[0]?.ma || 50}-day line, or {stopPct}% under the pivot{+timeN ? `, or not in profit after ${timeN} sessions` : ''} → next open</span>
        <span className="chip small">Delivery charges included</span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }} className="small">₹ a position
          <input className="input" style={{ width: 100, textAlign: 'right' }} inputMode="numeric" value={per} onChange={(e) => setPer(e.target.value.replace(/[^0-9]/g, ''))} />
          <button className="btn" onClick={() => act(api.followSettings(+per))} disabled={+per < 1000}>Save</button>
          <button className="btn primary" disabled={running} onClick={() => window.confirm(`Rebuild the whole book from the saved daily candles (last ${d.months} months)? Takes a few minutes.`) && act(api.followRebuild())}>{running ? 'Rebuilding…' : '↻ Rebuild'}</button></span>
      </div>
      <div className="muted small">One stock is one position at a time, tagged with every screen that flagged it · judged on each day's data only · updated every evening after the scan ·
        {d.as_of ? ` as of ${day(d.as_of)}` : ' not built yet: press Rebuild'} · a new ₹ a position applies to new entries (Rebuild to apply it to the history) · nothing is ordered.</div>
      {(running || d.status.message) && <div className="small" style={{ marginTop: 6 }}>{d.status.message}{running && <div className="bar"><i style={{ width: `${d.status.pct}%` }} /></div>}</div>}
      {err && <div className="warn small">{err}</div>}
    </div>
    <div className="panel">
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}><b>Try it as</b>
        <span className="muted small">Screens</span><Seg opts={SCREEN_F} v={scr} set={setScr} />
        <span className="muted small">Max open at once</span><Seg opts={MAX_F} v={maxOpen} set={setMaxOpen} />
        <span className="muted small">Stop under the pivot</span><Seg opts={STOP_F} v={stopPct} set={setStopPct} />
        <span className="muted small">Time stop (sessions)</span><Seg opts={TIME_F} v={timeN} set={setTimeN} /></div>
      <div className="muted small" style={{ marginTop: 4 }}>Every number below is for <b>{scope}</b>. "VCP + Flag" = flagged by either (a stock is still one position: a second screen adds its tag, never a second trade) · "VCP + Bases" = by both · "Bases only" = by no other screen.
        With a max, breakouts are taken in entry order; one with no free slot is skipped (greyed, out of every number). The stop and the time stop re-run every position's exits on its daily closes (sold at the next open); the 50-day-line exit always stays.</div>
      <div className="summary" style={{ marginTop: 10 }}>
        <Tile v={inr(t.net)} k={cls(t.net)} l={`net profit / loss · ${t.n} breakouts${skipped ? ` · ${skipped} skipped` : ''}`} />
        <Tile v={inr(t.open_net)} k={cls(t.open_net)} l={`open P&L · ${t.open} open now`} />
        <Tile v={`${t.closed} · ${t.win_rate ?? '—'}%`} l="closed · won" />
        <Tile v={`${pc(t.avg_win)} / ${pc(t.avg_loss)}`} l="avg win / avg loss" />
        <Tile v={inr(peak.value)} l={`peak money in use · ${peak.positions} positions${peak.date ? ` on ${day(peak.date)}` : ''}`} />
        <Tile v={peak.value ? pc(t.net / peak.value * 100) : '—'} k={cls(t.net)} l={`net on the peak money · NIFTY 50 ${pc(niftyPct)} over the same dates`} />
      </div>
    </div>
    <div className="panel"><b>Growth</b> <span className="muted small">· {scope} · vs NIFTY 50</span><Chart cv={cv} nifty={d.nifty} peakValue={peak.value} /></div>
    <div className="panel"><b>Drawdown</b> <span className="muted small">· {scope} · how deep and how long the profit curve fell</span>
      <div className="summary" style={{ marginTop: 8 }}>
        <Tile v={inr(-dd.value)} k="down" l={`worst fall from a peak${peak.value ? ` · ${pc(-dd.value / peak.value * 100)} of the peak money` : ''}`} />
        <Tile v={`${dd.sessions} sessions`} l={`${dd.from ? `peak ${day(dd.from)} → low ${day(dd.to)}` : 'no fall yet'}${dd.recovered ? ` · back at the peak ${day(dd.recovered)}` : dd.to ? ' · not back at that peak yet' : ''}`} />
        <Tile v={worstStreak} l="losing trades in a row, at worst" />
        <Tile v={worstTrade ? inr(worstTrade.net) : '—'} k={cls(worstTrade?.net)} l={`worst trade${worstTrade ? ` · ${worstTrade.symbol} ${day(worstTrade.exit_date)} · ${worstTrade.reason}` : ''}`} />
        <Tile v={worstMonth ? inr(worstMonth[1]) : '—'} k={cls(worstMonth?.[1])} l={`worst month${worstMonth ? ` · ${worstMonth[0]}` : ''} (closed trades)`} />
      </div></div>
    <div className="panel"><b>By screen</b> <span className="muted small">· the whole book, no cap · a breakout counts under every screen that flagged it</span>
      <div className="tablewrap"><table className="grid"><thead><tr><th>Screen</th><th className="num">Breakouts</th><th className="num">Open</th><th className="num">Closed · won</th><th className="num">Avg win / loss</th><th className="num">Profit / loss</th></tr></thead>
        <tbody>{Object.entries(d.by_screen).map(([k, a]) => <tr key={k}><td>{TAG[k] ? <Tags s={[k]} /> : null}{d.names[k]}</td><td className="num">{a.breakouts}</td><td className="num">{a.open}</td>
          <td className="num">{a.closed} · {a.win_rate ?? '—'}%</td><td className="num">{pc(a.avg_win)} / {pc(a.avg_loss)}</td><td className={`num ${cls(a.net)}`}>{inr(a.net)}</td></tr>)}</tbody></table></div></div>
    <div className="panel">
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 8 }}><b>Breakouts</b> <span className="muted small">· {scope} · newest first</span>
        <span className="muted small">Status</span><Seg opts={STATUS_F} v={st} set={setSt} /><span className="muted small">Result</span><Seg opts={RESULT_F} v={res} set={setRes} />
        <button className="btn" style={{ marginLeft: 'auto' }} onClick={() => csv(rows)} disabled={!rows.length}>⬇ CSV</button></div>
      {!rows.length ? <p className="muted">No breakouts match{d.as_of ? '' : ' (not built yet: press Rebuild, or wait for the evening scan)'}.</p>
        : phone ? <div className="pcards">{rows.slice(0, max).map((p) => <details key={p.symbol + p.breakout_date} className="pcard" style={p.skipped ? { opacity: 0.5 } : {}}><summary className="t"><span><b>{p.symbol}</b> <Tags s={p.screens} /> <span className="muted small">{day(p.breakout_date)}</span></span><b className={cls(p.net)}>{p.skipped ? 'skipped' : inr(p.net)}</b></summary>
          <div className="small">bought {day(p.entry_date)} @ {n2(p.entry)} × {p.qty} = {inr(p.value)} · pivot {n2(p.pivot)} · stop {n2(p.stop)}<br />{live(p) ? `now ${n2(p.last)} · open ${p.days} d` : `sold ${day(p.exit_date)} @ ${n2(p.exit)} · ${p.days} d`} · {pc(p.pct)}{p.reason ? ` · ${p.reason}` : ''}</div></details>)}</div>
          : <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Screens</th><th>Breakout</th><th>Bought</th><th className="num">Entry ₹</th><th className="num">Qty</th><th className="num">Value ₹</th>
            <th className="num">Pivot</th><th className="num">Stop</th><th>Sold</th><th className="num">Exit / Now ₹</th><th className="num">Profit / loss</th><th className="num">%</th><th className="num">Held</th><th>Status · why</th></tr></thead>
            <tbody>{rows.slice(0, max).map((p) => <tr key={p.symbol + p.breakout_date} style={p.skipped ? { opacity: 0.45 } : {}}><td><b>{p.symbol}</b></td><td><Tags s={p.screens} /></td><td>{day(p.breakout_date)}</td><td>{day(p.entry_date)}</td>
              <td className="num">{n2(p.entry)}</td><td className="num">{p.qty || '—'}</td><td className="num">{inr(p.value)}</td><td className="num">{n2(p.pivot)}</td><td className="num">{n2(p.stop)}</td>
              <td>{day(p.exit_date)}</td><td className="num">{n2(p.exit ?? p.last)}</td><td className={`num ${cls(p.net)}`}><b>{inr(p.net)}</b></td><td className={`num ${cls(p.pct)}`}>{pc(p.pct)}</td>
              <td className="num">{p.days} d</td><td>{p.skipped ? 'skipped · no free slot' : live(p) ? <span className="up">● {p.status === 'pending' ? 'buys at the next open' : p.status === 'closing' ? `sells at the next open · ${p.reason}` : 'open'}</span> : `closed · ${p.reason || ''}`}</td></tr>)}</tbody>
            <tfoot><tr style={{ fontWeight: 700 }}><td colSpan={6}>Total · {shownTaken.length} taken{rows.length - shownTaken.length ? ` · ${rows.length - shownTaken.length} skipped` : ''}</td><td className="num">{inr(sumShown((p) => p.value))}</td><td colSpan={4} /><td className={`num ${cls(sumShown((p) => p.net))}`}>{inr(sumShown((p) => p.net))}</td>
              <td className={`num ${cls(sumShown((p) => p.net))}`}>{sumShown((p) => p.value) ? pc(sumShown((p) => p.net) / sumShown((p) => p.value) * 100) : '—'}</td><td className="num">{shownTaken.length ? Math.round(sumShown((p) => p.days) / shownTaken.length) : 0} d avg</td><td>{nOpen} open · {nClosed} closed · {nWon} won</td></tr></tfoot></table></div>}
      {rows.length > max && <button className="linkbtn small" onClick={() => setMax(rows.length)}>show all {rows.length}</button>}
      <div className="muted small" style={{ marginTop: 6 }}>The total line follows the filters: value = the sum of every shown taken position's buy value, % = profit / loss over it. Open rows are marked at the last close, after the charges a sale would take.</div>
    </div>
  </>;
}
