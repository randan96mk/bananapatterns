import React, { useEffect, useMemo, useRef, useState } from 'react';

// ⌘K / Ctrl+K / "/": jump to any page, or open any stock of the universe (its chart, filings and news).
export default function CommandPalette({ sections, symbols, onClose, onPage, onStock }) {
  const [q, setQ] = useState('');
  const [k, setK] = useState(0);
  const input = useRef(null);
  useEffect(() => { input.current?.focus(); }, []);
  const items = useMemo(() => {
    const t = q.trim().toLowerCase();
    const pages = sections.flatMap(([, title, ps]) => ps.map(([id, label]) => ({ kind: 'page', id, label, hint: title })))
      .filter((p) => !t || `${p.label} ${p.hint}`.toLowerCase().includes(t));
    const stocks = t ? symbols.filter((s) => s.toLowerCase().startsWith(t)).slice(0, 8)
      .concat(symbols.filter((s) => !s.toLowerCase().startsWith(t) && s.toLowerCase().includes(t)).slice(0, 4))
      .map((s) => ({ kind: 'stock', id: s, label: s, hint: 'stock' })) : [];
    return [...stocks, ...pages].slice(0, 14);
  }, [q, sections, symbols]);
  useEffect(() => { setK(0); }, [q]);
  const pick = (it) => (it.kind === 'stock' ? onStock(it.id) : onPage(it.id));
  const key = (e) => {
    if (e.key === 'Escape') onClose();
    else if (e.key === 'ArrowDown') { e.preventDefault(); setK((x) => Math.min(x + 1, items.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setK((x) => Math.max(x - 1, 0)); }
    else if (e.key === 'Enter' && items[k]) pick(items[k]);
  };
  return (
    <div className="cmdk-back" onClick={onClose} role="presentation">
      <div className="cmdk" role="dialog" aria-label="Search" onClick={(e) => e.stopPropagation()}>
        <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={key}
          placeholder="Search a stock or a page…  (Esc to close)" aria-label="Search" />
        <div className="items">
          {items.length === 0 && <div className="item muted">Nothing found</div>}
          {items.map((it, i) => (
            <div key={`${it.kind}${it.id}`} className={`item ${i === k ? 'on' : ''}`} onMouseEnter={() => setK(i)} onClick={() => pick(it)}>
              <span>{it.kind === 'stock' ? '◆' : '→'}</span><b>{it.label}</b><span className="k">{it.hint}</span></div>))}
        </div>
      </div>
    </div>);
}
