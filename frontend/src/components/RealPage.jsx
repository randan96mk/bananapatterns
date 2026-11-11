import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp, ampm } from '../timefmt';
import { inr, rs, signed, pct, cls } from '../fmt';
import AttentionStrip from './AttentionStrip';
import NextUp from './NextUp';
import ReplanPanel from './ReplanPanel';
import { StopMeter } from './ui';
import { Head, Chip, Switch, Tag, Panel, FixedTable, KV, Act } from './ui2';
import { useNarrow } from '../useMedia';

const EXIT = { stop: '🛑 stop', trail: '🪜 late trail', exit: '📉 trend exit' };
const STATUS = { sent: 'ok', failed: 'bad', skipped: 'w', added: 'a', removed: 'w', edited: '' };
const SIDE = { BUY: 'up', EXIT: 'down' };                 // the order log's colour per action (hand corrections: muted)
const n2 = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const FIELD = { qty: 'qty', entry: 'entry', stop: 'stop', exit: 'exit', added: 'added' };
const editsTip = (edits) => (edits || []).map((e) => `${FIELD[e.field] || e.field} ${e.from == null ? '' : `${typeof e.from === 'number' ? n2(e.from) : e.from} → `}${typeof e.to === 'number' ? n2(e.to) : e.to} · ${day(e.at)}${e.note ? ` · ${e.note}` : ''} · by you`).join('\n');
const Edited = ({ edits }) => (edits?.length ? <Tag title={editsTip(edits)}>edited</Tag> : null);
const IN = { fontSize: 12, padding: '3px 6px', textAlign: 'right' };
const B = { fontSize: 11.5, padding: '3px 9px', whiteSpace: 'nowrap' };   // a button inside a table cell (td .small is muted text there)
const PEN = { fontSize: 11.5, padding: '2px 6px' };
const F = { display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', fontSize: 12, color: 'var(--muted)' };
const EFF = { fontSize: 11.5, color: 'var(--muted)', marginTop: 6, overflowWrap: 'anywhere' };
const Lbl = ({ children }) => <span style={{ display: 'inline-flex', gap: 4, alignItems: 'center', whiteSpace: 'nowrap' }}>{children}</span>;
const todayIso = () => new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const COL = { gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' };   // a column that never widens with its content
const Meta = ({ children }) => <span style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{children}</span>;
// E+ late trail: a small tag once the trail is active (+100%) or has raised the stop, else the best close
const Trail = ({ x }) => (x.trail_active || x.trailed ? <Tag c="ok" title={`E+ late trail: 25% under the highest close once +100%; only ever raised, from the next session · best ${rs(x.best)}`}>🪜 trail {x.trailed ? 'stop' : 'active'}</Tag>
  : x.best ? <Tag title="best close so far">best {rs(x.best)}</Tag> : null);
const scrollTo = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });

// 💰 Real trading: the Trend Breakout orders sent by webhook (BUY / EXIT), tracked like paper. Every switch and
// confirm() is kept as it was: nothing here trades on its own.
function PaperVsReal() {
  const [r, setR] = useState(null);
  useEffect(() => { api.realReconcile(30).then(setR).catch(() => setR({ rows: [] })); }, []);
  const c = r?.counts || {};
  const icon = (x) => (x.real === 'sent' ? ['✓', 'up'] : x.real === 'missing' || x.real === 'failed' ? ['!', 'down'] : ['·', '']);
  return <Panel id="reconcile" title="Paper vs Real" meta="same variant · 30 days" right={r && <Meta>{c.sent || 0} sent · {c.failed || 0} failed · {c.missing || 0} missing · {c.skipped || 0} skipped{r.avg_price_diff_pct != null ? ` · price ${r.avg_price_diff_pct > 0 ? '+' : ''}${r.avg_price_diff_pct}% vs paper` : ''}</Meta>}>
    {!r ? <p className="muted small rempty">Loading…</p> : !r.rows.length ? <p className="muted small rempty">Nothing to compare yet: every paper fill is checked against the order sent that day.</p>
      : r.rows.slice(0, 30).map((x, i) => { const [ic, k] = icon(x); return <Act key={i} icon={ic} cls={k}
        text={<><b className={x.action === 'BUY' ? 'up' : 'down'}>{x.action}</b> <b>{x.symbol}</b> · paper {x.paper_qty ?? '—'} @ {x.paper_price ?? '—'} · real {x.real_qty ?? '—'} @ {x.real_price ?? '—'}{x.price_diff_pct != null ? ` (${x.price_diff_pct > 0 ? '+' : ''}${x.price_diff_pct}%)` : ''}</>}
        meta={`${day(x.day)} · ${x.real}${x.note || x.reply || x.why ? ` · ${x.note || x.reply || x.why}` : ''}`} />; })}
    <div className="rnote">a check only · reconcile never places an order</div></Panel>;
}

// ✎ Correct an open position (a fill the webhook could only estimate, or a trade placed by hand): qty, entry, stop,
// note. The effects come from a dry run (nothing saved) and are listed again in the confirm before saving.
function PositionEditor({ x, onDone, onErr }) {
  const [f, setF] = useState({ qty: String(x.qty), entry: n2(x.buy).replace(/,/g, ''), stop: n2(x.stop).replace(/,/g, ''), note: '' });
  const [eff, setEff] = useState(null);
  const [busy, setBusy] = useState(false);
  const body = {};
  if (Number(f.qty) !== x.qty) body.qty = Number(f.qty);
  if (Math.abs(Number(f.entry) - x.buy) >= 0.005) body.entry = Number(f.entry);
  if (Math.abs(Number(f.stop) - x.stop) >= 0.005) body.stop = Number(f.stop);
  const changed = Object.keys(body).length > 0;
  const key = JSON.stringify(body);
  useEffect(() => {
    if (!changed) { setEff(null); return undefined; }
    const t = setTimeout(() => api.realEditPosition(x.symbol, { ...body, note: f.note }, true).then(setEff).catch((e) => setEff({ error: e.message })), 300);
    return () => clearTimeout(t);
  }, [key, changed, x.symbol]);
  const e = eff?.effects;
  const lines = e ? [body.entry != null && `entry ${n2(x.buy)} → ${n2(body.entry)}${e.est_cleared ? ' (est. cleared)' : ''}`, body.qty != null && `qty ${x.qty} → ${body.qty}`,
    body.stop != null && `stop ${n2(x.stop)} → ${n2(body.stop)}`, `P&L ${signed(e.pnl_before)} → ${signed(e.pnl_after)}`, `cash ${inr(e.cash)}`, `EXIT will send ${e.exit_qty} shares`,
    `stop check at ${n2(eff.position?.stop)} (${e.stop_pct}% away)`, body.entry != null && `trail arms at +100% from the new entry (${n2(e.trail_arms_at)})`, 'logged as "edited by you"'].filter(Boolean) : [];
  const save = () => {
    if (!changed || !e) return;
    if (!window.confirm(`Save the correction to ${x.symbol}?\n\n${lines.join('\n')}\n\nNothing is sent to the broker.`)) return;
    setBusy(true);
    api.realEditPosition(x.symbol, { ...body, note: f.note }).then(() => onDone(true)).catch((err) => onErr(err.message)).finally(() => setBusy(false));
  };
  const remove = () => {
    if (!window.confirm(`Remove ${x.symbol} from tracking?\n\nUse this only when the order never filled at the broker: the cash gets the cost (${inr(x.qty * x.buy)}) back and the charges are reversed, as if never bought. No order is sent.`)) return;
    setBusy(true);
    api.realRemovePosition(x.symbol, f.note || 'never filled').then(() => onDone(true)).catch((err) => onErr(err.message)).finally(() => setBusy(false));
  };
  const set = (k) => (ev) => setF({ ...f, [k]: ev.target.value });
  return <div>
    <div style={F}><b style={{ color: 'var(--text)' }}>Edit {x.symbol}</b>
      <Lbl>Qty <input className="input" type="number" min="1" step="1" value={f.qty} onChange={set('qty')} style={{ ...IN, width: 64 }} /></Lbl>
      <Lbl>Entry ₹ <input className="input" type="number" min="0" step="0.05" value={f.entry} onChange={set('entry')} style={{ ...IN, width: 96 }} /></Lbl>
      <Lbl>Stop ₹ <input className="input" type="number" min="0" step="0.05" value={f.stop} onChange={set('stop')} style={{ ...IN, width: 96 }} /></Lbl>
      <Lbl>Note <input className="input" type="text" value={f.note} onChange={set('note')} placeholder="filled by hand 11 Nov 9:52" style={{ ...IN, textAlign: 'left', width: 200 }} /></Lbl>
      <button type="button" className="btn primary" style={B} disabled={busy || !changed || !e} onClick={save}>Save…</button>
      <button type="button" className="btn ghost" style={B} disabled={busy} onClick={() => onDone(false)}>Cancel</button>
      <button type="button" className="btn ghost" style={{ ...B, color: 'var(--bad)', borderColor: 'var(--bad)', marginLeft: 'auto' }} disabled={busy} onClick={remove}>Remove from tracking…</button></div>
    <div style={EFF}>{eff?.error ? <span className="down">{eff.error}</span> : e ? <>Effects: {lines.map((l, i) => <span key={i}>{i ? ' · ' : ''}{l}</span>)}</>
      : changed ? 'Checking…' : x.est ? 'Estimated entry: the webhook\'s fill is not visible, so the entry is what the app saw when sending (corrected to the open in the evening unless you set it here).' : 'Change a value to see the effects before saving.'}</div></div>;
}

// ✎ Correct a closed trade's exit price / quantity (net and cash follow; est. cleared).
function TradeEditor({ t, onDone, onErr }) {
  const [f, setF] = useState({ exit: n2(t.sell).replace(/,/g, ''), qty: String(t.qty) });
  const [eff, setEff] = useState(null);
  const [busy, setBusy] = useState(false);
  const body = {};
  if (Math.abs(Number(f.exit) - t.sell) >= 0.005) body.exit = Number(f.exit);
  if (Number(f.qty) !== t.qty) body.qty = Number(f.qty);
  const changed = Object.keys(body).length > 0, key = JSON.stringify(body);
  useEffect(() => {
    if (!changed) { setEff(null); return undefined; }
    const h = setTimeout(() => api.realEditTrade(t.id, body, true).then(setEff).catch((e) => setEff({ error: e.message })), 300);
    return () => clearTimeout(h);
  }, [key, changed, t.id]);
  const e = eff?.effects;
  const text = e ? `realised ${signed(e.net_before)} → ${signed(e.net_after)} · cash ${signed(e.delta)}${e.est_cleared ? ' · est. cleared' : ''} · logged` : '';
  const save = () => {
    if (!e || !window.confirm(`Save the correction to the ${t.symbol} trade?\n\nEffects: ${text}\n\nNothing is sent to the broker.`)) return;
    setBusy(true);
    api.realEditTrade(t.id, body).then(() => onDone(true)).catch((err) => onErr(err.message)).finally(() => setBusy(false));
  };
  return <div>
    <div style={F}><b style={{ color: 'var(--text)' }}>Edit the exit · {t.symbol}</b>
      <Lbl>Exit ₹ <input className="input" type="number" min="0" step="0.05" value={f.exit} onChange={(ev) => setF({ ...f, exit: ev.target.value })} style={{ ...IN, width: 96 }} /></Lbl>
      <Lbl>Qty <input className="input" type="number" min="1" step="1" value={f.qty} onChange={(ev) => setF({ ...f, qty: ev.target.value })} style={{ ...IN, width: 64 }} /></Lbl>
      <button type="button" className="btn primary" style={B} disabled={busy || !changed || !e} onClick={save}>Save…</button>
      <button type="button" className="btn ghost" style={B} disabled={busy} onClick={() => onDone(false)}>Cancel</button></div>
    <div style={EFF}>{eff?.error ? <span className="down">{eff.error}</span> : e ? `Effects: ${text}` : changed ? 'Checking…' : t.est ? 'Estimated exit: the price the app saw when sending (corrected to the open in the evening unless you set it here).' : 'Change a value to see the effects before saving.'}</div></div>;
}

// + Add a position: a trade placed by hand that the app should track and exit by its rules. Nothing is sent.
function AddPosition({ d, init, onDone, onErr }) {
  const [f, setF] = useState({ symbol: '', qty: '', entry: '', day: todayIso(), stop: '', note: '', ...(init || {}) });
  const [busy, setBusy] = useState(false);
  const set = (k) => (ev) => setF({ ...f, [k]: k === 'symbol' ? ev.target.value.toUpperCase() : ev.target.value });
  const ok = f.symbol.trim() && Number(f.qty) >= 1 && Number(f.entry) > 0 && /^\d{4}-\d{2}-\d{2}$/.test(f.day);
  const n = d.positions.length, cap = d.max_pos;
  const add = () => {
    if (!ok) return;
    const stop = f.stop ? `stop ${n2(Number(f.stop))}` : 'the stop from the rule (ATR × mult or the stop % under the entry)';
    if (!window.confirm(`Track ${f.symbol} · ${Number(f.qty)} @ ${n2(Number(f.entry))} bought ${day(f.day)}, ${stop}?\n\nIt takes a slot (${n + 1} of ${cap}) and ${inr(Number(f.qty) * Number(f.entry))} of the cash. Nothing is sent to the broker; the app exits it by its rules.`)) return;
    setBusy(true);
    api.realAddPosition({ symbol: f.symbol.trim(), qty: Number(f.qty), entry: Number(f.entry), day: f.day, stop: f.stop ? Number(f.stop) : null, note: f.note })
      .then(() => onDone(true)).catch((err) => onErr(err.message)).finally(() => setBusy(false));
  };
  return <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--rowline)' }}>
    <div style={F}><b style={{ color: 'var(--text)' }}>Add a position</b>
      <Lbl>Symbol <input className="input" type="text" value={f.symbol} onChange={set('symbol')} placeholder="RELIANCE" style={{ ...IN, textAlign: 'left', width: 110 }} /></Lbl>
      <Lbl>Qty <input className="input" type="number" min="1" step="1" value={f.qty} onChange={set('qty')} style={{ ...IN, width: 64 }} /></Lbl>
      <Lbl>Entry ₹ <input className="input" type="number" min="0" step="0.05" value={f.entry} onChange={set('entry')} style={{ ...IN, width: 96 }} /></Lbl>
      <Lbl>Bought <input className="input" type="date" value={f.day} max={todayIso()} onChange={set('day')} style={{ ...IN, textAlign: 'left', width: 136 }} /></Lbl>
      <Lbl>Stop ₹ <input className="input" type="number" min="0" step="0.05" value={f.stop} onChange={set('stop')} placeholder="rule" style={{ ...IN, width: 96 }} /></Lbl>
      <Lbl>Note <input className="input" type="text" value={f.note} onChange={set('note')} style={{ ...IN, textAlign: 'left', width: 160 }} /></Lbl>
      <button type="button" className="btn primary" style={B} disabled={busy || !ok} onClick={add}>Add…</button>
      <button type="button" className="btn ghost" style={B} disabled={busy} onClick={() => onDone(false)}>Cancel</button></div>
    <div style={EFF}>A trade placed by hand that the app should track and exit by its rules. Stop defaults to the rule (ATR × {d.stop_rule?.atr_mult} or {d.stop_rule?.stop_pct}% under the entry, whichever is higher); uses a slot ({n + 1} of {cap}{n + 1 > cap ? ' → refused unless max open is raised' : ''}) and the cash · nothing is sent to the broker.</div></div>;
}

// Broker check: the positions pasted from the deployment's table / a CSV export, three ways against the app's book.
// A check only: the book moves only through the existing corrections (edit / remove / add), each with its confirm.
const TA = { display: 'block', width: '100%', boxSizing: 'border-box', height: 92, resize: 'vertical', fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 11.5, lineHeight: 1.5, whiteSpace: 'pre', overflow: 'auto' };
const SAMPLE = 'INSTRUMENT\tQTY\tLTP\tVALUE\tP&L\nRELIANCE\t8\t1,337.60\t₹10.70 k\t+₹346.40\n…  (or a positions CSV, or "RELIANCE 8" lines)';
const B2 = { fontSize: 11, padding: '2px 7px', whiteSpace: 'nowrap' };   // a button in a broker-check cell
const SHOW = 8;                                            // Both rows shown before "+N more · all match"
function BrokerCheck({ d, tick, onChanged, onAdd, onErr }) {
  const [text, setText] = useState('');
  const [r, setR] = useState(null);
  const [busy, setBusy] = useState(false);
  const [all, setAll] = useState(false);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    api.realBrokerCheckLast().then((x) => { setR(x); if (!loaded && x.text) setText(x.text); setLoaded(true); }).catch(() => setLoaded(true));
  }, [tick]);
  const compare = () => {
    if (!text.trim()) return;
    setBusy(true);
    api.realBrokerCheck(text).then((x) => { setR(x); setAll(false); }).catch((e) => onErr(e.message)).finally(() => setBusy(false));
  };
  const file = (ev) => {
    const f = ev.target.files?.[0];
    if (!f) return;
    const rd = new window.FileReader();
    rd.onload = () => setText(String(rd.result || ''));
    rd.readAsText(f);
    ev.target.value = '';
  };
  // a correction through the edit endpoint: the dry run's effects go in the confirm, then it is saved
  const edit = (sym, body, what) => {
    setBusy(true);
    api.realEditPosition(sym, { ...body, note: 'broker check' }, true).then((x) => {
      const e = x.effects;
      const lines = [what, `P&L ${signed(e.pnl_before)} → ${signed(e.pnl_after)}`, `cash ${inr(e.cash)}`, `EXIT will send ${e.exit_qty} shares`, `stop check at ${n2(x.position?.stop)} (${e.stop_pct}% away)`, e.est_cleared ? 'est. cleared' : null, 'logged as "edited by you"'].filter(Boolean);
      if (!window.confirm(`Save the correction to ${sym}?\n\n${lines.join('\n')}\n\nNothing is sent to the broker.`)) return null;
      return api.realEditPosition(sym, { ...body, note: 'broker check' }).then(() => onChanged());
    }).catch((e) => onErr(e.message)).finally(() => setBusy(false));
  };
  const remove = (sym, qty, entry) => {
    if (!window.confirm(`Remove ${sym} from tracking?\n\nUse this only when the order never filled at the broker: the cash gets the cost (${inr(qty * entry)}) back and the charges are reversed, as if never bought. No order is sent.`)) return;
    setBusy(true);
    api.realRemovePosition(sym, 'never filled (broker check)').then(() => onChanged()).catch((e) => onErr(e.message)).finally(() => setBusy(false));
  };
  const c = r?.counts || {};
  const stampLine = r?.at ? <Meta>last check <b>{stamp(r.at)}</b> · {c.differences ?? 0} difference{c.differences === 1 ? '' : 's'}</Meta> : <Meta>no check yet</Meta>;
  const appPos = Object.fromEntries((d.positions || []).map((x) => [x.symbol, x]));
  const bothCols = [{ h: 'Stock', ell: true }, { h: 'App', w: 56, n: true }, { h: 'Broker', w: 72, n: true }, { h: '', w: 146, key: 'act' }];
  const bothAll = r?.both || [];
  const shown = all ? bothAll : bothAll.slice(0, SHOW);
  const rest = bothAll.slice(shown.length);
  const bothRows = shown.map((x) => ({ key: x.symbol, cells: [<><b>{x.symbol}</b>{x.est && <Tag c="w" title="estimated entry: the price seen when sending">est. entry</Tag>}</>, x.app_qty, x.broker_qty,
    x.empty ? <button type="button" className="btn ghost small" style={{ ...B2, color: 'var(--bad)', borderColor: 'var(--bad)' }} disabled={busy} title="the broker shows an empty slot for it" onClick={() => remove(x.symbol, x.app_qty, x.app_entry)}>Remove from tracking…</button>
      : !x.match ? (x.est && x.broker_price ? <button type="button" className="btn ghost small" style={B2} disabled={busy} title={`the app's entry is estimated (${n2(x.app_entry)}); the broker's price ${n2(x.broker_price)} is used with the quantity`} onClick={() => edit(x.symbol, { qty: x.broker_qty, entry: x.broker_price }, `qty ${x.app_qty} → ${x.broker_qty} · entry ${n2(x.app_entry)} → ${n2(x.broker_price)}`)}>set qty {x.broker_qty}, entry…</button>
        : <button type="button" className="btn ghost small" style={B2} disabled={busy} onClick={() => edit(x.symbol, { qty: x.broker_qty }, `qty ${x.app_qty} → ${x.broker_qty}`)}>set qty {x.broker_qty}…</button>)
        : x.est && x.broker_price && Math.abs(x.broker_price - x.app_entry) >= 0.005 ? <button type="button" className="btn ghost small" style={B2} disabled={busy} title={`the app's entry is estimated (${n2(x.app_entry)}); use the broker's price`} onClick={() => edit(x.symbol, { entry: x.broker_price }, `entry ${n2(x.app_entry)} → ${n2(x.broker_price)}`)}>entry → {n2(x.broker_price)}…</button>
          : <Tag c="ok">match</Tag>] }));
  if (rest.length) bothRows.push(rest.every((x) => x.match) ? `+${rest.length} more · all match` : `+${rest.length} more`);
  const appCols = [{ h: 'Stock', ell: true }, { h: 'App', w: 56, n: true }, { h: '', w: 164, key: 'act' }];
  const appRows = (r?.app_only || []).map((x) => ({ key: x.symbol, cells: [<b>{x.symbol}</b>, x.qty,
    appPos[x.symbol] ? <button type="button" className="btn ghost small" style={{ ...B2, color: 'var(--bad)', borderColor: 'var(--bad)' }} disabled={busy} onClick={() => remove(x.symbol, x.qty, x.entry)}>Remove from tracking…</button> : <span className="muted">gone</span>] }));
  const brkCols = [{ h: 'Stock', ell: true }, { h: 'Broker', w: 72, n: true }, { h: '', w: 134, key: 'act' }];
  const brkRows = (r?.broker_only || []).map((x) => ({ key: x.symbol, cells: [<><b>{x.symbol}</b>{x.unknown && <Tag c="w" title="not in the stored universe: the app cannot track it (no candles)">not in universe</Tag>}</>, x.qty,
    x.empty ? <span className="muted">empty slot · ignore</span> : <button type="button" className="btn ghost small" style={B2} disabled={busy} onClick={() => onAdd({ symbol: x.symbol, qty: String(x.qty), entry: x.price ? String(x.price) : '' })}>Add to tracking…</button>] }));
  const warn = (n) => (n ? { borderColor: 'var(--warn)' } : undefined);
  return <Panel id="brokercheck" title="Broker check" meta="paste" right={<>{stampLine}
    <label className="btn small" style={{ cursor: 'pointer', alignSelf: 'center', display: 'inline-flex', alignItems: 'center', gap: 4 }} title="load a positions / holdings CSV export into the box">⬆ CSV<input type="file" accept=".csv,.txt,text/csv,text/plain" onChange={file} style={{ display: 'none' }} /></label></>}>
    <div style={{ padding: '12px 14px 6px' }}><textarea className="input" style={TA} value={text} onChange={(ev) => setText(ev.target.value)} placeholder={SAMPLE} spellCheck={false} /></div>
    <div className="rnote" style={{ paddingTop: 0 }}>Paste the deployment's positions table or the broker's positions export as copied (symbol and quantity are enough; price columns are used for fill prices when present).</div>
    <div style={{ ...F, padding: '0 14px 12px', gap: 8 }}>
      <button type="button" className="btn primary small" style={{ alignSelf: 'center' }} disabled={busy || !text.trim()} onClick={compare}>{busy ? 'Working…' : 'Compare'}</button>
      <span className="muted" style={{ fontSize: 11 }}>nothing is sent · the app's book changes only through the buttons below, each with a confirm</span></div>
    {r?.at && <div className="cols c3" style={{ padding: '0 14px 12px' }}>
      <Panel title="Both" meta={bothAll.length} right={<Meta>qty match {c.match ?? 0} · differs {c.differ ?? 0}</Meta>}>
        <FixedTable cols={bothCols} rows={bothRows} empty="No name is in both lists." />
        {rest.length > 0 && !all && <div className="rnote"><button type="button" className="linkbtn" onClick={() => setAll(true)}>show all</button></div>}
      </Panel>
      <div className="rpanel" style={warn(appRows.length)}><div className="ph"><b>App only</b><span className="n">{appRows.length}</span><span className="r"><Meta>never filled at the broker?</Meta></span></div>
        <FixedTable cols={appCols} rows={appRows} empty="None: every app position is at the broker." />
        <div className="rnote">removing restores cash and charges · no order goes out</div></div>
      <div className="rpanel" style={warn(c.broker_only)}><div className="ph"><b>Broker only</b><span className="n">{c.broker_only ?? 0}</span><span className="r"><Meta>not tracked by the app</Meta></span></div>
        <FixedTable cols={brkCols} rows={brkRows} empty="None: every broker position is tracked." />
        <div className="rnote">adding asks for the fill price and bought day; the app then exits it by its rules</div></div>
    </div>}
    <div className="rnote">Home › Needs a look says "Broker check: paste the positions after the 9:16 send" until you paste again after a send.</div>
  </Panel>;
}

export default function RealPage() {
  const [d, setD] = useState(null);
  const [edit, setEdit] = useState(() => { try { const s = sessionStorage.getItem('bp.real.edit'); sessionStorage.removeItem('bp.real.edit'); return s || null; } catch { return null; } });
  const [tedit, setTedit] = useState(null);                // the trade (id) whose exit is being edited
  const [adding, setAdding] = useState(false);
  const narrow = useNarrow();
  const [err, setErr] = useState('');
  const [tick, setTick] = useState(0);
  const [cap, setCap] = useState('');
  const [rp, setRp] = useState(null);                      // the last ↻ Re-evaluate report (shown under the orders)
  const [replanning, setReplanning] = useState(false);
  const reload = () => setTick((t) => t + 1);
  // ↻ Re-evaluate: the plan rebuilt from the evening's data and the account as it is now; with n, max open is saved first
  const replan = (n) => {
    setReplanning(true);
    api.realReplan(n).then((x) => { setRp(x); reload(); }).catch((e) => setErr(e.message)).finally(() => setReplanning(false));
  };

  useEffect(() => {
    api.getReal().then((x) => { setD(x); setErr(''); setCap(String(x.started ? x.account.capital : x.settings.capital)); })
      .catch((e) => setErr(e.message));
  }, [tick]);
  const saveCap = () => {
    const v = Number(cap);
    const now = d.started ? d.account.capital : d.settings.capital;
    if (!(v >= 10000) || v === now) return;
    const msg = d.started ? `Change the capital from ${inr(now)} to ${inr(v)}? This counts as ${v > now ? 'adding' : 'withdrawing'} ${inr(Math.abs(v - now))}: `
      + 'cash changes, the profit so far stays, and tomorrow\'s BUY quantities are re-sized.' : `Start with ${inr(v)}?`;
    if (window.confirm(msg)) api.realCapital(v).then((res) => { if (res.replan?.status === 'ok') setRp(res.replan); reload(); }).catch((e) => setErr(e.message));
  };

  const set = (body) => api.realSettings(body).then(reload).catch((e) => setErr(e.message));
  const kill = (on) => {
    if (!on || window.confirm('Stop ALL real orders now? Nothing is sent until you turn the kill switch off.')) {
      api.realKill(on).then(reload).catch((e) => setErr(e.message));
    }
  };
  const reset = () => {
    if (window.confirm('Reset the real account tracking? Positions, trades and the curve are cleared (the order log stays). Nothing is sent to the broker.')) {
      api.realReset().then(reload).catch((e) => setErr(e.message));
    }
  };

  const [busy, setBusy] = useState('');
  const retry = (o) => {
    const safe = /429/.test(o.reply || '');
    const msg = `Send ${o.action} ${o.symbol} ${o.qty ?? ''} again now, at the market?\n\n`
      + (safe ? 'It failed with "429 Too Many Requests": the order was refused, so it was not placed.'
        : `It failed with: ${o.reply || 'an error'}.\nThe order MAY have gone through. Check the positions at the broker first: if ${o.symbol} is already there, do NOT retry.`);
    if (!window.confirm(msg)) return;
    setBusy(o.symbol);
    api.realRetry(o.symbol, o.action).then((x) => { if (x.status !== 'sent') setErr(`Retry ${o.symbol}: ${x.error}`); reload(); })
      .catch((e) => setErr(e.message)).finally(() => setBusy(''));
  };
  const todayIst = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  const failedToday = (d?.log || []).filter((o) => o.status === 'failed' && o.day === todayIst);

  if (!d) return <div className="rd"><Panel>{err ? <div className="rbanner bad">{err}</div> : <p className="muted small rempty">Loading…</p>}</Panel></div>;
  const s = d.settings || {};
  const live = d.enabled && !d.killed;
  const A = d.account;
  const capNow = d.started ? d.account.capital : d.settings.capital;
  const risk = (d.positions || []).reduce((t, x) => t + Math.max(0, x.qty * (x.last - x.stop)), 0);
  const variant = d.variant === 'E' ? 'E+' : d.variant;
  const filterState = d.filter?.state;
  const orderCols = [{ h: 'Stock' }, { h: 'Side', w: 62 }, { h: 'Qty', w: 56, n: true }, { h: 'Stop', w: 90, n: true, lo: true }, { h: '≈ Value', w: 90, n: true }, { h: '₹ at risk', w: 86, n: true, lo: true }, { h: 'Why', w: 170, lo: true, ell: true }, { h: 'Status', w: 128 }];
  const orderRows = (d.orders || []).map((o) => ({ key: o.action + o.symbol, cells: [<b>{o.symbol}</b>, <Tag c={o.action === 'BUY' ? 'ok' : 'bad'}>{o.action}</Tag>, o.qty, o.stop ? rs(o.stop) : '—',
    o.close ? inr(o.qty * o.close) : '—', o.risk ? inr(o.risk) : '—', <span className="muted">{o.why}</span>,
    <Tag c={o.sent === 'sent' ? 'ok' : o.sent === 'failed' ? 'bad' : live ? 'a' : 'w'}>{o.sent || (live ? `queued · ${ampm(s.send_time)}` : 'not sending')}</Tag>] }));
  const done = (changed) => { setEdit(null); setTedit(null); setAdding(false); if (changed) reload(); };
  // Strategy is the short tag at 60 px; under 1440 px % and Days are dropped (nw: the row title keeps them) so Stock keeps 140 px+
  const posCols = [{ h: 'Stock', ell: true }, { h: 'Strat.', w: 60, lo: true }, { h: 'Qty', w: 56, n: true, lo: true }, { h: 'Entry', w: 90, n: true, lo: true }, { h: 'Now', w: 90, n: true }, { h: 'After costs', w: 100, n: true }, { h: '%', w: 64, n: true, lo: true, nw: true }, { h: 'Stop · distance', w: narrow ? 170 : 240, ell: true }, { h: 'Days · bought', w: 112, n: true, lo: true, nw: true }, { h: '', w: 50, key: 'edit' }];
  const posRows = (d.positions || []).flatMap((x) => [{ key: x.symbol, title: `${pct(x.buy ? (x.last / x.buy - 1) * 100 : null)} · ${x.days ?? '—'} days · bought ${day(x.in)} · before costs ${signed(x.gross)} · costs ${inr(x.costs)}${x.best ? ` · best close ${rs(x.best)}` : ''}${x.px_orig != null && x.px_orig !== x.buy ? ` · first entry ${rs(x.px_orig)}` : ''}${x.manual_stop ? ' · stop set by you (the trail may only raise it)' : ''}`,
    cells: [<><b>{x.symbol}</b>{x.est && <Tag c="w" title="estimated: the price seen when sending, corrected to the day's open in the evening · ✎ to set the real fill">est. entry</Tag>}<Edited edits={x.edits} />{x.manual && <Tag c="a" title="placed by hand, tracked and exited by the app's rules">by hand</Tag>}{x.last && x.stop && (x.last / x.stop - 1) * 100 < 2 && <Tag c="w">near stop</Tag>}</>,
      <span className={`v${x.variant}`}>{x.variant === 'E' ? 'E+' : x.variant}</span>, x.qty, rs(x.buy), rs(x.last), <b className={cls(x.net)}>{signed(x.net)}</b>, <span className={cls(x.gross)}>{pct(x.buy ? (x.last / x.buy - 1) * 100 : null)}</span>,
      <><StopMeter last={x.last} stop={x.stop} /> <span className="mono">{rs(x.stop)}</span><Trail x={x} /></>, <>{x.days ?? '—'} · {day(x.in)}</>,
      <button type="button" className={`btn ${edit === x.symbol ? 'primary' : 'ghost'}`} style={PEN} title="correct qty / entry / stop · remove from tracking" onClick={() => { setEdit(edit === x.symbol ? null : x.symbol); setAdding(false); }}>✎</button>] },
  ...(edit === x.symbol ? [{ key: `${x.symbol}-edit`, span: <PositionEditor x={x} onDone={done} onErr={setErr} /> }] : [])]);
  const tradeCols = [{ h: 'Stock', ell: true }, { h: 'Bought · sold', w: 150, lo: true }, { h: 'Qty', w: 56, n: true, lo: true }, { h: 'Exit ₹', w: 90, n: true, lo: true }, { h: 'Before', w: 100, n: true, lo: true, nw: true }, { h: 'Costs', w: 80, n: true, lo: true, nw: true }, { h: 'After', w: 104, n: true }, { h: 'Exit', w: 110 }, { h: '', w: 50, key: 'edit' }];
  const tradeRows = (d.trades || []).flatMap((t) => [{ key: t.id ?? t.symbol + t.in, cells: [<><b>{t.symbol}</b>{t.est && <Tag c="w" title="estimated exit: the price seen when sending, corrected to the day's open in the evening · ✎ to set the real fill">est. exit</Tag>}<Edited edits={t.edits} /></>, `${day(t.in)} · ${day(t.out)}`, t.qty, rs(t.sell), <span className={cls(t.gross)}>{signed(t.gross)}</span>, <span className="muted">{inr(t.costs)}</span>, <b className={cls(t.net)}>{signed(t.net)}</b>, EXIT[t.why] || t.why,
    t.id != null ? <button type="button" className={`btn ${tedit === t.id ? 'primary' : 'ghost'}`} style={PEN} title="correct the exit price / quantity" onClick={() => setTedit(tedit === t.id ? null : t.id)}>✎</button> : ''] },
  ...(tedit === t.id && t.id != null ? [{ key: `${t.id}-edit`, span: <TradeEditor t={t} onDone={done} onErr={setErr} /> }] : [])]);
  const capRow = <div className="rform"><div className="fr"><span>Capital<small>size {inr(d.limits?.size)} a stock (÷ {d.max_pos}) · largest order {inr(d.limits?.max_order)} ({s.max_order_pct}%) · whole shares, within the cash left</small></span>
    <span style={{ whiteSpace: 'nowrap' }}>₹<input className="input" type="number" min="10000" step="10000" value={cap} onChange={(e) => setCap(e.target.value)} style={{ width: 110 }} />{' '}
      <button type="button" className="btn small" onClick={saveCap} disabled={!(Number(cap) >= 10000) || Number(cap) === capNow}>Save</button></span></div></div>;

  return <div className="rd">
    <AttentionStrip tab="real" />
    {err && <div className="rbanner bad">{err}</div>}
    {failedToday.length > 0 && <div className="rbanner bad"><b>⚠️ {failedToday.length} order(s) failed today.</b>
      {failedToday.map((o) => <div key={o.symbol + o.action} style={{ marginTop: 6 }}><b className={o.action === 'BUY' ? 'up' : 'down'}>{o.action}</b> <b>{o.symbol}</b> {o.qty ?? ''}
        <span className="muted small"> · {o.reply}</span>{' '}
        <button type="button" className="btn small" disabled={!!busy || !live} onClick={() => retry(o)}>{busy === o.symbol ? 'Sending…' : '↻ Retry now'}</button></div>)}
      {!live && <div className="muted small">Retry needs real trading ON and the kill switch off.</div>}</div>}
    <Head title={`Real · ${variant}`} right={<>
      <button type="button" className="btn small" onClick={() => scrollTo('orderlog')}>Order log</button>
      <button type="button" className="btn small" onClick={() => scrollTo('tradelog')}>Trade log</button>
      <button type="button" className="btn small" onClick={() => scrollTo('reconcile')}>Reconcile</button></>}>
      <Chip dot={live ? 'ok' : 'bad'} c={live ? 'ok' : 'bad'}>{d.killed ? 'kill switch ON · nothing is sent' : d.enabled ? <>orders go out at <b>{ampm(s.send_time)}</b>{s.preview_time ? ` · reminder ${ampm(s.preview_time)}` : ''}</> : 'real trading OFF · nothing is sent'}</Chip>
      <Chip title="the market filter decided at the last close">today's filter <b className={filterState === 'OFF' ? 'down' : filterState ? 'up' : ''}>{filterState ? `${variant} ${filterState}${filterState === 'HALF' ? ` · ${d.filter.slots} slots` : ''}${d.filter.changed ? ` · was ${d.filter.prev}` : ''}` : 'after the first evening run'}</b>{d.decided ? ` · decided ${day(d.decided)}` : ''}</Chip>
      <Chip>{d.started ? <>started {day(d.start_date)} · updated {stamp(d.updated_at)}</> : 'starts at the next evening run'}</Chip>
      {!d.token_set && <Chip c="bad">webhook token missing in .env · orders can't be sent</Chip>}
    </Head>

    <div className="cols c4">
      <Panel title="Controls">
        <div className="rform">
          <div className="fr"><span>Real trading enabled<small>ON: tomorrow's orders are sent at {ampm(s.send_time)}; OFF: nothing is sent</small></span><Switch on={d.enabled} onChange={(on) => set({ enabled: on })} /></div>
          <div className="fr"><span>Kill switch · stop everything<small>{d.killed ? 'ON · blocks every order until turned off' : 'off · orders follow the switch above'}</small></span><Switch red on={d.killed} onChange={(on) => kill(on)} /></div>
          <div className="fr"><span>Send stop with each buy<small>a stop sent with the BUY is not moved later · set in the settings</small></span><Switch on={!!s.send_stop} disabled title="set in the settings file (real.send_stop)" /></div>
          <div className="fr"><span>Confirm before sending<small>the morning reminder lists the orders; each failure is retried only by you</small></span><Switch on disabled title="always: nothing is auto-retried" /></div>
        </div>
        <div className="rnote">status: <b className={live ? 'up' : 'down'}>{d.enabled ? (d.killed ? 'ON · but the kill switch blocks it' : 'Real trading ON') : 'Real trading OFF'}</b>{d.started && <> · <button type="button" className="linkbtn" onClick={reset}>Reset tracking…</button></>}</div>
      </Panel>
      <Panel title="Account" right={d.started && <Meta>capital <b className="mono">{inr(A.capital)}</b></Meta>}>
        {d.started ? <KV pairs={[
          ['Value', <>{inr(A.value)}<span className="muted"> · cash {inr(A.cash)}</span></>],
          ['Since start · after costs', <b className={cls(A.net)}>{signed(A.net)} · {pct(A.return)}</b>],
          ['Before costs · costs', <>{signed(A.gross)} · −{inr(A.costs)}</>],
          ['Realised · unrealised', `${signed(A.realised)} · ${signed(A.unrealised)}`],
          ['At risk if every stop hits', <span className="down">{inr(risk)}</span>],
          ['Won · profit factor · max DD', <>{d.won} of {d.closed} · {d.profit_factor ?? '—'} · <span className="down">{pct(-d.drawdown)}</span></>],
          ['NIFTY 50 same days', pct(A.nifty)],
          ['Positions', `${d.positions.length} of ${d.allowed ? d.cap : d.max_pos}`]]} />
          : <p className="muted small rempty">Starts with this capital at the next evening run{d.enabled ? '' : ' once real trading is ON'}.</p>}
        {capRow}
      </Panel>
      <Panel title="Safety limits">
        <KV pairs={[
          ['Max per order', <>{inr(d.limits?.max_order)}<span className="muted"> · {s.max_order_pct}%</span></>],
          ['Position size', <>{inr(d.limits?.size)}<span className="muted"> · value ÷ {d.max_pos}</span></>],
          ['Max open', d.max_pos],
          ['Daily new buys', s.max_buys_per_day],
          ['Orders sent', `${ampm(s.send_time)} – ${ampm(s.send_until)}`.replace(' AM –', ' –')],
          ['Reminder', s.preview_time ? `${ampm(s.preview_time)} · Telegram` : 'off'],
          ['Stop check', `every ${s.stop_check_min} min · live + low`],
          ['No-login alarm', `every ${s.nologin_alarm_min} min`],
          ['Stop with BUY', <b className={s.send_stop ? 'up' : 'muted'}>{s.send_stop ? 'ON' : 'OFF'}</b>],
          ['Retries', 'each order sent once']]} />
      </Panel>
      <Panel title="Strategy for new buys">
        <div className="rbody"><Chip><b className="vE">E+</b> · half filter, late trail · top {d.tops?.E ?? ''}</Chip></div>
        <div className="rnote">Half market filter: all slots above NIFTY's 200-day, half above the 50-day only, none below. Late trail: 25% under the highest close once +100%; only ever raised, from the next session.
          {' '}Open positions keep their own stop and exit.</div>
      </Panel>
    </div>

    {d.started ? <div className="two">
      <div className="rd" style={COL}>
        <Panel title="Orders for the next open" meta={(d.orders || []).length} right={<><Meta>decided {day(d.decided)} · sent {ampm(s.send_time)} · {live ? <b className="up">sending</b> : <b className="down">not sending</b>}</Meta>
          <button type="button" className="btn primary small" disabled={replanning} onClick={() => replan()} title="rebuild the plan from the evening's data and the account as it is now · nothing is sent">{replanning ? 'Planning…' : '↻ Re-evaluate plan'}</button></>}>
          <FixedTable cols={orderCols} rows={orderRows} empty="None." />
          {d.waiting > 0 && <div className="rnote">{d.waiting} more breakout(s) without a free slot / over the daily limit{d.waiting_why?.length ? `: ${d.waiting_why.slice(0, 6).map((w) => `${w.symbol} (${w.why})`).join(' · ')}${d.waiting_why.length > 6 ? ' · …' : ''}` : ''}.</div>}
        </Panel>
        <ReplanPanel r={rp} send={s} busy={replanning} onRaise={(n) => replan(n)} onKeep={() => setRp(null)} />
        <Panel id="positions" title="Open positions" meta={`${d.positions.length} of ${d.allowed ? d.cap : d.max_pos}`} right={<><Meta>stop check every {s.stop_check_min} min · at risk <b className="mono down">{inr(risk)}</b></Meta>
          <button type="button" className={`btn small ${adding ? 'primary' : ''}`} title="track a trade placed by hand · nothing is sent" onClick={() => { setAdding(!adding); setEdit(null); }}>+ Add a position</button></>}>
          {adding && <AddPosition key={typeof adding === 'object' ? adding.symbol : 'new'} d={d} init={typeof adding === 'object' ? adding : null} onDone={done} onErr={setErr} />}
          <FixedTable cols={posCols} rows={posRows} empty="None." />
          {d.checks?.length > 0 && <div className="rbanner warn" style={{ margin: '8px 14px' }}>⚠️ Position checks (warnings only, nothing is traded): {d.checks.join(' · ')}</div>}
          {d.notes?.length > 0 && <div className="rnote">{d.notes.map((n) => `📌 ${n.day}: ${n.text}`).join(' · ')}</div>}
          <div className="rnote">E+ positions: once the best close is +100% over the entry, the stop is raised to 25% under it after the close (never lowered) and watched from the next session like any stop; a stop sent with the BUY is not moved.
            {' '}No stop-loss orders: the app checks live prices every {s.stop_check_min} min and sends EXIT when a stop is reached (needs the daily Kite login). Prices are what the app saw when sending (est.), corrected to the day's open in the evening; your broker's fills may differ: ✎ sets the real fill, quantity or stop (every rule then follows the new values), "Remove from tracking…" drops an order that never filled, "+ Add a position" tracks a trade placed by hand. Nothing here sends an order.</div>
        </Panel>
        <BrokerCheck d={d} tick={tick} onChanged={reload} onErr={setErr} onAdd={(init) => { setAdding(init); setEdit(null); scrollTo('positions'); }} />
        <Panel id="tradelog" title="Trade log" meta={(d.trades || []).length}><FixedTable cols={tradeCols} rows={tradeRows} empty="None yet." /></Panel>
      </div>
      <div className="rd" style={COL}>
        <PaperVsReal />
        <Panel id="orderlog" title="Order log" meta={(d.log || []).length} right={<Meta>every webhook, never deleted · the token is never shown</Meta>}>
          {!d.log?.length ? <p className="muted small rempty">Nothing sent yet.</p> : d.log.slice(0, 60).map((o, k) => <Act key={k} icon={o.status === 'sent' ? '✓' : o.status === 'failed' ? '!' : SIDE[o.action] ? '·' : '✎'} cls={o.status === 'sent' ? 'up' : o.status === 'failed' ? 'down' : SIDE[o.action] ? 'warn' : ''}
            text={<><b className={SIDE[o.action] || 'muted'}>{o.action}</b> <b>{o.symbol}</b> {o.qty ?? ''}{o.price ? <span className="muted small"> ~{rs(o.price)}</span> : null} <Tag c={STATUS[o.status] || ''}>{o.status}</Tag></>}
            meta={`${stamp(o.created_at)}${o.reason ? ` · ${o.reason}` : ''}${o.reply ? ` · ${o.reply}` : ''}`}
            btn={o.status === 'failed' && o.day === todayIst ? '↻ Retry' : undefined} onBtn={() => { if (!busy && live) retry(o); }} />)}
        </Panel>
        <NextUp />
      </div>
    </div> : <div className="two">
      <Panel title="Not started" meta="no trades yet"><p className="muted small rempty">Real trading starts at the next evening run{d.enabled ? '' : ' once real trading is ON'}: the first orders are decided at the close and sent at {ampm(s.send_time)}.</p></Panel>
      <div className="rd" style={COL}>
        <Panel id="orderlog" title="Order log" meta={(d.log || []).length}>
          {!d.log?.length ? <p className="muted small rempty">Nothing sent yet.</p> : d.log.slice(0, 60).map((o, k) => <Act key={k} icon={o.status === 'sent' ? '✓' : '!'} cls={o.status === 'sent' ? 'up' : 'down'}
            text={<><b className={SIDE[o.action] || 'muted'}>{o.action}</b> <b>{o.symbol}</b> {o.qty ?? ''}</>} meta={`${stamp(o.created_at)}${o.reply ? ` · ${o.reply}` : ''}`} />)}
        </Panel>
        <NextUp />
      </div>
    </div>}
    <div className="rnote">Orders go out by webhook to the broker's side (or stay in its offline mode). The app never places an order in Kite; Kite is used only for prices. Past results don't guarantee future returns.</div>
  </div>;
}
