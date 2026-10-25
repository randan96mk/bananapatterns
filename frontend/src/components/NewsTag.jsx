import React from 'react';

// "📰 Order win" chip: the stock's newest important NSE filing in the last few days.
export default function NewsTag({ n }) {
  if (!n) return null;
  const d = new Date(n.dt);
  const day = d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  return (
    <span className={`tag ${n.kind === 'legal' ? 'bad' : ''}`} title={`${n.label} · ${day}: ${n.text}`}>
      📰 {n.label}
    </span>);
}
