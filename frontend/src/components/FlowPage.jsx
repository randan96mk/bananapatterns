import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { useIsPhone } from '../useMedia';

const W = 210, H = 112;                                   // box size on the canvas
const STATUS = { ok: ['✓', 'ok'], running: ['…', 'run'], idle: ['idle', 'idle'], error: ['!', 'err'] };
const MODES = [['off', 'Off'], ['signals', 'Signals']];
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
  const load = () => api.getFlow().then((x) => { setD(x); setErr(''); }).catch((e) => setErr(e.message));
  useEffect(() => {
    load();
    const a = setInterval(load, 5000), b = setInterval(() => tick((x) => x + 1), 1000);
    return () => { clearInterval(a); clearInterval(b); };
  }, []);
  if (!d) return <div className="panel">{err ? <span className="warn">{err}</span> : 'Loading…'}</div>;

  const byId = Object.fromEntries(d.nodes.map((n) => [n.id, n]));
  const node = byId[sel] || d.nodes[0];
  const setMode = (mode) => api.flowMode(mode).then(load).catch((e) => setErr(e.message));
  const change = (key, v) => api.flowNode(node.id, { [key]: v }).then(load).catch((e) => setErr(e.message));
  const runNow = () => api.flowRun().then(load).catch((e) => setErr(e.message));
  const maxX = Math.max(...d.nodes.map((n) => n.x)) + W + 30, maxY = Math.max(...d.nodes.map((n) => n.y)) + H + 30;
  const f = d.feed || {};

  return (
    <div className="flowpage">
      <div className="flowtop">
        <b className="flowtitle">⚡ Intraday Flow</b>
        <span className="sb">Template: <b>{d.template}</b></span>
        <span className="seg">{MODES.map(([id, label]) => (
          <button key={id} className={d.mode === id ? 'active' : ''} onClick={() => setMode(id)}>{label}</button>))}</span>
        <span className="sb"><i className={`dot ${f.state === 'live' ? 'ok' : f.state === 'off' ? '' : 'warn'}`} />
          {f.state === 'live' ? `Live ticks · ${f.tokens} stocks` : f.state === 'off' ? 'Feed off (starts 9:10 AM in Signals mode)' : `Feed ${f.state}`}</span>
        <span className="sb">Next {d.interval}m close in <b className="mono">{nextClose(d.interval)}</b></span>
        {d.last_run && <span className="sb">Last run {ist(d.last_run.at)} · {d.last_run.ms} ms</span>}
        <button className="btn" onClick={runNow} disabled={d.running}>{d.running ? 'Running…' : '▶ Run now'}</button>
      </div>
      {err && <div className="warn small">{err}</div>}
      <div className="flowbody">
        <div className="flowcanvas"><div className="flowinner" style={{ width: maxX, height: maxY }}>
          <svg className="flowedges" width={maxX} height={maxY}>
            {d.nodes.flatMap((n) => n.inputs.map((i) => {
              const a = byId[i]; const x1 = a.x + W, y1 = a.y + H / 2, x2 = n.x, y2 = n.y + H / 2;
              const dx = Math.max(40, Math.abs(x2 - x1) / 2);
              const path = x2 > x1 ? `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
                : `M${x1},${y1} C${x1 + 60},${y1} ${x1 + 60},${(y1 + y2) / 2} ${(x1 + x2) / 2},${(y1 + y2) / 2} S${x2 - 60},${y2} ${x2},${y2}`;
              return <path key={i + n.id} d={path} className={`edge ${a.status === 'ok' || a.status === 'running' ? 'live' : ''}`} />;
            }))}
          </svg>
          {d.nodes.map((n) => {
            const [label, cls] = STATUS[n.status] || STATUS.idle;
            return (
              <button key={n.id} className={`fnode ${sel === n.id ? 'sel' : ''}`} style={{ left: n.x, top: n.y, width: W }} onClick={() => setSel(n.id)}>
                <div className="fh"><span className="fico">{n.icon}</span><b>{n.title}</b><span className={`fst ${cls}`}>{label}</span></div>
                {n.in != null && <div className="fio"><b>{n.in}</b> → <b className="up">{n.out}</b> <span className="muted small">{n.ms} ms</span></div>}
                <div className="fsum">{n.summary}</div>
                {n.inputs.length > 0 && <span className="port in" />}<span className="port out" />
              </button>);
          })}
        </div></div>
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
      </div>
      <div className="panel"><h3>🟢 Today's signals <span className="muted small">· signals only, no orders</span></h3>
        {!d.signals.length ? <p className="muted">None yet today.</p> : (
          <div className="tablewrap"><table className="grid"><thead><tr><th>Time</th><th>Stock</th><th>Signal</th><th className="num">Entry</th>
            <th className="num">SL</th><th className="num">Target</th>{!phone && <><th className="num">RVOL</th><th className="num">RSI</th></>}</tr></thead>
            <tbody>{d.signals.map((s) => (
              <tr key={s.symbol}><td className="muted">{ist(s.at)}</td><td><b>{s.symbol}</b></td><td className="up">{s.signal}</td>
                <td className="num">{s.entry?.toFixed(2)}</td><td className="num">{s.sl?.toFixed(2)}</td><td className="num">{s.target?.toFixed(2)}</td>
                {!phone && <><td className="num">{s.rvol ?? '—'}×</td><td className="num">{s.rsi ?? '—'}</td></>}</tr>))}</tbody></table></div>)}
      </div>
    </div>);
}
