import React, { useState } from 'react';
import { Act, Seg } from './ui2';

const CHIPS = [['all', 'All'], ['results', '📊 Results'], ['deals', '📦 Orders & deals'], ['red', '🚩 Red flags'],
  ['funds', '🏦 Funds'], ['other', 'Other']];
const day = (dt) => new Date(dt.length > 10 ? dt : `${dt}T00:00:00`)
  .toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
const slot = { width: 92, textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', flex: 'none' };

// The News tab: exchange news on the board stocks (newest first), filterable by group.
export function NewsList({ data, onSelect, limit }) {
  const [chip, setChip] = useState('all');
  if (!data) return <p className="muted small rempty">Loading news…</p>;
  const shown = data.items.filter((i) => chip === 'all' || i.group === chip);
  return (
    <>
      <div className="rwrap"><Seg small opts={CHIPS.filter(([k]) => k === 'all' || data.counts[k]).map(([k, l]) => [k, k === 'all' ? l : `${l} ${data.counts[k]}`])} v={chip} set={setChip} /></div>
      {shown.length === 0 && (
        <p className="muted small rempty">No exchange news on the {data.scope} board stocks in the last {data.days} days.</p>)}
      {(limit ? shown.slice(0, limit) : shown).map((i, n) => (
        <Act key={n} icon={i.icon} cls={i.group === 'red' || i.side === 'sell' ? 'warn' : i.group === 'funds' ? 'up' : 'act'} onClick={() => onSelect(i.symbol)}
          text={<><b>{i.symbol}</b> · {i.label} <span className="muted small">({i.why})</span></>} meta={i.text}>
          <span className="muted small mono" style={slot}>{day(i.dt)}
            {i.url && <> · <a href={i.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>filing</a></>}</span>
        </Act>))}
    </>
  );
}

// The Results tab: board stocks reporting within the warning window, nearest first.
export function ResultsList({ data, onSelect }) {
  if (!data) return null;
  if (!data.results.length) return <p className="muted small rempty">No board stock reports results in the next few sessions.</p>;
  return data.results.map((r) => (
    <Act key={r.symbol} icon="📅" cls={r.why === 'held' ? 'warn' : ''} onClick={() => onSelect(r.symbol)}
      text={<><b>{r.symbol}</b> · {r.label}</>} meta={r.why}>
      <span className="warn small" style={slot}>gap risk</span>
    </Act>));
}
