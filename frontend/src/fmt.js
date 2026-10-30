// One number style everywhere: rupees in the Indian (lakh) format, signed with ▲▼ where it is a gain or loss.
export const inr = (v, d = 0) => (v == null ? '—' : `${v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d })}`);
export const rs = (v) => inr(v, 2);
export const signed = (v) => (v == null ? '—' : `${v > 0 ? '▲ +' : v < 0 ? '▼ −' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`);
export const pct = (v, d = 1) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}%`);
export const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down');
export const lakh = (v) => (v == null ? '—' : `₹${(v / 1e5).toFixed(2).replace(/\.00$/, '')}L`);
