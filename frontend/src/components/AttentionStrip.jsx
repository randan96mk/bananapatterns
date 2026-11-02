import React, { useEffect, useState } from 'react';
import { useLive } from '../services/live';

const ICON = { warn: '⚠️', sell: '🔴', filter: '🟡', buy: '🟢', running: '🔵' };

// Shared: the blinking signals, from the live connection (📡 services/live.js).
export function useAttention() {
  return useLive('attention') || null;
}

const seenKey = (tab) => `bp.seen.${tab}`;
export function isSeen(tab, sig) {
  try { return !sig || localStorage.getItem(seenKey(tab)) === sig; } catch { return false; }
}
export function markSeen(tab, sig) {
  try { if (sig) localStorage.setItem(seenKey(tab), sig); } catch { /* private mode */ }
}

// The pulsing dot next to a tab name: blinks until the tab is opened, then stays (still) while the reasons hold.
export function AttentionDot({ tab, active }) {
  const a = useAttention()?.[tab];
  useEffect(() => { if (active && a?.sig) markSeen(tab, a.sig); }, [active, a?.sig, tab]);
  if (!a?.level) return null;
  const fresh = !isSeen(tab, a.sig) && !active;
  const text = a.items.map((i) => i.text);
  return <span className={`pulse ${a.level} ${fresh || a.level === 'running' ? 'blink' : ''}`} title={text.join('\n')} aria-label={text.join('. ')} />;
}

// The strip at the top of the tab: what needs a look, most urgent first.
export default function AttentionStrip({ tab }) {
  const a = useAttention()?.[tab];
  if (!a?.items?.length) return null;
  return (
    <div className={`attnstrip ${a.level}`} role="status">
      <span className={`pulse ${a.level} blink`} aria-hidden="true" />
      {a.items.map((i) => <span key={i.text} className="attnitem">{ICON[i.level]} {i.text}</span>)}
    </div>
  );
}
