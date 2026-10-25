import { useEffect, useState } from 'react';

// True under 600px wide (phones). Matches the CSS breakpoint in App.css.
export const PHONE = '(max-width: 599px)';

export function useIsPhone() {
  const [phone, setPhone] = useState(() => window.matchMedia(PHONE).matches);
  useEffect(() => {
    const m = window.matchMedia(PHONE);
    const on = () => setPhone(m.matches);
    m.addEventListener('change', on);
    return () => m.removeEventListener('change', on);
  }, []);
  return phone;
}
