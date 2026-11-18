import React, { useEffect, useState } from 'react';
import { ampm, stamp } from '../timefmt';
import { Head, Chip, Panel, Act } from './ui2';
import * as api from '../services/api';

const HELP = {
  filings_weekend_from: 'Weekend rule for the Filings check: off on Saturday, on Sunday only from this time (24-hour HH:MM, IST)',
  send_gap_s: 'Seconds between two order webhooks at 9:16 (1–30); a 429 back-off adds to it',
  results_guard_sessions: 'Sessions before a results date: the position guard warns on a held stock (events); a planned real BUY that close to its results is skipped for the morning, switchable back on from the Real page or Telegram (real; 0 = off)',
  history_years: 'Backfill depth in years (8 ≈ 2 Kite calls per symbol)',
  hist_rate_per_sec: 'Historical API rate (Kite limit 3/s)',
  max_retries: 'Retries for network / 429 / 5xx errors',
  backoff_base_sec: 'Backoff base seconds (doubles per retry)',
  chunk_days: 'Max days per historical call (Kite: 2000 for day candles)',
  overlap_bars: 'Bars re-read on incremental sync to detect re-adjusted history',
  adjust_tolerance: 'Close mismatch that triggers a full refetch (0.005 = 0.5%)',
  min_bars_warn: 'Flag symbols with fewer bars (young listings)',
  include_series: 'Constituent series to ingest (EQ, BE = trade-for-trade; RR = REITs excluded)',
  universe_urls: 'Nifty 500 constituents CSV sources, tried in order',
  index_symbols: 'Kite index tradingsymbols to ingest (RS / regime)',
  eod_ready_time: "IST time after which today's daily bar is treated as final",
  // bases
  pivot_basis: '"close" = highest close in the base (matches the site) or "high"',
  min_base_bars: 'Minimum base length in sessions (5 = 1 week)',
  max_base_bars: 'Maximum base length in sessions (325 = 65 weeks)',
  max_base_depth: 'Max drop from pivot to base low (0.25 = 25%)',
  max_pivot_drift: 'pivot_basis=high only: max wick drift above the first high before it counts as a trend',
  prior_advance_bars: 'Lookback for the advance into the base',
  min_prior_advance: 'Base high must be this far above the lookback low (0.20 = 20%)',
  reset_ma: 'Close below this SMA restarts the count at B1 (0 = off)',
  reset_on_undercut: 'Base low under the previous base low restarts the count (true/false)',
  vol_baseline: 'Breakout volume rule baseline: "sma50" or "median20"',
  bo_vol_mult: 'Breakout volume must be >= this x baseline to be volume-confirmed',
  weak_close_cir: 'Breakout close-in-range below this = weak close',
  squat_bars: 'Close back under pivot within N sessions after breakout = squat',
  // screen (stage board)
  min_forming_weeks: 'Forming: active base at least this many weeks',
  max_dist_to_pivot: 'Forming: close within this much under the pivot (0.05 = 5%)',
  fresh_sessions: 'Fresh breakouts: broke out within the last N sessions',
  require_volume: 'Fresh/Climbing need a volume-confirmed breakout (true/false)',
  played_out_ma: 'Run ends on a close below this SMA (0 = off)',
  played_out_drop: 'Run ends on a close this far below the pivot (0.08 = 8%)',
  played_out_window: 'Played out: run ended within the last N sessions',
  trend_ma: 'All cards: close above this SMA (0 = off)',
  min_turnover_cr: 'All cards: median daily turnover at least this (Rs crore)',
  turnover_window: 'Sessions for the median turnover',
  // trend (RS + Trend Template)
  rs_weights: 'RS weights for the periods below, one per line (default 0.4, 0.2, 0.2, 0.2)',
  rs_periods: 'RS periods in sessions, one per line (63, 126, 189, 252 = 3/6/9/12 months)',
  rs_min_bars: 'Minimum history for an RS rating',
  ma_fast: 'Fast SMA (50)', ma_mid: 'Mid SMA (150)', ma_slow: 'Slow SMA (200)',
  slow_rising_bars: 'Slow SMA must be higher than this many sessions ago (21 = ~1 month; 63 is stricter)',
  min_above_low52: 'Close at least this far above the 52-week low (0.30 = 30%)',
  max_below_high52: 'Close within this much of the 52-week high (0.25 = 25%)',
  min_rs: 'Minimum RS rating for the Trend Template',
  // vcp
  max_depth: 'VCP: base at most this deep (0.30 = 30%)',
  min_weeks: 'VCP: base at least this many weeks',
  min_contractions: 'VCP: at least this many pullbacks in the base',
  max_contractions: 'VCP: at most this many pullbacks',
  require_shrinking: 'VCP: last pullback <= shrink x the largest earlier one (true/false)',
  shrink: 'Shrink factor for the rule above (0.8)',
  max_final_contraction: 'VCP: last pullback at most this deep (0.12 = 12%)',
  max_tightening: 'VCP: ATR(recent) / base mean true range at most this',
  max_dryup: 'VCP: recent volume / volume before the base at most this',
  min_trend_checks: 'VCP: at least this many of the 8 Trend Template checks',
  atr_recent: 'Sessions for the recent ATR', dryup_recent: 'Sessions for recent volume',
  ud_window: 'Sessions for the up/down volume ratio',
  zz_min_pct: 'ZigZag: minimum reversal to confirm a swing (0.05 = 5%)',
  zz_atr_mult: 'ZigZag: or this many ATR% if larger',
  stop_buffer: 'Stop this far under the last pullback low (0.01 = 1%)',
  max_stop: 'Stop never more than this under the pivot (0.08 = 8%)',
  score_weights: 'Score weights: RS, tightening, dry-up, pivot proximity, 52w-high proximity, industry RS',
  dryup_lookback: 'Sessions before the base used for "base volume vs prior pace"',
};

const SECTIONS = ['real', 'paper', 'futures', 'options', 'ai', 'tips', 'breakout', 'vcp', 'flag', 'screen', 'bases', 'trend', 'market', 'ingest', 'maintenance'];
const NAMES = { tips: '💡 Tips', real: 'Real trading', paper: 'Paper trading', futures: 'Stock futures', options: 'NIFTY options', ai: 'Local AI', breakout: 'Trend Breakout', vcp: 'VCP', flag: 'Bull flag', screen: 'Screens', bases: 'Bases', trend: 'Trend', market: 'Market', ingest: 'Data ingest', maintenance: '🧹 Archive' };
// Set on their own pages (switches, confirms, deposits): not editable here.
const HIDE = { real: ['enabled', 'variant', 'capital'], paper: ['capital', 'variants'], futures: ['long_real', 'short_real', 'roll_mode'], options: ['real'], ai: ['enabled'] };
const HELP2 = { send_time: 'Orders go out from here (24-hour HH:MM, IST)', send_until: '… until here; missed orders are not sent that day', preview_time: 'Morning reminder with the orders ("" = off)',
  max_buys_per_day: 'New BUYs per day at most', results_guard_sessions: 'Pre-skip a BUY whose results fall within this many sessions (0 = off)', max_pos: 'Max open positions · position size = value ÷ this',
  // 💰 fill confirmation (after the 18 Nov incident): no EXIT is ever sent for a BUY whose fill is not confirmed
  tt_sid: 'The webhook side\'s deployment id for the read-only fill check (0 = not set: fills are confirmed by hand only; the API key lives in .env)',
  confirm_time: 'The read-only fill check on trading days (24-hour HH:MM, IST; "" = off); it runs again in the evening job · never sends an order',
  confirm_alert: 'Telegram: the buys the fill check confirmed, and the evening "Real vs webhook side" line (true/false)',
  exit_needs_confirm: 'The EXIT gate (true/false, default false): false = buys and exits run on their own, nothing is asked of you; true = every buy must be confirmed by you (✓ filled / reply filled SYMBOL) before its EXIT can go out: the 9:16 message asks, a 10:00 nudge lists the rest, a stop hit on an unconfirmed buy is held and alerted' };
// 🧩 the options book (paper only; paper / enabled / capital are set on its own page)
const HELP_OPT = { window_start: 'The bias is looked for on 5-minute closes from here (24-hour HH:MM, IST)', window_end: '… to here; no break by then = no trade today',
  hedge_points: 'The bought leg this many points further out than the sold one (200 when its premium is under Rs 2)', stop_pct: 'Stop when the sold premium rises this % over its entry (100 = 2×: sold at ₹60, stop at ₹120; 50 = 1.5×) · compare on Options → Report as if',
  stop_pct_expiry: 'The same stop on expiry day (tighter; 70 = 1.7×)', vwap_exit: 'The VWAP exit: a 5-minute close back through VWAP against the bias closes the spread (true/false; the switch on the Options page sets the same)', be_after_pct: 'Once the sold premium has decayed this %, the stop becomes breakeven', target_pct: 'Target: the spread\'s value down this % from the entry credit',
  time_exit: 'The time exit (24-hour HH:MM, IST); a hard exit follows at 15:20, nothing is held overnight', daily_loss_pct: 'No new entries once the day\'s realised + open loss reaches this % of capital',
  vix_half: 'India VIX over this halves the size (1 lot at least)', pcr_bull: 'Chain score: PCR over this confirms a bullish bias', pcr_bear: '… under this confirms a bearish one (over 1.5 / under 0.6 = crowded, size halves)',
  max_gap_pct: 'No entry when the open gapped more than this % from the previous close', event_days: 'Comma-separated ISO dates with no entries (RBI policy, budget, …), e.g. 2026-12-05, 2027-02-01',
  margin_per_lot: 'The margin estimate shown per lot (rupees)', capital_per_lot: '1 lot per this much capital', max_lots: 'At most this many lots', max_delta: 'The sold strike: the first with |delta| at most this',
  min_premium: 'The sold premium must be at least this (else one strike nearer, delta 0.35 at most)', otm_pct: 'The sold strike at least this % out of the money (50-strike grid)', max_trades_day: 'Trades a day at most: re-entries count, the afternoon pair counts as one (3 = a first entry + two more)',
  reentry_same_bias: 'After a premium / VWAP / breakeven stop: the same bias once more (once per bias a day) on a 5-minute close back over the range and VWAP, the chain still agreeing (true/false; the switch on the Options page sets the same)',
  reentry_late: 'Re-entries (same or flipped bias) until reentry_until instead of window_end; first entries keep window_end (true/false; the switch on the Options page sets the same)',
  reentry_until: 'Re-entries are allowed until here (24-hour HH:MM, IST); a day with a stopped trade stays open for a re-entry until then',
  afternoon_sell: 'The afternoon pair: at afternoon_time, flat, on a quiet day, a call spread above + a put spread below, 1 lot each side, no VWAP exit, one trade (true/false; the switch on the Options page sets the same)',
  afternoon_time: 'When the afternoon pair is looked at, once a day (24-hour HH:MM, IST)', afternoon_max_range_pct: 'The afternoon pair only when the day\'s range so far (high − low since 9:15) is at most this % of spot (and spot within the opening range ± 0.25%)' };
// 🤖 the local AI (the enabled switch lives on its own page: it may go on only once both models are on disk)
const HELP_AI = { enabled: 'The master switch (set on System → AI; needs both models on disk)', night_batch: 'Night work (the night model) from the evening job\'s end until night_end (true/false)',
  day_answers: 'Day answers with the day model only, 9:00–15:40 (true/false)', night_model: 'The night model (Ollama tag, e.g. qwen2.5:7b-instruct, ~4.7 GB); download it from System → AI',
  day_model: 'The day model (Ollama tag, e.g. qwen2.5:3b-instruct, ~1.9 GB); download it from System → AI', timeout_s: 'One model call may take at most this many seconds; longer = the job fails',
  keep_alive: 'How long a model stays loaded after a call (Ollama duration, e.g. 10m; 0 = unload at once)', min_free_ram_gb: 'No AI work while the server\'s free RAM is under this many GB',
  slow_tick_s: '… or while the scheduler\'s last tick took longer than this many seconds (the app comes first)', breaker_failures: 'This many failures in a row open the breaker …',
  breaker_minutes: '… for this many minutes; one success closes it again', night_end: 'Night work stops here (24-hour HH:MM, IST)', max_queue: 'The queue refuses new jobs beyond this many waiting',
  notes_per_night: 'Filing / deal notes a night at most (held names first, then the plan, then the watchlist)', note_days: 'How many days back the notes job reads filings and deals (the first run too)',
  // 🤖 PR 3: suggestions, the trade "why" line, the morning brief, ask the app
  max_suggestions_open: 'Open suggestions at most (1-50); beyond it the night keeps its candidates for another night · each open one expires on its own time',
  suggest_muted: 'Suggestion kinds never born, comma-separated: skip, exit, flag, hold_options, roll_or_exit, backtest (the switches on System → AI → Scorecard set the same list)',
  why: 'The nightly "why" line under every trade closed today (Real, Paper, Futures, Options) and the evening "exits, explained" message (true/false)',
  brief: 'The morning brief on trading days: ≤ 5 lines from the plans, the books and the night\'s notes; one Telegram message, kept as a note (true/false)',
  brief_time: 'When the brief is written (24-hour HH:MM, IST); its window runs to 9:15 AM (before the 9:16 send), the night model, one call',
  ask: '"Ask the app": the day model picks one read-only tool, code runs it, the model answers from its result · any time of day, never on the trading path (true/false)',
  // 🤖 the AI view: a rating beside every system-suggested stock (it rates and explains, never acts)
  view_enabled: 'Rate the plan, held, futures plan, Follow and Screens names every evening with the night model (true/false)',
  view_rate_held: 'Also re-rate the held names: hold / trim / exit-watch (true/false)',
  view_rank_by_score: 'Ties in the plan (same breakout rank) ordered by the AI score · locked until the record beats the plan baseline 4 weeks in a row (true/false)',
  view_size_by_score: 'BUY qty = rule qty × 0.5–1.0 by the AI score, never above the rule size, never a skip · locked the same way (true/false)',
  view_size_floor: 'The smallest size-by-score multiple (0.5 = half the rule size at most off)',
  view_exit_watch_alert: 'An alert when a held name closes under the model\'s "setup fails below" level · a flag only, never an exit · locked the same way (true/false)',
  view_score_buy: 'Verdict buy at or above this score (hold for a held name); set by code from the score, not by the model',
  view_score_avoid: 'Verdict avoid under this score (exit-watch for a held name); in between = wait / trim',
  view_max_per_night: 'Names rated a night at most (plan first, then held, futures, Follow, Screens; ~10 s a name)',
  // 🤖 quiet hours on Telegram: AI messages inside the window are held and sent together at its end (or with the brief)
  quiet_from: 'AI messages on Telegram are held from here (24-hour HH:MM, IST; "" = never held) · stops, orders, Kite login and the brief are never held',
  quiet_to: '… until here; the held AI messages then go out in order, after one "held overnight · n messages" line' };
// 🌙 the one close-of-day Telegram summary across all the books (alerts section)
Object.assign(HELP_AI, {
  day_summary: 'One close-of-day Telegram summary across all the books (Real, Paper E+, Futures, Options, Signal track, AI) on trading days; replaces the separate options and signal-track day lines (true/false)',
  day_summary_time: 'When the day summary is sent (24-hour HH:MM, IST)' });
// 📖 the Week story page (Portfolio › Week)
Object.assign(HELP_AI, {
  week: 'Sunday evening: the local model writes up to three observations about the week from the trade rows; figures are checked by code. Off = the Week page shows the numbers only.' });
Object.assign(HELP, HELP2, HELP_OPT, HELP_AI);
// 💡 Tips (a channel's calls, decoded and scored; a separate paper book, never linked to any strategy). Section-scoped:
// these keys share names with other sections, so they are looked up for the tips section only.
const HELP_TIPS = {
  enabled: 'The Tips module: read the channels, decode the calls, push them to Telegram (true/false; off until the reader is logged in)',
  channels: 'Channels to read, comma-separated (any channel your Telegram account is a member of)',
  capital: 'Starting capital of the Tips paper book (rupees; separate from every other book)',
  lots: 'Lots per taken hint when the post names no size (the Take field starts here)',
  size_mode: 'How a take is sized when the post names no size: "lots" (the lots above) or "rupees" (the amount below)',
  rupees: 'With size_mode rupees: about this much premium per taken hint, in whole lots',
  push_hints: 'Push every new hint to Telegram at once (true/false)',
  push_followups: 'Push the channel\'s follow-ups on a hint as one line each (true/false)',
  auto_follow_channel_exit: 'The paper book follows the channel\'s own exit / SL hit / "exit near cost" by itself and tells you (true/false)',
  intraday_time_stop: 'An intraday hint still open is closed here (24-hour HH:MM, IST); a "when comes" zone never reached is skipped then',
  partial_at_t1: 'Book half at target 1 (true/false)',
  trail_to_cost_after_t1: 'After target 1, move the stop to the entry price (true/false)',
  model_classify: 'Let the local model sort the posts code could not (hint / follow-up / chatter) and link follow-ups (true/false)',
  preview_fallback: 'When the account reader is down, read the channel\'s public preview page every 30 s (true/false)',
  kite_mode: '⚡ Tips → Kite orders: "off" (paper book only), "click" (each leg by your click, each through a confirm), "auto" (arrives in the next update; works as click for now). Real money',
  kite_product: 'The Kite product of these orders: "NRML" (carry forward) or "MIS" (intraday)',
  kite_max_entries_day: 'Kite entry orders a day at most (a rejected one does not count)',
  kite_max_lots: 'Lots per Kite entry order at most',
  kite_loss_cap: 'Realised loss a day from these Kite orders (rupees); at or past it no new entry that day',
  kite_entry_from: 'Kite entries only from this time (24-hour HH:MM, IST; shown 12-hour)',
  kite_entry_until: 'Kite entries only until this time (24-hour HH:MM, IST; exits are never blocked)',
};
// 🧹 archive and vacuum (section-scoped: enabled / day / time are generic names)
const HELP_MAINT = {
  enabled: 'The weekly archive job runs (true/false). The Data page\'s Run now works either way',
  archive_days: 'Rows older than this many days in the log tables (option chain snapshots, tips quotes, AI jobs, job runs, sent alerts) MOVE to data/archive.db; nothing is deleted (at least 30; the options replay reads up to 90)',
  vacuum: 'Compact the live database after the move so the space goes back to the disk (true/false; needs free disk about the size of the database)',
  day: 'Weekday the archive runs (monday … sunday)',
  time: 'Time the archive runs on that day (24-hour HH:MM, IST); a missed run catches up later the same day',
};
const HELP_SEC = { tips: HELP_TIPS, maintenance: HELP_MAINT };

// Under 760 px the three columns stack (the same breakpoint redesign.css uses).
function useNarrow() {
  const [n, setN] = useState(() => window.matchMedia('(max-width: 760px)').matches);
  useEffect(() => { const m = window.matchMedia('(max-width: 760px)'); const on = () => setN(m.matches); m.addEventListener('change', on); return () => m.removeEventListener('change', on); }, []);
  return n;
}

// Generic editor for persisted config sections: a list of sections, the selected one's fields, the recent changes.
export default function SettingsPanel() {
  const [section, setSection] = useState(SECTIONS[0]);
  const [vals, setVals] = useState(null);
  const [draft, setDraft] = useState({});
  const [msg, setMsg] = useState('');
  const [changes, setChanges] = useState([]);
  const narrow = useNarrow();
  useEffect(() => { api.configChanges().then((x) => setChanges(x.changes || [])).catch(() => {}); }, [msg]);

  useEffect(() => {
    setVals(null); setMsg('');
    api.getConfig(section).then((v) => { setVals(v); setDraft(v); }).catch((e) => setMsg(e.message));
  }, [section]);

  const save = async () => {
    const changed = {};
    Object.keys(draft).forEach((k) => {
      if (JSON.stringify(draft[k]) !== JSON.stringify(vals[k])) changed[k] = draft[k];
    });
    try { const v = await api.putConfig(section, changed); setVals(v); setDraft(v); setMsg('Saved'); } catch (e) { setMsg(e.message); }
  };
  const dirty = vals ? Object.keys(draft).some((k) => JSON.stringify(draft[k]) !== JSON.stringify(vals[k])) : false;

  return (
    <div className="rd">
      <Head title="Settings" right={<><span className="msg">{msg}</span><button className="btn" disabled={!vals || !dirty} onClick={save}>Save</button></>}>
        <Chip>{NAMES[section]} · <b>{vals ? Object.keys(vals).length : '…'}</b> fields</Chip>
        {!['ingest', 'maintenance'].includes(section) && <Chip title="Charts update when reopened">after saving, press Rescan on the Screens tab</Chip>}
      </Head>
      <div className="two" style={narrow ? undefined : { gridTemplateColumns: '220px minmax(0, 1fr) 380px' }}>
        <Panel title="Sections">
          {SECTIONS.map((x) => <div key={x} style={x === section ? { background: 'var(--hover)', boxShadow: 'inset 3px 0 0 var(--accent)' } : undefined}>
            <Act icon={x === section ? '›' : '·'} cls={x === section ? 'act' : ''} text={NAMES[x]} meta={x} onClick={() => setSection(x)} /></div>)}
        </Panel>
        <Panel title={NAMES[section]} meta={vals ? Object.keys(vals).length : null} right={dirty ? <span className="warn">unsaved changes</span> : 'saved to config'}>
          {!vals ? <p className="muted small rempty">{msg || 'Loading…'}</p>
            : <div className="rform">{Object.entries(draft).filter(([k]) => !(HIDE[section] || []).includes(k)).map(([k, v]) => (
              <div key={k} className="fr">
                <div><code>{k}</code><small>{HELP_SEC[section]?.[k] || HELP[k] || ''}{/^\d{1,2}:\d{2}$/.test(String(v)) && ` (= ${ampm(String(v))} IST)`}</small></div>
                <div style={{ textAlign: 'right' }}>{Array.isArray(v)
                  ? <textarea rows={Math.min(4, v.length || 1)} style={{ width: 220, maxWidth: '100%', fontSize: 11, textAlign: 'left' }} value={v.join('\n')}
                      onChange={(e) => setDraft({ ...draft, [k]: e.target.value.split('\n').filter(Boolean) })} />
                  : <input className="mono" value={String(v)} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />}</div>
              </div>))}</div>}
          <div className="rwrap"><button className="btn small" disabled={!vals || !dirty} onClick={save}>Save</button><span className="msg">{msg}</span></div>
        </Panel>
        <Panel title="Recent changes" meta={changes.length || null}>
          {!changes.length && <p className="muted small rempty">No settings changed yet.</p>}
          {changes.slice(0, 30).map((c, i) => <Act key={i} icon="⚙" text={<><b>{c.section}</b> · {c.what.join(' · ')}</>} meta={stamp(c.at)} />)}
        </Panel>
      </div>
    </div>
  );
}
