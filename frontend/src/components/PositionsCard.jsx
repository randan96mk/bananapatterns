import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useIsPhone } from '../useMedia';
import ResultsTag from './ResultsTag';
import NewsTag from './NewsTag';
import FlagTags from './FlagTags';

const rs = (v) => (v == null ? '' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);
const LIVE = { under_stop: ['🔻 under stop', 'bad'], recovered: ['✅ recovered', 'ok'], heavy: ['⚠️ heavy selling', 'warnbg'] };
const pct = (v) => (v == null ? '' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);
// Live state chip (market hours) and the price line under it.
const liveChip = (l) => (l && LIVE[l.state] ? <span className={`tag ${LIVE[l.state][1]}`}>{LIVE[l.state][0]}</span> : null);
const livePrice = (l) => (l ? <span className="small"><b>{rs(l.ltp)}</b>{' '}
  <span className={l.day_pct >= 0 ? 'up' : 'down'}>{pct(l.day_pct)}</span></span> : null);
// How far the price is above the stop: full bar at 15%+, red when under it.
const stopBar = (l) => (l?.stop_dist_pct == null ? null : (
  <span className="stopbar" title={`${l.stop_dist_pct.toFixed(1)}% ${l.stop_dist_pct >= 0 ? 'above' : 'under'} the stop`}>
    <i className={l.stop_dist_pct < 0 ? 'bad' : l.stop_dist_pct < 3 ? 'warn' : 'ok'}
       style={{ width: `${Math.max(4, Math.min(100, (l.stop_dist_pct / 15) * 100))}%` }} /></span>));
const RULES = { stop: '🛑 stop', ma50: '📉 50-day', profit: '💰 profit zone', extended: '🎈 extended', failed: '⚠️ failed' };

// Positions the guard follows (from Kite holdings, read-only), with an editable stop.
export default function PositionsCard({ onSelect }) {
  const [rows, setRows] = useState(null);
  const [msg, setMsg] = useState('');
  const phone = useIsPhone();
  const load = () => api.getPositions().then((d) => setRows(d.positions)).catch(() => setRows([]));
  useEffect(() => { load(); }, []);
  if (!rows?.length) return null;
  const saveStop = async (sym, v) => {
    try { await api.setStop(sym, Number(v)); setMsg(`${sym} stop saved`); load(); } catch (e) { setMsg(e.message); }
  };
  return (
    <div className="panel">
      <div className="row"><b>📒 Positions{phone ? ` · ${rows.length}` : ' the guard follows'}</b>
        <span className={`muted small ${phone ? 'right' : ''}`}>{rows.some((p) => p.live) ? 'live · checked every 5 min'
          : phone ? 'checked after the evening scan' : 'from Kite holdings (read-only) · checked after the evening scan'}</span>
        <span className="msg">{msg}</span></div>
      {phone ? (
        <div className="posrows">{rows.map((p) => {
          const sent = Object.keys(p.flags || {});
          return (
            <div key={p.symbol} className="posrow">
              <span className="pk-left">
                <span><b className="link" onClick={() => onSelect(p.symbol)}>{p.symbol}</b>
                  {liveChip(p.live)}
                  {sent.includes('stop') && <span className="tag bad">🛑 stop</span>}
                  <ResultsTag r={p.results} /><NewsTag n={p.news} /><FlagTags f={p.signals} /></span>
                {livePrice(p.live)}
                <span className="muted small">{p.qty} @ {rs(p.avg_price)}{p.pivot ? ` · pivot ${rs(p.pivot)}` : ''}
                  {sent.filter((k) => k !== 'stop').map((k) => ` · ${RULES[k] || k}`).join('')}</span>
                {stopBar(p.live)}
              </span>
              <label className="pk-right">
                <input className="stopinput" inputMode="decimal" aria-label={`${p.symbol} stop`} defaultValue={p.stop}
                  onBlur={(e) => Number(e.target.value) !== p.stop && saveStop(p.symbol, e.target.value)} />
                <span className="muted small">{p.stop_source}</span></label>
            </div>);
        })}</div>
      ) : (
      <div className="tablewrap"><table className="grid"><thead><tr><th>Symbol</th><th className="num">Qty</th>
        <th className="num">Avg</th>{rows.some((p) => p.live) && <th className="num">Now</th>}<th className="num">Pivot</th>
        <th>Stop</th><th>Since</th><th>Alerts sent</th></tr></thead>
        <tbody>{rows.map((p) => (
          <tr key={p.symbol}>
            <td><b className="link" onClick={() => onSelect(p.symbol)}>{p.symbol}</b>{liveChip(p.live)}
              <ResultsTag r={p.results} /><NewsTag n={p.news} /><FlagTags f={p.signals} /></td>
            <td className="num">{p.qty}</td><td className="num">{rs(p.avg_price)}</td>
            {rows.some((q) => q.live) && <td className="num">{livePrice(p.live)}{stopBar(p.live)}</td>}
            <td className="num">{rs(p.pivot)}</td>
            <td><input className="stopinput" defaultValue={p.stop} onBlur={(e) => Number(e.target.value) !== p.stop
              && saveStop(p.symbol, e.target.value)} /> <span className="muted small">{p.stop_source}</span></td>
            <td className="muted">{p.first_seen}</td>
            <td className="small">{Object.keys(p.flags || {}).map((k) => RULES[k] || k).join(' · ')}</td>
          </tr>))}</tbody></table></div>
      )}
    </div>
  );
}
