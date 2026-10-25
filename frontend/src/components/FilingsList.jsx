import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

// The stock's NSE filings for the last 30 days: important ones in bold, each linking to the filing.
export default function FilingsList({ symbol }) {
  const [rows, setRows] = useState(null);
  useEffect(() => {
    setRows(null);
    api.stockFilings(symbol).then((d) => setRows(d.filings)).catch(() => setRows([]));
  }, [symbol]);
  if (!rows) return <p className="muted small">loading…</p>;
  if (!rows.length) return <p className="muted small">No exchange filings in the last 30 days (or not fetched yet).</p>;
  return (
    <ul className="filings">{rows.map((f) => (
      <li key={f.seq_id} className={f.kind === 'update' ? 'muted' : ''}>
        <span className="small muted">{f.dt.slice(0, 10)}</span>{' '}
        <b className={f.kind === 'legal' ? 'down' : ''}>{f.label}</b>{' '}
        {f.url ? <a href={f.url} target="_blank" rel="noreferrer">{f.text || f.category}</a> : (f.text || f.category)}
      </li>))}
    </ul>
  );
}
