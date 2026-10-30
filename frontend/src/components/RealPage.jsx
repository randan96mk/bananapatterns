import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { useIsPhone } from '../useMedia';
import AttentionStrip from './AttentionStrip';
import { FilterBadge } from './BreakoutPage';

const EXIT = { stop: '🛑 stop', exit: '📉 trend exit' };
const STATUS = { sent: 'ok', failed: 'err', skipped: 'wait' };
const inr = (v) => (v == null ? '—' : `${v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
const sgn = (v) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
const rs = (v) => (v == null ? '—' : `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`);
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down');
const day = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '');

// The 💰 Real trading tab: the Trend Breakout orders sent to Tradetron by webhook (BUY / EXIT), tracked like paper.
export default function RealPage() {
  const phone = useIsPhone();
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [tick, setTick] = useState(0);
  const [cap, setCap] = useState('');
  const reload = () => setTick((t) => t + 1);

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
    if (window.confirm(msg)) api.realCapital(v).then(reload).catch((e) => setErr(e.message));
  };

  const set = (body) => api.realSettings(body).then(reload).catch((e) => setErr(e.message));
  const kill = (on) => {
    if (!on || window.confirm('Stop ALL real orders now? Nothing is sent until you turn the kill switch off.')) {
      api.realKill(on).then(reload).catch((e) => setErr(e.message));
    }
  };
  const reset = () => {
    if (window.confirm('Reset the real account tracking? Positions, trades and the curve are cleared (the order log stays). Nothing is sent to Tradetron.')) {
      api.realReset().then(reload).catch((e) => setErr(e.message));
    }
  };

  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;
  const s = d.settings || {};
  const live = d.enabled && !d.killed;
  const A = d.account;
  return (
    <div className="orb">
      <div className="realbanner"><span>💰 REAL TRADING · orders go to Tradetron by webhook (BUY / EXIT) · never to Kite</span>
        <span className="muted small">{d.started ? <>Started {day(d.start_date)} · capital {inr(A.capital)} · updated {stamp(d.updated_at)}</> : 'Starts at the next evening run'}</span></div>
      <AttentionStrip tab="real" />
      {err && <div className="warn small">{err}</div>}

      <div className="realctrl">
        <div className="panel"><h3>Controls</h3>
          <label className="realsw"><input type="checkbox" checked={d.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
            <b className={live ? 'up' : 'down'}>{d.enabled ? (d.killed ? 'ON · but the kill switch blocks it' : 'Real trading ON') : 'Real trading OFF'}</b></label>
          <p className="muted small">ON: tomorrow's orders are sent at {s.send_time}; OFF: nothing is sent.</p>
          <p><b>Strategy for new buys</b>{' '}
            <span className="seg">{['A', 'E'].map((k) => (
              <button key={k} className={d.variant === k ? 'active' : ''} onClick={() => set({ variant: k })}>
                {k === 'A' ? 'A · 200-day' : 'E · half'} · top {d.tops?.[k]}</button>))}</span></p>
          <p className="muted small">Switching changes only NEW buys; open positions keep their own stop and exit.</p>
          {d.killed ? <button className="btn primary" onClick={() => kill(false)}>Turn the kill switch off</button>
            : <button className="btn killbtn" onClick={() => kill(true)}>⛔ Stop all · kill switch</button>}
          {!d.token_set && <p className="warn small">TRADETRON_TOKEN is missing in .env: orders can't be sent.</p>}
        </div>
        <div className="panel"><h3>Today's filter {d.decided && <span className="muted small">· {day(d.decided)} close</span>}</h3>
          {d.filter ? <FilterBadge k={d.variant} f={d.filter} /> : <p className="muted">After the first evening run.</p>}
          <h4>Safety limits</h4>
          <p className="small">orders sent {s.send_time}–{s.send_until} · stops checked every {s.stop_check_min} min in market hours ·
            {' '}max {s.max_buys_per_day} new buys a day · max {s.max_order_pct}% of the account an order ({inr(d.limits?.max_order)} now) · max {d.max_pos} positions ·
            {' '}each order sent once (a failure is not retried)</p></div>
        <div className="panel"><h3>Account</h3>
          <div className="capbox"><span>Capital</span> ₹<input className="input" type="number" min="10000" step="10000" value={cap}
            onChange={(e) => setCap(e.target.value)} /> <button className="btn" onClick={saveCap}
            disabled={!(Number(cap) >= 10000) || Number(cap) === (d.started ? d.account.capital : d.settings.capital)}>Save</button></div>
          <p className="muted small">Sizes follow automatically: <b>{inr(d.limits?.size)}</b> a stock now (account value ÷ {d.max_pos}) ·
            {' '}largest order <b>{inr(d.limits?.max_order)}</b> ({s.max_order_pct}%). Quantity = size ÷ price, whole shares, within the cash left.</p>
          {!d.started ? <p className="muted">Starts with this capital at the next evening run{d.enabled ? '' : ' once real trading is ON'}.</p> : (
            <table className="orbvs"><tbody>
              <tr><td>Value</td><td className="num"><b>{inr(A.value)}</b> <span className="muted small">cash {inr(A.cash)}</span></td></tr>
              <tr><td>Before costs · costs · after</td><td className="num">{sgn(A.gross)} · −{inr(A.costs)} · <b className={cls(A.net)}>{sgn(A.net)} ({pct(A.return)})</b></td></tr>
              <tr><td>Realised · unrealised</td><td className="num">{sgn(A.realised)} · {sgn(A.unrealised)}</td></tr>
              <tr><td>Won · profit factor · max DD</td><td className="num">{d.won} of {d.closed} · {d.profit_factor ?? '—'} · <span className="down">{pct(-d.drawdown)}</span></td></tr>
              <tr><td>NIFTY 50 same days</td><td className="num">{pct(A.nifty)}</td></tr>
            </tbody></table>)}
          {d.started && <p><button className="linkbtn" onClick={reset}>Reset tracking…</button></p>}
        </div>
      </div>

      {d.started && <>
        <div className="panel"><h3>Orders for the next open <span className="muted small">· decided {day(d.decided)} · sent {s.send_time}</span></h3>
          {!d.orders?.length ? <p className="muted">None.</p> : (
            <div className="tablewrap"><table className="grid"><thead><tr><th>Action</th><th>Stock</th><th className="num">Qty</th>
              <th className="num">Stop</th><th className="num">≈ Value</th><th className="num">₹ at risk</th><th>Why</th><th>Status</th></tr></thead>
              <tbody>{d.orders.map((o) => (
                <tr key={o.action + o.symbol}><td><b className={o.action === 'BUY' ? 'up' : 'down'}>{o.action}</b></td><td><b>{o.symbol}</b></td>
                  <td className="num">{o.qty}</td><td className="num">{o.stop ? rs(o.stop) : '—'}</td>
                  <td className="num">{o.close ? inr(o.qty * o.close) : '—'}</td><td className="num">{o.risk ? inr(o.risk) : '—'}</td>
                  <td className="muted">{o.why}</td>
                  <td><span className={`pill ${o.sent === 'sent' ? 'ok' : o.sent === 'failed' ? 'err' : 'pend'}`}>{o.sent || (live ? 'queued' : 'not sending')}</span></td></tr>))}
              </tbody></table></div>)}
          {d.waiting > 0 && <p className="muted small">{d.waiting} more breakout(s) without a free slot / over the daily limit.</p>}
        </div>

        <div className="orbcmp">
          <div className="panel"><h3>Open positions ({d.positions.length} of {d.allowed ? d.cap : d.max_pos})</h3>
            {!d.positions.length ? <p className="muted">None.</p> : (
              <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Bought</th><th className="num">Stop</th>
                <th className="num">Last</th><th className="num">After costs</th>{!phone && <th>Strategy</th>}</tr></thead>
                <tbody>{d.positions.map((x) => (
                  <tr key={x.symbol}><td><b>{x.symbol}</b> <span className="muted small">{x.qty} sh</span></td>
                    <td>{day(x.in)} · {rs(x.buy)}{x.est && <span className="muted small"> (est.)</span>}</td>
                    <td className="num">{rs(x.stop)}</td><td className="num">{rs(x.last)}</td>
                    <td className={`num ${cls(x.net)}`}><b>{sgn(x.net)}</b><div className="muted small">{sgn(x.gross)} − {inr(x.costs)}</div></td>
                    {!phone && <td className={`v${x.variant}`}>{x.variant}</td>}</tr>))}</tbody></table></div>)}
            <p className="muted small">No stop-loss orders: the app checks live prices every {s.stop_check_min} min and sends EXIT when a stop is reached
              {' '}(needs the daily Kite login). Prices are what the app saw when sending (est.), corrected to the day's open in the evening; your broker's fills may differ.</p></div>
          <div className="panel"><h3>Trade log</h3>
            {!d.trades?.length ? <p className="muted">None yet.</p> : (
              <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Bought · sold</th><th className="num">Before</th>
                <th className="num">Costs</th><th className="num">After</th><th>Exit</th></tr></thead>
                <tbody>{d.trades.map((t) => (
                  <tr key={t.symbol + t.in}><td><b>{t.symbol}</b> <span className="muted small">{t.qty}</span></td><td>{day(t.in)} · {day(t.out)}</td>
                    <td className={`num ${cls(t.gross)}`}>{sgn(t.gross)}</td><td className="num muted">{inr(t.costs)}</td>
                    <td className={`num ${cls(t.net)}`}><b>{sgn(t.net)}</b></td><td>{EXIT[t.why] || t.why}</td></tr>))}</tbody></table></div>)}</div>
        </div>
      </>}

      <div className="panel"><h3>Order log <span className="muted small">· every webhook to Tradetron, never deleted (the token is never shown)</span></h3>
        {!d.log?.length ? <p className="muted">Nothing sent yet.</p> : (
          <div className="tablewrap"><table className="grid"><thead><tr><th>Time</th><th>Order</th><th>Why</th><th>Tradetron reply</th><th>Status</th></tr></thead>
            <tbody>{d.log.map((o, k) => (
              <tr key={k}><td className="muted">{stamp(o.created_at)}</td><td><b className={o.action === 'BUY' ? 'up' : 'down'}>{o.action}</b> <b>{o.symbol}</b> {o.qty ?? ''}
                {o.price ? <span className="muted small"> ~{rs(o.price)}</span> : null}</td><td className="muted small">{o.reason}</td>
                <td className="muted small wrap">{o.reply}</td><td><span className={`pill ${STATUS[o.status] || ''}`}>{o.status}</span></td></tr>))}</tbody></table></div>)}
      </div>
      <p className="muted small">Orders go to Tradetron, which routes them to your broker (or keeps them in its offline mode). The app never places
        {' '}an order in Kite; Kite is used only for prices. Past results don't guarantee future returns.</p>
    </div>
  );
}
