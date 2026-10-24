import React from 'react';

const pct = (v, d = 2) => (v == null ? 'NA' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const num = (v, d = 2) => (v == null ? 'NA' : v.toFixed(d));

// Summary of the latest base under the chart, modelled on the site's breakout card.
export default function BaseCard({ latest, baseline }) {
  if (!latest) return <p className="muted">No base found with the current dials.</p>;
  const bo = latest.breakout;
  const rows = bo
    ? [
        ['Breakout date', bo.date],
        ['Breakout through', pct(bo.through_pct)],
        [`Breakout volume (vs ${baseline === 'median20' ? '20-day median' : '50-day avg'}, rule)`, `${num(bo.volume_ratio)}x`],
        [`Breakout volume (vs ${baseline === 'median20' ? '50-day avg' : '20-day median'})`,
          `${num(baseline === 'median20' ? bo.volume_ratio_sma50 : bo.volume_ratio_median20)}x`],
        ['Breakout close-in-range (0–1)', num(bo.close_in_range)],
        ['1-day gain on breakout', pct(bo.gain_1d_pct)],
        ['Breakout-day turnover (Rs cr)', num(bo.turnover_cr)],
        ['Base depth', `${num(latest.depth_pct, 1)}%`],
        ['Base volume vs prior pace', latest.vol_dryup == null ? 'NA' : `${num(latest.vol_dryup)}x`],
      ]
    : [
        ['Pivot', `₹${num(latest.pivot)}`],
        ['Distance under pivot', `${num(latest.dist_to_pivot_pct)}%`],
        ['Base length', `${latest.weeks} weeks (${latest.bars} sessions)`],
        ['Base depth', `${num(latest.depth_pct, 1)}%`],
        ['Base volume vs prior pace', latest.vol_dryup == null ? 'NA' : `${num(latest.vol_dryup)}x`],
        ['Base low', `₹${num(latest.low)} (${latest.low_date})`],
      ];
  // 'low volume' is already shown by the volume-rule pill
  const tags = bo ? [...bo.tags.filter((t) => t !== 'low volume'), ...(bo.squat === null ? ['squat: pending'] : [])] : [];
  return (
    <div className="basecard">
      <div className="row">
        <b>{bo ? `Broke out of B${latest.number}` : `Forming B${latest.number}`}</b>
        <span className="muted">{latest.weeks}w base · {latest.start_date} → {latest.end_date} · pivot ₹{num(latest.pivot)}</span>
        {bo && (bo.volume_ok
          ? <span className="tag ok">volume confirmed</span>
          : <span className="tag warnbg">below volume rule</span>)}
        {tags.map((t) => <span key={t} className="tag warnbg">{t}</span>)}
      </div>
      <div className="kv">
        {rows.map(([k, v]) => <div key={k}><span className="muted">{k}</span><b>{v}</b></div>)}
      </div>
    </div>
  );
}
