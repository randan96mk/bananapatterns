import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp, ampm } from '../timefmt';
import { inr, rs, signed, pct, cls } from '../fmt';
import AttentionStrip from './AttentionStrip';
import NextUp from './NextUp';
import { StopMeter } from './ui';
import { Head, Chip, Seg, Switch, Tag, Panel, FixedTable, KV, Act } from './ui2';

const EXIT = { stop: '🛑 stop', trail: '🪜 late trail', exit: '📉 trend exit' };
const STATUS = { sent: 'ok', failed: 'bad', skipped: 'w' };
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

export default function RealPage() {
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
  const posCols = [{ h: 'Stock' }, { h: 'Strategy', w: 86, lo: true }, { h: 'Qty', w: 56, n: true, lo: true }, { h: 'Bought', w: 180, lo: true }, { h: 'Last', w: 90, n: true }, { h: 'After costs', w: 104, n: true }, { h: '%', w: 64, n: true, lo: true }, { h: 'Stop · distance', w: 230 }, { h: 'Days', w: 56, n: true, lo: true }];
  const posRows = (d.positions || []).map((x) => ({ key: x.symbol, title: `before costs ${signed(x.gross)} · costs ${inr(x.costs)}${x.best ? ` · best close ${rs(x.best)}` : ''}`,
    cells: [<><b>{x.symbol}</b>{x.last && x.stop && (x.last / x.stop - 1) * 100 < 2 && <Tag c="w">near stop</Tag>}</>, <span className={`v${x.variant}`}>{x.variant === 'E' ? 'E+' : x.variant}</span>, x.qty,
      <>{day(x.in)} · {rs(x.buy)}{x.est && <small> (est.)</small>}</>, rs(x.last), <b className={cls(x.net)}>{signed(x.net)}</b>, <span className={cls(x.gross)}>{pct(x.buy ? (x.last / x.buy - 1) * 100 : null)}</span>,
      <><StopMeter last={x.last} stop={x.stop} /> <span className="mono">{rs(x.stop)}</span><Trail x={x} /></>, x.days ?? '—'] }));
  const tradeCols = [{ h: 'Stock' }, { h: 'Bought · sold', w: 150, lo: true }, { h: 'Qty', w: 56, n: true, lo: true }, { h: 'Before', w: 100, n: true, lo: true }, { h: 'Costs', w: 80, n: true, lo: true }, { h: 'After', w: 104, n: true }, { h: 'Exit', w: 110 }];
  const tradeRows = (d.trades || []).map((t) => ({ key: t.symbol + t.in, cells: [<b>{t.symbol}</b>, `${day(t.in)} · ${day(t.out)}`, t.qty, <span className={cls(t.gross)}>{signed(t.gross)}</span>, <span className="muted">{inr(t.costs)}</span>, <b className={cls(t.net)}>{signed(t.net)}</b>, EXIT[t.why] || t.why] }));
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
        <div className="rbody"><Seg opts={[['A', `A · 200-day · top ${d.tops?.A ?? ''}`], ['E', `E+ · half, late trail · top ${d.tops?.E ?? ''}`]]} v={d.variant} set={(k) => set({ variant: k })} /></div>
        <div className="rnote">{d.variant === 'E' ? 'E+ · late trail: 25% under the highest close once +100%; only ever raised, from the next session.' : 'A · buys only when NIFTY is above its 200-day average; fixed stop, exit under the 50-day.'}
          {' '}Switching changes only NEW buys; open positions keep their own stop and exit.</div>
      </Panel>
    </div>

    {d.started ? <div className="two">
      <div className="rd" style={COL}>
        <Panel title="Orders for the next open" meta={(d.orders || []).length} right={<Meta>decided {day(d.decided)} · sent {ampm(s.send_time)} · {live ? <b className="up">sending</b> : <b className="down">not sending</b>}</Meta>}>
          <FixedTable cols={orderCols} rows={orderRows} empty="None." />
          {d.waiting > 0 && <div className="rnote">{d.waiting} more breakout(s) without a free slot / over the daily limit.</div>}
        </Panel>
        <Panel title="Open positions" meta={`${d.positions.length} of ${d.allowed ? d.cap : d.max_pos}`} right={<Meta>stop check every {s.stop_check_min} min · at risk <b className="mono down">{inr(risk)}</b></Meta>}>
          <FixedTable cols={posCols} rows={posRows} empty="None." />
          {d.checks?.length > 0 && <div className="rbanner warn" style={{ margin: '8px 14px' }}>⚠️ Position checks (warnings only, nothing is traded): {d.checks.join(' · ')}</div>}
          {d.notes?.length > 0 && <div className="rnote">{d.notes.map((n) => `📌 ${n.day}: ${n.text}`).join(' · ')}</div>}
          <div className="rnote">E+ positions: once the best close is +100% over the entry, the stop is raised to 25% under it after the close (never lowered) and watched from the next session like any stop; a stop sent with the BUY is not moved.
            {' '}No stop-loss orders: the app checks live prices every {s.stop_check_min} min and sends EXIT when a stop is reached (needs the daily Kite login). Prices are what the app saw when sending (est.), corrected to the day's open in the evening; your broker's fills may differ.</div>
        </Panel>
        <Panel id="tradelog" title="Trade log" meta={(d.trades || []).length}><FixedTable cols={tradeCols} rows={tradeRows} empty="None yet." /></Panel>
      </div>
      <div className="rd" style={COL}>
        <PaperVsReal />
        <Panel id="orderlog" title="Order log" meta={(d.log || []).length} right={<Meta>every webhook, never deleted · the token is never shown</Meta>}>
          {!d.log?.length ? <p className="muted small rempty">Nothing sent yet.</p> : d.log.slice(0, 60).map((o, k) => <Act key={k} icon={o.status === 'sent' ? '✓' : o.status === 'failed' ? '!' : '·'} cls={o.status === 'sent' ? 'up' : o.status === 'failed' ? 'down' : 'warn'}
            text={<><b className={o.action === 'BUY' ? 'up' : 'down'}>{o.action}</b> <b>{o.symbol}</b> {o.qty ?? ''}{o.price ? <span className="muted small"> ~{rs(o.price)}</span> : null} <Tag c={STATUS[o.status] || ''}>{o.status}</Tag></>}
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
            text={<><b className={o.action === 'BUY' ? 'up' : 'down'}>{o.action}</b> <b>{o.symbol}</b> {o.qty ?? ''}</>} meta={`${stamp(o.created_at)}${o.reply ? ` · ${o.reply}` : ''}`} />)}
        </Panel>
        <NextUp />
      </div>
    </div>}
    <div className="rnote">Orders go out by webhook to the broker's side (or stay in its offline mode). The app never places an order in Kite; Kite is used only for prices. Past results don't guarantee future returns.</div>
  </div>;
}
