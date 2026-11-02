import React from 'react';

// ⓘ with the explanation on hover / focus / tap (instead of grey notes repeated on every visit).
export function Tip({ text }) {
  return <span className="tip" tabIndex={0} role="note" aria-label={text}>ⓘ<span className="tipbox">{text}</span></span>;
}

// How far the last price is above the stop: red under 3%, amber under 6%, green above.
export function StopMeter({ last, stop, wide = false }) {
  if (!last || !stop) return null;
  const d = (last / stop - 1) * 100;
  const level = d < 3 ? 'red' : d < 6 ? 'amber' : 'green';
  const pos = Math.max(2, Math.min(98, d * 5));                 // 0% at the stop, 20%+ at the right end
  return (
    <span className={`stopmeter ${wide ? 'wide' : ''}`} title={`${d.toFixed(1)}% above the stop`}>
      <span className="track"><i style={{ left: `${pos}%` }} /></span>
      <span className={`dist ${level}`}>{d.toFixed(1)}%{level === 'red' ? ' ⚠' : ''}</span>
    </span>);
}

// Sub-tabs inside a page.
export function SubTabs({ tabs, value, onChange }) {
  return (
    <div className="subtabs" role="tablist">
      {tabs.map(([id, label]) => (
        <button key={id} role="tab" aria-selected={value === id} className={value === id ? 'on' : ''} onClick={() => onChange(id)}>{label}</button>))}
    </div>);
}

// v2 building blocks: a figure tile, a small line chart, an account chip.
export function Kpi({ label, value, sub, cls = '', children }) {
  return <div className="kpi2"><div className="l">{label}</div><div className={`v mono ${cls}`}>{value}</div>
    {sub && <div className="s">{sub}</div>}{children}</div>;
}

export function Spark({ values, h = 46, color = 'var(--ok)', dash = false }) {
  const v = (values || []).filter((x) => x != null);
  if (v.length < 2) return null;
  const lo = Math.min(...v), hi = Math.max(...v);
  const pts = values.map((x, i) => (x == null ? '' : `${(i / (values.length - 1)) * 600},${h - 3 - ((x - lo) / (hi - lo || 1)) * (h - 6)}`)).join(' ');
  return <svg width="100%" height={h} viewBox={`0 0 600 ${h}`} preserveAspectRatio="none" aria-hidden="true">
    <polyline fill="none" stroke={color} strokeWidth="1.8" strokeDasharray={dash ? '4 3' : ''} vectorEffect="non-scaling-stroke" points={pts} /></svg>;
}

const ACCT_CLS = { real: 'g', intraday: 'b' };
export const Acct = ({ book, name }) => <span className={`achip ${ACCT_CLS[book] || ''}`}>{name}</span>;
