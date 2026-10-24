import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

const pct = (v) => (v == null ? '' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);
const rs = (v) => (v == null ? '' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);
const x2 = (v) => (v == null ? '' : `${v.toFixed(2)}×`);
const W = 300, H = 120, PAD = 4;

// Small SVG candle chart: base box, pivot line, breakout ▲, exit ✕, 50-day line.
function MiniChart({ s, r }) {
  if (!s || !s.c.length) return <div className="minichart empty muted small">no chart data</div>;
  const n = s.c.length;
  const lo = Math.min(...s.l, r.base_low ?? Infinity), hi = Math.max(...s.h, r.pivot ?? -Infinity);
  const y = (v) => PAD + (H - 2 * PAD) * (1 - (v - lo) / (hi - lo || 1));
  const step = W / n, bw = Math.max(1, step * 0.6);
  const x = (i) => i * step + step / 2;
  const idx = (d) => s.d.indexOf(d);
  let b0 = r.base_start ? idx(r.base_start) : -1;
  if (b0 < 0 && r.base_start && r.base_start < s.d[0]) b0 = 0;              // base began before the window
  const b1 = r.base_end ? idx(r.base_end) : -1;
  const bo = r.breakout_date ? idx(r.breakout_date) : -1;
  const ex = r.ended_date ? idx(r.ended_date) : -1;
  const ma = s.ma50.map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean);
  return (
    <svg className="minichart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
         aria-label={`${r.symbol} last ${n} sessions`}>
      {b0 >= 0 && b1 >= b0 && r.base_low != null && r.pivot != null && (
        <rect x={b0 * step} y={y(r.pivot)} width={(b1 - b0 + 1) * step} height={y(r.base_low) - y(r.pivot)}
              className="mc-box" />)}
      {r.pivot != null && <line x1={b0 >= 0 ? b0 * step : 0} x2={W} y1={y(r.pivot)} y2={y(r.pivot)} className="mc-pivot" />}
      {ma.length > 1 && <polyline points={ma.join(' ')} className="mc-ma" />}
      {s.c.map((c, i) => {
        const up = c >= s.o[i];
        return (
          <g key={i} className={up ? 'mc-up' : 'mc-down'}>
            <line x1={x(i)} x2={x(i)} y1={y(s.h[i])} y2={y(s.l[i])} />
            <rect x={x(i) - bw / 2} width={bw} y={y(Math.max(c, s.o[i]))}
                  height={Math.max(0.8, Math.abs(y(c) - y(s.o[i])))} />
          </g>);
      })}
      {bo >= 0 && (() => {                                   // ▲ above the breakout bar
        const cx = x(bo), top = Math.max(1, y(s.h[bo]) - 9);
        return <polygon points={`${cx - 4},${top + 7} ${cx + 4},${top + 7} ${cx},${top}`}
                        className={`mc-bo ${r.volume_ok === false ? 'weak' : ''}`} />;
      })()}
      {ex >= 0 && (() => {                                   // ✕ under the exit bar
        const cx = x(ex), cy = Math.min(H - 5, y(s.l[ex]) + 7);
        return <path d={`M${cx - 3},${cy - 3}L${cx + 3},${cy + 3}M${cx + 3},${cy - 3}L${cx - 3},${cy + 3}`} className="mc-ex" />;
      })()}
    </svg>
  );
}

// The stage-specific lines under the chart.
function Facts({ r, stage, isVcp }) {
  const pulls = (r.vcp?.contractions || []).map((c) => Math.round(c.depth_pct)).join('→');
  const lines = {
    forming: [`${r.dist_to_pivot_pct?.toFixed(1)}% under ${rs(r.pivot)}`, `Depth ${r.depth_pct?.toFixed(1)}%`],
    fresh: [`Broke out ${r.breakout_date} · ${pct(r.gain_from_pivot_pct)} over ${rs(r.pivot)}`,
      `Volume ${r.volume_ratio?.toFixed(1)}×`],
    climbing: [`${r.sessions_since_breakout} sessions since ${r.breakout_date}`,
      `${pct(r.gain_from_pivot_pct)} from pivot · peak ${pct(r.peak_gain_pct)}`],
    played_out: [`Ended ${r.ended_date}: ${r.ended_reason}`,
      `At end ${pct(r.gain_from_pivot_pct)} · peak ${pct(r.peak_gain_pct)}`],
  };
  const st = stage === 'fresh_low_volume' ? 'fresh' : stage;
  return (
    <div className="facts small">
      {(lines[st] || []).map((l) => <div key={l}>{l}</div>)}
      <div className="muted">
        {[r.rs != null && `RS ${r.rs}`, r.tt_of && `Trend ${r.tt_passed}/${r.tt_of}`,
          isVcp && r.score != null && `Score ${r.score.toFixed(0)}`].filter(Boolean).join(' · ')}
      </div>
      {isVcp && (pulls || r.vcp?.dryup != null) && (
        <div className="muted">{[pulls && `Pullbacks ${pulls}`, r.vcp?.dryup != null && `Dry-up ${x2(r.vcp.dryup)}`,
          r.vcp?.stop && `Stop ${rs(r.vcp.stop)}`].filter(Boolean).join(' · ')}</div>)}
    </div>
  );
}

export default function StageGrid({ screen, stage, asOf, rows, selected, onSelect }) {
  const isVcp = screen === 'vcp';
  const [sparks, setSparks] = useState(null);
  useEffect(() => {
    setSparks(null);
    api.screenSparks(screen, stage).then((d) => setSparks(d.sparks)).catch(() => setSparks({}));
  }, [screen, stage, asOf]);
  return (
    <div className="stockgrid">
      {rows.map((r) => {
        const tight = stage === 'forming' && r.dist_to_pivot_pct != null && r.dist_to_pivot_pct <= 3;
        return (
          <button key={r.symbol} onClick={() => onSelect(r.symbol)}
                  className={`stockcard s-${stage} ${tight ? 'tight' : ''} ${r.symbol === selected ? 'sel' : ''}`}>
            <div className="sc-head">
              <b>{r.symbol}</b>{!isVcp && r.vcp_tag && <span className="tag ok">VCP</span>}
              <span className="right muted small">{r.base_label}</span>
            </div>
            <div className="muted small sc-sub">{r.company}{r.industry ? ` · ${r.industry}` : ''}</div>
            {sparks ? <MiniChart s={sparks[r.symbol]} r={r} /> : <div className="minichart empty muted small">loading…</div>}
            <div className="sc-price">
              <b>{rs(r.close)}</b>
              <span className={r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}>{pct(r.change_pct)}</span>
              {tight && <span className="tag warnbg">near pivot</span>}
            </div>
            <Facts r={r} stage={stage} isVcp={isVcp} />
          </button>);
      })}
    </div>
  );
}
