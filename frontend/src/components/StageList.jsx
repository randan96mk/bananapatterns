import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

const pct = (v) => (v == null ? '' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);
const rs = (v) => (v == null ? '' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);

// Columns per stage: [header, render(row)]
const COLS = {
  forming: [['Base', (r) => r.base_label], ['Pivot', (r) => rs(r.pivot)], ['Under pivot', (r) => `${r.dist_to_pivot_pct?.toFixed(1)}%`],
    ['Depth', (r) => `${r.depth_pct?.toFixed(1)}%`]],
  fresh: [['Broke out of', (r) => r.base_label], ['Breakout', (r) => r.breakout_date], ['Pivot', (r) => rs(r.pivot)],
    ['From pivot', (r) => pct(r.gain_from_pivot_pct)], ['Volume', (r) => `${r.volume_ratio?.toFixed(2)}x`]],
  climbing: [['Broke out of', (r) => r.base_label], ['Breakout', (r) => r.breakout_date], ['Sessions', (r) => r.sessions_since_breakout],
    ['From pivot', (r) => pct(r.gain_from_pivot_pct)], ['Peak', (r) => pct(r.peak_gain_pct)]],
  played_out: [['Run from', (r) => r.base_label], ['Breakout', (r) => r.breakout_date], ['Ended', (r) => r.ended_date],
    ['Why', (r) => r.ended_reason], ['At end', (r) => pct(r.gain_from_pivot_pct)], ['Peak', (r) => pct(r.peak_gain_pct)]],
};
COLS.fresh_low_volume = COLS.fresh;
const TITLES = { forming: 'Forming', fresh: 'Fresh breakouts', climbing: 'Climbing', played_out: 'Played out',
  fresh_low_volume: 'Low-volume breakouts (below the volume rule; not counted)' };

function Table({ screen, stage, asOf, selected, onSelect }) {
  const [rows, setRows] = useState(null);
  useEffect(() => { setRows(null); api.screenResults(screen, stage).then((d) => setRows(d.rows)).catch(() => setRows([])); },
    [screen, stage, asOf]);
  const cols = COLS[stage];
  if (!rows) return <p className="muted">Loading…</p>;
  if (!rows.length) return <p className="muted">None as of {asOf}.</p>;
  return (
    <div className="tablewrap">
      <table className="grid">
        <thead><tr><th>Symbol</th><th>Industry</th><th className="num">Close</th><th className="num">Chg %</th>
          {cols.map(([h]) => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.symbol} className={r.symbol === selected ? 'sel' : ''} onClick={() => onSelect(r.symbol)}>
            <td><b>{r.symbol}</b><div className="muted small">{r.company}</div></td><td className="muted">{r.industry}</td>
            <td className="num">{rs(r.close)}</td>
            <td className={`num ${r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}`}>{pct(r.change_pct)}</td>
            {cols.map(([h, f]) => <td key={h}>{f(r)}</td>)}
          </tr>))}
        </tbody>
      </table>
    </div>
  );
}

export default function StageList(props) {
  return (
    <div className="panel">
      <h3>{TITLES[props.stage]}</h3>
      <Table {...props} />
      {props.stage === 'fresh' && (
        <>
          <h4>{TITLES.fresh_low_volume}</h4>
          <Table {...props} stage="fresh_low_volume" />
        </>
      )}
    </div>
  );
}
