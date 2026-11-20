import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { Panel, Tag, AiTag, FixedTable, Switch } from './ui2';

// 🤖 The AI view: a rating beside every system-suggested stock (score 0–100, a verdict set by code from the score,
// up to 3 reasons and the support / resistance levels it picked from the candidates code found). It rates and
// explains; it never acts. Every placement renders "—" (or nothing) when a stock has no view.

const parse = (x, d) => { if (x == null) return d; if (typeof x !== 'string') return x; try { return JSON.parse(x); } catch { return d; } };
// a view row from the API (row.ai_view or GET /api/ai/view) with its JSON fields parsed
export function norm(v) {
  if (!v || v.status === 'failed' || v.score == null) return null;
  const reasons = parse(v.reasons, []);
  return { ...v, reasons: Array.isArray(reasons) ? reasons : [String(reasons)], levels: parse(v.levels, null) || {}, candidates: parse(v.candidates, []) || [] };
}
const TONE = { buy: ['up', 'ok'], hold: ['up', 'ok'], wait: ['warn', 'w'], trim: ['warn', 'w'], avoid: ['down', 'bad'], 'exit-watch': ['down', 'bad'] };
export const tone = (verdict) => TONE[verdict] || ['', ''];
const prevScore = (v) => v?.previous?.score ?? v?.prev_score ?? null;
const tip = (v, note) => [`🤖 AI view ${v.score} · ${v.verdict}`, ...(v.reasons || []), v.change ? `changed: ${v.change}` : null,
  v.at || v.created ? `rated ${stamp(v.at || v.created)}` : null, note ? `what changed: ${note}` : null, 'a rating only: the system’s order, sizes and stops are unchanged'].filter(Boolean).join('\n');

// One call per table: the latest view of every symbol on screen (fills rows the backend does not join yet, and
// brings the previous view for the "44 → 68" arrow). A missing route or an error leaves the map empty.
export function useAiViews(symbols) {
  const key = [...new Set((symbols || []).filter(Boolean))].join(',');
  const [map, setMap] = useState({});
  useEffect(() => {
    if (!key) { setMap({}); return undefined; }
    let alive = true;
    api.getAiView(key.split(',')).then((r) => { if (alive) setMap(r?.views || {}); }).catch(() => { if (alive) setMap({}); });
    return () => { alive = false; };
  }, [key]);
  return map;
}
// the view for a row: the joined `ai_view` (wins) over the fetched one
export const viewOf = (row, map, sym = row?.symbol) => norm(row?.ai_view ? { ...(map?.[sym] || {}), ...row.ai_view } : map?.[sym]);

// The cell: score coloured by verdict · a small bar · the verdict tag · "44 → 68" when it moved. `compact` = score + tag.
export function AiView({ v, compact, note }) {
  if (!v) return <span className="muted" title="no AI view for this stock yet">—</span>;
  const [k, c] = tone(v.verdict), p = prevScore(v);
  return <span title={tip(v, note)} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap', maxWidth: '100%' }}>
    <b className={`mono ${k}`} style={{ width: 22, textAlign: 'right', flex: 'none' }}>{v.score}</b>
    {!compact && <span style={{ width: 26, height: 4, borderRadius: 2, background: 'var(--rowline)', flex: 'none', overflow: 'hidden' }}>
      <i style={{ display: 'block', height: 4, width: `${Math.max(0, Math.min(100, v.score))}%`, background: `var(--${c === 'ok' ? 'ok' : c === 'w' ? 'warn' : 'bad'})` }} /></span>}
    <Tag c={c}>{v.verdict}</Tag>
    {!compact && v.change && p != null && p !== v.score && <small className="mono muted" style={{ flex: 'none' }}>{p} → {v.score}</small>}
  </span>;
}

// The dry run's line under a BUY: "AI 82 · buy · reason" (one line, ellipsis; meant for a FixedTable span row)
export function AiLine({ v }) {
  if (!v) return null;
  const [k] = tone(v.verdict), text = `${v.score} · ${v.verdict}${v.reasons?.[0] ? ` · ${v.reasons.slice(0, 2).join(', ')}` : ''}`;
  return <div title={tip(v)} style={{ fontSize: 11, lineHeight: '16px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}>
    <AiTag of={v} /> <b className={`mono ${k}`}>{v.score}</b> <span className="muted">{text.slice(String(v.score).length)}</span></div>;
}
// "qty rule → AI" when size-by-score is on and the backend sent both (never above the rule)
export const qtyCell = (o) => (o.qty_rule != null && o.qty_ai != null && o.qty_rule !== o.qty_ai
  ? <span title={`the rule size ${o.qty_rule} → ${o.qty_ai} by the AI score (${(o.qty_ai / o.qty_rule).toFixed(1)}×, never above 1.0×) · reply "full ${o.symbol}" for the rule size`}><s className="muted">{o.qty_rule}</s> → {o.qty_ai}</span> : o.qty);

// The reasons cell (free text; the table cell carries the ellipsis)
export const aiReasons = (v) => (v?.reasons?.length ? v.reasons.join(' · ') : '—');
// the same, as a cell: the purple AI tag then the model's reasons (inline, so the cell's ellipsis still applies)
export const AiReasons = ({ v }) => (v?.reasons?.length ? <span title={aiReasons(v)}><AiTag of={v} /> {aiReasons(v)}</span> : <span className="muted">—</span>);
// held names: "what changed" (the change text, else the first reason)
export const aiChanged = (v) => (v ? v.change || v.reasons?.[0] || '—' : '—');

const near = (a, b) => a != null && b != null && Math.abs(a - b) <= Math.abs(b) * 0.002;
const priceOf = (x) => (x != null && typeof x === 'object' ? x.price : x);
const fmtP = (x) => (x == null ? '—' : (+x).toLocaleString('en-IN', { maximumFractionDigits: x >= 100 ? 0 : 2 }));
// [key, name, colour, price, label, fails] of the picked levels (+ the pivot); labels from the level itself or its candidate
export function levelRows(v, pivot) {
  if (!v) return [];
  const L = v.levels || {}, cands = v.candidates || [];
  const fb = priceOf(L.fails_below) ?? priceOf(L.support_1);
  const lab = (x, k) => (x != null && typeof x === 'object' && x.label) || (L.labels || {})[k] || cands.find((y) => near(y.price, priceOf(x)))?.label
    || cands.find((y) => near(y.price, priceOf(x)))?.kind?.replace(/_/g, ' ') || '';
  const piv = pivot ?? priceOf(L.pivot) ?? cands.find((c) => c.kind === 'pivot')?.price;
  return [['support_1', 'support 1', 'var(--ok)', priceOf(L.support_1), lab(L.support_1, 'support_1')],
    ['support_2', 'support 2', 'var(--muted)', priceOf(L.support_2), lab(L.support_2, 'support_2')],
    ['pivot', 'pivot', 'var(--blue)', piv, 'breakout level · entry'],
    ['resistance', 'resistance', 'var(--warn)', priceOf(L.resistance), lab(L.resistance, 'resistance')]]
    .filter((r) => r[3] != null).map((r) => [...r, r[0] !== 'pivot' && near(r[3], fb)]);
}

// The row drop-down: support 1 / support 2 / pivot / resistance / the candidates it did not pick.
export function AiLevels({ v, pivot, symbol }) {
  if (!v) return <span className="muted small">No AI view for {symbol || 'this stock'} yet.</span>;
  const rows = levelRows(v, pivot);
  const picked = rows.map((r) => r[3]);
  const rest = (v.candidates || []).filter((c) => !picked.some((p) => near(c.price, p)));
  const grid = { display: 'grid', gridTemplateColumns: '14px minmax(84px, 120px) 64px minmax(0, 1fr)', gap: '4px 8px', alignItems: 'baseline', fontSize: 12, lineHeight: '17px' };
  return <div style={{ maxWidth: 560, border: '1px solid var(--border)', borderRadius: 8, padding: '10px 12px' }}>
    <div style={grid}>
      {rows.map(([k, name, col, price, label, fails]) => <React.Fragment key={k}>
        <i style={{ width: 9, height: 9, borderRadius: 5, background: col, display: 'inline-block' }} /><span>{name}</span>
        <b className="mono" style={{ textAlign: 'right' }}>{fmtP(price)}</b>
        <span className="muted" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{label}{fails && <>{label && ' · '}<b style={{ color: 'var(--text)' }}>setup fails below</b></>}</span>
      </React.Fragment>)}
      {rest.length > 0 && <><span className="muted">·</span><span>not picked</span>
        <span className="mono muted" style={{ textAlign: 'right' }}>{rest.length}</span>
        <span className="muted" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{rest.map((c) => `${fmtP(c.price)} ${c.label || c.kind || ''}`.trim()).join(' · ')}</span></>}
    </div>
    <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>candidates found by code · picked and labelled by the model · drawn on the stock chart</div>
  </div>;
}

// The stock card under the chart: why · what would change it · levels picked by · numbers note (+ the levels).
export function AiViewCard({ symbol, v, pivot }) {
  if (!v) return null;
  const [, c] = tone(v.verdict), p = prevScore(v);
  const kinds = [...new Set((v.candidates || []).map((x) => x.kind).filter(Boolean))];
  const rows = [
    [`Why ${v.verdict}`, v.reasons?.length ? <><AiTag of={v} /> {aiReasons(v)}</> : '—'],
    v.change ? ['Since the last view', <>{p != null ? `${p} → ${v.score} · ` : ''}<AiTag of={v} /> {v.change}</>] : null,
    ['What would change it', v.would_change ? <><AiTag of={v} /> {v.would_change}</> : '—'],
    ['Levels picked by', `code found ${(v.candidates || []).length} candidates${kinds.length ? ` (${kinds.join(', ').replace(/_/g, ' ')})` : ''} · the model chose from those only`],
    ['Numbers', 'every figure above is code-measured · the model wrote the words and picked the levels'],
  ].filter(Boolean);
  return <div className="rd" style={{ marginTop: 10 }}>
    <Panel title={`${symbol} · AI view`} meta={<>score {v.score} · <Tag c={c}>{v.verdict}</Tag>{v.confidence != null && ` · confidence ${(+v.confidence).toFixed(1)}`}</>}
      right={[v.model, v.ms ? `${Math.round(v.ms / 1000)} s` : null, v.prompt_version, v.at || v.created ? stamp(v.at || v.created) : null].filter(Boolean).join(' · ') || 'shadow'}>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(110px, 180px) minmax(0, 1fr)', gap: '0 14px', padding: '6px 14px', fontSize: 12.5, lineHeight: '18px' }}>
        {rows.map(([k, val]) => <React.Fragment key={k}><div className="muted" style={{ padding: '6px 0', borderBottom: '1px solid var(--rowline)' }}>{k}</div>
          <div style={{ padding: '6px 0', borderBottom: '1px solid var(--rowline)', minWidth: 0, overflowWrap: 'anywhere' }}>{val}</div></React.Fragment>)}
      </div>
      <div style={{ padding: '8px 14px 12px' }}><AiLevels v={v} pivot={pivot} symbol={symbol} /></div>
    </Panel></div>;
}

// ---- System › AI: the record ---------------------------------------------------------------------------------------------------
const g = (o, ...ks) => { for (const k of ks) if (o && o[k] != null) return o[k]; return null; };
const rate = (x) => (x == null ? null : +x);                                               // hit rates come in percent
const pc0 = (x) => (x == null ? '—' : `${Math.round(rate(x))}%`);
const mv = (x) => (x == null ? '—' : `${x > 0 ? '+' : ''}${(x * 100).toFixed(1)}%`);              // a move comes as a fraction (0.041)
const txt = (x) => (x == null ? null : typeof x === 'string' ? x : Array.isArray(x) ? x.map(txt).filter(Boolean).join(' · ') : x.text || x.note || x.summary || null);
const SW = [['view_rank_by_score', 'Rank by score', 'ties in the plan ordered by the AI score'],
  ['view_size_by_score', 'Size by score', '0.5–1.0 × the rule size · never above, never a skip'],
  ['view_exit_watch_alert', 'Early-exit alert on "setup fails below"', 'a close under the model’s support 1 → an alert, not an exit']];
const GETS = [['✓', 'up', 'gets: base depth, length, contractions, volume vs average, distance to pivot, RS rank, SMA stack, ATR, market filter, sector move, last 3 filing and deal notes, results date'],
  ['✓', 'up', 'gets: the candidate levels found by code · its previous view and what happened since'],
  ['✗', 'down', 'never gets: raw prices to compute from · it cannot see a chart'],
  ['✗', 'down', 'never does: add a name, skip a name, change a stop, send an order'],
  ['✗', 'down', 'its strongest act, once unlocked and switched on: size a buy down to 0.5× the rule size']];

// The "AI view record" panel: hit rates by verdict against the plan baseline, the shuffle test, by week, the feature it
// leans on, where it is wrong, the three locked switches (PUT /api/ai/settings answers 400 while locked) and a
// "Rate tonight's names now" button (POST /api/ai/view/run). `view` = GET /api/ai's view block (may be missing).
export function AiViewRecord({ view, phone, onChanged }) {
  const [rec, setRec] = useState(null);
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [tick, setTick] = useState(0);
  const at = view?.last_night?.at;
  useEffect(() => { let on = true; api.getAiViewRecord(4).then((r) => on && setRec(r)).catch(() => on && setRec(null)); return () => { on = false; }; }, [at, tick]);
  const RS = view?.record_summary || {}, sw = view?.switches || {};
  const unlocked = !!(g(rec, 'unlocked') ?? RS.unlocked ?? (sw.locked === false));
  const act = (key, p) => { setBusy(key); p.then(() => { setErr(''); setTick((t) => t + 1); onChanged && onChanged(); }).catch((e) => setErr(e.message)).finally(() => setBusy('')); };
  const flip = (k, label, on) => { if (window.confirm(on ? `Switch "${label}" on?\n\nIt changes nothing until the next plan; the stop and the trail stay as the rules set them. You can switch it off any time.` : `Switch "${label}" off?\n\nThe plan goes back to the rules alone.`)) act(k, api.putAiSettings({ [k]: on })); };
  const run = () => { if (window.confirm("Rate tonight's names now?\n\nThe plan, held, Follow and Screens names go into the AI queue (one model call each, ~10 s a name). The guard still decides when each runs; nothing is sent or changed.")) act('run', api.aiViewRun()); };
  const byV = g(rec, 'by_verdict', 'verdicts') || {};
  const vrow = (k) => (Array.isArray(byV) ? byV.find((x) => x.verdict === k) : byV[k]);
  const base = g(rec, 'baseline', 'plan_baseline') || {};
  const b20 = rate(g(base, 'hit20', 'hit_20', 'hit@20') ?? RS.baseline20);
  const vCols = [{ h: 'Verdict' }, { h: 'Votes', w: 64, n: true }, { h: 'Hit @5', w: 70, n: true, lo: true }, { h: 'Hit @20', w: 76, n: true }, { h: 'Avg @20', w: 92, n: true, lo: true, title: 'average move 20 sessions after the view' }, { h: 'vs base', w: 92, n: true, title: 'hit @20 against the plan baseline, in points' }];
  const vRows = [['buy', 'ok'], ['wait', 'w'], ['avoid', 'bad']].map(([k, c]) => { const r = vrow(k) || {}; const h20 = rate(g(r, 'hit20', 'hit_20', 'hit@20') ?? (k === 'buy' ? RS.buy_hit20 : k === 'avoid' ? RS.avoid_hit20 : null));
    return { key: k, cells: [<Tag c={c}>{k}</Tag>, g(r, 'votes', 'n') ?? '—', pc0(g(r, 'hit5', 'hit_5', 'hit@5')), <span className={k === 'buy' ? 'up' : k === 'avoid' ? 'down' : ''}>{pc0(h20)}</span>, mv(g(r, 'avg20', 'avg_20', 'avg_move20')),
      h20 != null && b20 != null ? <span className={h20 - b20 > 0 ? (k === 'avoid' ? 'down' : 'up') : (k === 'avoid' ? 'up' : 'down')}>{h20 - b20 > 0 ? '+' : ''}{Math.round(h20 - b20)} pts</span> : '—'] }; });
  vRows.push({ key: 'base', cells: [<span className="muted">plan baseline · all names</span>, g(base, 'votes', 'n') ?? '—', pc0(g(base, 'hit5', 'hit_5')), pc0(b20), mv(g(base, 'avg20', 'avg_20')), '—'] });
  const weeks = g(rec, 'by_week', 'weeks_rows') || [];
  const wCols = [{ h: 'Week' }, { h: 'Votes', w: 60, n: true }, { h: 'Buy @20', w: 86, n: true, title: 'buy hit @20' }, { h: 'Avoid @20', w: 96, n: true, lo: true, title: 'avoid hit @20' }, { h: 'Base', w: 72, n: true, lo: true, title: 'plan baseline hit @20' }, { h: 'Edge', w: 64, n: true }, { h: 'Note', w: 220, ell: true, lo: true, nw: true }];
  const wkLabel = (w) => { const f = g(w, 'from'); if (!f) return g(w, 'label', 'week') || '—'; const a = new Date(`${f}T12:00:00`), b = new Date(a.getTime() + 4 * 86400000);
    const dm = (x, m) => x.toLocaleDateString('en-IN', m ? { day: 'numeric', month: 'short' } : { day: 'numeric' }); return `${a.getMonth() === b.getMonth() ? dm(a) : dm(a, 1)}–${dm(b, 1)}`; };
  const wRows = (Array.isArray(weeks) ? weeks : []).map((w, i) => { const bh = g(w, 'buy_hit20'), bs = g(w, 'baseline20'); const ed = g(w, 'edge') ?? (bh != null && bs != null ? bh - bs : null);
    const beat = g(w, 'beat');
    return { key: g(w, 'week', 'from') || i, cells: [wkLabel(w), g(w, 'votes', 'n') ?? '—', <span className="up">{pc0(bh)}</span>, <span className="down">{pc0(g(w, 'avoid_hit20'))}</span>,
      pc0(bs), <span className={ed > 0 ? 'up' : ed < 0 ? 'down' : ''}>{ed == null ? '—' : `${ed > 0 ? '+' : ''}${Math.round(ed)}`}</span>,
      txt(g(w, 'note')) || (beat == null ? 'not scored yet' : beat ? 'beat the baseline (buy over, avoid under)' : 'did not beat the baseline')] }; });
  const sh = g(rec, 'shuffle') || {};
  const shOk = g(sh, 'ok', 'passes', 'pass') ?? RS.shuffle_ok;
  const shReal = g(sh, 'real_hit20', 'real', 'real_hit'), shFake = g(sh, 'shuffled_hit20', 'shuffled', 'shuffled_hit');
  const L = g(rec, 'leans'), LB = L?.by_verdict || {};
  const leanVal = (k) => { const x = LB[k]?.[L?.feature]; return x == null ? null : L.feature === 'dry_up_pct' ? `${x}%` : x; };
  const lean = L?.label ? `${L.label}${leanVal('buy') != null && leanVal('avoid') != null ? ` · "buy" names ${leanVal('buy')} against "avoid" names ${leanVal('avoid')}` : ''}` : txt(g(rec, 'leans_on'));
  const wrong = txt(g(rec, 'wrong', 'where_wrong', 'worst'));
  const unlockText = txt(g(rec, 'unlock')) || sw.unlock_text || '4 weeks in a row with buy hit@20 over the plan baseline and avoid hit@20 under it';
  const since = g(rec, 'since') || (Array.isArray(weeks) && weeks[0]?.from ? new Date(`${weeks[0].from}T12:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : null);
  const votes = g(rec, 'total', 'votes') ?? (Array.isArray(byV) ? byV : Object.values(byV)).reduce((a, r) => a + (r?.votes || 0), 0);
  const LN = { padding: '8px 14px', fontSize: 12.5, lineHeight: '18px', borderBottom: '1px solid var(--rowline)', minWidth: 0, overflowWrap: 'anywhere' };
  const last = view?.last_night;
  return <Panel id="ai-view-record" title="AI view record" meta={`${since ? `since ${since} · ` : ''}4 weeks · ${votes || 0} votes`}
    right={<button type="button" className="btn ghost small" disabled={!!busy} onClick={run} title="queue tonight's ratings now · nothing is sent or changed">{busy === 'run' ? 'Queuing…' : "Rate tonight's names now"}</button>}>
    {err && <div className="rbanner bad" style={{ margin: '8px 14px' }}>{err}</div>}
    <div style={{ display: 'grid', gridTemplateColumns: phone ? 'minmax(0, 1fr)' : 'minmax(0, 1.5fr) minmax(0, 1fr)', gap: 0 }}>
      <div style={{ minWidth: 0, borderRight: phone ? 0 : '1px solid var(--rowline)' }}>
        <FixedTable cols={vCols} rows={vRows} />
        <div style={LN}>shuffle test: {shOk == null ? <span className="muted">not run yet</span> : <b className={shOk ? 'up' : 'down'}>{shOk ? 'passes' : 'fails'}</b>}
          {shReal != null && shFake != null && <> · the real view hits {pc0(shReal)} at 20 sessions, the same prompt with the features of another stock {pc0(shFake)}</>}
          <span className="muted"> · a hit = +3% or the trail armed before the stop</span></div>
        <FixedTable cols={wCols} rows={wRows} empty="No scored weeks yet: a view is scored 5, 10 and 20 sessions after it is made." />
        <div style={LN}><span className="muted">Feature it leans on most · </span>{lean || '—'}</div>
        <div style={{ ...LN, borderBottom: 0 }}><span className="muted">Where it is wrong · </span>{wrong || '—'}</div>
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ ...LN, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={last ? `tonight ${last.n ?? 0} rated` : undefined}>{last ? <>tonight: <b className="mono">{last.n ?? 0}</b> rated{last.by_verdict ? ` · ${Object.entries(last.by_verdict).map(([k, n]) => `${n} ${k}`).join(' · ')}` : ''}{last.ms_total ? ` · ${Math.round(last.ms_total / 1000)} s` : ''}{last.at ? ` · ${stamp(last.at)}` : ''}</>
          : <span className="muted">no ratings yet · the night job rates the plan, held, Follow and Screens names after the suggestions</span>}</div>
        {SW.map(([k, label, what]) => { const on = !!(sw[k] ?? sw[k.replace('view_', '')]); const locked = !unlocked && !on;
          return <div key={k} style={{ display: 'flex', gap: 10, alignItems: 'center', ...LN }}>
            <Switch on={on} disabled={!!busy || locked} title={locked ? `locked until the record unlocks it: ${unlockText}` : on ? 'on · click to switch off' : 'click to switch on'} onChange={(x) => flip(k, label, x)} />
            <div style={{ minWidth: 0 }}><b>{label}</b><div className="muted" style={{ fontSize: 11.5 }}>{what} · {on ? 'on' : locked ? '🔒 locked' : 'off'}</div></div></div>; })}
        <div style={LN}><span className="muted">unlock: </span>{unlocked ? <b className="up">unlocked · the switches may be turned on</b> : unlockText}</div>
        {GETS.map(([i, c, t]) => <div key={t} style={{ display: 'flex', gap: 8, ...LN }}><span className={c} style={{ flex: 'none', width: 12 }}>{i}</span><span className="muted">{t}</span></div>)}
      </div>
    </div>
    <div className="rnote">the view rates and explains; it never acts · every figure is code-measured · the verdict is set by code from the score (buy ≥ {g(sw, 'view_score_buy', 'score_buy') ?? 60}, avoid &lt; {g(sw, 'view_score_avoid', 'score_avoid') ?? 40})</div>
  </Panel>;
}
