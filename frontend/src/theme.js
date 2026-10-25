// Light / dark theme. The mode (auto / light / dark) is remembered per browser; "auto" follows the
// system setting. The resolved theme is set as <html data-theme="light|dark"> (index.html sets it
// before React loads, so there is no flash) and announced with a "themechange" event.
import { useEffect, useState } from 'react';

const KEY = 'bp.theme';
const media = () => window.matchMedia('(prefers-color-scheme: light)');

export function getMode() {
  try { return localStorage.getItem(KEY) || 'auto'; } catch { return 'auto'; }
}

export function resolve(mode) {
  return mode === 'auto' ? (media().matches ? 'light' : 'dark') : mode;
}

export function applyMode(mode) {
  try { localStorage.setItem(KEY, mode); } catch { /* private mode: not remembered */ }
  const theme = resolve(mode);
  document.documentElement.dataset.theme = theme;
  window.dispatchEvent(new CustomEvent('themechange', { detail: theme }));
}

// Follow the system while in auto mode.
media().addEventListener('change', () => { if (getMode() === 'auto') applyMode('auto'); });

export function useTheme() {
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme || resolve(getMode()));
  useEffect(() => {
    const on = (e) => setTheme(e.detail);
    window.addEventListener('themechange', on);
    return () => window.removeEventListener('themechange', on);
  }, []);
  return theme;
}

// A CSS custom property's current value (charts need real colours, not var()).
export const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// With alpha: cssVar colour (#rrggbb) + 2-digit hex alpha.
export const withAlpha = (name, aa) => `${cssVar(name)}${aa}`;
