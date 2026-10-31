import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useIsPhone } from '../useMedia';
import { StopMeter, SubTabs } from './ui';
import { inr, signed, cls } from '../fmt';

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

// ⚡ Intraday Flow: the template's boxes on a canvas, live (in → out, status, run time), settings per box.
// Signals only in phase 1: nothing is ever ordered from here.
export default function FlowPage() {
  const phone = useIsPhone();
  const [d, setD] = useState(null);
  const [sel, setSel] = useState('filters');
  const [err, setErr] = useState('');
  const [, tick] = useState(0);
  const [sub, setSub] = useState('flow');
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
  const load = () => api.getFlow().then((x) => { setD(x); setErr(''); }).catch((e) => setErr(e.message));
  useEffect(() => {
    load();
    const a = setInterval(load, 5000), b = setInterval(() => tick((x) => x + 1), 1000);
    return () => { clearInterval(a); clearInterval(b); };
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
      {d.stats && <div className="summary" style={{ marginBottom: 12 }}>
        <div className="k"><div className={`v ${cls(d.stats.realised + d.stats.unrealised)}`}>{signed(d.stats.realised + d.stats.unrealised)}</div>
          <div className="l">today after costs · realised {signed(d.stats.realised)} · open {signed(d.stats.unrealised)}</div></div>
        <div className="k"><div className="v">{d.stats.trades}</div><div className="l">paper trades · {d.stats.won} won · {d.stats.open} open</div></div>
        <div className="k"><div className="v down">{inr(d.stats.at_risk)}</div><div className="l">at risk now (to the stops)</div></div>
        <div className="k"><div className="v">{inr(Math.max(0, -d.stats.realised))} / {inr(d.stats.loss_limit)}</div>
          <div className="l">daily loss used · capital {inr(d.stats.capital)}</div></div>
      </div>}
      <SubTabs tabs={[['flow', 'Flow'], ['positions', `Positions (${d.positions?.length || 0})`], ['journal', 'Journal']]} value={sub} onChange={setSub} />
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
            <button className="btn" onClick={resetLayout} title="Put every box back in its place">↺ Reset layout</button>
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
              <button key={n.id} className={`fnode ${sel === n.id ? 'sel' : ''} ${drag?.id === n.id ? 'dragging' : ''}`}
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
          {node.in != null && <><h4>Last run</h4>
            <div className="small">{node.in} in · <b>{node.out} passed</b> · {node.ms} ms</div>
            {Object.keys(node.drops || {}).length > 0 && <div className="small muted" style={{ marginTop: 4 }}>Dropped: {Object.entries(node.drops).map(([k, v]) => `${k} ${v}`).join(' · ')}</div>}
            {node.sample?.length > 0 && <div className="flowchips">{node.sample.map((s) => <span key={s} className="spill">{s}</span>)}
              {node.out > node.sample.length && <span className="muted small">+{node.out - node.sample.length}</span>}</div>}</>}
        </div>
      </div></>}
      {sub === 'flow' && <div className="panel"><h3>🟢 Today's signals <span className="muted small">· {d.mode === 'paper' ? 'paper trades are opened from these (simulated)' : 'signals only, no orders'}</span></h3>
        {!d.signals.length ? <p className="muted">None yet today.</p> : (
          <div className="tablewrap"><table className="grid"><thead><tr><th>Time</th><th>Stock</th><th>Signal</th><th className="num">Entry</th>
            <th className="num">SL</th><th className="num">Target</th>{!phone && <><th className="num">RVOL</th><th className="num">RSI</th></>}</tr></thead>
            <tbody>{d.signals.map((s) => (
              <tr key={s.symbol}><td className="muted">{ist(s.at)}</td><td><b>{s.symbol}</b>{s.qty ? <span className="muted small"> {s.qty} sh</span> : null}</td>
                <td className={s.side === 'short' ? 'down' : 'up'}>{s.signal}</td>
                <td className="num">{s.entry?.toFixed(2)}</td><td className="num">{s.sl?.toFixed(2)}</td><td className="num">{s.target?.toFixed(2)}</td>
                {!phone && <><td className="num">{s.rvol ?? '—'}×</td><td className="num">{s.rsi ?? '—'}</td></>}</tr>))}</tbody></table></div>)}
      </div>}
    </div>);
}
