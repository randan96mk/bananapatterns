import React from 'react';
import { stamp, ampm } from '../timefmt';
import { inr } from '../fmt';
import { Panel, FixedTable, Tag, Act } from './ui2';

// ↻ Re-evaluate: the next open's plan rebuilt from the evening's data and the account as it is now (Real and Paper).
// Shows which breakouts fit and which wait, and why; offers to raise max open so the idle cash is used. Nothing here
// sends an order: the plan still goes out (or fills, for paper) the way it does today.
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');
const Meta = ({ children }) => <span style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{children}</span>;
const COLS = [{ h: 'Breakout' }, { h: 'Qty', w: 70, n: true }, { h: 'Capital', w: 110, n: true }, { h: 'Why', w: 220, ell: true }];
const SHOW = 8;                                            // rows shown before a "+n more" line

// the next weekday (IST); holidays are not known here, so it says "next session"
const nextSession = () => {
  const d = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  do { d.setDate(d.getDate() + 1); } while (d.getDay() === 0 || d.getDay() === 6);
  return d.toLocaleDateString('en-IN', { weekday: 'short' });
};

export default function ReplanPanel({ r, paper, send, onRaise, onKeep, busy }) {
  if (!r) return null;
  if (r.status !== 'ok') {
    return <Panel title="Re-evaluate" meta={r.status}><p className="muted small rempty">{r.error || (r.status === 'not_started' ? 'The account has not started yet: the first plan is made at the evening run.' : r.status)}</p></Panel>;
  }
  const added = new Set((r.added || []).map((o) => o.symbol + o.action));
  const taken = paper ? (r.taken || []) : (r.after || []).filter((o) => o.action === 'BUY' && !o.sent);
  const waiting = r.waiting || [];
  const rows = [
    ...taken.map((o) => ({ key: `t${o.symbol}`, cells: [<b>{o.symbol}</b>, o.qty, inr(o.capital ?? o.qty * o.close),
      <Tag c="ok">{added.has(`${o.symbol}BUY`) ? 'added' : paper ? 'fits' : 'planned'}</Tag>] })),
    ...waiting.slice(0, SHOW).map((w) => ({ key: `w${w.symbol}`, cells: [<b>{w.symbol}</b>, '—', '—', <span className="muted">{w.why}</span>] })),
    ...(waiting.length > SHOW ? [`+${waiting.length - SHOW} more · ${waiting[SHOW].why}`] : []),
  ];
  const sug = r.suggestion;
  const when = paper ? `fills at the next open (${nextSession()} 9:15 AM) in the paper account`
    : `orders go out ${nextSession()} ${ampm(send?.send_time || r.send_time)}${(send?.preview_time || r.preview_time) ? ` after the ${ampm(send?.preview_time || r.preview_time)} reminder` : ''}`;
  const raise_ = () => {
    const msg = `Raise max open from ${r.max_pos} to ${sug.max_pos} and re-plan?\n\n`
      + `Position size becomes ${inr(sug.size)} (value ÷ ${sug.max_pos}). ${sug.fits} of ${waiting.length} waiting breakout(s) fit: ${sug.symbols.join(', ')}. `
      + `${inr(sug.used)} used, ${inr(sug.left)} left.\n`
      + (paper ? `The paper account ${when}; nothing is sent anywhere.\n`
        : `The ${when}, with the confirm switch, the kill switch and the daily limit (${r.max_buys_per_day} a day) as today. Nothing is sent now.\n`)
      + `The backtest ran with max open ${r.bt_max_pos ?? 10}: re-run it to see the effect.`;
    if (window.confirm(msg)) onRaise(sug.max_pos);
  };
  return <Panel title={`Re-evaluated ${busy ? '…' : 'just now'}`} meta={stamp(r.at)}
    right={<Meta>capital <b className="mono">{inr(r.capital)}</b> · cash <b className="mono">{inr(r.cash)}</b> · {r.slots_used} of {r.slots} slots · filter {r.filter?.state || '—'} · scan of {day(r.decided)}</Meta>}>
    <FixedTable cols={COLS} rows={rows} empty="No breakout today: nothing to plan." />
    <div className="rnote" style={{ borderTop: '1px solid var(--rowline)', paddingTop: 10, fontSize: 12 }}>
      {waiting.length ? <><b style={{ color: 'var(--text)' }}>{inr(r.idle_cash)} is idle.</b> Position size is value ÷ max open ({r.max_pos} → {inr(r.size)}); the {r.slots_used} open positions keep their quantities, so the new money waits for an exit.</>
        : <><b style={{ color: 'var(--text)' }}>{inr(r.idle_cash)} is idle</b> · no breakout is waiting: the cash goes to the next breakouts at {inr(r.size)} each.</>}
    </div>
    {sug && <Act icon="↑" cls="act" text={<b>Raise max open to {sug.max_pos} and re-plan</b>}
      meta={`size becomes ${inr(sug.size)} · ${sug.fits} of ${waiting.length} breakout(s) fit (${sug.symbols.join(' · ')}) · ${inr(sug.used)} used · ${inr(sug.left)} left · ${when} · backtest ran with ${r.bt_max_pos ?? 10}: re-run it to see the effect`}
      btn={busy ? 'Planning…' : `Raise to ${sug.max_pos} · plan`} onBtn={() => { if (!busy) raise_(); }} />}
    <Act icon="=" text={<b>Keep max open at {r.max_pos}</b>}
      meta={waiting.length ? `cash waits for the next exit · new buys sized at ${inr(r.size)}` : `new buys sized at ${inr(r.size)}`}
      btn="Keep" onBtn={onKeep} />
    <div className="rnote">Nothing is sent by this panel. {paper ? 'Paper orders fill at the next open from the daily candle as today.'
      : `Orders still go out at ${ampm(send?.send_time || r.send_time)} with the confirm switch, the kill switch and the daily limit as today.`} Saving the Capital box runs this re-evaluation by itself.</div>
  </Panel>;
}
