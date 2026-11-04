import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import ResultsTag from './ResultsTag';
import NewsTag from './NewsTag';
import FlagTags from './FlagTags';

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
      <defs><marker id="mc-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5"
                    orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" className="mc-arrowhead" /></marker></defs>
      {r.flag && (() => {                                    // flag: P -> T pole arrow, then the flag channel
        const pl = r.flag.pole, fl = r.flag.flag;
        const P = idx(pl.start), T = idx(pl.top);
        const [[ta, y0], [te, y1]] = fl.top_line, [[, b0_], [, b1_]] = fl.bot_line;
        const a = idx(ta), e = idx(te);
        return (<>
          {P >= 0 && T >= 0 && <line className="mc-pole" x1={x(P)} y1={y(pl.low)} x2={x(T)} y2={y(pl.high)}
                                     markerEnd="url(#mc-arrow)" />}
          {a >= 0 && e >= 0 && <>
            <polygon className="mc-flag-fill" points={`${x(a)},${y(y0)} ${x(e)},${y(y1)} ${x(e)},${y(b1_)} ${x(a)},${y(b0_)}`} />
            <line className="mc-flag" x1={x(a)} y1={y(y0)} x2={x(e)} y2={y(y1)} />
            <line className="mc-flag" x1={x(a)} y1={y(b0_)} x2={x(e)} y2={y(b1_)} />
          </>}
        </>);
      })()}
      {!r.flag && b0 >= 0 && b1 >= b0 && r.base_low != null && r.pivot != null && (
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
  const flag = r.flag;
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
          (isVcp || flag) && r.score != null && `Score ${r.score.toFixed(0)}`].filter(Boolean).join(' · ')}
      </div>
      {isVcp && (pulls || r.vcp?.dryup != null) && (
        <div className="muted">{[pulls && `Pullbacks ${pulls}`, r.vcp?.dryup != null && `Dry-up ${x2(r.vcp.dryup)}`,
          r.vcp?.stop && `Stop ${rs(r.vcp.stop)}`].filter(Boolean).join(' · ')}</div>)}
      {flag && (
        <div className="muted">{`Pole +${flag.pole.gain_pct.toFixed(0)}% · ${flag.pole.bars}d · flag −${flag.flag.depth_pct.toFixed(1)}%`
          + ` · ${flag.flag.bars}d${flag.flag.volume_x != null ? ` · vol ${x2(flag.flag.volume_x)}` : ''} · stop ${rs(flag.stop)}`}</div>)}
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
              {r.tier && <span className={`tier t-${r.tier}`} title={r.tier === 'A' ? 'A setup: alerts at once'
                : 'B setup: digest only'}>{r.tier}</span>}
              <b>{r.symbol}</b>{screen === 'bases' && r.vcp_tag && <span className="tag ok">VCP</span>}
              {screen === 'bases' && r.flag_tag && <span className="tag ok">Flag</span>}
              {r.flag?.htf && <span className="tag ok" title="High-tight flag: pole +90% within 8 weeks">HTF</span>}
              {r.flag?.tight && !r.flag?.htf && <span className="tag" title="Gave back 25% of the pole or less">tight</span>}
              {r.flag?.post_results && <span className="tag" title="Results came out during the pole">post-results</span>}
              <span className="right muted small">{r.base_label}</span>
            </div>
            <div className="muted small sc-sub">{r.company}{r.industry ? ` · ${r.industry}` : ''}</div>
            {sparks ? <MiniChart s={sparks[r.symbol]} r={r} /> : <div className="minichart empty muted small">loading…</div>}
            <div className="sc-price">
              <b>{rs(r.close)}</b>
              <span className={r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}>{pct(r.change_pct)}</span>
              {tight && <span className="tag warnbg">near pivot</span>}
              <ResultsTag r={r.results} />
              <NewsTag n={r.news} />
              <FlagTags f={r.flags} />
            </div>
            <Facts r={r} stage={stage} isVcp={isVcp} />
          </button>);
      })}
    </div>
  );
}
