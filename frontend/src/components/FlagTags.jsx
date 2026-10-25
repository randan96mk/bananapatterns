import React from 'react';

// 🚩 red flag (promoter selling, legal action, auditor exit, downgrade) and 🏦 fund buying / selling chips.
export default function FlagTags({ f }) {
  if (!f) return null;
  const chip = (i, cls, icon) => (
    <span key={`${icon}${i.label}`} className={`tag ${cls}`} title={i.text}>{icon} {i.label}</span>);
  return (
    <>
      {f.red?.[0] && chip(f.red[0], 'bad', '🚩')}
      {f.buy?.[0] && chip(f.buy[0], 'ok', '🏦')}
      {f.sell?.[0] && chip(f.sell[0], 'warnbg', '🏦')}
    </>
  );
}
