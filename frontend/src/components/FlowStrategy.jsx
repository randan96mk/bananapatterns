import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

// 📜 The strategy running now, in plain words: one text to copy or save, rewritten whenever a box changes; the last changes.
const when = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true }) : '');

export default function FlowStrategy({ stamp }) {
  const [d, setD] = useState(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => { api.flowStrategy().then(setD).catch(() => {}); }, [JSON.stringify(stamp || null)]);   // re-read when the boxes change
  if (!d) return <div className="panel muted">Loading…</div>;
  const copy = () => navigator.clipboard?.writeText(d.text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  const save = () => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([d.text], { type: 'text/plain' }));
    a.download = `intraday-strategy-${new Date().toISOString().slice(0, 10)}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return <>
    <div className="panel">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
        <b>📜 What is running now</b>
        <span className="muted small">· written from the boxes on the canvas · changes the moment a box changes, a setup is loaded or the boxes are reset</span>
        <button className="btn primary" style={{ marginLeft: 'auto' }} onClick={copy}>{copied ? '✓ Copied' : '📋 Copy'}</button>
        <button className="btn" onClick={save}>⬇ .txt</button>
      </div>
      <pre className="mono small" style={{ whiteSpace: 'pre-wrap', margin: 0, lineHeight: 1.55 }}>{d.text}</pre>
    </div>
    <div className="panel"><b>Change log</b> <span className="muted small">· the last {d.changes.length} changes to the boxes</span>
      {!d.changes.length ? <p className="muted small">No changes recorded yet (changes are logged from now on).</p>
        : <div className="v2list">{d.changes.map((c, i) => <div key={i} className="it"><span className="muted small" style={{ width: 120 }}>{when(c.at)}</span>
          <div style={{ flex: 1 }}><b>{c.box}</b><div className="small">{c.what.join(' · ')}</div></div></div>)}</div>}
    </div>
  </>;
}
