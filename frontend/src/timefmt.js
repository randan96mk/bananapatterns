// 12-hour IST times for display ("6:01 AM", "3:15 PM"). Settings stay HH:MM (24-hour) internally.
const IST = 'Asia/Kolkata';

// "15:15" (or "15:15:30") -> "3:15 PM"; seconds kept when asked.
export function ampm(hm, seconds = false) {
  if (!hm || !/^\d{1,2}:\d{2}/.test(hm)) return hm || '';
  const [h, m, s] = hm.split(':').map(Number);
  return `${h % 12 || 12}:${String(m).padStart(2, '0')}${seconds && s != null ? `:${String(s).padStart(2, '0')}` : ''} `
    + (h < 12 ? 'AM' : 'PM');
}

// Backend timestamps are IST; one without an offset is read as IST, never as the device's own time zone.
const toDate = (iso) => new Date(/[zZ]$|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso.replace(' ', 'T')}+05:30`);

function parts(iso) {
  const p = {};
  new Intl.DateTimeFormat('en-GB', { timeZone: IST, day: 'numeric', month: 'short', hour: '2-digit',
    minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(toDate(iso)).forEach((x) => { p[x.type] = x.value; });
  return p;
}

// ISO timestamp -> "6:30 PM" (IST).
export function clock(iso, seconds = false) {
  if (!iso) return '';
  const p = parts(iso);
  return ampm(`${p.hour}:${p.minute}:${p.second}`, seconds);
}

// ISO timestamp -> "27 Oct, 6:30 PM" (IST).
export function stamp(iso, seconds = false) {
  if (!iso) return '';
  const p = parts(iso);
  return `${p.day} ${p.month}, ${ampm(`${p.hour}:${p.minute}:${p.second}`, seconds)}`;
}
