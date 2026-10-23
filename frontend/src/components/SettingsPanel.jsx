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
};

// Generic editor for a persisted config section. Phase 2 reuses it for the VCP dials.
export default function SettingsPanel({ section = 'ingest' }) {
  const [vals, setVals] = useState(null);
  const [draft, setDraft] = useState({});
  const [msg, setMsg] = useState('');

  useEffect(() => { api.getConfig(section).then((v) => { setVals(v); setDraft(v); }).catch((e) => setMsg(e.message)); }, [section]);
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
      <h3>Settings · {section}</h3>
      <table className="grid small settings">
        <tbody>
          {Object.entries(draft).map(([k, v]) => (
            <tr key={k}>
              <td><code>{k}</code></td>
              <td>{Array.isArray(v)
                ? <textarea rows={v.length || 1} value={v.join('\n')}
                    onChange={(e) => setDraft({ ...draft, [k]: e.target.value.split('\n').filter(Boolean) })} />
                : <input value={v} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />}</td>
              <td className="muted">{HELP[k] || ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row"><button className="btn" onClick={save}>Save</button><span className="msg">{msg}</span></div>
    </div>
  );
}
