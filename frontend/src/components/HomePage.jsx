import React from 'react';
import { useLive } from '../services/live';
import { useSchedule, left } from './StatusBar';
import { useAttention } from './AttentionStrip';
import { Kpi, Spark, Acct, StopMeter } from './ui';
import { inr, pct, cls, signed } from '../fmt';
import { useIsPhone } from '../useMedia';

const at12 = (s) => {
  if (!s) return '';
  const d = new Date(s.length > 10 ? s : `${s}T00:00:00`);
  return isNaN(d) ? s : d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true });
};

// 🏠 Home: today across every account, open positions, what needs a look, what runs next, the latest activity.
export default function HomePage({ onGo, onStock }) {
  const o = useLive('overview');
  const { data: sch, now } = useSchedule();
  const att = useAttention();
  const phone = useIsPhone();
  if (!o) return <div className="v2card muted">Loading…</div>;
  const A = o.accounts || {};
  const needs = att ? Object.entries(att).filter(([, a]) => a?.level && a.level !== 'running') : [];
  const jobs = (sch?.jobs || []).filter((j) => !j.skip).slice(0, 5);
  const it = A.intraday || {};
  return <>
    <div className="kpis">
      <Kpi label="Today · all accounts" value={signed(o.today)} cls={cls(o.today)}
        sub="intraday today · swing accounts: last session's change" />
      {A.real && <Kpi label={`Real · Tradetron ${A.real.on ? '· ON' : '· OFF'}`} value={inr(A.real.value)}
        sub={<><span className={cls(A.real.day_pnl)}>{signed(A.real.day_pnl)}</span> last session · {A.real.positions} positions · {pct(A.real.return)} since start</>} />}
      {A.paper_A && <Kpi label="Paper A · swing" value={pct(A.paper_A.return)} cls={cls(A.paper_A.return)}
        sub={<>{inr(A.paper_A.value)} · <span className={cls(A.paper_A.day_pnl)}>{signed(A.paper_A.day_pnl)}</span> last session · {A.paper_A.positions} open</>} />}
      {A.paper_E && <Kpi label="Paper E+ · swing" value={pct(A.paper_E.return)} cls={cls(A.paper_E.return)}
        sub={<>{inr(A.paper_E.value)} · <span className={cls(A.paper_E.day_pnl)}>{signed(A.paper_E.day_pnl)}</span> last session · {A.paper_E.positions} open</>} />}
      <Kpi label={`Intraday · ${it.mode === 'paper' ? 'paper' : it.mode || 'off'}`} value={signed(it.day_pnl)} cls={cls(it.day_pnl)}
        sub={`${it.open || 0} open · ${it.trades || 0} trades · risk ${inr(it.at_risk)} · loss limit ${inr(Math.max(0, -(it.realised || 0)))}/${inr(it.loss_limit)}`} />
    </div>
    <div className="v2grid">
      <div className="v2card"><h3>Open positions <span className="achip">{o.positions.length}</span>
        <span className="r">{o.feed === 'live' ? 'live' : 'as of the last price'} · <button className="linkbtn" onClick={() => onGo('portfolio')}>Portfolio →</button></span></h3>
        {!o.positions.length ? <p className="muted">No open positions.</p> : phone
          ? <div className="v2list">{o.positions.map((p) => <div key={p.book + p.symbol} className="it" onClick={() => onStock(p.symbol)}>
            <div><b>{p.symbol}</b><div className="muted small"><Acct book={p.book} name={p.account} /> {p.qty} @ {p.entry}</div></div>
            <div style={{ marginLeft: 'auto', textAlign: 'right' }}><b className={cls(p.pnl)}>{signed(p.pnl)}</b><div className="muted small">{p.now} · {pct(p.pct)}</div></div></div>)}</div>
          : <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Account</th><th>Side</th><th className="num">Qty</th>
            <th className="num">Entry</th><th className="num">Now</th><th className="num">P&amp;L</th><th className="num">%</th><th>Stop</th></tr></thead>
            <tbody>{o.positions.map((p) => <tr key={p.book + p.symbol} style={{ cursor: 'pointer' }} onClick={() => onStock(p.symbol)}>
              <td><b>{p.symbol}</b></td><td><Acct book={p.book} name={p.account} /></td><td className={p.side === 'short' ? 'down' : 'up'}>{p.side}</td>
              <td className="num">{p.qty}</td><td className="num">{p.entry}</td><td className="num">{p.now ?? '—'}</td>
              <td className={`num ${cls(p.pnl)}`}>{signed(p.pnl)}</td><td className={`num ${cls(p.pct)}`}>{pct(p.pct)}</td>
              <td>{p.now && p.stop ? <StopMeter last={p.side === 'short' ? -p.now : p.now} stop={p.side === 'short' ? -p.stop : p.stop} /> : '—'}</td></tr>)}</tbody></table></div>}
      </div>
      <div>
        <div className="v2card"><h3>Needs attention {needs.length > 0 && <span className="achip">{needs.length}</span>}</h3>
          {needs.length ? <div className="v2list">{needs.map(([tab, a]) => <div key={tab} className="it" style={{ cursor: 'pointer' }} onClick={() => onGo(tab)}>
            <span>{a.level === 'warn' ? '⚠️' : a.level === 'sell' ? '🔴' : a.level === 'buy' ? '🟢' : '🟡'}</span><span>{(a.items || []).map((x) => x.text).join(' · ') || tab}</span></div>)}</div>
            : <p className="muted small">Nothing right now.</p>}</div>
        <div className="v2card"><h3>Next up</h3><div className="v2list">{jobs.map((j) => <div key={j.key} className="it">
          <span>{j.icon}</span><span>{j.name}</span><span className="mono" style={{ marginLeft: 'auto' }}>{left(j.at, now)}</span></div>)}</div></div>
        <div className="v2card"><h3>Activity</h3>{!o.activity.length ? <p className="muted small">Nothing yet.</p>
          : <div className="v2list">{o.activity.map((a, i) => <div key={i} className="it"><span className="t">{at12(a.at)}</span>
            <span><b>{a.account}</b> · {a.text}</span></div>)}</div>}</div>
      </div>
    </div>
  </>;
}

export { Spark };
