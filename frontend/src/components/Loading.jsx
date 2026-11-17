import React from 'react';

// The one "Loading…" line shown while a page or the chart library downloads (code splitting).
// Same height as a panel header, muted, so nothing jumps when the real content arrives.
export default function Loading() {
  return <div className="lazyload muted small" role="status">Loading…</div>;
}
