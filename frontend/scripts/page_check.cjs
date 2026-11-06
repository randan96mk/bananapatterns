// Layout check for one page of the real app: node frontend/scripts/page_check.cjs http://127.0.0.1:8000 research/follow out.png [1600|390]
// Fails (exit 1) on: horizontal page overflow, card parts overlapping or spilling, clipped table cells / headers,
// anything spilling out of its panel, page errors, and any card / row / panel whose size changes when every number
// on the page is swapped for a much longer one (the "layout must not move with values" rule).
const { chromium } = require('playwright');
const [base, route, out = 'page.png', width = '1600'] = process.argv.slice(2);
const CHECK = () => {
  const bad = []; const de = document.documentElement; const R = (e) => e.getBoundingClientRect();
  if (de.scrollWidth > de.clientWidth + 1) bad.push(`page overflow ${de.scrollWidth} > ${de.clientWidth}`);
  const ov = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
  const shown = (e) => getComputedStyle(e).display !== 'none' && R(e).width > 0;
  document.querySelectorAll('.hcard').forEach((k) => {
    const parts = [...k.querySelectorAll(':scope > .l, :scope > .vrow, :scope > .s, :scope > .bl, :scope > .hspark')].filter(shown); const kr = R(k);
    parts.forEach((p) => { const r = R(p); if (r.right > kr.right + 1 || r.bottom > kr.bottom + 1) bad.push(`card part spills: ${p.className} "${p.textContent.slice(0, 30)}"`);
      if (innerWidth > 760 && p.scrollWidth > p.clientWidth + 2 && !p.classList.contains('hspark')) bad.push(`card text clipped: "${p.textContent.slice(0, 40)}"`); });
    for (let i = 0; i < parts.length; i++) for (let j = i + 1; j < parts.length; j++) if (ov(R(parts[i]), R(parts[j]))) bad.push(`card overlap: ${parts[i].className} × ${parts[j].className} in "${k.querySelector('.l')?.textContent}"`);
  });
  document.querySelectorAll('.rd .ftab td, .rd .ftab th, .rd .rbox .t, .rd .rbox .d, .rd .ph, .rd .rkv div').forEach((e) => { if (shown(e) && e.scrollWidth > e.clientWidth + 2) bad.push(`clipped: "${e.textContent.trim().slice(0, 40)}"`); });
  document.querySelectorAll('.rd .rpanel, .rd .hcard, .rd .rbox, .rd .rsc').forEach((p) => { const pr = R(p); [...p.querySelectorAll('*')].forEach((c) => {
    const r = R(c), q = R(c.parentElement); const pos = getComputedStyle(c).position;
    if (r.width && (r.right > pr.right + 2 || r.left < pr.left - 2) && !(q.right > pr.right + 2 || q.left < pr.left - 2) && pos !== 'fixed' && pos !== 'absolute') bad.push(`spill: ${c.tagName}.${c.className} out of ${p.className.split(' ')[0]} "${c.textContent.trim().slice(0, 30)}"`); }); });
  return [...new Set(bad)];
};
const DRIFT = () => {
  const sel = '.rd .hcard, .rd .ftab tr, .rd .rbox, .rd .rpanel, .rd .rsc, .rd .ract'; const els = [...document.querySelectorAll(sel)];
  const snap = () => els.map((e) => { const r = e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; });
  const before = snap();
  document.querySelectorAll('.rd .mono, .rd .num, .rd td.n, .rd .hcard .v').forEach((e) => { if (!e.children.length && /\d/.test(e.textContent)) e.textContent = e.textContent.replace(/[\d,.]+/g, '1,23,45,678.90'); });
  const after = snap();
  return [...new Set(before.map((b, i) => (b[0] !== after[i][0] || b[1] !== after[i][1] ? `size drift: ${els[i].className.split(' ').slice(0, 2).join('.')} ${b} → ${after[i]} "${els[i].textContent.trim().slice(0, 30)}"` : null)).filter(Boolean))];
};
(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: +width, height: 900 }, deviceScaleFactor: 1 });
  const errs = []; p.on('pageerror', (e) => errs.push(e.message.split('\n')[0]));
  await p.goto(`${base}/#/${route}`, { waitUntil: 'domcontentloaded' }); await p.waitForTimeout(2500);
  const bad = await p.evaluate(CHECK); await p.screenshot({ path: out, fullPage: true });
  const drift = await p.evaluate(DRIFT);
  const all = [...errs.map((e) => `page error: ${e}`), ...bad, ...drift];
  console.log(all.length ? all.join('\n') : `PAGE LAYOUT OK ${route} @${width}`); await b.close(); process.exit(all.length ? 1 : 0);
})();
