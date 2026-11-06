import React from 'react';
import ResultsTag from './ResultsTag';
import FlagTags from './FlagTags';
import { Panel, FixedTable, Tag } from './ui2';

const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);

// Tonight's ranked setups (the saved plan behind the 19:00 Top N, 08:50 plan and 09:10 opening check).
export default function PlanCard({ plan, onSelect }) {
  if (!plan?.items?.length) return null;
  const cols = [{ h: 'Stock' }, { h: 'Close above', w: 110, n: true }, { h: 'Away', w: 70, n: true }, { h: 'Zone to', w: 100, n: true, lo: true },
    { h: 'Stop', w: 100, n: true }, { h: 'Qty', w: 70, n: true, lo: true }];
  const rows = plan.items.map((i, n) => ({ key: i.symbol, onClick: () => onSelect(i.symbol), cells: [
    <><span className="muted">{n + 1}</span> {i.tier && <span className={`tier t-${i.tier}`}>{i.tier}</span>} <b>{i.symbol}</b>
      {i.coiled && <Tag c="w" title="coiled: volume dried up">🌀 {i.coiled.volume_x}×</Tag>}<ResultsTag r={i.results} /><FlagTags f={i.flags} /></>,
    rs(i.pivot), i.dist_to_pivot_pct != null ? `${i.dist_to_pivot_pct.toFixed(1)}%` : '—', rs(i.zone_top), rs(i.stop), i.size ? `~${i.size.qty}` : '—'] }));
  return (
    <Panel title="🎯 Next session's plan" meta={plan.items.length} right={<span>from the {plan.as_of} scan</span>}>
      <FixedTable cols={cols} rows={rows} />
    </Panel>
  );
}
