import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { Head, Chip, Panel, Tag, Spark } from './ui2';
import { pct, cls, signed } from '../fmt';

// 🧠 Strategies: one panel per strategy (today, backtest figures, the curve); "Open" goes to the strategy's own page
// (rules, backtest, live).
const SLOT = { display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 8, padding: '10px 14px 4px' };
const Slot = ({ l, v, k }) => <div style={{ minWidth: 0 }}><div className="muted" style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.04em' }}>{l}</div>
  <b className={`mono ${k || ''}`} style={{ display: 'block', fontSize: 16, overflow: 'hidden', textOverflow: 'ellipsis' }}>{v}</b></div>;

function StratPanel({ title, kind, chip, on, today, cagr, dd, open, curve, color, onOpen, note }) {
  return <Panel title={title} meta={kind} right={<><Tag c={on ? 'ok' : ''}>{chip}</Tag><button type="button" className="btn small" onClick={onOpen}>Open →</button></>}>
    <div style={SLOT}>
      <Slot l="Today" v={today == null ? '—' : signed(today)} k={cls(today)} />
      <Slot l="Backtest CAGR" v={cagr == null ? '—' : pct(cagr)} />
      <Slot l="Max drawdown" v={dd == null ? '—' : pct(-dd)} k="down" />
      <Slot l="Open" v={open ?? 0} />
    </div>
    <div style={{ padding: '4px 14px 0', height: 44 }}>{curve?.length > 1 ? <Spark values={curve} h={36} w={300} color={color} /> : <span className="muted small">no curve yet</span>}</div>
    <div className="rnote" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{note}</div>
  </Panel>;
}

export default function StrategiesPage({ onGo }) {
  const o = useLive('overview');
  const [bo, setBo] = useState(null);
  useEffect(() => { api.getBreakout().then(setBo).catch(() => {}); }, []);
  const V = bo?.run?.variants || {};
  const A = o?.accounts || {};
  const pos = o?.positions || [];
  const openOf = (b) => pos.filter((p) => b.includes(p.book)).length;
  const real = A.real?.variant;
  const running = V.E ? 1 : 0;
  return <div className="rd">
    <Head title="Strategies"><Chip>{running} running · {1 - running} paused</Chip></Head>
    <div className="cols c2">
      {V.E && <StratPanel title={`📈 ${V.E.name || 'Trend Breakout E+'}`} kind="swing · daily" chip={real === 'E' ? 'Real + Paper' : 'Paper'} on today={(A.paper_E?.day_pnl || 0) + (real === 'E' ? A.real?.day_pnl || 0 : 0)}
        cagr={V.E.normal?.cagr} dd={V.E.normal?.drawdown} open={openOf(real === 'E' ? ['paper_E', 'real'] : ['paper_E'])} curve={V.E.curve?.normal}
        color="var(--ok)" onOpen={() => onGo('breakout')} note={`${V.E.normal?.trades} trades · robust ${V.E.robust_pass}/${(V.E.robust || []).length}`} />}
      {!V.E && <Panel title="📈 Trend Breakout · E+" meta="swing · daily" right={<button type="button" className="btn small" onClick={() => onGo('breakout')}>Open →</button>}>
        <p className="muted small rempty">Not backtested yet: open the page and run it once.</p></Panel>}
    </div>
  </div>;
}
