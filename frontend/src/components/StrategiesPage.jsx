import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useLive } from '../services/live';
import { Head, Chip, Panel, Tag, Spark, FixedTable } from './ui2';
import { inr, pct, cls, signed } from '../fmt';

// 🧠 Strategies: one panel per strategy (today, backtest figures, the curve) and the saved intraday setups with their
// latest backtest; "Open" goes to the strategy's own page (rules / canvas, backtest, live).
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
  const running = (V.E ? 1 : 0) + (flow && !flow.stopped && flow.mode !== 'off' ? 1 : 0);
  const setups = Object.keys(bt?.setups || {});
  const cols = [{ h: 'Setup' }, { h: 'Period', w: 70 }, { h: 'Trades', w: 70, n: true }, { h: 'Plain profit', w: 110, n: true, lo: true }, { h: 'Net after costs', w: 140, n: true },
    { h: 'CAGR', w: 80, n: true }, { h: 'PF', w: 60, n: true, lo: true }, { h: 'Ran', w: 100, lo: true }];
  const rows = setups.map((n) => { const r = latest(n); return { key: n, onClick: () => onGo('flow'), cells: [
    <><b>{n}</b>{bt.setups[n]?.preset && <Tag>preset</Tag>}{!r && <small className="muted"> · not run yet</small>}</>,
    r ? r.period : '', r ? r.trades : '', r ? <span className={cls(r.charges?.plain)}>{inr(r.charges?.plain)}</span> : '',
    r ? <b className={cls(r.net)}>{signed(r.net)}</b> : '', r ? pct(r.cagr) : '', r ? r.profit_factor ?? '—' : '', r ? (r.ran_at || '').slice(0, 10) : ''] }; });
  return <div className="rd">
    <Head title="Strategies"><Chip>{running} running · {2 - running} paused</Chip>{flow?.template && <Chip>Intraday <b>{flow.template}</b></Chip>}</Head>
    <div className="cols c2">
      {V.E && <StratPanel title={`📈 ${V.E.name || 'Trend Breakout E+'}`} kind="swing · daily" chip={real === 'E' ? 'Real + Paper' : 'Paper'} on today={(A.paper_E?.day_pnl || 0) + (real === 'E' ? A.real?.day_pnl || 0 : 0)}
        cagr={V.E.normal?.cagr} dd={V.E.normal?.drawdown} open={openOf(real === 'E' ? ['paper_E', 'real'] : ['paper_E'])} curve={V.E.curve?.normal}
        color="var(--ok)" onOpen={() => onGo('breakout')} note={`${V.E.normal?.trades} trades · robust ${V.E.robust_pass}/${(V.E.robust || []).length}`} />}
      {!V.E && <Panel title="📈 Trend Breakout · E+" meta="swing · daily" right={<button type="button" className="btn small" onClick={() => onGo('breakout')}>Open →</button>}>
        <p className="muted small rempty">Not backtested yet: open the page and run it once.</p></Panel>}
      <StratPanel title={`⚡ Intraday Flow${flow?.template ? ` · ${flow.template}` : ''}`} kind="intraday · paper" chip={flow?.stopped ? 'stopped' : flow?.mode || '—'} on={flow && !flow.stopped && flow.mode !== 'off'}
        today={A.intraday?.day_pnl} cagr={canvas?.cagr} dd={canvas?.drawdown} open={openOf(['intraday'])} curve={canvas?.curve?.net}
        color="#f2b84b" onOpen={() => onGo('flow')} note={canvas ? `backtest ${canvas.period} · ${canvas.trades} trades · net ${inr(canvas.net)}` : 'not backtested yet'} />
    </div>
    <Panel title="📊 Saved intraday setups" meta={setups.length} right="latest backtest of each · open the Backtest tab of Intraday Flow to run or compare">
      {!bt ? <p className="muted small rempty">Loading…</p> : <FixedTable cols={cols} rows={rows} empty="No setups saved yet." />}
    </Panel>
  </div>;
}
