import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { Spark } from './ui';
import { inr, pct, cls, signed } from '../fmt';

// 🧠 Strategies: one card per strategy (today, backtest figures, the curve) and the saved intraday setups with their
// latest backtest; "Open" goes to the strategy's own page (rules / canvas, backtest, live).
function Card({ title, kind, chip, today, cagr, dd, open, curve, color, onOpen, note }) {
  return <div className="v2card"><h3>{title} {chip && <span className="achip b">{chip}</span>}<span className="r">{kind}</span></h3>
    <div className="kpis" style={{ marginBottom: 6 }}>
      <div><div className="muted small">Today</div><b className={`mono ${cls(today)}`}>{today == null ? '—' : signed(today)}</b></div>
      <div><div className="muted small">Backtest CAGR</div><b className="mono">{cagr == null ? '—' : pct(cagr)}</b></div>
      <div><div className="muted small">Max drawdown</div><b className="mono down">{dd == null ? '—' : pct(-dd)}</b></div>
      <div><div className="muted small">Open</div><b className="mono">{open ?? 0}</b></div></div>
    <Spark values={curve} h={56} color={color} />
    <div style={{ display: 'flex', alignItems: 'center', marginTop: 8 }}><span className="muted small">{note}</span>
      <button className="btn" style={{ marginLeft: 'auto' }} onClick={onOpen}>Open →</button></div></div>;
}

export default function StrategiesPage({ onGo }) {
  const o = useLive('overview');
  const flow = useLive('flow');
  const [bo, setBo] = useState(null);
  const [bt, setBt] = useState(null);
  useEffect(() => { api.getBreakout().then(setBo).catch(() => {}); api.flowBacktest().then(setBt).catch(() => {}); }, []);
  const V = bo?.run?.variants || {};
  const A = o?.accounts || {};
  const pos = o?.positions || [];
  const openOf = (b) => pos.filter((p) => b.includes(p.book)).length;
  const results = bt?.results || [];
  const latest = (setup) => results.filter((r) => r.setup === setup).sort((a, b) => (b.ran_at || '').localeCompare(a.ran_at || ''))[0];
  const canvas = latest('On the canvas');
  const real = A.real?.variant;
  return <>
    <div className="cards2">
      {V.E && <Card title={V.E.name || 'Trend Breakout E+'} kind="Swing · daily" chip={real === 'E' ? 'Real + Paper' : 'Paper'} today={(A.paper_E?.day_pnl || 0) + (real === 'E' ? A.real?.day_pnl || 0 : 0)}
        cagr={V.E.normal?.cagr} dd={V.E.normal?.drawdown} open={openOf(real === 'E' ? ['paper_E', 'real'] : ['paper_E'])} curve={V.E.curve?.normal}
        color="var(--ok)" onOpen={() => onGo('breakout')} note={`${V.E.normal?.trades} trades · robust ${V.E.robust_pass}/${(V.E.robust || []).length}`} />}
      {V.A && <Card title={V.A.name || 'Trend Breakout A'} kind="Swing · daily" chip={real === 'A' ? 'Real + Paper' : 'Paper'} today={(A.paper_A?.day_pnl || 0) + (real === 'A' ? A.real?.day_pnl || 0 : 0)}
        cagr={V.A.normal?.cagr} dd={V.A.normal?.drawdown} open={openOf(real === 'A' ? ['paper_A', 'real'] : ['paper_A'])} curve={V.A.curve?.normal}
        color="#6c8cff" onOpen={() => onGo('breakout')} note={`${V.A.normal?.trades} trades · robust ${V.A.robust_pass}/${(V.A.robust || []).length}`} />}
      <Card title={`Intraday Flow${flow?.template ? ` · ${flow.template}` : ''}`} kind="Intraday · paper" chip={flow?.stopped ? 'stopped' : flow?.mode}
        today={A.intraday?.day_pnl} cagr={canvas?.cagr} dd={canvas?.drawdown} open={openOf(['intraday'])} curve={canvas?.curve?.net}
        color="#f2b84b" onOpen={() => onGo('flow')} note={canvas ? `backtest ${canvas.period} · ${canvas.trades} trades · net ${inr(canvas.net)}` : 'not backtested yet'} />
    </div>
    <div className="v2card"><h3>Saved intraday setups <span className="r">latest backtest of each · open the Backtest tab of Intraday Flow to run or compare</span></h3>
      {!bt ? <p className="muted">Loading…</p> : <div className="tablewrap"><table className="grid"><thead><tr><th>Setup</th><th>Period</th><th className="num">Trades</th>
        <th className="num">Plain profit</th><th className="num">Net after costs</th><th className="num">CAGR</th><th className="num">PF</th><th>Ran</th></tr></thead>
        <tbody>{Object.keys(bt.setups || {}).map((n) => { const r = latest(n); return <tr key={n} style={{ cursor: 'pointer' }} onClick={() => onGo('flow')}>
          <td><b>{n}</b>{bt.setups[n]?.preset && <span className="achip" style={{ marginLeft: 6 }}>preset</span>}</td>
          {r ? <><td>{r.period}</td><td className="num">{r.trades}</td><td className={`num ${cls(r.charges?.plain)}`}>{inr(r.charges?.plain)}</td>
            <td className={`num ${cls(r.net)}`}><b>{signed(r.net)}</b></td><td className="num">{pct(r.cagr)}</td><td className="num">{r.profit_factor ?? '—'}</td>
            <td className="muted small">{(r.ran_at || '').slice(0, 10)}</td></> : <td colSpan={7} className="muted">not run yet</td>}</tr>; })}</tbody></table></div>}
    </div>
  </>;
}
