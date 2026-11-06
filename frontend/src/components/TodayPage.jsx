import React, { useEffect, useState } from 'react';
import NextUp from './NextUp';
import { clock } from '../timefmt';
import { inr } from '../fmt';
import * as api from '../services/api';
import StockChart from './StockChart';
import ResultsTag from './ResultsTag';
import { Head, Chip, Cards, Card, Panel, KV, Tag } from './ui2';

const rs = (v) => (v == null ? '—' : v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const pct = (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const HEALTH = { uptrend: ['🟢 Uptrend', 'ok'], pressure: ['🟡 Under pressure', 'warn'], correction: ['🔴 Correction', 'bad'] };

// Today's 5-minute closes, with a dashed reference line (pivot or stop).
function Spark({ points, level }) {
  if (!points || points.length < 2) return <div className="muted small" style={{ height: 36, display: 'flex', alignItems: 'center' }}>no 5-minute closes yet</div>;
  const all = level != null ? [...points, level] : points;
  const lo = Math.min(...all), hi = Math.max(...all);
  const y = (v) => 22 - 20 * ((v - lo) / ((hi - lo) || 1)) + 1;
  const x = (i) => (i * 300) / (points.length - 1);
  const up = points[points.length - 1] >= points[0];
  return (
    <svg viewBox="0 0 300 24" preserveAspectRatio="none" aria-hidden="true" style={{ width: '100%', height: 36, display: 'block' }}>
      {level != null && <line x1="0" x2="300" y1={y(level)} y2={y(level)} stroke="var(--warn)" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />}
      <polyline fill="none" stroke={up ? 'var(--ok)' : 'var(--bad)'} strokeWidth="1.5" vectorEffect="non-scaling-stroke" points={points.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')} />
    </svg>
  );
}

function setupChip(l, s) {
  if (!l) return <Tag>last close</Tag>;
  if (l.verdict === 'valid') return <Tag c="ok">🟢 valid</Tag>;
  if (l.verdict === 'wait') return <Tag c="w">🟡 wait · {l.passed}/5</Tag>;
  if (l.verdict === 'skip') return <Tag c="bad">🔴 {l.vs_pivot_pct > (l.zone_top / s.pivot - 1) * 100 ? 'extended' : 'skip'}</Tag>;
  return <Tag c="w">{Math.abs(l.vs_pivot_pct).toFixed(1)}% under</Tag>;
}

// The Today page: market strip, the plan + near-pivot names with the live entry checklist, and the schedule.
// Refreshes every 30 s while NSE is open (5 min otherwise); sparklines every 5 min.
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

  if (!d) return <div className="rd"><div className={`rbanner ${err ? 'bad' : ''}`}>{err || 'Loading…'}</div></div>;
  const m = d.market || {};
  const [hl, hc] = HEALTH[m.health] || ['Market —', ''];
  const time = clock(d.as_of);
  const liveNow = d.live && d.status === 'open';
  const anyValid = d.setups.some((s) => s.live?.verdict === 'valid');
  const inPlan = d.setups.filter((s) => s.source === 'plan').length, near = d.setups.length - inPlan;
  const valid = d.setups.filter((s) => s.live?.verdict === 'valid').length;
  const sized = d.setups.filter((s) => s.live?.size);
  const risk = sized.reduce((a, s) => a + (s.live.size.risk_rs || 0), 0);
  const value = sized.reduce((a, s) => a + (s.live.size.qty || 0) * (s.live.ltp || 0), 0);
  const withResults = d.setups.filter((s) => s.results);
  const sizeNote = m.size_note ? m.size_note.charAt(0).toUpperCase() + m.size_note.slice(1) : '';

  return (
    <div className="rd">
      <Head title="Today's plan · for the next open" right={<Chip dot={liveNow ? 'on' : ''} c={d.live ? 'ok' : ''}>
        {d.live ? (d.status === 'open' ? `Live · ${time}` : `NSE ${d.status.replace('_', '-')} · ${time}`) : 'Last close · log in for live'}</Chip>}>
        <Chip c={hc}>{hl}</Chip>
        {m.nifty500_pct != null && <Chip>Nifty 500 <b className={`mono ${m.nifty500_pct >= 0 ? 'up' : 'down'}`}>{pct(m.nifty500_pct, 2)}</b></Chip>}
        {m.vix_pct != null && <Chip>VIX <b className="mono">{pct(m.vix_pct)}</b></Chip>}
        {sizeNote && <Chip c="warn">⚖️ {sizeNote}</Chip>}
      </Head>
      {d.error && <div className="rbanner bad">Live prices failed: {d.error}</div>}
      {err && <div className="rbanner bad">{err}</div>}
      <Cards n={4}>
        <Card unit="n" label="Buys in the plan" value={inPlan} num={inPlan} k={inPlan ? 'up' : ''} sub={`${near} more near the pivot · ${valid} valid right now`} live={liveNow}
          fact={d.live ? <><b>{d.setups.length}</b> names watched live</> : 'live checks need a Kite login'} />
        <Card label="Capital" value={d.capital_set ? (sized.length ? inr(value) : 'set') : 'not set'} num={value} k={d.capital_set ? '' : 'warn'}
          sub={sizeNote || (d.capital_set ? 'full size for new buys' : 'no quantity until it is set')} fact={d.capital_set ? <>in <b>{sized.length}</b> sized buys</> : 'Alerts → Size → Capital'} />
        <Card label="Worst case if stops hit" value={sized.length ? inr(-risk) : '—'} num={-risk} k={risk ? 'down' : ''} sub={`${sized.length} sized setup${sized.length === 1 ? '' : 's'} · qty × (price − stop)`}
          fact={value ? <><b>{pct(-100 * risk / value)}</b> of the money in</> : 'sizes appear once a setup is valid'} />
        <Card unit="n" label="Results this week" value={withResults.length} num={withResults.length} k={withResults.length ? 'warn' : ''}
          sub={withResults.length ? withResults.map((s) => s.symbol).join(' · ') : 'no board meeting near'} fact={withResults.length ? 'gap risk · size down?' : 'a results gap can jump the stop'} />
      </Cards>
      {d.setups.length === 0 && <div className="rbanner">No plan names or Forming names near their pivot.</div>}
      {d.setups.length > 0 && <div className="rgrid">
        {d.setups.map((s) => {
          const l = s.live, unfolded = open === s.symbol;
          const signals = l ? [l.vwap != null && <span key="v" className={l.ltp > l.vwap ? 'up' : 'down'}>VWAP {l.ltp > l.vwap ? '▲' : '▼'}</span>,
            l.pace != null && <span key="p" className={l.pace >= 1.5 ? 'up' : ''}>vol {l.pace.toFixed(1)}×</span>,
            l.vs_nifty != null && l.vs_pivot_pct > 0 && <span key="n" className={l.vs_nifty > 0 ? 'up' : 'down'}>vs Nifty {pct(l.vs_nifty)}</span>].filter(Boolean) : [];
          return (
            <div key={s.symbol} style={{ cursor: 'pointer', minWidth: 0 }} onClick={() => (l?.checks ? setOpen(unfolded ? null : s.symbol) : setSelected(s.symbol))}>
              <Panel title={<>{s.symbol} {s.tier && <span className={`tier t-${s.tier}`}>{s.tier}</span>}</>} meta={setupChip(l, s)}
                right={<><Tag c="a">{s.source === 'plan' ? 'in the plan' : 'near the pivot'}</Tag><ResultsTag r={s.results} /></>}>
                <KV pairs={[
                  ['🎯 Buy up to · trigger', l?.zone_top != null ? <>{rs(l.zone_top)} <small className="muted">· trigger {rs(s.pivot)}</small></> : <>trigger {rs(s.pivot)}</>],
                  ['Stop', <>{rs(s.stop)} <small className="muted">· {s.stop && s.pivot ? pct((s.stop / s.pivot - 1) * 100) : '—'}</small></>],
                  ['Qty · risk', l?.size ? <>{l.size.qty} <small className="muted">· {inr(l.size.risk_rs)}</small></> : '—'],
                  ['Live', l ? <><span className={l.vs_pivot_pct > 0 ? (l.verdict === 'skip' ? 'down' : 'up') : ''}>{rs(l.ltp)}</span> <small className={(l.day_pct ?? 0) >= 0 ? 'up' : 'down'}>{pct(l.day_pct)}</small>
                    <small className="muted"> · {pct(l.vs_pivot_pct)} pivot</small></> : <small className="muted">last close · log in for live</small>],
                  ...(signals.length ? [['Signals', <span className="small">{signals.map((x, i) => <React.Fragment key={i}>{i ? ' · ' : ''}{x}</React.Fragment>)}</span>]] : []),
                ]} />
                <div style={{ padding: '4px 14px 8px' }}><Spark points={sparks[s.symbol]} level={s.pivot} /></div>
                {unfolded && l?.checks && (
                  <div className="rnote" onClick={(e) => e.stopPropagation()} style={{ fontSize: 12, color: 'var(--text)' }}>
                    {l.checks.map((c) => <div key={c.id}>{c.ok ? '✅' : c.ok === null ? '⏳' : '❌'} {c.text}</div>)}
                    {l.verdict === 'valid' && <div><b>🎯 Buy up to {rs(l.zone_top)}</b> · stop {rs(s.stop)}
                      {l.size ? ` · qty ${l.size.qty} · risk ${inr(l.size.risk_rs)}` : ''}</div>}
                    <button type="button" className="btn ghost small" style={{ marginTop: 6 }} onClick={() => setSelected(s.symbol)}>Open the chart ›</button>
                  </div>)}
              </Panel>
            </div>);
        })}
      </div>}
      {anyValid && !d.capital_set && <div className="rbanner warn">Set <b>Capital</b> in Alerts → Size to see a quantity.</div>}
      <div style={{ minWidth: 0, overflow: "hidden" }}><NextUp /></div>
      <p className="muted small" style={{ margin: 0 }}>Refreshes every 30 s while NSE is open · tap a setup for its checklist, then "Open the chart".
        Unconfirmed until the close: breakouts count only on a closing basis.</p>
      {selected && <StockChart symbol={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
