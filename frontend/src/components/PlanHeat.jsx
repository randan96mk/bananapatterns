import React from 'react';
import * as api from '../services/api';
import { useLiveOr } from '../services/live';
import { clock } from '../timefmt';
import { Panel, Chip, Tag, AiTag } from './ui2';
import { tone } from './AiView';

// 🔥 Research › Plan › Live heat: one tile per plan name against its levels (GET /api/plan/heat, stream topic
// `plan_heat`, 60 s while the market is open). State, colour and the marker's place on the range bar are set by code
// from the live price (the last close outside market hours); the model wrote only the tagged reason line. Display
// only: no orders, no stops. Hover = the reasons and the levels; tap = the chart.
const ORDER = { broken: 0, testing: 1, above: 2, quiet: 3, skipped: 4 };
const LABEL = { broken: 'fail level broken', testing: 'testing a level', above: 'above the pivot', quiet: 'inside the base', skipped: 'skipped today' };
const COLOR = { broken: 'var(--bad)', testing: 'var(--warn)', above: 'var(--ok)', quiet: 'var(--muted)', skipped: 'var(--muted)' };
const num = (v) => (v == null || isNaN(v) ? null : Number(v));
const rs = (v) => (v == null ? '—' : `₹${Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);
const chg = (v) => (v == null ? '' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}%`);
const clip = (x) => Math.max(2, Math.min(98, x));
const textOf = (x) => (x == null ? '' : typeof x === 'string' ? x : x.text || '');

function Range({ t, color }) {
  const L = t.levels || {};
  const stop = num(L.stop ?? t.stop), fail = num(L.fails_below ?? t.fails_below), pivot = num(L.pivot ?? t.pivot), zone = num(L.zone_top ?? t.zone_top), price = num(t.price);
  const lo = stop ?? fail ?? pivot, hi = zone ?? pivot;
  const x = (v) => (lo == null || hi == null || hi === lo ? 50 : clip(((v - lo) / (hi - lo)) * 100));
  const ticks = [[stop, 'stop'], [fail, 'fail'], [pivot, 'pivot'], [zone, 'zone']].filter(([v]) => v != null);
  const pos = t.pos != null ? clip(Number(t.pos) * 100) : price != null ? x(price) : null;
  let lastAt = -100;                                                 // a label too close to the one before is left out (its tick stays)
  const shown = ticks.map(([v, l]) => { const at = x(v), ok = at - lastAt >= 9; if (ok) lastAt = at; return [v, l, at, ok]; });
  return <div className="ht-rg" aria-hidden="true"><div className="ht-ln" />
    {shown.map(([, l, at, ok]) => <React.Fragment key={l}><i className="ht-tk" style={{ left: `${at}%` }} />{ok && <span className={`ht-lb ${l}`} style={{ left: `${at}%` }}>{l}</span>}</React.Fragment>)}
    {pos != null && <i className="ht-mk" style={{ left: `${pos}%`, background: color }} />}</div>;
}

export default function PlanHeat({ onSelect }) {
  const [d] = useLiveOr('plan_heat', api.getPlanHeat, 60000);
  if (!d || d.error) return null;
  const items = (Array.isArray(d.items) ? d.items : Array.isArray(d.tiles) ? d.tiles : []).filter((t) => t && t.symbol);
  if (!items.length) return null;
  const sorted = [...items].sort((a, b) => (ORDER[a.state] ?? 3) - (ORDER[b.state] ?? 3));
  const n = (s) => d.counts?.[s] ?? items.filter((t) => t.state === s).length;
  const live = d.live ?? items.some((t) => t.live);
  const when = live ? `${d.at ? `${clock(d.at)} · ` : ''}live · ${Math.round((d.poll_s || 60))} s` : 'last close · market closed';
  const right = <><Chip title="by code from the price against the levels"><span className="dot ok" />{n('above')} above · <span className="dot warn" />{n('testing')} testing · <span className="dot bad" />{n('broken')} broken</Chip>
    <Chip title={live ? 'live prices from the market feed' : 'outside market hours: the last close'}>{when}</Chip></>;
  return <Panel className="plan-heat" title="Live heat · the plan against its levels" meta={items.length} right={right}>
    <div className="ht-grid">{sorted.map((t) => {
      const st = ORDER[t.state] != null ? t.state : 'quiet', color = COLOR[st];
      const L = t.levels || {};
      const reason = textOf(t.reason) || textOf((t.reasons || [])[0]);
      const [vk] = tone(t.verdict);
      const tip = [`${t.symbol} · ${t.status || LABEL[st]}${t.price != null ? ` · ${rs(t.price)}` : ''}${t.change_pct != null ? ` (${chg(t.change_pct)})` : ''}`,
        ...(t.reasons || []).map((r) => `AI · ${textOf(r)}`), !(t.reasons || []).length && reason ? `AI · ${reason}` : null,
        `stop ${rs(L.stop ?? t.stop)} · fail ${rs(L.fails_below ?? t.fails_below)} · pivot ${rs(L.pivot ?? t.pivot)} · zone ${rs(L.zone_top ?? t.zone_top)}`,
        (L.support_1 ?? t.support_1) != null ? `support ${rs(L.support_1 ?? t.support_1)}` : null, (L.resistance ?? t.resistance) != null ? `resistance ${rs(L.resistance ?? t.resistance)}` : null,
        t.live === false || !live ? 'price: the last close' : null].filter(Boolean).join('\n');
      return <button key={t.symbol} type="button" className={`ht-tile ${st}`} title={tip} onClick={() => onSelect && onSelect(t.symbol)}>
        <div className="n1"><b>{t.symbol}</b><span className="mono" style={{ color }}>{chg(t.change_pct)}</span></div>
        <div className="xst"><Tag c={st === 'broken' ? 'bad' : st === 'testing' ? 'w' : st === 'above' ? 'ok' : ''}>{t.status || LABEL[st]}</Tag>
          {t.score != null && <span className="muted">AI <b className={`mono ${vk}`}>{t.score}</b> {t.verdict || ''}</span>}</div>
        <Range t={t} color={color} />
        <div className="rs">{reason ? <><AiTag of={t} /><span>{reason}</span></> : <span className="muted">no AI view for this name yet</span>}</div>
      </button>;
    })}</div>
    <div className="ht-legend"><span><i style={{ background: 'var(--ok)' }} />above the pivot</span><span><i style={{ background: 'var(--warn)' }} />testing a level (±0.5%)</span>
      <span><i style={{ background: 'var(--bad)' }} />the AI's fail level or the stop broken</span><span><i style={{ background: 'var(--muted)' }} />quiet · inside the base</span>
      <span><i style={{ background: 'var(--border)' }} />skipped this morning</span>
      <span className="r">colour and position by code from {live ? 'live prices' : 'the last close'} · the model wrote only the tagged line · hover for levels · tap for the chart</span></div>
  </Panel>;
}
