import React, { useState } from 'react';
import { inr, pct, cls, signed } from '../fmt';
import { Tag } from './ui2';

// 📱 Home's open positions on a phone (≤ 760 px): one scrollable chip line (filter + sort) and a book picker, a
// "net · worst case" line, one 44 px fold row per book (Real open by default, Paper E+ and Intraday folded; the
// fold state is remembered), and inside it one fixed 46 px row per stock: name + "qty · now" | P&L + % | a 56 px
// stop meter + distance. 10 rows then "show n more"; tapping a row opens a details block (entry, stop, days, trail,
// results) with chart / ✎ edit (Real) / page buttons. Styles are inline: nothing here depends on a number's length.
const BOOKS = [['real', 'Real', 'real'], ['paper_E', 'Paper E+', 'paper'], ['futures', 'Futures', 'futures'], ['options', 'Options', 'options'], ['intraday', 'Intraday', 'flow']];
const FOLDS = 'bp.home.folds';
const readFolds = () => { try { return { real: true, paper_E: false, intraday: false, ...JSON.parse(localStorage.getItem(FOLDS) || '{}') }; } catch { return { real: true, paper_E: false, intraday: false }; } };
const dayName = (s) => (s ? new Date(s.length > 10 ? s : `${s}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '—');
const daysSince = (s) => (s ? Math.max(0, Math.round((Date.now() - new Date(`${s.slice(0, 10)}T00:00:00`)) / 86400000)) : null);
const ELL = { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 };
const SMALL = { display: 'block', fontSize: 10.5, color: 'var(--muted)', ...ELL };
const chip = (on) => ({ flex: 'none', fontSize: 11.5, padding: '3px 9px', border: '1px solid var(--border)', borderRadius: 12, color: on ? 'var(--text)' : 'var(--muted)', background: on ? 'var(--bg)' : 'transparent', fontWeight: on ? 600 : 400, cursor: 'pointer', whiteSpace: 'nowrap' });
const BTN = { padding: '3px 9px' };

// the 56 px stop meter: 0% at the stop, 20%+ at the right end (the same scale as StopMeter)
const Meter = ({ d }) => (d == null ? <small style={{ color: 'var(--muted)' }}>—</small> : <>
  <span style={{ display: 'block', width: 56, height: 5, borderRadius: 3, marginLeft: 'auto', marginBottom: 2, position: 'relative', background: 'linear-gradient(90deg, var(--bad), var(--warn), var(--ok))' }}>
    <i style={{ position: 'absolute', top: -3, width: 2, height: 11, background: 'var(--text)', left: `${Math.max(2, Math.min(98, d * 5))}%` }} /></span>
  <small className={d < 3 ? 'down' : d < 6 ? 'warn' : 'up'} style={{ fontSize: 10.5 }}>{d.toFixed(1)}%</small></>);

function Row({ p, open, onOpen, f }) {
  return <div className="ppr" data-key={p.book + p.symbol} onClick={onOpen} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 84px 92px', gap: 6, alignItems: 'center', height: 46, padding: '0 10px', borderBottom: open ? 0 : '1px solid var(--rowline)', background: open ? 'var(--bg)' : undefined, cursor: 'pointer' }}>
    <div style={ELL}><b style={{ fontSize: 13 }}>{p.symbol}</b>{p.stop_dist != null && p.stop_dist < 2 && <Tag c="w">near</Tag>}{f === 'new' && <Tag c="a">new</Tag>}<small style={SMALL}>{p.qty} · now {p.now ?? '—'}</small></div>
    <div style={{ textAlign: 'right', ...ELL }}><b className={`mono ${cls(p.pnl)} ${f && f !== 'new' ? `flash-${f}` : ''}`} style={{ fontSize: 12.5 }}>{signed(p.pnl)}</b><small className={cls(p.pct)} style={{ ...SMALL, color: undefined }}>{pct(p.pct)}</small></div>
    <div style={{ textAlign: 'right', ...ELL }}><Meter d={p.stop_dist} /></div>
  </div>;
}

function Details({ p, onStock, onGo }) {
  const t = p.trail, page = BOOKS.find(([b]) => b === p.book)?.[2] || 'portfolio';
  const trail = !t ? '—' : t.active ? `active${t.raised ? ' · raised' : ''}` : `${pct(p.pct, 1).replace('%', '')} · arms +${Math.round((p.pct || 0) + (t.pct_to_arm || 0))}`;
  const cell = (k, v) => <div style={ELL}><span style={{ display: 'block', color: 'var(--muted)', fontSize: 9.5, textTransform: 'uppercase', letterSpacing: '.04em' }}>{k}</span><b>{v}</b></div>;
  return <div className="pdx" style={{ background: 'var(--bg)', display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '4px 8px', padding: '6px 10px 10px', borderBottom: '1px solid var(--rowline)', fontSize: 11 }}>
    {cell('Entry', p.entry ?? '—')}{cell('Stop', p.stop ?? '—')}{cell('Days', daysSince(p.opened) ?? '—')}
    {cell('Trail', trail)}{cell('Results', p.results_date ? `${dayName(p.results_date)}${p.results_in != null ? ` · ${p.results_in}s` : ''}` : '—')}{cell('Book', p.account)}
    <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 6, marginTop: 4 }}>
      <button type="button" className="btn small" style={BTN} onClick={(e) => { e.stopPropagation(); onStock(p.symbol); }}>chart</button>
      {p.book === 'real' && <button type="button" className="btn small" style={BTN} title="correct qty / entry / stop on the Real page" onClick={(e) => { e.stopPropagation(); try { sessionStorage.setItem('bp.real.edit', p.symbol); } catch { /* private mode */ } onGo('real'); }}>✎ edit</button>}
      <button type="button" className="btn small" style={BTN} onClick={(e) => { e.stopPropagation(); onGo(page); }}>{BOOKS.find(([b]) => b === p.book)?.[1] || 'Portfolio'} page →</button>
    </div></div>;
}

export default function PhonePositions({ positions, groups, counts, filter, setFilter, sort, setSort, net, worstCase, flash, onStock, onGo, differs }) {
  const [folds, setFoldsRaw] = useState(readFolds);
  const [more, setMore] = useState({});
  const [open, setOpen] = useState(null);
  const setFolds = (b) => setFoldsRaw((f) => { const n = { ...f, [b]: !f[b] }; try { localStorage.setItem(FOLDS, JSON.stringify(n)); } catch { /* private mode */ } return n; });
  const nextSort = () => setSort((s) => (s.key === 'pnl' ? { key: 'pct', dir: 'desc' } : s.key === 'pct' ? { key: 'stop', dir: 'asc' } : { key: 'pnl', dir: 'desc' }));
  const sortLabel = { pnl: 'P&L ▾', pct: '% ▾', stop: 'Stop ▴' }[sort.key] || 'P&L ▾';
  const byBook = Object.fromEntries(groups);
  const all = (b) => positions.filter((p) => p.book === b);
  return <div>
    <div style={{ display: 'flex', gap: 6, padding: '6px 10px', overflowX: 'auto', borderBottom: '1px solid var(--border)', scrollbarWidth: 'none' }}>
      {[['all', `All ${positions.length}`], ['near', `Near stop ${counts.near}`], ['trail', `Trail armed ${counts.trail}`], ['results', `Results ${counts.results}`]].map(([k, l]) =>
        <span key={k} role="button" style={chip(filter === k)} onClick={() => setFilter(k)}>{l}</span>)}
      <span role="button" style={chip(false)} onClick={nextSort} title="sort inside each book">{sortLabel}</span>
    </div>
    <div style={{ fontSize: 10.5, color: 'var(--muted)', padding: '6px 10px', ...ELL }}>net <b className={cls(net)}>{signed(net)}</b>{worstCase != null && <> · worst case <b className="down">{signed(worstCase)}</b></>} · tap a row for details</div>
    {BOOKS.map(([b, name]) => { const ps = byBook[b] || [], every = all(b), on = !!folds[b], shown = more[b] ? ps : ps.slice(0, 10);
      const bookNet = every.reduce((a, p) => a + (p.pnl || 0), 0), inUse = every.reduce((a, p) => a + (p.entry || 0) * (p.qty || 0), 0);
      const near = every.filter((p) => p.stop_dist != null && p.stop_dist < 2).length;
      const sub = b === 'intraday' ? (every.length ? `${every.length} open · out by 3:15 pm` : 'out by 3:15 pm') : b === 'options' ? (every.length ? `${every.length} legs · out by 3:10 pm` : 'out by 3:10 pm') : near ? `${near} near stop` : b === 'paper_E' && every.length && every.every((p) => !differs(p)) ? 'same names as Real' : every.length ? 'none near a stop' : 'nothing open';
      return <div key={b}>
        <div className="pfold" data-book={b} data-open={on ? '1' : '0'} role="button" onClick={() => setFolds(b)} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0 10px', height: 44, borderBottom: '1px solid var(--rowline)', cursor: 'pointer' }}>
          <span style={{ width: 12, color: 'var(--muted)' }}>{on ? '▾' : '▸'}</span><b style={{ fontSize: 13 }}>{name}</b>
          <span style={{ fontSize: 11, color: 'var(--muted)', ...ELL }}>{every.length ? `${every.length} · ${inr(inUse)} ${b === 'options' ? 'premium' : 'in use'}` : '0 open'}</span>
          <span style={{ marginLeft: 'auto', textAlign: 'right', fontSize: 12, flex: 'none' }}><span className={every.length ? cls(bookNet) : 'muted'}>{every.length ? signed(bookNet) : '—'}</span><small style={{ ...SMALL, textAlign: 'right' }}>{sub}</small></span>
        </div>
        {on && shown.map((p) => { const k = p.book + p.symbol; return <React.Fragment key={k}><Row p={p} f={flash[k]} open={open === k} onOpen={() => setOpen(open === k ? null : k)} />{open === k && <Details p={p} onStock={onStock} onGo={onGo} />}</React.Fragment>; })}
        {on && every.length > 0 && !ps.length && <div style={{ fontSize: 10.5, color: 'var(--muted)', padding: '6px 10px' }}>Nothing matches this filter.</div>}
        {on && ps.length > 10 && !more[b] && <div role="button" onClick={() => setMore((m) => ({ ...m, [b]: true }))} style={{ fontSize: 10.5, color: 'var(--muted)', padding: '6px 10px', textAlign: 'center', cursor: 'pointer' }}>show {ps.length - 10} more ▾</div>}
      </div>; })}
  </div>;
}
