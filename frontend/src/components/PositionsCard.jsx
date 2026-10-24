import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

const rs = (v) => (v == null ? '' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);
const RULES = { stop: '🛑 stop', ma50: '📉 50-day', profit: '💰 profit zone', extended: '🎈 extended', failed: '⚠️ failed' };

// Positions the guard follows (from Kite holdings, read-only), with an editable stop.
export default function PositionsCard({ onSelect }) {
  const [rows, setRows] = useState(null);
  const [msg, setMsg] = useState('');
  const load = () => api.getPositions().then((d) => setRows(d.positions)).catch(() => setRows([]));
  useEffect(() => { load(); }, []);
  if (!rows?.length) return null;
  const saveStop = async (sym, v) => {
    try { await api.setStop(sym, Number(v)); setMsg(`${sym} stop saved`); load(); } catch (e) { setMsg(e.message); }
  };
  return (
    <div className="panel">
      <div className="row"><b>📒 Positions the guard follows</b>
        <span className="muted small">from Kite holdings (read-only) · checked after the evening scan</span>
        <span className="msg">{msg}</span></div>
      <div className="tablewrap"><table className="grid"><thead><tr><th>Symbol</th><th className="num">Qty</th>
        <th className="num">Avg</th><th className="num">Pivot</th><th>Stop</th><th>Since</th><th>Alerts sent</th></tr></thead>
        <tbody>{rows.map((p) => (
          <tr key={p.symbol}>
            <td><b className="link" onClick={() => onSelect(p.symbol)}>{p.symbol}</b></td>
            <td className="num">{p.qty}</td><td className="num">{rs(p.avg_price)}</td><td className="num">{rs(p.pivot)}</td>
            <td><input className="stopinput" defaultValue={p.stop} onBlur={(e) => Number(e.target.value) !== p.stop
              && saveStop(p.symbol, e.target.value)} /> <span className="muted small">{p.stop_source}</span></td>
            <td className="muted">{p.first_seen}</td>
            <td className="small">{Object.keys(p.flags).map((k) => RULES[k] || k).join(' · ')}</td>
          </tr>))}</tbody></table></div>
    </div>
  );
}
