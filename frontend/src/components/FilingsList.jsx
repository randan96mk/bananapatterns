import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

const cr = (d) => (d.qty * d.price) / 1e7;
const dealText = (d) => (d.source === 'sast'
  ? `${d.client} ${d.side === 'BUY' ? 'acquired' : 'sold'} ${d.pct?.toFixed(2)}%`
    + `${d.after_pct != null ? ` (now ${d.after_pct.toFixed(2)}%)` : ''}${d.promoter ? ' · promoter' : ''}`
  : `${d.client} ${d.side === 'BUY' ? 'bought' : 'sold'} ₹${cr(d).toFixed(1)} Cr @ ₹${d.price} (${d.source} deal)`);

// The stock's NSE filings, bulk / block deals and holder disclosures for the last 30 days.
export default function FilingsList({ symbol }) {
  const [rows, setRows] = useState(null);
  const [deals, setDeals] = useState(null);
  useEffect(() => {
    setRows(null); setDeals(null);
    api.stockFilings(symbol).then((d) => setRows(d.filings)).catch(() => setRows([]));
    api.stockDeals(symbol).then((d) => setDeals(d.deals)).catch(() => setDeals([]));
  }, [symbol]);
  if (!rows) return <p className="muted small">loading…</p>;
  return (
    <>
      {deals?.length > 0 && (
        <ul className="filings">{deals.map((d) => (
          <li key={`${d.date}${d.client}${d.side}${d.qty}${d.source}`}>
            <span className="small muted">{d.date}</span>{' '}
            <b className={d.side === 'BUY' ? 'up' : 'down'}>{d.source === 'sast' ? 'Holder' : 'Deal'}</b>{' '}
            {d.url ? <a href={d.url} target="_blank" rel="noreferrer">{dealText(d)}</a> : dealText(d)}
          </li>))}
        </ul>)}
      {!rows.length ? <p className="muted small">No exchange filings in the last 30 days (or not fetched yet).</p> : (
    <ul className="filings">{rows.map((f) => (
      <li key={f.seq_id} className={f.kind === 'update' ? 'muted' : ''}>
        <span className="small muted">{f.dt.slice(0, 10)}</span>{' '}
        <b className={f.kind === 'legal' ? 'down' : ''}>{f.label}</b>{' '}
        {f.url ? <a href={f.url} target="_blank" rel="noreferrer">{f.text || f.category}</a> : (f.text || f.category)}
      </li>))}
    </ul>)}
    </>
  );
}
