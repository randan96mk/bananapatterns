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

function Table({ screen, stage, asOf, selected, onSelect, sort }) {
  const [rows, setRows] = useState(null);
  useEffect(() => { setRows(null); api.screenResults(screen, stage).then((d) => setRows(d.rows)).catch(() => setRows([])); },
    [screen, stage, asOf]);
  const cols = COLS[stage];
  if (!rows) return <p className="muted">Loading…</p>;
  const shown = sort === 'rs' ? [...rows].sort((a, b) => (b.rs ?? -1) - (a.rs ?? -1)) : rows;
  if (!rows.length) return <p className="muted">None as of {asOf}.</p>;
  return (
    <div className="tablewrap">
      <table className="grid">
        <thead><tr><th>Symbol</th><th>Industry</th><th className="num">Close</th><th className="num">Chg %</th>
          <th className="num">RS</th><th className="num">Trend</th>
          {cols.map(([h]) => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>{shown.map((r) => (
          <tr key={r.symbol} className={r.symbol === selected ? 'sel' : ''} onClick={() => onSelect(r.symbol)}>
            <td><b>{r.symbol}</b><div className="muted small">{r.company}</div></td><td className="muted">{r.industry}</td>
            <td className="num">{rs(r.close)}</td>
            <td className={`num ${r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}`}>{pct(r.change_pct)}</td>
            <td className="num">{r.rs ?? ''}</td>
            <td className={`num ${r.tt_all ? 'up' : ''}`}>{r.tt_of ? `${r.tt_passed}/${r.tt_of}` : ''}</td>
            {cols.map(([h, f]) => <td key={h}>{f(r)}</td>)}
          </tr>))}
        </tbody>
      </table>
    </div>
  );
}

export default function StageList(props) {
  const [sort, setSort] = useState('default');
  return (
    <div className="panel">
      <div className="row">
        <h3>{TITLES[props.stage]}</h3>
        <span className="right muted">Sort</span>
        <select value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="default">Stage order</option>
          <option value="rs">RS rating</option>
        </select>
      </div>
      <Table {...props} sort={sort} />
      {props.stage === 'fresh' && (
        <>
          <h4>{TITLES.fresh_low_volume}</h4>
          <Table {...props} sort={sort} stage="fresh_low_volume" />
        </>
      )}
    </div>
  );
}
