import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

const fmt = (v, d = 2) => (v == null ? '—' : v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }));
const STATUS = { open: 'Live · NSE open', pre_open: 'Pre-open', closed: 'Closed', holiday: 'Holiday' };

// Tiny intraday line: today's 5-minute closes against the previous close (dashed).
function Spark({ points, prev, cls }) {
  if (!points || points.length < 2) return null;
  const all = prev != null ? [...points, prev] : points;
  const lo = Math.min(...all), hi = Math.max(...all);
  const y = (v) => 24 - 22 * ((v - lo) / ((hi - lo) || 1)) + 1;
  const x = (i) => (i * 64) / (points.length - 1);
  return (
    <svg className={`tk-spark ${cls}`} viewBox="0 0 64 26" aria-hidden="true">
      {prev != null && <line x1="0" x2="64" y1={y(prev)} y2={y(prev)} className="tk-prev" />}
      <polyline points={points.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')} />
    </svg>
  );
}

// The index ticker bar: Nifty 50, Nifty 500, Bank Nifty, India VIX, Sensex. Polls every 30 s while the market
// is open (5 min otherwise); a click opens the index chart.
export default function IndexTicker({ onSelect }) {
  const [d, setD] = useState(null);
  useEffect(() => {
    let alive = true, timer;
    const load = () => api.getIndices().then((r) => { if (alive) setD(r); return r; }).catch(() => null)
      .then((r) => { if (alive) timer = setTimeout(load, r && ['open', 'pre_open'].includes(r.status) ? 30000 : 300000); });
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, []);
  if (!d || !d.items?.length) return null;
  const time = d.as_of ? d.as_of.slice(11, 19) : '';
  return (
    <div className="ticker" role="list" aria-label="Indices">
      {d.items.map((i) => {
        if (i.missing) return null;
        const vix = i.symbol === 'INDIA VIX';
        const up = (i.change ?? 0) >= 0;
        const cls = vix ? (up ? 'warn' : 'up') : (up ? 'up' : 'down');
        const sign = up ? '+' : '';
        return (
          <button key={i.symbol} className="tk" role="listitem" onClick={() => onSelect(i.symbol)}
                  title={`${i.label}: open the chart`}>
            <Spark points={i.spark} prev={i.prev_close} cls={cls} />
            <span className="tk-body">
              <span className="nm">{i.label}
                {vix ? (up && i.change ? <span className="ma warnbg">fear ↑</span> : null)
                  : i.above_ma50 != null && <span className={`ma ${i.above_ma50 ? 'ok' : 'bad'}`}
                      title={`${i.above_ma50 ? 'Above' : 'Below'} its 50-day average (${fmt(i.ma50)})`}>
                      {i.above_ma50 ? '▲' : '▼'} 50d</span>}
              </span>
              <span className="px">{fmt(i.last)}</span>
              <span className={`ch ${cls}`}>{i.change == null ? '' : `${sign}${fmt(i.change)} (${sign}${fmt(i.change_pct)}%)`}</span>
            </span>
          </button>);
      })}
      <div className="tstat">
        <div><span className={`dot ${d.status === 'open' && d.live ? 'on' : ''}`} />
          {d.live ? STATUS[d.status] : 'Last close · log in for live'}</div>
        <div>{d.live ? `updated ${time}` : (d.items.find((i) => i.date)?.date || '')}</div>
      </div>
    </div>
  );
}
