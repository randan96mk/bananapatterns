import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import { stamp } from '../timefmt';
import { Panel, Act } from './ui2';

// 📜 The strategy running now, in plain words: one text to copy or save, rewritten whenever a box changes; the last changes.
export default function FlowStrategy({ stamp: boxes }) {
  const [d, setD] = useState(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => { api.flowStrategy().then(setD).catch(() => {}); }, [JSON.stringify(boxes || null)]);   // re-read when the boxes change
  if (!d) return <Panel title="📜 What is running now"><p className="muted small rempty">Loading…</p></Panel>;
  const copy = () => navigator.clipboard?.writeText(d.text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  const save = () => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([d.text], { type: 'text/plain' }));
    a.download = `intraday-strategy-${new Date().toISOString().slice(0, 10)}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return <div className="two">
    <Panel title="📜 What is running now" right={<><span>built from the boxes · changes the moment a box changes, a setup is loaded or the boxes are reset</span>
      <button type="button" className="btn primary small" onClick={copy}>{copied ? '✓ Copied' : '📋 Copy'}</button>
      <button type="button" className="btn small" onClick={save}>⬇ .txt</button></>}>
      <pre className="rtxt">{d.text}</pre>
    </Panel>
    <Panel title="Change log" meta={d.changes.length} right="the last changes to the boxes">
      {!d.changes.length ? <p className="muted small rempty">No changes recorded yet (changes are logged from now on).</p>
        : d.changes.map((c, i) => <Act key={i} icon="⚙" cls="act" text={<><b>{c.box}</b> · {c.what.join(' · ')}</>} meta={stamp(c.at)} />)}
    </Panel>
  </div>;
}
