// 💓 Heartbeat: a block pulses when its own data changes. useBeat(value) says which way a number moved (for the
// ring and the flash, 1.2 s) and keeps the delta chip for 10 s; useFlashKeys(map) marks the rows whose value
// changed this update; useStale(stamp, ms) says a live block has had no update for too long; useUnread(count)
// puts a dot in the browser tab while there is something new.
import { useEffect, useRef, useState } from 'react';

export const RING_MS = 1200, CHIP_MS = 10000;

export function useBeat(value) {
  const prev = useRef(value);
  const [beat, setBeat] = useState({ dir: null, delta: null });
  useEffect(() => {
    if (typeof value !== 'number' || typeof prev.current !== 'number' || value === prev.current) { prev.current = value; return undefined; }
    const delta = value - prev.current;
    prev.current = value;
    setBeat({ dir: delta > 0 ? 'up' : 'down', delta, on: true });
    const a = setTimeout(() => setBeat((b) => ({ ...b, on: false })), RING_MS);
    const b = setTimeout(() => setBeat({ dir: null, delta: null }), CHIP_MS);
    return () => { clearTimeout(a); clearTimeout(b); };
  }, [value]);
  return beat;
}

// map: {key: number}. Returns {key: 'up' | 'down'} for the keys whose number changed in the latest update, for 1.2 s.
export function useFlashKeys(map) {
  const prev = useRef({});
  const [flash, setFlash] = useState({});
  useEffect(() => {
    const changed = {};
    Object.entries(map || {}).forEach(([k, v]) => {
      const p = prev.current[k];
      if (typeof v === 'number' && typeof p === 'number' && v !== p) changed[k] = v > p ? 'up' : 'down';
      else if (p === undefined && Object.keys(prev.current).length) changed[k] = 'new';
    });
    prev.current = { ...(map || {}) };
    if (!Object.keys(changed).length) return undefined;
    setFlash(changed);
    const t = setTimeout(() => setFlash({}), RING_MS);
    return () => clearTimeout(t);
  }, [map]);
  return flash;
}

// True when `stamp` (ms) is older than `ms` and `live` is on: the block should dim and say so.
export function useStale(stamp, ms, live = true) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 15000); return () => clearInterval(t); }, []);
  return !!(live && stamp && Date.now() - stamp > ms);
}

// A dot in the tab title while `unread` is true.
export function useUnread(unread) {
  useEffect(() => {
    const base = document.title.replace(/^● /, '');
    document.title = unread ? `● ${base}` : base;
    return () => { document.title = base; };
  }, [unread]);
}

// Items seen before (by id) across page loads; returns the set of new ids among `ids` and marks them seen after `ms`.
export function useNewIds(key, ids, ms = 60000) {
  const [fresh, setFresh] = useState([]);
  useEffect(() => {
    if (!ids.length) return undefined;                      // nothing loaded yet: neither seen nor new
    let seen = null;
    try { const raw = sessionStorage.getItem(key); seen = raw == null ? null : JSON.parse(raw); } catch { /* private mode */ }
    const now = seen == null ? [] : ids.filter((id) => !seen.includes(id));
    try { sessionStorage.setItem(key, JSON.stringify(ids.slice(-200))); } catch { /* private mode */ }
    if (!now.length) { setFresh([]); return undefined; }
    setFresh(now);
    const t = setTimeout(() => setFresh([]), ms);
    return () => clearTimeout(t);
  }, [key, ids.join('|')]);
  return fresh;
}
