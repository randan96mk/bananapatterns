import { useEffect, useState } from 'react';

// True under 600px wide (phones). Matches the CSS breakpoint in App.css.
export const PHONE = '(max-width: 599px)';

// A narrow desktop window (under 1440 px): tables drop their low-priority columns so the Stock column keeps 140 px.
export const NARROW = '(max-width: 1439px)';

export function useMedia(query) {
  const [on, setOn] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const m = window.matchMedia(query);
    const f = () => setOn(m.matches);
    m.addEventListener('change', f);
    return () => m.removeEventListener('change', f);
  }, [query]);
  return on;
}

export const useIsPhone = () => useMedia(PHONE);
export const useNarrow = () => useMedia(NARROW);
