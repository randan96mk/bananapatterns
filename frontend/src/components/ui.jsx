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
