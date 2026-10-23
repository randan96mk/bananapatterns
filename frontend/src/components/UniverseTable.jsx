import React, { useMemo, useState } from 'react';

const COLS = [
  ['symbol', 'Symbol'], ['company', 'Company'], ['industry', 'Industry'], ['close', 'Close'],
  ['change_pct', 'Chg %'], ['bars', 'Bars'], ['first_date', 'From'], ['last_date', 'To'],
];

// Sortable/filterable list of the universe with data coverage per symbol.
export default function UniverseTable({ data, selected, onSelect }) {
  const [q, setQ] = useState('');
  const [industry, setIndustry] = useState('');
  const [sort, setSort] = useState(['symbol', 1]);

  const rows = data?.rows || [];
  const industries = useMemo(
    () => [...new Set(rows.map((r) => r.industry).filter(Boolean))].sort(), [rows]);

  const view = useMemo(() => {
    const needle = q.trim().toUpperCase();
    const [key, dir] = sort;
    return rows
      .filter((r) => (!needle || r.symbol.includes(needle) || (r.company || '').toUpperCase().includes(needle))
        && (!industry || r.industry === industry))
      .sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === 'index' ? -1 : 1;
        const x = a[key], y = b[key];
        if (x == null) return 1;
        if (y == null) return -1;
        return (x > y ? 1 : x < y ? -1 : 0) * dir;
      });
  }, [rows, q, industry, sort]);

  if (!data) return <div className="panel">Loading…</div>;
  if (!rows.length) {
    return <div className="panel"><p>No universe yet. Log in, then use <b>Data → Refresh universe</b> and <b>Sync candles</b>.</p></div>;
  }
  const withData = rows.filter((r) => r.bars > 0).length;
  const lastDate = rows.reduce((m, r) => (r.last_date && r.last_date > m ? r.last_date : m), '');
  const failed = rows.filter((r) => r.last_error).length;

  return (
    <div className="panel">
      <div className="row">
        <b>Nifty 500</b>
        <span className="muted">snapshot {data.snapshot_date} · {withData}/{rows.length} with data · latest bar {lastDate || '—'}
          {failed > 0 && <span className="warn"> · {failed} with errors</span>}
          {data.unmatched.length > 0 && <span className="warn"> · unmatched: {data.unmatched.join(', ')}</span>}
          {data.excluded?.length > 0 && <span> · excluded (series): {data.excluded.join(', ')}</span>}
        </span>
      </div>
      <div className="row">
        <input placeholder="Search symbol / company" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={industry} onChange={(e) => setIndustry(e.target.value)}>
          <option value="">All industries</option>
          {industries.map((i) => <option key={i}>{i}</option>)}
        </select>
        <span className="muted">{view.length} shown</span>
      </div>
      <div className="tablewrap">
        <table className="grid">
          <thead><tr>{COLS.map(([k, label]) => (
            <th key={k} onClick={() => setSort([k, sort[0] === k ? -sort[1] : 1])}>
              {label}{sort[0] === k ? (sort[1] > 0 ? ' ▲' : ' ▼') : ''}</th>))}</tr></thead>
          <tbody>
            {view.map((r) => (
              <tr key={r.instrument_token} className={r.symbol === selected ? 'sel' : ''}
                  onClick={() => onSelect(r.symbol)} title={r.last_error || ''}>
                <td><b>{r.symbol}</b>{r.kind === 'index' && <span className="tag">index</span>}
                  {r.series && r.series !== 'EQ' && <span className="tag" title={`Kite: ${r.tradingsymbol}`}>{r.series}</span>}
                  {r.last_error && <span className="tag bad">err</span>}</td>
                <td>{r.company}</td><td>{r.industry}</td>
                <td className="num">{r.close?.toFixed(2)}</td>
                <td className={`num ${r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}`}>
                  {r.change_pct != null ? r.change_pct.toFixed(2) : ''}</td>
                <td className={`num ${r.bars && r.bars < 260 ? 'warn' : ''}`}>{r.bars}</td>
                <td>{r.first_date}</td><td>{r.last_date}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
