import React, { useState } from 'react';

const CHIPS = [['all', 'All'], ['results', '📊 Results'], ['deals', '📦 Orders & deals'], ['red', '🚩 Red flags'],
  ['funds', '🏦 Funds'], ['other', 'Other']];
const day = (dt) => new Date(dt.length > 10 ? dt : `${dt}T00:00:00`)
  .toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });

// The News tab: exchange news on the board stocks (newest first), filterable by group.
export function NewsList({ data, onSelect, limit }) {
  const [chip, setChip] = useState('all');
  if (!data) return <p className="muted">Loading news…</p>;
  const shown = data.items.filter((i) => chip === 'all' || i.group === chip);
  return (
    <>
      <div className="chips">
        {CHIPS.filter(([k]) => k === 'all' || data.counts[k]).map(([k, label]) => (
          <button key={k} className={`chip ${chip === k ? 'on' : ''}`} onClick={() => setChip(k)}>
            {label}{k !== 'all' && ` ${data.counts[k]}`}</button>))}
      </div>
      {shown.length === 0 && (
        <p className="muted">No exchange news on the {data.scope} board stocks in the last {data.days} days.</p>)}
      {(limit ? shown.slice(0, limit) : shown).map((i, n) => (
        <div key={n} className={`feeditem news g-${i.group} ${i.side === 'sell' ? 'sell' : ''}`}
             onClick={() => onSelect(i.symbol)}>
          <span>
            {i.icon} <b>{i.symbol}</b> · {i.label} <span className="muted small">({i.why})</span>
            <span className="newstext">{i.text}</span>
          </span>
          <span className="num muted small">{day(i.dt)}
            {i.url && <> · <a href={i.url} target="_blank" rel="noreferrer"
                              onClick={(e) => e.stopPropagation()}>filing</a></>}</span>
        </div>))}
    </>
  );
}

// The Results tab: board stocks reporting within the warning window, nearest first.
export function ResultsList({ data, onSelect }) {
  if (!data) return null;
  if (!data.results.length) return <p className="muted">No board stock reports results in the next few sessions.</p>;
  return data.results.map((r) => (
    <div key={r.symbol} className={`feeditem results ${r.why === 'held' ? 'held' : ''}`} onClick={() => onSelect(r.symbol)}>
      <span>📅 <b>{r.symbol}</b> · {r.label} <span className="muted small">({r.why})</span></span>
      <span className="num small warn">gap risk</span>
    </div>));
}
