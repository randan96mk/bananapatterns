import React, { useEffect, useState } from 'react';
import NextUp from './NextUp';
import { clock } from '../timefmt';
import * as api from '../services/api';
import StockChart from './StockChart';
import ResultsTag from './ResultsTag';

const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v, d = 1) => (v == null ? '' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const HEALTH = { uptrend: ['🟢 Uptrend', 'up'], pressure: ['🟡 Under pressure', 'warn'], correction: ['🔴 Correction', 'down'] };
const POS_STATE = { under_stop: ['🔻 under stop', 'bad'], recovered: ['✅ recovered', 'ok'], heavy: ['⚠️ heavy selling', 'warnbg'] };

// Today's 5-minute closes, with a dashed reference line (pivot or stop).
function Spark({ points, level }) {
  if (!points || points.length < 2) return <span className="tdspark" />;
  const all = level != null ? [...points, level] : points;
  const lo = Math.min(...all), hi = Math.max(...all);
  const y = (v) => 22 - 20 * ((v - lo) / ((hi - lo) || 1)) + 1;
  const x = (i) => (i * 64) / (points.length - 1);
  const up = points[points.length - 1] >= points[0];
  return (
    <svg className={`tdspark ${up ? 'up' : 'down'}`} viewBox="0 0 64 24" aria-hidden="true">
      {level != null && <line x1="0" x2="64" y1={y(level)} y2={y(level)} />}
      <polyline points={points.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')} />
    </svg>
  );
}

function setupChip(l, s) {
  if (!l) return null;
  if (l.verdict === 'valid') return <span className="vd g">🟢 valid</span>;
  if (l.verdict === 'wait') return <span className="vd y">🟡 wait · {l.passed}/5</span>;
  if (l.verdict === 'skip') return <span className="vd r">🔴 {l.vs_pivot_pct > (l.zone_top / s.pivot - 1) * 100 ? 'extended' : 'skip'}</span>;
  return <span className="vd y">{Math.abs(l.vs_pivot_pct).toFixed(1)}% under</span>;
}

// The Today page: market strip, your positions (live guard) and the plan + near-pivot names with the entry
// checklist. Refreshes every 30 s while NSE is open (5 min otherwise); sparklines every 5 min.
export default function TodayPage() {
  const [d, setD] = useState(null);
  const [sparks, setSparks] = useState({});
  const [err, setErr] = useState('');
  const [selected, setSelected] = useState(null);
  const [open, setOpen] = useState(null);           // a setup whose checklist is unfolded

  useEffect(() => {
    let alive = true, t1, t2;
    const load = () => api.getToday().then((r) => { if (alive) { setD(r); setErr(''); } return r; })
      .catch((e) => { if (alive) setErr(e.message); return null; })
      .then((r) => { if (alive) t1 = setTimeout(load, r && ['open', 'pre_open'].includes(r.status) ? 30000 : 300000); });
    const loadSparks = () => api.getTodaySparks().then((r) => { if (alive) setSparks(r.sparks || {}); }).catch(() => {})
      .then(() => { if (alive) t2 = setTimeout(loadSparks, 300000); });
    load(); loadSparks();
    return () => { alive = false; clearTimeout(t1); clearTimeout(t2); };
  }, []);

  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;
  const m = d.market || {};
  const [hl, hc] = HEALTH[m.health] || ['Market', ''];
  const time = clock(d.as_of);
  const anyValid = d.setups.some((s) => s.live?.verdict === 'valid');

  return (
    <div className="today">
      <NextUp />
      <div className={`panel tdmarket m-${m.health || ''}`}>
        <b className={hc}>{hl}</b>
        {m.nifty500_pct != null && <span>Nifty 500 <b className={m.nifty500_pct >= 0 ? 'up' : 'down'}>{pct(m.nifty500_pct, 2)}</b></span>}
        {m.vix_pct != null && <span className="muted">VIX {pct(m.vix_pct)}</span>}
        <span className="right muted small"><span className={`dot ${d.live && d.status === 'open' ? 'on' : ''}`} />
          {d.live ? (d.status === 'open' ? `Live · ${time}` : `NSE ${d.status.replace('_', '-')} · ${time}`)
            : 'Last close · log in for live'}</span>
        {m.size_note && <span className="small warn tdsize">⚖️ {m.size_note.charAt(0).toUpperCase() + m.size_note.slice(1)}</span>}
      </div>
      {d.error && <div className="warn small">Live prices failed: {d.error}</div>}

      <div className="tdsec">Positions · {d.positions.length}</div>
      <div className="panel tdlist">
        {d.positions.length === 0 && <p className="muted">No positions followed. They come from your Kite holdings.</p>}
        {d.positions.map((p) => {
          const l = p.live, [st, sc] = POS_STATE[l?.state] || [];
          return (
            <button key={p.symbol} className={`tdrow ${l?.state === 'under_stop' ? 'alert' : ''}`} onClick={() => setSelected(p.symbol)}>
              <span className="tdl"><span><b>{p.symbol}</b>{st && <span className={`tag ${sc}`}>{st}</span>}</span>
                <span className="muted small">stop {rs(p.stop)}
                  {l?.r != null && ` · ${l.r > 0 ? '+' : ''}${l.r.toFixed(1)}R`}
                  {l?.state === 'under_stop' ? (l.pace ? ` · vol ${l.pace.toFixed(1)}×` : '')
                    : l?.stop_dist_pct != null ? ` · ${l.stop_dist_pct.toFixed(1)}% above` : ''}</span></span>
              <Spark points={sparks[p.symbol]} level={p.stop} />
              <span className="tdr"><b>{rs(l?.ltp)}</b><span className={`small ${(l?.day_pct ?? 0) >= 0 ? 'up' : 'down'}`}>{pct(l?.day_pct)}</span></span>
            </button>);
        })}
      </div>

      <div className="tdsec">Plan &amp; near the pivot · {d.setups.length}</div>
      <div className="panel tdlist">
        {d.setups.length === 0 && <p className="muted">No plan names or Forming names near their pivot.</p>}
        {d.setups.map((s) => {
          const l = s.live;
          return (
            <div key={s.symbol}>
              <button className="tdrow" onClick={() => (l?.checks ? setOpen(open === s.symbol ? null : s.symbol) : setSelected(s.symbol))}>
                <span className="tdl"><span><b>{s.symbol}</b>{s.tier && <span className={`tier t-${s.tier}`}>{s.tier}</span>}
                  {setupChip(l, s)}<ResultsTag r={s.results} /></span>
                  {l ? <span className="chips">
                    {l.vs_pivot_pct > 0 ? <span className={`c ${l.verdict === 'skip' ? 'bad' : 'ok'}`}>{pct(l.vs_pivot_pct)} pivot</span>
                      : <span className="c">trigger {rs(s.pivot)}</span>}
                    {l.vwap != null && <span className={`c ${l.ltp > l.vwap ? 'ok' : 'bad'}`}>VWAP {l.ltp > l.vwap ? '▲' : '▼'}</span>}
                    {l.pace != null && <span className={`c ${l.pace >= 1.5 ? 'ok' : ''}`}>vol {l.pace.toFixed(1)}×</span>}
                    {l.vs_nifty != null && l.vs_pivot_pct > 0 && <span className={`c ${l.vs_nifty > 0 ? 'ok' : 'bad'}`}>vs Nifty {pct(l.vs_nifty)}</span>}
                  </span> : <span className="muted small">trigger {rs(s.pivot)} · stop {rs(s.stop)}</span>}</span>
                <Spark points={sparks[s.symbol]} level={s.pivot} />
                <span className="tdr"><b>{rs(l?.ltp)}</b>
                  {l?.size ? <span className="small">qty {l.size.qty}</span>
                    : <span className={`small ${(l?.day_pct ?? 0) >= 0 ? 'up' : 'down'}`}>{pct(l?.day_pct)}</span>}</span>
              </button>
              {open === s.symbol && l?.checks && (
                <div className="tdchecks small">
                  {l.checks.map((c) => <div key={c.id}>{c.ok ? '✅' : c.ok === null ? '⏳' : '❌'} {c.text}</div>)}
                  {l.verdict === 'valid' && <div><b>🎯 Buy up to {rs(l.zone_top)}</b> · stop {rs(s.stop)}
                    {l.size ? ` · qty ${l.size.qty} · risk ₹${l.size.risk_rs.toLocaleString('en-IN')}` : ''}</div>}
                  <button className="linkbtn" onClick={() => setSelected(s.symbol)}>Open the chart ›</button>
                </div>)}
            </div>);
        })}
        {anyValid && !d.capital_set && <p className="muted small">Set <b>Capital</b> in Alerts → Size to see a quantity.</p>}
      </div>
      <p className="muted small tdfoot">Refreshes every 30 s while NSE is open · tap a position for its chart, a setup for its checklist.
        Unconfirmed until the close: breakouts count only on a closing basis.</p>
      {selected && <StockChart symbol={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
