import React from 'react';
import { Panel, Tag } from './ui2';

export const STATUS = { uptrend: ['🟢 Uptrend', 'ok'], pressure: ['🟡 Under pressure', 'w'], correction: ['🔴 Correction', 'bad'] };
const mark = (v) => (v === true ? '✓' : v === false ? '✗' : '–');
const DOT = { uptrend: 'var(--ok)', pressure: 'var(--warn)', correction: 'var(--bad)' };

// The signals behind the market status (used by the Market panel on the Screens page and the Market filter card).
export function marketSignals(data) {
  const h = data?.health;
  if (!h) return [];
  const ix = h.index || {}, br = h.breadth || {}, bo = h.breakouts || {};
  return [
    [ix.above_50, 'Nifty 500 above 50-day'], [ix.above_200, 'above 200-day'], [ix.ma50_rising, '50-day rising'],
    [br.above_50_pct == null ? null : br.above_50_pct >= data.config.breadth_up, `Breadth ${br.above_50_pct ?? '–'}% above 50-day`],
    [h.dist_days == null ? null : h.dist_days <= data.config.dist_days_max, `${h.dist_days ?? '–'} distribution days`],
    [bo.rate == null ? null : bo.rate >= data.config.breakout_rate_min, `Breakouts working ${bo.working ?? 0}/${bo.total ?? 0}`],
  ];
}

// Market health on the Screens page: status, the signals behind it, and the last 60 sessions as a strip of dots.
export default function MarketCard({ data }) {
  const h = data?.health;
  if (!h) return null;
  const [label, cls] = STATUS[h.status] || [h.status, ''];
  const prev = data.previous && data.previous.status !== h.status ? STATUS[data.previous.status]?.[0] : null;
  const ix = h.index || {}, br = h.breadth || {};
  return (
    <Panel title="Market" meta={<Tag c={cls}>{label}</Tag>} right={<>{prev && <span>was {prev}</span>}
      {ix.change_pct != null && <span className={`mono ${ix.change_pct >= 0 ? 'up' : 'down'}`}>Nifty 500 {ix.change_pct > 0 ? '+' : ''}{ix.change_pct}%</span>}
      <span className="mono">52w H/L {br.new_highs ?? '–'}/{br.new_lows ?? '–'}</span><span>{h.as_of}</span></>}>
      <div className="rwrap" style={{ gap: '4px 12px' }}>
        {marketSignals(data).map(([ok, text]) => <span key={text} className={ok === true ? 'up' : ok === false ? 'down' : 'muted'}>{mark(ok)} {text}</span>)}
        <span className="muted">52-week highs {br.new_highs ?? '–'} / lows {br.new_lows ?? '–'}</span>
        {h.status === 'correction' && <b className="down">New buys: smaller size, or skip.</b>}
      </div>
      {(data.history || []).length > 0 && <div className="rnote" title="Market status, last 60 sessions" style={{ display: 'flex', gap: 2, alignItems: 'center', flexWrap: 'wrap' }}>
        <span style={{ marginRight: 6 }}>last {data.history.length} sessions</span>
        {data.history.map((d) => <i key={d.as_of} title={`${d.as_of}: ${d.status}`} style={{ width: 6, height: 10, borderRadius: 2, background: DOT[d.status] || 'var(--border)', display: 'inline-block' }} />)}
      </div>}
    </Panel>
  );
}
