import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { inr, rs, signed, pct, cls } from '../fmt';
import HowItWorks from './HowItWorks';
import AttentionStrip from './AttentionStrip';
import NextUp from './NextUp';
import ReplanPanel from './ReplanPanel';
import { StopMeter } from './ui';
import { useIsPhone } from '../useMedia';
import { Head, Chip, Tag, Cards, Card, Bar, Panel, FixedTable, LineChart, Act } from './ui2';

const EXIT = { stop: '🛑 stop', trail: '🪜 late trail', exit: '📉 trend exit' };
const COLOR = { E: '#b39dff' };
const NAME = { E: 'Paper E+' };
const nm = (k) => (k === 'E' ? 'E+' : k);
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const COL = { gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' };   // a column that never widens with its content
const Meta = ({ children }) => <span style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{children}</span>;
// E+ late trail: a small tag once the trail is active (+100%) or has raised the stop, else the best close
const Trail = ({ x }) => (x.trail_active || x.trailed ? <Tag c="ok" title={`E+ late trail: 25% under the highest close once +100%; only ever raised, from the next session · best ${rs(x.best)}`}>🪜 trail {x.trailed ? 'stop' : 'active'}</Tag>
  : x.best ? <Tag title="best close so far">best {rs(x.best)}</Tag> : null);
const filterText = (f) => (f ? `${f.state}${f.state === 'HALF' ? ` (${f.slots} slots)` : ''}${f.changed ? ` · was ${f.prev}` : ''}` : '—');

const POS_COLS = [{ h: 'Stock' }, { h: 'Strategy', w: 86, lo: true }, { h: 'Qty', w: 56, n: true, lo: true }, { h: 'Bought', w: 150, lo: true }, { h: 'SMA50', w: 86, n: true, lo: true }, { h: 'Last', w: 90, n: true },
  { h: 'After costs', w: 104, n: true }, { h: '%', w: 64, n: true, lo: true }, { h: 'Stop · distance', w: 270, ell: true }, { h: 'Days', w: 56, n: true, lo: true }];
const ORD_COLS = [{ h: 'Stock' }, { h: 'Book', w: 58 }, { h: 'Side', w: 58 }, { h: 'Qty', w: 50, n: true }, { h: 'Stop', w: 80, n: true }, { h: 'Risk', w: 72, n: true, lo: true }];
const TRD_COLS = [{ h: 'Stock' }, { h: 'Book', w: 62 }, { h: 'Closed', w: 150, lo: true }, { h: 'Days', w: 56, n: true, lo: true }, { h: 'Before', w: 100, n: true, lo: true }, { h: 'Costs', w: 80, n: true, lo: true }, { h: 'After', w: 104, n: true }, { h: 'Exit', w: 110, lo: true }];

// 📝 Paper trading: the E+ account, from its start day. Simulated: no real orders.
export default function PaperPage() {
  const [d, setD] = useState(null);
  const [rules, setRules] = useState(null);
  const [err, setErr] = useState('');
  const [cap, setCap] = useState('');
  const [tick, setTick] = useState(0);
  const [how, setHow] = useState(false);
  const [rp, setRp] = useState(null);                      // the last ↻ Re-evaluate report (shown under the orders)
  const [replanning, setReplanning] = useState(false);
  // ↻ Re-evaluate: the next open's orders decided again from the evening's data and the account as it is now
  const replan = (n) => {
    setReplanning(true);
    api.paperReplan(n).then((x) => { setRp(x); setTick((t) => t + 1); }).catch((e) => setErr(e.message)).finally(() => setReplanning(false));
  };
  const phone = useIsPhone();                              // a phone card is narrow: plain-text sub-lines and facts, no bars
  const P = (rich, plain) => (phone ? plain : rich);

  useEffect(() => {
    api.getPaper().then((x) => { setD(x); setErr(''); setCap(String(x.started ? Object.values(x.accounts)[0]?.account.capital ?? x.capital : x.capital)); }).catch((e) => setErr(e.message));
    api.getBreakout().then((b) => setRules(b.rules)).catch(() => {});
  }, [tick]);

  const start = () => api.startPaper(Number(cap)).then(() => setTick((t) => t + 1)).catch((e) => setErr(e.message));
  // the capital box after the start: like Real's, a change is money added / withdrawn on every paper account
  const saveCap = () => {
    const v = Number(cap);
    const now = Object.values(d.accounts)[0]?.account.capital ?? d.capital;
    if (!(v >= 10000) || v === now) return;
    const msg = `Change the paper capital from ${inr(now)} to ${inr(v)}? This counts as ${v > now ? 'adding' : 'withdrawing'} ${inr(Math.abs(v - now))}: `
      + 'cash changes, the profit so far stays, and the next BUY quantities follow the new value.';
    if (window.confirm(msg)) api.paperCapital(v).then((res) => { if (res.replan?.status === 'ok') setRp(res.replan); setTick((t) => t + 1); }).catch((e) => setErr(e.message));
  };
  const reset = () => {
    if (window.confirm('Reset paper trading? The paper account and all its trades are deleted.')) {
      api.resetPaper().then(() => setTick((t) => t + 1)).catch((e) => setErr(e.message));
    }
  };
  if (!d) return <div className="rd"><Panel>{err ? <div className="rbanner bad">{err}</div> : <p className="muted small rempty">Loading…</p>}</Panel></div>;
  if (!d.started) {
    return <div className="rd">
      <Head title="Paper · swing"><Chip>simulated with the Trend Breakout rules · no real orders are placed</Chip></Head>
      {err && <div className="rbanner bad">{err}</div>}
      <div className="two">
        <Panel title="Start paper trading">
          <div className="rtg">A virtual account, <b className="vE">E+ · half filter, top {rules?.top ?? 300}, late trail</b>, starts with
            {' '}this capital. Every evening the breakouts are "filled" at the next open from the daily candle, with the same size, stop, exit
            {' '}and delivery costs as the backtest. Nothing is sent to Kite.</div>
          <div className="rform"><div className="fr"><span>Capital<small>position size = account value ÷ {d.max_pos}; quantities follow from the capital. The default is the paper capital setting.</small></span>
            <span style={{ whiteSpace: 'nowrap' }}>₹<input className="input" type="number" min="10000" step="10000" value={cap} onChange={(e) => setCap(e.target.value)} style={{ width: 110 }} />{' '}
              <button type="button" className="btn primary small" onClick={start} disabled={!(Number(cap) > 0)}>Start</button></span></div></div>
        </Panel>
        <div><HowItWorks r={rules} paper open /></div>
      </div>
    </div>;
  }
  const accts = d.accounts;
  const keys = Object.keys(accts);
  const a0 = accts[keys[0]];
  const shown = keys;
  const slots = (a) => (a.allowed ? a.cap : 0);
  const free = (a) => Math.max(0, slots(a) - a.positions.length + a.orders.filter((o) => o.action === 'SELL').length);
  const last = keys.find((k) => accts[k].curve?.net?.length);
  const lastSession = (k) => { const n = accts[k].curve?.net || []; return n.length > 1 ? n[n.length - 1] - n[n.length - 2] : null; };
  const allTrades = keys.flatMap((k) => accts[k].trades.map((t) => ({ ...t, k }))).sort((a, b) => (b.out || '').localeCompare(a.out || ''));
  const wonN = allTrades.filter((t) => t.net > 0).length;
  const avg = (xs) => (xs.length ? xs.reduce((a, t) => a + (t.sell / t.buy - 1) * 100, 0) / xs.length : null);
  const dailyAll = (() => { const n = (accts[last]?.curve?.net || []); return n.slice(1).map((v, i) => v - n[i]); })();
  const nOrders = keys.reduce((a, k) => a + accts[k].orders.length, 0);
  const buysOf = (k) => accts[k].orders.filter((o) => o.action === 'BUY');
  const skippedN = keys.reduce((a, k) => a + Math.max(0, buysOf(k).length - free(accts[k])), 0);
  const slotsUsed = keys.reduce((a, k) => a + accts[k].positions.length, 0), slotsAll = keys.reduce((a, k) => a + (accts[k].allowed ? accts[k].cap : d.max_pos), 0);

  // open positions, grouped per shown account
  const posRows = shown.flatMap((k) => { const a = accts[k]; return [
    `${NAME[k]} · ${a.positions.length} of ${a.allowed ? a.cap : d.max_pos}${a.filter?.state === 'HALF' ? ' · HALF' : ''} · ${signed(a.positions.reduce((s, x) => s + (x.net || 0), 0))}`,
    ...a.positions.map((x) => ({ key: k + x.symbol, title: `before costs ${signed(x.gross)} · costs ${inr(x.costs)}${x.warning ? ` · ⚠️ ${x.warning}` : ''}`,
      cells: [<><b>{x.symbol}</b>{x.warning && <Tag c="w" title={x.warning}>⚠️ split?</Tag>}</>, <span className={`v${k}`}>{k === 'E' ? 'E+' : k}</span>, x.qty, <>{day(x.in)} · {rs(x.buy)}</>, x.sma != null ? rs(x.sma) : '—', rs(x.last),
        <b className={cls(x.net)}>{signed(x.net)}</b>, <span className={cls(x.gross)}>{pct(x.buy ? (x.last / x.buy - 1) * 100 : null)}</span>,
        <><StopMeter last={x.last} stop={x.stop} /> <span className="mono">{rs(x.stop)}</span><Trail x={x} /></>, x.days ?? '—'] }))]; });
  // orders for the next open: SELL rows first, BUY rows up to the free slots, then the skipped count
  const ordRows = shown.flatMap((k) => { const a = accts[k], sells = a.orders.filter((o) => o.action === 'SELL'), buys = buysOf(k), f = free(a);
    return [...sells.map((o) => ({ key: `${k}s${o.symbol}`, title: o.why, cells: [<b>{o.symbol}</b>, <span className={`v${k}`}>{k === 'E' ? 'E+' : k}</span>, <Tag c="bad">SELL</Tag>, o.qty, '—', <span className="muted">{o.why}</span>] })),
      ...buys.slice(0, f).map((o) => ({ key: `${k}b${o.symbol}`, cells: [<b>{o.symbol}</b>, <span className={`v${k}`}>{k === 'E' ? 'E+' : k}</span>, <Tag c="ok">BUY</Tag>, `~${o.qty_est}`, rs(o.stop), inr(o.risk_est)] })),
      ...(buys.length > f ? [`${NAME[k]} · +${buys.length - f} skipped · ${a.allowed ? 'slots full' : 'filter off'}`] : [])]; });
  const trdRows = allTrades.filter((t) => shown.includes(t.k)).map((t) => ({ key: t.k + t.symbol + t.in, title: `charges ${inr(t.charges)} + slippage ${inr(t.slippage)}`,
    cells: [<b>{t.symbol}</b>, <span className={`v${t.k}`}>{t.k === 'E' ? 'E+' : t.k}</span>, `${day(t.in)} → ${day(t.out)}`, t.days, <span className={cls(t.gross)}>{signed(t.gross)}</span>, <span className="muted">{inr(t.costs)}</span>, <b className={cls(t.net)}>{signed(t.net)}</b>, EXIT[t.why] || t.why] }));
  const totals = shown.map((k) => accts[k].trade_totals).reduce((a, t) => ({ gross: a.gross + (t.gross || 0), costs: a.costs + (t.costs || 0), net: a.net + (t.net || 0) }), { gross: 0, costs: 0, net: 0 });
  if (trdRows.length) trdRows.push({ key: 'total', cells: [<b>Total</b>, '', '', '', <span className={cls(totals.gross)}>{signed(totals.gross)}</span>, <span className="muted">{inr(totals.costs)}</span>, <b className={cls(totals.net)}>{signed(totals.net)}</b>, ''] });
  const curve = a0.curve || {};
  const series = [...shown.map((k) => ({ name: `${NAME[k]} ${pct(accts[k].account.return)}`, pts: accts[k].curve?.net || [], color: COLOR[k], fill: k === 'E' })),
    { name: `NIFTY 50 ${pct(a0.account.nifty)}`, pts: curve.nifty || [], color: 'var(--muted)', dash: true }];
  const n = (curve.dates || []).length;
  const labels = n ? [day(curve.dates[0]), ...(n > 2 ? [day(curve.dates[Math.floor(n / 2)])] : []), day(curve.dates[n - 1])] : [];

  return <div className="rd">
    <AttentionStrip tab="paper" />
    {err && <div className="rbanner bad">{err}</div>}
    <Head title="Paper · swing" right={<>
      <button type="button" className="btn small" onClick={() => setHow((h) => !h)}>{how ? 'Hide how it works' : 'How it works'}</button>
      <button type="button" className="btn small" onClick={() => document.getElementById('papertrades')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>Trade log</button>
      <button type="button" className="btn ghost small" style={{ color: 'var(--bad)', borderColor: 'var(--bad)' }} onClick={reset}>Reset…</button></>}>
      <Chip title={`decided at the ${day(a0.decided)} close`}>filter tonight · {keys.map((k) => <span key={k}> <b className={`v${k}`}>{nm(k)}</b> <b className={accts[k].filter?.state === 'OFF' ? 'down' : 'up'}>{filterText(accts[k].filter)}</b></span>)}</Chip>
      <Chip>started {day(a0.start_date)} · {inr(a0.account.capital)} · data to {day(a0.as_of)} · updated {stamp(a0.updated_at)}</Chip>
    </Head>
    <Cards n={keys.length + 3}>
      {keys.map((k) => { const a = accts[k], A = a.account, cmp = a.compare || {}; return <Card key={k} label={<><span className={`v${k}`}>{NAME[k]}</span>{a.filter ? <span className={a.filter.state === 'OFF' ? 'down' : 'up'}> · {a.filter.state}</span> : null}</>} value={inr(A.value)} num={A.value} k={cls(A.net)}
        sub={P(<><span className={cls(A.return)}>{pct(A.return)}</span> since {day(a.start_date)} · {a.positions.length} open · {a.positions.length} of {a.allowed ? a.cap : d.max_pos} slots · cash {inr(A.cash)}</>, `${pct(A.return)} · ${a.positions.length} of ${a.allowed ? a.cap : d.max_pos} slots · cash ${inr(A.cash)}`)}
        spark={a.curve?.net}
        fact={P(<>vs NIFTY <b className={cls(A.return - (A.nifty || 0))}>{pct(A.return - (A.nifty || 0))}</b> · after costs <b className={cls(A.net)}>{signed(A.net)}</b>{cmp.return != null ? <> · backtest {pct(cmp.return)}</> : null}</>, `vs NIFTY ${pct(A.return - (A.nifty || 0))} · net ${signed(A.net)}`)} />; })}
      <Card label={`Last session · ${last ? nm(last) : '—'}`} value={signed(lastSession(last))} num={lastSession(last)} k={cls(lastSession(last))}
        sub={`${keys.filter((k) => k !== last).map((k) => `${nm(k)} ${signed(lastSession(k))}`).join(' · ')}${keys.length > 1 ? ' · ' : ''}${day(a0.as_of)}`} spark={dailyAll.slice(-30)}
        fact={dailyAll.length ? P(<>best day <b className="up">{signed(Math.max(...dailyAll))}</b> · worst <b className="down">{signed(Math.min(...dailyAll))}</b></>, `best ${signed(Math.max(...dailyAll))} · worst ${signed(Math.min(...dailyAll))}`) : 'after the first session'} />
      <Card unit="n" label="Trades closed" value={allTrades.length} num={allTrades.length} sub={`won ${wonN} · lost ${allTrades.length - wonN} · ${allTrades.length ? Math.round(1000 * wonN / allTrades.length) / 10 : 0}%`}
        fact={P(<>avg win <b className="up">{pct(avg(allTrades.filter((t) => t.net > 0)))}</b> · avg loss <b className="down">{pct(avg(allTrades.filter((t) => t.net <= 0)))}</b>{keys.map((k) => <span key={k}> · PF {accts[k].profit_factor ?? '—'} · DD {pct(-accts[k].drawdown)}</span>)}</>, `avg win ${pct(avg(allTrades.filter((t) => t.net > 0)))} · loss ${pct(avg(allTrades.filter((t) => t.net <= 0)))}`)} />
      <Card unit="n" label="Orders for the next open" value={nOrders} num={nOrders} sub={`${keys.map((k) => `${keys.length > 1 ? `${nm(k)} ` : ''}buys ${buysOf(k).length}`).join(' · ')} · at the 9:15 AM open`}
        fact={P(<><Bar pct={slotsAll ? 100 * slotsUsed / slotsAll : 0} />slots {slotsUsed} of {slotsAll}{skippedN ? <> · <b>{skippedN}</b> skipped</> : ''}</>, `slots ${slotsUsed} of ${slotsAll}${skippedN ? ` · ${skippedN} skipped` : ''}`)} />
    </Cards>
    {how && <HowItWorks r={rules} paper open />}
    <div className="two">
      <div className="rd" style={COL}>
        <Panel title="Since the start · vs NIFTY 50" meta="after costs" right={<Meta>NIFTY 50 with the same capital, dashed</Meta>}>
          <LineChart series={series} labels={labels} h={200} />
        </Panel>
        <Panel title={`Open · ${shown.map((k) => (k === 'E' ? 'E+' : k)).join(' + ')}`} meta={shown.reduce((a, k) => a + accts[k].positions.length, 0)} right={<Meta>net <b className={`mono ${cls(shown.reduce((a, k) => a + accts[k].positions.reduce((s, x) => s + (x.net || 0), 0), 0))}`}>{signed(shown.reduce((a, k) => a + accts[k].positions.reduce((s, x) => s + (x.net || 0), 0), 0))}</b> · after costs, if sold</Meta>}>
          <FixedTable cols={POS_COLS} rows={posRows} empty="None." />
          {shown.some((k) => accts[k].notes?.length) && <div className="rnote">{shown.flatMap((k) => (accts[k].notes || []).map((x) => `📌 ${x.day}: ${x.text}`)).join(' · ')}</div>}
        </Panel>
        <Panel id="papertrades" title="Trade log" meta={trdRows.length ? trdRows.length - 1 : 0}>
          <FixedTable cols={TRD_COLS} rows={trdRows} empty="None yet." />
          <div className="rnote">Fills use the day's open from the daily candle (+{d.slippage_pct}% slippage a fill) and delivery charges (STT, exchange, SEBI, GST, stamp, DP). Stops are checked on the day's low after the close (no live intraday price), as in the backtest. Past results don't guarantee future returns.</div>
        </Panel>
      </div>
      <div className="rd" style={COL}>
        <Panel title="Orders for the next open" meta={nOrders} right={<><Meta>decided {day(a0.decided)}</Meta>
          <button type="button" className="btn primary small" disabled={replanning} onClick={() => replan()} title="decide the next open's orders again from the evening's data and the account as it is now · simulated, nothing is sent">{replanning ? 'Planning…' : '↻ Re-evaluate plan'}</button></>}>
          <FixedTable cols={ORD_COLS} rows={ordRows} empty="None." />
        </Panel>
        <ReplanPanel r={rp} paper busy={replanning} onRaise={(n) => replan(n)} onKeep={() => setRp(null)} />
        <Panel title="Accounts" meta={keys.length} right={<Meta>capital <b className="mono">{inr(a0.account.capital)}</b></Meta>}>
          {keys.map((k) => { const A = accts[k].account; return <Act key={k} icon={k} cls={cls(A.net)} text={<><b className={`v${k}`}>{NAME[k]}</b> · {signed(A.gross)} − {inr(A.costs)} = <b className={cls(A.net)}>{signed(A.net)}</b></>}
            meta={`capital ${inr(A.capital)} · deposits ${(A.deposited || 0) < 0 ? '−' : '+'}${inr(Math.abs(A.deposited || 0))} · charges ${inr(A.charges)} · slippage ${inr(A.slippage)} · realised ${signed(A.realised)} · unrealised ${signed(A.unrealised)} · won ${accts[k].won} of ${accts[k].closed}`} />; })}
          <div className="rform"><div className="fr"><span>Capital<small>size = account value ÷ {d.max_pos} · a change counts as money added / withdrawn, not profit</small></span>
            <span style={{ whiteSpace: 'nowrap' }}>₹<input className="input" type="number" min="10000" step="10000" value={cap} onChange={(e) => setCap(e.target.value)} style={{ width: 110 }} />{' '}
              <button type="button" className="btn small" onClick={saveCap} disabled={!(Number(cap) >= 10000) || Number(cap) === a0.account.capital}>Save</button></span></div></div>
        </Panel>
        <NextUp />
      </div>
    </div>
  </div>;
}
