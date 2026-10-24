import React, { useCallback, useEffect, useState } from 'react';
import * as api from '../services/api';
import StockChart from './StockChart';
import StageList from './StageList';
import MarketCard from './MarketCard';
import PlanCard from './PlanCard';
import PositionsCard from './PositionsCard';

const fmtDate = (iso) => (iso ? iso.split('-').reverse().join('/') : '—');

// The stage board: four cards, what-changed feed, and a drill-down list per stage.
export default function ScreensHome({ hasData }) {
  const [screen, setScreen] = useState('vcp');
  const [summary, setSummary] = useState(null);
  const [feed, setFeed] = useState(null);
  const [stage, setStage] = useState('fresh');
  const [selected, setSelected] = useState(null);
  const [fullFeed, setFullFeed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [mkt, setMkt] = useState(null);
  const [plan, setPlan] = useState(null);

  const load = useCallback(async () => {
    try {
      const [s, f] = await Promise.all([api.screenSummary(screen), api.getChanges(screen)]);
      setSummary(s); setFeed(f); setErr('');
      api.marketHealth().then(setMkt).catch(() => setMkt(null));
      api.getPlan().then(setPlan).catch(() => setPlan(null));
    } catch (e) { setErr(e.message); }
  }, [screen]);
  useEffect(() => { load(); }, [load]);

  const rescan = async () => {
    setBusy(true);
    try { await api.runScan(); await load(); } catch (e) { setErr(e.message); }
    setBusy(false);
  };

  const c = summary?.counts || {};
  const n = summary?.fresh_sessions || 5;
  const CARDS = [
    ['forming', 'Forming', 'pausing in a tight range under its lid'],
    ['fresh', 'Fresh breakouts', `cleared it in the last ${n} sessions`],
    ['climbing', 'Climbing', 'broke out earlier — the breakout is holding'],
    ['played_out', 'Played out', 'ended recently: below the 50-day line or 8% under the pivot'],
  ];
  const rows = feed?.rows || [];

  return (
    <>
      <div className="row toolbar">
        <select value={screen} onChange={(e) => { setScreen(e.target.value); setSelected(null); }}>
          <option value="vcp">VCP</option>
          <option value="bases">Bases (all)</option>
        </select>
        <button className="btn ghost" disabled={busy} onClick={rescan}>{busy ? 'Scanning…' : 'Rescan'}</button>
        {summary?.stale && <span className="warn">Board is older than the data (latest bar {fmtDate(summary.latest_bar)}): rescan.</span>}
        {err && <span className="warn">{err}</span>}
        <span className="right updated">Updated EOD · {fmtDate(summary?.as_of)}</span>
      </div>

      <MarketCard data={mkt} />
      <PlanCard plan={plan} onSelect={setSelected} />
      <PositionsCard onSelect={setSelected} />

      <div className="panel board">
        {!summary?.as_of ? (
          <p>No scan yet. {hasData ? <>Press <b>Rescan</b>.</> : <>Load data first: <b>Data → Refresh universe → Sync candles</b>.</>}</p>
        ) : (
          <>
            <h2>{c.fresh || 0} fresh breakout{c.fresh === 1 ? '' : 's'} in the last {n} sessions.</h2>
            <p className="muted">{screen === 'vcp'
              ? 'Classic VCPs: shrinking pullbacks, drying volume, RS 70+, in an uptrend — tap a stage to see the names.'
              : 'Every base the engine finds, at the stage it has reached — tap a stage to see the names.'}</p>
            <div className="boardgrid">
              <div className="cards">
                {CARDS.map(([id, title, sub]) => (
                  <button key={id} className={`card ${stage === id ? 'active' : ''}`} onClick={() => { setStage(id); setSelected(null); }}>
                    <span className="tap">tap to view ›</span>
                    <span className="count">{c[id] ?? 0}</span>
                    <span className="title">{title}</span>
                    <span className="sub">{sub}{id === 'fresh' && c.fresh_low_volume ? ` · +${c.fresh_low_volume} on low volume` : ''}</span>
                  </button>
                ))}
              </div>
              <div className="feed">
                <div className="row"><b>What changed since last close</b>
                  <span className="muted">({fmtDate(feed?.as_of)}) · end of day · {rows.length} moves</span></div>
                {rows.length === 0 && <p className="muted">No stage moves today.</p>}
                {(fullFeed ? rows : rows.slice(0, 6)).map((r, i) => (
                  <div key={i} className={`feeditem k-${r.kind}`} onClick={() => setSelected(r.symbol)}>
                    <span>{r.text}</span>
                    <span className={`num ${r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}`}>
                      ₹{r.close?.toLocaleString('en-IN')} {r.change_pct != null && `${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(1)}%`}</span>
                  </div>
                ))}
                {rows.length > 6 && <button className="linkbtn" onClick={() => setFullFeed(!fullFeed)}>
                  {fullFeed ? 'Show less' : `Show the full feed (${rows.length}) ›`}</button>}
              </div>
            </div>
          </>
        )}
      </div>

      {selected && <StockChart symbol={selected} onClose={() => setSelected(null)} />}
      {summary?.as_of && <StageList screen={screen} stage={stage} asOf={summary.as_of} selected={selected} onSelect={setSelected} />}
    </>
  );
}
