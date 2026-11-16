import React, { useState } from 'react';
import ResultsTag from './ResultsTag';
import FlagTags from './FlagTags';
import { Panel, FixedTable, Tag, AiTag } from './ui2';
import { useAiViews, viewOf, AiView, AiLevels, AiReasons, aiReasons } from './AiView';
import { useIsPhone } from '../useMedia';

const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);

// Tonight's ranked setups (the saved plan behind the 19:00 Top N, 08:50 plan and 09:10 opening check), with the 🤖 AI
// view beside each name: score · verdict · reasons, and "levels ▾" opens the levels it picked under the row. The AI
// column is for your eyes: the system's order and sizes are unchanged.
export default function PlanCard({ plan, onSelect }) {
  const [open, setOpen] = useState(null);
  const phone = useIsPhone();                     // phone: the reasons go under the name (the column is dropped)
  const items = plan?.items || [];
  const views = useAiViews(items.map((i) => i.symbol));
  if (!items.length) return null;
  const rated = items.filter((i) => viewOf(i, views)).length;
  const cols = [{ h: 'Stock' }, { h: 'Close above', w: 104, n: true }, { h: 'Away', w: 64, n: true }, { h: 'Zone to', w: 100, n: true, lo: true, nw: true },
    { h: 'Stop', w: 96, n: true }, { h: 'Qty', w: 60, n: true, lo: true }, { h: 'AI view · score', w: 178 }, { h: 'Reasons (measured facts + filings)', w: 400, ell: true, nw: true }, { h: 'Levels', w: 92 }];
  const rows = items.flatMap((i, n) => {
    const v = viewOf(i, views);
    const row = { key: i.symbol, onClick: () => onSelect(i.symbol), title: v ? aiReasons(v) : undefined, cells: [
      <><span className="muted">{n + 1}</span> {i.tier && <span className={`tier t-${i.tier}`}>{i.tier}</span>} <b>{i.symbol}</b>
        {i.coiled && <Tag c="w" title="coiled: volume dried up">🌀 {i.coiled.volume_x}×</Tag>}<ResultsTag r={i.results} /><FlagTags f={i.flags} />
        {phone && v && <small style={{ display: 'block', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}><AiReasons v={v} /></small>}</>,
      rs(i.pivot), i.dist_to_pivot_pct != null ? `${i.dist_to_pivot_pct.toFixed(1)}%` : '—', rs(i.zone_top), rs(i.stop), i.size ? `~${i.size.qty}` : '—',
      <AiView v={v} />, <AiReasons v={v} />,
      v ? <button type="button" className="btn ghost small" aria-expanded={open === i.symbol} onClick={(e) => { e.stopPropagation(); setOpen(open === i.symbol ? null : i.symbol); }}>levels {open === i.symbol ? '▴' : '▾'}</button> : <span className="muted">—</span>] };
    return open === i.symbol ? [row, { key: `${i.symbol}-lv`, span: <><b style={{ fontSize: 12.5 }}>{i.symbol} · levels</b><div style={{ marginTop: 6 }}><AiLevels v={v} pivot={i.pivot} symbol={i.symbol} /></div></> }] : [row];
  });
  return (
    <Panel title="🎯 Next session's plan" meta={items.length} right={<span>from the {plan.as_of} scan{rated ? <> · <AiTag title="the AI view: reasons written by the local model; the score and the verdict are set by code" /> view on {rated} of {items.length} · rating only</> : ''}</span>}>
      <FixedTable cols={cols} rows={rows} />
      {rated > 0 && <div className="rnote">ranked by the system · the AI view is for your eyes: the order, sizes and stops are unchanged · scored at 5, 10 and 20 sessions · System › AI › AI view record</div>}
    </Panel>
  );
}
