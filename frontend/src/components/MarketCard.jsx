import React, { useState } from 'react';

const STATUS = { uptrend: ['🟢 Uptrend', 'ok'], pressure: ['🟡 Under pressure', 'warnc'], correction: ['🔴 Correction', 'bad'] };
const mark = (v) => (v === true ? '✓' : v === false ? '✗' : '–');

// Market health strip on the Screens home: status, the signals behind it, and the last 60 days.
export default function MarketCard({ data }) {
  const [open, setOpen] = useState(false);              // phone: signals folded under the status line
  const h = data?.health;
  if (!h) return null;
  const [label, cls] = STATUS[h.status] || [h.status, ''];
  const prev = data.previous && data.previous.status !== h.status ? STATUS[data.previous.status]?.[0] : null;
  const ix = h.index || {}, br = h.breadth || {}, bo = h.breakouts || {};
  const sig = [
    [ix.above_50, 'Nifty 500 above 50-day'], [ix.above_200, 'above 200-day'], [ix.ma50_rising, '50-day rising'],
    [br.above_50_pct == null ? null : br.above_50_pct >= data.config.breadth_up,
      `Breadth ${br.above_50_pct ?? '–'}% above 50-day`],
    [h.dist_days == null ? null : h.dist_days <= data.config.dist_days_max, `${h.dist_days ?? '–'} distribution days`],
    [bo.rate == null ? null : bo.rate >= data.config.breakout_rate_min,
      `Breakouts working ${bo.working ?? 0}/${bo.total ?? 0}`],
  ];
  return (
    <div className={`panel marketcard m-${h.status} ${open ? 'open' : ''}`}>
      <div className="row">
        <button className={`mstatus ${cls}`} aria-expanded={open} onClick={() => setOpen(!open)}>
          <span className="mword">Market </span>{label}
          {h.breadth?.above_50_pct != null && <span className="mshort"> · breadth {h.breadth.above_50_pct}%</span>}
          <span className="mshort"> · 52w H/L {br.new_highs ?? '–'}/{br.new_lows ?? '–'}</span>
          <span className="mchev" aria-hidden="true"> ▾</span></button>
        {prev && <span className="muted small">was {prev}</span>}
        {ix.change_pct != null && <span className={`small ${ix.change_pct >= 0 ? 'up' : 'down'}`}>
          Nifty 500 {ix.change_pct > 0 ? '+' : ''}{ix.change_pct}% ({h.as_of})</span>}
        <span className="muted small">52-week highs {br.new_highs ?? '–'} / lows {br.new_lows ?? '–'}</span>
        <span className="right mhist" title="Market status, last 60 sessions">
          {(data.history || []).map((d) => <i key={d.as_of} className={`m-${d.status}`} title={`${d.as_of}: ${d.status}`} />)}
        </span>
      </div>
      <div className="msignals small">
        {sig.map(([ok, text]) => <span key={text} className={ok === true ? 'up' : ok === false ? 'down' : 'muted'}>
          {mark(ok)} {text}</span>)}
        {h.status === 'correction' && <b className="down">New buys: smaller size, or skip.</b>}
      </div>
    </div>
  );
}
