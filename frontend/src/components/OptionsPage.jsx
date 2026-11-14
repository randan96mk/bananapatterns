import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp, clock, ampm } from '../timefmt';
import { inr, signed, pct, cls, lakh } from '../fmt';
import { useLive } from '../services/live';
import AttentionStrip from './AttentionStrip';
import NextUp from './NextUp';
import { Head, Chip, Switch, Tag, Panel, FixedTable, Act, Cards, Card, Bar } from './ui2';
import { useNarrow, useIsPhone } from '../useMedia';

// 🧩 NIFTY weekly options, paper only: one credit spread a day on the opening-range bias (9:45–11:00), confirmed by the
// option chain, out by 3:10 PM. Everything here reads GET /api/options (stream topic "options"); the switches, the
// kill switch, "Close now", "Hold today" and a deposit confirm first. The Real switch exists but is not wired yet.
const n2 = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const n0 = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 }));
const day = (d) => (d ? new Date(`${d.slice(0, 10)}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const wday = (d) => (d ? new Date(`${d.slice(0, 10)}T00:00:00`).toLocaleDateString('en-IN', { weekday: 'short' }) : '');
const short = (t) => (t || '').replace(/ (AM|PM)$/, '');                       // "9:50 AM" -> "9:50" on the strip (the full time is in its tooltip)
const mins12 = (t) => { const m = /(\d+):(\d+) (AM|PM)/.exec(t || ''); return m ? ((+m[1] % 12) + (m[3] === 'PM' ? 12 : 0)) * 60 + +m[2] : null; };
const COL = { gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' };
const ROW = { display: 'flex', gap: 10, alignItems: 'center', fontSize: 12.5, minHeight: 34, padding: '0 14px', borderBottom: '1px solid var(--rowline)', minWidth: 0 };
const LINE = { padding: '10px 14px 0', fontSize: 12.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
const Meta = ({ children, title }) => <span title={title} style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{children}</span>;
const Ell = ({ children, title, c = '' }) => <span className={c} title={title} style={{ display: 'block', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}>{children}</span>;
const RULE_LABELS = ['Book', 'Bias', 'Chain', 'Strikes', 'Size', 'Exits', 'No entry', 'Fills'];
const STEP_META = { 'Opening range': '15-minute high and low on the NIFTY 50 index, 9:15–9:30', 'VWAP (futures)': 'VWAP from the near-month NIFTY future’s 1-minute candles (the index has no volume)',
  'Previous day': 'the previous session’s high must be taken for a bullish bias, its low for a bearish one', 'Option chain': 'walls · OI change since 9:30 · PCR → score 0–3 · 2–3 full size · 1 half · 0 no trade' };
const STRIP_LABEL = { contracts: 'contracts', range: 'range set', open_snap: 'snapshot', entry: 'entered', be: 'breakeven', exit: 'exited', no_trade: 'no trade', close_snap: 'close snap',
  summary: 'summary', hold: 'hold', skip: 'no entry', disagree: 'chain says no', nochain: 'no chain', nolots: 'no size', nostrike: 'no strike', fail: 'failed', noquote: 'no quote', no_data: 'no data' };
const ENDED = (r) => (/target/.test(r) ? 'ok' : /stop|kill/.test(r) ? 'bad' : /hard|no data|closed by you/.test(r) ? 'w' : '');
const ALERT_ICON = (a) => ({ entry: '🧭', exit: /stop|kill/.test(a.text) ? '🛑' : '🎯', summary: '🌙', no_trade: '◦', chain_disagrees: '🧩', no_data: '❗' }[a.kind] || '⚠');
const ALERTS = [['🧭', '9:45–11:00', 'bias found with the strikes, credit, stop and worst case · or “no trade today” at 11:00'], ['🛑', 'live', 'premium stop · VWAP stop · breakeven stop armed'],
  ['🎯', 'live', 'target 60% hit · time exit 3:10'], ['⚠', 'daily', 'event day skipped · VIX > 20 half size · daily loss limit · chain disagrees'], ['🌙', '3:35', 'day summary: credit kept, trades, book']];

// Today's strip: what the book did (from the timeline), "now", what is still to come (from the schedule).
function DayStrip({ today, open, phone }) {
  const nowM = (() => { const d = new Date(); const p = d.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false }); return +p.slice(0, 2) * 60 + +p.slice(3, 5); })();
  const seen = new Set();
  const done = (today.timeline || []).map((x) => { const k = (x.key || '').split('|')[0]; const label = k.startsWith('bias') ? `bias ${/BEARISH/.test(x.key) ? '↓' : '↑'}` : STRIP_LABEL[k] || x.text.split(' · ')[0];
    return { t: x.t, label, title: `${x.t} · ${x.text}`, st: 'done' }; }).filter((x) => { const id = x.t + x.label; if (seen.has(id)) return false; seen.add(id); return true; });
  const status = { open: 'holding', waiting: 'waiting', no_trade: 'no trade', closed: 'closed', done: 'done', no_data: 'no data', holiday: 'holiday' }[today.status] || today.status;
  const lastM = Math.max(nowM, ...(today.timeline || []).map((x) => +x.hm.slice(0, 2) * 60 + +x.hm.slice(3, 5)));
  const future = (today.schedule || []).filter((s) => (mins12(s.t) ?? 0) > lastM).map((s) => ({ t: s.t.split('–')[0], label: s.text.split(' + ')[0].replace(' on 5-minute closes', ''), title: `${s.t} · ${s.text}`, st: 'future' }));
  const nDone = phone ? 3 : 4, nFut = phone ? 1 : 2;
  const items = [...done.slice(-nDone), { t: 'now', label: open ? 'holding' : status, title: `now · ${status}`, st: 'now' }, ...future.slice(0, nFut)];
  const col = { done: 'var(--ok)', now: 'var(--blue)', future: 'var(--border)' };
  return <div style={{ display: 'flex', gap: 6, padding: '10px 14px 6px' }}>
    {items.map((x, i) => <div key={i} title={x.title} style={{ flex: '1 1 0', minWidth: 0, borderTop: `2px solid ${col[x.st]}`, paddingTop: 5, color: x.st === 'future' ? 'var(--muted)' : 'var(--text)', fontSize: 11.5, lineHeight: 1.3 }}>
      <b style={{ display: 'block', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', color: x.st === 'now' ? 'var(--blue)' : undefined }}>{short(x.t)}</b>
      <span style={{ display: 'block', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{x.label}</span></div>)}
  </div>;
}

// The sold leg's stop meter: the premium from 0 (left) to the stop (right); the marker is where it trades now.
const StopBar = ({ last, stop }) => { const p = stop ? Math.max(2, Math.min(98, (last / stop) * 100)) : 0;
  return <span style={{ display: 'inline-block', verticalAlign: 'middle', width: 70, height: 6, borderRadius: 3, position: 'relative', marginRight: 8, background: 'linear-gradient(90deg, var(--ok), var(--warn), var(--bad))' }}>
    <i style={{ position: 'absolute', top: -3, left: `${p}%`, width: 2, height: 12, background: 'var(--text)' }} /></span>; };
const OiBar = ({ v, max, right, c }) => <span style={{ display: 'flex', justifyContent: right ? 'flex-end' : 'flex-start' }}>
  <span style={{ display: 'block', width: `${max ? Math.max(2, (100 * (v || 0)) / max) : 0}%`, maxWidth: 120, height: 10, borderRadius: 3, background: c, opacity: 0.85 }} /></span>;

export default function OptionsPage() {
  const live = useLive('options');
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [tick, setTick] = useState(0);
  const [cap, setCap] = useState('');
  const [busy, setBusy] = useState('');
  const narrow = useNarrow();
  const phone = useIsPhone();
  const reload = () => setTick((t) => t + 1);
  useEffect(() => { api.getOptions().then((x) => { setD(x); setErr(''); setCap((c) => c || String(x.capital)); }).catch((e) => setErr(e.message)); }, [tick]);
  useEffect(() => { if (live && !live.error) { setD(live); setCap((c) => c || String(live.capital)); } }, [live]);
  const act = (key, p) => { setBusy(key); p.then(() => { setErr(''); reload(); }).catch((e) => setErr(e.message)).finally(() => setBusy('')); };
  const setSw = (body, text) => { if (!text || window.confirm(text)) act('sw', api.putOptionsSettings(body)); };
  const kill = (on) => {
    if (!on || window.confirm('Kill the options book now?\n\nAn open spread is closed at once at the quotes (paper, mid ± ₹1 a leg) and no new entries are taken until you turn the kill switch off.')) act('kill', api.optionsKill(on));
  };
  const closeNow = () => {
    const o = d.open;
    if (window.confirm(`Close the open ${o.bias} spread now?\n\nBuy back ${n0(o.legs[0].strike)} ${o.legs[0].type} at the mid + ₹1 and sell the hedge ${n0(o.legs[1].strike)} ${o.legs[1].type} at the mid − ₹1 · ${o.lots} lot${o.lots === 1 ? '' : 's'} · MTM now ${signed(o.pnl)} (before the exit charges). Paper only; nothing is sent anywhere.`)) act('close', api.optionsClose());
  };
  const hold = () => {
    if (window.confirm(`Hold the options book today?\n\nNo new spread is opened today (${day(d.today.date)}); an open spread keeps its stops, target and the time exit. Tomorrow starts normally. The kill switch and the enable switch stay as they are.`)) act('hold', api.optionsHold());
  };
  const saveCap = () => {
    const v = Number(cap), now = d.capital;
    if (!(v >= 10000) || v === now) return;
    const text = d.started && d.deposits?.length + (d.days || []).length > 0 ? `Change the options capital from ${inr(now)} to ${inr(v)}? This counts as ${v > now ? 'adding' : 'withdrawing'} ${inr(Math.abs(v - now))}: the cash changes, the profit so far stays. Size follows (1 lot per ${lakh(d.margin.capital_per_lot)}).` : `Set the options capital to ${inr(v)}?`;
    if (window.confirm(text)) act('cap', api.optionsCapital(v));
  };
  if (!d) return <div className="rd"><Panel>{err ? <div className="rbanner bad">{err}</div> : <p className="muted small rempty">Loading…</p>}</Panel></div>;

  const sw = d.switches || {}, G = d.guards || {}, M = d.margin || {}, T = d.today || {}, o = d.open, B = d.bias || {}, C = d.chain || {}, st = d.settings || {};
  const days = d.days || [], traded = days.filter((x) => x.trades), won = traded.filter((x) => (x.pnl || 0) > 0);
  const biasRec = (b) => { const xs = traded.filter((x) => x.bias === b); return `${xs.filter((x) => x.pnl > 0).length} of ${xs.length}`; };
  const best = traded.length ? Math.max(...traded.map((x) => x.pnl || 0)) : null, worst = traded.length ? Math.min(...traded.map((x) => x.pnl || 0)) : null;
  const noTrade = days.filter((x) => x.status === 'no_trade').length;
  const lossPct = G.daily_loss?.limit ? Math.min(100, (100 * Math.max(0, G.daily_loss.used)) / G.daily_loss.limit) : 0;
  const marginPct = d.capital ? Math.min(100, (100 * (M.est || 0)) / d.capital) : 0;
  const toTarget = o ? Math.max(0, Math.min(100, (100 * (o.credit - o.value)) / (o.credit - o.target_value || 1))) : 0;
  const sessions = M.sessions_to_expiry;
  const otm = (leg) => (o?.spot ? `${(((leg.type === 'PE' ? o.spot - leg.strike : leg.strike - o.spot) / o.spot) * 100).toFixed(1)}% OTM` : '');
  const nextEvent = (G.event_days || []).find((x) => x >= T.date);
  const statusText = { waiting: G.trading_day === false ? 'Not a trading day · the book waits for the next session.' : `Waiting for a bias · ${G.window?.start}–${G.window?.end} on 5-minute closes${T.hold ? ' · on hold today' : ''}${G.kill ? ' · kill switch ON' : ''}.`,
    no_trade: `No trade today · ${T.why || 'no bias'}.`, closed: `Closed · ${T.why} · realised ${signed(T.realised)}${(T.trades || 0) < (G.trades?.max || 2) && G.window?.open ? ' · one re-entry possible on a flipped bias' : ''}.`,
    done: `Done for the day · ${T.why} · realised ${signed(T.realised)}.`, no_data: `${T.why || 'no data'} · the day is recorded as no data.`, holiday: 'Not a trading day.' }[T.status] || T.status;

  // the open spread's legs
  const legCols = [{ h: 'Leg', ell: true }, { h: 'Side', w: 62 }, { h: 'Lots', w: 50, n: true }, { h: 'Entry', w: 80, n: true }, { h: 'Now', w: 80, n: true }, { h: 'MTM', w: 96, n: true },
    { h: 'Stop · distance', w: narrow ? 170 : 200, ell: true }, { h: 'Delta · OTM', w: 140, lo: true, nw: true }, { h: 'Expiry', w: 140, lo: true }];
  const legRows = o ? o.legs.map((l) => ({ key: l.leg, title: `${l.tradingsymbol} · IV ${l.iv ?? '—'}% · delta ${l.delta?.toFixed(2)} · ${l.leg === 'sold' ? `stop ${n2(o.stop_premium)} (${o.stop_pct}%)${o.be_active ? ' · breakeven stop on' : ''}` : 'the hedge caps the loss at the spread width'}`,
    cells: [<><b>NIFTY {n0(l.strike)} {l.type}</b> <small className="muted">{l.leg === 'sold' ? 'sold' : 'hedge'} · {day(o.expiry)}</small></>, <Tag c={l.side === 'SELL' ? 'bad' : 'ok'}>{l.side}</Tag>, o.lots, n2(l.entry),
      <>{n2(l.last)}{o.marked_at && <span className="livedot" title={`marked ${clock(o.marked_at)}`} />}</>, <b className={cls(l.pnl)}>{signed(l.pnl)}</b>,
      l.leg === 'sold' ? <><StopBar last={l.last} stop={o.stop_premium} /><span className={`mono ${o.near_stop ? 'down' : 'up'}`}>{n2(o.stop_premium)} · {o.near_stop ? 'near stop' : o.be_active ? 'breakeven on' : 'stop far'}</span></> : <span className="muted">exits with the sold leg</span>,
      <span className="mono">{l.delta != null ? Math.abs(l.delta).toFixed(2) : '—'} · {otm(l)}</span>, <>{day(o.expiry)}{sessions != null && <span className="muted"> · {sessions} session{sessions === 1 ? '' : 's'}</span>}</>] })) : [];
  if (o) legRows.push(`${o.footer} · ${o.lots} lot${o.lots === 1 ? '' : 's'} × ${o.lot_size} · opened ${o.opened_t}${o.be_active ? '' : ` · breakeven stop at ${n2(o.be_at)}`}`);
  if ((T.closed || []).length) legRows.push(`closed today: ${T.closed.map((c) => `#${c.n} ${c.why} ${signed(c.net)} at ${c.t}`).join(' · ')}`);

  // the chain: ±5 strikes around the money, widened to the open legs
  const strikes = C.strikes || [];
  const atm = C.atm || (strikes.length ? strikes[Math.floor(strikes.length / 2)].strike : null);
  const legStrikes = o ? o.legs.map((l) => l.strike) : [];
  const shown = strikes.filter((s) => Math.abs(s.strike - atm) <= 250 || legStrikes.includes(s.strike)).sort((a, b) => b.strike - a.strike);
  const maxOi = Math.max(1, ...shown.flatMap((s) => [s.ce?.oi || 0, s.pe?.oi || 0]));
  const chg = (x) => { if (!x || x.oi_chg == null) return <span className="muted">—</span>; const base = (x.oi || 0) - x.oi_chg; const p = base ? (100 * x.oi_chg) / base : 0;
    return <span className={`mono ${x.oi_chg > 0 ? 'up' : x.oi_chg < 0 ? 'down' : 'muted'}`} title={`${n0(x.oi)} OI · ${x.oi_chg > 0 ? '+' : ''}${n0(x.oi_chg)} since the baseline`}>{p > 0 ? '+' : p < 0 ? '−' : ''}{Math.abs(p).toFixed(0)}%</span>; };
  const mark = (k) => { if (o && k === o.legs[0].strike) return <span className="up">sold ✓</span>; if (o && k === o.legs[1].strike) return <span className="muted">hedge</span>; if (k === atm) return <span>◆ spot</span>;
    if (k === C.walls?.support) return <span className="muted small">put wall</span>; if (k === C.walls?.resistance) return <span className="muted small">call wall</span>; return ''; };
  const chainCols = [{ h: 'Call OI', n: true }, { h: 'Δ OI', w: 64, n: true }, { h: 'IV', w: 54, n: true, lo: true }, { h: 'Strike', w: 84, n: true }, { h: '', w: 90, key: 'mark' }, { h: 'IV', w: 54, n: true, lo: true }, { h: 'Δ OI', w: 64, n: true },
    { h: 'Put OI', w: narrow ? 160 : 260 }];
  const chainRows = shown.map((s) => ({ key: s.strike, cls: o && s.strike === o.legs[0].strike ? '' : undefined, title: `${n0(s.strike)} · call OI ${n0(s.ce?.oi)} · put OI ${n0(s.pe?.oi)} · call delta ${s.ce?.delta?.toFixed(2) ?? '—'} · put delta ${s.pe?.delta?.toFixed(2) ?? '—'}`,
    cells: [<OiBar v={s.ce?.oi} max={maxOi} right c="var(--warn)" />, chg(s.ce), <span className="mono muted">{s.ce?.iv ?? '—'}</span>, <b className="mono">{n0(s.strike)}</b>, mark(s.strike), <span className="mono muted">{s.pe?.iv ?? '—'}</span>, chg(s.pe),
      <OiBar v={s.pe?.oi} max={maxOi} c="var(--ok)" />] }));
  const chainLine = C.spot ? `spot ${n0(C.spot)} · put wall ${n0(C.walls?.support)} (support) · call wall ${n0(C.walls?.resistance)} (resistance) · put IV ${C.iv?.put ?? '—'}% · call IV ${C.iv?.call ?? '—'}% · OI change since ${C.baseline === 'open' ? '9:30' : C.baseline === 'close' ? 'the previous close' : '—'} · green = building, red = unwinding${C.at ? ` · ${clock(C.at)}` : ''}` : '';

  // the bias steps (+ the entry line when a spread was opened today)
  const steps = (B.steps || []).map((s) => ({ n: s.n, name: s.name, text: s.text, ok: s.ok }));
  const todayTrade = o || (d.trades || []).find((t) => t.day === T.date);
  if (todayTrade) {
    const s = todayTrade.legs ? { sold: { strike: todayTrade.legs[0].strike, type: todayTrade.legs[0].type, entry: todayTrade.legs[0].entry }, hedge: { strike: todayTrade.legs[1].strike, type: todayTrade.legs[1].type, entry: todayTrade.legs[1].entry } } : todayTrade;
    steps.push({ n: 5, name: 'Entry', ok: true, text: `sold ${n0(s.sold.strike)} ${s.sold.type} at ${n2(s.sold.entry)} · hedge ${n0(s.hedge.strike)} ${s.hedge.type} at ${n2(s.hedge.entry)} · ${todayTrade.lots} lot${todayTrade.lots === 1 ? '' : 's'} · credit ₹${n0(todayTrade.credit_rs)}`,
      meta: `${todayTrade.opened_t || todayTrade.in} · gap ${T.gap_pct != null ? pct(T.gap_pct, 2) : '—'} · VIX ${T.vix ?? '—'} · ${sessions ?? '—'} session${sessions === 1 ? '' : 's'} to expiry${T.size_note ? ` · ${T.size_note}` : ''}` });
  }

  const dayCols = [{ h: 'Day · spread', ell: true }, { h: 'Bias', w: 50 }, { h: 'P&L', w: 96, n: true }, { h: 'Ended', w: 100, ell: true }];
  const dayRows = days.map((x) => ({ key: x.day, title: `${x.day} · ${x.result || x.why || ''}${x.chain_line ? ` · ${x.chain_line}` : ''}${x.size_note ? ` · ${x.size_note}` : ''}`,
    cells: [<><b>{wday(x.day)} {x.day.slice(8)}</b> <small className="muted">{x.strikes ? `${x.strikes}${x.lots ? ` · ${x.lots} lot${x.lots === 1 ? '' : 's'}` : ''}` : x.no_data ? 'no data' : 'no trade'}</small></>, x.bias ? <span className={x.bias === 'BULLISH' ? 'up' : 'down'}>{x.bias === 'BULLISH' ? '↑' : '↓'}</span> : <span className="muted">—</span>,
      <b className={x.trades ? cls(x.pnl) : 'muted'}>{x.trades ? signed(x.pnl) : '₹0'}</b>,
      <Tag c={x.trades ? ENDED(x.result || '') : x.no_data ? 'w' : ''} title={x.result || x.why}>{x.trades ? x.result : x.no_data ? 'no data' : 'no trade'}</Tag>] }));
  const card = (c) => ({ rs: inr(c.value), rs_signed: signed(c.value), pct: c.value == null ? '—' : `${Math.round(c.value)}%`, n: c.value ?? '—' }[c.fmt] ?? c.value);
  const byKey = Object.fromEntries((d.cards || []).map((c) => [c.key, c]));
  const expiryChip = M.expiry ? <Chip title={`the nearest NIFTY weekly expiry from the contracts dump (as of ${d.contracts?.as_of || '—'})`}>expiry <b>{wday(M.expiry)} {day(M.expiry)}</b> · {sessions} session{sessions === 1 ? '' : 's'}{M.expiry_day ? <b className="warn"> · expiry day</b> : null} · lot {M.lot_size ?? '—'}</Chip>
    : <Chip title="the weekly chain is read from the Kite instruments dump on the first tick of a trading day">no contracts yet · read on the first tick</Chip>;
  const capRow = <div className="rform"><div className="fr"><span>Capital<small>a change is money added / withdrawn · {(d.deposits || []).length} deposit{(d.deposits || []).length === 1 ? '' : 's'}</small></span>
    <span style={{ whiteSpace: 'nowrap' }}>₹<input className="input" type="number" min="10000" step="10000" value={cap} onChange={(e) => setCap(e.target.value)} style={{ width: 110 }} />{' '}
      <button type="button" className="btn small" onClick={saveCap} disabled={!!busy || !(Number(cap) >= 10000) || Number(cap) === d.capital}>Deposit…</button></span></div></div>;

  return <div className="rd">
    <AttentionStrip tab="options" />
    {err && <div className="rbanner bad">{err}</div>}
    <Head title={phone ? '🧩 NIFTY Options' : '🧩 NIFTY Options · directional credit spreads · intraday'} right={<>
      {o && <button type="button" className="btn ghost small" style={{ color: 'var(--bad)', borderColor: 'var(--bad)' }} disabled={!!busy} onClick={closeNow} title="close the open spread now at the quotes (paper) · confirm first">{busy === 'close' ? 'Closing…' : 'Close now…'}</button>}
      <button type="button" className="btn ghost small" disabled={!!busy || T.hold} onClick={hold} title={T.hold ? 'already on hold today' : 'no new spread today · an open one keeps its exits'}>{T.hold ? 'On hold today' : busy === 'hold' ? 'Holding…' : 'Hold today…'}</button></>}>
      <Chip>capital <b>{inr(d.capital)}</b> · separate book</Chip>
      {expiryChip}
      <Chip dot={G.kill ? 'bad' : o ? 'ok' : 'warn'} c={G.kill ? 'bad' : ''}>{G.kill ? 'kill switch ON · nothing opens' : !G.enabled ? 'options OFF · no new entries' : o ? <><b>1</b> spread open · bias <b className={o.bias === 'BULLISH' ? 'up' : 'down'}>{o.bias}</b> since {B.at}</>
        : B.state ? <>bias <b className={B.state === 'BULLISH' ? 'up' : 'down'}>{B.state}</b> at {B.at} · {T.status === 'closed' || T.status === 'done' ? `closed · ${T.why}` : 'no spread open'}</> : <>no spread open · {T.status === 'no_trade' ? 'no trade today' : G.window?.open ? 'looking for a bias' : G.trading_day === false ? 'not a trading day' : `bias ${G.window?.start}–${G.window?.end}`}</>}</Chip>
      <Chip><Tag c="ok">PAPER</Tag> · real not wired</Chip>
      {d.updated_at && <Chip title="the book's last state change">since {day(d.start_date)} · updated {stamp(d.updated_at)}</Chip>}
    </Head>
    <div className="cols c4">
      <Panel title="Switches" meta="paper only for now">
        <div style={ROW}><Switch on={sw.paper} disabled={!!busy} onChange={(on) => setSw({ paper: on }, on ? 'Turn the Paper switch on?\n\nThe book ticks every minute 9:15–3:40 on trading days: range at 9:30, the bias 9:45–11:00, paper fills at the mid ± ₹1 a leg.' : 'Turn the Paper switch off?\n\nNo new spread is opened; an open spread still runs to its exit (nothing is held overnight).')} /><b>Paper</b><Meta><span className="muted">fills at mid ± ₹1 a leg · MTM every minute</span></Meta></div>
        <div style={ROW}><Switch red on={false} disabled title={sw.real_note} /><b className="muted">Real</b><Meta><span className="muted">{sw.real_note || 'no webhook yet · stays off'}</span></Meta></div>
        <div style={ROW}><Switch on={sw.enabled} disabled={!!busy} onChange={(on) => setSw({ enabled: on }, on ? `Enable the options book?\n\nIt looks for a bias ${G.window?.start}–${G.window?.end} on 5-minute closes and opens one credit spread when the chain agrees.` : 'Disable the options book?\n\nNo new entries; an open spread keeps its stops, target and the time exit. Paper marks go on.')} /><b>Enabled</b><Meta><span className="muted">looks for a bias {G.window?.start}–{G.window?.end}</span></Meta></div>
        <div style={ROW}><Switch red on={G.kill} disabled={!!busy} onChange={kill} /><b>Kill switch</b><Meta><span className="muted">closes the spread, no new entries</span></Meta></div>
        <div className="rnote">{T.hold ? '⏸ on hold today · ' : ''}{G.no_data ? '❗ no data today · ' : ''}tagged “paper” · Telegram: opt close · opt hold</div>
      </Panel>
      <Panel title="Today" meta={`${wday(T.date)} ${day(T.date)}`} right={<Meta>{G.trading_day === false ? 'not a trading day' : T.status === 'open' ? 'holding' : T.status}</Meta>}>
        <DayStrip today={T} open={!!o} phone={phone} />
        <div className="rnote" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', paddingTop: 4 }} title={[T.range && `range ${n0(T.range.low)} – ${n0(T.range.high)}`, T.vwap && `VWAP (futures) ${n0(T.vwap)}`, T.prev && `prev day ${n0(T.prev.low)} – ${n0(T.prev.high)}`, T.gap_pct != null && `gap ${pct(T.gap_pct, 2)}`, T.vix && `VIX ${T.vix}`, G.event_day ? 'EVENT DAY · no entry' : 'no event today'].filter(Boolean).join(' · ')}>
          {T.range ? <>range {n0(T.range.low)} – {n0(T.range.high)} · VWAP {T.vwap ? n0(T.vwap) : '—'} · prev {B.state === 'BEARISH' ? `low ${n0(T.prev?.low)}` : `high ${n0(T.prev?.high)}`}{B.state ? ' taken' : ''} · gap {T.gap_pct != null ? pct(T.gap_pct, 2) : '—'} · VIX {T.vix ?? '—'} · {G.event_day ? <span className="down">event day</span> : 'no event today'}</>
            : <>range at 9:30 · VWAP (futures) · prev day {T.prev ? `${n0(T.prev.low)} – ${n0(T.prev.high)}` : '—'} · {G.event_day ? <span className="down">event day · no entry</span> : nextEvent ? `next event day ${day(nextEvent)}` : 'no event today'}</>}
        </div>
      </Panel>
      <Panel title="Guards">
        {[[`Daily loss limit · ${st.daily_loss_pct}%`, <span className={G.daily_loss?.hit ? 'down' : 'up'}><b className="mono">{inr(G.daily_loss?.limit)}</b> · used {lossPct.toFixed(0)}%{G.daily_loss?.hit ? ' · HIT' : ''}</span>],
          ['Trades today', <span><b>{G.trades?.used ?? 0} of {G.trades?.max}</b> · {Math.max(0, (G.trades?.max || 0) - (G.trades?.used || 0))} re-entry left</span>],
          ['Entry window', <span>{short(G.window?.start)} – {G.window?.end} · <b className={G.window?.open ? 'up' : 'warn'}>{G.window?.open ? 'open' : 'closed'}</b></span>],
          ['No-trade days', <span title={(G.event_days || []).length ? `event days: ${G.event_days.join(', ')} (settings → options → event_days)` : 'no event days set (settings → options → event_days)'}>{(G.event_days || []).length} event day{(G.event_days || []).length === 1 ? '' : 's'}{nextEvent ? ` (next ${day(nextEvent)})` : ''} · gap &gt; {G.gap?.max}% · VIX &gt; {G.vix?.half_at} halves size</span>],
          ['Expiry day', <span>allowed · premium stop {st.stop_pct_expiry}%{M.expiry_day ? <b className="warn"> · today</b> : ''}</span>],
          ['Now', <span className={G.kill || T.hold || G.no_data ? 'down' : ''}>{[G.kill && 'kill ON', T.hold && 'hold today', G.no_data && 'no data', G.gap?.pct != null && `gap ${pct(G.gap.pct, 2)} ${G.gap.ok ? '✓' : '✗'}`, G.vix?.value != null && `VIX ${G.vix.value}${G.vix.half ? ' · half size' : ''}`].filter(Boolean).join(' · ') || 'all clear'}</span>]]
          .map(([k, v]) => <div key={k} style={{ display: 'flex', gap: 12, padding: '6px 14px', borderBottom: '1px solid var(--rowline)', fontSize: 12.5 }}><span className="muted" style={{ flex: 'none', width: 104, lineHeight: 1.25 }}>{k}</span><span style={{ flex: 1, minWidth: 0, textAlign: 'right', overflowWrap: 'anywhere' }}>{v}</span></div>)}
      </Panel>
      <Panel title="Margin · risk" meta="spread, defined">
        <div style={LINE} title={`estimate: ${inr(M.per_lot)} a lot × ${M.lots} lot${M.lots === 1 ? '' : 's'} · worst case = (spread width ${st.hedge_points} − credit) × lot × lots`}>margin <b className="mono">{inr(M.est)}</b> · <span className="mono">{marginPct.toFixed(0)}%</span> of capital · worst case <b className="mono down">{M.worst_case != null ? `−${inr(M.worst_case)}` : '—'}</b></div>
        <div style={{ height: 10, borderRadius: 5, background: 'var(--hover)', overflow: 'hidden', margin: '6px 14px' }}><i style={{ display: 'block', height: '100%', width: `${marginPct}%`, background: marginPct >= 80 ? 'var(--bad)' : marginPct >= 50 ? 'var(--warn)' : 'var(--ok)' }} /></div>
        <div className="rnote" style={{ paddingTop: 0 }}>1 lot per {lakh(M.capital_per_lot)} · {M.max_lots} lots max · {o ? `${o.lots} lot${o.lots === 1 ? '' : 's'} open` : `${M.lots} lot${M.lots === 1 ? '' : 's'} planned`} · the bought leg caps the loss at the width{st.vix_half ? ` · VIX > ${st.vix_half} halves` : ''}</div>
        {capRow}
      </Panel>
    </div>
    <Cards n={6}>
      <Card label="Options · today" value={card(byKey.today || { fmt: 'rs_signed' })} num={byKey.today?.value} k={cls(byKey.today?.value)} live={!!o} spark={(d.curve?.net || []).slice(-30)}
        sub={byKey.today?.sub} fact={o ? <>to target <Bar pct={toTarget} ok={toTarget >= 100} /> {toTarget.toFixed(0)}%</> : <span className="muted">realised {signed(d.realised_today)} · charges in the net</span>}
        title={o ? `credit ${n2(o.credit)} · value now ${n2(o.value)} · target ${n2(o.target_value)} · ${o.decay_pct}% of the sold premium decayed` : byKey.today?.sub} />
      <Card label={`Book · since ${day(d.start_date) || 'start'}`} value={card(byKey.equity || { fmt: 'rs' })} num={byKey.equity?.value} k={cls(d.net)} spark={(d.curve?.net || []).slice(-30)}
        sub={`${pct(d.return, 2)} · ${days.length} day${days.length === 1 ? '' : 's'} · ${traded.length} traded · won ${traded.length ? Math.round((100 * won.length) / traded.length) : 0}%`}
        fact={traded.length ? <>best <b className="up">{signed(best)}</b> · worst <b className="down">{signed(worst)}</b></> : <span className="muted">no traded day yet</span>} title={`equity ${inr(d.equity)} · cash ${inr(d.cash)} · net ${signed(d.net)} · charges ${inr(d.charges)}`} />
      <Card label="Capital" value={card(byKey.capital || { fmt: 'rs' })} num={byKey.capital?.value} sub={byKey.capital?.sub} fact={<>lot {M.lot_size ?? '—'} · {lakh(M.capital_per_lot)} a lot · {M.max_lots} max</>} title="the options book's own capital · deposits are money added after the start" />
      <Card unit="%" label="Win rate" value={card(byKey.win || { fmt: 'pct' })} num={byKey.win?.value} k={byKey.win?.value == null ? 'muted' : byKey.win.value >= 50 ? 'up' : 'down'} sub={byKey.win?.sub}
        fact={<>bias record <b className="up">↑ {biasRec('BULLISH')}</b> · <b className="down">↓ {biasRec('BEARISH')}</b></>} title="days traded that ended in a gain · won of found, per bias side" />
      <Card unit="n" label="Days" value={card(byKey.days || { fmt: 'n' })} num={byKey.days?.value} sub={byKey.days?.sub} fact={<>no trade <Bar pct={days.length ? (100 * noTrade) / days.length : 0} /> {noTrade} of {days.length}</>} title="trading days the book has seen · no trade = no break by the window's end, a filter, or the chain disagreed" />
      <Card label="Charges" value={card(byKey.charges || { fmt: 'rs' })} num={byKey.charges?.value} k="muted" sub={byKey.charges?.sub} fact={o ? <>entry charges <b>{inr(o.charges_in)}</b></> : <span className="muted">4 orders a round trip</span>} title="brokerage ₹20 an order · STT 0.1% on the sell side · exchange 0.0495% · SEBI · GST 18% · stamp 0.003% on the buy side" />
    </Cards>
    <div className="two">
      <div className="rd" style={COL}>
        <Panel title="Open spread · paper" meta={o ? 1 : 0} right={<><Meta>marked every minute from the quotes’ mids · stops every minute · VWAP on 5-minute closes · nothing held overnight</Meta>
          {o && <button type="button" className="btn ghost small" style={{ color: 'var(--bad)', borderColor: 'var(--bad)' }} disabled={!!busy} onClick={closeNow}>{busy === 'close' ? 'Closing…' : 'Close now…'}</button>}</>}>
          <FixedTable cols={legCols} rows={legRows} empty={statusText} />
          {!o && (T.closed || []).length > 0 && <div className="rnote">closed today: {T.closed.map((c) => `#${c.n} ${c.why} ${signed(c.net)} at ${c.t}`).join(' · ')}</div>}
          <div className="rnote">Sold leg: stop at +{st.stop_pct}% of its entry premium ({st.stop_pct_expiry}% on expiry day) or a 5-minute close back through VWAP · breakeven stop once it has decayed {st.be_after_pct}% · target when the spread is worth {100 - st.target_pct}% of the credit · time exit {ampm(st.time_exit)} · hard exit 3:20 PM. Paper fills at the mid ± ₹1 a leg, never at the last price. Nothing here places an order.</div>
        </Panel>
        <Panel title={`Option chain${C.expiry ? ` · ${day(C.expiry)}` : ''}`} meta={C.score != null ? `score ${C.score} of 3 · PCR ${C.pcr ?? '—'}` : C.pcr != null ? `PCR ${C.pcr}${C.kind ? ` · last ${C.kind} snapshot` : ''}` : null}
          right={<Meta>+1 spot above the call wall or ≥ 0.5% over the put wall · +1 put OI building, call OI not · +1 PCR &gt; {st.pcr_bull} (bullish; mirror for bearish, PCR &lt; {st.pcr_bear}) · 2–3 full size · 1 half · 0 no trade</Meta>}>
          {chainLine && <div className="rnote" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', paddingBottom: 4 }} title={chainLine}>{chainLine}</div>}
          {phone && shown.length ? <div style={{ padding: '2px 10px 6px' }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 40px 92px 40px minmax(0, 1fr)', gap: 6, fontSize: 10.5, color: 'var(--muted)', padding: '4px 0' }}><span style={{ textAlign: 'right' }}>CALL OI</span><span>Δ</span><span style={{ textAlign: 'center' }}>STRIKE</span><span>Δ</span><span>PUT OI</span></div>
            {shown.map((s) => <div key={s.strike} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 40px 92px 40px minmax(0, 1fr)', gap: 6, alignItems: 'center', height: 30, borderBottom: '1px solid var(--rowline)', fontSize: 11.5 }}>
              <OiBar v={s.ce?.oi} max={maxOi} right c="var(--warn)" />{chg(s.ce)}
              <span style={{ textAlign: 'center', whiteSpace: 'nowrap', overflow: 'hidden' }}><b className={`mono ${o && s.strike === o.legs[0].strike ? 'up' : ''}`}>{n0(s.strike)}</b>{o && s.strike === o.legs[0].strike ? <small className="up"> ✓</small> : o && s.strike === o.legs[1].strike ? <small className="muted"> h</small> : s.strike === atm ? <small> ◆</small> : null}</span>
              {chg(s.pe)}<OiBar v={s.pe?.oi} max={maxOi} c="var(--ok)" /></div>)}
            <div className="rnote" style={{ padding: '6px 0 0' }}>✓ sold · h hedge · ◆ spot</div></div>
            : <FixedTable cols={chainCols} rows={chainRows} empty="No chain yet · the first snapshot comes at 9:30 on a trading day (needs the Kite login)." />}
          {C.line && <div className="rnote">{C.line}{C.crowded ? ' · PCR crowded: half size' : ''}</div>}
        </Panel>
        <Panel title="How the bias was found" meta={B.at || `${B.window?.start}–${B.window?.end}`} right={B.state ? <Tag c={B.state === 'BULLISH' ? 'ok' : 'bad'}>{B.state}{B.score != null ? ` · score ${B.score}` : ''}</Tag> : <Meta>no bias yet · 5-minute closes {B.window?.start}–{B.window?.end}</Meta>}>
          {steps.map((s) => <Act key={s.n} icon={s.n} cls={s.ok === true ? 'up' : s.ok === false ? 'down' : ''} text={<><b>{s.name}</b> · {s.text}</>} meta={s.meta || STEP_META[s.name]} />)}
          {(B.checks || []).length > 0 && <div className="rnote">checked {B.checks.map((c) => c.t).join(' · ')}{B.checks.length >= 16 ? '' : ' · a 5-minute close is tested on each slot'}</div>}
        </Panel>
      </div>
      <div className="rd" style={COL}>
        <Panel title="Rules" meta="directional seller">
          {(d.rules || []).map((r, i) => <div key={i} style={{ display: 'flex', gap: 12, padding: '6px 14px', borderBottom: '1px solid var(--rowline)', fontSize: 12.5 }}><span className="muted" style={{ width: 62, flex: 'none' }}>{RULE_LABELS[i] || ''}</span><span style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{r}</span></div>)}
        </Panel>
        <Panel title="Recent days · paper" meta={days.length} right={traded.length ? <Meta>won {won.length} of {traded.length} traded · {noTrade} no trade</Meta> : null}>
          <FixedTable cols={dayCols} rows={dayRows} empty="No day summarised yet (the first comes at 3:35 PM on a trading day)." />
        </Panel>
        <Panel title="Alerts · options" meta={(d.alerts || []).length || null} right={<Meta>tagged “paper” · Telegram: opt close · opt hold</Meta>}>
          {(d.alerts || []).length ? d.alerts.slice(0, 8).map((a, i) => <Act key={i} icon={ALERT_ICON(a)} cls={a.kind === 'entry' ? 'up' : /stop|kill/.test(a.text) && a.kind === 'exit' ? 'down' : a.kind === 'exit' ? 'up' : a.kind === 'summary' ? '' : 'warn'} text={<><b>{a.t}</b> · {a.text}</>} meta={`${day(a.day)} · ${a.kind.replace('_', ' ')} · ${a.tag}`} />)
            : ALERTS.map(([i, t, x]) => <Act key={t} icon={i} text={<><b>{t}</b> · {x}</>} />)}
        </Panel>
        {!phone && <NextUp />}
      </div>
    </div>
    <div className="rnote">NIFTY weekly options only, paper only: the bias on the index’s 5-minute closes, VWAP from the near-month future, the chain from one quote call. Fills at the mid ± ₹1 a leg; charges per order like a real account. The Real switch stays off until a webhook exists. The app never places an order in Kite; Kite is used only for quotes and candles. Past results don’t guarantee future returns.</div>
  </div>;
}
