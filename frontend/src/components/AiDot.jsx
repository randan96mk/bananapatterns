import React from 'react';
import { stamp } from '../timefmt';
import { AiTag } from './ui2';

// 🤖 A position row's AI note: a small robot and a coloured dot after the symbol when the stock has a note in the last
// 7 days (`row.ai_note` = {title, severity, at} from the overview). The dot is red for high, amber for medium, muted
// otherwise (an emoji takes no colour, so the dot carries it); the title is the tooltip. `dot` = the dot only (phone).
// No column, no layout change: it renders nothing without a note.
const COLOR = { high: 'var(--bad)', medium: 'var(--warn)' };
export default function AiDot({ note, dot }) {
  if (!note) return null;
  const c = COLOR[note.severity] || 'var(--muted)';
  return <span title={`🤖 AI · ${note.severity} · ${note.title}${note.at ? ` · ${stamp(note.at)}` : ''} · System → AI`} aria-label={`AI note: ${note.title}`}
    style={{ display: 'inline-flex', alignItems: 'center', gap: 3, marginLeft: 4, verticalAlign: 'middle', fontSize: 10, lineHeight: 1, whiteSpace: 'nowrap' }}>
    {!dot && <span style={{ opacity: COLOR[note.severity] ? 1 : 0.7 }}>🤖</span>}
    <i style={{ display: 'inline-block', width: 7, height: 7, borderRadius: 4, background: c, flex: 'none' }} /></span>;
}

// 🤖 The "why" line under a closed trade (`row.ai_why` = {id, text, at}): one small line with an ellipsis, the full
// text as the tooltip. Meant for a FixedTable span row, so the trade row itself keeps its 34 px.
export function AiWhy({ why }) {
  if (!why || !why.text) return null;
  return <div className="muted" title={`🤖 AI · likely cause · ${why.text}${why.at ? ` · ${stamp(why.at)}` : ''} · System → AI › Notes › Why`}
    style={{ fontSize: 11, lineHeight: '16px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}><AiTag of={why} /> {why.text}</div>;
}
