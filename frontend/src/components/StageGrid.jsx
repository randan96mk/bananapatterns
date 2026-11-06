import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { Tag } from './ui2';

const pct = (v) => (v == null ? '' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);
const rs = (v) => (v == null ? '—' : v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const x2 = (v) => (v == null ? '' : `${v.toFixed(2)}×`);
const W = 96, H = 38, PAD = 3;
const LINE = { forming: 'var(--warn)', fresh: 'var(--ok)', fresh_low_volume: 'var(--warn)', climbing: 'var(--blue)', played_out: 'var(--bad)' };

// The corner chart of a stage card: closes, the 50-day line, the base box, the pivot (dashed), ▲ at the breakout, ✕ at the exit.
function MiniChart({ s, r, stage }) {
  if (!s || !s.c.length) return null;
  const n = s.c.length;
  const lo = Math.min(...s.l, r.base_low ?? Infinity), hi = Math.max(...s.h, r.pivot ?? -Infinity);
  const y = (v) => PAD + (H - 2 * PAD) * (1 - (v - lo) / (hi - lo || 1));
  const x = (i) => (i / Math.max(n - 1, 1)) * W;
  const idx = (d) => s.d.indexOf(d);
  let b0 = r.base_start ? idx(r.base_start) : -1;
  if (b0 < 0 && r.base_start && r.base_start < s.d[0]) b0 = 0;              // base began before the window
  const b1 = r.base_end ? idx(r.base_end) : -1;
  const bo = r.breakout_date ? idx(r.breakout_date) : -1;
  const ex = r.ended_date ? idx(r.ended_date) : -1;
  const line = (arr) => arr.map((v, i) => (v == null ? '' : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join(' ');
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={`${r.symbol} last ${n} sessions`}>
      {!r.flag && b0 >= 0 && b1 >= b0 && r.base_low != null && r.pivot != null && (
        <rect x={x(b0)} y={y(r.pivot)} width={x(b1) - x(b0)} height={Math.max(0, y(r.base_low) - y(r.pivot))} fill="var(--blue)" opacity=".12" />)}
      {s.ma50?.some((v) => v != null) && <polyline fill="none" stroke="var(--muted)" strokeWidth="0.8" opacity=".7" points={line(s.ma50)} />}
      {r.pivot != null && <line x1="0" x2={W} y1={y(r.pivot)} y2={y(r.pivot)} stroke="var(--warn)" strokeWidth="0.8" strokeDasharray="3 3" />}
      <polyline fill="none" stroke={LINE[stage] || 'var(--ok)'} strokeWidth="1.6" vectorEffect="non-scaling-stroke" points={line(s.c)} />
      {bo >= 0 && <polygon points={`${x(bo) - 3},${Math.max(1, y(s.h[bo]) - 2)} ${x(bo) + 3},${Math.max(1, y(s.h[bo]) - 2)} ${x(bo)},${Math.max(1, y(s.h[bo]) - 7)}`}
        fill={r.volume_ok === false ? 'var(--warn)' : 'var(--ok)'} />}
      {ex >= 0 && (() => { const cx = x(ex), cy = Math.min(H - 3, y(s.l[ex]) + 4);
        return <path d={`M${cx - 2.5},${cy - 2.5}L${cx + 2.5},${cy + 2.5}M${cx + 2.5},${cy - 2.5}L${cx - 2.5},${cy + 2.5}`} stroke="var(--bad)" strokeWidth="1.2" />; })()}
    </svg>
  );
}

const dayOf = (iso) => (iso ? new Date(`${iso}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
// The chips a stock carries, as one text line: results date, newest filing, red flag / fund buying or selling, flag tags.
export function tagLine(r) {
  const f = r.flags || {};
  return [r.results && `📅 results ${dayOf(r.results.date)}`, r.news && `📰 ${r.news.label}`, f.red?.[0] && `🚩 ${f.red[0].label}`,
    f.buy?.[0] && `🏦 ${f.buy[0].label}`, f.sell?.[0] && `🏦 selling · ${f.sell[0].label}`, r.flag?.htf && 'HTF', r.flag?.tight && !r.flag?.htf && 'tight flag',
    r.flag?.post_results && 'post-results'].filter(Boolean).join(' · ');
}

// The fact lines of a card: [stage facts, RS · trend · score, VCP / flag details, tags]. Each one stays one line (ellipsis).
export function factLines(r, stage, isVcp) {
  const flag = r.flag;
  const pulls = (r.vcp?.contractions || []).map((c) => Math.round(c.depth_pct)).join(' → ');
  const st = stage === 'fresh_low_volume' ? 'fresh' : stage;
  const main = {
    forming: [`${r.dist_to_pivot_pct?.toFixed(1)}% under the pivot`, `depth ${r.depth_pct?.toFixed(1)}%`],
    fresh: [`broke out ${r.breakout_date}`, `${pct(r.gain_from_pivot_pct)} over the pivot`, `vol ${r.volume_ratio?.toFixed(1)}×`],
    climbing: [`${r.sessions_since_breakout} sessions since ${r.breakout_date}`, `${pct(r.gain_from_pivot_pct)} from pivot`, `peak ${pct(r.peak_gain_pct)}`],
    played_out: [`ended ${r.ended_date}: ${r.ended_reason}`, `at end ${pct(r.gain_from_pivot_pct)}`, `peak ${pct(r.peak_gain_pct)}`],
  }[st] || [];
  const stats = [r.rs != null && `RS ${r.rs}`, r.tt_of && `trend ${r.tt_passed}/${r.tt_of}`,
    (isVcp || flag) && r.score != null && `score ${r.score.toFixed(0)}`, r.industry].filter(Boolean);
  const detail = isVcp && (pulls || r.vcp?.dryup != null)
    ? [pulls && `pullbacks ${pulls}%`, r.vcp?.dryup != null && `dry-up ${x2(r.vcp.dryup)}`, r.vcp?.stop && `stop ${rs(r.vcp.stop)}`].filter(Boolean)
    : flag ? [`pole +${flag.pole.gain_pct.toFixed(0)}% · ${flag.pole.bars}d`, `flag −${flag.flag.depth_pct.toFixed(1)}% · ${flag.flag.bars}d`,
      flag.flag.volume_x != null && `vol ${x2(flag.flag.volume_x)}`, `stop ${rs(flag.stop)}`].filter(Boolean) : [];
  return [main.join(' · '), stats.join(' · '), detail.join(' · '), tagLine(r)].filter(Boolean);
}

// Stage cards (class rsc): name · screen tag · stage tag · base length | facts | close · pivot (bottom-left) | chart (bottom-right).
export default function StageGrid({ screen, stage, asOf, rows, selected, onSelect }) {
  const isVcp = screen === 'vcp';
  const [sparks, setSparks] = useState(null);
  useEffect(() => {
    setSparks(null);
    api.screenSparks(screen, stage).then((d) => setSparks(d.sparks)).catch(() => setSparks({}));
  }, [screen, stage, asOf]);
  const scrTag = (r) => (screen === 'vcp' || (screen === 'bases' && r.vcp_tag) ? 'VCP' : screen === 'flag' || (screen === 'bases' && r.flag_tag) ? 'Flag' : 'Bases');
  return (
    <div className="rgrid">
      {rows.map((r) => {
        const tight = stage === 'forming' && r.dist_to_pivot_pct != null && r.dist_to_pivot_pct <= 3;
        const lines = factLines(r, stage, isVcp);
        return (
          <button key={r.symbol} type="button" onClick={() => onSelect(r.symbol)} className={`rsc ${r.symbol === selected ? 'sel' : ''}`} title={r.company}>
            <div className="nm">
              {r.tier && <span className={`tier t-${r.tier}`} title={r.tier === 'A' ? 'A setup: alerts at once' : 'B setup: digest only'}>{r.tier}</span>}
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}>{r.symbol}</span><Tag c="a">{scrTag(r)}</Tag>
              {tight && <Tag c="w">near pivot</Tag>}
              <small style={{ overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}>{r.base_label}</small>
            </div>
            {lines.map((l, i) => <div key={l} className="r" title={l} style={i >= 3 ? { paddingRight: 104 } : undefined}>{l}</div>)}
            <div className="pv">close <b className="mono">{rs(r.close)}</b> <span className={`mono ${r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}`}>{pct(r.change_pct)}</span> · pivot <span className="mono">{rs(r.pivot)}</span></div>
            {sparks && <MiniChart s={sparks[r.symbol]} r={r} stage={stage} />}
          </button>);
      })}
    </div>
  );
}
