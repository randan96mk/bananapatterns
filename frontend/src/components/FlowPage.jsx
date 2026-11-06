import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { push, useLive } from '../services/live';
import { StopMeter } from './ui';
import { Head, Chip, Cards, Card, Bar, Panel, FixedTable, Seg, Tag, Box } from './ui2';
import { clock } from '../timefmt';
import { inr, signed, cls } from '../fmt';
import FlowBacktest from './FlowBacktest';
import FlowStrategy from './FlowStrategy';
import JourneyPanel, { NodeStocks } from './JourneyPanel';

const STATUS = { ok: ['✓', 'ok'], running: ['…', 'run'], idle: ['idle', 'idle'], error: ['!', 'err'] };
const MODES = [['off', 'Off'], ['signals', 'Signals'], ['paper', '📝 Paper']];
const SUBS = [['flow', 'Flow'], ['positions', 'Positions'], ['journal', 'Journal'], ['backtest', '🧪 Backtest'], ['strategy', '📜 Strategy']];

// Seconds to the next candle close of `interval` minutes (aligned to 9:15 IST).
function nextClose(interval) {
  const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  const m = now.getHours() * 60 + now.getMinutes() - (9 * 60 + 15);
  const left = (interval - (((m % interval) + interval) % interval)) * 60 - now.getSeconds();
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
}

// Boxes in the order they run: each after every box it reads from.
function ordered(nodes) {
  const out = [], done = new Set();
  let left = nodes.slice();
  while (left.length) {
    const ready = left.filter((n) => n.inputs.every((i) => done.has(i)));
    (ready.length ? ready : [left[0]]).forEach((n) => { out.push(n); done.add(n.id); });
    left = left.filter((n) => !done.has(n.id));
  }
  return out;
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
const n2 = (v) => (v == null ? '—' : (+v).toFixed(2));
const STATUS_TXT = { open: ['open', 'ok'], pending: ['filling', 'w'], closed: ['closed', ''], cancelled: ['cancelled', ''], signal: ['signal', 'a'] };
function Status({ r }) {
  const [t, c] = STATUS_TXT[r.status] || ['—', ''];
  return <Tag c={c} title={r.status === 'pending' ? 'filling at the next minute' : undefined}>{t}{r.status === 'closed' && r.exit_t ? ` ${r.exit_t}` : ''}</Tag>;
}
// The box's one line: the first parts of its summary that fit (the whole summary sits in the settings panel).
const brief = (s) => { const parts = (s || '').split(' · '); let out = parts[0].slice(0, 34); for (const p of parts.slice(1)) { if (out.length + p.length + 3 > 34) return `${out} …`; out += ` · ${p}`; } return out; };
const drops = (n) => Object.entries(n.drops || {}).map(([k, v]) => `${k} ${v}`).join(' · ');

const SIG_COLS = [{ h: 'Stock' }, { h: 'Time', w: 70 }, { h: 'Signal', w: 110 }, { h: 'Status', w: 110 }, { h: 'Entry', w: 80, n: true }, { h: 'Now', w: 90, n: true },
  { h: 'P&L after costs', w: 110, n: true }, { h: '%', w: 70, n: true, lo: true }, { h: 'R', w: 50, n: true, lo: true }, { h: 'Stop ← price → target', w: 150 },
  { h: 'SL', w: 90, n: true, lo: true }, { h: 'Target', w: 80, n: true, lo: true }, { h: 'RVOL', w: 60, n: true, lo: true }];
const POS_COLS = [{ h: 'Stock' }, { h: 'Side', w: 80 }, { h: 'Qty', w: 60, n: true }, { h: 'Entry', w: 90, n: true }, { h: 'Stop', w: 90, n: true }, { h: 'Target', w: 90, n: true, lo: true },
  { h: 'Distance to stop', w: 150 }, { h: 'Status', w: 100 }, { h: 'After costs', w: 130, n: true }];
const AGG_COLS = [{ h: '' }, { h: 'Trades', w: 70, n: true }, { h: 'Won', w: 70, n: true }, { h: 'Net', w: 110, n: true }, { h: 'Avg R', w: 70, n: true }];

// ⚡ Intraday Flow: the template's boxes as a pipeline, live (in → out, status), settings per box.
// Signals only in phase 1: nothing is ever ordered from here.
export default function FlowPage() {
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
  const [full, setFull] = useState(false);                // full screen: the pipe alone, no side column
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
  if (!d) return <div className="rd"><Panel title="⚡ Intraday Flow"><p className="muted small rempty">{err ? <span className="warn">{err}</span> : 'Loading…'}</p></Panel></div>;

  const nodes = ordered(d.nodes);
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const resetLayout = () => api.flowLayout({}).then(load).catch((e) => setErr(e.message));
  const node = byId[sel] || d.nodes[0];
  const setMode = (mode) => api.flowMode(mode).then(load).catch((e) => setErr(e.message));
  const change = (key, v) => {
    if (node.kind === 'orb' && key.startsWith('set_') && !v                     // at least one entry setup stays ticked
      && !node.schema.some((f) => f.key.startsWith('set_') && f.key !== key && node.config[f.key])) {
      setErr('At least one entry setup must stay ticked.');
      return;
    }
    api.flowNode(node.id, { [key]: v }).then(load).catch((e) => setErr(e.message));
  };
  // a setting labelled "…" belongs to the switch above it: hidden while that switch is off
  const visible = (schema, cfg) => { let on = true; return schema.filter((f) => {
    if (f.type === 'bool' && !f.label.startsWith('…')) { on = !!cfg[f.key]; return true; }
    return !f.label.startsWith('…') || on; }); };
  const toggleStop = () => {
    if (!d.stopped && !window.confirm(`Stop all intraday activity?${d.stats?.open ? `\n${d.stats.open} open paper position(s) will be squared off at the last price.` : ''}\nNo live data, signals or trades until you press Start.`)) return;
    (d.stopped ? api.flowStart() : api.flowStop()).then(load).catch((e) => setErr(e.message));
  };
  const runNow = () => api.flowRun().then(load).catch((e) => setErr(e.message));
  const defaults = () => window.confirm('Put every box back to its default settings? Applies from the next candle. Save the canvas as a setup first (🧪 Backtest) if you want to keep the current one.')
    && api.flowResetNodes().then(load).catch((e) => setErr(e.message));
  const f = d.feed || {};
  const stats = lv?.stats || d.stats;
  const used = stats ? Math.max(0, -stats.realised) : 0;
  const today = stats ? stats.realised + stats.unrealised : null;

  const sigRows = d.signals.map((s) => { const r = lv?.rows?.[s.symbol] || {}; return { key: s.symbol, cells: [
    <><b>{s.symbol}</b>{s.qty ? <small> {s.qty} sh</small> : null}</>, clock(s.at), <span className={s.side === 'short' ? 'down' : 'up'}>{s.signal}</span>, <Status r={r} />,
    n2(r.entry ?? s.entry), r.status === 'closed' ? <span className="muted" title="exit">{n2(r.exit)} (exit)</span> : r.now ? <b>{r.now.toFixed(2)}</b> : '—',
    r.pnl != null ? <b className={cls(r.pnl)}>{signed(r.pnl)}</b> : '—',
    <span className={cls(r.pct ?? r.move_pct)}>{r.pct != null ? pctx(r.pct) : r.move_pct != null ? <span title="move since the signal">{pctx(r.move_pct)}</span> : '—'}</span>,
    <span className={cls(r.r)}>{r.r ?? '—'}</span>,
    r.status === 'open' && r.now ? <StopMeter last={s.side === 'short' ? -r.now : r.now} stop={s.side === 'short' ? -r.stop : r.stop} />
      : <small>{r.status === 'closed' ? `out ${r.exit_t} · ${r.reason}` : r.status === 'pending' ? 'waiting for the fill' : ''}</small>,
    <>{n2(r.stop ?? s.sl)}{r.trailed && <small> ↑ trailed</small>}</>, s.target ? s.target.toFixed(2) : '—', `${s.rvol ?? '—'}×`] }; });
  const posRows = (d.positions || []).map((p) => {
    const lg = p.side === 'long', last = p.last_price || p.entry;
    const unreal = p.status === 'open' && p.entry ? (lg ? last - p.entry : p.entry - last) * p.open_qty : null;
    return { key: p.id, cells: [<><b>{p.symbol}</b> <small>{p.entry_t || p.signal_t}</small></>, <span className={lg ? 'up' : 'down'}>{lg ? '▲ long' : '▼ short'}</span>,
      p.open_qty ?? p.qty, p.entry ? p.entry.toFixed(2) : 'pending', n2(p.stop ?? p.stop0), p.target ? p.target.toFixed(2) : '—',
      p.status === 'open' ? <StopMeter last={lg ? last : (p.stop ?? p.stop0)} stop={lg ? (p.stop ?? p.stop0) : last} /> : '',
      p.status === 'closed' ? p.reason : p.status,
      <><b className={cls(p.net ?? unreal)}>{signed(p.net ?? unreal)}</b>{p.costs != null && <small title={`${signed(p.gross)} before − ${inr(p.costs)} costs`}> {signed(p.gross)} − {inr(p.costs)}</small>}</>] };
  });
  const aggRows = (g) => Object.entries(jr?.by?.[g] || {}).map(([k, v]) => ({ key: k, cells: [k, v.trades, `${v.win_rate}%`, <span className={cls(v.net)}>{signed(v.net)}</span>, v.avg_r] }));

  const nodePanel = <Panel title={`${node.icon} ${node.title}`} right={<span title={node.summary}>{node.summary}</span>}>
    {node.schema.length > 0 && <div className="rform">
      {visible(node.schema, node.config).map((s) => <label key={s.key} className="fr"><span>{s.label}</span><Field f={s} value={node.config[s.key]} onChange={(v) => change(s.key, v)} /></label>)}
      <div className="rnote" style={{ padding: '6px 0' }}>Applies from the next candle.</div></div>}
    {node.kind !== 'monitor' && <div className="rbody">
      <button type="button" className="btn small" onClick={() => rerun(node.id)} title="Same candle's data, the settings above; no alert, no trade">↻ Re-run from this box</button>
      {rr?.from === node.id && <div className={`small ${rr.error ? 'warn' : 'muted'}`} style={{ marginTop: 6 }}>{rr.error ? rr.error
        : <>Dry run on the latest candle with these settings: {rr.boxes.length} box(es) re-run · <b>{rr.would_signal}</b> would signal (no alert sent, no trade opened).</>}</div>}</div>}
    {node.in != null && <div className="rbody"><b>Last run</b> {node.dry && <Tag c="w">dry run</Tag>}
      <div className="small"><span className="mono">{node.in}</span> in · <b className="mono">{node.out}</b> passed · <span className="mono">{node.ms}</span> ms</div>
      {drops(node) && <div className="small muted" style={{ marginTop: 4 }}>Dropped: {drops(node)}</div>}
      {node.sample?.length > 0 && <div className="flowchips">{node.sample.map((s) => <span key={s} className="spill">{s}</span>)}
        {node.out > node.sample.length && <span className="muted small">+{node.out - node.sample.length}</span>}</div>}</div>}
    {node.kind !== 'monitor' && <div className="rbody"><NodeStocks nid={node.id} stamp={`${d.last_run?.at}|${rr?.from || ''}|${node.in}|${node.out}`} onSymbol={(x) => { setJsym(x); setJourney(true); }} /></div>}
  </Panel>;

  return (
    <div className="rd">
      <Head title="⚡ Intraday Flow" right={<>
        <button type="button" className={`btn small killbtn ${d.stopped ? 'start' : 'stop'}`} onClick={toggleStop}>{d.stopped ? '▶ Start intraday' : '⏹ Stop intraday'}</button>
        <Seg opts={MODES} v={d.mode} set={setMode} />
        <button type="button" className="btn small" onClick={runNow} disabled={d.running}>{d.running ? 'Running…' : '▶ Run now'}</button></>}>
        <Chip dot={f.state === 'live' ? 'ok' : f.state === 'off' ? '' : 'warn'} title={f.error || undefined}>
          {f.state === 'live' ? <>Live ticks · <b>{f.tokens}</b> stocks</> : f.state === 'off' ? 'Feed off (starts 9:10 AM in Signals or Paper mode)' : `Feed ${f.state}`}</Chip>
        <Chip title={d.template}>Template <b>{d.template}</b></Chip>
        <Chip>Next {d.interval}m close in <b className="mono" style={{ display: 'inline-block', width: 40 }}>{nextClose(d.interval)}</b></Chip>
        {d.last_run && <Chip>Last run <b>{clock(d.last_run.at)}</b> · <span className="mono">{d.last_run.ms}</span> ms</Chip>}
      </Head>
      {d.stopped && <div className="rbanner bad">⏹ <b>INTRADAY STOPPED</b> · no live data, no flow runs, no signals, no paper
        trades. Stays stopped (also after a restart or the next day) until you press ▶ Start intraday.</div>}
      {err && <div className="rbanner warn">{err}</div>}
      {stats && <Cards n={4}>
        <Card label="Today · after costs" value={signed(today)} num={today} k={cls(today)} live={feedLive}
          sub={<>realised <span className={cls(stats.realised)}>{signed(stats.realised)}</span> · open <span className={cls(stats.unrealised)}>{signed(stats.unrealised)}</span></>}
          fact={`mode ${d.mode}`} />
        <Card unit="n" label="Paper trades" value={stats.trades} num={stats.trades} sub={`${stats.won} won · ${stats.open} open · ${stats.closed ?? 0} closed`} fact={d.mode === 'paper' ? 'simulated, no orders' : d.mode === 'signals' ? 'signals tracked like paper' : 'mode off: nothing tracked'} />
        <Card label="At risk now" value={inr(stats.at_risk)} num={stats.at_risk} k="down" sub="to the stops, open positions" fact={`capital ${inr(stats.capital)}`} />
        <Card label="Daily loss used" value={inr(used)} num={used} k={used > 0 ? 'down' : ''} sub={`of ${inr(stats.loss_limit)} limit`}
          fact={<><Bar pct={stats.loss_limit ? (used / stats.loss_limit) * 100 : 0} bad={used >= stats.loss_limit} /> {stats.loss_limit ? Math.round((used / stats.loss_limit) * 100) : 0}% used</>} />
      </Cards>}
      <Seg opts={SUBS.map(([k, l]) => [k, k === 'positions' ? `${l} (${d.positions?.length || 0})` : l])} v={sub} set={setSub} />

      {sub === 'positions' && <Panel title="📝 Paper positions today" meta={d.positions?.length || 0} right="simulated, no orders">
        <FixedTable cols={POS_COLS} rows={posRows} empty={`None today${d.mode === 'off' ? ' (the mode is Off)' : ''}.`} /></Panel>}
      {journey && <><div className="drawer-back" onClick={() => setJourney(false)} />
        <JourneyPanel symbol={jsym} onSymbol={setJsym} onClose={() => setJourney(false)} /></>}
      {sub === 'backtest' && <FlowBacktest />}
      {sub === 'strategy' && <FlowStrategy stamp={d?.nodes} />}
      {sub === 'journal' && (!jr ? <Panel title="📒 Journal"><p className="muted small rempty">Loading…</p></Panel> : !jr.total.trades
        ? <Panel title="📒 Journal" right="closed paper trades, last 30 days, after costs"><p className="muted small rempty">No closed paper trades yet.</p></Panel> : <>
          <Cards n={4}>
            <Card label="Journal · net after costs" value={signed(jr.total.net)} num={jr.total.net} k={cls(jr.total.net)} sub={`${signed(jr.total.gross)} before − ${inr(jr.total.costs)} costs`} fact="last 30 days" />
            <Card unit="%" label="Won" value={`${jr.total.win_rate}%`} num={jr.total.win_rate} sub={`${jr.total.trades} trades`} />
            <Card unit="n" label="Average R" value={`${jr.total.avg_r ?? '—'}R`} num={jr.total.avg_r} sub="per closed trade" />
            <Card unit="n" label="Profit factor" value={jr.total.profit_factor ?? '—'} num={jr.total.profit_factor} sub="gross wins ÷ gross losses" />
          </Cards>
          <div className="cols c2">
            <Panel title="By side"><FixedTable cols={AGG_COLS} rows={aggRows('side')} empty="Nothing yet." /></Panel>
            <Panel title="By entry time"><FixedTable cols={AGG_COLS} rows={aggRows('time')} empty="Nothing yet." /></Panel>
          </div></>)}

      {sub === 'flow' && <div className={full ? '' : 'two'}>
        <div className="rd">
          <Panel title="The flow · live counts from the last scan" meta={`${nodes.length} boxes`} right={<>
            <button type="button" className="btn ghost small" onClick={() => setFull(!full)} title={full ? 'Exit full screen (Esc)' : 'Full screen'}>{full ? '✕ Exit full screen' : '⛶ Full screen'}</button>
            <button type="button" className="btn ghost small" onClick={() => { setJsym(null); setJourney(true); }} title="Why a trade or not: every step, the rules, a stock's check">🧭 Journey</button>
            <button type="button" className="btn ghost small" onClick={resetLayout} title="Put every box back in its place">↺ Reset layout</button>
            <button type="button" className="btn ghost small" title="Every box's settings back to the recommended defaults" onClick={defaults}>↺ Default settings</button></>}>
            <div className="rpipe" style={{ gridTemplateColumns: `repeat(auto-fit, minmax(${full ? 220 : 236}px, 1fr))`, rowGap: 10 }}>{nodes.map((n) => <Box key={n.id} icon={n.icon} title={n.title} inN={n.in} outN={n.out} text={brief(n.summary)} status={STATUS[n.status] || STATUS.idle}
              on={!n.config?.skip} selected={sel === n.id} onClick={() => setSel(n.id)} />)}</div>
            <div className="rsteps">{nodes.map((n, i) => <div key={n.id} className={`step ${sel === n.id ? 'sel' : ''}`} onClick={() => setSel(n.id)}>
              <span className="c">{i + 1}</span><span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', opacity: n.config?.skip ? 0.5 : 1 }}>{n.icon} {n.title}</span>
              <span className="n mono">{n.out ?? '—'}{n.in != null && <small className="muted"> of {n.in}</small>}</span></div>)}</div>
            {full && <div className="rnote">{node.icon} <b>{node.title}</b> · {node.summary}{node.in != null && <> · {node.in} in · <b>{node.out} passed</b> · {node.ms} ms{drops(node) ? ` · dropped: ${drops(node)}` : ''}</>} · exit full screen to change settings.</div>}
          </Panel>
          <Panel title="🟢 Today's signals" meta={d.signals.length} right={<>
            <span>{d.mode === 'paper' ? 'paper trades are opened from these (simulated)' : d.mode === 'signals' ? 'signals only, no orders · each is tracked like a paper trade' : 'mode off'}</span>
            {lv && <Chip dot={feedLive ? 'ok' : 'warn'}>{feedLive ? <>live · prices <b>{clock(lv.at)}</b></> : <>prices as of <b>{clock(lv.at)}</b> (feed {lv.feed})</>}</Chip>}</>}>
            <FixedTable cols={SIG_COLS} rows={sigRows} empty="None yet today." /></Panel>
        </div>
        {!full && nodePanel}
      </div>}
    </div>);
}
