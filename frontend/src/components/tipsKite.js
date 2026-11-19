// ⚡ Tips → Kite: the words every confirm uses, shared by Research › Tips (TipsPage.jsx) and Home's "💡 Tips · Kite" block
// (HomePage.jsx), so both ask exactly the same question before the same API call.
import { inr } from '../fmt';

const isNum = (v) => v != null && v !== '' && !Number.isNaN(Number(v));
const px = (v) => (isNum(v) ? Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—');
const plural = (n, w) => `${n} ${w}${Number(n) === 1 ? '' : 's'}`;

export const KITE_WARN = 'Real money on your Kite account. The app never averages, never re-enters after a stop, and stops for the day at the loss cap.';
export const DRY_WARN = 'DRY RUN · nothing reaches Kite. A simulator fills from live quotes; the caps count the dry run only.';
export const DRY_NOTE = 'dry run: same buttons, same legs · a simulator fills from live quotes · nothing reaches Kite';
export const KMODES = [['off', 'Off', 'paper book only · nothing reaches Kite'], ['dry', 'Dry run', DRY_NOTE], ['click', 'By my click', 'you press every button · each one confirms first'], ['auto', 'Auto', "auto: entries armed on the level / the channel's activation · SL + T1 on the fill · the channel's exits followed · you are told"]];
export const kmodeNote = (m) => (KMODES.find((x) => x[0] === m) || KMODES[0])[2];
export const hintName = (h) => h?.title || [h?.side, h?.symbol, h?.strike, h?.opt_type].filter((x) => x != null && x !== '').join(' ') || h?.tradingsymbol || 'hint';

// The mode segment's confirm (Off / Dry run / By my click / Auto).
export function modeConfirm(m) {
  const label = (KMODES.find((x) => x[0] === m) || [])[1] || m;
  const why = m === 'off' ? 'Nothing more is sent to Kite from the Tips. Orders already on Kite stay there: exit or cancel them from the cards.'
    : m === 'auto' ? "The app arms every placeable hint by itself: a level rests on Kite (SL-M trigger / LIMIT) or is watched (an index level); wait words alone wait for the channel's activation. SL + T1 go on the fill; the channel's exit / SL / trail / book part are followed on the app's own positions; one Telegram line each. Never averages; one Kite position at a time (kite_auto_max_open). The kill switch, the caps, the window and the loss cap hold as always."
      : m === 'dry' ? 'A rehearsal: the same buttons and legs, each through its confirm, but a simulator fills them from live quotes (outside market hours a MARKET order fills at the last price). The caps count the dry run only. A hint you trade dry is not traded real the same day.'
        : 'The cards get Kite legs: every order is your click, each through a confirm. Place all = entry now, the stop and T1 the moment it fills.';
  return `Kite orders: ${label}?\n\n${why}\n\n${m === 'dry' ? DRY_WARN : KITE_WARN}`;
}

// The kill switch's confirm.
export const killConfirm = (on) => (on ? 'Turn the Kite kill switch ON?\n\nEvery working Tips order on Kite is cancelled and every open quantity is exited at market now. No new order until you turn it off.'
  : 'Turn the Kite kill switch off?\n\nNew Tips orders are allowed again (each still your click). Nothing is placed by turning it off.');

export const legLine = (lg) => (lg ? `${lg.type_text} ×${px(lg.qty)}` : '');

// One hint's Kite confirms: exit, cancel a leg, send an armed entry now, move the stop (to cost or a price).
// `h` carries id, title, tradingsymbol, exchange, ltp and kite (GET /api/tips or GET /api/home/tips); `kinfo` the product.
export function kiteTexts(h, kinfo) {
  const k = h?.kite || {};
  const W = k.sim ? DRY_WARN : KITE_WARN;               // a dry run's confirms say so in place of the real-money warning
  const to = k.sim ? 'the dry run' : 'Kite';
  const L = Object.fromEntries((Array.isArray(k.legs) ? k.legs : []).map((x) => [x.leg, x]));
  const id = h?.id;
  const name = hintName(h);
  const contract = `${h?.tradingsymbol || '—'} · ${h?.exchange || ''} · ${plural(px(k.lots), 'lot')} = ${px(k.qty)} · ${kinfo?.product || 'NRML'}${isNum(h?.ltp) ? ` · last ${px(h.ltp)}` : ''}`;
  const head = (t) => `${k.sim ? 'DRY RUN · ' : ''}⚡ ${t} · #${id} ${name}\n\ncontract   ${contract}\n`;
  return {
    W, to, L, id, name, contract, head,
    exit: `⚡ Exit #${id} ${name} on Kite now?\n\n${k.state === 'armed' ? 'The entry has not filled: it is cancelled.' : `The working stop and targets are cancelled, then ${L.stop?.side || 'SELL'} ${px(k.qty_open)} MARKET.`}\n\n${W}`,
    cancel: (leg) => `Cancel the ${L[leg]?.name || leg} of #${id} on Kite?\n\norder ${L[leg]?.order_id || '—'} · ${legLine(L[leg])}\n\nNothing else changes.`,
    sendNow: `${head('Send the armed entry now')}order   ${L.entry?.side} ${px(L.entry?.qty)} ${L.entry?.type_text || 'MARKET'}\n\nSkips the channel's activation: the entry goes to Kite now (every guard is checked again).\n\n${W}`,
    moveStop: (dest) => (dest === 'cost' ? `Move the Kite stop of #${id} to cost?\n\nSL-M ${px(L.stop?.trigger)} → ${px(k.entry_px)} (the entry fill) ×${px(L.stop?.qty)}\n\n${W}`
      : `Move the Kite stop of #${id} to ${px(dest)}?\n\nSL-M ${px(L.stop?.trigger)} → ${px(dest)} ×${px(L.stop?.qty)}\n\n${W}`),
  };
}

export const kiteMoney = (v) => (!isNum(v) ? '—' : Number(v) > 0 ? `+${inr(Number(v))}` : inr(Number(v)));
