import React from 'react';

// "📅 28 Oct" chip for a stock's next results (NSE board meeting); amber when within the warning window.
export default function ResultsTag({ r }) {
  if (!r) return null;
  const d = new Date(`${r.date}T00:00:00`);
  const day = d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  return (
    <span className={`tag ${r.warn ?? true ? 'warnbg' : ''}`}
          title={`${r.label || `Results ${day}`}: a results gap can jump past the stop`}>
      📅 Results {day}
    </span>
  );
}
