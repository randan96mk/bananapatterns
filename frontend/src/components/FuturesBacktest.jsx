import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { inr, pct, cls, lakh } from '../fmt';
import { Chip, Cards, Card, Bar, Panel, FixedTable, Seg, LineChart, Act, Tag } from './ui2';

// 🧪 Backtest F: E+ long / E− short on stock futures (one lot a name) against the same signals in cash and NIFTY 50.
// Read-only report of the saved run; ▷ Run re-runs it in the background; "Report as if" saves the capital and the lot
// cap to the futures settings before the run (the owner confirms: these are the live book's limits too).
const PAGE = 100;
const SIDES = [['long', 'Long'], ['short', 'Short']];
const SUBS = [['report', 'Report'], ['robust', 'Robustness'], ['trades', 'Trades']];
const EXIT = { stop: '🛑 stop', trail: '🪜 late trail', time: '⏱ time stop', rule: '📉 trend exit', roll_exit: '↻ roll · rule fired', end: '⏹ end of test' };
const SKIP = { lot: 'lot too big', risk: 'risk over the cap', slots: 'no free slot', ban: 'in the ban list', results: 'results close', notional: 'notional cap', cash: 'cash floor', heat: 'heat cap', oi: 'OI rank', held: 'held elsewhere', margin: 'margin 80%', daily_loss: 'daily loss limit', no_bar: 'no futures bar' };
const n2 = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' }) : '');
const mon = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { month: 'short', year: 'numeric' }) : '');
const IN = { fontSize: 12, padding: '3px 6px', textAlign: 'right' };

function ReportAsIf({ rules, onDone, onErr }) {
  const [f, setF] = useState({ capital: String(rules.capital), lot: String(rules.max_lot_value) });
  const [busy, setBusy] = useState(false);
  const ok = Number(f.capital) >= 100000 && Number(f.lot) >= 100000;
  const go = () => {
    if (!ok || !window.confirm(`Save capital ${inr(Number(f.capital))} and lot cap ${lakh(Number(f.lot))} to the futures settings and re-run Backtest F?\n\nThese are the live book's limits too (eligibility and sizing); the report then runs with them.`)) return;
    setBusy(true);
    api.putConfig('futures', { capital: Number(f.capital), max_lot_value: Number(f.lot) }).then(() => api.runFuturesBacktest()).then(() => onDone()).catch((e) => onErr(e.message)).finally(() => setBusy(false));
  };
  return <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', padding: '10px 14px', fontSize: 12, color: 'var(--muted)' }}>
    <b style={{ color: 'var(--text)' }}>⚙ Report as if</b>
    <span style={{ whiteSpace: 'nowrap' }}>capital ₹ <input className="input" type="number" min="100000" step="50000" value={f.capital} onChange={(e) => setF({ ...f, capital: e.target.value })} style={{ ...IN, width: 110 }} /></span>
    <span style={{ whiteSpace: 'nowrap' }}>lot cap ₹ <input className="input" type="number" min="100000" step="50000" value={f.lot} onChange={(e) => setF({ ...f, lot: e.target.value })} style={{ ...IN, width: 110 }} /></span>
    <button type="button" className="btn primary small" disabled={!ok || busy} onClick={go}>{busy ? 'Starting…' : '▷ Run with these'}</button>
    <span className="small">saves to the futures settings first (max 2 lots, stop cap, roll T-2 stay)</span></div>;
}

export default function FuturesBacktest({ onBook }) {
  const [d, setD] = useState(null);
  const [side, setSide] = useState('long');
  const [sub, setSub] = useState('report');
  const [trades, setTrades] = useState({ total: 0, rows: [] });
  const [err, setErr] = useState('');
  const [tick, setTick] = useState(0);
  const [asIf, setAsIf] = useState(false);
  useEffect(() => {
    let alive = true, timer;
    const load = () => Promise.all([api.getFuturesBacktest(), api.getFuturesBacktestTrades(side, PAGE, 0)]).then(([s, t]) => {
      if (!alive) return;
      setD(s); setTrades(t); setErr('');
      if (s.progress?.state !== 'idle') timer = setTimeout(load, 3000);
    }).catch((e) => { if (alive) setErr(e.message); });
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, [side, tick]);
  const more = () => api.getFuturesBacktestTrades(side, PAGE, trades.rows.length).then((t) => setTrades({ total: t.total, rows: [...trades.rows, ...t.rows] })).catch((e) => setErr(e.message));
  const run = () => api.runFuturesBacktest().then(() => setTick((x) => x + 1)).catch((e) => setErr(e.message));
  if (!d) return <Panel title="Backtest F"><p className="muted small rempty">{err ? <span className="warn">{err}</span> : 'Loading…'}</p></Panel>;
  const prog = d.progress || {}, R = d.rules || {}, Rf = R.futures || {};
  const running = prog.state && prog.state !== 'idle';
  const runBtn = running ? <Chip dot="warn"><span className="pulse running blink" /> running… {prog.message}</Chip>
    : <button type="button" className="btn primary small" onClick={run}>{d.built ? '▷ Re-run' : '▷ Run Backtest F'}</button>;
  const head = <div className="rhead" style={{ marginTop: 0 }}><h1 style={{ fontSize: 15 }}>Backtest F · E+ long / E− short on stock futures</h1>
    {d.built && <Chip>{mon(d.run.run.from)} → {mon(d.run.run.to)} · <b>{d.run.run.sessions}</b> sessions · {d.run.run.names} futures names · signals on cash candles</Chip>}
    <Chip>{inr(Rf.capital)} · max {Rf.max_pos} lots · lot ≤ {lakh(Rf.max_lot_value)} · stop ATR cap {Rf.max_stop_pct}% · roll T-{Rf.roll_days}</Chip>
    {d.built && <Chip>updated <b>{stamp(d.run.ran_at)}</b></Chip>}
    <span className="r"><button type="button" className={`btn small ${asIf ? 'primary' : ''}`} onClick={() => setAsIf(!asIf)}>⚙ Report as if…</button>{runBtn}{onBook && <button type="button" className="btn small" onClick={onBook}>← Book</button>}</span></div>;
  if (!d.built) {
    return <div className="rd" style={{ gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' }}>{head}
      {err && <div className="rbanner warn">{err}</div>}
      {asIf && <Panel><ReportAsIf rules={Rf} onDone={() => { setAsIf(false); setTick((x) => x + 1); }} onErr={setErr} /></Panel>}
      <Panel title="Not run yet"><div className="rbody">Three years of the Trend Breakout signals traded on stock futures from the stored cash and continuous futures candles: entries at the next futures open, ATR stops in points, one lot a name, margin, roll cost, futures charges; dev / val / OOS splits, robustness neighbours and a verdict per side. {prog.message ? <span className="muted">{prog.message}</span> : 'Run it once to see it (needs the futures data step to have run).'}</div></Panel></div>;
  }
  const r = d.run, L = r.long, Sh = r.short, C = r.cash_same_signals || {}, NF = r.nifty || {};
  const V = side === 'long' ? L : Sh;
  const oos = L.splits?.[L.splits.length - 1];
  const cap = r.run.capital;
  const dates = L.curve?.dates || [];
  const years = []; dates.forEach((x, i) => { if (i === 0 || x.slice(0, 4) !== dates[i - 1].slice(0, 4)) years.push(x.slice(0, 4)); });
  const series = [{ name: 'Long futures', pts: L.curve?.net, color: 'var(--ok)' }, { name: 'Long · stressed', pts: L.curve?.stress, color: 'var(--ok)', dash: true },
    { name: 'Short futures (paper)', pts: Sh.curve?.net, color: 'var(--bad)' }, { name: 'NIFTY 50', pts: NF.curve, color: 'var(--muted)', dash: true }];
  const cmpCols = [{ h: '' }, { h: `Cash E+ (${lakh(cap)})`, w: 150, n: true }, { h: 'Futures long', w: 140, n: true }, { h: 'Futures short', w: 140, n: true }];
  const row = (label, a, b, c) => ({ key: label, cells: [label, <span className="muted">{a}</span>, <b>{b}</b>, c] });
  const cmpRows = [row('Signals taken', C.trades, `${L.trades} (lots)`, Sh.trades), row('CAGR', pct(C.cagr), <span className={cls(L.cagr)}>{pct(L.cagr)}</span>, <span className={cls(Sh.cagr)}>{pct(Sh.cagr)}</span>),
    row('Net after costs', inr(C.net), <span className={cls(L.net)}>{inr(L.net)}</span>, <span className={cls(Sh.net)}>{inr(Sh.net)}</span>),
    row('Max drawdown', pct(-C.drawdown, 0), <span className="down">{pct(-L.drawdown, 0)}</span>, <span className="down">{pct(-Sh.drawdown, 0)}</span>),
    row('Costs + rolls', inr(C.costs), inr(L.costs), inr(Sh.costs)), row('Worst month', pct(C.worst_month), pct(L.worst_month), pct(Sh.worst_month)),
    row('Days with no slot', C.no_slot_days, L.no_slot_days, Sh.no_slot_days), row('Profit factor · win rate', '—', `${L.profit_factor ?? '—'} · ${L.win_rate?.toFixed(0)}%`, `${Sh.profit_factor ?? '—'} · ${Sh.win_rate?.toFixed(0)}%`),
    row('Average hold', '—', `${L.avg_hold?.toFixed(0)} days`, `${Sh.avg_hold?.toFixed(0)} days`), row('Margin · worst day', '—', `${L.margin_worst_pct}%`, `${Sh.margin_worst_pct}%`),
    row('NIFTY 50', `${pct(NF.cagr)} CAGR`, `DD ${pct(-NF.drawdown, 0)}`, `${pct(NF.return)} total`)];
  const SHORT = { Development: 'Dev', Validation: 'Val', 'Out-of-sample': 'OOS' };
  const splitCols = [{ h: '', ell: true }, ...L.splits.map((x) => ({ h: SHORT[x.name] || x.name.slice(0, 3), key: x.name, w: 72, n: true, title: x.name }))];
  const splitRows = [{ key: 'l', cells: ['Long · after costs', ...L.splits.map((x) => <b className={cls(x.net)}>{pct(x.net, 0)}</b>)] },
    { key: 'ls', cls: 'dim', cells: ['stressed', ...L.splits.map((x) => <small className={cls(x.stress)}>{pct(x.stress, 0)}</small>)] },
    { key: 's', cells: ['Short · after costs', ...Sh.splits.map((x) => <b className={cls(x.net)}>{pct(x.net, 0)}</b>)] },
    { key: 'ss', cls: 'dim', cells: ['stressed', ...Sh.splits.map((x) => <small className={cls(x.stress)}>{pct(x.stress, 0)}</small>)] },
    { key: 'n', cells: ['NIFTY 50', ...L.splits.map((x) => <span className="muted">{pct(x.nifty, 0)}</span>)] }];
  const yearCols = [{ h: 'Year' }, { h: 'Long', w: 90, n: true }, { h: 'Short', w: 90, n: true }, { h: 'NIFTY 50', w: 90, n: true }];
  const yearRows = L.years.map((y, i) => ({ key: y.year, cells: [y.year, <span className={cls(y.net)}>{pct(y.net)}</span>, <span className={cls(Sh.years[i]?.net)}>{pct(Sh.years[i]?.net)}</span>, <span className={cls(y.nifty)}>{pct(y.nifty)}</span>] }));
  const trCols = [{ h: 'Contract' }, { h: 'Entry', w: 150 }, { h: 'Exit', w: 150 }, { h: 'Days', w: 60, n: true, lo: true }, { h: 'Rolls', w: 60, n: true, lo: true }, { h: 'Before costs', w: 115, n: true, lo: true },
    { h: 'Costs', w: 90, n: true, lo: true }, { h: 'After costs', w: 110, n: true }, { h: 'Exit reason', w: 150 }];
  const trRows = trades.rows.map((t) => ({ key: `${t.sym}${t.in}`, cells: [<><b>{t.sym}</b> <small>{t.lots} lot · {t.qty}</small></>, <>{day(t.in)} <small>{n2(t.entry)}</small></>, <>{day(t.out)} <small>{n2(t.exit)}</small></>,
    t.days, t.rolls, <span className={cls(t.gross)}>{inr(t.gross)}</span>, <span className="muted">{inr(t.costs)}</span>, <b className={cls(t.net)}>{inr(t.net)}</b>, EXIT[t.why] || t.why] }));
  const rollsPerYear = r.run.sessions ? (L.rolls.n / (r.run.sessions / 250)) : 0;
  const basisPct = cap && r.run.sessions ? (100 * L.rolls.basis / cap) / (r.run.sessions / 250) : 0;
  const skipped = (k) => (V.skipped || {})[k] ?? 0;
  return <div className="rd" style={{ gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' }}>
    {head}
    {err && <div className="rbanner warn">{err}</div>}
    {asIf && <Panel><ReportAsIf rules={Rf} onDone={() => { setAsIf(false); setTick((x) => x + 1); }} onErr={setErr} /></Panel>}
    {r.run.missing?.length > 0 && <div className="rbanner">⚠️ {r.run.missing.length} futures name(s) without enough candles were left out: {r.run.missing.slice(0, 12).join(', ')}{r.run.missing.length > 12 ? ' …' : ''}</div>}
    <Cards n={6}>
      <Card unit="%" label="Long · CAGR" value={pct(L.cagr)} num={L.cagr} k={cls(L.cagr)} sub={`${lakh(cap)} → ${lakh(cap + L.net)} · after costs and rolls`} spark={L.curve?.net}
        fact={<>max DD <Bar pct={L.drawdown} bad /> {pct(-L.drawdown)}</>} title={`stressed CAGR ${pct(L.stress?.cagr)} · net ${inr(L.net)} · costs ${inr(L.costs)}`} />
      <Card unit="n" label="Long · trades · won" value={`${L.trades} · ${L.win_rate?.toFixed(0)}%`} num={L.trades} sub={<>avg win <span className="up">{inr(L.avg_win)}</span> · avg loss <span className="down">{inr(L.avg_loss)}</span></>}
        fact={<>skipped: lot too big <b>{L.skipped?.lot ?? 0}</b> · slots <b>{L.skipped?.slots ?? 0}</b></>} title={`profit factor ${L.profit_factor ?? '—'} · ${L.avg_hold?.toFixed(0)} days held · ${L.no_slot_days} days with no slot`} />
      <Card unit="%" label="Short · CAGR" value={pct(Sh.cagr)} num={Sh.cagr} k={cls(Sh.cagr)} sub={`${Sh.trades} trades · won ${Sh.win_rate?.toFixed(0)}% · paper only`} spark={Sh.curve?.net}
        fact={<>max DD <Bar pct={Sh.drawdown} bad /> {pct(-Sh.drawdown)}</>} title={`stressed CAGR ${pct(Sh.stress?.cagr)} · net ${inr(Sh.net)}`} />
      <Card label="Roll cost · long" value={inr(L.rolls.cost)} num={L.rolls.cost} k="down" sub={`${L.rolls.n} rolls · ${inr(L.rolls.n ? L.rolls.cost / L.rolls.n : 0)} each · ${rollsPerYear.toFixed(0)} a year`}
        fact={<>basis paid <Bar pct={Math.min(100, Math.abs(basisPct) * 20)} /> {basisPct.toFixed(1)}% a year</>} title={`basis booked after each expiry ${inr(L.rolls.basis)} · roll charges T-${Rf.roll_days}`} />
      <Card unit="%" label="Margin · worst day" value={`${L.margin_worst_pct}%`} num={L.margin_worst_pct} k={L.margin_worst_pct >= 80 ? 'down' : L.margin_worst_pct >= 70 ? 'warn' : ''} sub={`${Rf.max_pos} lots · ${L.loss_days} daily-loss days`}
        fact={<>cash floor held <Bar pct={L.cash_floor_held ? 100 : 30} ok={L.cash_floor_held} bad={!L.cash_floor_held} /> {L.cash_floor_held ? 'always' : 'broken'}</>} />
      <Card unit="%" label="Out-of-sample · long" value={pct(oos?.net)} num={oos?.net} k={cls(oos?.net)} sub={`${oos?.name} · ${day(oos?.from)} → ${day(oos?.to)} · stressed ${pct(oos?.stress)}`}
        fact={<>robust <Bar pct={L.robust_of ? (100 * L.robust_pass) / L.robust_of : 0} ok={L.robust_pass >= R.robust_min} bad={L.robust_pass < R.robust_min} /> {L.robust_pass} of {L.robust_of}</>} title={`${L.robust_pass} of ${L.robust_of} counted neighbours pass (need ${R.robust_min})`} />
    </Cards>
    <Seg opts={SUBS} v={sub} set={setSub} />
    {sub === 'report' && <div className="two">
      <div className="rd" style={{ gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' }}>
        <Panel title={`Equity · ${inr(cap)} start`} right={<span className="muted">{years[0]} → {years[years.length - 1]} · ₹</span>}>
          <LineChart series={series} h={220} labels={years} />
        </Panel>
        <Panel title="Why futures beat or lose to cash on the same signals" right={<span className="muted">{C.note}</span>}>
          <FixedTable cols={cmpCols} rows={cmpRows} />
          <div className="rnote">Cash: E+ in the cash book at the futures capital on the futures names (delivery charges). Futures: one lot a name, margin {Rf.margin_pct}%, STT on the sell side, brokerage ₹{R.brokerage} an order, slippage {R.slippage_pct}% ({R.stress_slippage_pct}% stressed).</div>
        </Panel>
        <Panel title="How trades ended" right={<Seg opts={SIDES} v={side} set={setSide} small />}>
          {Object.keys(EXIT).map((e) => <Act key={e} icon="·" text={EXIT[e]}><b className="mono" style={{ width: 48, textAlign: 'right', flex: 'none' }}>{V.exits?.[e] ?? 0}</b></Act>)}
          <div className="rnote">Signals not taken ({side})</div>
          {Object.keys(SKIP).filter((k) => skipped(k)).map((e) => <Act key={e} icon="⛔" text={<span className="muted">{SKIP[e]}</span>}><b className="mono" style={{ width: 48, textAlign: 'right', flex: 'none' }}>{skipped(e)}</b></Act>)}
          {!Object.keys(SKIP).some((k) => skipped(k)) && <p className="muted small rempty">Every signal fit.</p>}
        </Panel>
      </div>
      <div className="rd" style={{ gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' }}>
        <Panel title="Dev · val · out-of-sample" right={<span className="muted">50% / 25% / 25%</span>}>
          <FixedTable cols={splitCols} rows={splitRows} />
          <div className="rnote">An edge must hold in all three, at stressed slippage too. {L.splits.map((x) => `${x.name} ${day(x.from)} – ${day(x.to)}`).join(' · ')}</div></Panel>
        <Panel title={`Robustness · ${side}`} meta={`${V.robust_pass} of ${V.robust_of}`} right={<Seg opts={SIDES} v={side} set={setSide} small />}>
          {V.robust.map((x) => <Act key={x.label} icon={x.ok ? '✅' : '❌'} text={<>{x.label}{!x.counted && <Tag title="shown for information: not counted">info</Tag>}</>}
            meta={x.detail || `${x.trades} trades · CAGR ${pct(x.cagr)} · DD ${pct(-x.drawdown, 0)}${x.periods ? ` · ${x.periods.map((v) => pct(v, 0)).join(' / ')}` : ''}`} />)}
          <div className="rnote">✅ = profitable in every split (stressed too) and beats NIFTY 50 · {R.robust_min} of {R.robust_of} counted neighbours must pass</div></Panel>
        <Panel title="Verdict">
          <Act icon={L.verdict ? '✓' : '✗'} cls={L.verdict ? 'up' : 'down'} text={<b>Long side: {L.verdict ? 'fit for Real' : 'not yet'}</b>} meta={L.verdict ? 'once 4 weeks of paper match the backtest within slippage' : L.reasons.join(' · ')} />
          <Act icon={Sh.verdict ? '✓' : '✗'} cls={Sh.verdict ? 'up' : 'down'} text={<b>Short side: {Sh.verdict ? 'real allowed' : 'paper only'}</b>} meta={Sh.verdict ? 'the short Real switch may be turned on' : `${Sh.reasons.join(' · ')} · revisit after 100 paper signals`} />
          <div className="rnote">drawdown line −{R.dd_cap?.long}% (long) / −{R.dd_cap?.short}% (short) · the short verdict gates the short Real switch</div></Panel>
        <Panel title="Year by year" right="after costs"><FixedTable cols={yearCols} rows={yearRows} /></Panel>
      </div>
    </div>}
    {sub === 'robust' && <Panel title="Robustness · one setting changed at a time" right={<><Seg opts={SIDES} v={side} set={setSide} small /><span className="muted">{V.robust_pass} of {V.robust_of} pass</span></>}>
      <FixedTable cols={[{ h: 'Changed' }, { h: 'Trades', w: 80, n: true, lo: true }, { h: 'CAGR', w: 90, n: true }, { h: 'Stressed', w: 90, n: true, lo: true }, { h: 'Max DD', w: 90, n: true, lo: true }, { h: 'Dev / Val / OOS', w: 170, n: true, lo: true }, { h: 'Pass', w: 60 }]}
        rows={V.robust.map((x) => ({ key: x.label, cells: [<>{x.label}{!x.counted && <Tag>info</Tag>}</>, x.trades, <span className={cls(x.cagr)}>{pct(x.cagr)}</span>, pct(x.stress_cagr), pct(-x.drawdown, 0), x.periods ? x.periods.map((v) => pct(v, 0)).join(' / ') : x.detail || '—', x.ok ? '✅' : '❌'] }))} />
      <div className="rnote">{r.run.notes?.map((n, i) => <div key={i}>• {n}</div>)}</div></Panel>}
    {sub === 'trades' && <Panel title={`Trades · ${side}`} meta={trades.total} right={<><Seg opts={SIDES} v={side} set={setSide} small /><a className="btn small" href={`/api/futures/backtest/csv?side=${side}`} download>⬇ CSV</a></>}>
      <FixedTable cols={trCols} rows={trRows} empty="No trades." />
      {trades.total > trades.rows.length && <div className="rnote"><button type="button" className="btn ghost small" onClick={more}>Show {Math.min(PAGE, trades.total - trades.rows.length)} more</button><span className="muted small"> &nbsp;{trades.rows.length} of {trades.total}</span></div>}
    </Panel>}
    <div className="rnote">⚠️ Universe is today's futures list with today's lot sizes (survivorship and lot-size bias); the basis at each roll comes from the continuous series' jump net of the cash move. Past results don't guarantee future returns.</div>
  </div>;
}
