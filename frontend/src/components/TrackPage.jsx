import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import StockChart from './StockChart';
import { ampm, stamp } from '../timefmt';
import { useIsPhone } from '../useMedia';

const PERIODS = [['1w', '1W'], ['1m', '1M'], ['3m', '3M'], ['6m', '6M'], ['1y', '1Y'], ['2y', '2Y']];
const PATTERNS = [['all', 'All patterns'], ['vcp', 'VCP'], ['cup', 'Cup'], ['flag', 'Flag'], ['bases', 'Bases']];
const TIERS = [['all', 'All tiers'], ['A', 'Tier A'], ['B', 'Tier B']];
const RULES = [['swing', 'Swing · 8% stop'], ['i5m', 'Intraday · 5-min trail'], ['iday', 'Intraday · daily trail'],
  ['iday_s50', 'Daily trail · stop ≥ 0.5%'], ['iday_s75', 'Daily trail · stop ≥ 0.75%']];
const STATUSES = [['all', 'All'], ['running', 'Running'], ['closed', 'Closed'], ['no_entry', 'No entry']];
const STOCKS_SHOWN = 60;
const PNAME = { vcp: 'VCP', cup: 'Cup', flag: 'Flag', bases: 'Bases' };
const MKT = { uptrend: '🟢', pressure: '🟡', correction: '🔴' };
const SNAPS = [['1w', '1W'], ['2w', '2W'], ['1m', '1M'], ['3m', '3M']];

const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const rr = (v) => (v == null ? '' : `${v > 0 ? '+' : ''}${v.toFixed(2)}R`);
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down');
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const lakh = (v) => (v == null ? '—' : `₹${(v / 1e5).toFixed(2).replace(/\.00$/, '')}L`);

function Chips({ items, value, onChange }) {
  return items.map(([id, label]) => (
    <button key={id} className={`spill ${value === id ? 'on' : ''}`} aria-pressed={value === id}
            onClick={() => onChange(id)}>{label}</button>));
}

// Account value (%) under the swing rules vs Nifty 500 buy and hold.
function Curve({ p }) {
  if (!p?.dates?.length) return <p className="muted">No trades in this period yet.</p>;
  const W = 560, H = 190, L = 40, T = 14, B = 24;
  const all = [...p.strategy, ...(p.nifty || []), 0];
  let lo = Math.min(...all), hi = Math.max(...all);
  if (hi - lo < 4) { hi += 2; lo -= 2; }
  const n = p.dates.length;
  const x = (i) => L + ((W - L - 8) * i) / Math.max(1, n - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const line = (vals) => vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const step = (hi - lo) > 60 ? 20 : (hi - lo) > 25 ? 10 : 5;
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v);
  const months = [];
  p.dates.forEach((d, i) => { if (i === 0 || d.slice(5, 7) !== p.dates[i - 1].slice(5, 7)) months.push([i, d]); });
  const every = Math.ceil(months.length / 8);
  const sl = p.strategy[n - 1], nl = p.nifty?.length ? p.nifty[p.nifty.length - 1] : null;  // end dots
  return (
    <svg className="trcurve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Account value vs Nifty 500">
      {ticks.map((v) => <g key={v}><line x1={L} x2={W - 8} y1={y(v)} y2={y(v)} className={v === 0 ? 'zero' : 'grid'} />
        <text x={L - 6} y={y(v) + 3} textAnchor="end">{v > 0 ? '+' : ''}{v}%</text></g>)}
      {months.filter((_, k) => k % every === 0).map(([i, d]) => (
        <text key={d} x={x(i)} y={H - 6} textAnchor="middle">{day(d).split(' ')[1]}</text>))}
      <polygon points={`${x(0)},${y(0)} ${line(p.strategy)} ${x(n - 1)},${y(0)}`} className="area" />
      {p.nifty?.length > 1 && <polyline points={line(p.nifty)} className="nifty" />}
      <polyline points={line(p.strategy)} className="strat" />
      <circle cx={x(n - 1)} cy={y(sl)} r="3.5" className="dot" />
      {nl != null && <circle cx={x(n - 1)} cy={y(nl)} r="3" className="ndot" />}
    </svg>
  );
}

// Closes from just before the entry, the stop dashed, dots at 🎯 half / exit.
function Spark({ t }) {
  const s = t.spark;
  if (!s || s.c.length < 2) return <span className="trspark" />;
  const vals = t.stop ? [...s.c, t.stop] : s.c;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const w = 110, h = 28;
  const y = (v) => h - 2 - (h - 4) * ((v - lo) / ((hi - lo) || 1));
  const x = (i) => (i * w) / (s.c.length - 1);
  const at = (d) => s.d.indexOf(d);
  const up = (t.result_pct ?? 0) >= 0;
  const dots = (t.events || []).filter((e) => e.kind === 'half' || e.kind === 'exit').map((e) => [at(e.date), e.kind])
    .filter(([i]) => i >= 0);
  return (
    <svg className={`trspark ${up ? 'up' : 'down'}`} viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      {t.entry && <line x1="0" x2={w} y1={y(t.entry)} y2={y(t.entry)} className="entry" />}
      {t.stop && <line x1="0" x2={w} y1={y(t.stop)} y2={y(t.stop)} className="stop" />}
      <polyline points={s.c.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')} />
      {dots.map(([i, k]) => <circle key={`${k}${i}`} cx={x(i)} cy={y(s.c[i])} r="2.6" className={k} />)}
    </svg>
  );
}

const STATUS = {
  running: ['🟢 running', 'run'], pending: ['⏳ entry next open', 'none'], won: ['✅ closed', 'won'],
  lost: ['🛑 closed', 'lost'], no_entry: ['⛔ no entry', 'none'],
};
const at = (e) => (e?.time ? `, ${ampm(e.time)}` : '');           // ", 10:35 AM" on intraday events

function statusChip(t) {
  if (t.status === 'pending' && t.events?.length === 0 && t.reason?.startsWith('5-minute')) {
    return <span className="st none">⏳ waiting for 5-min candles</span>;
  }
  if (t.status === 'running') {
    const half = (t.events || []).some((e) => e.kind === 'half');
    return <span className="st run">{half ? '🎯 ½ booked' : '🟢 running'} · day {t.days}</span>;
  }
  const [label, c] = STATUS[t.status] || [t.status, 'none'];
  return <span className={`st ${c}`}>{label}{t.days && t.status !== 'no_entry' ? ` · day ${t.days}` : ''}</span>;
}

// "🔒 breakeven 24 Jun → 🎯 ½ booked +20% (8 Jul) → 📉 ½ closed below 50-day +22.8% (14 Aug)" or the next step.
function timeline(t, rules, set) {
  const intra = set && set !== 'swing';
  if (t.status === 'no_entry') {
    if (intra) return `⛔ ${t.reason}`;
    const raw = (t.events || []).find((e) => e.kind === 'raw');
    return `${t.reason}: not chased.${raw?.pct != null ? ` Raw run since: ${pct(raw.pct)}` : ''}`;
  }
  if (t.status === 'pending') {
    return intra ? `Crossed the pivot ${day(t.breakout_date)} · ${t.reason}` : `Breakout ${day(t.breakout_date)} · entry at the next open`;
  }
  const parts = [];
  (t.events || []).forEach((e) => {
    if (e.kind === 'breakeven') parts.push(`🔒 breakeven ${day(e.date)}`);
    if (e.kind === 'half') parts.push(`🎯 ${intra ? `${Math.round((rules?.book_frac ?? 0.5) * 100)}%` : '½'} booked at ${rs(e.price)} (${day(e.date)}${at(e)})`);
    if (e.kind === 'exit') {
      const r = (e.price / t.entry - 1) * 100;
      const what = e.why === '3M mark' ? '⏱ 3-month mark' : e.why?.startsWith('close under') || e.why?.startsWith('ATR trail')
        ? `📉 ${e.why}` : `🛑 ${e.why}`;
      parts.push(`${what} ${pct(r)} (${day(e.date)}${at(e)})`);
    }
  });
  if (intra && t.status === 'running') {
    const half = (t.events || []).some((e) => e.kind === 'half');
    parts.push(half ? `now: stop at entry ${rs(t.entry)}, the rest on the ${set === 'i5m' ? '5-minute' : 'daily'} ATR trail`
      : `stop ${rs(t.stop)} · next: 🎯 book at +${rules?.target_pct ?? 2}% (${rs(t.entry * (1 + (rules?.target_pct ?? 2) / 100))})`);
  } else if (t.status === 'running') {
    const kinds = (t.events || []).map((e) => e.kind);
    if (kinds.includes('half')) parts.push(`now trailing: exit on a close below the ${rules?.exit_ma ?? 50}-day`);
    else if (kinds.includes('breakeven')) parts.push(`stop at entry ${rs(t.entry)} · next: 🎯 ½ at +${rules?.book_half_pct ?? 20}% (${rs(t.entry * (1 + (rules?.book_half_pct ?? 20) / 100))})`);
    else parts.push(`stop ${rs(t.stop)} · next: 🔒 breakeven at +${rules?.breakeven_pct ?? 10}% (${rs(t.entry * (1 + (rules?.breakeven_pct ?? 10) / 100))})`);
  }
  return parts.length ? parts.join(' → ') : intra ? `Entered ${at(t.events?.[0]).slice(2)}` : 'Entered at the open';
}

// Intraday: the crosses that didn't trigger an entry, folded into one line per stock.
function noEntryLine(ts) {
  const why = {};
  ts.forEach((t) => { const k = (t.reason || '').replace('crossed the pivot, but ', ''); why[k] = (why[k] || 0) + 1; });
  const parts = Object.entries(why).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${n} × ${k}`);
  return `⛔ ${ts.length} cross${ts.length === 1 ? '' : 'es'} without an entry: ${parts.join(' · ')}`;
}

function groupSummary(ts) {
  const closed = ts.filter((t) => t.status === 'won' || t.status === 'lost');
  const running = ts.filter((t) => t.status === 'running').length;
  const entered = ts.filter((t) => ['won', 'lost', 'running'].includes(t.status)).length;
  const r = closed.reduce((a, t) => a + (t.r || 0), 0);
  const bits = entered ? [`${entered} trade${entered === 1 ? '' : 's'}`] : [];
  const pending = ts.filter((t) => t.status === 'pending').length;
  if (pending) bits.push('⏳ entry at the next open');
  if (closed.length) bits.push(`${r >= 0 ? '+' : ''}${r.toFixed(1)}R closed`);
  if (running) bits.push(`${running} running`);
  const noEntry = ts.filter((t) => t.status === 'no_entry').length;
  if (noEntry) bits.push(`${noEntry} no entry`);
  return bits.join(' · ');
}

// The 📈 Track record tab: every breakout of the last year under the swing rules, and where they'd have got you.
export default function TrackPage() {
  const phone = useIsPhone();
  const [f, setF] = useState({ period: '1y', pattern: 'all', tier: 'all', status: 'all', rules: 'swing' });
  const [d, setD] = useState(null);
  const [trades, setTrades] = useState(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);       // new filters: the old numbers stay, dimmed
  const [limit, setLimit] = useState(STOCKS_SHOWN);     // stocks listed (a 2-year list is thousands of rows)
  const [chart, setChartState] = useState(null);      // {symbol, trade}: opens above the filters
  const setChart = (c) => { setChartState(c); if (c) window.scrollTo({ top: 0, behavior: 'smooth' }); };
  const set = (k) => (v) => setF({ ...f, [k]: v });

  useEffect(() => {
    let alive = true, timer;
    setLoading(true);
    setLimit(STOCKS_SHOWN);
    const load = () => Promise.all([api.getTrack(f), api.getTrackTrades(f)])
      .then(([s, t]) => {
        if (!alive) return;
        setD(s); setTrades(t.stocks); setErr(''); setLoading(false);
        if ((!s.built && s.rules_set === 'swing') || s.progress?.state !== 'idle') timer = setTimeout(load, 3000);   // building: refresh
      })
      .catch((e) => { if (alive) { setErr(e.message); setLoading(false); } });
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, [f]);

  const started = () => setD({ ...d, progress: { state: 'building', done: 0, total: 0 } });
  const rebuild = () => (intra ? api.buildIntradayTrack(true) : api.rebuildTrack()).then(started)
    .catch((e) => setErr(e.message));
  const runIntraday = () => api.buildIntradayTrack().then(started).then(() => setF({ ...f }))
    .catch((e) => setErr(e.message));

  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;
  const prog = d.progress || {};
  const intra = d.rules_set ? d.rules_set !== 'swing' : f.rules !== 'swing';
  const ruleChips = (
    <div className="trfilters trrules"><span className="muted small">Rules</span>
      <Chips items={RULES} value={f.rules} onChange={set('rules')} /></div>);
  if (!d.built && intra && !loading) {
    const running = prog.state && prog.state !== 'idle';
    return (
      <div className="track">{ruleChips}
        <div className="panel"><h3>⚡ Intraday entry backtest</h3>
          {d.stale && <p className="warn small">Re-run needed: {d.stale}. The 5-minute candles already fetched are re-used;
            only the missing days are fetched.</p>}
          <p>The same watchlist, bought on the breakout day from 5-minute candles: entry from 9:30 AM to 1:30 PM on a
            5-minute close over the pivot (over VWAP, volume pace 1.5×+, at most 3% over the pivot), a stop of at most 1.5%,
            {' '}50% booked at +2%, the rest on an ATR trail (5-minute and daily, side by side with the swing rules).</p>
          <p className="muted small">It also counts the days a stock crossed its pivot but closed back under it. The 5-minute
            candles are fetched from Kite for those days only: about 3,000 requests (15–20 minutes) for each year of history,
            once; candles already fetched are re-used. After that the evening job adds each new day.</p>
          {running ? <p><b>Running…</b> {prog.total ? `${prog.done} of ${prog.total} stocks` : ''} <span className="muted small">{prog.message}</span></p>
            : <><button className="btn primary" disabled={!d.kite} onClick={runIntraday}>{d.stale ? 'Re-run the intraday backtest' : 'Run the intraday backtest'}</button>
              {!d.kite && <span className="muted small"> &nbsp;Log in to Kite first (it needs 5-minute candles).</span>}
              {prog.message && <p className="muted small">{prog.message}</p>}</>}
          {err && <div className="warn small">{err}</div>}</div></div>);
  }
  if (!d.built) {
    return (
      <div className="panel"><h3>📈 Track record</h3>
        <p>Building the track record from your stored candles{prog.total ? `: ${prog.done} of ${prog.total} stocks` : '…'}</p>
        <p className="muted small">It replays the last year of breakouts once (about a minute); after that the evening
          job only adds new ones. {prog.message}</p></div>);
  }
  const c = d.counts || {};
  const p = d.portfolio || {};
  const cmp = p.compare || {};
  const sv = cmp.strategy || {}, nv = cmp.nifty || {};
  const set$ = p.settings || {};
  const fold = intra && f.status !== 'no_entry';       // fold the no-entry crosses (pick "No entry" to list them)
  const shown = (ts) => ts.filter((t) => t.status !== 'in_trade' && !(fold && t.status === 'no_entry'));
  const folded = (ts) => (fold ? ts.filter((t) => t.status === 'no_entry') : []);
  const days = (n) => `${Math.round(n)} day${Math.round(n) === 1 ? '' : 's'}`;

  return (
    <div className={`track ${loading ? 'busy' : ''}`}>
      {chart && <StockChart symbol={chart.symbol} trade={chart.trade} onClose={() => setChart(null)} />}
      {ruleChips}
      <div className="trfilters">
        <span className="muted small">Period</span><Chips items={PERIODS} value={f.period} onChange={set('period')} />
        <span className="sep" /><Chips items={PATTERNS} value={f.pattern} onChange={set('pattern')} />
        <span className="sep" /><Chips items={TIERS} value={f.tier} onChange={set('tier')} />
        <span className="sep" /><Chips items={STATUSES} value={f.status} onChange={set('status')} />
      </div>
      {err && <div className="warn small">{err}</div>}

      <div className="trstatus">
        <span className="s"><b>{c.breakouts ?? 0}</b> {intra ? 'pivot crosses' : 'breakouts'}</span><span className="ar">→</span>
        <span className="s"><b>{c.entered ?? 0}</b> entered</span>
        <span className="s"><b>{c.no_entry ?? 0}</b> ⛔ no entry{intra ? '' : ' (gapped)'}</span><span className="ar">→</span>
        <span className="s"><b>{c.closed ?? 0}</b> closed: <span className="up">{c.won ?? 0} won</span> · <span className="down">{c.lost ?? 0} lost</span></span>
        <span className="s"><b className="up">{c.running ?? 0}</b> 🟢 running</span>
        {c.pending > 0 && <span className="s"><b>{c.pending}</b> ⏳ {intra ? 'waiting for 5-min candles' : 'entry next open'}</span>}
        <span className="muted small trmeta">Data to {day(d.as_of)} · updated {stamp(d.updated_at)}
          {prog.state !== 'idle' ? ` · ${prog.state}…` : <> · <button className="linkbtn" onClick={rebuild}>Rebuild</button></>}</span>
      </div>

      <div className="trkpis">
        <div className="k"><div className="v up">{d.win_rate != null ? `${d.win_rate.toFixed(0)}%` : '—'}</div><div className="l">win rate (closed trades)</div></div>
        <div className="k"><div className="v up">{pct(d.avg_win)}</div><div className="l">avg winner · <span className="down">{pct(d.avg_loss)}</span> avg loser</div></div>
        <div className="k"><div className={`v ${cls(d.expectancy_r)}`}>{rr(d.expectancy_r) || '—'}</div><div className="l">expectancy per trade{d.net_avg_pct != null ? <> · <span className={cls(d.net_avg_pct)}>{pct(d.net_avg_pct, 2)}</span> a trade after costs</> : ''}</div></div>
        <div className="k"><div className="v sm">{SNAPS.map(([k]) => pct(d.snaps?.[k])).join(' · ')}</div><div className="l">avg 1W · 2W · 1M · 3M run</div></div>
        <div className="k"><div className="v">{d.hold_days?.all != null ? days(d.hold_days.all) : '—'}</div>
          <div className="l">avg holding{d.hold_days?.won != null ? ` (winners ${Math.round(d.hold_days.won)} · losers ${Math.round(d.hold_days.lost ?? 0)})` : ''}</div></div>
        <div className="k"><div className={`v ${d.profit_factor >= 1 ? 'up' : 'down'}`}>{d.profit_factor ?? '—'}</div><div className="l">profit factor (won R / lost R)</div></div>
      </div>

      {d.rule_sets?.length > 1 && (
        <div className="panel"><h3>Rule sets compared <span className="muted small">· the same filters · tap a row to switch</span></h3>
          <div className="tablewrap"><table className="grid trsize trrs"><thead><tr><th>Rules</th>
            {!phone && <><th className="num">Trades</th><th className="num">Avg win · loss</th><th className="num">Profit factor</th></>}
            <th className="num">Win</th>{!phone && <th className="num">Expectancy</th>}<th className="num">{phone ? 'Net / trade' : 'Per trade, after costs'}</th><th className="num">Return</th>
            {!phone && <th className="num">Worst drawdown</th>}<th className="num">{phone ? 'Ret ÷ fall' : 'Return ÷ drawdown'}</th></tr></thead>
            <tbody>{d.rule_sets.map((x) => (
              <tr key={x.rules} className={x.rules === f.rules ? 'cur' : ''} onClick={() => set('rules')(x.rules)}>
                <td>{x.name}{x.rules === f.rules && <span className="trnow">shown</span>}</td>
                {!phone && <><td className="num">{x.closed}</td>
                  <td className="num"><span className="up">{pct(x.avg_win)}</span> · <span className="down">{pct(x.avg_loss)}</span></td>
                  <td className="num">{x.profit_factor ?? '—'}</td></>}
                <td className="num">{x.win_rate != null ? `${x.win_rate.toFixed(0)}%` : '—'}</td>
                {!phone && <td className="num"><b className={cls(x.expectancy_r)}>{rr(x.expectancy_r) || '—'}</b></td>}
                <td className={`num ${cls(x.net_avg_pct)}`}>{pct(x.net_avg_pct, 2)}</td>
                <td className={`num ${cls(x.return)}`}><b>{pct(x.return)}</b></td>
                {!phone && <td className="num down">{pct(x.drawdown)}</td>}
                <td className="num">{x.ratio != null ? x.ratio.toFixed(2) : '—'}</td></tr>))}
            </tbody></table></div>
          <div className="muted small">Trades = closed trades. Per trade, return and drawdown are after costs: {d.costs?.cost_pct}% a round trip
            (STT, stamp duty, exchange, GST, DP) plus {d.costs?.slippage_pct}% slippage on every fill. Return and drawdown use the portfolio
            rules below (₹10L, 1% risk, max 20% per position{set$.max_new_per_day ? `, at most ${set$.max_new_per_day} new entries a day` : ''}).
            Expectancy (R) is before costs.</div></div>)}

      <div className="trcmp">
        <div className="panel"><h3>If you followed {intra ? `the ${d.name} rules` : 'these swing rules'} vs Nifty 500</h3>
          <div className="muted small">{lakh(set$.capital)} start · {((set$.risk_pct ?? 0.01) * 100).toFixed(1)}% risk per trade, sized by the stop{set$.position_cap ? `, max ${Math.round(set$.position_cap * 100)}% per position` : ''} · max {set$.max_positions} open positions · after costs</div>
          <Curve p={p} />
          <div className="muted small"><span className="up"><b>━ {intra ? d.name : 'Swing rules'} {pct(sv.return)}</b></span> &nbsp; ╍ Nifty 500 {pct(nv.return)} (buy and hold)
            {p.skipped ? ` · ${p.skipped} signals skipped (positions full or no cash)` : ''}</div></div>
        <div className="panel"><h3>Where you would be ({PERIODS.find(([k]) => k === d.period)?.[1]})</h3>
          <table className="trvs"><thead><tr><th /><th>{intra ? 'Intraday rules' : 'Swing rules'}</th><th>Nifty 500</th></tr></thead><tbody>
            <tr><td>Return</td><td className={cls(sv.return)}><b>{pct(sv.return)}</b></td><td className={cls(nv.return)}><b>{pct(nv.return)}</b></td></tr>
            <tr><td>{lakh(set$.capital)} became</td><td><b>{lakh(sv.value)}</b></td><td><b>{lakh(nv.value)}</b></td></tr>
            <tr><td>Worst drawdown</td><td className="down">{pct(sv.drawdown)}</td><td className="down">{pct(nv.drawdown)}</td></tr>
            <tr><td>Months up</td><td>{sv.months_up?.[1] ? `${sv.months_up[0]} of ${sv.months_up[1]}` : '—'}</td>
              <td>{nv.months_up?.[1] ? `${nv.months_up[0]} of ${nv.months_up[1]}` : '—'}</td></tr>
            <tr><td>Avg cash idle</td><td>{sv.cash_idle != null ? `${sv.cash_idle.toFixed(0)}%` : '—'}</td><td>0%</td></tr>
          </tbody></table>
          <div className="trsplit small">
            {(d.by_market || []).map((m) => <span key={m.market}>{MKT[m.market]} {m.market} <b className={cls(m.expectancy_r)}>{rr(m.expectancy_r) || '—'}</b></span>)}
          </div></div>
      </div>

      {p.sizing?.length > 0 && (
        <div className="panel"><h3>Sizing comparison <span className="muted small">· the same trades, sized four ways</span></h3>
          <div className="tablewrap"><table className="grid trsize"><thead><tr><th>Sizing</th><th className="num">Return</th>
            {phone ? <><th className="num">Worst fall</th><th className="num">Ret ÷ fall</th><th className="num">Skip</th></>
              : <><th className="num">Worst drawdown</th><th className="num">Return ÷ drawdown</th><th className="num">Signals skipped</th></>}</tr></thead>
            <tbody>{p.sizing.map((x) => (
              <tr key={x.label} className={x.current ? 'cur' : ''}><td>{x.label}{x.current && <span className="trnow">{phone ? 'used' : 'used above'}</span>}</td>
                <td className={`num ${cls(x.return)}`}><b>{pct(x.return)}</b></td><td className="num down">{pct(x.drawdown)}</td>
                <td className="num">{x.ratio != null ? x.ratio.toFixed(2) : '—'}</td><td className="num">{x.skipped}</td></tr>))}
            </tbody></table></div>
          <div className="muted small">Return ÷ drawdown: return earned per 1% of worst fall; higher is smoother. A cap stops a tight stop from putting too much of the account in one stock.</div></div>)}

      <div className="panel"><h3>By pattern</h3>
        <div className="tablewrap"><table className="grid trpat"><thead><tr><th>Pattern</th><th className="num">Signals</th>
          <th className="num">Win</th>{!phone && SNAPS.map(([k, l]) => <th key={k} className="num">Avg {l}</th>)}
          <th className="num">Stopped</th><th className="num">Expectancy</th></tr></thead>
          <tbody>{(d.by_pattern || []).map((x) => (
            <tr key={x.pattern}><td><span className={`pt p-${x.pattern}`}>{PNAME[x.pattern]}</span></td>
              <td className="num">{x.signals}</td><td className="num">{x.win_rate != null ? `${x.win_rate.toFixed(0)}%` : '—'}</td>
              {!phone && SNAPS.map(([k]) => <td key={k} className={`num ${cls(x.snaps?.[k])}`}>{pct(x.snaps?.[k])}</td>)}
              <td className="num">{x.stopped_pct != null ? `${x.stopped_pct.toFixed(0)}%` : '—'}</td>
              <td className="num"><b className={cls(x.expectancy_r)}>{rr(x.expectancy_r) || '—'}</b>
                {x.expectancy_r > 0 && <span className="trbar" style={{ width: `${Math.min(80, x.expectancy_r * 80)}px` }} />}</td></tr>))}
          </tbody></table></div></div>

      <div className="panel"><h3>Trades by stock <span className="muted small">· newest first · tap a trade for its chart</span></h3>
        {!trades?.length && <p className="muted">No breakouts for these filters.</p>}
        {phone ? (trades || []).slice(0, limit).map((g) => (
          <div key={g.symbol} className="trgroup">
            <div className="trghead"><b>{g.symbol}</b> <span className="muted small">· {groupSummary(g.trades)}</span></div>
            {folded(g.trades).length > 0 && <div className="trfold muted small">{noEntryLine(folded(g.trades))}</div>}
            {shown(g.trades).map((t) => (
              <button key={t.breakout_date} className="trcard" onClick={() => setChart({ symbol: g.symbol, trade: t })}>
                <span className="trc1"><span className="muted small">{day(t.breakout_date)}{t.trade_no ? ` #${t.trade_no}` : ''}</span>
                  <span className={`pt p-${t.pattern}`}>{PNAME[t.pattern]}</span><span className={`tier t-${t.tier}`}>{t.tier}</span>
                  <span>{MKT[t.market] || ''}</span><span className="right">{statusChip(t)}</span></span>
                {t.entry != null && <span className="trc2"><span className="small">entry {rs(t.entry)}{intra && t.events?.[0]?.time ? ` (${ampm(t.events[0].time)})` : ''} · stop {rs(t.stop)}</span><Spark t={t} />
                  <b className={cls(t.result_pct)}>{t.status === 'no_entry' ? '' : `${pct(t.result_pct)} ${rr(t.r)}`}</b></span>}
                <span className="trc3 muted small">{timeline(t, d.rules, f.rules)}</span>
              </button>))}
          </div>))
          : (
          <div className="tablewrap"><table className="grid trtrades"><thead><tr><th>{intra ? 'Crossed' : 'Breakout'}</th><th>Pattern · tier · mkt</th>
            <th className="num">{intra ? 'Entry (5-min close)' : 'Entry (next open)'}</th><th className="num">Stop</th><th>Run</th>
            {SNAPS.map(([k, l]) => <th key={k} className="num">{l}</th>)}<th className="num">Result</th><th>Status</th></tr></thead>
            <tbody>{(trades || []).slice(0, limit).map((g) => (
              <React.Fragment key={g.symbol}>
                <tr className="grp"><td colSpan={11}><b>{g.symbol}</b> <span className="muted">· {groupSummary(g.trades)}</span></td></tr>
                {folded(g.trades).length > 0 && <tr className="tl"><td colSpan={11}>{noEntryLine(folded(g.trades))}</td></tr>}
                {g.trades.filter((t) => !(fold && t.status === 'no_entry')).map((t) => (t.status === 'in_trade' ? (
                  <tr key={t.breakout_date} className="tl"><td colSpan={11}>↳ {day(t.breakout_date)}: {intra ? "crossed the pivot again" : "re-breakout"} ({PNAME[t.pattern]}) while the trade was open, not counted twice</td></tr>
                ) : (
                  <React.Fragment key={t.breakout_date}>
                    <tr className="trrow" onClick={() => setChart({ symbol: g.symbol, trade: t })}>
                      <td className="muted">{day(t.breakout_date)} {t.trade_no ? <b>#{t.trade_no}</b> : ''}</td>
                      <td><span className={`pt p-${t.pattern}`}>{PNAME[t.pattern]}</span> <span className={`tier t-${t.tier}`}>{t.tier}</span> {MKT[t.market] || ''}</td>
                      <td className="num">{t.status === 'no_entry' || !t.entry ? '—' : rs(t.entry)}
                        {intra && t.events?.[0]?.time && <div className="muted small">{ampm(t.events[0].time)}</div>}</td>
                      <td className="num">{t.stop ? <>{rs(t.stop)}<div className="muted small">{pct((t.stop / t.entry - 1) * 100)}</div></> : '—'}</td>
                      <td><Spark t={t} /></td>
                      {SNAPS.map(([k]) => <td key={k} className={`num ${cls(t.snaps?.[k])}`}>{pct(t.snaps?.[k])}</td>)}
                      <td className="num"><b className={cls(t.result_pct)}>{t.status === 'no_entry' || t.status === 'pending' ? '—' : `${pct(t.result_pct)} · ${rr(t.r)}`}</b></td>
                      <td>{statusChip(t)}</td>
                    </tr>
                    <tr className="tl"><td /><td colSpan={10}>{timeline(t, d.rules, f.rules)}</td></tr>
                  </React.Fragment>)))}
              </React.Fragment>))}
            </tbody></table></div>)}
        {trades?.length > limit && (
          <p className="trmore"><button className="btn" onClick={() => setLimit(limit + STOCKS_SHOWN)}>
            Show {Math.min(STOCKS_SHOWN, trades.length - limit)} more</button>
            <span className="muted small"> &nbsp;{limit} of {trades.length} stocks shown</span></p>)}
        {intra ? (
          <p className="muted small">The watchlist is rebuilt for each evening of the past year (only today's Nifty 500 members:
            a slight optimistic bias); a day counts when the high crossed the pivot, even if it closed back under it. Entry
            on the first 5-minute close over the pivot between {ampm(d.rules?.entry_start)} and {ampm(d.rules?.entry_end)}, over VWAP, volume
            pace {d.rules?.volume_pace}×+, at most {d.rules?.max_ext_pct}% over the pivot, no gap over {d.rules?.gap_max_pct}%, Nifty 500 over its
            open; stop = the closer of the first-{d.rules?.or_minutes}-minute low and the candle low, at most {d.rules?.max_stop_pct}% away
            {d.rules?.min_stop_pct ? ` and at least ${d.rules.min_stop_pct}% (a closer one is moved down)` : ''};
            {' '}{Math.round((d.rules?.book_frac ?? 0.5) * 100)}% booked at +{d.rules?.target_pct}%, then the stop at the entry and the rest on a close under
            the highest high − {d.rules?.atr_mult} × ATR({d.rules?.atr_len}); stop and target in one candle count as the stop.
            {' '}{prog.state !== 'idle' ? '' : <button className="linkbtn" onClick={rebuild}>Re-run from scratch</button>}</p>
        ) : (
        <p className="muted small">The past year is rebuilt from stored candles: tier and market state are recalculated for each
          date, and only today's Nifty 500 members are included (a slight optimistic bias). Rules: entry at the next open
          (no entry beyond the buy zone), pattern stop capped at {((d.rules?.max_stop ?? 0.08) * 100).toFixed(0)}%, breakeven
          at +{d.rules?.breakeven_pct}%, half at +{d.rules?.book_half_pct}%, the rest on a close below
          the {d.rules?.exit_ma}-day, {d.rules?.max_hold}-session mark.</p>)}
      </div>
    </div>
  );
}
