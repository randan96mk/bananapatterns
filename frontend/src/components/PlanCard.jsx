import React from 'react';
import ResultsTag from './ResultsTag';
import FlagTags from './FlagTags';
import { useIsPhone } from '../useMedia';

const rs = (v) => (v == null ? '' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);

// Tonight's ranked setups (the saved plan behind the 19:00 Top N, 08:50 plan and 09:10 opening check).
export default function PlanCard({ plan, onSelect }) {
  const phone = useIsPhone();
  if (!plan?.items?.length) return null;
  if (phone) {      // one row per stock: rank, tier, symbol | trigger and distance, then zone and stop
    return (
      <div className="panel plancard">
        <div className="row"><b>🎯 Next session's plan</b><span className="right muted small">from the {plan.as_of} scan</span></div>
        {plan.items.map((i, n) => (
          <button key={i.symbol} className="peekrow" onClick={() => onSelect(i.symbol)}>
            <span className="pk-left"><span className="muted">{n + 1}</span>
              {i.tier && <span className={`tier t-${i.tier}`}>{i.tier}</span>}<b>{i.symbol}</b>
              {i.coiled && <span className="tag warnbg">🌀 {i.coiled.volume_x}×</span>}
              <ResultsTag r={i.results} /><FlagTags f={i.flags} /></span>
            <span className="pk-right"><span>close &gt; <b>{rs(i.pivot)}</b> · {i.dist_to_pivot_pct?.toFixed(1)}%</span>
              <span className="muted small">zone {rs(i.zone_top)} · stop {rs(i.stop)}{i.size ? ` · ~${i.size.qty} sh` : ''}</span></span>
          </button>))}
      </div>
    );
  }
  return (
    <div className="panel plancard">
      <div className="row"><b>🎯 Next session's plan</b><span className="muted small">from the {plan.as_of} scan</span></div>
      <div className="planrows">
        {plan.items.map((i, n) => (
          <button key={i.symbol} className="planrow" onClick={() => onSelect(i.symbol)}>
            <span className="muted">{n + 1}</span>
            {i.tier && <span className={`tier t-${i.tier}`}>{i.tier}</span>}
            <b>{i.symbol}</b>
            <span>close above <b>{rs(i.pivot)}</b> <span className="muted">({i.dist_to_pivot_pct?.toFixed(1)}% away)</span></span>
            <span className="muted">zone to {rs(i.zone_top)} · stop {rs(i.stop)}{i.size ? ` · ~${i.size.qty} sh` : ''}</span>
            {i.coiled && <span className="tag warnbg">🌀 coiled {i.coiled.volume_x}×</span>}
            <ResultsTag r={i.results} />
            <FlagTags f={i.flags} />
          </button>))}
      </div>
    </div>
  );
}
