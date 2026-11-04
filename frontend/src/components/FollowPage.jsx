import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useIsPhone } from '../useMedia';
import { inr, cls } from '../fmt';

// 📈 Follow: every fresh breakout on the Research screens bought blindly with ₹X: totals, by screen, every position.
const TAG = { bases: ['BASES', '#26a69a'], vcp: ['VCP', '#c58af9'], flag: ['FLAG', '#ff8a65'] };
const day = (d) => (d ? new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' }) : '—');
const n2 = (v) => (v == null ? '—' : (+v).toLocaleString('en-IN', { maximumFractionDigits: 2 }));
const pc = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${(+v).toFixed(1)}%`);
const Tags = ({ s }) => s.map((k) => <span key={k} className="chip small" style={{ color: TAG[k]?.[1], borderColor: TAG[k]?.[1], marginRight: 3 }}>{TAG[k]?.[0] || k}</span>);
const SCREEN_F = [['all', 'All'], ['vcp', 'VCP'], ['bases', 'Bases'], ['flag', 'Flag'], ['vcp+bases', 'VCP + Bases']];
const STATUS_F = [['all', 'All'], ['open', 'Open'], ['closed', 'Closed']];
const RESULT_F = [['all', 'All'], ['profit', 'Profit'], ['loss', 'Loss']];
const Seg = ({ opts, v, set }) => <span className="seg">{opts.map(([k, l]) => <button key={k} className={v === k ? 'active' : ''} onClick={() => set(k)}>{l}</button>)}</span>;
const live = (p) => p.status !== 'closed';

function csv(rows) {
  const cols = ['stock', 'screens', 'breakout', 'bought', 'entry', 'qty', 'value', 'pivot', 'stop', 'sold', 'exit_or_now', 'profit_loss', 'pct', 'days', 'status', 'why'];
  const lines = rows.map((p) => [p.symbol, p.screens.join('+'), p.breakout_date, p.entry_date || '', p.entry ?? '', p.qty, p.value, p.pivot, p.stop,
    p.exit_date || '', p.exit ?? p.last ?? '', p.net, p.pct ?? '', p.days, p.status, p.reason || ''].join(','));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([[cols.join(','), ...lines].join('\n')], { type: 'text/csv' }));
  a.download = `follow_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

export default function FollowPage() {
  const phone = useIsPhone();
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [scr, setScr] = useState('all');
  const [st, setSt] = useState('all');
  const [res, setRes] = useState('all');
  const [per, setPer] = useState('');
  const [max, setMax] = useState(100);
  const load = () => api.follow().then((x) => { setD(x); setErr(''); if (per === '') setPer(String(x.per_position)); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  useEffect(() => {                                           // while a rebuild runs: progress every 2 s
    if (d?.status?.state !== 'running') return undefined;
    const id = setInterval(load, 2000);
    return () => clearInterval(id);
  }, [d?.status?.state]);
  if (!d) return <div className="panel muted">{err || 'Loading…'}</div>;
  const t = d.total;
  const rows = d.rows.filter((p) => p.qty > 0 || live(p))
    .filter((p) => scr === 'all' || (scr === 'vcp+bases' ? p.screens.includes('vcp') && p.screens.includes('bases') : p.screens.includes(scr)))
    .filter((p) => st === 'all' || (st === 'open' ? live(p) : !live(p)))
    .filter((p) => res === 'all' || (res === 'profit' ? p.net > 0 : p.net <= 0));
  const sum = (f) => rows.reduce((a, p) => a + (f(p) || 0), 0);
  const nOpen = rows.filter(live).length, nClosed = rows.length - nOpen, nWon = rows.filter((p) => !live(p) && p.net > 0).length;
  const value = sum((p) => p.value), net = sum((p) => p.net);
  const running = d.status.state === 'running';
  const act = (p) => p.then(load).catch((e) => setErr(e.message));
  const savePer = () => act(api.followSettings(+per));
  return <>
    <div className="panel">
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <b>📈 Every fresh breakout bought with {inr(d.per_position)}, nothing skipped</b>
        <span className="chip small">Buy: next day's open after the breakout day</span>
        <span className="chip small">Sell: run ends (close under the {d.rows[0]?.ma || 50}-day line or {d.rows[0] ? Math.round((1 - d.rows[0].stop / d.rows[0].pivot) * 100) : 8}% under the pivot) → next open</span>
        <span className="chip small">Delivery charges included</span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }} className="small">₹ a position
          <input className="input" style={{ width: 100, textAlign: 'right' }} inputMode="numeric" value={per} onChange={(e) => setPer(e.target.value.replace(/[^0-9]/g, ''))} />
          <button className="btn" onClick={savePer} disabled={+per < 1000}>Save</button>
          <button className="btn primary" disabled={running} onClick={() => window.confirm(`Rebuild the whole book from the saved daily candles (last ${d.months} months)? Takes a few minutes.`) && act(api.followRebuild())}>{running ? 'Rebuilding…' : '↻ Rebuild'}</button></span>
      </div>
      <div className="muted small">One stock is one position at a time, tagged with every screen that flagged it · judged on each day's data only · updated every evening after the scan ·
        {d.as_of ? ` as of ${day(d.as_of)}` : ' not built yet: press Rebuild'} · a new ₹ a position applies to new entries (Rebuild to apply it to the history) · nothing is ordered.</div>
      {(running || d.status.message) && <div className="small" style={{ marginTop: 6 }}>{d.status.message}{running && <div className="bar"><i style={{ width: `${d.status.pct}%` }} /></div>}</div>}
      {err && <div className="warn small">{err}</div>}
      <div className="summary" style={{ marginTop: 10 }}>
        <div className="k"><div className={`v ${cls(t.net)}`}>{inr(t.net)}</div><div className="l">net profit / loss · {t.breakouts} breakouts</div></div>
        <div className="k"><div className={`v ${cls(t.open_net)}`}>{inr(t.open_net)}</div><div className="l">open P&L · {t.open} open now</div></div>
        <div className="k"><div className="v">{t.closed} · {t.win_rate ?? '—'}%</div><div className="l">closed · won</div></div>
        <div className="k"><div className="v">{pc(t.avg_win)} / {pc(t.avg_loss)}</div><div className="l">avg win / avg loss</div></div>
        <div className="k"><div className="v">{inr(t.peak.value)}</div><div className="l">peak money in use · {t.peak.positions} positions{t.peak.date ? ` on ${day(t.peak.date)}` : ''}</div></div>
        <div className="k"><div className="v down">{inr(-t.drawdown)}</div><div className="l">worst drawdown (closed net)</div></div>
      </div>
    </div>
    <div className="panel"><b>By screen</b> <span className="muted small">· a breakout counts under every screen that flagged it</span>
      <div className="tablewrap"><table className="grid"><thead><tr><th>Screen</th><th className="num">Breakouts</th><th className="num">Open</th><th className="num">Closed · won</th><th className="num">Avg win / loss</th><th className="num">Profit / loss</th></tr></thead>
        <tbody>{Object.entries(d.by_screen).map(([k, a]) => <tr key={k}><td>{TAG[k] ? <Tags s={[k]} /> : null}{d.names[k]}</td><td className="num">{a.breakouts}</td><td className="num">{a.open}</td>
          <td className="num">{a.closed} · {a.win_rate ?? '—'}%</td><td className="num">{pc(a.avg_win)} / {pc(a.avg_loss)}</td><td className={`num ${cls(a.net)}`}>{inr(a.net)}</td></tr>)}</tbody></table></div></div>
    <div className="panel">
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 8 }}><b>Breakouts</b> <span className="muted small">· newest first</span>
        <span className="muted small">Screen</span><Seg opts={SCREEN_F} v={scr} set={setScr} /><span className="muted small">Status</span><Seg opts={STATUS_F} v={st} set={setSt} />
        <span className="muted small">Result</span><Seg opts={RESULT_F} v={res} set={setRes} />
        <button className="btn" style={{ marginLeft: 'auto' }} onClick={() => csv(rows)} disabled={!rows.length}>⬇ CSV</button></div>
      {!rows.length ? <p className="muted">No breakouts match{d.as_of ? '' : ' (not built yet: press Rebuild, or wait for the evening scan)'}.</p>
        : phone ? <div className="pcards">{rows.slice(0, max).map((p) => <details key={p.symbol + p.breakout_date} className="pcard"><summary className="t"><span><b>{p.symbol}</b> <Tags s={p.screens} /> <span className="muted small">{day(p.breakout_date)}</span></span><b className={cls(p.net)}>{inr(p.net)}</b></summary>
          <div className="small">bought {day(p.entry_date)} @ {n2(p.entry)} × {p.qty} = {inr(p.value)} · pivot {n2(p.pivot)} · stop {n2(p.stop)}<br />{live(p) ? `now ${n2(p.last)} · open ${p.days} d` : `sold ${day(p.exit_date)} @ ${n2(p.exit)} · ${p.days} d`} · {pc(p.pct)}{p.reason ? ` · ${p.reason}` : ''}</div></details>)}</div>
          : <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Screens</th><th>Breakout</th><th>Bought</th><th className="num">Entry ₹</th><th className="num">Qty</th><th className="num">Value ₹</th>
            <th className="num">Pivot</th><th className="num">Stop</th><th>Sold</th><th className="num">Exit / Now ₹</th><th className="num">Profit / loss</th><th className="num">%</th><th className="num">Held</th><th>Status · why</th></tr></thead>
            <tbody>{rows.slice(0, max).map((p) => <tr key={p.symbol + p.breakout_date}><td><b>{p.symbol}</b></td><td><Tags s={p.screens} /></td><td>{day(p.breakout_date)}</td><td>{day(p.entry_date)}</td>
              <td className="num">{n2(p.entry)}</td><td className="num">{p.qty || '—'}</td><td className="num">{inr(p.value)}</td><td className="num">{n2(p.pivot)}</td><td className="num">{n2(p.stop)}</td>
              <td>{day(p.exit_date)}</td><td className="num">{n2(p.exit ?? p.last)}</td><td className={`num ${cls(p.net)}`}><b>{inr(p.net)}</b></td><td className={`num ${cls(p.pct)}`}>{pc(p.pct)}</td>
              <td className="num">{p.days} d</td><td>{live(p) ? <span className="up">● {p.status === 'pending' ? 'buys at the next open' : p.status === 'closing' ? `sells at the next open · ${p.reason}` : 'open'}</span> : `closed · ${p.reason || ''}`}</td></tr>)}</tbody>
            <tfoot><tr style={{ fontWeight: 700 }}><td colSpan={6}>Total · {rows.length} shown</td><td className="num">{inr(value)}</td><td colSpan={4} /><td className={`num ${cls(net)}`}>{inr(net)}</td>
              <td className={`num ${cls(net)}`}>{value ? pc(net / value * 100) : '—'}</td><td className="num">{rows.length ? Math.round(sum((p) => p.days) / rows.length) : 0} d avg</td><td>{nOpen} open · {nClosed} closed · {nWon} won</td></tr></tfoot></table></div>}
      {rows.length > max && <button className="linkbtn small" onClick={() => setMax(rows.length)}>show all {rows.length}</button>}
      <div className="muted small" style={{ marginTop: 6 }}>The total line follows the filters: value = the sum of every shown position's buy value, % = profit / loss over it. Open rows are marked at the last close, after the charges a sale would take.</div>
    </div>
  </>;
}
