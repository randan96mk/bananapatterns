import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

const x2 = (v) => (v == null ? 'NA' : `${v.toFixed(2)}x`);

// VCP check for the latest base: verdict, failed rules, metrics, pullback sequence and stop.
export default function VcpPanel({ symbol }) {
  const [d, setD] = useState(null);
  useEffect(() => { setD(null); api.getVcp(symbol).then(setD).catch(() => setD({})); }, [symbol]);
  if (!d) return null;
  const v = d.vcp;
  if (!v) return <p className="muted">No base to judge as a VCP.</p>;
  const cons = v.contractions || [];
  return (
    <div className="vcppanel">
      <div className="row">
        <b>VCP check · {d.base_label}</b>
        {v.qualifies ? <span className="tag ok">VCP ✓</span> : <span className="tag warnbg">not a VCP</span>}
        {!v.qualifies && <span className="muted">{v.fails.join(' · ')}</span>}
        <span className="muted right">judged on {v.eval_date}</span>
      </div>
      <div className="kv">
        <div><span className="muted">Pullbacks (ZigZag)</span><b>{cons.length ? cons.map((c) => `${c.depth_pct.toFixed(1)}%`).join(' → ') : 'none'}</b></div>
        <div><span className="muted">Tightening (ATR ratio)</span><b>{x2(v.tightening)}</b></div>
        <div><span className="muted">Volume dry-up</span><b>{x2(v.dryup)}</b></div>
        <div><span className="muted">Up/down volume ratio</span><b>{x2(v.ud_ratio)}</b></div>
        <div><span className="muted">Stop (last pullback low − 1%, max 8%)</span><b>{v.stop ? `₹${v.stop.toFixed(2)} (−${v.stop_pct.toFixed(1)}%)` : 'NA'}</b></div>
        <div><span className="muted">Shrinking</span><b>{v.shrinking == null ? 'NA' : v.shrinking ? 'yes' : 'no'}</b></div>
      </div>
    </div>
  );
}
