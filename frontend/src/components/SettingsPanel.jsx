import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

const HELP = {
  history_years: 'Backfill depth in years (8 ≈ 2 Kite calls per symbol)',
  hist_rate_per_sec: 'Historical API rate (Kite limit 3/s)',
  max_retries: 'Retries for network / 429 / 5xx errors',
  backoff_base_sec: 'Backoff base seconds (doubles per retry)',
  chunk_days: 'Max days per historical call (Kite: 2000 for day candles)',
  overlap_bars: 'Bars re-read on incremental sync to detect re-adjusted history',
  adjust_tolerance: 'Close mismatch that triggers a full refetch (0.005 = 0.5%)',
  min_bars_warn: 'Flag symbols with fewer bars (young listings)',
  include_series: 'Constituent series to ingest (EQ, BE = trade-for-trade; RR = REITs excluded)',
  universe_urls: 'Nifty 500 constituents CSV sources, tried in order',
  index_symbols: 'Kite index tradingsymbols to ingest (RS / regime)',
  eod_ready_time: "IST time after which today's daily bar is treated as final",
  // bases
  pivot_basis: '"close" = highest close in the base (matches the site) or "high"',
  min_base_bars: 'Minimum base length in sessions (5 = 1 week)',
  max_base_bars: 'Maximum base length in sessions (325 = 65 weeks)',
  max_base_depth: 'Max drop from pivot to base low (0.25 = 25%)',
  max_pivot_drift: 'pivot_basis=high only: max wick drift above the first high before it counts as a trend',
  prior_advance_bars: 'Lookback for the advance into the base',
  min_prior_advance: 'Base high must be this far above the lookback low (0.20 = 20%)',
  reset_ma: 'Close below this SMA restarts the count at B1 (0 = off)',
  reset_on_undercut: 'Base low under the previous base low restarts the count (true/false)',
  vol_baseline: 'Breakout volume rule baseline: "sma50" or "median20"',
  bo_vol_mult: 'Breakout volume must be >= this x baseline to be volume-confirmed',
  weak_close_cir: 'Breakout close-in-range below this = weak close',
  squat_bars: 'Close back under pivot within N sessions after breakout = squat',
  // screen (stage board)
  min_forming_weeks: 'Forming: active base at least this many weeks',
  max_dist_to_pivot: 'Forming: close within this much under the pivot (0.05 = 5%)',
  fresh_sessions: 'Fresh breakouts: broke out within the last N sessions',
  require_volume: 'Fresh/Climbing need a volume-confirmed breakout (true/false)',
  played_out_ma: 'Run ends on a close below this SMA (0 = off)',
  played_out_drop: 'Run ends on a close this far below the pivot (0.08 = 8%)',
  played_out_window: 'Played out: run ended within the last N sessions',
  trend_ma: 'All cards: close above this SMA (0 = off)',
  min_turnover_cr: 'All cards: median daily turnover at least this (Rs crore)',
  turnover_window: 'Sessions for the median turnover',
  // trend (RS + Trend Template)
  rs_weights: 'RS weights for the periods below, one per line (default 0.4, 0.2, 0.2, 0.2)',
  rs_periods: 'RS periods in sessions, one per line (63, 126, 189, 252 = 3/6/9/12 months)',
  rs_min_bars: 'Minimum history for an RS rating',
  ma_fast: 'Fast SMA (50)', ma_mid: 'Mid SMA (150)', ma_slow: 'Slow SMA (200)',
  slow_rising_bars: 'Slow SMA must be higher than this many sessions ago (21 = ~1 month; 63 is stricter)',
  min_above_low52: 'Close at least this far above the 52-week low (0.30 = 30%)',
  max_below_high52: 'Close within this much of the 52-week high (0.25 = 25%)',
  min_rs: 'Minimum RS rating for the Trend Template',
  // vcp
  max_depth: 'VCP: base at most this deep (0.30 = 30%)',
  min_weeks: 'VCP: base at least this many weeks',
  min_contractions: 'VCP: at least this many pullbacks in the base',
  max_contractions: 'VCP: at most this many pullbacks',
  require_shrinking: 'VCP: last pullback <= shrink x the largest earlier one (true/false)',
  shrink: 'Shrink factor for the rule above (0.8)',
  max_final_contraction: 'VCP: last pullback at most this deep (0.12 = 12%)',
  max_tightening: 'VCP: ATR(recent) / base mean true range at most this',
  max_dryup: 'VCP: recent volume / volume before the base at most this',
  min_trend_checks: 'VCP: at least this many of the 8 Trend Template checks',
  atr_recent: 'Sessions for the recent ATR', dryup_recent: 'Sessions for recent volume',
  ud_window: 'Sessions for the up/down volume ratio',
  zz_min_pct: 'ZigZag: minimum reversal to confirm a swing (0.05 = 5%)',
  zz_atr_mult: 'ZigZag: or this many ATR% if larger',
  stop_buffer: 'Stop this far under the last pullback low (0.01 = 1%)',
  max_stop: 'Stop never more than this under the pivot (0.08 = 8%)',
  score_weights: 'Score weights: RS, tightening, dry-up, pivot proximity, 52w-high proximity, industry RS',
  dryup_lookback: 'Sessions before the base used for "base volume vs prior pace"',
};

const SECTIONS = ['vcp', 'screen', 'bases', 'trend', 'ingest'];

// Generic editor for persisted config sections (bases, ingest; vcp next).
export default function SettingsPanel() {
  const [section, setSection] = useState(SECTIONS[0]);
  const [vals, setVals] = useState(null);
  const [draft, setDraft] = useState({});
  const [msg, setMsg] = useState('');

  useEffect(() => {
    setVals(null); setMsg('');
    api.getConfig(section).then((v) => { setVals(v); setDraft(v); }).catch((e) => setMsg(e.message));
  }, [section]);
  if (!vals) return <div className="panel">{msg || 'Loading…'}</div>;

  const save = async () => {
    const changed = {};
    Object.keys(draft).forEach((k) => {
      if (JSON.stringify(draft[k]) !== JSON.stringify(vals[k])) changed[k] = draft[k];
    });
    try { const v = await api.putConfig(section, changed); setVals(v); setDraft(v); setMsg('Saved'); } catch (e) { setMsg(e.message); }
  };

  return (
    <div className="panel">
      <div className="row">
        <h3>Settings</h3>
        {SECTIONS.map((x) => (
          <button key={x} className={`btn ${x === section ? '' : 'ghost'}`} onClick={() => setSection(x)}>{x}</button>))}
        {section !== 'ingest' && <span className="muted">After saving, press Rescan on the Screens tab (charts update when reopened).</span>}
      </div>
      <table className="grid small settings">
        <tbody>
          {Object.entries(draft).map(([k, v]) => (
            <tr key={k}>
              <td><code>{k}</code></td>
              <td>{Array.isArray(v)
                ? <textarea rows={v.length || 1} value={v.join('\n')}
                    onChange={(e) => setDraft({ ...draft, [k]: e.target.value.split('\n').filter(Boolean) })} />
                : <input value={String(v)} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />}</td>
              <td className="muted">{HELP[k] || ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row"><button className="btn" onClick={save}>Save</button><span className="msg">{msg}</span></div>
    </div>
  );
}
