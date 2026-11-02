import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { push, useLive } from '../services/live';
import { useIsPhone } from '../useMedia';
import { StopMeter, SubTabs } from './ui';
import { inr, signed, cls } from '../fmt';
import FlowBacktest from './FlowBacktest';
import JourneyPanel, { NodeStocks } from './JourneyPanel';

const W = 210, H = 112;                                   // box size on the canvas
const STATUS = { ok: ['✓', 'ok'], running: ['…', 'run'], idle: ['idle', 'idle'], error: ['!', 'err'] };
const MODES = [['off', 'Off'], ['signals', 'Signals'], ['paper', '📝 Paper']];
const ist = (iso) => (iso ? new Date(iso).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit', hour12: true }) : '');

// Seconds to the next candle close of `interval` minutes (aligned to 9:15 IST).
function nextClose(interval) {
  const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  const m = now.getHours() * 60 + now.getMinutes() - (9 * 60 + 15);
  const left = (interval - (((m % interval) + interval) % interval)) * 60 - now.getSeconds();
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
}

function Field({ f, value, onChange }) {
  if (f.type === 'bool') return <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />;
  if (f.type === 'select') {
    return <select value={value} onChange={(e) => onChange(isNaN(+e.target.value) ? e.target.value : +e.target.value)}>
      {f.options.map((o) => <option key={o} value={o}>{o}</option>)}</select>;
  }
  return <input className="input flownum" type="number" min={f.min} max={f.max} step={f.step || 1} value={value}
                onChange={(e) => onChange(+e.target.value)} />;
}

const pctx = (v) => `${v > 0 ? '+' : ''}${v.toFixed(2)}%`;
const STATUS_TXT = { open: ['open', 'up'], pending: ['filling at the next minute', 'warn'], closed: ['closed', 'muted'], cancelled: ['cancelled', 'muted'], signal: ['signal', 'muted'] };
function Status({ r }) {
  const [t, c] = STATUS_TXT[r.status] || ['—', 'muted'];
  return <span className={`small ${c}`}>{t}{r.status === 'closed' && r.exit_t ? ` ${r.exit_t}` : ''}</span>;
}
function SignalCard({ s, r }) {
  return <div className="pcard"><div className="t"><span><b>{s.symbol}</b> <Status r={r} /></span>
    <b className={cls(r.pnl ?? r.move_pct)}>{r.pnl != null ? `${signed(r.pnl)} · ${pctx(r.pct || 0)}` : r.move_pct != null ? pctx(r.move_pct) : '—'}</b></div>
    <div className="muted small">{s.signal} {s.qty ? `${s.qty} @ ` : 'at '}{(r.entry ?? s.entry)?.toFixed(2)} · now <b>{r.now ? r.now.toFixed(2) : '—'}</b>{r.r != null ? ` · ${r.r}R` : ''} · {ist(s.at)}</div>
    {r.status === 'open' && r.now && <StopMeter last={s.side === 'short' ? -r.now : r.now} stop={s.side === 'short' ? -r.stop : r.stop} wide />}
    <div className="muted small">stop {(r.stop ?? s.sl)?.toFixed(2)}{r.trailed ? ' (trailed)' : ''} · {s.target ? `target ${s.target.toFixed(2)}` : 'no target'}</div></div>;
}

// ⚡ Intraday Flow: the template's boxes on a canvas, live (in → out, status, run time), settings per box.
// Signals only in phase 1: nothing is ever ordered from here.
export default function FlowPage() {
  const phone = useIsPhone();
  const d = useLive('flow');
  const [journey, setJourney] = useState(false);          // 🧭 the side panel, and a stock to check in it
  const [jsym, setJsym] = useState(null);
  const [rr, setRr] = useState(null);                    // the latest ↻ re-run (dry run) of a box
  const rerun = (nid) => api.flowRerun(nid).then((x) => { setRr(x.error ? { error: x.error, from: nid } : x); load(); })
    .catch((e) => setRr({ error: e.message, from: nid }));                              // 📡 the flow's state from the live connection
  const [sel, setSel] = useState('filters');
  const [err, setErr] = useState('');
  const [, tick] = useState(0);
  const [sub, setSub] = useState('flow');
  const lv = useLive('flow_live');                       // live prices and P&L of today's signals (every 3 s when live)
  const feedLive = d?.feed?.state === 'live';
  const [jr, setJr] = useState(null);
  const [full, setFull] = useState(false);
  const [drag, setDrag] = useState(null);                 // {id, dx, dy, x, y} while a box is dragged
  const [moved, setMoved] = useState({});                 // positions dropped here, until the next refresh
  useEffect(() => {
    const esc = (e) => { if (e.key === 'Escape') setFull(false); };
    window.addEventListener('keydown', esc);
    return () => window.removeEventListener('keydown', esc);
  }, []);
  useEffect(() => { if (sub === 'journal') api.flowJournal(30).then(setJr).catch((e) => setErr(e.message)); }, [sub]);
  // after an action: fetch now and share it (the live connection keeps it current otherwise)
  const load = () => Promise.all([api.getFlow().then((x) => { push('flow', x); setErr(''); }),
    api.flowLive().then((x) => push('flow_live', x))]).catch((e) => setErr(e.message));
  useEffect(() => {
    load();
    const b = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(b);
  }, []);
  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;

  const nodes = d.nodes.map((n) => {
    const p = drag?.id === n.id ? [drag.x, drag.y] : moved[n.id];
    return p ? { ...n, x: p[0], y: p[1] } : n;
  });
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const down = (e, n) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    setDrag({ id: n.id, dx: e.clientX - n.x, dy: e.clientY - n.y, x: n.x, y: n.y, moved: false });
  };
  const move = (e) => {
    if (!drag) return;
    const x = Math.max(0, Math.round((e.clientX - drag.dx) / 5) * 5), y = Math.max(0, Math.round((e.clientY - drag.dy) / 5) * 5);
    if (x !== drag.x || y !== drag.y) setDrag({ ...drag, x, y, moved: true });
  };
  const up = () => {
    if (!drag) return;
    if (drag.moved) {
      setMoved({ ...moved, [drag.id]: [drag.x, drag.y] });
      api.flowLayout({ [drag.id]: [drag.x, drag.y] }).catch((e) => setErr(e.message));
    } else setSel(drag.id);
    setDrag(null);
  };
  const resetLayout = () => api.flowLayout({}).then(() => { setMoved({}); load(); }).catch((e) => setErr(e.message));
  const node = byId[sel] || d.nodes[0];
  const setMode = (mode) => api.flowMode(mode).then(load).catch((e) => setErr(e.message));
  const change = (key, v) => api.flowNode(node.id, { [key]: v }).then(load).catch((e) => setErr(e.message));
  const toggleStop = () => {
    if (!d.stopped && !window.confirm(`Stop all intraday activity?${d.stats?.open ? `\n${d.stats.open} open paper position(s) will be squared off at the last price.` : ''}\nNo live data, signals or trades until you press Start.`)) return;
    (d.stopped ? api.flowStart() : api.flowStop()).then(load).catch((e) => setErr(e.message));
  };
  const runNow = () => api.flowRun().then(load).catch((e) => setErr(e.message));
  const maxX = Math.max(...nodes.map((n) => n.x)) + W + 30, maxY = Math.max(...nodes.map((n) => n.y)) + H + 30;
  const f = d.feed || {};
  const stats = lv?.stats || d.stats;

  return (
    <div className="flowpage">
      <div className="flowtop">
        <b className="flowtitle">⚡ Intraday Flow</b>
        <button className={`btn killbtn ${d.stopped ? 'start' : 'stop'}`} onClick={toggleStop}>
          {d.stopped ? '▶ Start intraday' : '⏹ Stop intraday'}</button>
        <span className="sb">Template: <b>{d.template}</b></span>
        <span className="seg">{MODES.map(([id, label]) => (
          <button key={id} className={d.mode === id ? 'active' : ''} onClick={() => setMode(id)}>{label}</button>))}</span>
        <span className="sb"><i className={`dot ${f.state === 'live' ? 'ok' : f.state === 'off' ? '' : 'warn'}`} />
          {f.state === 'live' ? `Live ticks · ${f.tokens} stocks` : f.state === 'off' ? 'Feed off (starts 9:10 AM in Signals or Paper mode)' : `Feed ${f.state}`}</span>
        <span className="sb">Next {d.interval}m close in <b className="mono">{nextClose(d.interval)}</b></span>
        {d.last_run && <span className="sb">Last run {ist(d.last_run.at)} · {d.last_run.ms} ms</span>}
        <button className="btn" onClick={runNow} disabled={d.running}>{d.running ? 'Running…' : '▶ Run now'}</button>
      </div>
      {d.stopped && <div className="stopbanner">⏹ <b>INTRADAY STOPPED</b> · no live data, no flow runs, no signals, no paper
        trades. Stays stopped (also after a restart or the next day) until you press ▶ Start intraday.</div>}
      {err && <div className="warn small">{err}</div>}
      {stats && <div className="summary" style={{ marginBottom: 12 }}>
        <div className="k"><div className={`v ${cls(stats.realised + stats.unrealised)}`}>{signed(stats.realised + stats.unrealised)}</div>
          <div className="l">today after costs · realised {signed(stats.realised)} · open {signed(stats.unrealised)}</div></div>
        <div className="k"><div className="v">{stats.trades}</div><div className="l">paper trades · {stats.won} won · {stats.open} open</div></div>
        <div className="k"><div className="v down">{inr(stats.at_risk)}</div><div className="l">at risk now (to the stops)</div></div>
        <div className="k"><div className="v">{inr(Math.max(0, -stats.realised))} / {inr(stats.loss_limit)}</div>
          <div className="l">daily loss used · capital {inr(stats.capital)}</div></div>
      </div>}
      <SubTabs tabs={[['flow', 'Flow'], ['positions', `Positions (${d.positions?.length || 0})`], ['journal', 'Journal'], ['backtest', '🧪 Backtest']]} value={sub} onChange={setSub} />
      {sub === 'positions' && <div className="panel"><h3>📝 Paper positions today <span className="muted small">· simulated, no orders</span></h3>
        {!d.positions?.length ? <p className="muted">None today{d.mode !== 'paper' ? ' (switch the mode to 📝 Paper)' : ''}.</p> : (
          <div className="tablewrap"><table className="grid"><thead><tr><th>Stock</th><th>Side</th><th className="num">Qty</th><th className="num">Entry</th>
            <th className="num">Stop</th><th className="num">Target</th><th>Distance to stop</th><th>Status</th><th className="num">After costs</th></tr></thead>
            <tbody>{d.positions.map((p) => {
              const lg = p.side === 'long', last = p.last_price || p.entry;
              const unreal = p.status === 'open' && p.entry ? (lg ? last - p.entry : p.entry - last) * p.open_qty : null;
              return (<tr key={p.id}><td><b>{p.symbol}</b> <span className="muted small">{p.entry_t || p.signal_t}</span></td>
                <td className={lg ? 'up' : 'down'}>{lg ? '▲ long' : '▼ short'}</td><td className="num">{p.open_qty ?? p.qty}</td>
                <td className="num">{p.entry ? p.entry.toFixed(2) : 'pending'}</td><td className="num">{(p.stop ?? p.stop0)?.toFixed(2)}</td>
                <td className="num">{p.target ? p.target.toFixed(2) : '—'}</td>
                <td>{p.status === 'open' && <StopMeter last={lg ? last : (p.stop ?? p.stop0)} stop={lg ? (p.stop ?? p.stop0) : last} />}</td>
                <td>{p.status === 'closed' ? p.reason : p.status}</td>
                <td className={`num ${cls(p.net ?? unreal)}`}><b>{signed(p.net ?? unreal)}</b>
                  {p.costs != null && <div className="muted small">{signed(p.gross)} − {inr(p.costs)}</div>}</td></tr>);
            })}</tbody></table></div>)}</div>}
      {journey && <><div className="drawer-back" onClick={() => setJourney(false)} />
        <JourneyPanel symbol={jsym} onSymbol={setJsym} onClose={() => setJourney(false)} /></>}
      {sub === 'backtest' && <FlowBacktest />}
      {sub === 'journal' && <div className="panel"><h3>📒 Journal <span className="muted small">· closed paper trades, last 30 days, after costs</span></h3>
        {!jr ? 'Loading…' : !jr.total.trades ? <p className="muted">No closed paper trades yet.</p> : <>
          <div className="summary">
            <div className="k"><div className={`v ${cls(jr.total.net)}`}>{signed(jr.total.net)}</div><div className="l">net · {signed(jr.total.gross)} before − {inr(jr.total.costs)} costs</div></div>
            <div className="k"><div className="v">{jr.total.win_rate}%</div><div className="l">won · {jr.total.trades} trades</div></div>
            <div className="k"><div className="v">{jr.total.avg_r ?? '—'}R</div><div className="l">average R</div></div>
            <div className="k"><div className="v">{jr.total.profit_factor ?? '—'}</div><div className="l">profit factor</div></div></div>
          {['side', 'time'].map((g) => (
            <table key={g} className="orbvs" style={{ marginTop: 12 }}><thead><tr><th>{g === 'side' ? 'Side' : 'Entry time'}</th><th className="num">Trades</th>
              <th className="num">Won</th><th className="num">Net</th><th className="num">Avg R</th></tr></thead>
              <tbody>{Object.entries(jr.by[g]).map(([k, v]) => <tr key={k}><td>{k}</td><td className="num">{v.trades}</td><td className="num">{v.win_rate}%</td>
                <td className={`num ${cls(v.net)}`}>{signed(v.net)}</td><td className="num">{v.avg_r}</td></tr>)}</tbody></table>))}
        </>}</div>}
      {sub === 'flow' && <><div className="flowbody">
        <div className={`flowcanvas ${full ? 'full' : ''}`} onPointerMove={move} onPointerUp={up} onPointerCancel={up}>
          <div className="flowtools">
            <button className="btn" onClick={() => setFull(!full)} title={full ? 'Exit full screen (Esc)' : 'Full screen'}>{full ? '✕ Exit full screen' : '⛶ Full screen'}</button>
            <button className="btn" onClick={() => { setJsym(null); setJourney(true); }} title="Why a trade or not: every step, the rules, a stock's check">🧭 Journey</button>
            <button className="btn" onClick={resetLayout} title="Put every box back in its place">↺ Reset layout</button>
            <button className="btn" title="Every box's settings back to the recommended defaults"
              onClick={() => window.confirm('Put every box back to its default settings? Applies from the next candle. Save the canvas as a setup first (🧪 Backtest) if you want to keep the current one.')
                && api.flowResetNodes().then(load).catch((e) => setErr(e.message))}>↺ Default settings</button>
            <span className="muted small">drag a box to move it · click to configure</span></div>
          <div className="flowinner" style={{ width: maxX, height: maxY }}>
          <svg className="flowedges" width={maxX} height={maxY}>
            {nodes.flatMap((n) => n.inputs.map((i) => {
              const a = byId[i]; const x1 = a.x + W, y1 = a.y + H / 2, x2 = n.x, y2 = n.y + H / 2;
              const dx = Math.max(40, Math.abs(x2 - x1) / 2);
              const path = x2 > x1 ? `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
                : `M${x1},${y1} C${x1 + 60},${y1} ${x1 + 60},${(y1 + y2) / 2} ${(x1 + x2) / 2},${(y1 + y2) / 2} S${x2 - 60},${y2} ${x2},${y2}`;
              return <path key={i + n.id} d={path} className={`edge ${a.status === 'ok' || a.status === 'running' ? 'live' : ''}`} />;
            }))}
          </svg>
          {nodes.map((n) => {
            const [label, cls] = STATUS[n.status] || STATUS.idle;
            return (
              <button key={n.id} className={`fnode ${n.config?.skip ? 'skipped' : ''} ${sel === n.id ? 'sel' : ''} ${drag?.id === n.id ? 'dragging' : ''}`}
                      style={{ left: n.x, top: n.y, width: W }} onPointerDown={(e) => down(e, n)}
                      onKeyDown={(e) => { if (e.key === 'Enter') setSel(n.id); }}>
                <div className="fh"><span className="fico">{n.icon}</span><b>{n.title}</b><span className={`fst ${cls}`}>{label}</span></div>
                {n.in != null && <div className="fio"><b>{n.in}</b> → <b className="up">{n.out}</b> <span className="muted small">{n.ms} ms</span></div>}
                <div className="fsum">{n.summary}</div>
                {n.inputs.length > 0 && <span className="port in" />}<span className="port out" />
              </button>);
          })}
        </div>
          {full && <div className="panel flowpanel floating">
            <h3>{node.icon} {node.title}</h3><div className="muted small">{node.summary}</div>
            {node.in != null && <div className="small" style={{ marginTop: 8 }}>{node.in} in · <b>{node.out} passed</b> · {node.ms} ms
              {Object.keys(node.drops || {}).length > 0 && <div className="muted">Dropped: {Object.entries(node.drops).map(([k, v]) => `${k} ${v}`).join(' · ')}</div>}</div>}
            <div className="muted small" style={{ marginTop: 8 }}>Exit full screen to change settings.</div></div>}
        </div>
        <div className="panel flowpanel">
          <h3>{node.icon} {node.title}</h3>
          <div className="muted small">{node.summary}</div>
          {node.schema.length > 0 && <><h4>Settings</h4>
            {node.schema.map((s) => (
              <label key={s.key} className="flowfield"><span>{s.label}</span><Field f={s} value={node.config[s.key]} onChange={(v) => change(s.key, v)} /></label>))}
            <div className="muted small">Applies from the next candle.</div></>}
          {node.kind !== 'monitor' && <div style={{ margin: '10px 0' }}>
            <button className="btn" onClick={() => rerun(node.id)} title="Same candle's data, the settings above; no alert, no trade">↻ Re-run from this box</button>
            {rr?.from === node.id && <div className={`small ${rr.error ? 'warn' : 'muted'}`} style={{ marginTop: 6 }}>{rr.error ? rr.error
              : <>Dry run on the latest candle with these settings: {rr.boxes.length} box(es) re-run · <b>{rr.would_signal}</b> would signal (no alert sent, no trade opened).</>}</div>}</div>}
          {node.in != null && <><h4>Last run {node.dry && <span className="achip">dry run</span>}</h4>
            <div className="small">{node.in} in · <b>{node.out} passed</b> · {node.ms} ms</div>
            {Object.keys(node.drops || {}).length > 0 && <div className="small muted" style={{ marginTop: 4 }}>Dropped: {Object.entries(node.drops).map(([k, v]) => `${k} ${v}`).join(' · ')}</div>}
            {node.sample?.length > 0 && <div className="flowchips">{node.sample.map((s) => <span key={s} className="spill">{s}</span>)}
              {node.out > node.sample.length && <span className="muted small">+{node.out - node.sample.length}</span>}</div>}</>}
          {node.kind !== 'monitor' && <NodeStocks nid={node.id} stamp={`${d.last_run?.at}|${rr?.from || ''}|${node.in}|${node.out}`} onSymbol={(x) => { setJsym(x); setJourney(true); }} />}
        </div>
      </div></>}
      {sub === 'flow' && <div className="panel"><h3>🟢 Today's signals <span className="muted small">· {d.mode === 'paper' ? 'paper trades are opened from these (simulated)' : 'signals only, no orders'}</span></h3>
        {lv && <span className="muted small">{feedLive ? <><i className="dot ok" /> live · prices {ist(lv.at)}</> : `prices as of ${ist(lv.at)} (feed ${lv.feed})`}</span>}
        {!d.signals.length ? <p className="muted">None yet today.</p> : phone ? (
          <div className="pcards">{d.signals.map((s) => <SignalCard key={s.symbol} s={s} r={lv?.rows?.[s.symbol] || {}} />)}</div>) : (
          <div className="tablewrap"><table className="grid"><thead><tr><th>Time</th><th>Stock</th><th>Signal</th><th>Status</th><th className="num">Entry</th>
            <th className="num">Now</th><th className="num">P&amp;L after costs</th><th className="num">%</th><th className="num">R</th><th>Stop ← price → target</th>
            <th className="num">SL</th><th className="num">Target</th><th className="num">RVOL</th></tr></thead>
            <tbody>{d.signals.map((s) => { const r = lv?.rows?.[s.symbol] || {}; return (
              <tr key={s.symbol}><td className="muted">{ist(s.at)}</td><td><b>{s.symbol}</b>{s.qty ? <span className="muted small"> {s.qty} sh</span> : null}</td>
                <td className={s.side === 'short' ? 'down' : 'up'}>{s.signal}</td><td><Status r={r} /></td>
                <td className="num">{(r.entry ?? s.entry)?.toFixed(2)}</td>
                <td className="num">{r.status === 'closed' ? <span className="muted">{r.exit} (exit)</span> : r.now ? <b>{r.now.toFixed(2)}</b> : '—'}</td>
                <td className={`num ${cls(r.pnl)}`}>{r.pnl != null ? <b>{signed(r.pnl)}</b> : '—'}</td>
                <td className={`num ${cls(r.pct ?? r.move_pct)}`}>{r.pct != null ? pctx(r.pct) : r.move_pct != null ? <span title="move since the signal">{pctx(r.move_pct)}</span> : '—'}</td>
                <td className={`num ${cls(r.r)}`}>{r.r ?? '—'}</td>
                <td>{r.status === 'open' && r.now ? <StopMeter last={s.side === 'short' ? -r.now : r.now} stop={s.side === 'short' ? -r.stop : r.stop} />
                  : <span className="muted small">{r.status === 'closed' ? `out ${r.exit_t} · ${r.reason}` : r.status === 'pending' ? 'waiting for the fill' : ''}</span>}</td>
                <td className="num">{(r.stop ?? s.sl)?.toFixed(2)}{r.trailed && <span className="muted small"> ↑ trailed</span>}</td>
                <td className="num">{s.target ? s.target.toFixed(2) : '—'}</td><td className="num">{s.rvol ?? '—'}×</td></tr>); })}</tbody></table></div>)}
      </div>}
    </div>);
}
