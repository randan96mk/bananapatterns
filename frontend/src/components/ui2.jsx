import React from 'react';
import { useBeat } from '../heartbeat';
import { signed } from '../fmt';
import { useNarrow } from '../useMedia';

// 🧱 Redesign building blocks (every page after Home). Wrap a page in <div className="rd"> so redesign.css applies.
// Rules baked in: a card is a fixed-height grid (label · value · sub-line · fact | corner chart); a table has fixed
// column widths and fixed row heights, numbers never wrap, and on a phone each row becomes a fixed-shape stacked card.

// `values2` is a reference series (NIFTY rebased to the same start) drawn dashed and muted on the same scale.
export function Spark({ values, values2, color = 'var(--ok)', h = 22, w = 300 }) {
  const v = (values || []).filter((x) => x != null), v2 = (values2 || []).filter((x) => x != null);
  if (v.length < 2) return null;
  const all = v2.length > 1 ? [...v, ...v2] : v;
  const lo = Math.min(...all), hi = Math.max(...all);
  const line = (a) => a.map((x, i) => `${(i / (a.length - 1)) * w},${h - 3 - ((x - lo) / (hi - lo || 1)) * (h - 6)}`).join(' ');
  return <svg className="hspark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
    {v2.length > 1 && <polyline fill="none" stroke="var(--muted)" strokeWidth="1.2" strokeDasharray="3 2" opacity=".8" vectorEffect="non-scaling-stroke" points={line(v2)} />}
    <polyline fill="none" stroke={color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" points={line(v)} /></svg>;
}

// label · value (beats when `num` changes) · sub-line · fact (bottom-left) | sparkline (bottom-right corner)
// `title` is the hover tooltip for everything that did not fit the two lines; `spark2` a dashed reference series.
export function Card({ label, value, num, sub, k, spark, spark2, stale, live, fact, onClick, unit = '₹', title }) {   // unit: '₹' | 'n' | '%'
  const beat = useBeat(num);
  const trend = spark && spark.length > 1 ? (spark[spark.length - 1] >= spark[0] ? 'var(--ok)' : 'var(--bad)') : 'var(--muted)';
  return <div className={`kpi2 hcard ${beat.on ? `beat-${beat.dir}` : ''} ${stale ? 'stale' : ''} ${onClick ? 'click' : ''}`} onClick={onClick} title={title}>
    <div className="l">{label}{live && <span className="livedot" title="live" />}{stale && <span className="warn"> · stale</span>}</div>
    <div className="vrow"><span className={`v mono ${k || ''} ${beat.on ? `flash-${beat.dir}` : ''}`}>{value}</span></div>
    {beat.delta != null && <span className={`delta ${beat.delta > 0 ? 'up' : 'down'}`}>{unit === '₹' ? signed(beat.delta) : `${beat.delta > 0 ? '+' : '−'}${Math.abs(+beat.delta.toFixed(2))}${unit === '%' ? '%' : ''}`}</span>}
    <div className="s" title={typeof sub === 'string' ? sub : undefined}>{sub}</div>
    <div className="bl" title={typeof fact === 'string' ? fact : undefined}>{fact}</div>{spark ? <Spark values={spark} values2={spark2} color={trend} /> : <span />}</div>;
}
export const Bar = ({ pct, bad, ok }) => <span className="fbar"><i style={{ width: `${Math.max(0, Math.min(100, pct || 0))}%`, background: bad ? 'var(--bad)' : ok ? 'var(--ok)' : 'var(--blue)' }} /></span>;
export const Cards = ({ children, n }) => <div className="kpis rcards" style={n ? { '--n': n } : undefined}>{children}</div>;

// A panel: title · count pill · right-hand meta; the body is the children (a table, a list, a chart…).
export function Panel({ title, meta, right, children, className = '', id }) {
  return <div className={`rpanel ${className}`} id={id}>
    {(title || right) && <div className="ph"><b>{title}</b>{meta != null && meta !== '' && <span className="n">{meta}</span>}{right && <span className="r">{right}</span>}</div>}
    {children}</div>;
}

// Fixed-width table. cols: [{h, w, n (number, right-aligned), lo (hidden on a phone), nw (dropped with its cells on a
// desktop window under 1440 px, so the first column keeps its room), ell (free text: an ellipsis is
// expected), key, onClick (a sortable header)}]; the first column takes
// the remaining width. rows: [{key, cells: [...], cls, onClick, title}], a string for a group row, or {key, span}
// for one full-width row whose content wraps (an inline editor under the row above it).
export function FixedTable({ cols: allCols, rows: allRows, empty, className = '' }) {
  const narrow = useNarrow();
  const drop = narrow && allCols.some((c) => c.nw);
  const cols = drop ? allCols.filter((c) => !c.nw) : allCols;
  const rows = drop ? (allRows || []).map((r) => (typeof r === 'string' || r.span ? r : { ...r, cells: r.cells.filter((_, i) => !allCols[i]?.nw) })) : allRows;
  if (!rows?.length) return empty ? <p className="muted small rempty">{empty}</p> : null;
  return <table className={`ftab ${className}`}><colgroup>{cols.map((c, i) => <col key={c.key || c.h || i} style={i ? { width: c.w } : undefined} className={c.lo ? 'lo' : ''} />)}</colgroup>
    <thead><tr>{cols.map((c, i) => <th key={c.key || c.h || i} className={`${c.n ? 'n' : ''} ${c.lo ? 'lo' : ''} ${c.onClick ? 'sort' : ''}`} title={c.title} onClick={c.onClick}>{c.h}</th>)}</tr></thead>
    <tbody>{rows.map((r, i) => (typeof r === 'string' ? <tr key={`g${i}`} className="grp"><td colSpan={cols.length}>{r}</td></tr>
      : r.span ? <tr key={r.key ?? i} className={`span ${r.cls || ''}`}><td colSpan={cols.length} style={{ height: 'auto', whiteSpace: 'normal', padding: '10px 14px', overflow: 'visible' }}>{r.span}</td></tr>
      : <tr key={r.key ?? i} data-key={r.key} className={`${r.cls || ''} ${r.onClick ? 'click' : ''}`} onClick={r.onClick} title={r.title}>
        {r.cells.map((v, j) => <td key={j} data-h={cols[j]?.h} className={`${cols[j]?.n ? 'n' : ''} ${cols[j]?.lo ? 'lo' : ''} ${cols[j]?.ell ? 'ell' : ''}`} title={cols[j]?.ell && typeof v === 'string' ? v : undefined}>{v}</td>)}</tr>))}</tbody></table>;
}

export const Seg = ({ opts, v, set, small }) => <span className={`seg ${small ? 'small' : ''}`}>{opts.map(([k, l, title]) => (
  <button key={k} type="button" className={v === k ? 'active' : ''} title={title} onClick={() => set(k)}>{l}</button>))}</span>;

export const Switch = ({ on, onChange, label, red, disabled, title }) => (
  <button type="button" className={`rsw ${on ? 'on' : ''} ${red ? 'red' : ''}`} role="switch" aria-checked={!!on} disabled={disabled} title={title}
    onClick={() => onChange && onChange(!on)}><i />{label}</button>);

export const Tag = ({ children, c = '', title }) => <span className={`rtag ${c}`} title={title}>{children}</span>;

// A row in a list: icon · text (+ small meta) · a button on the right.
export const Act = ({ icon, text, meta, btn, onBtn, cls = '', onClick, children }) => (
  <div className={`ract ${onClick ? 'click' : ''}`} onClick={onClick}><span className={`i ${cls}`}>{icon}</span>
    <div className="t"><div>{text}</div>{meta && <div className="m">{meta}</div>}</div>{children}
    {btn && <button type="button" className="btn ghost small" onClick={(e) => { e.stopPropagation(); onBtn && onBtn(); }}>{btn}</button>}</div>);

// label / value pairs
export const KV = ({ pairs }) => <div className="rkv">{pairs.map(([k, v], i) => <React.Fragment key={i}><div>{k}</div><div className="mono">{v}</div></React.Fragment>)}</div>;

// Line chart: series [{name, pts: number[] (null allowed), color, dash, fill}], a fixed height, x labels under it.
export function LineChart({ series, h = 180, labels = [], zero }) {
  const all = series.flatMap((s) => (s.pts || []).filter((v) => v != null));
  if (all.length < 2) return <p className="muted small rempty">Not enough points yet.</p>;
  const W = 1000, lo = Math.min(...all, zero ? 0 : Infinity), hi = Math.max(...all, zero ? 0 : -Infinity);
  const y = (v) => (h - 4 - ((v - lo) / (hi - lo || 1)) * (h - 8)).toFixed(1);
  const n = Math.max(...series.map((s) => (s.pts || []).length));
  const x = (i, len) => ((i / Math.max((len || n) - 1, 1)) * W).toFixed(1);
  const pts = (s) => s.pts.map((v, i) => (v == null ? '' : `${x(i, s.pts.length)},${y(v)}`)).join(' ');
  return <div className="rchart" style={{ '--h': `${h}px` }}>
    <svg viewBox={`0 0 ${W} ${h}`} preserveAspectRatio="none" aria-hidden="true">
      {[0.25, 0.5, 0.75].map((f) => <line key={f} x1="0" x2={W} y1={h * f} y2={h * f} className="grid" />)}
      {zero && lo < 0 && <line x1="0" x2={W} y1={y(0)} y2={y(0)} className="zero" />}
      {series.filter((s) => s.fill).map((s, k) => <polygon key={`f${k}`} fill={s.color} opacity=".14" points={`0,${h} ${pts(s)} ${W},${h}`} />)}
      {series.map((s, k) => <polyline key={k} fill="none" stroke={s.color} strokeWidth={s.dash ? 1.4 : 1.8} strokeDasharray={s.dash ? '6 4' : ''} vectorEffect="non-scaling-stroke" points={pts(s)} />)}
    </svg>
    <div className="lg">{series.map((s, k) => <span key={k}><i style={{ background: s.color }} />{s.name}</span>)}{labels.map((l, k) => <span key={`l${k}`} className="muted">{l}</span>)}</div></div>;
}

// Bars around zero (daily P&L, drawdown…), fixed height.
export function Bars({ vals, h = 120, titles }) {
  const v = (vals || []).map((x) => x || 0);
  if (!v.length) return <p className="muted small rempty">No sessions yet.</p>;
  const W = 1000, mx = Math.max(...v.map(Math.abs)) || 1, bw = W / v.length, z = h / 2;
  return <div className="rchart" style={{ '--h': `${h}px` }}><svg viewBox={`0 0 ${W} ${h}`} preserveAspectRatio="none" aria-hidden="true">
    <line x1="0" x2={W} y1={z} y2={z} className="grid" />
    {v.map((x, i) => <rect key={i} x={(i * bw + 1).toFixed(1)} width={Math.max(1, bw - 2).toFixed(1)} y={x >= 0 ? z - (Math.abs(x) / mx) * (z - 4) : z}
      height={(Math.abs(x) / mx) * (z - 4)} fill={x >= 0 ? 'var(--ok)' : 'var(--bad)'} opacity=".85"><title>{titles?.[i]}</title></rect>)}</svg></div>;
}

// Month heatmap: rows [[name, [12 values or null]]]; v in % (or any unit with `scale` = the value that is fully saturated).
export function Heat({ rows, scale = 6, fmt = (v) => `${v > 0 ? '+' : ''}${v}` }) {
  const M = ['J', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D'];
  return <div className="rheat"><div className="rl" />{M.map((m, i) => <div key={i} className="muted">{m}</div>)}
    {rows.map(([name, vals]) => <React.Fragment key={name}><div className="rl">{name}</div>
      {vals.map((v, i) => (v == null ? <div key={i} /> : <div key={i} className="mono" title={`${name} ${M[i]}: ${fmt(v)}`}
        style={{ background: `color-mix(in srgb, ${v >= 0 ? 'var(--ok)' : 'var(--bad)'} ${Math.round(12 + Math.min(1, Math.abs(v) / scale) * 60)}%, transparent)` }}>{fmt(v)}</div>))}</React.Fragment>)}</div>;
}

// A flow box: icon · title · out of in · one line · a pass bar.
export const Box = ({ icon, title, inN, outN, text, status, on, selected, onClick, children }) => (
  <button type="button" className={`rbox ${on === false ? 'off' : ''} ${selected ? 'sel' : ''}`} onClick={onClick}>
    {status && <span className={`st ${status[1] || ''}`}>{status[0]}</span>}
    <div className="t">{icon} {title}</div>
    <div className="io mono">{outN ?? '—'}{inN != null && <small> of {inN}</small>}</div>
    <div className="d">{text}</div>
    <div className="pb"><i style={{ width: `${inN ? Math.max(3, (outN / inN) * 100) : 0}%` }} /></div>{children}</button>);

export const Chip = ({ children, dot, c = '', title, onClick }) => <span className={`rchip ${c} ${onClick ? 'click' : ''}`} title={title} onClick={onClick}>{dot && <span className={`dot ${dot}`} />}{children}</span>;

// Page header: title · chips · buttons on the right.
export const Head = ({ title, children, right }) => <div className="rhead"><h1>{title}</h1>{children}{right && <span className="r">{right}</span>}</div>;
