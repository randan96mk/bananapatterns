// 📡 One live connection for the whole page (GET /api/stream, server-sent events) and one shared store: every
// panel showing the same data (schedule, attention, Kite, the flow, live prices, backtest progress) reads it from
// here, instead of each polling on its own timer. The connection carries only the topics on screen, and the server
// sends a topic only when it changes. If the connection drops, the browser reconnects by itself.
import { useEffect, useState } from 'react';

const subs = {};            // topic -> Set of setState
const last = {};            // topic -> latest value
const stamps = {};          // topic -> ms of the latest value (the heartbeat's stale check)
let es = null;
let open = '';
let pending = null;

function sync() {
  pending = null;
  const topics = Object.keys(subs).filter((t) => subs[t].size).sort().join(',');
  if (topics === open) return;
  open = topics;
  if (es) es.close();
  es = null;
  if (!topics || typeof EventSource === 'undefined') return;
  es = new EventSource(`/api/stream?topics=${topics}`);
  topics.split(',').forEach((t) => es.addEventListener(t, (e) => {
    try { push(t, JSON.parse(e.data)); } catch { /* a bad message is skipped */ }
  }));
}
const later = () => { if (!pending) pending = setTimeout(sync, 0); };

// Puts a value in the store (from the stream, or right after an action that changed it) and tells every panel.
export function push(topic, value) {
  last[topic] = value;
  stamps[topic] = Date.now();
  (subs[topic] || []).forEach((set) => set(value));
}

// The latest value of a topic (undefined until the first one arrives); subscribes while the component is on screen.
export function useLive(topic) {
  const [value, setValue] = useState(last[topic]);
  useEffect(() => {
    (subs[topic] = subs[topic] || new Set()).add(setValue);
    if (last[topic] !== undefined) setValue(last[topic]);
    later();
    return () => { subs[topic].delete(setValue); later(); };
  }, [topic]);
  return value;
}

// When the topic's latest value arrived (ms), re-read every 15 s so a stale block can dim.
export function useLiveStamp(topic) {
  const value = useLive(topic);
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 15000); return () => clearInterval(t); }, []);
  return value === undefined ? null : stamps[topic] || null;
}
