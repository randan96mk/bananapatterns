// Themes. A theme ("skin") sets colours, fonts, corners and shadows; each is light or dark underneath
// (<html data-theme="light|dark" data-skin="...">) so charts and components keep working. The choice is remembered
// per browser; "auto" follows the system: Fintech Light by day settings, Precision Dark by night settings.
// index.html applies the same before React loads (no flash) and a "themechange" event announces changes.
import { useEffect, useState } from 'react';

const KEY = 'bp.theme';
const media = () => window.matchMedia('(prefers-color-scheme: light)');

// id -> [label, light|dark]
export const THEMES = {
  auto: ['◐ Auto (Fintech Light / Precision Dark)', null],
  fintech: ['Fintech Light', 'light'],
  precision: ['Precision Dark', 'dark'],
  consumer: ['Consumer Black', 'dark'],
  workspace: ['Pro Workspace', 'dark'],
  glass: ['Glass', 'dark'],
  clay: ['Clay', 'light'],
  light: ['Classic Light', 'light'],
  dark: ['Classic Dark', 'dark'],
};

export function getMode() {
  try { const m = localStorage.getItem(KEY) || 'auto'; return THEMES[m] ? m : 'auto'; } catch { return 'auto'; }
}

// mode -> {skin, theme}
export function resolveSkin(mode) {
  const m = THEMES[mode] ? mode : 'auto';
  if (m === 'auto') return media().matches ? { skin: 'fintech', theme: 'light' } : { skin: 'precision', theme: 'dark' };
  return { skin: m, theme: THEMES[m][1] };
}

export const resolve = (mode) => resolveSkin(mode).theme;

export function applyMode(mode) {
  try { localStorage.setItem(KEY, mode); } catch { /* private mode: not remembered */ }
  const { skin, theme } = resolveSkin(mode);
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.skin = skin;
  window.dispatchEvent(new CustomEvent('themechange', { detail: `${theme}:${skin}` }));
}

// Follow the system while in auto mode.
media().addEventListener('change', () => { if (getMode() === 'auto') applyMode('auto'); });

// "light|dark:skin": changes whenever the colours do (charts rebuild on it).
export function useTheme() {
  const [theme, setTheme] = useState(() => {
    const d = document.documentElement.dataset;
    const r = resolveSkin(getMode());
    return `${d.theme || r.theme}:${d.skin || r.skin}`;
  });
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
