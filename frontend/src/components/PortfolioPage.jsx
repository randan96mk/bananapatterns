import React, { useEffect, useMemo, useState } from 'react';
import * as api from '../services/api';
import { useLive, useLiveStamp } from '../services/live';
import { StopMeter } from './ui';
import { inr, pct, cls, signed } from '../fmt';
import { useStale } from '../heartbeat';
import { useIsPhone } from '../useMedia';
import { Head, Chip, Seg, Cards, Card, Bar, Panel, FixedTable, KV, LineChart, Bars, Heat } from './ui2';

// 💼 Portfolio: every account in one place (the app's own: Real, swing paper, intraday paper, Follow), filtered
// by book: open positions, daily P&L, the month heatmap, trade history (CSV), orders, the curves, risk.
const VIEWS = [['open', 'Open'], ['daily', 'Daily P&L'], ['month', 'By month'], ['trades', 'Trade history'], ['orders', 'Orders'], ['curves', 'Curves']];
const COLORS = { real: 'var(--ok)', paper_E: '#b39dff', intraday: '#f2b84b' };
const BOOK = { real: 'Real', paper_E: 'Paper E+', intraday: 'Intraday' };
const dayName = (s) => (s ? new Date(s.length > 10 ? s : `${s}T00:00:00`).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short' }) : '');
const hm24 = (t) => (t ? new Date(`2000-01-01T${t}:00`).toLocaleTimeString('en-IN', { hour: 'numeric', minute: '2-digit', hour12: true }) : '');
const daysHeld = (p) => (p.book === 'intraday' || !p.opened ? '—' : Math.max(0, Math.round((Date.now() - new Date(`${p.opened.slice(0, 10)}T00:00:00+05:30`)) / 86400000)));
const COL = { gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' };   // a column that never widens with its content
const Meta = ({ children }) => <span style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{children}</span>;
const stamp16 = (s) => (s ? `${dayName(s)}, ${new Date(s).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit', hour12: true })}` : '');

function csv(rows) {
  const cols = ['account', 'strategy', 'symbol', 'side', 'qty', 'opened', 'closed', 'entry', 'exit', 'gross', 'costs', 'net', 'pct', 'reason'];
  const esc = (v) => (v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v);
  const body = [cols.join(','), ...rows.map((r) => cols.map((c) => esc(r[c])).join(','))].join('\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([body], { type: 'text/csv' }));
  a.download = 'trades.csv';
  a.click();
}

const POS_COLS = [{ h: 'Stock' }, { h: 'Strategy', w: 130, lo: true }, { h: 'Qty', w: 60, n: true, lo: true }, { h: 'Entry', w: 86, n: true, lo: true },
  { h: 'Now', w: 90, n: true }, { h: 'P&L', w: 90, n: true }, { h: '%', w: 64, n: true, lo: true }, { h: 'Stop · distance', w: 210 }, { h: 'Days', w: 56, n: true, lo: true }];

export default function PortfolioPage({ onStock }) {
  const o = useLive('overview');                           // positions and the account cards update live; history loads once
  const stampAt = useLiveStamp('overview');
  const [d, setD] = useState(null);
  const [book, setBook] = useState('all');
  const [view, setView] = useState('open');
  const [err, setErr] = useState('');
  useEffect(() => { api.getPortfolio().then(setD).catch((e) => setErr(e.message)); }, []);
  const stale = useStale(stampAt, 180000, o?.feed === 'live');
  const phone = useIsPhone();                              // a phone card is narrow: plain-text sub-lines and facts, no bars
  const names = d?.names || {};
  const books = useMemo(() => ['all', ...Object.keys(names).filter((b) => (d?.curves?.[b] || []).length || (o?.positions || []).some((p) => p.book === b))], [d, o, names]);
  const pick = (xs) => (book === 'all' ? xs : xs.filter((x) => x.book === book));
  const pos = pick(o?.positions || d?.positions || []);
  const trades = pick(d?.trades || []);
  const orders = pick(d?.orders || []);
  const month = new Date().toISOString().slice(0, 7);
  const mt = trades.filter((t) => (t.closed || '').slice(0, 7) === month);
  const won = mt.filter((t) => t.net > 0).length;
  const openPnl = pos.reduce((s, p) => s + (p.pnl || 0), 0);
  const realisedM = mt.reduce((s, t) => s + (t.net || 0), 0), costsM = mt.reduce((s, t) => s + (t.costs || 0), 0);
  const daily = {};
  Object.entries(d?.daily || {}).forEach(([b, m]) => { if (book === 'all' || b === book) Object.entries(m).forEach(([day, v]) => { daily[day] = (daily[day] || 0) + v; }); });
  const days = Object.keys(daily).sort().slice(-30);
  const dvals = days.map((x) => daily[x]);
  const greens = dvals.filter((v) => v > 0).length, reds = dvals.filter((v) => v < 0).length;
  if (err) return <div className="rd"><div className="rbanner bad">{err}</div></div>;
  if (!d) return <div className="rd"><Panel><p className="muted small rempty">Loading…</p></Panel></div>;
  const A = o?.accounts || {}, it = A.intraday || {}, R = o?.risk || {}, F = o?.facts || {};
  const liveNow = o?.feed === 'live' || pos.some((p) => p.live);
  const P = (rich, plain) => (phone ? plain : rich);
  const factPaper = (f) => f && (phone ? `vs NIFTY ${f.vs_nifty != null ? `${f.vs_nifty > 0 ? '+' : ''}${f.vs_nifty} pts` : '—'}${f.filter ? ` · filter ${f.filter}` : ''}` : <>vs NIFTY <b className={cls(f.vs_nifty)}>{f.vs_nifty != null ? `${f.vs_nifty > 0 ? '+' : ''}${f.vs_nifty} pts` : '—'}</b>{f.filter ? <> · filter <b className={/ON|FULL/.test(f.filter) ? 'up' : 'muted'}>{f.filter}</b></> : null}</>);
  const cards = [
    o && <Card key="all" label="All books · today" value={signed(o.today)} num={o.today} k={cls(o.today)} sub={`intraday ${signed(it.day_pnl)} · swing last session ${signed((o.today || 0) - (it.day_pnl || 0))}`} spark={(o.today_curve || []).map((p) => p[1])} stale={stale} live={liveNow}
      fact={F.today ? P(<>high <b className="up">{signed(F.today.high)}</b> {hm24(F.today.high_at)} · low <b className="down">{signed(F.today.low)}</b> {hm24(F.today.low_at)}</>, `high ${signed(F.today.high)} · low ${signed(F.today.low)}`) : 'high / low from the open'} />,
    A.real && <Card key="real" label={<>Real <span className={A.real.on ? 'up' : 'muted'}>· {A.real.on ? 'ON' : 'OFF'}{A.real.variant ? ` · ${A.real.variant === 'E' ? 'E+' : A.real.variant}` : ''}</span></>} value={inr(A.real.value)} num={A.real.value}
      sub={P(<><span className={cls(A.real.day_pnl)}>{signed(A.real.day_pnl)}</span> last session · {A.real.positions} positions · {pct(A.real.return)} since start</>, `${signed(A.real.day_pnl)} last session · ${A.real.positions} open · ${pct(A.real.return)}`)} spark={A.real.spark} stale={stale}
      fact={F.real && P(<><Bar pct={F.real.capital ? 100 * F.real.in_use / F.real.capital : 0} /><b>{inr(F.real.in_use)}</b> in use · {F.real.slots} of {F.real.max_pos} slots</>, `${inr(F.real.in_use)} in use · ${F.real.slots} of ${F.real.max_pos}`)} />,
    A.paper_E && <Card key="pE" label="Paper E+ · swing" value={pct(A.paper_E.return)} num={A.paper_E.value} k={cls(A.paper_E.return)} sub={P(<>{inr(A.paper_E.value)} · <span className={cls(A.paper_E.day_pnl)}>{signed(A.paper_E.day_pnl)}</span> last session · {A.paper_E.positions} open</>, `${inr(A.paper_E.value)} · ${signed(A.paper_E.day_pnl)} · ${A.paper_E.positions} open`)} spark={A.paper_E.spark} fact={factPaper(F.paper_E)} />,
    o && <Card key="it" label={`Intraday · ${it.mode === 'paper' ? 'paper' : it.mode || 'off'}`} value={signed(it.day_pnl)} num={it.day_pnl} k={cls(it.day_pnl)} sub={`${it.open || 0} open · ${it.trades || 0} trades · risk ${inr(it.at_risk)}`} spark={it.spark} stale={stale} live={o.feed === 'live'}
      fact={F.intraday && P(<><Bar pct={F.intraday.loss_limit ? 100 * F.intraday.loss_used / F.intraday.loss_limit : 0} bad />loss limit <b>{inr(F.intraday.loss_used)}</b> / {inr(F.intraday.loss_limit)}{F.intraday.per_day ? <> · signals <b>{F.intraday.signals}</b> of {F.intraday.per_day}</> : null}</>, `loss ${inr(F.intraday.loss_used)} / ${inr(F.intraday.loss_limit)}${F.intraday.per_day ? ` · signals ${F.intraday.signals} of ${F.intraday.per_day}` : ''}`)} />,
    A.follow && A.follow.breakouts > 0 && <Card key="fo" label="Follow · blind" value={signed(A.follow.net)} num={A.follow.net} k={cls(A.follow.net)} sub={`${A.follow.breakouts} breakouts · won ${A.follow.win_rate ?? '—'}% · ${inr(A.follow.per_position)} each`}
      fact={F.follow && P(<><b>{F.follow.open}</b> open · <b className={cls(F.follow.open_pnl)}>{signed(F.follow.open_pnl)}</b> · buys next open <b>{F.follow.pending ?? 0}</b></>, `${F.follow.open} open · ${signed(F.follow.open_pnl)} · ${F.follow.pending ?? 0} pending`)} />,
  ].filter(Boolean);

  // open positions, grouped by book
  const groups = ['real', 'paper_E', 'intraday'].map((b) => [b, pos.filter((p) => p.book === b)]).filter(([, ps]) => ps.length);
  const posRows = groups.flatMap(([b, ps]) => [
    `${BOOK[b] || names[b] || b} · ${ps.length} · ${signed(ps.reduce((a, p) => a + (p.pnl || 0), 0))}${b === 'intraday' ? ' · out by 3:15 PM' : ` · ${inr(ps.reduce((a, p) => a + (p.entry || 0) * (p.qty || 0), 0))} in use`}`,
    ...ps.map((p) => ({ key: p.book + p.symbol, onClick: () => onStock(p.symbol), title: `${p.side} · opened ${p.opened} · value ${inr((p.now || p.entry) * p.qty)}`,
      cells: [<><b>{p.symbol}</b>{p.side === 'short' && <span className="rtag bad">short</span>}{p.stop_dist != null && p.stop_dist < 2 && <span className="rtag w">near stop</span>}</>, <span className="muted">{p.strategy}</span>, p.qty, p.entry,
        <>{p.now ?? '—'}{p.live && <span className="livedot" />}</>, <span className={cls(p.pnl)}>{signed(p.pnl)}</span>, <span className={cls(p.pct)}>{pct(p.pct)}</span>,
        p.now && p.stop ? <><StopMeter last={p.side === 'short' ? -p.now : p.now} stop={p.side === 'short' ? -p.stop : p.stop} /> <span className="mono">{p.stop}</span></> : <span className="muted">{p.stop ?? '—'}</span>, daysHeld(p)] }))]);

  // by month: % of the account value where there is one, else ₹ thousands
  const year = new Date().getFullYear();
  const heatBooks = Object.keys(d.daily || {}).filter((b) => book === 'all' || b === book);
  const hasValue = heatBooks.every((b) => A[b]?.value || A[b]?.capital);
  const heatRows = heatBooks.map((b) => { const base = A[b]?.value || A[b]?.capital; const vals = Array(12).fill(null);
    Object.entries(d.daily[b]).forEach(([day, v]) => { if (+day.slice(0, 4) === year) { const m = +day.slice(5, 7) - 1; vals[m] = (vals[m] || 0) + v; } });
    return [BOOK[b] || names[b] || b, vals.map((v) => (v == null ? null : hasValue ? +(100 * v / base).toFixed(1) : +(v / 1000).toFixed(1)))]; });
  const heat = heatRows.length ? <Heat rows={heatRows} scale={hasValue ? 6 : 10} /> : <p className="muted small rempty">No sessions yet.</p>;
  const heatMeta = hasValue ? `% of the account value · ${year}` : `₹ thousands · ${year}`;
  const daysPanel = (h) => <>{!days.length ? <p className="muted small rempty">No sessions yet.</p> : <Bars vals={dvals} h={h} titles={days.map((x) => `${dayName(x)}: ${signed(daily[x])}`)} />}
    {days.length > 0 && <div className="rnote"><span className="up">● {greens} green</span> · <span className="down">● {reds} red</span> · best <b className="up mono">{signed(Math.max(...dvals))}</b> · worst <b className="down mono">{signed(Math.min(...dvals))}</b> · {dayName(days[0])} → {dayName(days[days.length - 1])} · after costs</div>}</>;

  // trade history and its per-book month table
  const tbooks = [...new Set(trades.map((t) => t.book))];
  const mm = {};
  trades.forEach((t) => { const k = (t.closed || '').slice(0, 7); mm[k] = mm[k] || {}; mm[k][t.book] = (mm[k][t.book] || 0) + (t.net || 0); });
  const months = Object.keys(mm).sort().reverse();
  const monthCols = [{ h: 'Month' }, ...tbooks.map((b) => ({ h: names[b] || b, w: 110, n: true, key: b, lo: tbooks.length > 2 })), { h: 'All', w: 110, n: true }];
  const monthRows = months.map((k) => { const tot = tbooks.reduce((a, b) => a + (mm[k][b] || 0), 0);
    return { key: k, cells: [k, ...tbooks.map((b) => <span className={cls(mm[k][b])}>{mm[k][b] != null ? inr(mm[k][b]) : '—'}</span>), <b className={cls(tot)}>{inr(tot)}</b>] }; });
  const tradeCols = [{ h: 'Closed' }, { h: 'Account', w: 90, lo: true }, { h: 'Stock', w: 120 }, { h: 'Qty', w: 56, n: true, lo: true }, { h: 'Entry', w: 86, n: true, lo: true }, { h: 'Exit', w: 86, n: true, lo: true },
    { h: 'Gross', w: 90, n: true, lo: true }, { h: 'Costs', w: 80, n: true, lo: true }, { h: 'Net', w: 96, n: true }, { h: '%', w: 64, n: true }, { h: 'Reason', w: 120, lo: true }];
  const tradeRows = trades.slice(0, 400).map((t, i) => ({ key: i, cells: [dayName(t.closed), t.account, <b>{t.symbol}</b>, t.qty, t.entry, t.exit, <span className={cls(t.gross)}>{inr(t.gross)}</span>,
    <span className="down">{inr(-(t.costs || 0))}</span>, <b className={cls(t.net)}>{signed(t.net)}</b>, <span className={cls(t.pct)}>{pct(t.pct)}</span>, <span className="muted">{t.reason}</span>] }));
  const orderCols = [{ h: 'When' }, { h: 'Account', w: 90, lo: true }, { h: 'Order', w: 60 }, { h: 'Stock', w: 120 }, { h: 'Qty', w: 56, n: true, lo: true }, { h: 'Price', w: 86, n: true, lo: true }, { h: 'Status', w: 130 }, { h: 'Note', w: 180, lo: true }];
  const orderRows = orders.slice(0, 300).map((x, i) => ({ key: i, cells: [stamp16(x.at), x.account, <b className={/SELL|EXIT/.test(x.action) ? 'down' : 'up'}>{x.action}</b>, <b>{x.symbol}</b>, x.qty ?? '', x.price ?? '—', x.status, <span className="muted">{x.note}</span>] }));
  const curves = Object.entries(d.curves || {}).filter(([b]) => book === 'all' || b === book);
  const sectors = R.sectors ? Object.entries(R.sectors).slice(0, 3).map(([k, v]) => `${k} ${v}%`).join(' · ') : null;

  return <div className="rd">
    <Head title="Portfolio" right={<>
      <Seg small opts={books.map((b) => [b, b === 'all' ? 'All' : names[b]])} v={book} set={setBook} />
      <Seg opts={VIEWS} v={view} set={setView} />
      <a className="btn small" href="/api/export/trades.csv" download title="every trade, every book">⬇ CSV</a></>}>
      <Chip dot={liveNow ? 'ok' : ''}>{liveNow ? 'live · prices tick in market hours' : 'last close · live in market hours'}</Chip>
      <Chip title="the app's own accounts">no broker holdings</Chip>
    </Head>
    {cards.length > 0 && <Cards n={cards.length}>{cards}</Cards>}
    <div className="two">
      <div className="rd" style={COL}>
        {view === 'open' && <Panel title="Open positions" meta={pos.length} right={<Meta>open P&L <b className={`mono ${cls(openPnl)}`}>{signed(openPnl)}</b> · after exit costs{R.worst_case != null && <> · worst case <b className="mono down">{signed(R.worst_case)}</b></>}</Meta>}>
          <FixedTable cols={POS_COLS} rows={posRows} empty="No open positions." /></Panel>}
        {view === 'daily' && <Panel title="Daily P&L" meta={`${days.length} sessions`} right="after costs">{daysPanel(200)}</Panel>}
        {view === 'month' && <Panel title="By month" meta={heatMeta}>{heat}</Panel>}
        {view === 'trades' && <>
          {months.length > 0 && <Panel title="By month" meta="net after costs · every book" right={<a className="btn ghost small" href="/api/export/trades.csv" download>⬇ CSV · every trade, every book</a>}>
            <FixedTable cols={monthCols} rows={monthRows} /></Panel>}
          <Panel title="Trade history" meta={`${trades.length} closed`} right={<><Meta>this month <b className={`mono ${cls(realisedM)}`}>{signed(realisedM)}</b> · {mt.length} trades · {mt.length ? Math.round(100 * won / mt.length) : 0}% won · costs <b className="mono">{inr(costsM)}</b>
            · 1 year net <b className={`mono ${cls(trades.reduce((s, t) => s + (t.net || 0), 0))}`}>{signed(trades.reduce((s, t) => s + (t.net || 0), 0))}</b></Meta> <button type="button" className="btn ghost small" onClick={() => csv(trades)}>⬇ CSV</button></>}>
            <FixedTable cols={tradeCols} rows={tradeRows} empty="No closed trades in the last year." /></Panel></>}
        {view === 'orders' && <Panel title="Orders" meta={orders.length} right="newest first"><FixedTable cols={orderCols} rows={orderRows} empty="No orders yet." /></Panel>}
        {view === 'curves' && (!curves.length ? <Panel title="Curves"><p className="muted small rempty">No curve yet.</p></Panel>
          : <div className={`cols ${curves.length > 1 ? 'c2' : ''}`}>{curves.map(([b, pts]) => <Panel key={b} title={names[b] || b} meta={`${pts.length} sessions`} right={<b className="mono">{inr(pts[pts.length - 1]?.[1])}</b>}>
            <LineChart h={140} series={[{ name: names[b] || b, pts: pts.map((x) => x[1]), color: COLORS[b] || 'var(--blue)', fill: true }]} labels={pts.length ? [dayName(pts[0][0]), dayName(pts[pts.length - 1][0])] : []} /></Panel>)}</div>)}
      </div>
      <div className="rd" style={COL}>
        {view !== 'daily' && <Panel title="Daily P&L" meta={`${days.length} sessions`}>{daysPanel(110)}</Panel>}
        {view !== 'month' && <Panel title="By month" meta={heatMeta}>{heat}</Panel>}
        {o && !R.error && <Panel title="Risk" meta="all books"><KV pairs={[
          ['Money in use', <>{inr(R.in_use)}{R.capital ? <span className="muted"> · {Math.round(100 * R.in_use / R.capital)}% of {inr(R.capital)}</span> : null}</>],
          ['Worst case to the stops', <span className="down">{signed(R.worst_case)}</span>],
          ['Biggest position', R.biggest ? `${R.biggest.symbol} ${inr(R.biggest.value)}` : '—'],
          ['Within 2% of a stop', R.near_stop?.length ? <span className="warn">{R.near_stop.map((x) => x.symbol || x).join(' · ')}</span> : <span className="muted">none</span>],
          ['Real orders today', <span className={R.orders_today?.failed ? 'down' : ''}>{R.orders_today?.sent ?? 0} sent · {R.orders_today?.failed ?? 0} failed</span>],
          ['Intraday loss limit', `${inr(R.loss_used)} / ${inr(R.loss_limit)}`],
          ...(sectors ? [['Sectors', sectors]] : [])]} /></Panel>}
      </div>
    </div>
  </div>;
}
