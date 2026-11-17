import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { clock } from '../timefmt';
import { Panel, FixedTable, AiTag } from './ui2';
import { AiView } from './AiView';

// 🌙 Home › Overnight: which AI ratings moved since the night before (GET /api/ai/overnight). Code decides what moved
// (|Δ| ≥ 5, a verdict change, a fail level broken) and writes the level text from the candles; the model's sentence
// on why it moved carries the AI tag. "n unchanged · show all" unfolds the rest, remembered per device. A rating never
// changes a stop or a plan.
const KEY = 'bp.home.overnight';
const remembered = () => { try { return localStorage.getItem(KEY) === 'all'; } catch { return false; } };
const remember = (all) => { try { localStorage.setItem(KEY, all ? 'all' : 'moved'); } catch { /* private mode */ } };
const dm = (d) => (d ? new Date(`${String(d).slice(0, 10)}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const BOOK = { real: 'Real', paper: 'Paper', paper_E: 'Paper', futures: 'Futures', options: 'Options' };
const levelTone = (t) => (/broken|lost|under/i.test(t || '') ? 'bad' : /held|reclaimed|over/i.test(t || '') ? 'ok' : '');
const src = (r) => {
  const s = r.source || '';
  if (/held/i.test(s)) return `held${r.book ? ` · ${BOOK[r.book] || r.book}` : ''}`;
  return [s, r.rank != null ? `#${r.rank}` : null, !/held/i.test(s) && r.book ? BOOK[r.book] || r.book : null].filter(Boolean).join(' ');
};
const textOf = (x) => (x == null ? '' : typeof x === 'string' ? x : x.text || '');

export default function OvernightPanel() {
  const [d, setD] = useState(undefined);
  const [all, setAll] = useState(remembered);
  useEffect(() => {
    let alive = true;
    api.getOvernight().then((r) => { if (alive) setD(r || null); }).catch(() => { if (alive) setD(null); });
    return () => { alive = false; };
  }, []);
  if (d === undefined) return null;                                 // loading: nothing (Home stays calm)
  const rows = Array.isArray(d?.rows) ? d.rows : Array.isArray(d?.items) ? d.items : Array.isArray(d) ? d : [];
  const moved = rows.filter((r) => r.moved !== false);
  const rest = rows.filter((r) => r.moved === false);
  const unchanged = d?.unchanged ?? rest.length;
  const shown = all ? [...moved, ...rest] : moved;
  const toggle = () => { remember(!all); setAll(!all); };
  const prevDay = d?.prev_day || rows.find((r) => r.prev?.day)?.prev?.day;
  const title = <>🌙 Overnight{prevDay || d?.day ? ` · ${[dm(prevDay), dm(d?.day)].filter(Boolean).join(' → ')}` : ''}</>;
  const right = [d?.counts?.held != null && `${d.counts.held} held`, d?.counts?.planned != null && `${d.counts.planned} planned`, d?.counts?.screens != null && `${d.counts.screens} screens`,
    d?.rated_at ? `ratings at ${clock(d.rated_at)}` : null].filter(Boolean).join(' · ');
  const cols = [{ h: 'Stock', ell: true }, { h: 'Last night', w: 130 }, { h: '', w: 36, key: 'arrow', lo: true }, { h: 'This night', w: 130 }, { h: 'Why it moved', w: '40%', ell: true }, { h: 'Level · by code', w: 200 }];
  const trs = shown.map((r) => {
    const p = r.prev || {}, n = r.now || {};
    const delta = r.delta ?? (n.score != null && p.score != null ? n.score - p.score : null);
    const why = textOf(r.change);
    const lvl = r.level && r.level !== '—' ? r.level : null;
    return { key: `${r.symbol}${r.book || ''}`, cls: r.moved === false ? 'dim' : '', title: [why && `AI · ${why}`, lvl].filter(Boolean).join('\n') || undefined, cells: [
      <><b>{r.symbol}</b> <small className="muted">{src(r)}</small></>,
      p.score != null ? <AiView compact v={{ score: p.score, verdict: p.verdict || '' }} /> : <span className="muted">—</span>,
      delta == null || delta === 0 ? <span className="muted">→</span> : <b className={delta > 0 ? 'up' : 'down'} style={{ fontSize: 15 }} title={`${delta > 0 ? '+' : ''}${delta}`}>{delta > 0 ? '↗' : '↘'}</b>,
      n.score != null ? <AiView compact v={{ score: n.score, verdict: n.verdict || '' }} /> : <span className="muted">—</span>,
      why ? <span className="ov-why"><AiTag of={r} /><span>{why}</span></span> : <span className="muted">—</span>,
      lvl ? <span className={`ov-lvl ${levelTone(lvl)}`} title={lvl}>{lvl}</span> : <span className="muted">—</span>] };
  });
  const empty = rows.length || unchanged ? 'no ratings moved overnight'
    : `ratings run tonight after ${d?.next_at ? clock(d.next_at) : '6:34 PM'}`;
  return <Panel id="overnight" title={title} meta={`${moved.length} moved`} right={right || undefined}>
    {trs.length ? <FixedTable className="ov-tab" cols={cols} rows={trs} /> : <p className="muted small rempty">{empty}</p>}
    <div className="rnote ov-foot">{unchanged > 0 || rest.length > 0
      ? <><span>{unchanged} unchanged · </span>{rest.length > 0 && <button type="button" className="btn ghost small" onClick={toggle} aria-expanded={all}>{all ? 'moved only ▴' : 'show all ▾'}</button>}<span> · </span></> : null}
      <span>built from each rating's memory block · a rating never changes a stop or a plan</span></div>
  </Panel>;
}
