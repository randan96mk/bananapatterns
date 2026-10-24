import React from 'react';

const ordinal = (n) => {
  const suf = ['th', 'st', 'nd', 'rd'];
  const v = n % 100;
  return n + (suf[(v - 20) % 10] || suf[v] || suf[0]);
};

// RS rating, industry rank and the Trend Template checklist for one stock.
export default function TrendPanel({ m }) {
  if (!m || !m.as_of) return <p className="muted">No RS / trend data yet: run a scan.</p>;
  const t = m.trend;
  return (
    <div className="trendpanel">
      <div className="row">
        <span className="pill big">RS {m.rs ?? 'n/a'}</span>
        {m.industry && m.industry_rank && (
          <span className="muted">{m.industry}: {ordinal(m.industry_rank)} of {m.industry_of} industries by RS</span>)}
        <span className={`tag ${t.passed_all ? 'ok' : 'warnbg'}`}>Trend Template {t.passed}/{t.of}</span>
        <span className="muted">52w high {t.pct_from_high52 > 0 ? '+' : ''}{t.pct_from_high52}% · 52w low +{t.pct_from_low52}%</span>
      </div>
      <div className="checks">
        {t.checks.map((c) => (
          <div key={c.key} className={c.pass ? 'pass' : 'fail'} title={c.value}>
            <span>{c.pass ? '✓' : '✗'}</span> {c.label} <span className="muted">({c.value})</span>
          </div>))}
      </div>
    </div>
  );
}
