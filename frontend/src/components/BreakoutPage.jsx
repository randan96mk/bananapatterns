import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import HowItWorks from './HowItWorks';
import AttentionStrip from './AttentionStrip';
import NextUp from './NextUp';
import { Tip } from './ui';
import { Head, Chip, Cards, Card, Bar, Panel, FixedTable, Seg, Tag } from './ui2';
import { signed as signedInr, inr, pct, cls, lakh } from '../fmt';

const SCEN = [['normal', 'Normal slippage'], ['stress', 'Stressed slippage']];
const EXIT = { stop: '🛑 stop', trail: '🪜 late trail', exit: '📉 trend exit (SMA50)', end: '⏹ end of test' };
const SKIP = { no_slot: 'no free slot', market_off: 'market filter off', too_small: 'too small (under 1 share)' };
const STATE = { ON: ['✅', 'up'], FULL: ['✅', 'up'], HALF: ['🟡', 'warn'], OFF: ['⛔', 'down'] };
const PAGE = 100;
const SUBS = [['results', 'Results'], ['robust', 'Robustness'], ['scan', "Tonight's scan"], ['trades', 'Trades'], ['how', 'How it works']];

const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' }) : '');

export function FilterBadge({ k, f }) {
  if (!f) return null;
  const [icon, c] = STATE[f.state] || ['', ''];
  return (
    <span className="filterbadge"><b className={`v${k}`}>{k}</b> <b className={c}>{icon} {f.state}</b>
      {f.state === 'HALF' && <span className="muted small"> ({f.slots} slots)</span>}
      {f.changed && <span className="muted small"> · was {f.prev}</span>}</span>);
}

// One-line label and a fixed-width number slot for the exit rows (the row never grows with a number).
const ONE = { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
const SLOT = { minWidth: 0, overflow: 'hidden' };
const ROW = (n) => ({ display: 'grid', gridTemplateColumns: `minmax(0, 1fr) repeat(${n}, 110px)`, gap: 10 });

// A fixed-width share bar for a list row (the card bar only lives inside a card).
const Share = ({ pct: p, c }) => <span style={{ display: 'inline-block', width: 70, height: 4, borderRadius: 2, background: 'var(--hover)', overflow: 'hidden', verticalAlign: 'middle', flex: 'none' }}>
  <i style={{ display: 'block', height: '100%', width: `${Math.max(0, Math.min(100, p || 0))}%`, background: c }} /></span>;

// Account value (₹): A (blue), E (purple), NIFTY 50 with the same capital (dashed).
function Curve({ dates, lines, nifty, capital }) {
  if (!dates?.length) return <p className="muted small rempty">No curve yet.</p>;
  const W = 560, H = 200, L = 46, T = 14, B = 24;
  const all = [...lines.flatMap((l) => l.vals), ...nifty, capital];
  const lo = Math.min(...all), hi = Math.max(...all);
  const n = dates.length;
  const x = (i) => L + ((W - L - 8) * i) / Math.max(1, n - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
  const line = (vals) => vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const raw = (hi - lo) / 6, mag = 10 ** Math.floor(Math.log10(raw || 1));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((v) => v >= raw);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v);
  const years = [];
  dates.forEach((d, i) => { if (i === 0 || d.slice(0, 4) !== dates[i - 1].slice(0, 4)) years.push([i, d]); });
  return (
    <svg className="orbcurve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Account value, A and E vs NIFTY 50">
      {ticks.map((v) => <g key={v}><line x1={L} x2={W - 8} y1={y(v)} y2={y(v)} className="grid" />
        <text x={L - 6} y={y(v) + 3} textAnchor="end">{lakh(v)}</text></g>)}
      {years.map(([i, d]) => <text key={d} x={x(i)} y={H - 6} textAnchor="middle">{d.slice(0, 4)}</text>)}
      <polyline points={line(nifty)} className="nifty" />
      {lines.map((l) => <polyline key={l.k} points={line(l.vals)} className={`var${l.k}`} />)}
    </svg>
  );
}

// The 📈 Trend Breakout tab: A (200-day filter) and E (half filter) side by side; the evening scan. Signals only.
export default function BreakoutPage() {
  const [scen, setScen] = useState('normal');
  const [tv, setTv] = useState('A');
  const [d, setD] = useState(null);
  const [trades, setTrades] = useState({ total: 0, rows: [] });
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  const [sub, setSub] = useState('results');

  useEffect(() => {
    let alive = true, timer;
    setLoading(true);
    const load = () => Promise.all([api.getBreakout(), api.getBreakoutTrades(tv, scen, PAGE, 0)])
      .then(([s, t]) => {
        if (!alive) return;
        setD(s); setTrades(t); setErr(''); setLoading(false);
        if (s.progress?.state !== 'idle') timer = setTimeout(load, 3000);   // running: refresh
      })
      .catch((e) => { if (alive) { setErr(e.message); setLoading(false); } });
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, [scen, tv, tick]);

  const more = () => api.getBreakoutTrades(tv, scen, PAGE, trades.rows.length)
    .then((t) => setTrades({ total: t.total, rows: [...trades.rows, ...t.rows] })).catch((e) => setErr(e.message));
  const run = () => api.runBreakout().then(() => setTick((x) => x + 1)).catch((e) => setErr(e.message));

  if (!d) return <div className="rd"><Panel title="📈 Trend Breakout"><p className="muted small rempty">{err ? <span className="warn">{err}</span> : 'Loading…'}</p></Panel></div>;
  const prog = d.progress || {};
  const R = d.rules || {};
  const running = prog.state && prog.state !== 'idle';
  const runBtn = running ? <Chip dot="warn"><span className="pulse running blink" /> running… {prog.message}</Chip>
    : <button type="button" className="btn primary small" onClick={run}>{d.built ? '▷ Re-run' : '▷ Run the backtest'}</button>;

  if (!d.built) {
    return (
      <div className="rd">
        <AttentionStrip tab="breakout" />
        <Head title="📈 Trend Breakout · A vs E" right={runBtn}>{prog.message && !running && <Chip>{prog.message}</Chip>}</Head>
        {err && <div className="rbanner warn">{err}</div>}
        <Panel title="Not run yet"><div className="rbody">A 6-year swing backtest of two variants side by side (A: 200-day market filter, E: half filter), from the stored daily candles
          (no 5-minute data, no Kite requests), plus the evening scan. Run it once to see both.</div></Panel>
        <Panel><HowItWorks r={R} open /></Panel>
      </div>);
  }
  const r = d.run;
  const V = r.variants;
  const keys = Object.keys(V);
  const S = (k) => V[k][scen];
  const sc = d.scan;
  const scenTxt = scen === 'stress' ? 'stressed' : 'normal';
  const E = V.E?.[scen];
  const oos = V.E?.splits?.[V.E.splits.length - 1];
  const row = (label, f, extra) => ({ key: label, cells: [label, ...keys.map((k) => f(V[k], k)), extra ?? ''] });
  const resCols = [{ h: '' }, ...keys.map((k) => ({ h: V[k].name, key: k, w: 220, n: true })), { h: 'NIFTY 50', w: 120, n: true }];
  const resRows = [
    row('Net after costs', (v) => <b className={cls(v[scen].net)}>{inr(v[scen].net)}</b>, inr(r.nifty.return / 100 * R.capital)),
    row('Before costs · costs', (v) => <span className="muted">{inr(v[scen].gross)} · −{inr(v[scen].costs)}</span>),
    row('CAGR after costs', (v) => <b>{pct(v[scen].cagr)}</b>, pct(r.nifty.cagr)),
    row('CAGR stressed', (v) => pct(v.stress.cagr)),
    row('Worst drawdown', (v) => <span className="down">{pct(-v[scen].drawdown, 0)}</span>, pct(-r.nifty.drawdown, 0)),
    row('Trades · won', (v) => `${v[scen].trades} · ${v[scen].win_rate.toFixed(0)}%`),
    row('Profit factor', (v) => v[scen].profit_factor ?? '—'),
    row('Average win / loss', (v) => (v[scen].avg_win != null ? <span><span className="up">{inr(v[scen].avg_win)}</span> / <span className="down">{inr(v[scen].avg_loss)}</span></span> : '—')),
    row('Biggest winner', (v) => (v[scen].best_trade_pct != null ? <span className="up">+{v[scen].best_trade_pct.toFixed(0)}%</span> : '—')),
    row('Late trail', (v) => (v.late_at ? <span title={`${v.late_pct}% under the best close once +${v.late_at}%`}>{v.late_pct}% from +{v.late_at}% · {v[scen].exits?.trail ?? 0} exits</span> : 'none')),
    row('Average hold', (v) => `${v[scen].avg_hold.toFixed(0)} days`),
    row('Costs a trade', (v) => inr(v[scen].cost_per_trade)),
    row('New buys allowed', (v) => `${v.allowed_pct.toFixed(0)}% of days`),
    row('Universe', (v) => `top ${v.top ?? '—'}`),
    row('Different stocks traded', (v) => v.stocks_traded ?? '—'),
    row('Robustness', (v) => `${v.robust_pass} of ${v.robust.length} pass`),
  ];
  const splits = V[keys[0]].splits;
  const splitCols = [{ h: '' }, ...splits.map((x) => ({ h: x.name, key: x.name, w: 185, n: true }))];
  const splitRows = keys.flatMap((k) => [
    { key: k, cells: [<><b className={`v${k}`}>{k}</b> after costs</>, ...V[k].splits.map((x) => <b className={cls(x.net)}>{pct(x.net)}</b>)] },
    { key: `${k}b`, cls: 'dim', cells: ['before · costs · stressed', ...V[k].splits.map((x) => <small>{pct(x.gross)} · {pct(-x.costs)} · {pct(x.stress)}</small>)] }])
    .concat([{ key: 'nifty', cells: ['NIFTY 50', ...splits.map((x) => <span className="muted">{pct(x.nifty)}</span>)] }]);
  const yearCols = [{ h: 'Year' }, ...keys.map((k) => ({ h: k, key: k, w: 100, n: true })), { h: 'NIFTY 50', w: 100, n: true }];
  const yearRows = V[keys[0]].years.map((y, i) => ({ key: y.year, cells: [y.year, ...keys.map((k) => <span className={cls(V[k].years[i].net)}>{pct(V[k].years[i].net)}</span>), <span className={cls(y.nifty)}>{pct(y.nifty)}</span>] }));
  const robCols = [{ h: 'Changed' }, ...keys.flatMap((k) => [{ h: `${k} trades`, key: `${k}t`, w: 80, n: true, lo: true }, { h: `${k} CAGR`, key: `${k}c`, w: 90, n: true },
    { h: `${k} Dev / Val / OOS`, key: `${k}s`, w: 160, n: true, lo: true }, { h: k, key: `${k}o`, w: 50 }])];
  const robRows = V[keys[0]].robust.map((x, i) => ({ key: x.label, cells: [x.label, ...keys.flatMap((k) => { const y = V[k].robust[i]; return [y.trades,
    <span className={cls(y.cagr)}>{pct(y.cagr)}</span>, y.periods.map((v) => pct(v, 0)).join(' / '), y.ok ? '✅' : '❌']; })] }));
  const scanCols = [{ h: 'Breakout (next open)' }, { h: '#', w: 40, n: true, lo: true }, { h: 'For', w: 110 }, { h: 'Close', w: 100, n: true }, { h: 'Stop', w: 130, n: true }, { h: 'Qty · value', w: 130, n: true }, { h: '₹ at risk', w: 100, n: true }];
  const scanRows = (sc?.buys || []).map((b, k) => ({ key: b.symbol, cells: [<><b>{b.symbol}</b> <small>6M {pct(b.ret126, 0)}</small></>, k + 1,
    b.for ? <Tag c={b.for.length > 1 ? 'ok' : 'a'}>{b.for.length > 1 ? 'A+E' : `${b.for[0]} only`}{b.turnover_rank ? ` · #${b.turnover_rank}` : ''}</Tag> : '',
    rs(b.close), <>{rs(b.stop)} <small>{b.stop_pct.toFixed(1)}%</small></>, `${b.qty} · ${inr(b.value)}`, <span className="down">{inr(b.risk)}</span>] }));
  const trCols = [{ h: 'Stock' }, { h: 'Entry', w: 150 }, { h: 'Exit', w: 150 }, { h: 'Days', w: 60, n: true, lo: true }, { h: 'Before costs', w: 115, n: true, lo: true },
    { h: 'Costs', w: 90, n: true, lo: true }, { h: 'After costs', w: 110, n: true }, { h: 'Exit reason', w: 150 }];
  const trRows = trades.rows.map((t) => ({ key: `${t.sym}${t.in}`, cells: [<><b>{t.sym}</b> <small>{t.qty} sh</small></>, <>{day(t.in)} <small>{rs(t.buy)}</small></>, <>{day(t.out)} <small>{rs(t.sell)}</small></>,
    t.days, <span className={cls(t.gross)}>{inr(t.gross)}</span>, <span className="muted">{inr(t.costs)}</span>, <b className={cls(t.net)}>{inr(t.net)}</b>, EXIT[t.why] || t.why] }));
  const share = (k, n) => (S(k).trades ? (100 * n) / S(k).trades : 0);

  return (
    <div className={`rd ${loading ? 'busy' : ''}`}>
      <AttentionStrip tab="breakout" />
      <Head title="📈 Trend Breakout · A vs E" right={runBtn}>
        <Seg opts={SCEN} v={scen} set={setScen} small />
        <Chip>{scen === 'stress' ? R.stress_slippage_pct : R.slippage_pct}% a fill</Chip>
        <Chip title={`${day(r.from)} → ${day(r.to)} · ${r.sessions} sessions · ${r.stocks} stocks`}>{day(r.from)} → {day(r.to)} · <b>{r.sessions}</b> sessions · <b>{r.stocks}</b> stocks</Chip>
        <Chip>updated <b>{stamp(r.updated_at)}</b></Chip>
      </Head>
      {err && <div className="rbanner warn">{err}</div>}
      {V.E?.updated_note && <div className="rbanner">ℹ️ E+ · {V.E.updated_note}.</div>}
      {r.stale?.length > 0 && <div className="rbanner warn">⚠️ {r.stale.join(' and ')}'s rules changed since this run (universe or late trail): its numbers are out of date.
        {' '}{running ? 'Re-running…' : <button type="button" className="btn primary small" onClick={run}>Re-run now</button>} (it also re-runs tonight).</div>}
      {r.missing?.length > 0 && <div className="rbanner">⚠️ {r.missing.length} Nifty 500 stock(s) without enough daily candles (left out, see the log):
        {' '}{r.missing.slice(0, 20).join(', ')}{r.missing.length > 20 ? ' …' : ''}</div>}
      <div className="rbanner"><b>{keys.map((k) => `${V[k].verdict ? '✅' : '❌'} ${k}`).join(' · ')}</b>
        <span className="muted"> · ✅ = profitable after costs in all three periods (stressed too) and beats NIFTY 50</span>
        <div className="muted small">⚠️ Universe is today's Nifty 500 list (survivorship bias): real results will be somewhat lower.
          {' '}E's out-of-sample gain comes mostly from 2026. Past results don't guarantee future returns.</div></div>
      {V.E && <Cards n={5}>
        <Card unit="%" label={<>CAGR · A<Tip text="Yearly growth of the account after all charges and slippage, over the whole test." /></>} value={pct(V.A?.[scen].cagr)} num={V.A?.[scen].cagr} k={cls(V.A?.[scen].cagr)}
          sub={`${lakh(R.capital)} → ${lakh(R.capital + (V.A?.[scen].net || 0))} · ${scenTxt}`} spark={V.A?.curve?.[scen]} fact={<><Bar pct={V.A?.[scen].drawdown} bad /> max DD {pct(-V.A?.[scen].drawdown, 0)}</>} />
        <Card unit="%" label="CAGR · E+" value={pct(E.cagr)} num={E.cagr} k={cls(E.cagr)} sub={`${lakh(R.capital)} → ${lakh(R.capital + E.net)} · ${scenTxt}`} spark={V.E.curve?.[scen]}
          fact={<><Bar pct={E.drawdown} bad /> max DD {pct(-E.drawdown, 0)}</>} />
        <Card unit="%" label="NIFTY 50" value={pct(r.nifty.cagr)} num={r.nifty.cagr} k={cls(r.nifty.cagr)} sub="same period · buy and hold" spark={r.curve?.nifty} fact={<><Bar pct={r.nifty.drawdown} bad /> max DD {pct(-r.nifty.drawdown, 0)}</>} />
        <Card unit="n" label={<>E+ trades · won<Tip text="Profit on the starting capital: the price moves (before costs) minus charges and slippage." /></>} value={`${E.trades} · ${E.win_rate.toFixed(0)}%`} num={E.trades}
          sub={<><span className={cls(E.net)}>{signedInr(E.net)}</span> net · {inr(E.gross)} − {inr(E.costs)}</>} fact={`profit factor ${E.profit_factor ?? '—'} · ${E.avg_hold.toFixed(0)} days held`} />
        <Card unit="n" label={<>E+ out-of-sample<Tip text="Each setting moved one step (14; E+ adds 4 for the late trail: from +50% / +150%, trail 20% / 30%); how many still pass every period (stressed too) and beat NIFTY 50." /></>}
          value={pct(oos?.net)} num={oos?.net} k={cls(oos?.net)} sub={`${oos?.name || 'OOS'} · stressed ${pct(oos?.stress)}`}
          fact={<><Bar pct={V.E.robust.length ? (100 * V.E.robust_pass) / V.E.robust.length : 0} ok /> robust {V.E.robust_pass} of {V.E.robust.length}</>} />
      </Cards>}
      <Seg opts={SUBS} v={sub} set={setSub} />

      {sub === 'results' && <div className="two">
        <div className="rd">
          <Panel title={`${lakh(R.capital)} on each`} right={<>{keys.map((k) => <span key={k} className={`v${k}`}><b>━ {k} {pct(S(k).cagr)}</b></span>)}<span>╍ NIFTY 50 {pct(r.nifty.cagr)} (CAGR)</span></>}>
            <div className="rbody"><Curve dates={r.curve.dates} nifty={r.curve.nifty} capital={R.capital} lines={keys.map((k) => ({ k, vals: V[k].curve[scen] }))} /></div></Panel>
          <Panel title="Results" right={`${scenTxt} slippage`}><FixedTable cols={resCols} rows={resRows} /></Panel>
          <Panel title="Development · validation · out-of-sample" right="first 50% / next 25% / last 25% of the sessions">
            <FixedTable cols={splitCols} rows={splitRows} />
            <div className="rnote">An edge must hold in all three, at stressed slippage too. {splits.map((x) => `${x.name} ${day(x.from)} – ${day(x.to)}`).join(' · ')}</div></Panel>
          <Panel title="Year by year" right="after costs"><FixedTable cols={yearCols} rows={yearRows} /></Panel>
        </div>
        <div className="rd">
          <Panel title="How trades ended" right={keys.map((k) => <b key={k} className={`v${k}`}>{k}</b>)}>
            {Object.keys(EXIT).map((e) => <div key={e} className="ract" style={ROW(keys.length)}><div className="t"><div style={ONE}>{EXIT[e]}</div></div>
              {keys.map((k) => <span key={k} className="mono" style={{ ...SLOT, display: 'inline-flex', alignItems: 'center', gap: 6, justifyContent: 'flex-end' }}>
                <Share pct={share(k, S(k).exits?.[e] ?? 0)} c={k === 'A' ? '#6aa8f0' : '#b18cf0'} /><b style={{ width: 34, flex: 'none', textAlign: 'right', overflow: 'hidden' }}>{S(k).exits?.[e] ?? 0}</b></span>)}</div>)}
            <div className="rnote">Signals not taken</div>
            {Object.keys(SKIP).map((e) => <div key={e} className="ract" style={ROW(keys.length)}><div className="t"><div className="muted" style={ONE}>⛔ {SKIP[e]}</div></div>
              {keys.map((k) => <span key={k} className="mono" style={{ ...SLOT, display: 'inline-block', textAlign: 'right' }}>{S(k).skipped?.[e] ?? 0}</span>)}</div>)}
          </Panel>
          <div style={{ width: 0, minWidth: '100%' }}><NextUp /></div>{/* its own width never sizes the column */}
        </div>
      </div>}

      {sub === 'robust' && <Panel title="Robustness" right={<>one setting changed at a time · {keys.map((k) => <span key={k} className={`v${k}`}>{k}: {V[k].robust_pass} of {V[k].robust.length} pass</span>)}</>}>
        <div className="rnote">✅ = profitable after costs in all three periods, stressed too, and beats NIFTY 50. Under 75% passing means a modest,
          lumpy edge: expect real results to vary.</div>
        <FixedTable cols={robCols} rows={robRows} /></Panel>}

      {sub === 'scan' && <Panel title="Tonight's scan" meta={sc?.buys?.length ?? 0} right={<>signals only, no orders{sc ? ` · ${day(sc.as_of)} · ${stamp(sc.computed_at)}` : ''}</>}>
        {!sc ? <p className="muted small rempty">Runs after the evening sync (and with Re-run).</p> : <>
          <div className="rbody small">Market filter: {Object.entries(sc.filters || {}).map(([k, f]) => <FilterBadge key={k} k={k} f={f} />)}
            <span className="muted"> · NIFTY 50 {sc.nifty?.toLocaleString('en-IN')} · 50-day {sc.nifty_sma50?.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
              {' '}· 200-day {sc.nifty_sma200?.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span></div>
          <FixedTable cols={scanCols} rows={scanRows} empty="No breakouts today." />
          {sc.buys?.length > 0 && <div className="rnote">{Object.entries(sc.filters || {}).map(([k, f]) => `${k}: ${f.state === 'OFF' ? 'not bought (filter off)'
            : f.state === 'HALF' ? `buy only while under ${f.slots} positions` : 'buy at the next open'}`).join(' · ')}</div>}
        </>}
      </Panel>}

      {sub === 'trades' && <Panel title="Trades" meta={trades.total} right={<><span>newest first · {scenTxt} slippage</span>
        <Seg small opts={[['A', `A · 200-day filter, top ${R.top}`], ['E', `E+ · half filter, top ${R.top_e}, late trail`]]} v={tv} set={setTv} /></>}>
        <FixedTable cols={trCols} rows={trRows} empty="No trades." />
        {trades.total > trades.rows.length && (
          <div className="rnote"><button type="button" className="btn ghost small" onClick={more}>Show {Math.min(PAGE, trades.total - trades.rows.length)} more</button>
            <span className="muted small"> &nbsp;{trades.rows.length} of {trades.total}</span></div>)}
      </Panel>}
      {sub === 'how' && <Panel><HowItWorks r={R} open /></Panel>}
    </div>
  );
}
